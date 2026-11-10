"""GPU discovery, live telemetry, and capability probing.

Why probing is needed: NVENC/NVDEC session limits are a driver policy, not
something the card advertises.  Consumer GeForce cards have historically been
capped at 2 (sometimes 3/5/8) concurrent NVENC sessions, and the cap changed
between driver branches.  The only reliable answer is: *try it and see how many
parallel encodes actually succeed, and where throughput stops scaling.*
The probe result is cached in state/gpus.json and drives the scheduler.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time

from . import util

# codecs we may hardware-decode, mapped to ffmpeg's cuvid decoder names
CUVID_DECODERS = {
    "h264": "h264_cuvid",
    "hevc": "hevc_cuvid",
    "av1": "av1_cuvid",
    "vp9": "vp9_cuvid",
    "vp8": "vp8_cuvid",
    "mpeg2video": "mpeg2_cuvid",
    "mpeg1video": "mpeg1_cuvid",
    "mpeg4": "mpeg4_cuvid",
    "vc1": "vc1_cuvid",
    "mjpeg": "mjpeg_cuvid",
}

ENC_CODECS = ("hevc_nvenc", "h264_nvenc", "av1_nvenc")

# conservative fallback when the probe cannot run at all
FALLBACK_CAP = {
    "enc_max": 2,
    "recommended_parallel": 1,
    "enc_codecs": ["hevc_nvenc", "h264_nvenc"],
    "dec_codecs": ["h264", "hevc"],
    "agg_fps": None,
    "probe_ok": False,
}


def smi_bin(cfg):
    return cfg.get("paths.nvidia_smi", "nvidia-smi") or "nvidia-smi"


def _run(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except FileNotFoundError:
        return 127, "", "not found: %s" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


def driver_version(cfg):
    rc, out, _ = _run([smi_bin(cfg), "--query-gpu=driver_version", "--format=csv,noheader"], timeout=8)
    if rc == 0:
        return out.strip().splitlines()[0].strip() if out.strip() else ""
    return ""


def _parse_csv_line(line):
    return [x.strip() for x in line.split(",")]


QUERY_FIELDS = [
    "index", "uuid", "name", "memory.total", "memory.used", "memory.free",
    "utilization.gpu", "utilization.memory", "temperature.gpu", "power.draw",
    "power.limit", "clocks.sm", "clocks.max.sm", "fan.speed",
]
QUERY_FIELDS_ENC = ["encoder.stats.sessionCount", "encoder.stats.averageFps"]
QUERY_FIELDS_DEC = ["decoder.stats.sessionCount"]

MSI_FIELDS = ["pci.bus_id", "vbios_version"]


def _numbers(values):
    out = []
    for v in values:
        v = v.replace("MiB", "").replace("W", "").replace("%", "").replace("MHz", "")
        v = v.replace("[N/A]", "").replace("N/A", "").strip()
        try:
            out.append(float(v))
        except ValueError:
            out.append(None)
    return out


def list_gpus(cfg):
    """Return [{index, uuid, name, mem_total_mb, mem_used_mb, util, temp, power,
    power_limit, clock, max_clock, fan, enc_sessions, enc_fps}, ...]."""
    bin_ = smi_bin(cfg)
    if not util.which(bin_):
        return []
    fields = QUERY_FIELDS + ["pci.bus_id"]
    rc, out, err = _run([bin_, "--query-gpu=" + ",".join(fields), "--format=csv,noheader,nounits"], timeout=10)
    if rc != 0:
        return []
    gpus = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = _parse_csv_line(line)
        if len(parts) < len(fields):
            continue
        nums = _numbers(parts[3:])
        g = {
            "index": int(float(parts[0])),
            "uuid": parts[1],
            "name": parts[2],
            "mem_total_mb": nums[0] or 0,
            "mem_used_mb": nums[1] or 0,
            "mem_free_mb": nums[2] or 0,
            "util": nums[3] or 0,
            "mem_util": nums[4] or 0,
            "temp": nums[5],
            "power": nums[6],
            "power_limit": nums[7],
            "clock": nums[8],
            "max_clock": nums[9],
            "fan": nums[10],
            "bus": parts[-1],
            "enc_sessions": 0,
            "enc_fps": 0,
            "dec_sessions": 0,
        }
        gpus.append(g)
    # optional session counters (not available on every driver)
    for key, field, attr in (("enc", "encoder.stats.sessionCount", "enc_sessions"),
                             ("encfps", "encoder.stats.averageFps", "enc_fps"),
                             ("dec", "decoder.stats.sessionCount", "dec_sessions")):
        rc, out, _ = _run([bin_, "--query-gpu=" + field, "--format=csv,noheader,nounits"], timeout=10)
        if rc != 0 or len(out.strip().splitlines()) != len(gpus):
            continue
        for g, line in zip(gpus, out.strip().splitlines()):
            val = _numbers([line])[0]
            g[attr] = int(val or 0)
    # processes (miners, other containers) for display
    rc, out, _ = _run([bin_, "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
                       "--format=csv,noheader,nounits"], timeout=10)
    procs = []
    if rc == 0:
        for line in out.splitlines():
            parts = _parse_csv_line(line)
            if len(parts) >= 4:
                procs.append({"uuid": parts[0], "pid": parts[1],
                              "name": os.path.basename(parts[2]), "mem_mb": parts[3]})
    for g in gpus:
        g["procs"] = [p for p in procs if p["uuid"] == g["uuid"]]
        g["foreign_mem_mb"] = sum(int(float(p["mem_mb"] or 0)) for p in g["procs"])
    return gpus


def encoder_sessions(cfg):
    """{gpu_index: active NVENC sessions} — used to avoid probing a busy card."""
    rc, out, _ = _run([smi_bin(cfg), "--query-gpu=index,encoder.stats.sessionCount",
                       "--format=csv,noheader,nounits"], timeout=8)
    busy = {}
    if rc != 0:
        return busy
    for line in out.splitlines():
        parts = _parse_csv_line(line)
        if len(parts) >= 2:
            try:
                busy[int(float(parts[0]))] = int(float(parts[1]))
            except ValueError:
                continue
    return busy


NVDEC_CODECS = ["h264", "hevc", "vp9", "mpeg2video", "vc1", "mjpeg"]


class LiveMonitor:
    """Caches nvidia-smi output for a short time so UI + scheduler can poll fast."""

    def __init__(self, cfg, ttl=1.0):
        self.cfg = cfg
        self.ttl = ttl
        self._lock = threading.Lock()
        self._at = 0.0
        self._gpus = []
        self._error = ""

    def get(self, force=False):
        with self._lock:
            if not force and time.time() - self._at < self.ttl:
                return self._gpus
            gpus = list_gpus(self.cfg)
            self._at = time.time()
            self._gpus = gpus
            if not gpus:
                self._error = "nvidia-smi unavailable"
            return gpus

    @property
    def error(self):
        return self._error


# ------------------------------------------------------------------ capabilities


def _tiny_sample(cfg, codec, workdir):
    """Create (and cache) a 1s sample encoded with a software encoder."""
    util.ensure_dir(workdir)
    path = os.path.join(workdir, "tiny_%s.mkv" % codec)
    enc = {"h264": "libx264", "hevc": "libx265", "av1": "libsvtav1", "vp9": "libvpx-vp9",
           "mpeg2video": "mpeg2video", "mpeg4": "mpeg4", "vc1": None, "vp8": "libvpx"}.get(codec)
    if not enc or not os.path.isfile(path) or os.path.getsize(path) < 1024:
        if not enc:
            return None
        cmd = [cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=1",
               "-c:v", enc, path]
        rc, _, err = _run(cmd, timeout=60)
        if rc != 0 or not os.path.isfile(path):
            return None
    return path


def _probe_encoders(cfg, gpu_index):
    ok = []
    for enc in ENC_CODECS:
        cmd = [cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", "color=c=black:s=320x240:r=10:d=0.2",
               "-c:v", enc, "-gpu", str(gpu_index), "-f", "null", "-"]
        rc, _, _ = _run(cmd, timeout=30)
        if rc == 0:
            ok.append(enc)
    return ok


def _probe_decoders(cfg, gpu_index, workdir):
    ok = []
    for codec, dec in CUVID_DECODERS.items():
        sample = _tiny_sample(cfg, codec, workdir)
        if not sample:
            continue
        cmd = [cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-c:v", dec, "-gpu", str(gpu_index), "-i", sample,
               "-frames:v", "3", "-f", "null", "-"]
        rc, _, err = _run(cmd, timeout=30)
        if rc == 0 and "No such" not in err:
            ok.append(codec)
    return ok


def _probe_saturation(cfg, gpu_index, cap_max, seconds, resolution, codec, saturation_pct,
                      target_parallel=0):
    """Run K parallel encodes, K=1..cap_max, measure aggregate throughput."""
    results = []
    best = 0.0
    max_ok = 0
    tmpdir = os.path.join(cfg.state_dir, "probe")
    util.ensure_dir(tmpdir)

    def run_k(k):
        t0 = time.time()
        procs = []
        for i in range(k):
            out = os.path.join(tmpdir, "sat_%d_%d_%d.mkv" % (gpu_index, k, i))
            cmd = [cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
                   "-f", "lavfi", "-i", "testsrc2=size=%s:rate=30:duration=%d" % (resolution, seconds),
                   "-c:v", codec, "-preset", "p4", "-cq", "28", "-gpu", str(gpu_index), out]
            try:
                procs.append((subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                               stderr=subprocess.PIPE), out))
            except OSError:
                pass
        failed = 0
        for p, out in procs:
            try:
                p.communicate(timeout=120)
            except subprocess.TimeoutExpired:
                p.kill()
                failed += 1
                continue
            if p.returncode != 0:
                failed += 1
            try:
                os.unlink(out)
            except OSError:
                pass
        wall = max(0.001, time.time() - t0)
        ok_n = k - failed
        return ok_n, (ok_n * seconds * 30.0) / wall, failed

    for k in range(1, cap_max + 1):
        ok_n, agg, failed = run_k(k)
        if failed:
            # a single transient failure (miner holding memory, slow open) must
            # not cap the card for the next 24h: retry this K once
            ok_n2, agg2, failed2 = run_k(k)
            if failed2 == 0:
                ok_n, agg, failed = ok_n2, agg2, 0
        results.append({"k": k, "ok": ok_n, "failed": failed, "agg_fps": round(agg, 1)})
        if failed == 0:
            max_ok = k
        else:
            break
        if agg > best:
            best = agg
    if max_ok == 0:
        # not even one session opened: the card is busy / driver refused.
        # Never turn that into "this card only supports 1 session".
        return {"probe_failed": True, "samples": results,
                "agg_fps": 0.0, "enc_max": None, "recommended_parallel": None}

    # Scrapers/miners sharing the card make single samples noisy: median-filter
    # the throughput curve before looking for the saturation knee.
    vals = [r["agg_fps"] for r in results]
    smooth = []
    for i in range(len(vals)):
        window = vals[max(0, i - 1):i + 2]
        smooth.append(sorted(window)[len(window) // 2])
    best = max(smooth) if smooth else 0.0
    # pick the concurrency with the highest smoothed throughput; ties -> fewer
    rec = 1
    for i, val in enumerate(smooth):
        if results[i]["k"] <= max_ok and val >= best - 1e-6:
            rec = results[i]["k"]
            break
    if max_ok >= 2:
        rec = max(2, rec)          # one session per card wastes the engine
    rec = max(1, min(rec, max_ok))
    if target_parallel:
        rec = min(rec, max(1, int(target_parallel)))
    return {"enc_max": max_ok, "recommended_parallel": rec,
            "agg_fps": round(best, 1), "samples": results,
            "smooth": [round(v, 1) for v in smooth],
            "enc_max_capped": max_ok >= cap_max,
            "saturation_pct": saturation_pct}


def _run_round_robin(cfg, gpus, total, seconds, resolution, codec):
    """Launch `total` encodes spread over all GPUs; return (ok, failed, agg)."""
    tmpdir = os.path.join(cfg.state_dir, "probe")
    util.ensure_dir(tmpdir)
    t0 = time.time()
    procs = []
    for i in range(total):
        g = gpus[i % len(gpus)]["index"]
        out = os.path.join(tmpdir, "glob_%d_%d.mkv" % (total, i))
        cmd = [cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
               "-f", "lavfi", "-i", "testsrc2=size=%s:rate=30:duration=%d" % (resolution, seconds),
               "-c:v", codec, "-preset", "p4", "-cq", "28", "-gpu", str(g), out]
        try:
            procs.append((subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.PIPE), out))
        except OSError:
            pass
    failed = 0
    for proc, out in procs:
        try:
            proc.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            failed += 1
            continue
        if proc.returncode != 0:
            failed += 1
        try:
            os.unlink(out)
        except OSError:
            pass
    wall = max(0.001, time.time() - t0)
    ok = total - failed
    return ok, failed, (ok * seconds * 30.0) / wall


def _probe_global(cfg, gpus, max_total, seconds, resolution, codec):
    """Measure the *system-wide* NVENC session budget.

    Some driver patches expose the session limit globally (not per card): the
    12th/13th session fails with NV_ENC_ERR_INCOMPATIBLE_CLIENT_KEY no matter
    which GPU opens it.  Probing cards in parallel would then randomly blame
    whichever card lost the race, which is why two identical 2080 Ti's can show
    different ceilings.  Measure the global budget explicitly instead.
    """
    if not gpus:
        return 0, []
    samples = []
    best = 0
    step = max(1, len(gpus))
    for total in range(max(1, step), max_total + 1, step):
        ok, failed, agg = _run_round_robin(cfg, gpus, total, seconds, resolution, codec)
        if failed:
            ok2, failed2, agg2 = _run_round_robin(cfg, gpus, total, seconds, resolution, codec)
            if failed2 == 0:
                ok, failed, agg = ok2, 0, agg2
        samples.append({"total": total, "ok": ok, "failed": failed, "agg_fps": round(agg, 1)})
        if failed:
            break
        best = total
    return best, samples


def apply_sat_result(cap, old, sat):
    """Merge a saturation result into cap; a failed probe keeps the last good one."""
    if sat.get("probe_failed"):
        if old and old.get("enc_max"):
            for key in ("enc_max", "recommended_parallel", "agg_fps", "samples",
                        "smooth", "enc_max_capped"):
                if key in old:
                    cap[key] = old[key]
            cap["probe_warning"] = "探测时 NVENC 被占用，保留上次结果"
        else:
            cap.update({"enc_max": 2, "recommended_parallel": 1, "probe_ok": False,
                        "probe_warning": "探测失败（NVENC 被占用？）"})
    else:
        cap.update(sat)
    return cap


def _caps_default(gpu):
    cap = dict(FALLBACK_CAP)
    cap.update({"index": gpu["index"], "uuid": gpu["uuid"], "name": gpu["name"],
                "mem_total_mb": gpu.get("mem_total_mb", 0)})
    return cap


def probe_gpus(cfg, progress=None, force=False):
    """Probe every GPU (in parallel) and return {'probed_at', 'driver', 'gpus': {uuid: cap}}."""
    gpus = list_gpus(cfg)
    cached = util.read_json(cfg.gpu_cache_path(), {}) or {}
    ttl = float(cfg.get("gpu.probe.ttl_hours", 24)) * 3600
    drv = driver_version(cfg)

    def fresh(uuid, cap):
        if force or not cap.get("probe_ok"):
            return False
        if ttl and time.time() - float(cap.get("probed_at") or 0) > ttl:
            return False
        if cached.get("driver") and drv and cached.get("driver") != drv:
            return False
        return True

    if not gpus:
        return {"probed_at": time.time(), "driver": drv, "probe_ok": False, "gpus": {}}

    probe_cfg = cfg.get("gpu.probe") or {}
    seconds = int(probe_cfg.get("sample_seconds", 2) or 2)
    cap_max = int(probe_cfg.get("max_test", 8) or 8)
    resolution = probe_cfg.get("resolution", "1280x720")
    codec = probe_cfg.get("codec", "hevc_nvenc")
    sat = probe_cfg.get("saturation_pct", 92)

    result = {"probed_at": time.time(), "driver": drv, "probe_ok": True, "gpus": {}}
    workdir = os.path.join(cfg.state_dir, "probe")
    util.ensure_dir(workdir)
    todo = []
    for gpu in gpus:
        uuid = gpu["uuid"]
        old = (cached.get("gpus") or {}).get(uuid)
        if not probe_cfg.get("enabled", True):
            cap = _caps_default(gpu)
            result["gpus"][uuid] = cap
            continue
        if old and fresh(uuid, old) and not force:
            cap = dict(old)
            cap.update({"index": gpu["index"], "name": gpu["name"],
                        "mem_total_mb": gpu.get("mem_total_mb", 0)})
            result["gpus"][uuid] = cap
            continue
        todo.append(gpu)

    def work(gpu):
        uuid = gpu["uuid"]
        cap = _caps_default(gpu)
        cap["probed_at"] = time.time()
        if progress:
            progress("probing gpu %d (%s): encoders/decoders" % (gpu["index"], gpu["name"]))
        enc = _probe_encoders(cfg, gpu["index"])
        dec = _probe_decoders(cfg, gpu["index"], workdir)
        cap.update({"enc_codecs": enc, "dec_codecs": dec, "probe_ok": bool(enc)})
        result["gpus"][uuid] = cap
        return cap

    threads = [threading.Thread(target=work, args=(g,)) for g in todo]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Saturation / session limits are measured one card at a time so a global
    # driver budget cannot make identical cards report different ceilings.
    sat = probe_cfg.get("saturation_pct", 85)
    tp = int(probe_cfg.get("target_parallel") or 0)
    busy = encoder_sessions(cfg)
    for gpu in gpus:
        if busy.get(gpu["index"], 0) > 0:
            cap = result["gpus"].get(gpu["uuid"])
            old = (cached.get("gpus") or {}).get(gpu["uuid"]) or {}
            if cap is not None and old:
                for key in ("enc_max", "recommended_parallel", "agg_fps", "samples",
                            "smooth", "enc_max_capped"):
                    if key in old:
                        cap[key] = old[key]
                cap["probe_warning"] = "探测时该卡有 %d 个编码会话，保留上次结果" % busy[gpu["index"]]
            if progress:
                progress("gpu %d 有 %d 个编码会话在占用，跳过探测（保留上次结果）"
                         % (gpu["index"], busy[gpu["index"]]))
            continue
        cap = result["gpus"].get(gpu["uuid"])
        if not cap or not cap.get("probe_ok") or not force and cap.get("samples"):
            continue
        if not probe_cfg.get("enabled", True):
            continue
        enc = cap.get("enc_codecs") or []
        if not enc:
            continue
        if progress:
            progress("probing gpu %d: session limit / scaling (serial)" % gpu["index"])
        sat_info = _probe_saturation(cfg, gpu["index"], cap_max, seconds, resolution,
                                     codec if codec in enc else enc[0], sat, tp)
        old = (cached.get("gpus") or {}).get(gpu["uuid"]) or {}
        apply_sat_result(cap, old, sat_info)
        if progress:
            progress("gpu %d: 会话上限=%s 推荐并发=%s 聚合=%s fps" % (
                gpu["index"], cap.get("enc_max"), cap.get("recommended_parallel"), cap.get("agg_fps")))

    if probe_cfg.get("enabled", True) and todo and not any(v > 0 for v in busy.values()):
        if progress:
            progress("measuring system-wide NVENC session budget")
        gmax, gsamples = _probe_global(cfg, gpus, int(probe_cfg.get("max_total", 24) or 24),
                                       seconds, resolution, codec)
        if gmax:
            result["global_max"] = gmax
            result["global_samples"] = gsamples
        elif cached.get("global_max"):
            result["global_max"] = cached.get("global_max")
        if progress:
            progress("系统级 NVENC 会话上限 = %s（各卡合计请求 %s）" % (
                gmax or "?", sum(int(result["gpus"][g["uuid"]].get("recommended_parallel") or 0)
                                 for g in gpus if g["uuid"] in result["gpus"])))
    elif "global_max" in cached:
        result["global_max"] = cached.get("global_max")

    try:
        util.atomic_write_json(cfg.gpu_cache_path(), result)
    except OSError:
        pass
    return result


def load_caps(cfg, live=None):
    """Return {uuid: cap} merging cache with live GPU list (adds defaults for new cards)."""
    cached = util.read_json(cfg.gpu_cache_path(), {}) or {}
    caps = {}
    for uuid, cap in (cached.get("gpus") or {}).items():
        merged = dict(FALLBACK_CAP)
        merged.update(cap)
        caps[uuid] = merged
    for gpu in live or []:
        if gpu["uuid"] not in caps:
            caps[gpu["uuid"]] = _caps_default(gpu)
    return caps


def caps_for(cfg, caps, gpu):
    cap = (caps or {}).get(gpu.get("uuid"))
    if not isinstance(cap, dict) or "enc_max" not in cap:
        cap = _caps_default(gpu)
    return cap


def estimate_vram_mb(cfg, width, height):
    table = cfg.get("gpu.vram_per_job_mb") or {}
    # buckets are keyed by frame height (720/1080/1440/2160), which is what
    # actually drives NVENC surface memory
    px = int(height or 0) or max(int(width or 0), int(height or 0))
    buckets = sorted(((int(k), int(v)) for k, v in table.items()), key=lambda kv: kv[0])
    if not buckets:
        return 512
    for b, v in buckets:
        if px <= b:
            return v
    return buckets[-1][1]


def caps_degraded(caps):
    """True when a cached probe result looks like it was taken on a busy card."""
    return any(int(c.get("enc_max") or 0) <= 1 for c in (caps or {}).values())


def enc_label(cap):
    """'m6' / 'm8+' ('+' = probe stopped at max_test, real limit may be higher)."""
    n = cap.get("enc_max")
    if not n:
        return "-"
    return "m%d%s" % (n, "+" if cap.get("enc_max_capped") else "")


def per_gpu_limit(cfg, cap=None):
    """Manual per-card concurrency (every GPU gets this limit)."""
    return max(1, int(cfg.get("gpu.max_parallel_per_gpu") or 4))


def choose_gpu(cfg, live, jobs_by_gpu, job_meta, excluded=(), limits=None):
    """Pick a GPU for a job (dict with width/height/est_vram_mb).

    jobs_by_gpu: {index: running_count}; limits: optional {index: max}
    Returns (gpu_dict, limit) or (None, 0).
    """
    if not live:
        return None, 0
    prefer = str(cfg.get("gpu.prefer") or "auto")
    excluded = set(str(x) for x in (excluded or ()))
    site_exclude = set(str(x) for x in (cfg.get("gpu.exclude") or []))
    reserve = float(cfg.get("gpu.reserve_vram_mb") or 0)
    min_free = float(cfg.get("gpu.min_free_vram_mb") or 0)
    est = float(job_meta.get("est_vram_mb") or 0)
    best = None
    for gpu in live:
        idx = str(gpu["index"])
        if idx in excluded or gpu["uuid"] in excluded or idx in site_exclude or gpu["uuid"] in site_exclude:
            continue
        limit = int((limits or {}).get(gpu["index"]) or per_gpu_limit(cfg))
        running = int(jobs_by_gpu.get(gpu["index"], 0))
        if running >= limit:
            continue
        free = float(gpu.get("mem_free_mb") or 0) - reserve
        if free - est < min_free:
            continue
        load = running / float(limit)
        headroom = free / max(1.0, float(gpu.get("mem_total_mb") or 1))
        if prefer == "least-loaded":
            key = (load, -headroom, gpu["index"])
        elif prefer == "round-robin":
            key = (running, gpu["index"])
        else:  # auto: balance first, then give big jobs to cards with room
            key = (load, -(free / max(1.0, est + reserve)), gpu["index"])
        if best is None or key < best[0]:
            best = (key, gpu, limit)
    if best is None:
        return None, 0
    return best[1], best[2]


def summary_line(gpu, cap, running, limit):
    name = re.sub(r"NVIDIA\s+", "", gpu.get("name") or "?")
    return {
        "index": gpu["index"], "name": name, "uuid": gpu["uuid"],
        "util": gpu.get("util"), "mem_used": gpu.get("mem_used_mb"),
        "mem_total": gpu.get("mem_total_mb"), "temp": gpu.get("temp"),
        "power": gpu.get("power"), "power_limit": gpu.get("power_limit"),
        "enc_running": running, "enc_limit": limit,
        "enc_max": cap.get("enc_max"), "agg_fps": cap.get("agg_fps"),
        "enc_codecs": cap.get("enc_codecs") or [],
        "dec_codecs": cap.get("dec_codecs") or [],
        "foreign_mem_mb": gpu.get("foreign_mem_mb", 0),
        "procs": gpu.get("procs") or [],
    }
