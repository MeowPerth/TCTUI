"""The executor.  It knows how to run ffmpeg and nothing about policy.

Two jobs per worker: optional concat (episode merge) and one encode.  All
filesystem surgery (rename in/out, delete sources, keep-or-discard) is done by
the scheduler so this process can be killed at any moment without losing a
source file.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time

from . import util

FFMPEG_ERROR_HINTS = (
    "OpenEncodeSessionEx failed",
    "Cannot load libcuda",
    "Cannot load nvcuda",
    "no capable devices found",
    "No NVENC capable devices found",
    "CUDA_ERROR",
    "cannot open shared object",
    "InitializeEncoder failed",
    "out of memory",
    "No device available",
)


class Worker:
    def __init__(self, spec):
        self.spec = spec
        self.job_id = spec.get("job_id")
        self.progress_path = spec["progress"]
        self.result_path = spec["result"]
        self.log_path = spec.get("log") or (spec["result"] + ".log")
        self.stop_requested = False
        self.current_proc = None
        self.wrote_progress = 0.0
        self.last = {}
        self.phase = "transcode"
        self.started = time.time()
        self.output = spec.get("out")
        self.merge_tmp = None
        self.cleanup_paths = []

    # ---------------------------------------------------------------- helpers
    def log(self, msg):
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write("[worker %s] %s\n" % (time.strftime("%H:%M:%S"), msg))
        except OSError:
            pass

    def write_progress(self, force=False):
        now = time.time()
        if not force and now - self.wrote_progress < 0.6:
            return
        self.wrote_progress = now
        dur = float(self.last.get("duration") or self.spec.get("duration") or 0)
        out_time = float(self.last.get("out_time") or 0)
        frac = 0.0
        if dur > 0:
            frac = util.clamp(out_time / dur, 0.0, 1.0)
        speed = self.last.get("speed")
        eta = None
        if speed and dur > 0 and speed > 0:
            eta = max(0.0, (dur - out_time) / speed)
        payload = {
            "job_id": self.job_id, "phase": self.phase, "ts": now, "pid": os.getpid(),
            "progress": round(frac, 4), "out_time": round(out_time, 2),
            "duration": dur or None, "fps": self.last.get("fps"),
            "speed": speed, "eta": eta, "total_size": self.last.get("total_size"),
            "bitrate": self.last.get("bitrate"), "attempt": self.spec.get("attempt", 1),
            "gpu": self.spec.get("gpu"), "mode": self.last.get("mode"),
            "state": "running" if not self.stop_requested else "stopping",
        }
        try:
            util.atomic_write_json(self.progress_path, payload)
        except OSError:
            pass

    def write_result(self, ok, **extra):
        payload = {
            "job_id": self.job_id,
            "ok": bool(ok),
            "phase": self.phase,
            "ts": time.time(),
            "elapsed": round(time.time() - self.started, 1),
            "attempts": self.spec.get("attempt", 1),
            "out_path": self.output,
            "log": self.log_path,
            "canceled": bool(self.stop_requested),
            "cleanup": self.cleanup_paths,
        }
        payload.update(extra)
        if os.path.exists(self.output or ""):
            try:
                payload["bytes_out"] = os.path.getsize(self.output)
            except OSError:
                pass
        try:
            util.atomic_write_json(self.result_path, payload)
        except OSError:
            pass
        return payload

    # ---------------------------------------------------------------- commands
    def build_transcode_args(self, src, mode, attempt, sw_decode=False):
        spec = self.spec
        enc = spec.get("encode") or {}
        info = spec.get("source_info") or {}
        gpu = spec.get("gpu")
        out_ext = (enc.get("out_ext") or "mkv").lstrip(".")
        duration = float(spec.get("duration") or info.get("duration") or 0)
        args = [spec["ffmpeg"], "-hide_banner", "-nostdin", "-y",
                "-loglevel", "warning", "-progress", "pipe:1"]
        use_hwdec = False
        codec = (info.get("vcodec") or "").lower()
        decoders = spec.get("dec_codecs") or []
        if mode == "gpu" and gpu is not None and gpu >= 0 and codec in decoders and not sw_decode:
            use_hwdec = True
        bit_depth = int(info.get("bit_depth") or 8)
        hdr = bool(info.get("hdr")) and (enc.get("hdr") or "auto") != "force-sdr"
        max_width = int(enc.get("max_width") or 0)
        width = int(info.get("width") or 0)
        height = int(info.get("height") or 0)
        need_scale = bool(max_width and width and width > max_width)

        if use_hwdec:
            # NVDEC has a 32-surface limit; ffmpeg's default 16 decoder threads
            # can ask for more (e.g. 34 on a 464x848 H.264 file) and fail with
            # CUDA_ERROR_INVALID_VALUE. Four threads stay well under the limit.
            args += ["-threads", "4", "-hwaccel", "cuda", "-hwaccel_device", str(gpu),
                     "-hwaccel_output_format", "cuda"]
        args += ["-i", src]

        vf = []
        hw_filter = ""
        if mode == "gpu":
            hw_fmt = "p010le" if (bit_depth > 8 or hdr) else "nv12"
            sw_fmt = "p010le" if (bit_depth > 8 or hdr) else "yuv420p"
            if use_hwdec:
                # Keep the whole chain on the GPU: never let ffmpeg insert a
                # CPU auto_scale between cuda frames and nvenc (that fails with
                # "Impossible to convert between ... auto_scale" / exit -38).
                if need_scale:
                    tgt_h = int(round(height * max_width / float(width) / 2.0)) * 2 if width and height else -2
                    hw_filter = "scale_cuda=%d:%d:format=%s" % (max_width, max(2, tgt_h), hw_fmt)
                elif bit_depth > 8 or hdr:
                    hw_filter = "scale_cuda=iw:-2:format=p010le"
                else:
                    # declare the output format explicitly; otherwise ffmpeg may
                    # try to insert a CPU auto_scale between scale_cuda and nvenc
                    hw_filter = "scale_cuda=iw:-2:format=nv12"
            elif need_scale:
                tgt_h = int(round(height * max_width / float(width) / 2.0)) * 2 if width and height else -2
                vf.append("scale=%d:%d" % (max_width, max(2, tgt_h)))
                vf.append("format=%s" % sw_fmt)
        else:  # cpu encode
            if need_scale:
                tgt_h = int(round(height * max_width / float(width) / 2.0)) * 2 if width and height else -2
                vf.append("scale=%d:%d:flags=lanczos" % (max_width, max(2, tgt_h)))
            if hdr:
                vf.append("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")

        # stream mapping: real video only + optional audio/subs/attachments
        if hw_filter:
            args += ["-noautoscale", "-filter_complex",
                     "[0:V:0]%s[out]" % hw_filter, "-map", "[out]"]
        else:
            args += ["-map", "0:V:0"]
        args += ["-map", "0:a?"]
        if (enc.get("subs") or "copy") != "none":
            args += ["-map", "0:s?"]
        if out_ext in ("mkv", "matroska"):
            args += ["-map", "0:t?"]

        if mode == "gpu":
            presets = ["-c:v", enc.get("video_codec") or "hevc_nvenc"]
            if enc.get("preset"):
                presets += ["-preset", str(enc.get("preset"))]
            if enc.get("tune"):
                presets += ["-tune", str(enc.get("tune"))]
            if enc.get("cq") is not None:
                presets += ["-rc", "vbr", "-cq", str(int(enc.get("cq"))), "-b:v", "0"]
            if gpu is not None and gpu >= 0:
                presets += ["-gpu", str(gpu)]
            args += presets
            if bit_depth > 8 or hdr:
                # With CUDA frames the filter already outputs p010le; adding
                # -pix_fmt here makes ffmpeg insert auto_scale (exit -38).
                if not use_hwdec:
                    args += ["-pix_fmt", "p010le"]
                if (enc.get("video_codec") or "").startswith("hevc"):
                    args += ["-profile:v", "main10"]
        else:
            cpu = enc.get("cpu_codec") or "libx265"
            args += ["-c:v", cpu, "-preset", str(enc.get("cpu_preset") or "medium")]
            if cpu.startswith("libx26"):
                args += ["-crf", str(int(enc.get("cq") or 27) - 1)]
            else:
                args += ["-b:v", "0"]
            if hdr:
                args += ["-pix_fmt", "yuv420p"]

        if vf:
            args += ["-vf", ",".join(vf)]

        # audio
        audio = enc.get("audio") or "copy"
        if audio == "copy":
            args += ["-c:a", "copy"]
        elif audio == "none":
            args += ["-an"]
        else:
            args += ["-c:a", audio, "-b:a", str(enc.get("audio_bitrate") or "192k")]

        # subtitles
        if (enc.get("subs") or "copy") == "copy":
            args += ["-c:s", "copy"]
        else:
            args += ["-sn"]

        # hdr colour signalling (best effort, keeps graders happy)
        if mode == "gpu" and hdr:
            transfer = info.get("color_transfer") or "smpte2084"
            primaries = info.get("color_primaries") or "bt2020"
            if transfer in ("unknown", "reserved", ""):
                transfer = "smpte2084"
            if primaries in ("unknown", "reserved", ""):
                primaries = "bt2020"
            args += ["-color_primaries", primaries, "-color_trc", transfer,
                     "-colorspace", "bt2020nc", "-color_range", "tv"]
        elif mode == "cpu" and hdr:
            args += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]

        if out_ext in ("mp4", "mov", "m4v"):
            args += ["-tag:v", "hvc1" if (enc.get("video_codec") or "").startswith("hevc") else "avc1"]
            args += ["-movflags", "+faststart"]
        args += ["-map_metadata", "0", "-map_chapters", "0"]
        tag = enc.get("tag")
        if tag:
            args += ["-metadata", "comment=%s" % tag]
        args += ["-metadata", "encoder=tctui/%s" % "0.1"]
        args += ["-max_muxing_queue_size", str(int(enc.get("max_muxing_queue_size") or 4096))]
        for extra in (enc.get("extra_video_args") or []):
            args.append(str(extra))
        args += ["-f", "matroska" if out_ext == "mkv" else out_ext]
        for extra in (enc.get("extra_args") or []):
            args.append(str(extra))
        args += [self.output]
        return args, use_hwdec

    # ---------------------------------------------------------------- progress pump
    def pump(self, proc, duration, phase, mode):
        """Read -progress output from ffmpeg, forward stderr into the job log."""
        self.phase = phase
        self.last["duration"] = duration
        self.last["mode"] = mode
        buf = b""
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    key, _, value = line.decode("utf-8", "replace").partition("=")
                    key = key.strip()
                    value = value.strip()
                    if key == "out_time_us" or key == "out_time_ms":
                        try:
                            self.last["out_time"] = int(value) / 1000000.0
                        except ValueError:
                            pass
                    elif key == "out_time":
                        m = re.match(r"(\d+):(\d+):(\d+(?:\.\d+)?)", value)
                        if m:
                            self.last["out_time"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                    elif key == "fps":
                        try:
                            self.last["fps"] = float(value)
                        except ValueError:
                            pass
                    elif key == "speed":
                        self.last["speed"] = util.parse_speed(value)
                    elif key == "total_size":
                        try:
                            self.last["total_size"] = int(value)
                        except ValueError:
                            pass
                    elif key == "bitrate":
                        self.last["bitrate"] = value
                    elif key == "progress":
                        self.write_progress(force=True)
                    else:
                        continue
                    self.write_progress()
        except (ValueError, OSError):
            pass
        self.write_progress(force=True)

    def run_ffmpeg(self, args, duration, phase, mode):
        self.log("run: %s" % " ".join(args))
        errpath = self.log_path
        errfh = open(errpath, "a", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=errfh, bufsize=0)
        self.current_proc = proc
        if self.stop_requested:
            self._terminate()
        try:
            self.pump(proc, duration, phase, mode)
        finally:
            rc = proc.wait()
            errfh.close()
            self.current_proc = None
        return rc

    def _terminate(self):
        proc = self.current_proc
        if not proc:
            return
        try:
            proc.send_signal(signal.SIGINT)
        except OSError:
            return
        for _ in range(50):
            if proc.poll() is not None:
                return
            time.sleep(0.1)
        try:
            proc.kill()
        except OSError:
            pass

    # ---------------------------------------------------------------- main
    def run(self):
        # nvidia-smi orders GPUs by PCI bus; CUDA's default FASTEST_FIRST order
        # can swap cards (e.g. the 2060 and a 2080 Ti). Force the same order so
        # -hwaccel_device N / -gpu N really mean nvidia-smi index N.
        os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        spec = self.spec
        util.ensure_dir(os.path.dirname(self.log_path))
        util.ensure_dir(os.path.dirname(self.progress_path))
        self.log("start job=%s gpu=%s src=%s" % (self.job_id, spec.get("gpu"), spec.get("src")))
        duration = float(spec.get("duration") or 0)
        merge_files = spec.get("merge_files") or []

        # ---- optional episode merge (concat -c copy)
        if merge_files:
            self.phase = "merge"
            merged = spec.get("merge_tmp") or os.path.join(os.path.dirname(self.output),
                                                           ".tctui-%s.merged.mkv" % self.job_id)
            self.merge_tmp = merged
            list_path = merged + ".list.txt"
            try:
                with open(list_path, "w", encoding="utf-8") as fh:
                    for p in merge_files:
                        fh.write("file '%s'\n" % p.replace("'", "'\\''"))
                args = [spec["ffmpeg"], "-hide_banner", "-nostdin", "-y", "-loglevel", "warning",
                        "-progress", "pipe:1", "-f", "concat", "-safe", "0", "-i", list_path,
                        "-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero",
                        "-max_muxing_queue_size", "4096", merged]
                rc = self.run_ffmpeg(args, duration, "merge", "copy")
            finally:
                try:
                    os.unlink(list_path)
                except OSError:
                    pass
            if rc != 0 or not os.path.isfile(merged) or os.path.getsize(merged) < 1024:
                return self.write_result(False, exit_code=rc, error="分集合并失败 (concat rc=%s)" % rc,
                                         merge_failed=True)
            try:
                duration = float(spec.get("duration") or 0)
                if duration <= 0:
                    duration = self._probe_duration(merged) or 0
            except Exception:  # noqa: BLE001
                pass
            src = merged
            self.cleanup_paths.append(merged)
        else:
            src = spec["src"]

        if self.stop_requested:
            return self.write_result(False, error="已取消", canceled=True)

        # ---- encode, with hardware-decode fallback
        attempts = []
        final_rc = -1
        gpu_error = False
        mode = "gpu" if (spec.get("gpu") is not None and spec.get("gpu") >= 0) else "cpu"
        if spec.get("force_mode"):
            mode = spec["force_mode"]
        plan = [mode]
        if mode == "gpu" and spec.get("encode", {}).get("fallback_software_decode", True):
            plan.append("gpu-swdec")
        if mode == "gpu" and spec.get("encode", {}).get("fallback_to_cpu", False):
            plan.append("cpu")

        for idx, attempt_mode in enumerate(plan):
            if self.stop_requested:
                return self.write_result(False, error="已取消", canceled=True, attempts=attempts)
            self.phase = "transcode"
            m = "gpu" if attempt_mode.startswith("gpu") else "cpu"
            args, use_hwdec = self.build_transcode_args(
                src, m, idx + 1, sw_decode=(attempt_mode == "gpu-swdec"))
            self.log("attempt %d mode=%s hwdec=%s" % (idx + 1, attempt_mode, use_hwdec))
            # clean partial output between attempts
            if idx and os.path.exists(self.output):
                try:
                    os.unlink(self.output)
                except OSError:
                    pass
            rc = self.run_ffmpeg(args, duration, "transcode", m)
            attempts.append({"mode": attempt_mode, "rc": rc})
            final_rc = rc
            if rc == 0 and os.path.isfile(self.output) and os.path.getsize(self.output) > 0:
                self.phase = "done"
                self.write_progress(force=True)
                speed = self.last.get("speed")
                return self.write_result(True, exit_code=0, mode=attempt_mode,
                                         hwdecode=use_hwdec, duration=duration,
                                         speed=speed, fps=self.last.get("fps"),
                                         attempts_list=attempts)
            tail = self._stderr_tail()
            if any(h in tail for h in FFMPEG_ERROR_HINTS):
                if self._hwdec_setup_failed(tail) and idx + 1 < len(plan):
                    self.log("hardware decoder init failed; retrying with software decode")
                    continue
                gpu_error = True
                self.log("gpu error detected: %s" % (tail.splitlines()[-1] if tail else "gpu error"))
                # GPU problems will not be fixed by decoding in software
                break
            self.log("attempt %d failed rc=%s" % (idx + 1, rc))
        self.phase = "failed"
        return self.write_result(False, exit_code=final_rc, error=self._stderr_tail()[-600:] or "ffmpeg 失败",
                                 gpu_error=gpu_error, attempts_list=attempts)

    @staticmethod
    def _hwdec_setup_failed(tail):
        return ("hwaccel initialisation returned error" in tail
                or "Failed setup for format cuda" in tail
                or "cuvidCreateDecoder" in tail)

    def _stderr_tail(self):
        lines = util.tail_lines(self.log_path, 40)
        # keep only ffmpeg's own lines, not our [worker] lines
        lines = [ln for ln in lines if not ln.startswith("[worker")]
        return "\n".join(lines[-12:])

    def _probe_duration(self, path):
        cmd = [self.spec["ffprobe"], "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", path]
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
            return float(p.stdout.decode().strip())
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None


def main(argv):
    if len(argv) < 2:
        print("usage: worker.py <spec.json>", file=sys.stderr)
        return 2
    spec = util.read_json(argv[1])
    if not spec:
        print("cannot read spec %s" % argv[1], file=sys.stderr)
        return 2
    worker = Worker(spec)

    def on_signal(signum, frame):  # noqa: ARG001
        worker.stop_requested = True
        worker.log("signal %s received" % signum)
        worker._terminate()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        result = worker.run()
    except Exception as exc:  # noqa: BLE001
        worker.log("worker crashed: %r" % exc)
        result = worker.write_result(False, error="worker crashed: %r" % exc)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
