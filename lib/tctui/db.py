"""SQLite state store shared by the daemon, the workers and the UI.

WAL mode allows the UI to read while the daemon writes.  Every write goes
through a short busy-timeout retry so a blocked reader can never crash a job.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

from . import util

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch TEXT,
  src TEXT NOT NULL,
  src_size INTEGER DEFAULT 0,
  out TEXT,
  out_ext TEXT,
  merge_files TEXT,
  gpu INTEGER,
  gpu_uuid TEXT,
  status TEXT NOT NULL DEFAULT 'queued',
  phase TEXT,
  pid INTEGER,
  rid TEXT,
  created REAL,
  started REAL,
  finished REAL,
  bytes_in INTEGER,
  bytes_out INTEGER,
  savings_pct REAL,
  progress REAL DEFAULT 0,
  fps REAL,
  speed REAL,
  eta REAL,
  attempts INTEGER DEFAULT 0,
  gpu_attempts INTEGER DEFAULT 0,
  exit_code INTEGER,
  error TEXT,
  log_path TEXT,
  width INTEGER,
  height INTEGER,
  duration REAL,
  vcodec TEXT,
  audio_codecs TEXT,
  meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_started ON jobs(started);

CREATE TABLE IF NOT EXISTS processed (
  rel TEXT PRIMARY KEY,
  src TEXT,
  when_ REAL,
  outcome TEXT
);

CREATE TABLE IF NOT EXISTS media_cache (
  path TEXT PRIMARY KEY,
  size INTEGER,
  mtime REAL,
  info TEXT,
  when_ REAL
);

CREATE TABLE IF NOT EXISTS gpu_samples (
  ts REAL, gpu INTEGER, util REAL, mem_used REAL, temp REAL, power REAL,
  enc INTEGER, dec INTEGER
);
CREATE INDEX IF NOT EXISTS idx_gpu_samples ON gpu_samples(gpu, ts);

CREATE TABLE IF NOT EXISTS fleet_samples (
  ts REAL, running INTEGER, queued INTEGER, fps REAL, speed REAL
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, level TEXT, msg TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS commands (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, cmd TEXT, args TEXT, done INTEGER DEFAULT 0, result TEXT
);

CREATE TABLE IF NOT EXISTS kv (
  k TEXT PRIMARY KEY, v TEXT
);
"""


class DB:
    def __init__(self, path, readonly=False):
        self.path = path
        self.readonly = readonly
        if not readonly:
            util.ensure_dir(os.path.dirname(path))
        self.lock = threading.RLock()
        self.conn = self._connect()

    def _connect(self):
        if self.readonly:
            uri = "file:%s?mode=ro" % self.path.replace("?", "%3f")
            conn = sqlite3.connect(uri, uri=True, timeout=5.0, check_same_thread=False)
        else:
            conn = sqlite3.connect(self.path, timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            if not self.readonly:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass
        return conn

    def init(self):
        if self.readonly:
            return self
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        return self

    def close(self):
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    # ------------------------------------------------------------- retry wrapper
    def execute(self, sql, params=()):
        last = None
        with self.lock:
            for _ in range(6):
                try:
                    cur = self.conn.execute(sql, params)
                    self.conn.commit()
                    return cur
                except sqlite3.OperationalError as exc:
                    last = exc
                    if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                        time.sleep(0.15)
                        continue
                    raise
        raise last

    def query(self, sql, params=()):
        with self.lock:
            cur = self.conn.execute(sql, params)
            return cur.fetchall()

    def query_one(self, sql, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------- jobs
    def add_job(self, job):
        now = util.now()
        meta = job.get("meta") or {}
        if not isinstance(meta, str):
            meta = json.dumps(meta, ensure_ascii=False)
        cur = self.execute(
            """INSERT INTO jobs(batch, src, src_size, out, out_ext, merge_files, status, created,
                                width, height, duration, vcodec, audio_codecs, meta)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job.get("batch"), job["src"], int(job.get("src_size") or 0), job.get("out"),
             job.get("out_ext"), json.dumps(job.get("merge_files") or [], ensure_ascii=False),
             "queued", now, job.get("width"), job.get("height"), job.get("duration"),
             job.get("vcodec"), json.dumps(job.get("audio_codecs") or []), meta))
        return cur.lastrowid

    def update_job(self, job_id, **fields):
        if not fields:
            return
        cols = []
        vals = []
        for k, v in fields.items():
            cols.append("%s=?" % k)
            vals.append(v)
        vals.append(job_id)
        self.execute("UPDATE jobs SET %s WHERE id=?" % ",".join(cols), vals)

    def get_job(self, job_id, conn=None):
        row = (conn or self).query_one("SELECT * FROM jobs WHERE id=?", (job_id,))
        return dict(row) if row else None

    def jobs(self, statuses=None, limit=300, order="id ASC"):
        if statuses:
            marks = ",".join("?" for _ in statuses)
            sql = "SELECT * FROM jobs WHERE status IN (%s) ORDER BY %s LIMIT ?" % (marks, order)
            return [dict(r) for r in self.query(sql, tuple(statuses) + (limit,))]
        return [dict(r) for r in self.query("SELECT * FROM jobs ORDER BY %s LIMIT ?" % order, (limit,))]

    def queued(self, limit=200):
        return [dict(r) for r in self.query(
            "SELECT * FROM jobs WHERE status='queued' ORDER BY created ASC, id ASC LIMIT ?", (limit,))]

    def active(self):
        return [dict(r) for r in self.query(
            "SELECT * FROM jobs WHERE status IN ('running','verifying','staged') ORDER BY started ASC")]

    def counts(self):
        rows = self.query("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    def totals(self):
        row = self.query_one("""SELECT
            COUNT(*) AS total,
            COALESCE(SUM(CASE WHEN status='done' THEN bytes_in - bytes_out ELSE 0 END),0) AS saved,
            COALESCE(SUM(CASE WHEN status='done' THEN bytes_in ELSE 0 END),0) AS bytes_in,
            COALESCE(SUM(CASE WHEN status='done' THEN bytes_out ELSE 0 END),0) AS bytes_out,
            COALESCE(AVG(CASE WHEN status='done' THEN speed END),0) AS avg_speed
            FROM jobs""")
        return dict(row) if row else {"total": 0, "saved": 0, "bytes_in": 0, "bytes_out": 0, "avg_speed": 0}

    def running_fps(self):
        row = self.query_one("SELECT COALESCE(SUM(fps),0) AS fps, COUNT(*) AS n FROM jobs WHERE status='running'")
        return (row["fps"], row["n"]) if row else (0, 0)

    def per_gpu_active(self):
        rows = self.query("""SELECT gpu, COUNT(*) AS n FROM jobs
                             WHERE status IN ('running','verifying') AND gpu IS NOT NULL GROUP BY gpu""")
        return {int(r["gpu"]): r["n"] for r in rows}

    def queue_media_seconds(self):
        row = self.query_one("SELECT COALESCE(SUM(duration),0) AS d FROM jobs WHERE status='queued'")
        return float(row["d"]) if row else 0.0

    def recent_done(self, limit=20):
        return [dict(r) for r in self.query(
            "SELECT * FROM jobs WHERE status IN ('done','kept','failed') ORDER BY finished DESC LIMIT ?",
            (limit,))]

    def cancel_request(self, job_id):
        self.update_job(job_id, status="canceled", finished=util.now())

    # ------------------------------------------------------------- queue control
    def clear_queue(self, mark_skipped=False):
        """Cancel every queued job.  Running jobs are untouched."""
        rows = self.query("SELECT id, src, merge_files FROM jobs WHERE status='queued'")
        if not rows:
            return 0
        now = util.now()
        self.execute("UPDATE jobs SET status='canceled', finished=?, error=? WHERE status='queued'",
                     (now, "清空队列"))
        if mark_skipped:
            for r in rows:
                paths = [r["src"]]
                try:
                    paths += json.loads(r["merge_files"] or "[]")
                except (TypeError, ValueError):
                    pass
                for p in paths:
                    self.mark_processed(os.path.abspath(p), p, "skipped")
        return len(rows)

    def clear_finished(self):
        cur = self.execute("""DELETE FROM jobs WHERE status IN
                              ('done','kept','failed','canceled','interrupted')""")
        return cur.rowcount

    # ------------------------------------------------------------- processed
    def is_processed(self, rel, outcomes=("done", "kept", "skipped")):
        marks = ",".join("?" for _ in outcomes)
        row = self.query_one("SELECT 1 FROM processed WHERE rel=? AND outcome IN (%s)" % marks,
                             (rel,) + tuple(outcomes))
        return row is not None

    def mark_processed(self, rel, src, outcome):
        self.execute("INSERT OR REPLACE INTO processed(rel, src, when_, outcome) VALUES(?,?,?,?)",
                     (rel, src, util.now(), outcome))

    def processed_count(self):
        row = self.query_one("SELECT COUNT(*) AS n FROM processed")
        return int(row["n"]) if row else 0

    def clear_processed(self, path=None):
        if path:
            self.execute("DELETE FROM processed WHERE rel=? OR src=?", (path, path))
        else:
            self.execute("DELETE FROM processed")

    # ------------------------------------------------------------- media cache
    def media_cached(self, path, size, mtime):
        row = self.query_one("SELECT info FROM media_cache WHERE path=?", (path,))
        if not row:
            return None
        try:
            info = json.loads(row["info"])
        except (TypeError, ValueError):
            return None
        if int(info.get("_size") or 0) != int(size) or abs(float(info.get("_mtime") or 0) - float(mtime)) > 1:
            return None
        return info

    def media_put(self, path, size, mtime, info):
        payload = dict(info)
        payload["_size"] = size
        payload["_mtime"] = mtime
        self.execute("INSERT OR REPLACE INTO media_cache(path,size,mtime,info,when_) VALUES(?,?,?,?,?)",
                     (path, int(size), float(mtime), json.dumps(payload, ensure_ascii=False), util.now()))

    # ------------------------------------------------------------- samples
    def add_gpu_sample(self, ts, gpu, util_, mem_used, temp, power, enc, dec):
        self.execute("INSERT INTO gpu_samples(ts,gpu,util,mem_used,temp,power,enc,dec) VALUES(?,?,?,?,?,?,?,?)",
                     (ts, gpu, util_, mem_used, temp, power, enc, dec))

    def gpu_history(self, gpu, seconds=120, max_points=120):
        since = util.now() - seconds
        rows = self.query("SELECT ts,util,mem_used,enc,dec FROM gpu_samples WHERE gpu=? AND ts>=? ORDER BY ts",
                          (gpu, since))
        if len(rows) > max_points:
            step = max(1, len(rows) // max_points)
            rows = rows[::step]
        return [dict(r) for r in rows]

    def add_fleet_sample(self, ts, running, queued, fps, speed):
        self.execute("INSERT INTO fleet_samples(ts,running,queued,fps,speed) VALUES(?,?,?,?,?)",
                     (ts, running, queued, fps, speed))

    def fleet_history(self, seconds=600, max_points=120):
        since = util.now() - seconds
        rows = self.query("SELECT ts,running,queued,fps,speed FROM fleet_samples WHERE ts>=? ORDER BY ts",
                          (since,))
        if len(rows) > max_points:
            step = max(1, len(rows) // max_points)
            rows = rows[::step]
        return [dict(r) for r in rows]

    def prune(self, history_hours=2):
        cutoff = util.now() - float(history_hours) * 3600
        self.execute("DELETE FROM gpu_samples WHERE ts<?", (cutoff,))
        self.execute("DELETE FROM fleet_samples WHERE ts<?", (cutoff,))
        self.execute("DELETE FROM events WHERE ts<?", (util.now() - 24 * 3600,))

    # ------------------------------------------------------------- events
    def event(self, level, msg):
        self.execute("INSERT INTO events(ts,level,msg) VALUES(?,?,?)", (util.now(), level, msg))

    def recent_events(self, limit=100):
        return [dict(r) for r in self.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    # ------------------------------------------------------------- commands
    def push_command(self, cmd, args=None):
        return self.execute("INSERT INTO commands(ts,cmd,args,done) VALUES(?,?,?,0)",
                            (util.now(), cmd, json.dumps(args or {}, ensure_ascii=False))).lastrowid

    def pending_commands(self):
        return [dict(r) for r in self.query("SELECT * FROM commands WHERE done=0 ORDER BY id ASC LIMIT 50")]

    def complete_command(self, cmd_id, result=""):
        self.execute("UPDATE commands SET done=1, result=? WHERE id=?", (str(result)[:500], cmd_id))

    # ------------------------------------------------------------- kv
    def kv_get(self, k, default=None):
        row = self.query_one("SELECT v FROM kv WHERE k=?", (k,))
        return row["v"] if row else default

    def kv_set(self, k, v):
        self.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, str(v)))
