"""Configuration: JSON file + deep-merged defaults.

Every policy knob that the old shell script hard-coded (or silently resolved by
accident) lives here so it can be changed without touching code.
"""
from __future__ import annotations

import copy
import os
import sys

from . import util

APP_NAME = "ffmpeg-tui"
APP_VERSION = "0.1.5"

DEFAULT_FFMPEG_DIR = "/volume1/Software/其他软件/视频编辑工具/FFmpeg/ffmpeg-n7.1.3-40-gcddd06f3b9-linux64-gpl-7.1/bin"

DEFAULTS = {
    "version": 1,
    "state_dir": "",
    # ------------------------------------------------------------- binaries
    "paths": {
        "ffmpeg": os.path.join(DEFAULT_FFMPEG_DIR, "ffmpeg"),
        "ffprobe": os.path.join(DEFAULT_FFMPEG_DIR, "ffprobe"),
        "nvidia_smi": "nvidia-smi",
    },
    # ------------------------------------------------------------- scanning
    "scan": {
        # the one directory that "scan" (and startup scan) uses; empty = not set yet
        "path": "",
        "exts": ["mp4", "mkv", "avi", "mov", "flv", "wmv", "m4v", "ts", "m2ts", "rmvb", "webm", "mpg", "mpeg"],
        "exclude_dirs": ["@eaDir", "#recycle", "@tmp", ".tctui", "lost+found", "@Recycle"],
        "min_size_mb": 20,          # ignore tiny files
        "skip_if_codec": ["hevc"],  # already HEVC => nothing to gain (old script re-encoded them)
        "skip_if_tagged": "Transcoded By HServer",
        "skip_if_pix_fmt_10bit": False,
        # 0 = off.  Source already this cheap (kbps, per 1080p frame) is unlikely
        # to shrink further at the configured CQ; enable to save GPU time.
        "skip_if_source_bitrate_kbps": 0,
        "import_legacy_log": True,
        "legacy_log_name": "video_transcode_log.txt",
        "follow_symlinks": False,
        "max_files_per_scan": 20000,
        "on_start": True,
    },
    # ------------------------------------------------------------- series merge
    "series": {
        "enabled": True,
        "pattern": r"^(?P<base>.+?)[._\- ]?(?P<num>\d{1,4})$",
        "min_files": 2,
        "delete_sources_after_success": True,   # old script deleted them *before* transcoding
        "require_same_codec": True,             # concat -c copy only works for matching params
        "require_same_resolution": True,
    },
    # ------------------------------------------------------------- encode
    "encode": {
        "video_codec": "hevc_nvenc",   # hevc_nvenc | h264_nvenc | av1_nvenc
        "preset": "p7",
        "cq": 27,
        "out_ext": "mkv",
        "audio": "copy",               # copy | aac | opus | flac
        "audio_bitrate": "192k",
        "subs": "copy",                # copy | none
        "max_width": 0,                # 0 = keep resolution
        "hdr": "auto",                 # auto | force-sdr | ignore
        "tune": "",
        "tag": "Transcoded By HServer",
        "extra_video_args": [],
        "extra_args": [],
        "fallback_software_decode": True,   # retry without NVDEC when hw decode fails
        "fallback_to_cpu": False,           # last resort libx265 when all GPUs fail
        "cpu_codec": "libx265",
        "cpu_preset": "medium",
        "cpu_parallel": 1,
        "max_muxing_queue_size": 4096,
    },
    # ------------------------------------------------------------- post-transcode policy
    "policy": {
        "keep_original_min_savings_pct": 10,   # below this => keep source, discard output
        "mark_kept_as_processed": True,        # old script did; means no future retry
        "verify_min_duration_ratio": 0.97,     # output duration / source duration sanity check
        "delete_temp_on_failure": True,
        "min_free_disk_mb": 1024,
    },
    # ------------------------------------------------------------- GPUs
    "gpu": {
        "enabled": True,
        "exclude": [],                  # GPU indices or UUIDs to never use
        "reserve_vram_mb": 1024,
        "min_free_vram_mb": 512,
        "vram_per_job_mb": {"720": 320, "1080": 640, "1440": 1000, "2160": 1800},
        "max_parallel_per_gpu": 4,      # 手动：单卡最大并发（每张卡各自按这个值）
        "prefer": "auto",               # auto | least-loaded | round-robin | gpu:N
    },
    # ------------------------------------------------------------- daemon
    "daemon": {
        "poll_seconds": 0.5,
        "sample_seconds": 2.0,
        "history_hours": 2,
        "log_level": "info",
    },
    "ui": {
        "host": "",
        "mouse": True,
        "refresh_seconds": 1.0,
        "log_tail_lines": 8,
        "theme": "nvtop",
    },
}


def _deep_merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def default_home() -> str:
    env = os.environ.get("TCTUI_HOME")
    if env:
        return os.path.abspath(env)
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return here


class Config:
    def __init__(self, home=None, path=None, overrides=None):
        self.home = os.path.abspath(home or default_home())
        self.path = os.path.abspath(path) if path else os.path.join(self.home, "config.json")
        self.data = _deep_merge(DEFAULTS, {})
        loaded = util.read_json(self.path, None)
        if isinstance(loaded, dict):
            self.data = _deep_merge(self.data, loaded)
        try:
            self._mtime = os.path.getmtime(self.path)
        except OSError:
            self._mtime = 0.0
        if overrides:
            self.data = _deep_merge(self.data, overrides)
        self._normalize()
        state = self.data.get("state_dir")
        self.state_dir = os.path.abspath(state) if state else os.path.join(self.home, "state")
        util.ensure_dir(self.state_dir)
        self.logs_dir = os.path.join(self.home, "logs")
        util.ensure_dir(self.logs_dir)
        self.runtime_dir = os.path.join(self.state_dir, "run")
        util.ensure_dir(self.runtime_dir)

    def _normalize(self):
        """Migrate old config shapes in place."""
        # old configs had 0 = "use probed value"; probing is gone, so 0 means 4
        gpu_cfg = self.data.get("gpu") or {}
        if not int(gpu_cfg.get("max_parallel_per_gpu") or 0):
            gpu_cfg["max_parallel_per_gpu"] = 4
        # 0.1.0 had a `watch` list; now there is one persistent scan path
        scan_cfg = self.data.setdefault("scan", {})
        if not scan_cfg.get("path"):
            old = self.data.get("watch") or []
            if isinstance(old, str):
                old = [old]
            if old:
                scan_cfg["path"] = os.path.abspath(os.path.expanduser(str(old[-1])))
        self.data.pop("watch", None)

    @property
    def py_path(self):
        """Directory/zip that contains the `tctui` package.

        Derived from the package location, not from `home`, so the program can
        live in /opt while config/state/logs live in /var/lib/tctui.
        """
        here = os.path.abspath(__file__)
        return os.path.dirname(os.path.dirname(here))

    # dict-style access -------------------------------------------------
    def __getitem__(self, key):
        return self.data[key]

    def get(self, dotted, default=None):
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted, value):
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def save(self):
        util.atomic_write_json(self.path, self.data)
        try:
            self._mtime = os.path.getmtime(self.path)
        except OSError:
            pass

    def reload_if_changed(self):
        """Hot reload config.json (the daemon uses this every couple of seconds)."""
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return False
        if abs(mtime - self._mtime) < 0.001:
            return False
        self._mtime = mtime
        loaded = util.read_json(self.path, None)
        if isinstance(loaded, dict):
            self.data = _deep_merge(DEFAULTS, loaded)
            self._normalize()
            return True
        return False

    # convenience -------------------------------------------------------
    @property
    def ffmpeg(self):
        return self.get("paths.ffmpeg")

    @property
    def ffprobe(self):
        return self.get("paths.ffprobe")

    def db_path(self):
        return os.path.join(self.state_dir, "state.db")

    def gpu_cache_path(self):
        return os.path.join(self.state_dir, "gpus.json")

    def heartbeat_path(self):
        return os.path.join(self.runtime_dir, "daemon.json")

    def jobs_dir(self):
        d = os.path.join(self.runtime_dir, "jobs")
        util.ensure_dir(d)
        return d

    def log_path(self, name):
        return os.path.join(self.logs_dir, name)

    def check_binaries(self):
        missing = []
        for key in ("ffmpeg", "ffprobe"):
            p = self.get("paths." + key)
            if not util.which(p):
                missing.append("%s -> %s" % (key, p))
        return missing


def write_example(path):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["scan"]["path"] = "/media/videos"
    import json
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path


def load(args_config=None, home=None):
    cfg = Config(home=home, path=args_config)
    if not os.path.exists(cfg.path):
        cfg.save()
    return cfg
