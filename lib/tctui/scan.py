"""Scanning, probing and planning.

The planner is where the old shell script's implicit decisions become explicit:
 * which files are skipped (codec, tag, size, already done),
 * how episode groups are detected and merged,
 * what the output path will be.

The planner never touches files: it only produces job rows.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time

from . import util

HDR_TRANSFERS = ("smpte2084", "arib-std-b67")


def _ffprobe_json(cfg, path, timeout=30):
    cmd = [cfg.ffprobe, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout.decode("utf-8", "replace"))
    except ValueError:
        return None


def _fps(rate):
    try:
        if not rate or rate in ("0/0", "N/A"):
            return None
        num, _, den = str(rate).partition("/")
        den = float(den or 1)
        if den == 0:
            return None
        return round(float(num) / den, 3)
    except (TypeError, ValueError):
        return None


def probe_media(cfg, path, db=None):
    """Return a normalized media dict (cached by size+mtime when db is given)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if db is not None:
        cached = db.media_cached(path, st.st_size, st.st_mtime)
        if cached:
            cached.pop("_size", None)
            cached.pop("_mtime", None)
            return cached
    data = _ffprobe_json(cfg, path)
    if not data:
        return None
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    vstreams = [s for s in streams if s.get("codec_type") == "video"
                and not (s.get("disposition") or {}).get("attached_pic")]
    astreams = [s for s in streams if s.get("codec_type") == "audio"]
    sstreams = [s for s in streams if s.get("codec_type") == "subtitle"]
    if not vstreams:
        info = {"path": path, "size": st.st_size, "mtime": st.st_mtime, "has_video": False}
        if db is not None:
            db.media_put(path, st.st_size, st.st_mtime, info)
        return info
    v = vstreams[0]
    tags = fmt.get("tags") or {}
    duration = None
    for cand in (fmt.get("duration"), v.get("duration")):
        try:
            duration = float(cand)
            break
        except (TypeError, ValueError):
            continue
    pix_fmt = v.get("pix_fmt") or ""

    def _color(value):
        value = (value or "").lower()
        return "" if value in ("unknown", "reserved", "n/a") else value

    transfer = _color(v.get("color_transfer"))
    primaries = _color(v.get("color_primaries"))
    hdr = transfer in HDR_TRANSFERS or pix_fmt in ("p010le", "yuv420p10le", "yuv420p12le")
    info = {
        "path": path,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "has_video": True,
        "vcodec": (v.get("codec_name") or "").lower(),
        "profile": v.get("profile") or "",
        "pix_fmt": pix_fmt,
        "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0),
        "duration": duration,
        "fps": _fps(v.get("avg_frame_rate")) or _fps(v.get("r_frame_rate")),
        "bitrate": int(float(fmt.get("bit_rate") or 0)) if fmt.get("bit_rate") else None,
        "nb_frames": int(v.get("nb_frames") or 0) if str(v.get("nb_frames", "")).isdigit() else None,
        "format": fmt.get("format_name") or "",
        "comment": tags.get("comment") or "",
        "title": tags.get("title") or "",
        "audio_codecs": sorted({(s.get("codec_name") or "").lower() for s in astreams}),
        "audio_count": len(astreams),
        "sub_codecs": sorted({(s.get("codec_name") or "").lower() for s in sstreams}),
        "sub_count": len(sstreams),
        "color_transfer": transfer,
        "color_primaries": primaries,
        "hdr": hdr,
        "bit_depth": 10 if ("10" in pix_fmt or pix_fmt == "p010le") else (12 if "12" in pix_fmt else 8),
    }
    if db is not None:
        db.media_put(path, st.st_size, st.st_mtime, info)
    return info


# ------------------------------------------------------------------ scanning


def iter_video_files(cfg, roots):
    exts = {e.lower().lstrip(".") for e in (cfg.get("scan.exts") or [])}
    excl = set((cfg.get("scan.exclude_dirs") or []))
    excl.add(".tctui")
    state_dir = os.path.abspath(cfg.state_dir)
    follow = bool(cfg.get("scan.follow_symlinks"))
    max_files = int(cfg.get("scan.max_files_per_scan") or 0)
    count = 0
    seen = set()
    for root in roots:
        root = os.path.abspath(os.path.expanduser(root))
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=follow):
            dirnames[:] = [d for d in dirnames if d not in excl and not d.startswith(".tctui")]
            if os.path.abspath(dirpath) == state_dir:
                dirnames[:] = []
                continue
            for name in filenames:
                if name.startswith(".tctui-") or name.startswith("."):
                    continue
                stem, dot, ext = name.rpartition(".")
                if not dot or ext.lower() not in exts:
                    continue
                path = os.path.join(dirpath, name)
                if path in seen:
                    continue
                seen.add(path)
                count += 1
                if max_files and count > max_files:
                    return
                yield path


SERIES_RE_DEFAULT = r"^(?P<base>.+?)[._\- ]?(?P<num>\d{1,4})$"


def series_key(path, cfg):
    """Return (dir, base, num) if the stem looks like an episode, else None."""
    stem = os.path.splitext(os.path.basename(path))[0]
    pattern = cfg.get("series.pattern") or SERIES_RE_DEFAULT
    try:
        m = re.match(pattern, stem)
    except re.error:
        m = re.match(SERIES_RE_DEFAULT, stem)
    if not m:
        return None
    base = (m.groupdict().get("base") or "").strip(" ._-")
    num = m.groupdict().get("num")
    if not base or num is None:
        return None
    # a bare number (e.g. "2024") is not an episode marker
    if not base:
        return None
    try:
        num_i = int(num)
    except ValueError:
        return None
    return (os.path.dirname(path), base, num_i)


def group_series(paths, cfg):
    """Group episode-like paths.  Returns [(dir, base, [paths sorted by num])]."""
    buckets = {}
    for p in paths:
        key = series_key(p, cfg)
        if not key:
            continue
        d, base, num = key
        buckets.setdefault((d, base), []).append((num, p))
    groups = []
    min_files = int(cfg.get("series.min_files") or 2)
    for (d, base), items in buckets.items():
        items = sorted(set(items))
        if len(items) >= min_files:
            groups.append((d, base, [p for _, p in items]))
    return groups


# ------------------------------------------------------------------ naming


def safe_target_path(src, out_ext, taken=()):
    """Where the transcoded file will land (never overwrites an existing file)."""
    d = os.path.dirname(src)
    stem = os.path.splitext(os.path.basename(src))[0]
    out_ext = out_ext.lstrip(".")
    candidate = os.path.join(d, "%s.%s" % (stem, out_ext))
    if candidate == src:
        return candidate
    n = 1
    while os.path.exists(candidate) or candidate in taken:
        candidate = os.path.join(d, "%s_%d.%s" % (stem, n, out_ext))
        n += 1
    return candidate


# ------------------------------------------------------------------ planning


class Planner:
    """Turns files into job dicts, applying every policy in config['scan']/['series']."""

    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        self.skips = []

    def _skip(self, path, reason):
        self.skips.append({"path": path, "reason": reason})
        self.db.event("info", "skip %s: %s" % (path, reason))

    def import_legacy_log(self, roots):
        """Import the old script's video_transcode_log.txt files (relative paths)."""
        if not self.cfg.get("scan.import_legacy_log", True):
            return 0
        name = self.cfg.get("scan.legacy_log_name") or "video_transcode_log.txt"
        imported = 0
        for root in roots:
            log = os.path.join(root, name)
            if not os.path.isfile(log):
                continue
            try:
                with open(log, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        rel = line.strip()
                        if not rel:
                            continue
                        full = os.path.join(root, rel)
                        if self.db.is_processed(full):
                            continue
                        self.db.mark_processed(full, full, "done")
                        imported += 1
            except OSError:
                continue
        if imported:
            self.db.event("info", "从旧脚本日志导入 %d 条已处理记录" % imported)
        return imported

    def active_paths(self):
        paths = set()
        for job in self.db.jobs(statuses=("queued", "running", "verifying", "staged"), limit=1000):
            paths.add(job["src"])
            try:
                for p in json.loads(job.get("merge_files") or "[]"):
                    paths.add(p)
            except (TypeError, ValueError):
                pass
        return paths

    def plan(self, roots, progress=None):
        """Scan + classify + build jobs.  Returns list of job dicts (not yet inserted)."""
        cfg = self.cfg
        started = time.time()
        self.skips = []
        tag = cfg.get("encode.tag") or "Transcoded By HServer"
        skip_codecs = {c.lower() for c in (cfg.get("scan.skip_if_codec") or [])}
        min_size = int(cfg.get("scan.min_size_mb") or 0) * 1024 * 1024
        ext_out = cfg.get("encode.out_ext") or "mkv"
        self.import_legacy_log(roots)
        active = self.active_paths()
        candidates = []
        scanned = 0
        for path in iter_video_files(cfg, roots):
            scanned += 1
            if progress and scanned % 25 == 0:
                progress("scanning: %d files" % scanned)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size < min_size:
                continue
            if os.path.abspath(path) in active:
                continue
            if self.db.is_processed(os.path.abspath(path)):
                continue
            candidates.append(path)
        if progress:
            progress("scanned %d files, %d candidates; probing metadata" % (scanned, len(candidates)))

        infos = {}
        for i, path in enumerate(candidates):
            if progress and i % 25 == 0:
                progress("probing %d/%d" % (i + 1, len(candidates)))
            info = probe_media(cfg, path, self.db)
            if not info or not info.get("has_video"):
                self._skip(path, "ffprobe 无法解析或无视频流")
                continue
            vcodec = (info.get("vcodec") or "").lower()
            if info.get("comment") == tag:
                self._skip(path, "已由 HServer 转码过(comment 标记)")
                continue
            if skip_codecs and vcodec in skip_codecs and vcodec == "hevc":
                self._skip(path, "源已是 HEVC，重编码无收益")
                continue
            if cfg.get("scan.skip_if_pix_fmt_10bit") and info.get("bit_depth", 8) > 8:
                self._skip(path, "10bit 源(按配置跳过)")
                continue
            min_kbps = int(cfg.get("scan.skip_if_source_bitrate_kbps") or 0)
            if min_kbps > 0 and info.get("bitrate"):
                height = int(info.get("height") or 1080) or 1080
                # normalise to 1080p so a 720p file is not judged too harshly
                per_1080 = float(info["bitrate"]) * (1080.0 / height) ** 2 / 1000.0
                if per_1080 < min_kbps:
                    self._skip(path, "源码率过低 %.0f kbps(1080p 归一化)" % per_1080)
                    continue
            infos[path] = info

        # episodes -> merge groups; groups are removed from the single-file queue
        jobs = []
        grouped = {}
        if cfg.get("series.enabled"):
            groups = group_series(list(infos.keys()), cfg)
            for d, base, paths in groups:
                if any(p not in infos for p in paths):
                    continue
                group_infos = [infos[p] for p in paths]
                if cfg.get("series.require_same_codec"):
                    codecs = {(i.get("vcodec") or "") for i in group_infos}
                    if len(codecs) > 1:
                        self.db.event("warn", "分集 %s: 编码不一致 %s，改为逐集处理" % (base, sorted(codecs)))
                        continue
                if cfg.get("series.require_same_resolution"):
                    res = {(i.get("width"), i.get("height")) for i in group_infos}
                    if len(res) > 1:
                        self.db.event("warn", "分集 %s: 分辨率不一致 %s，改为逐集处理" % (base, sorted(res)))
                        continue
                total_size = sum(i.get("size") or 0 for i in group_infos)
                total_dur = sum(float(i.get("duration") or 0) for i in group_infos)
                first = group_infos[0]
                ep_ext = os.path.splitext(paths[0])[1].lstrip(".") or ext_out
                notional = os.path.join(d, "%s.%s" % (base, ep_ext))
                job = {
                    "batch": ",".join(roots),
                    "src": notional,
                    "src_size": total_size,
                    "merge_files": paths,
                    "width": first.get("width"),
                    "height": first.get("height"),
                    "duration": total_dur,
                    "vcodec": first.get("vcodec"),
                    "audio_codecs": first.get("audio_codecs") or [],
                    "meta": {"episodes": len(paths), "series": base,
                             "hdr": first.get("hdr", False)},
                }
                jobs.append(job)
                for p in paths:
                    grouped[p] = base
        for path, info in infos.items():
            if path in grouped:
                continue
            job = {
                "batch": ",".join(roots),
                "src": path,
                "src_size": info.get("size") or 0,
                "width": info.get("width"),
                "height": info.get("height"),
                "duration": info.get("duration"),
                "vcodec": info.get("vcodec"),
                "audio_codecs": info.get("audio_codecs") or [],
                "meta": {"hdr": info.get("hdr", False), "pix_fmt": info.get("pix_fmt"),
                         "subs": info.get("sub_codecs") or [], "profile": info.get("profile")},
            }
            jobs.append(job)
        if progress:
            progress("planned %d jobs (%d skipped) in %.1fs" % (len(jobs), len(self.skips), time.time() - started))
        return jobs
