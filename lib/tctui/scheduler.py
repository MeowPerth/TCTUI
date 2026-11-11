"""The scheduler daemon: queue, GPU allocation, filesystem finalisation, stats.

It is the only process allowed to rename/delete library files.  Workers only run
ffmpeg.  Everything is journaled in SQLite (job.meta.staged / job.out temp) so a
crash can be recovered without losing a source file.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

from . import config as cfgmod
from . import gpu, scan, util
from .db import DB

ACTIVE = ("running", "verifying", "staged")
FINISHED = ("done", "kept", "failed", "canceled")


def should_keep_source(savings, threshold):
    """Never replace a source with a bigger file, even when the threshold is 0."""
    return savings <= 0 or (threshold > 0 and savings < threshold)


class Scheduler:
    def __init__(self, cfg):
        self.cfg = cfg
        self.db = DB(cfg.db_path()).init()
        self.monitor = gpu.LiveMonitor(cfg, ttl=1.0)
        self.paused = self.db.kv_get("paused", "0") == "1"
        self.disabled_gpus = set(json.loads(self.db.kv_get("disabled_gpus", "[]") or "[]"))
        self.busy = ""
        self.stop = False
        self._last_sample = 0.0
        self._last_prune = 0.0
        self._last_progress_write = 0.0
        self._scan_lock = threading.Lock()
        self.started_at = util.now()
        self.version = cfgmod.APP_VERSION
        util.ensure_dir(cfg.jobs_dir())

    # ------------------------------------------------------------------ helpers
    def log(self, level, msg):
        self.db.event(level, msg)
        print("[%s] %s" % (level, msg), flush=True)

    def heartbeat(self):
        payload = {
            "pid": os.getpid(), "ts": util.now(), "paused": self.paused,
            "busy": self.busy, "version": self.version,
            "started": self.started_at,
            "gpus": [g["index"] for g in self.monitor.get()],
        }
        try:
            util.atomic_write_json(self.cfg.heartbeat_path(), payload)
        except OSError:
            pass

    def _job_meta(self, job):
        try:
            return json.loads(job.get("meta") or "{}")
        except (TypeError, ValueError):
            return {}

    def _set_meta(self, job, meta):
        self.db.update_job(job["id"], meta=json.dumps(meta, ensure_ascii=False))

    # ------------------------------------------------------------------ recovery
    def recover(self):
        """Adopt live workers, finalise finished ones, restore interrupted sources."""
        for job in self.db.active():
            result_path = self._result_path(job["id"])
            if os.path.isfile(result_path):
                result = util.read_json(result_path, {}) or {}
                self.log("info", "恢复：任务 #%s 已有结果，直接结算" % job["id"])
                self.finalize(job, result)
                continue
            pid = job.get("pid")
            if pid and util.pid_alive(pid):
                self.log("info", "恢复：接管运行中的任务 #%s (pid %s)" % (job["id"], pid))
                continue
            meta = self._job_meta(job)
            self.log("warn", "恢复：任务 #%s 已中断，还原源文件" % job["id"])
            self._restore_source(job, meta)
            self.db.update_job(job["id"], status="interrupted", phase="restored",
                               finished=util.now(), error="daemon 重启时任务中断，已还原源文件")
            self._cleanup_temp(job, meta)
        # stale queued jobs whose staged temp vanished are simply re-dispatched

    def _progress_path(self, job_id):
        return os.path.join(self.cfg.jobs_dir(), "%s.progress.json" % job_id)

    def _result_path(self, job_id):
        return os.path.join(self.cfg.jobs_dir(), "%s.result.json" % job_id)

    def _spec_path(self, job_id):
        return os.path.join(self.cfg.jobs_dir(), "%s.spec.json" % job_id)

    def _log_path(self, job_id):
        d = os.path.join(self.cfg.logs_dir, "jobs")
        util.ensure_dir(d)
        return os.path.join(d, "%s.log" % job_id)

    # ------------------------------------------------------------------ dispatch
    def gpu_limits(self):
        live = self.monitor.get()
        limit = gpu.per_gpu_limit(self.cfg)
        return {g["index"]: limit for g in live}

    def dispatch(self):
        # never start jobs while our own probe/scan is running: the probe opens
        # many NVENC sessions and would collide with the session budget
        if self.paused or self.busy:
            return
        live = self.monitor.get()
        limits = self.gpu_limits()
        active = self.db.per_gpu_active()
        cpu_fallback = bool(self.cfg.get("encode.fallback_to_cpu")) and not live
        if not live and not cpu_fallback:
            return
        q = self.db.queued(limit=200)
        for job in q:
            if self.stop:
                return
            meta = self._job_meta(job)
            defer = float(meta.get("defer_until") or 0)
            if defer > util.now():
                continue
            est = gpu.estimate_vram_mb(self.cfg, job.get("width") or 0, job.get("height") or 0)
            excluded = set(meta.get("excluded_gpus") or [])
            gpu_obj, limit = gpu.choose_gpu(
                self.cfg, live, active, {"est_vram_mb": est},
                excluded=excluded | self.disabled_gpus, limits=limits)
            if gpu_obj is None:
                if not live and cpu_fallback:
                    gpu_obj, limit = {"index": -1, "uuid": "", "name": "CPU", "mem_total_mb": 0,
                                      "mem_free_mb": 0, "util": 0}, 1
                else:
                    continue
            try:
                self.start_job(job, gpu_obj, meta, est)
            except Exception as exc:  # noqa: BLE001
                self.log("error", "任务 #%s 启动失败: %r" % (job["id"], exc))
                self.db.update_job(job["id"], status="failed", finished=util.now(),
                                   error="启动失败: %r" % exc)
            active = self.db.per_gpu_active()

    def start_job(self, job, gpu_obj, meta, est_vram):
        cfg = self.cfg
        out_ext = (cfg.get("encode.out_ext") or "mkv").lstrip(".")
        src = job["src"]
        merge_files = json.loads(job.get("merge_files") or "[]")
        is_merge = bool(merge_files)

        if is_merge:
            missing = [p for p in merge_files if not os.path.isfile(p)]
            if missing:
                raise RuntimeError("分集源文件缺失: %s" % ", ".join(missing[:2]))
            total_size = sum(os.path.getsize(p) for p in merge_files)
            # output keeps the *group* base name (SeriesX-1..3 -> SeriesX.mkv),
            # never the first episode's name
            target = scan.safe_target_path(job["src"], out_ext)
            workdir = os.path.dirname(merge_files[0])
            staged = ""
        else:
            if not os.path.isfile(src):
                raise RuntimeError("源文件不存在: %s" % src)
            total_size = os.path.getsize(src)
            target = scan.safe_target_path(src, out_ext)
            workdir = os.path.dirname(src)
            staged = os.path.join(workdir, ".tctui-%d.old.%s" % (
                job["id"], os.path.splitext(src)[1].lstrip(".") or "bak"))

        # disk space guard: source stays on disk while the output grows
        need = int(total_size * 1.05) + 1024 * 1024 * 512
        try:
            du = shutil.disk_usage(workdir)
        except OSError as exc:
            raise RuntimeError("无法检查磁盘空间: %r" % exc)
        min_free = int(cfg.get("policy.min_free_disk_mb") or 1024) * 1024 * 1024
        if du.free < need + min_free:
            meta["defer_until"] = util.now() + 300
            self._set_meta(job, meta)
            self.log("warn", "任务 #%s 延后：磁盘剩余 %s < 需要 %s" % (
                job["id"], util.fmt_bytes(du.free), util.fmt_bytes(need + min_free)))
            return

        out_tmp = os.path.join(os.path.dirname(target), ".tctui-%d.out.%s" % (job["id"], out_ext))
        if os.path.exists(out_tmp):
            try:
                os.unlink(out_tmp)
            except OSError:
                pass

        # stage the source away (single-file jobs only): the original name is free
        if not is_merge:
            try:
                os.replace(src, staged)
            except OSError as exc:
                raise RuntimeError("无法暂存源文件: %r" % exc)

        info = scan.probe_media(cfg, staged if staged else merge_files[0], self.db) or {}
        spec = {
            "job_id": job["id"],
            "src": staged or (merge_files[0] if merge_files else src),
            "merge_files": merge_files,
            "merge_tmp": os.path.join(workdir, ".tctui-%d.merged.mkv" % job["id"]),
            "out": out_tmp,
            "gpu": int(gpu_obj["index"]),
            "gpu_uuid": gpu_obj.get("uuid") or "",
            "duration": float(job.get("duration") or info.get("duration") or 0),
            "source_info": {
                "vcodec": info.get("vcodec"), "pix_fmt": info.get("pix_fmt"),
                "width": info.get("width"), "height": info.get("height"),
                "bit_depth": info.get("bit_depth") or 8, "hdr": bool(info.get("hdr")),
                "color_transfer": info.get("color_transfer"),
                "color_primaries": info.get("color_primaries"),
                "audio_codecs": info.get("audio_codecs") or [],
                "subs": info.get("sub_codecs") or [],
            },
            "dec_codecs": list(gpu.NVDEC_CODECS),
            "encode": cfg.data.get("encode") or {},
            "ffmpeg": cfg.ffmpeg, "ffprobe": cfg.ffprobe,
            "progress": self._progress_path(job["id"]),
            "result": self._result_path(job["id"]),
            "log": self._log_path(job["id"]),
            "attempt": int(job.get("attempts") or 0) + 1,
        }
        for p in (spec["progress"], spec["result"]):
            try:
                os.unlink(p)
            except OSError:
                pass
        util.atomic_write_json(self._spec_path(job["id"]), spec)

        meta.update({"staged": staged, "out_tmp": out_tmp, "target": target,
                     "workdir": workdir, "est_vram_mb": est_vram})
        self.db.update_job(
            job["id"], status="running", phase="merge" if is_merge else "transcode",
            gpu=int(gpu_obj["index"]), gpu_uuid=gpu_obj.get("uuid") or "",
            started=job.get("started") or util.now(), out=target, out_ext=out_ext,
            attempts=int(job.get("attempts") or 0) + 1,
            log_path=spec["log"], meta=json.dumps(meta, ensure_ascii=False))

        env = dict(os.environ)
        env["TCTUI_HOME"] = cfg.home
        env["PYTHONPATH"] = cfg.py_path + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUNBUFFERED"] = "1"
        logfh = open(spec["log"], "a", encoding="utf-8", errors="replace")
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "tctui", "worker", self._spec_path(job["id"])],
                cwd=cfg.home, env=env, stdout=logfh, stderr=subprocess.STDOUT,
                start_new_session=True)
        except OSError as exc:
            logfh.close()
            self._restore_source(job, meta)
            self._cleanup_temp(job, meta)
            self.db.update_job(job["id"], status="failed", finished=util.now(),
                               error="无法启动 worker: %r" % exc)
            return
        logfh.close()
        self.db.update_job(job["id"], pid=proc.pid)
        self.log("info", "启动 #%s GPU%s %s (%s)" % (
            job["id"], gpu_obj["index"], os.path.basename(job["src"]),
            util.fmt_bytes(total_size)))

    # ------------------------------------------------------------------ monitor
    def reap(self):
        for job in self.db.active():
            jid = job["id"]
            meta = self._job_meta(job)
            rpath = self._result_path(jid)
            if os.path.isfile(rpath):
                result = util.read_json(rpath, {}) or {}
                self.finalize(job, result)
                continue
            pid = job.get("pid")
            dead = (not pid and util.now() - float(job.get("started") or 0) > 10) or \
                   (pid and not util.pid_alive(pid))
            if not dead and pid and meta.get("cancel"):
                # cancel not taking effect: escalate to the whole process group
                if util.now() - float(meta.get("cancel_ts") or 0) > 20:
                    self.log("warn", "任务 #%s 取消超时，强制结束进程组" % jid)
                    self._kill_tree(pid)
                    dead = True
            if dead:
                canceled = bool(meta.get("cancel"))
                self._kill_tree(pid)   # reap orphaned ffmpeg left by kill -9
                self.log("warn" if not canceled else "info",
                         "任务 #%s 的 worker 已退出但没有结果，还原源文件" % jid)
                self._restore_source(job, meta)
                self._cleanup_temp(job, meta)
                self.db.update_job(jid, status="canceled" if canceled else "interrupted",
                                   finished=util.now(),
                                   error="用户取消" if canceled else "worker 异常退出（未写结果），已还原源文件")
                continue
            p = util.read_json(self._progress_path(jid), None)
            if p:
                self.db.update_job(
                    jid, progress=float(p.get("progress") or 0), fps=p.get("fps"),
                    speed=p.get("speed"), eta=p.get("eta"), phase=p.get("phase") or job.get("phase"))

    # ------------------------------------------------------------------ finalise
    def finalize(self, job, result):
        jid = job["id"]
        meta = self._job_meta(job)
        ok = bool(result.get("ok"))
        out_tmp = meta.get("out_tmp") or ""
        staged = meta.get("staged") or ""
        target = meta.get("target") or job.get("out") or ""
        merge_files = json.loads(job.get("merge_files") or "[]")
        cleanup = result.get("cleanup") or []
        for p in cleanup:
            self._unlink(p)

        if not ok:
            canceled = bool(result.get("canceled")) or job.get("status") == "canceled"
            gpu_error = bool(result.get("gpu_error"))
            excluded = meta.get("excluded_gpus") or []
            if gpu_error and int(job.get("gpu_attempts") or 0) < 2 and self.monitor.get():
                excluded = excluded + [job.get("gpu")]
                meta["excluded_gpus"] = excluded
                meta["defer_until"] = util.now() + 8   # let the session budget free up
                self._restore_source(job, meta)
                self._cleanup_temp(job, meta)
                self.db.update_job(jid, status="queued", phase="requeue", pid=None,
                                   gpu_attempts=int(job.get("gpu_attempts") or 0) + 1,
                                   error="显卡错误，换卡重试", meta=json.dumps(meta, ensure_ascii=False))
                self.log("warn", "任务 #%s 显卡错误，换卡重试（已排除 GPU%s）" % (jid, job.get("gpu")))
                return
            self._restore_source(job, meta)
            if self.cfg.get("policy.delete_temp_on_failure", True):
                self._unlink(out_tmp)
            status = "canceled" if canceled else "failed"
            self.db.update_job(jid, status=status, phase="done", pid=None, finished=util.now(),
                               exit_code=result.get("exit_code"), error=result.get("error") or "",
                               fps=result.get("fps"), speed=result.get("speed"))
            self.log("warn" if status == "failed" else "info",
                     "任务 #%s %s: %s" % (jid, "已取消" if canceled else "失败",
                                          (result.get("error") or "").replace("\n", " ")[:200]))
            return

        # ---------- success path
        if not out_tmp or not os.path.isfile(out_tmp):
            self._restore_source(job, meta)
            self._cleanup_temp(job, meta)
            self.db.update_job(jid, status="failed", finished=util.now(), error="输出临时文件不存在")
            self.log("error", "任务 #%s 输出临时文件丢失" % jid)
            return
        bytes_in = int(job.get("src_size") or 0)
        bytes_out = os.path.getsize(out_tmp)
        savings = ((bytes_in - bytes_out) * 100.0 / bytes_in) if bytes_in > 0 else 0.0
        dur_ratio = 1.0
        expected = float(job.get("duration") or 0)
        if expected > 0:
            got = self._probe_duration(out_tmp)
            if got:
                dur_ratio = got / expected
        min_ratio = float(self.cfg.get("policy.verify_min_duration_ratio") or 0)
        if min_ratio and dur_ratio < min_ratio:
            self._restore_source(job, meta)
            self._unlink(out_tmp)
            self._cleanup_temp(job, meta)
            self.db.update_job(jid, status="failed", finished=util.now(),
                               error="输出时长异常 %.2f%% (期望 >= %.0f%%)" % (dur_ratio * 100, min_ratio * 100))
            self.log("error", "任务 #%s 输出时长异常，已丢弃输出并还原源文件" % jid)
            return

        threshold = float(self.cfg.get("policy.keep_original_min_savings_pct") or 0)
        if should_keep_source(savings, threshold):
            self._restore_source(job, meta)
            self._unlink(out_tmp)
            self._cleanup_temp(job, meta)
            self.db.update_job(jid, status="kept", finished=util.now(), bytes_in=bytes_in,
                               bytes_out=bytes_out, savings_pct=round(savings, 2),
                               phase="done", pid=None)
            if self.cfg.get("policy.mark_kept_as_processed", True):
                if merge_files:
                    for p in merge_files:
                        self.db.mark_processed(os.path.abspath(p), p, "kept")
                else:
                    self.db.mark_processed(os.path.abspath(job["src"]), job["src"], "kept")
            if savings <= 0:
                self.log("info", "任务 #%s 输出没有变小 (%+.1f%%)，保留源文件" % (jid, savings))
            else:
                self.log("info", "任务 #%s 压缩率 %.1f%% < %.0f%%，保留源文件" % (jid, savings, threshold))
            return

        # rename temp -> final
        final = scan.safe_target_path(target, (self.cfg.get("encode.out_ext") or "mkv"))
        try:
            os.replace(out_tmp, final)
        except OSError as exc:
            self._restore_source(job, meta)
            self._cleanup_temp(job, meta)
            self.db.update_job(jid, status="failed", finished=util.now(),
                               error="无法重命名输出文件: %r" % exc)
            self.log("error", "任务 #%s 无法重命名输出: %r" % (jid, exc))
            return

        # success: drop staged source (single) / episodes (merge, if configured)
        if staged:
            self._unlink(staged)
        if merge_files:
            if self.cfg.get("series.delete_sources_after_success", True):
                for p in merge_files:
                    self._unlink(p)
                self.db.event("info", "分集合并完成，已删除 %d 个分集源文件: %s" % (
                    len(merge_files), os.path.basename(final)))
            for p in merge_files:
                self.db.mark_processed(os.path.abspath(p), p, "done")
        self._cleanup_temp(job, meta)
        self.db.mark_processed(os.path.abspath(final), final, "done")
        self.db.update_job(jid, status="done", finished=util.now(), bytes_in=bytes_in,
                           bytes_out=bytes_out, savings_pct=round(savings, 2),
                           phase="done", pid=None, progress=1.0,
                           fps=result.get("fps"), speed=result.get("speed"),
                           out=final)
        self.log("info", "完成 #%s %s 节省 %.1f%% (%s -> %s)" % (
            jid, os.path.basename(final), savings, util.fmt_bytes(bytes_in), util.fmt_bytes(bytes_out)))

    def _probe_duration(self, path):
        cmd = [self.cfg.ffprobe, "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", path]
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120)
            return float(p.stdout.decode().strip())
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None

    # ------------------------------------------------------------------ fs ops
    def _restore_source(self, job, meta):
        staged = meta.get("staged")
        if not staged:
            return
        if os.path.isfile(staged):
            try:
                if os.path.exists(job["src"]):
                    self.log("warn", "任务 #%s 源文件已存在，保留 .old 备份: %s" % (job["id"], staged))
                else:
                    os.replace(staged, job["src"])
            except OSError as exc:
                self.log("error", "任务 #%s 还原失败: %r (备份: %s)" % (job["id"], exc, staged))
        meta["staged"] = ""
        self.db.update_job(job["id"], meta=json.dumps(meta, ensure_ascii=False))

    def _cleanup_temp(self, job, meta):
        self._unlink(meta.get("out_tmp") or "")
        merged = os.path.join(meta.get("workdir") or "", ".tctui-%d.merged.mkv" % job["id"]) \
            if meta.get("workdir") else ""
        if merged:
            self._unlink(merged)
        self._unlink(self._spec_path(job["id"]))
        self._unlink(self._progress_path(job["id"]))

    def _kill_tree(self, pid):
        """Kill a worker and its ffmpeg child (worker is a session/pg leader)."""
        if not pid:
            return
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return
        if util.pid_alive(pid) and not util.is_our_worker(pid):
            return
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def _unlink(self, path):
        if not path:
            return
        try:
            if os.path.isdir(path):
                return
            os.unlink(path)
        except OSError:
            pass

    # ------------------------------------------------------------------ commands
    def handle_commands(self):
        for cmd in self.db.pending_commands():
            name = cmd.get("cmd")
            try:
                args = json.loads(cmd.get("args") or "{}")
            except ValueError:
                args = {}
            try:
                self.run_command(name, args)
                self.db.complete_command(cmd["id"], "ok")
            except Exception as exc:  # noqa: BLE001
                self.log("error", "命令 %s 失败: %r" % (name, exc))
                self.db.complete_command(cmd["id"], "error: %r" % exc)

    def run_command(self, name, args):
        if name == "pause":
            self.paused = True
            self.db.kv_set("paused", "1")
            self.log("info", "已暂停派发（运行中的任务继续）")
        elif name == "resume":
            self.paused = False
            self.db.kv_set("paused", "0")
            self.log("info", "已恢复派发")
        elif name == "cancel":
            self.cancel_job(int(args["id"]))
        elif name == "retry":
            self.retry_job(int(args["id"]))
        elif name == "scan":
            self.start_scan(args.get("roots") or None)
            self.log("info", "开始强制探测显卡")
        elif name == "set_gpu":
            idx = str(args.get("index"))
            enabled = bool(args.get("enabled", True))
            if enabled:
                self.disabled_gpus.discard(idx)
            else:
                self.disabled_gpus.add(idx)
            self.db.kv_set("disabled_gpus", json.dumps(sorted(self.disabled_gpus)))
            self.log("info", "GPU%s 已%s" % (idx, "启用" if enabled else "停用"))
        elif name == "clear_finished":
            n = self.db.clear_finished()
            self.log("info", "清除 %d 条已完成任务记录" % n)
        elif name == "clear_queue":
            n = self.db.clear_queue(bool(args.get("mark_skipped")))
            self.log("info", "清空排队任务 %d 个%s" % (
                n, "（并标记为跳过）" if args.get("mark_skipped") else ""))
        elif name == "shutdown":
            self.log("info", "收到关闭指令")
            self.stop = True
        elif name == "status":
            pass
        else:
            self.log("warn", "未知命令: %s" % name)

    def cancel_job(self, jid):
        job = self.db.get_job(jid)
        if not job:
            return
        if job["status"] == "queued":
            self.db.update_job(jid, status="canceled", finished=util.now(), error="用户取消")
            self.log("info", "任务 #%s 已取消（未开始）" % jid)
            return
        pid = job.get("pid")
        meta = self._job_meta(job)
        meta["cancel"] = True
        meta["cancel_ts"] = util.now()
        if pid and util.pid_alive(pid):
            try:
                os.kill(int(pid), 15)
                self.log("info", "已请求取消任务 #%s (pid %s)" % (jid, pid))
            except OSError as exc:
                self.log("warn", "取消任务 #%s 失败: %r" % (jid, exc))
        else:
            self.log("warn", "任务 #%s 没有存活进程，等待回收" % jid)
        self.db.update_job(jid, phase="canceling", pid=pid, meta=json.dumps(meta, ensure_ascii=False))

    def retry_job(self, jid):
        job = self.db.get_job(jid)
        if not job or job["status"] in ACTIVE:
            return
        merge_files = json.loads(job.get("merge_files") or "[]")
        sources = merge_files or [job["src"]]
        missing = [p for p in sources if not os.path.isfile(p)]
        if missing:
            self.log("warn", "任务 #%s 无法重试：源文件已不存在（%s）" % (jid, os.path.basename(missing[0])))
            return
        meta = self._job_meta(job)
        meta["excluded_gpus"] = []
        meta.pop("staged", None)
        meta.pop("out_tmp", None)
        self.db.execute("DELETE FROM processed WHERE rel=? OR src=?", (os.path.abspath(job["src"]), job["src"]))
        self.db.update_job(jid, status="queued", phase=None, progress=0, fps=None, speed=None,
                           eta=None, error=None, exit_code=None, pid=None, finished=None,
                           gpu_attempts=0, out=None, meta=json.dumps(meta, ensure_ascii=False))
        self.log("info", "任务 #%s 重新入队" % jid)

    # ------------------------------------------------------------------ scanning
    def start_scan(self):
        path = self.cfg.get("scan.path") or ""
        if not path:
            self.log("warn", "还没有设置扫描路径（界面按 a，或 tctui config --set scan.path=/path）")
            return
        roots = [path]
        if self._scan_lock.locked():
            return
        t = threading.Thread(target=self._scan_thread, args=(roots,), daemon=True)
        t.start()

    def _scan_thread(self, roots):
        if not self._scan_lock.acquire(False):
            return
        db = None
        try:
            self.busy = "scanning"
            self.heartbeat()
            self.log("info", "开始扫描: %s" % ", ".join(roots))
            db = DB(self.cfg.db_path()).init()
            planner = scan.Planner(self.cfg, db)
            jobs = planner.plan(roots, progress=lambda m: self.log("info", m))
            added = 0
            for j in jobs:
                jid = db.add_job(j)
                added += 1
                self.log("info", "入队 #%s %s" % (jid, os.path.basename(j["src"])))
            self.db.kv_set("last_scan", str(util.now()))
            self.log("info", "扫描完成：新增 %d 个任务，跳过 %d 个文件" % (added, len(planner.skips)))
            if planner.skips:
                with open(self.cfg.log_path("skips.log"), "a", encoding="utf-8") as fh:
                    for s in planner.skips:
                        fh.write("%s\t%s\n" % (s["path"], s["reason"]))
        except Exception as exc:  # noqa: BLE001
            self.log("error", "扫描失败: %r" % exc)
        finally:
            if db:
                db.close()
            self.busy = ""
            self._scan_lock.release()
            self.heartbeat()

    # ------------------------------------------------------------------ telemetry
    def sample_telemetry(self):
        now = util.now()
        interval = float(self.cfg.get("daemon.sample_seconds") or 2.0)
        if now - self._last_sample < interval:
            return
        self._last_sample = now
        live = self.monitor.get(force=True)
        for g in live:
            self.db.add_gpu_sample(now, g["index"], g.get("util") or 0, g.get("mem_used_mb") or 0,
                                   g.get("temp") or 0, g.get("power") or 0,
                                   int(g.get("enc_sessions") or 0), int(g.get("dec_sessions") or 0))
        fps, running = self.db.running_fps()
        counts = self.db.counts()
        self.db.add_fleet_sample(now, running, counts.get("queued", 0), fps,
                                 self.db.totals().get("avg_speed") or 0)
        if now - self._last_prune > 300:
            self._last_prune = now
            self.db.prune(self.cfg.get("daemon.history_hours", 2))

    # ------------------------------------------------------------------ main loop
    def run(self):
        self.log("info", "调度器启动 pid=%s home=%s 单卡并发=%s" % (
            os.getpid(), self.cfg.home, gpu.per_gpu_limit(self.cfg)))
        self.recover()
        self.heartbeat()
        if self.cfg.get("scan.on_start", True) and not self.db.counts().get("queued"):
            self.start_scan()
        poll = float(self.cfg.get("daemon.poll_seconds") or 0.5)
        last_hb = 0.0
        last_cfg_check = 0.0
        while not self.stop:
            try:
                if util.now() - last_cfg_check > 2.0:
                    last_cfg_check = util.now()
                    if self.cfg.reload_if_changed():
                        self.log("info", "配置已重新加载")
                self.handle_commands()
                self.reap()
                self.dispatch()
                self.sample_telemetry()
                if util.now() - last_hb > 1.0:
                    self.heartbeat()
                    last_hb = util.now()
            except Exception as exc:  # noqa: BLE001
                self.log("error", "主循环异常: %r" % exc)
                time.sleep(1.0)
            time.sleep(poll)
        self.log("info", "调度器退出")
        self.db.close()


def run_daemon(cfg):
    sched = Scheduler(cfg)
    return sched.run()
