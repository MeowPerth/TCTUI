"""nvtop-style curses UI.

Rendering goes through a tiny cell buffer (Screen) with two backends:
  * CursesBackend  - real terminal
  * DumpBackend    - ANSI snapshot, used by `tctui ui --dump` (screenshots/tests)
CJK-width aware so Chinese library names line up.
"""
from __future__ import annotations

import curses
import json
import locale
import os
import shutil
import subprocess
import sys
import time
import unicodedata

from . import config as cfgmod
from . import gpu, util
from .db import DB

# ------------------------------------------------------------------ styles

STYLES = [
    ("norm", "default", "default", False),
    ("title", "black", "cyan", True),
    ("head", "cyan", "default", True),
    ("dim", "brightblack", "default", False),
    ("ok", "green", "default", False),
    ("warn", "yellow", "default", False),
    ("err", "red", "default", True),
    ("sel", "white", "blue", True),
    ("accent", "magenta", "default", False),
    ("gpu", "yellow", "default", True),
    ("pause", "black", "yellow", True),
    ("progress", "green", "default", False),
    ("bar_bg", "brightblack", "default", False),
    ("white", "white", "default", False),
    ("blue", "blue", "default", True),
    ("bar_warn", "yellow", "default", False),
    ("bar_err", "red", "default", False),
    ("scan", "black", "blue", True),
    ("okbg", "black", "green", True),
    ("errbg", "white", "red", True),
]
S = {name: i for i, (name, _f, _b, _bold) in enumerate(STYLES)}

ANSI_FG = {
    "default": 39, "black": 30, "red": 31, "green": 32, "yellow": 33, "blue": 34,
    "magenta": 35, "cyan": 36, "white": 37, "brightblack": 90,
}
ANSI_BG = {
    "default": 49, "black": 40, "red": 41, "green": 42, "yellow": 43, "blue": 44,
    "magenta": 45, "cyan": 46, "white": 47, "brightblack": 100,
}
CURSES_COLOR = {
    "default": -1, "black": curses.COLOR_BLACK, "red": curses.COLOR_RED,
    "green": curses.COLOR_GREEN, "yellow": curses.COLOR_YELLOW, "blue": curses.COLOR_BLUE,
    "magenta": curses.COLOR_MAGENTA, "cyan": curses.COLOR_CYAN, "white": curses.COLOR_WHITE,
    "brightblack": curses.COLOR_BLACK,
}

SPARK = "▁▂▃▄▅▆▇█"
EIGHTHS = " ▏▎▍▌▋▊▉█"


def _curses_btn(name, fallback=0):
    """ncurses builds differ (e.g. DSM's Python has no BUTTON5_*); never crash."""
    return getattr(curses, name, fallback)


def mouse_masks():
    """bstate masks, tolerant to Python builds that omit BUTTON5_*.

    Button N occupies 5 bits of the ncurses bstate, so BUTTON5_PRESSED is
    BUTTON4_PRESSED << 5.  DSM's Python 3.8 exposes BUTTON1..4 only, yet the
    wheel-down event still arrives with that bit set.
    """
    up = _curses_btn("BUTTON4_PRESSED")
    down = _curses_btn("BUTTON5_PRESSED", (up << 5) if up else 0x200000)
    return {
        "left": _curses_btn("BUTTON1_CLICKED") | _curses_btn("BUTTON1_PRESSED"),
        "right": _curses_btn("BUTTON3_CLICKED") | _curses_btn("BUTTON3_PRESSED"),
        "up": up,
        "down": down,
    }


def _cw(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


class Screen:
    """Double-buffered cell screen; attrs are STYLES indices."""

    def __init__(self, w, h):
        self.w = max(1, w)
        self.h = max(1, h)
        self.cells = [[(" ", S["norm"]) for _ in range(self.w)] for _ in range(self.h)]

    def put(self, y, x, text, attr=S["norm"], maxw=None):
        if y < 0 or y >= self.h:
            return
        if not text:
            return
        limit = self.w - x if maxw is None else min(maxw, self.w - x)
        if limit <= 0:
            return
        cx = x
        for ch in str(text):
            cw = _cw(ch)
            if cw == 0:
                continue
            if cx < 0:            # clip from the left (never index -1 => row tail)
                cx += cw
                limit -= cw
                continue
            if limit < cw or cx + cw > self.w:
                break
            self.cells[y][cx] = (ch, attr)
            if cw == 2:
                self.cells[y][cx + 1] = ("", attr)
            cx += cw
            limit -= cw

    def fill(self, y, x, w, ch=" ", attr=S["norm"]):
        if y < 0 or y >= self.h or w <= 0:
            return
        if x < 0:                 # clip left: keep the visible right part
            w += x
            x = 0
        for i in range(max(0, min(w, self.w - x))):
            self.cells[y][x + i] = (ch, attr)

    def hline(self, y, x, w, ch="─", attr=S["dim"], left="", right="", title=""):
        self.fill(y, x, w, ch, attr)
        if left:
            self.put(y, x, left, attr)
        if right:
            self.put(y, x + w - util.disp_width(right), right, attr)
        if title:
            self.put(y, x + 2, " %s " % title, attr)

    def bar(self, y, x, w, frac, attr, bg_attr=S["bar_bg"], ch="█", bg="░"):
        frac = util.clamp(frac, 0.0, 1.0)
        self.fill(y, x, w, bg, bg_attr)
        total_eighths = int(round(frac * w * 8))
        full, rem = divmod(total_eighths, 8)
        if full:
            self.fill(y, x, min(full, w), ch, attr)
        if full < w and rem:
            self.put(y, x + full, EIGHTHS[rem], attr)

    def text(self, y, x, w, text, attr=S["norm"], align="left"):
        self.put(y, x, util.pad_disp(text, w, align), attr)

    def row(self, y):
        out = []
        cur = None
        buf = []
        for ch, attr in self.cells[y]:
            if ch == "":
                continue
            if attr != cur:
                if cur is not None:
                    out.append((cur, "".join(buf)))
                cur = attr
                buf = [ch]
            else:
                buf.append(ch)
        if cur is not None:
            out.append((cur, "".join(buf)))
        return out


class CursesBackend:
    def __init__(self, stdscr):
        self.scr = stdscr
        curses.start_color()
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        for i, (_name, fg, bg, _bold) in enumerate(STYLES):
            pair = i + 1
            exists = pair <= curses.COLOR_PAIRS - 1
            if exists:
                try:
                    curses.init_pair(pair, CURSES_COLOR.get(fg, -1), CURSES_COLOR.get(bg, -1))
                except curses.error:
                    pass
        self._attr = []
        for i, (_n, _f, _b, bold) in enumerate(STYLES):
            a = curses.color_pair(min(i + 1, curses.COLOR_PAIRS - 1))
            if bold:
                a |= curses.A_BOLD
            if _n == "dim":
                a |= curses.A_DIM
            self._attr.append(a)

    def size(self):
        h, w = self.scr.getmaxyx()
        return w, h

    def draw(self, screen):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        for y in range(min(screen.h, h)):
            x = 0
            for attr, text in screen.row(y):
                if not text:
                    continue
                if x >= w:
                    break
                try:
                    self.scr.addstr(y, x, text, self._attr[attr])
                except curses.error:
                    pass
                x += util.disp_width(text)
        self.scr.noutrefresh()
        curses.doupdate()


class DumpBackend:
    def __init__(self, w, h, color=True):
        self.w, self.h = w, h
        self.color = color

    def draw(self, screen):
        lines = []
        for y in range(screen.h):
            buf = []
            cur = None
            for attr, text in screen.row(y):
                if self.color:
                    code = self._code(attr)
                    if attr != cur:
                        buf.append(code)
                        cur = attr
                buf.append(text)
            if self.color:
                buf.append("\x1b[0m")
            lines.append("".join(buf).rstrip())
        return "\n".join(lines)

    def _code(self, attr):
        name, fg, bg, bold = STYLES[attr]
        # full state every time: partial SGR leaks background across runs
        parts = ["0"] + (["1"] if bold else []) + [str(ANSI_FG.get(fg, 39)),
                                                   str(ANSI_BG.get(bg, 49))]
        return "\x1b[%sm" % ";".join(parts)


# ------------------------------------------------------------------ view model

class View:
    def __init__(self):
        self.jobs = []
        self.counts = {}
        self.totals = {}
        self.gpus = []
        self.hist = {}
        self.fleet = []
        self.hb = {}
        self.daemon_alive = False
        self.per_gpu = {}
        self.events = []
        self.queued_media = 0.0
        self.running_fps = 0.0
        self.ts = 0.0


STATUS_STYLE = {
    "queued": S["dim"], "running": S["ok"], "verifying": S["warn"],
    "done": S["dim"], "kept": S["warn"], "failed": S["err"],
    "canceled": S["dim"], "interrupted": S["err"],
}
STATUS_LABEL = {
    "queued": "排队", "running": "转码", "verifying": "校验", "done": "完成",
    "kept": "保留", "failed": "失败", "canceled": "取消", "interrupted": "中断",
}

# Mouse/keyboard editable settings.  Each entry writes config.json directly
# (the daemon hot-reloads within 2s), so no restart is ever needed.
HELP_ENTRIES = [
    ("任务", "↑ ↓ / PgUp PgDn / Home End", "选择任务、翻页、跳到首尾"),
    ("任务", "x", "取消选中任务（还原源文件）"),
    ("任务", "r", "重试选中任务（失败/取消/中断/保留）"),
    ("任务", "d", "显示/隐藏详情面板"),
    ("任务", "l", "详情面板切换为任务日志"),
    ("队列", "s", "扫描当前路径（扫描时底栏显示“扫描中”）"),
    ("队列", "a", "设置扫描路径（替换）并立即扫描"),
    ("队列", "C", "清空排队任务（连按两次确认）"),
    ("队列", "T", "清零累计统计（连按两次确认）"),
    ("队列", "Space", "暂停/恢复派发（暂停时底栏显示“暂停中”）"),
    ("队列", "D", "启动/关闭守护（底栏显示“守护已启动 / 守护已关闭”）"),
    ("设置", "m / 点标题 ⚙ / 点底栏", "打开设置面板"),
    ("设置", "↑ ↓ / ← → / Enter", "选择、调整、切换"),
    ("设置", "点击值 / 右键", "直接修改 / 反向调整"),
    ("设置", "1-9", "启用/停用对应编号的显卡"),
    ("设置", "FFmpeg / FFprobe 路径", "点数值修改；回车保存并自动校验能否运行"),
    ("设置", "扫描路径", "点数值修改；设置后一直用它，直到再次修改"),
    ("设置", "扫描格式面板", "↑↓←→ 移动，空格勾选，Esc 返回；底部有 全选/仅常用/反选"),
    ("设置", "Esc / m / q", "关闭面板"),
    ("系统", "?", "打开/关闭本帮助"),
    ("系统", "q", "退出界面（守护进程继续跑）"),
    ("显卡", "编码并发 1/2", "正在用的编码路数 / 调度允许的并发上限"),
    ("鼠标", "点任务行", "选中任务"),
    ("鼠标", "点 GPU 行 / 标题 ⚙ / 底栏按钮", "打开设置 / 执行对应操作"),
    ("鼠标", "滚轮", "任务列表、设置面板、帮助 上下滚动"),
    ("鼠标", "点弹层外面", "关闭弹层"),
    ("输入", "回车", "确认（扫描路径 / FFmpeg 路径）"),
    ("输入", "Esc", "取消输入"),
    ("输入", "退格", "删除一个字符"),
    ("输入", "中文", "路径支持中文和空格"),
]

CODEC_LABEL = {
    "hevc_nvenc": "H.265", "h264_nvenc": "H.264", "av1_nvenc": "AV1",
    "libx265": "x265(CPU)", "libx264": "x264(CPU)",
}
COMMON_EXTS = ["mp4", "mkv", "avi", "mov", "m4v", "ts", "wmv", "flv", "webm", "m2ts", "rmvb", "mpg", "mpeg"]

SETTINGS_DEFS = [
    {"key": "paths.ffmpeg", "kind": "text", "label": "FFmpeg 路径"},
    {"key": "paths.ffprobe", "kind": "text", "label": "FFprobe 路径"},
    {"key": "scan.path", "kind": "text", "label": "扫描路径"},
    {"key": "encode.video_codec", "kind": "enum", "label": "视频编码", "values": []},
    {"key": "encode.cq", "kind": "int", "label": "CQ 画质/体积",
     "values": list(range(18, 41))},
    {"key": "encode.preset", "kind": "enum", "label": "NVENC 预设",
     "values": ["p1", "p2", "p3", "p4", "p5", "p6", "p7"]},
    {"key": "encode.out_ext", "kind": "enum", "label": "输出格式(容器)",
     "values": ["mkv", "mp4", "mov"]},
    {"key": "encode.max_width", "kind": "int", "label": "最大宽度(0=原样)",
     "values": [0, 640, 720, 1280, 1920, 2560, 3840]},
    {"key": "encode.audio", "kind": "enum", "label": "音频",
     "values": ["copy", "aac", "opus", "flac", "none"]},
    {"key": "encode.subs", "kind": "enum", "label": "字幕",
     "values": ["copy", "none"]},
    {"key": "encode.hdr", "kind": "enum", "label": "HDR 处理",
     "values": ["auto", "force-sdr", "ignore"]},
    {"key": "policy.keep_original_min_savings_pct", "kind": "int",
     "label": "低于此压缩率保留源", "values": [0, 3, 5, 8, 10, 12, 15, 20, 25, 30]},
    {"key": "policy.mark_kept_as_processed", "kind": "bool",
     "label": "\u201c保留\u201d记为已处理"},
    {"key": "policy.verify_min_duration_ratio", "kind": "enum", "label": "时长校验比例",
     "values": [0, 0.90, 0.95, 0.97, 0.99]},
    {"key": "series.enabled", "kind": "bool", "label": "自动合并分集"},
    {"key": "series.delete_sources_after_success", "kind": "bool",
     "label": "分集成功后删源"},
    {"key": "_skip_hevc", "kind": "bool", "label": "跳过已是 HEVC 的源"},
    {"key": "scan.skip_if_source_bitrate_kbps", "kind": "int",
     "label": "低码率跳过(kbps)", "values": [0, 800, 1000, 1200, 1500, 2000, 2500, 3000]},
    {"key": "scan.min_size_mb", "kind": "int", "label": "最小文件(MB)",
     "values": [0, 1, 5, 10, 20, 50, 100, 200]},
    {"key": "_scan_exts", "kind": "panel", "label": "扫描哪些视频格式", "panel": "exts"},
    {"key": "gpu.max_parallel_per_gpu", "kind": "int", "label": "单卡最大并发",
     "values": [1, 2, 3, 4, 5, 6, 8]},
]


class App:
    def __init__(self, cfg, demo=False, no_color=False):
        self.cfg = cfg
        self.demo = demo
        self.no_color = no_color
        self.monitor = gpu.LiveMonitor(cfg, ttl=1.0)
        self.view = View()
        self.sel_id = None
        self.sel_idx = 0
        self.scroll = 0

        self.input_mode = False
        self.input_kind = "path"        # path | ffmpeg | ffprobe
        self.input_buf = ""
        self.confirm = None            # {"action": "...", "count": n, "ts": ...}
        self.show_help = False
        self.help_scroll = 0
        self.help_visible = 8
        self.show_settings = False
        self.show_exts = False
        self.exts_sel = 0
        self.settings_sel = 0
        self.settings_scroll = 0
        self.hits = {}
        self._last_h = 40
        self.last_click = (0, 0, 0.0)
        self.show_detail = True
        self.show_log = False
        self.focus = "jobs"
        self.toast = ("", 0.0)
        self.last_refresh = 0.0
        self.log_scroll = 0
        self.refresh_seconds = float(cfg.get("ui.refresh_seconds") or 1.0)
        self.spark_n = int(cfg.get("ui.sparkline_points") or 40)
        self._db = None

    # -------------------------------------------------------------- data
    def db(self):
        if self.demo:
            return None
        if self._db is not None:
            return self._db
        try:
            self._db = DB(self.cfg.db_path(), readonly=True)
            self._db.query_one("SELECT 1 FROM jobs LIMIT 1")
            return self._db
        except Exception:  # noqa: BLE001
            self._db = None
            return None

    def db_write(self):
        try:
            return DB(self.cfg.db_path()).init()
        except Exception:  # noqa: BLE001
            return None

    def notify(self, msg):
        self.toast = (msg, time.time())

    def reload(self):
        v = self.view
        v.ts = time.time()
        if self.demo:
            self._demo_data(v)
            return
        db = self.db()
        v.gpus = self.monitor.get()
        v.hist = {}
        v.hb = util.read_json(self.cfg.heartbeat_path(), {}) or {}
        v.daemon_alive = bool(v.hb.get("pid") and util.pid_alive(v.hb.get("pid")))
        if not db:
            v.jobs = []
            return
        jobs = []
        running = db.jobs(statuses=("running", "verifying"), limit=200, order="started ASC")
        queued = db.jobs(statuses=("queued",), limit=300, order="id ASC")
        finished = [dict(r) for r in db.query(
            """SELECT * FROM jobs WHERE status IN ('done','kept','failed','canceled','interrupted')
               ORDER BY COALESCE(finished, created) DESC LIMIT 200""")]
        jobs = running + queued + finished
        v.jobs = jobs
        v.counts = db.counts()
        v.totals = db.totals()
        v.per_gpu = db.per_gpu_active()
        v.events = db.recent_events(60)
        v.queued_media = db.queue_media_seconds()
        v.fleet = db.fleet_history(600, 120)
        v.running_fps = db.running_fps()[0]
        for g in v.gpus:
            v.hist[g["index"]] = db.gpu_history(g["index"], 300, self.spark_n)
        if self.sel_id is not None:
            for i, j in enumerate(self.visible_jobs()):
                if j["id"] == self.sel_id:
                    self.sel_idx = i
                    break
        self._clamp()

    def visible_jobs(self):
        return self.view.jobs

    def selected(self):
        jobs = self.visible_jobs()
        if not jobs:
            return None
        self.sel_idx = util.clamp(self.sel_idx, 0, len(jobs) - 1)
        self.sel_id = jobs[self.sel_idx]["id"]
        return jobs[self.sel_idx]

    def _clamp(self):
        jobs = self.visible_jobs()
        if not jobs:
            self.sel_idx = 0
            return
        self.sel_idx = util.clamp(self.sel_idx, 0, len(jobs) - 1)

    # -------------------------------------------------------------- demo data
    def _demo_data(self, v):
        now = time.time()
        v.gpus = [
            {"index": 0, "uuid": "GPU-0", "name": "NVIDIA GeForce RTX 2080 Ti", "mem_total_mb": 22528,
             "mem_used_mb": 3612, "mem_free_mb": 18916, "util": 87, "temp": 61, "power": 231,
             "power_limit": 250, "enc_sessions": 3, "dec_sessions": 3, "foreign_mem_mb": 2211},
            {"index": 1, "uuid": "GPU-1", "name": "NVIDIA GeForce RTX 2060 SUPER", "mem_total_mb": 8192,
             "mem_used_mb": 4731, "mem_free_mb": 3461, "util": 94, "temp": 71, "power": 158,
             "power_limit": 160, "enc_sessions": 2, "dec_sessions": 2, "foreign_mem_mb": 2145},
            {"index": 2, "uuid": "GPU-2", "name": "NVIDIA GeForce RTX 2080 Ti", "mem_total_mb": 22528,
             "mem_used_mb": 4188, "mem_free_mb": 18340, "util": 79, "temp": 58, "power": 206,
             "power_limit": 250, "enc_sessions": 3, "dec_sessions": 3, "foreign_mem_mb": 2211},
        ]
        est = 4
        v.counts = {"queued": 37, "running": 8, "done": 1240, "kept": 96, "failed": 3, "interrupted": 1}
        v.totals = {"total": 1385, "saved": 4.62 * 1024 ** 4, "avg_speed": 2.1}
        v.per_gpu = {0: 3, 1: 2, 2: 3}
        v.queued_media = 37 * 4200.0
        names = [
            (0, "running", "电影/喋血战士/喋血战士.Bloodshot.2020.HD1080P.中英字幕.mkv", 1080, 6420, 0.62, 1.83, 39.1, "hevc", 3412),
            (1, "running", "电视剧/切尔诺贝利/Chernobyl.S01E02.1080p.mkv", 1080, 3780, 0.41, 2.10, 44.6, "h264", 1730),
            (2, "running", "电影/1917/1917.2019.UHD.BluRay.2160p.HEVC.REMUX-FraMeSToR.mkv", 2160, 7320, 0.28, 0.92, 20.4, "hevc", 4102),
            (0, "running", "动画/白蛇：缘起/白蛇：缘起.mp4", 1080, 5910, 0.77, 2.41, 52.0, "h264", 2901),
            (2, "running", "电视剧/三体/三体.S01E07.2160p.WEB-DL.mkv", 2160, 4600, 0.55, 1.18, 27.8, "h264", 3204),
            (1, "running", "纪录片/地球脉动/Planet.Earth.II.03.1080p.mkv", 1080, 3210, 0.66, 2.02, 41.2, "h264", 1508),
            (0, "running", "电影/刺客信条/刺客信条.mp4", 1080, 7080, 0.19, 1.95, 46.1, "h264", 3318),
            (2, "running", "电影/阿甘正传/Forrest.Gump.1994.2160p.mkv", 2160, 8520, 0.47, 1.05, 23.2, "hevc", 4420),
        ]
        v.jobs = []
        for i, (g, st, path, res, dur, prog, speed, fps, codec, size) in enumerate(names):
            v.jobs.append({
                "id": 1200 + i, "src": "/volume2/video/" + path, "status": st, "gpu": g,
                "progress": prog, "speed": speed, "fps": fps, "duration": dur,
                "width": res, "height": int(res * 9 / 16), "src_size": size * 1024 * 1024,
                "vcodec": codec, "started": now - (i + 1) * 137, "attempts": 1,
                "phase": "transcode", "out": "/volume2/video/" + path.rsplit(".", 1)[0] + ".mkv",
                "eta": (1 - prog) * dur / max(0.01, speed),
            })
        for i in range(8):
            v.jobs.append({
                "id": 1300 + i, "src": "/volume2/video/电影/超验骇客/超验骇客.part%d.mp4" % (i + 1),
                "status": "queued", "progress": 0, "duration": 5400, "width": 1080, "height": 1920,
                "src_size": 2 * 1024 ** 3, "vcodec": "h264",
            })
        for i in range(6):
            v.jobs.append({
                "id": 1100 + i, "src": "/volume2/video/电影/示例已完成_%d.mkv" % (i + 1),
                "status": "done" if i % 3 else "kept", "progress": 1.0,
                "bytes_in": 3 * 1024 ** 3, "bytes_out": 1.1 * 1024 ** 3, "savings_pct": 63.2,
                "finished": now - i * 900, "gpu": i % 3, "duration": 6000,
            })
        v.jobs.append({"id": 1109, "src": "/volume2/video/电影/坏文件.mp4", "status": "failed",
                       "error": "转码失败: FFmpeg退出码 1", "finished": now - 120, "gpu": 1})
        v.hb = {"pid": 4242, "ts": now, "paused": False, "busy": ""}
        v.daemon_alive = True
        for g in v.gpus:
            base = g["util"]
            v.hist[g["index"]] = [{"util": util.clamp(base + (i * 7 % 23) - 11, 0, 100),
                                   "enc": 1 + (i * 5 + g["index"]) % max(1, g["enc_sessions"] + 1)}
                                  for i in range(self.spark_n)]
        v.fleet = [{"fps": 180 + (i * 13 % 40) - 20, "running": 8, "queued": 20} for i in range(60)]
        v.events = [{"ts": now - i * 40, "level": "info", "msg": m} for i, m in enumerate([
            "启动 #1207 GPU0 刺客信条.mp4 (3.2 GiB)",
            "完成 #1199 阿甘正传.mkv 节省 58.3% (4.1 GiB -> 1.7 GiB)",
            "GPU0 会话 4/4, GPU1 3/4, GPU2 4/4",
            "跳过 白蛇：缘起.mp4: 已由 HServer 转码过(comment 标记)",
        ])]

    # -------------------------------------------------------------- drawing
    def draw(self, scr):
        w, h = scr.w, scr.h
        v = self.view
        self.hits = {"jobs": {}, "gpu": {}, "footer": [], "title": [],
                     "settings": [], "job_area": (0, 0),
                     "exts": [], "exts_btns": []}
        y = 0
        y = self.draw_title(scr, y, w)
        y = self.draw_summary(scr, y, w)
        gpu_rows = len(v.gpus) if v.gpus else 1
        gpu_block = min(gpu_rows + 1, max(2, h - y - 8))
        footer_rows = self._footer_layout(w)
        footer_h = len(footer_rows)
        if h - y - gpu_block - footer_h < 4:
            footer_h = 1
        avail = max(3, h - y - gpu_block - footer_h)
        jobs_need = len(self.visible_jobs()) + 3   # separator + header + rows
        if self.show_detail and avail >= 12:
            jobs_h = util.clamp(jobs_need, 3, avail - 5)
            detail_h = avail - jobs_h
        else:
            jobs_h = avail
            detail_h = 0
        y = self.draw_gpus(scr, y, w, gpu_block - 1)
        y = self.draw_jobs(scr, y, w, jobs_h)
        if detail_h:
            self.draw_detail(scr, y, w, detail_h)
        self.draw_footer(scr, h - footer_h, w, footer_rows[:footer_h])
        overlay_h = max(8, h - footer_h)
        if self.show_settings:
            self.draw_settings(scr, w, overlay_h)
        if self.show_exts:
            self.draw_exts(scr, w, overlay_h)
        if self.show_help:
            self.draw_help(scr, w, overlay_h)

    def draw_title(self, scr, y, w):
        v = self.view
        scr.fill(y, 0, w, " ", S["title"])
        left = " tctui %s " % cfgmod.APP_VERSION
        left_w = util.disp_width(left)
        scr.put(y, 0, left, S["title"])

        clock_txt = " %s " % time.strftime("%m-%d %H:%M:%S")
        cw = util.disp_width(clock_txt)
        clock_x = max(0, w - cw)
        scr.put(y, clock_x, clock_txt, S["title"])

        alive = v.daemon_alive
        paused = bool(v.hb.get("paused"))
        scanning = v.hb.get("busy") == "scanning"
        states = []
        if scanning:
            states.append("扫描中")
        if paused:
            states.append("已暂停")
        if not states:
            states.append("派发中" if alive else "守护未运行")
        status = "%s %s " % ("●" if alive else "○", " · ".join(states))
        if not alive:
            sattr = S["errbg"]
        elif paused:
            sattr = S["pause"]
        elif scanning:
            sattr = S["scan"]
        else:
            sattr = S["title"]
        sw = util.disp_width(status)
        status_x = max(left_w + 1, clock_x - sw)
        scr.put(y, status_x, status, sattr)

        import socket
        host = self.cfg.get("ui.host") or (socket.gethostname() or "localhost")
        host = util.truncate_disp("HServer" if self.demo else host, 14)
        host_txt = " %s " % host
        hw = util.disp_width(host_txt)
        host_x = status_x - hw
        if host_x > left_w + 2:
            scr.put(y, host_x, host_txt, S["title"])

        btn = " \u2699 设置 "
        bw = util.disp_width(btn)
        bx = (host_x if host_x > left_w + 2 else status_x) - bw - 1
        if bx > left_w + 2:
            scr.put(y, bx, btn, S["accent"])
            self.hits["title"].append((bx, bx + bw, "settings"))

        # current scan path, immediately left of the settings button
        path = self.cfg.get("scan.path") or ""
        avail = bx - left_w - 3
        path_x = None
        if avail >= 12:
            shown = util.truncate_middle(path or "未设置", max(4, avail - 7))
            seg = "扫描 %s" % shown
            px = bx - util.disp_width(seg) - 2
            if px > left_w + 2:
                path_x = px
                scr.put(y, px, "扫描 ", S["title"])
                scr.put(y, px + 5, shown,
                        S["pause"] if not path else S["title"])
        mid = " 多卡 NVENC 转码调度 "
        mx = left_w + 2
        limit = path_x if path_x is not None else bx
        if mx + util.disp_width(mid) + 2 < limit:
            scr.put(y, mx, mid, S["title"])
        return y + 1

    def draw_summary(self, scr, y, w):
        v = self.view
        c = v.counts
        scr.fill(y, 0, w, " ", S["norm"])
        x = 0
        chips = [
            ("队列", c.get("queued", 0), S["warn"]),
            ("运行", c.get("running", 0), S["ok"]),
            ("完成", c.get("done", 0), S["dim"]),
            ("保留", c.get("kept", 0), S["warn"]),
            ("失败", c.get("failed", 0) + c.get("interrupted", 0), S["err"]),
        ]
        for label, val, attr in chips:
            if label == "完成":
                scr.put(y, x, " 累计 ", S["dim"])
                x += util.disp_width(" 累计 ")
            txt = " %s %s " % (label, val)
            scr.put(y, x, txt, attr)
            x += util.disp_width(txt)
        fps = getattr(v, "running_fps", 0) or sum(
            float(j.get("fps") or 0) for j in v.jobs if j.get("status") == "running")
        running_n = c.get("running", 0)
        totals = v.totals or {}
        saved = float(totals.get("saved") or 0)
        if saved >= 0:
            saved_txt = "节省 %s" % util.fmt_bytes(saved)
        else:
            saved_txt = "增大 %s" % util.fmt_bytes(-saved)
        info = "%s   %d fps   %d 路并发" % (saved_txt, int(round(fps)), running_n)
        qmedia = getattr(v, "queued_media", 0) or 0
        if qmedia and v.counts.get("queued"):
            info += "   队列媒体 %s" % util.fmt_eta(qmedia)
        scr.put(y, x + 2, info, S["head"], maxw=max(0, w - x - 2))
        # fleet sparkline on the right
        spark = self._spark([float(p.get("fps") or 0) for p in v.fleet], 18)
        if spark and w > util.disp_width(info) + x + 30:
            scr.put(y, w - util.disp_width(spark) - 2, spark, S["accent"])
        return y + 1

    def _spark(self, vals, width, floor=0.0):
        if not vals:
            return ""
        vals = vals[-width:]
        hi = max(max(vals), floor)
        if hi <= 0:
            return ""
        out = []
        for v in vals:
            idx = int(round(v / hi * (len(SPARK) - 1)))
            out.append(SPARK[max(0, min(len(SPARK) - 1, idx))])
        return "".join(out)

    def _gpu_attr(self, util_pct):
        if util_pct is None:
            return S["dim"]
        if util_pct >= 90:
            return S["bar_err"]
        if util_pct >= 60:
            return S["warn"]
        return S["progress"]

    def draw_gpus(self, scr, y, w, rows):
        v = self.view
        narrow = w < 92
        compact = w < 84            # too tight for bars: text only
        spark_w = 12 if w >= 118 else 0
        chip = not narrow
        mem_bar_w = 8 if narrow else 10
        util_bar_w = 8 if narrow else 10
        right = w - 2 - spark_w
        pw_x = right - 8
        temp_x = pw_x - 6
        util_x = temp_x - (4 + 1 + util_bar_w + 2)
        mem_x = util_x - (10 + 1 + mem_bar_w + 2)
        name_x = 6
        name_w = max(8, mem_x - name_x - (10 if chip else 2))
        if compact:
            temp_x = w - 6
            util_x = temp_x - 7
            mem_x = util_x - 12
            sess_x = mem_x - 5
            name_w = max(0, sess_x - name_x - 2)
        scr.fill(y, 0, w, " ", S["head"])
        scr.put(y, 1, "GPU", S["head"])
        scr.put(y, name_x, util.pad_disp("型号", name_w), S["head"])
        if compact:
            scr.put(y, sess_x, "编码", S["head"], maxw=4)
        elif chip:
            scr.put(y, name_x + name_w + 1, util.pad_disp("编码并发", 9), S["head"])
        scr.put(y, mem_x, "显存", S["head"])
        scr.put(y, util_x, "使用率", S["head"])
        scr.put(y, temp_x, "温度", S["head"])
        if not narrow:
            scr.put(y, pw_x, "功耗", S["head"])
        if spark_w:
            scr.put(y, right + 2, util.pad_disp("会话走势", spark_w), S["head"])
        y += 1
        if not v.gpus:
            scr.put(y, 2, "未检测到 NVIDIA 显卡 (nvidia-smi 不可用) — 启用 CPU 回退时用 libx265 串行处理",
                    S["warn"], maxw=w - 2)
            return y + 1
        per_gpu = v.per_gpu or {}
        for i, g in enumerate(v.gpus):
            if i >= rows:
                break
            limit = gpu.per_gpu_limit(self.cfg)
            idx = g["index"]
            running = per_gpu.get(idx, 0)
            name = (g.get("name") or "").replace("NVIDIA ", "").replace("GeForce ", "")
            disabled = str(idx) in (self.cfg.get("gpu.exclude") or [])
            scr.fill(y, 0, w, " ", S["norm"])
            scr.put(y, 1, str(idx), S["gpu"])
            scr.put(y, name_x, util.truncate_disp(name, name_w), S["dim"] if disabled else S["white"])
            if compact:
                scr.put(y, sess_x, "%d/%d" % (running, limit),
                        S["ok"] if running < limit else S["warn"])
                scr.put(y, mem_x, "%3.1f/%4.1fG" % ((g["mem_used_mb"] or 0) / 1024.0,
                                                    (g["mem_total_mb"] or 1) / 1024.0), S["white"])
                u = g.get("util") or 0
                scr.put(y, util_x, "%3d%%" % u, self._gpu_attr(u))
                temp = g.get("temp")
                scr.put(y, temp_x, "%2.0f°C" % temp if temp is not None else "  -°C",
                        S["err"] if (temp or 0) >= 75 else S["white"])
                y += 1
                continue
            if chip:
                x = name_x + name_w + 1
                txt = "%d/%d" % (running, limit)
                scr.put(y, x, txt, S["ok"] if running < limit else S["warn"])
            used = g["mem_used_mb"] or 0
            total = g["mem_total_mb"] or 1
            scr.put(y, mem_x, "%3.1f/%4.1fG" % (used / 1024.0, total / 1024.0), S["white"])
            scr.bar(y, mem_x + 11, mem_bar_w, used / total, S["accent"], S["bar_bg"], "█", "░")
            u = g.get("util") or 0
            scr.put(y, util_x, "%3d%%" % u, self._gpu_attr(u))
            scr.bar(y, util_x + 5, util_bar_w, u / 100.0, self._gpu_attr(u), S["bar_bg"], "█", "░")
            temp = g.get("temp")
            scr.put(y, temp_x, "%2.0f°C" % temp if temp is not None else "  -°C",
                    S["err"] if (temp or 0) >= 75 else S["white"])
            if not narrow:
                pw = g.get("power")
                pl = g.get("power_limit")
                if pw is not None and pl:
                    scr.put(y, pw_x, "%3.0f/%3.0fW" % (pw, pl), S["dim"])
            if spark_w:
                # sparkline = *our* encode sessions (miner traffic would flatten a
                # GPU-util graph to 100% and tell us nothing)
                vals = [float(p.get("enc") or 0) for p in (v.hist.get(idx) or [])]
                busy = any(v > 0 for v in vals)
                spark = self._spark(vals, spark_w, floor=1.0)
                scr.put(y, right + 2, spark, S["accent"] if busy else S["dim"])
            self.hits["gpu"][y] = idx
            y += 1
        return y

    def _job_stage(self, job):
        st = job.get("status")
        if st == "running":
            p = job.get("phase") or ""
            return "合并" if p == "merge" else ("收尾" if p == "finalize" else "转码")
        return STATUS_LABEL.get(st, st or "?")

    def draw_jobs(self, scr, y, w, height):
        v = self.view
        jobs = self.visible_jobs()
        self._clamp()
        bar_w = 14 if w >= 110 else 8
        # column layout
        c_id = 1
        c_st = 6
        c_path = c_st + 6
        path_w = max(18, w - c_path - (bar_w + 4 + 6 + 6 + 7 + 6 + 6 + 2))
        c_bar = c_path + path_w + 1
        c_pct = c_bar + bar_w + 1
        c_speed = c_pct + 5
        c_fps = c_speed + 6
        c_eta = c_fps + 5
        scr.hline(y, 0, w, "─", S["dim"], title="任务 %d/%d" % (len(jobs), len(v.jobs)))
        y += 1
        head = util.pad_disp(" #", c_st - c_id)
        head += util.pad_disp("状态", c_st and 6)
        scr.put(y, c_id, head, S["head"])
        scr.put(y, c_path, util.pad_disp("文件", path_w), S["head"])
        scr.put(y, c_bar, util.pad_disp("进度", bar_w + 6), S["head"])
        scr.put(y, c_speed, util.pad_disp("速度", 6), S["head"])
        scr.put(y, c_fps, util.pad_disp("fps", 5), S["head"])
        show_eta = c_eta + 9 <= w
        if show_eta:
            scr.put(y, c_eta, util.pad_disp("剩余/节省", 9), S["head"])
        if w > c_eta + 12:
            scr.put(y, w - 6, util.pad_disp("显卡", 6, "right"), S["head"])
        y += 1
        body_h = height - 2
        body_h = max(1, body_h)
        sel = self.sel_idx
        if sel < self.scroll:
            self.scroll = sel
        elif sel >= self.scroll + body_h:
            self.scroll = sel - body_h + 1
        max_scroll = max(0, len(jobs) - body_h)
        self.scroll = util.clamp(self.scroll, 0, max_scroll)
        for r in range(body_h):
            i = self.scroll + r
            if i >= len(jobs):
                scr.fill(y, 0, w, " ", S["norm"])
                y += 1
                continue
            j = jobs[i]
            st = j["status"]
            is_sel = (i == sel)
            base = S["sel"] if is_sel else S["norm"]
            scr.fill(y, 0, w, " ", base)
            scr.put(y, c_id, str(j["id"]), S["sel"] if is_sel else S["dim"])
            scr.put(y, c_st, util.pad_disp(self._job_stage(j), 6), S["sel"] if is_sel else STATUS_STYLE.get(st, S["norm"]))
            src = j.get("src") or ""
            name = self._pretty_path(src)
            scr.put(y, c_path, util.truncate_middle(name, path_w), base)
            prog = float(j.get("progress") or 0)
            if st in ("done", "kept"):
                prog = 1.0
            p_attr = S["progress"] if st in ("running", "done") else (
                S["warn"] if st == "kept" else (S["err"] if st in ("failed", "interrupted") else S["dim"]))
            scr.bar(y, c_bar, bar_w, prog, p_attr, S["bar_bg"], "█", "░")
            scr.put(y, c_pct, "%3d%%" % int(prog * 100), base)
            speed = j.get("speed")
            scr.put(y, c_speed, "%5s" % (("%.2fx" % speed) if speed else "-"), base)
            fps = j.get("fps")
            scr.put(y, c_fps, "%4s" % (("%.0f" % fps) if fps else "-"), base)
            if st in ("done", "kept"):
                sv = j.get("savings_pct")
                txt = ("%+.1f%%" % sv) if sv is not None else "-"
                if show_eta:
                    scr.put(y, c_eta, util.pad_disp(txt, 9), base if is_sel else (
                        S["progress"] if (sv or 0) > 0 else S["warn"]))
            else:
                eta = j.get("eta") if st == "running" else None
                if show_eta:
                    scr.put(y, c_eta, util.pad_disp(util.fmt_eta(eta) if eta else "-", 9), base)
            if w > c_eta + 12:
                g = j.get("gpu")
                if g == -1:
                    label = "CPU"
                elif g is None:
                    label = "-"
                else:
                    label = "GPU%s" % g
                scr.put(y, w - 6, util.pad_disp(label, 6, "right"), base)
            self.hits["jobs"][y] = i
            y += 1
        self.hits["job_area"] = (self.scroll, self.scroll + body_h)
        return y

    def _pretty_path(self, path):
        root = self.cfg.get("scan.path") or ""
        if root and path.startswith(root.rstrip("/") + "/"):
            return util.rel_to(path, root)
        return path

    def draw_detail(self, scr, y, w, height):
        v = self.view
        job = self.selected()
        scr.hline(y, 0, w, "─", S["dim"], title="详情 · %s" % ("日志" if self.show_log else "任务"))
        y += 1
        if not job:
            scr.put(y, 2, "没有选中的任务", S["dim"])
            return y + height - 1
        rows = []
        name = os.path.basename(job.get("src") or "")
        rows.append(("#%s  %s" % (job["id"], util.truncate_disp(name, max(10, w - 12))), S["white"]))
        if self.show_log:
            log = util.tail_lines(job.get("log_path") or "", max(2, height - 2))
            for ln in log[-max(1, height - 2):]:
                rows.append(("  " + util.truncate_disp(ln, w - 4), S["dim"]))
        else:
            res = "%sx%s" % (job.get("width") or "?", job.get("height") or "?")
            dur = util.fmt_duration(job.get("duration") or 0)
            rows.append(("源 %s  %s  %s  %s   GPU%s" % (
                job.get("vcodec") or "?", res, util.fmt_bytes(job.get("src_size") or 0),
                dur, job.get("gpu") if job.get("gpu") is not None else "-"), S["dim"]))
            prog = float(job.get("progress") or 0)
            sp = job.get("speed")
            status = STATUS_LABEL.get(job.get("status"), job.get("status"))
            if job.get("status") == "running":
                eta = "剩余 %s" % util.fmt_eta(job.get("eta"))
            elif job.get("finished") and job.get("started"):
                eta = "耗时 %s" % util.fmt_duration(job["finished"] - job["started"])
            else:
                eta = "-"
            rows.append(("状态 %s  阶段 %s  进度 %.0f%%  速度 %s  %s  尝试 %s" % (
                status, self._job_stage(job), prog * 100,
                ("%.2fx" % sp) if sp else "-", eta, job.get("attempts") or 0), S["norm"]))
            out = job.get("out") or ""
            rows.append(("输出 %s" % (util.truncate_middle(out, max(20, w - 8)) if out else "-"), S["dim"]))
            if job.get("error"):
                rows.append(("错误 %s" % util.truncate_disp(str(job["error"]).replace("\n", " "), w - 8), S["err"]))
            else:
                sv = job.get("savings_pct")
                if sv is not None:
                    rows.append(("压缩率 %+.1f%%   %s -> %s" % (
                        sv, util.fmt_bytes(job.get("bytes_in") or 0),
                        util.fmt_bytes(job.get("bytes_out") or 0)), S["norm"]))
        used = 0
        for text, attr in rows[:max(1, height - 1)]:
            scr.put(y, 1, text, attr, maxw=w - 2)
            y += 1
            used += 1
        remain = height - 1 - used
        if remain >= 3 and not self.show_log and v.events:
            scr.hline(y, 0, w, "─", S["dim"], title="最近事件")
            y += 1
            for ev in v.events[:remain - 1]:
                lvl = ev.get("level") or "info"
                attr = S["err"] if lvl == "error" else (S["warn"] if lvl == "warn" else S["dim"])
                ts = time.strftime("%H:%M:%S", time.localtime(float(ev.get("ts") or 0)))
                txt = "%s  %s" % (ts, ev.get("msg") or "")
                scr.put(y, 1, util.truncate_disp(txt, w - 3), attr)
                y += 1
        return y

    def _footer_items(self):
        v = self.view
        paused = bool(v.hb.get("paused"))
        scanning = v.hb.get("busy") == "scanning"
        items = [("q", "退出", "q", None), ("?", "帮助", "?", None),
                 ("Space", "暂停中" if paused else "暂停", " ",
                  "pause" if paused else None),
                 ("a", "路径", "a", None),
                 ("s", "扫描中" if scanning else "扫描", "s",
                  "scan" if scanning else None),
                 ("C", "清队", "C", None), ("T", "清零", "T", None),
                 ("D", "守护已启动" if v.daemon_alive else "守护已关闭", "D",
                  "okbg" if v.daemon_alive else "errbg"),
                 ("m", "设置", "settings", None), ("x", "取消", "x", None),
                 ("r", "重试", "r", None), ("d", "详情", "d", None),
                 ("l", "日志", "l", None), ("1-9", "显卡", "settings", None)]
        return items

    def _footer_layout(self, w):
        """Every shortcut at the bottom; balanced two rows when one is not enough."""
        items = self._footer_items()

        def need(it):
            return util.disp_width(it[0]) + 1 + util.disp_width(it[1])

        def row_width(sub):
            if not sub:
                return 0
            return sum(need(it) for it in sub) + len(sub) - 1

        groups = None
        if row_width(items) <= w - 1:
            groups = [items]
        else:
            best = None
            for k in range(1, len(items)):
                wa = row_width(items[:k])
                wb = row_width(items[k:])
                if wa <= w - 1 and wb <= w - 1:
                    score = abs(wa - wb)
                    if best is None or score < best[0]:
                        best = (score, k)
            if best is not None:
                k = best[1]
                groups = [items[:k], items[k:]]
            else:
                groups = []
                cur = []
                for it in items:
                    if cur and row_width(cur + [it]) > w - 1:
                        groups.append(cur)
                        cur = []
                    cur.append(it)
                if cur:
                    groups.append(cur)
        rows = []
        for group in groups[:3]:
            row = []
            x = 1
            for it in group:
                row.append((x, it[0], it[1], it[2], it[3]))
                x += need(it) + 1
            rows.append(row)
        return rows

    def draw_footer(self, scr, y0, w, rows=None):
        rows = rows or self._footer_layout(w)
        for i in range(len(rows)):
            scr.fill(y0 + i, 0, w, " ", S["norm"])
        self.hits["footer_rows"] = list(range(y0, y0 + len(rows)))
        self.hits["footer_y"] = y0 + len(rows) - 1
        last = y0 + len(rows) - 1
        toast, ts = self.toast
        if self.confirm:
            c = self.confirm
            msg = "再按一次 %s 确认%s（Esc 取消）" % (c.get("key", "C"), c.get("label", ""))
            scr.put(last, 1, util.truncate_disp(msg, w - 2), S["err"])
            return
        if self.input_mode:
            if self.input_kind == "scanpath":
                prompt = "扫描路径（回车保存，Esc 取消）: %s█" % self.input_buf
            elif self.input_kind in ("ffmpeg", "ffprobe"):
                prompt = "%s 路径（回车保存，Esc 取消，Ctrl+U 清空）: %s█" % (
                    "FFmpeg" if self.input_kind == "ffmpeg" else "FFprobe", self.input_buf)
            else:
                prompt = "扫描路径（回车开始，Esc 取消）: %s█" % self.input_buf
            scr.put(last, 1, prompt, S["warn"], maxw=w - 2)
            return
        if toast and time.time() - ts < 4:
            scr.put(last, 1, util.truncate_disp(toast, w - 2), S["warn"])
            return
        for r, row in enumerate(rows):
            y = y0 + r
            for (x, key, label, act, flag) in row:
                kw = util.disp_width(key)
                lw = util.disp_width(label)
                if flag:
                    scr.fill(y, x, kw + 1 + lw, " ", S[flag])
                    scr.put(y, x, key, S[flag])
                    scr.put(y, x + kw + 1, label, S[flag])
                else:
                    scr.put(y, x, key, S["accent"])
                    scr.put(y, x + kw + 1, label, S["dim"])
                self.hits["footer"].append((y, x, x + kw + 1 + lw, act))
        scr.put(last, max(0, w - 2), " ", S["norm"])

    def _help_rows(self):
        rows = []
        last = None
        for sec, key, desc in HELP_ENTRIES:
            if sec != last:
                rows.append(("sec", sec, ""))
                last = sec
            rows.append(("row", key, desc))
        return rows

    def draw_help(self, scr, w, h):
        rows = self._help_rows()
        bw = min(96, w - 4)
        bh = min(len(rows) + 4, h - 2)
        x0 = (w - bw) // 2
        y0 = max(1, (h - bh) // 2)
        visible = max(1, bh - 4)
        self.help_visible = visible
        max_scroll = max(0, len(rows) - visible)
        self.help_scroll = util.clamp(self.help_scroll, 0, max_scroll)
        for i in range(bh):
            scr.fill(y0 + i, x0, bw, " ", S["norm"])
        scr.put(y0, x0, "┌" + "─" * (bw - 2) + "┐", S["head"])
        for i in range(1, bh - 1):
            scr.put(y0 + i, x0, "│", S["head"])
            scr.put(y0 + i, x0 + bw - 1, "│", S["head"])
        scr.put(y0 + bh - 1, x0, "└" + "─" * (bw - 2) + "┘", S["head"])
        scr.put(y0, x0 + 2, " 帮助 / 全部快捷键（? 或 Esc 关闭） ", S["head"])
        for i in range(visible):
            idx = self.help_scroll + i
            if idx >= len(rows):
                break
            kind, a, b = rows[idx]
            y = y0 + 2 + i
            if kind == "sec":
                scr.hline(y, x0 + 2, bw - 4, "─", S["dim"], title=a)
            else:
                scr.put(y, x0 + 3, util.pad_disp(a, 26), S["accent"], maxw=26)
                scr.put(y, x0 + 30, util.truncate_disp(b, bw - 32), S["norm"])
        hint = "? / Esc 关闭 · ↑↓ / PgUp PgDn / 滚轮 滚动"
        scr.put(y0 + bh - 2, x0 + 2, util.truncate_disp(hint, bw - 18), S["dim"])
        if max_scroll:
            scr.put(y0 + bh - 2, x0 + bw - 13,
                    util.pad_disp("%d/%d" % (self.help_scroll + 1, max_scroll + 1), 11, "right"), S["dim"])
        self.hits["help_box"] = (x0, y0, x0 + bw, y0 + bh)

    def _help_key(self, key):
        rows = len(self._help_rows())
        visible = max(1, self.help_visible)
        max_scroll = max(0, rows - visible)
        if key in ("\x1b", "?", "q", "Q", "m", "M"):
            self.show_help = False
            return None
        if key in ("\x1b[A", "k", "K"):
            self.help_scroll -= 1
        elif key in ("\x1b[B", "j", "J"):
            self.help_scroll += 1
        elif key == "KEY_PPAGE":
            self.help_scroll -= max(1, visible - 2)
        elif key == "KEY_NPAGE":
            self.help_scroll += max(1, visible - 2)
        elif key == "KEY_HOME":
            self.help_scroll = 0
        elif key == "KEY_END":
            self.help_scroll = max_scroll
        self.help_scroll = util.clamp(self.help_scroll, 0, max_scroll)
        return None

    # -------------------------------------------------------------- settings
    def _settings_rows(self):
        rows = []
        for d in SETTINGS_DEFS:
            item = dict(d)
            if item["key"] == "encode.video_codec":
                choices = self._codec_choices()
                cur = self.cfg.get("encode.video_codec")
                if cur and cur not in choices:
                    choices = choices + [cur]
                item["values"] = choices
                item["value"] = cur
                rows.append(item)
                continue
            if item["key"] == "_scan_exts":
                exts = self.cfg.get("scan.exts") or []
                item["value"] = len(exts)
                item["label"] = "扫描哪些视频格式（已选 %d 种）" % len(exts)
                rows.append(item)
                continue
            if item["key"] == "_skip_hevc":
                item["value"] = "hevc" in (self.cfg.get("scan.skip_if_codec") or [])
            elif item["kind"] == "bool":
                item["value"] = bool(self.cfg.get(item["key"]))
            else:
                item["value"] = self.cfg.get(item["key"])
                vals = item.get("values") or []
                if vals and item["value"] not in vals:
                    try:
                        item["value"] = min(vals, key=lambda v: abs(float(v) - float(item["value"] or 0)))
                    except (TypeError, ValueError):
                        item["value"] = vals[0]
            rows.append(item)
        for g in (self.view.gpus or []):
            disabled = str(g["index"]) in (self.cfg.get("gpu.exclude") or [])
            name = (g.get("name") or "").replace("NVIDIA ", "").replace("GeForce ", "")
            rows.append({"kind": "gpu", "gpu": g["index"],
                         "label": "GPU%s %s" % (g["index"], name), "value": not disabled})
        rows.append({"kind": "action", "action": "scan", "label": "立即扫描当前路径", "value": True})
        paused = bool(self.view.hb.get("paused"))
        rows.append({"kind": "action", "action": "pause",
                     "label": "派发状态（现在是%s）" % ("暂停" if paused else "运行"), "value": not paused})
        queued = int(self.view.counts.get("queued", 0))
        confirming = bool(self.confirm and self.confirm.get("action") == "clear_queue")
        rows.append({"kind": "action", "action": "clear_queue",
                     "label": ("确认清空？再点一次（%d 个）" % queued) if confirming
                              else "清空排队任务（%d 个）" % queued, "value": True})
        finished = sum(int(self.view.counts.get(s, 0)) for s in
                       ("done", "kept", "failed", "canceled", "interrupted"))
        confirming_t = bool(self.confirm and self.confirm.get("action") == "clear_finished")
        rows.append({"kind": "action", "action": "clear_finished",
                     "label": ("确认清零？再点一次（%d 条）" % finished) if confirming_t
                              else "清零累计统计（%d 条）" % finished, "value": True})
        return rows

    def _codec_choices(self):
        return ["hevc_nvenc", "h264_nvenc"]

    def _setting_value(self, row):
        if row.get("key") == "encode.video_codec":
            return CODEC_LABEL.get(row["value"], str(row["value"]))
        if row.get("key") == "encode.max_width":
            return "原样" if not row["value"] else str(row["value"])
        v = row["value"]
        return ("%g" % v) if isinstance(v, float) else str(v)

    def _setting_text(self, row):
        if row["kind"] == "action":
            return "[ 执行 ]"
        if row["kind"] == "panel":
            return "[ 打开 ]"
        if row["kind"] == "text":
            val = row["value"]
            if isinstance(val, (list, tuple)):
                val = ", ".join(str(x) for x in val)
            return util.truncate_left(str(val or "-"), 16)
        if row["kind"] in ("bool", "gpu"):
            return "[ 开 ]" if row["value"] else "[ 关 ]"
        return "◀ %s ▶" % self._setting_value(row)

    def _clamp_settings(self):
        rows = self._settings_rows()
        if not rows:
            self.settings_sel = 0
            return
        self.settings_sel = util.clamp(self.settings_sel, 0, len(rows) - 1)
        visible = max(3, self._settings_visible())
        if self.settings_sel < self.settings_scroll:
            self.settings_scroll = self.settings_sel
        elif self.settings_sel >= self.settings_scroll + visible:
            self.settings_scroll = self.settings_sel - visible + 1
        self.settings_scroll = util.clamp(self.settings_scroll, 0, max(0, len(rows) - visible))

    def _settings_visible(self):
        return max(3, min(len(self._settings_rows()), self._last_h - 7))

    def _open_settings(self):
        self.show_settings = True
        self._clamp_settings()

    def draw_settings(self, scr, w, h):
        rows = self._settings_rows()
        self._last_h = h
        pw = min(78, w - 4)
        ph = min(len(rows) + 3, h - 4)
        x0 = (w - pw) // 2
        y0 = (h - ph) // 2
        visible = ph - 3
        self._clamp_settings()
        for i in range(ph):
            scr.fill(y0 + i, x0, pw, " ", S["norm"])
        scr.put(y0, x0, "┌" + "─" * (pw - 2) + "┐", S["head"])
        for i in range(1, ph - 1):
            scr.put(y0 + i, x0, "│", S["head"])
            scr.put(y0 + i, x0 + pw - 1, "│", S["head"])
        scr.put(y0 + ph - 1, x0, "└" + "─" * (pw - 2) + "┘", S["head"])
        scr.put(y0, x0 + 2, " 设置 · 点一下就能改，保存后 2 秒生效 ", S["head"])
        label_w = max(10, pw - 26)
        ctl_w = 16
        self.hits["settings"] = []
        self.hits["settings_panel"] = (x0, y0, x0 + pw, y0 + ph)
        for r in range(visible):
            idx = self.settings_scroll + r
            if idx >= len(rows):
                break
            row = rows[idx]
            ry = y0 + 1 + r
            sel = (idx == self.settings_sel)
            base = S["sel"] if sel else S["norm"]
            if sel:
                scr.fill(ry, x0 + 1, pw - 2, " ", base)
            label = util.truncate_disp(row["label"], label_w)
            scr.put(ry, x0 + 3, label, S["sel"] if sel else S["white"])
            ctl = self._setting_text(row)
            cw = util.disp_width(ctl)
            cx = x0 + pw - 4 - cw
            if row["kind"] in ("bool", "gpu"):
                vattr = S["ok"] if row["value"] else S["err"]
                if sel:
                    vattr = S["sel"]
                scr.put(ry, cx, ctl, vattr)
                self.hits["settings"].append((ry, idx, cx, cx + cw, "toggle"))
            elif row["kind"] == "text":
                val = util.truncate_left(self._setting_text(row), ctl_w)
                scr.put(ry, cx, val, base)
                self.hits["settings"].append((ry, idx, cx, cx + ctl_w, "toggle"))
            elif row["kind"] in ("action", "panel"):
                scr.put(ry, cx, ctl, S["sel"] if sel else S["accent"])
                self.hits["settings"].append((ry, idx, cx, cx + cw, "toggle"))
            else:
                scr.put(ry, cx, "◀", S["accent"] if not sel else S["sel"])
                scr.put(ry, cx + 2, self._setting_value(row), base, maxw=ctl_w - 4)
                scr.put(ry, cx + cw - 1, "▶", S["accent"] if not sel else S["sel"])
                self.hits["settings"].append((ry, idx, cx, cx + 2, "dec"))
                self.hits["settings"].append((ry, idx, cx + cw - 2, cx + cw, "inc"))
                self.hits["settings"].append((ry, idx, cx + 2, cx + cw - 2, "inc"))
            self.hits["settings"].append((ry, idx, x0 + 2, cx - 1, "label"))
        hint = "点击值修改（路径可直接编辑）· 右键反向 · ↑↓ 选择 · ←→ 调整 · Esc 关闭"
        if len(rows) > visible:
            hint = "滚动查看 · " + hint
        scr.put(y0 + ph - 2, x0 + 2, util.truncate_disp(hint, pw - 4), S["dim"])

    # -------------------------------------------------------------- extensions panel
    def _ext_rows(self):
        active = [e.lower() for e in (self.cfg.get("scan.exts") or [])]
        rows = [(e, e in active) for e in COMMON_EXTS]
        for e in active:
            if e not in COMMON_EXTS:
                rows.append((e, True))
        return rows

    def _toggle_ext(self, ext):
        active = [e.lower() for e in (self.cfg.get("scan.exts") or [])]
        if ext in active:
            active = [e for e in active if e != ext]
        else:
            active.append(ext)
        active = [e for e in COMMON_EXTS if e in active] + [e for e in active if e not in COMMON_EXTS]
        self.cfg.set("scan.exts", active)
        if self._save_cfg():
            self.notify("扫描格式 %d 种（已保存）" % len(active))

    def _exts_set(self, exts):
        self.cfg.set("scan.exts", list(exts))
        if self._save_cfg():
            self.notify("扫描格式改为 %d 种（已保存）" % len(exts))

    def draw_exts(self, scr, w, h):
        rows = self._ext_rows()
        cols = 3
        grid_rows = (len(rows) + cols - 1) // cols
        # same width as the settings panel so it fully covers it (no bleed-through)
        pw = min(78, w - 4)
        ph = grid_rows + 5
        x0 = (w - pw) // 2
        y0 = max(1, (h - ph) // 2)
        for i in range(ph):
            scr.fill(y0 + i, x0, pw, " ", S["norm"])
        scr.put(y0, x0, "┌" + "─" * (pw - 2) + "┐", S["head"])
        for i in range(1, ph - 1):
            scr.put(y0 + i, x0, "│", S["head"])
            scr.put(y0 + i, x0 + pw - 1, "│", S["head"])
        scr.put(y0 + ph - 1, x0, "└" + "─" * (pw - 2) + "┘", S["head"])
        scr.put(y0, x0 + 2, " 扫描视频格式 · 点一下勾选/取消 ", S["head"])
        cell_w = (pw - 4) // cols
        self.hits["exts"] = []
        self.hits["exts_panel"] = (x0, y0, x0 + pw, y0 + ph)
        for i, (ext, on) in enumerate(rows):
            r, c = divmod(i, cols)
            ex = x0 + 2 + c * cell_w
            ey = y0 + 1 + r
            sel = (i == self.exts_sel)
            if sel:
                scr.fill(ey, ex, cell_w - 1, " ", S["sel"])
            box = "[✓]" if on else "[ ]"
            scr.put(ey, ex, box, S["sel"] if sel else (S["ok"] if on else S["dim"]))
            scr.put(ey, ex + 3, " %-6s" % ext, S["sel"] if sel else S["white"])
            self.hits["exts"].append((ey, i, ex, ex + cell_w - 1, "toggle"))
        by = y0 + ph - 2
        bx = x0 + 2
        for label, action in (("全选", "all"), ("仅常用", "common"), ("反选", "invert"), ("关闭", "close")):
            txt = " %s " % label
            tw = util.disp_width(txt)
            if bx + tw > x0 + pw - 2:
                break
            scr.put(by, bx, txt, S["accent"])
            self.hits.setdefault("exts_btns", []).append((by, bx, bx + tw, action))
            bx += tw + 1

    def _save_cfg(self):
        try:
            self.cfg.save()
            return True
        except OSError as exc:
            self.notify("保存失败: %r" % exc)
            return False

    def _change_setting(self, row, delta):
        if self.demo:
            self.notify("demo 模式只预览，不写配置")
            return
        kind = row["kind"]
        if kind == "text":
            self.input_mode = True
            if row["key"] == "scan.path":
                self.input_kind = "scanpath"
                self.input_buf = str(row["value"] or "")
            else:
                self.input_kind = "ffmpeg" if row["key"].endswith("ffmpeg") else "ffprobe"
                self.input_buf = str(row["value"] or "")
            return
        if kind == "panel":
            self.show_exts = True
            return
        if kind == "action":
            act = row["action"]
            if act == "pause":
                paused = bool(self.view.hb.get("paused"))
                self.command("resume" if paused else "pause")
                self.notify("已%s派发" % ("恢复" if paused else "暂停"))
            elif act == "scan":
                path = self.cfg.get("scan.path") or ""
                if not path:
                    self.notify("还没有设置扫描路径，按 a 设置")
                else:
                    self.command("scan")
                    self.notify("开始扫描: %s" % path)
            elif act == "clear_queue":
                if self.confirm and self.confirm.get("action") == "clear_queue":
                    self.confirm = None
                    self._clear_queue()
                else:
                    self._request_clear_queue()
            elif act == "clear_finished":
                if self.confirm and self.confirm.get("action") == "clear_finished":
                    self.confirm = None
                    self._clear_finished()
                else:
                    self._request_clear_finished()
            return
        if kind == "gpu":
            idx = str(row["gpu"])
            disabled = set(self.cfg.get("gpu.exclude") or [])
            if idx in disabled:
                disabled.discard(idx)
                enabled = True
            else:
                disabled.add(idx)
                enabled = False
            self.cfg.set("gpu.exclude", sorted(disabled))
            if self._save_cfg():
                self.command("set_gpu", {"index": int(idx), "enabled": enabled})
                self.notify("GPU%s 已%s" % (idx, "启用" if enabled else "停用"))
            return
        if row["key"] == "_skip_hevc":
            skip = list(self.cfg.get("scan.skip_if_codec") or [])
            if "hevc" in skip:
                skip = [x for x in skip if x != "hevc"]
            else:
                skip.append("hevc")
            self.cfg.set("scan.skip_if_codec", skip)
            if self._save_cfg():
                self.notify("跳过 HEVC = %s" % ("开" if "hevc" in skip else "关"))
            return
        if kind == "bool":
            new = not row["value"]
        else:
            vals = row.get("values") or []
            if not vals:
                return
            cur = row["value"]
            try:
                i = vals.index(cur)
            except ValueError:
                try:
                    i = min(range(len(vals)), key=lambda k: abs(float(vals[k]) - float(cur or 0)))
                except (TypeError, ValueError):
                    i = 0
            new = vals[(i + delta) % len(vals)]
        self.cfg.set(row["key"], new)
        if self._save_cfg():
            self.notify("%s = %s（已保存，2 秒内生效）" % (row["key"], ("%g" % new) if isinstance(new, float) else new))

    def _exts_key(self, key):
        if key == "?":
            self.show_help = True
            self.help_scroll = 0
            return None
        rows = self._ext_rows()
        cols = 3
        if key in ("\x1b", "m", "M", "q", "Q"):
            self.show_exts = False
            return None
        if key in ("\x1b[A", "k", "K"):
            self.exts_sel -= cols
        elif key in ("\x1b[B", "j", "J"):
            self.exts_sel += cols
        elif key in ("\x1b[D", "h", "H"):
            self.exts_sel -= 1
        elif key in ("\x1b[C", "l", "L"):
            self.exts_sel += 1
        elif key in ("\n", "\r", " "):
            if 0 <= self.exts_sel < len(rows):
                self._toggle_ext(rows[self.exts_sel][0])
            return None
        self.exts_sel = util.clamp(self.exts_sel, 0, max(0, len(rows) - 1))
        return None

    def _settings_key(self, key):
        if key == "?":
            self.show_help = True
            self.help_scroll = 0
            return None
        rows = self._settings_rows()
        if key in ("\x1b", "m", "M", "q", "Q"):
            self.show_settings = False
            return None
        if key in ("\x1b[A", "k", "K"):
            self.settings_sel -= 1
        elif key in ("\x1b[B", "j", "J"):
            self.settings_sel += 1
        elif key in ("\x1b[D", "h", "H"):
            self._change_setting(rows[self.settings_sel], -1)
        elif key in ("\x1b[C", "l", "L", "\n", "\r", " "):
            row = rows[self.settings_sel]
            if row["kind"] == "panel":
                self.show_exts = True
            else:
                self._change_setting(row, 1)
        elif key == "KEY_PPAGE":
            self.settings_sel -= 5
        elif key == "KEY_NPAGE":
            self.settings_sel += 5
        self._clamp_settings()
        return None

    def on_mouse(self, x, y, button=1):
        """button: 1=left, 3=right, 4=wheel-up, 5=wheel-down."""
        if button in (4, 5):
            step = -3 if button == 4 else 3
            if self.show_help:
                rows = len(self._help_rows())
                self.help_scroll = util.clamp(self.help_scroll + step, 0,
                                              max(0, rows - max(1, self.help_visible)))
            elif self.show_settings:
                self.settings_sel += step
                self._clamp_settings()
            else:
                self.sel_idx += step
                self._clamp()
            return
        if button == 1:
            now = time.time()
            lx, ly, lt = self.last_click
            if (lx, ly) == (x, y) and now - lt < 0.25:
                return
            self.last_click = (x, y, now)
        if self.show_exts:
            panel = self.hits.get("exts_panel")
            if panel and not (panel[0] <= x < panel[2] and panel[1] <= y < panel[3]):
                self.show_exts = False
                return
            for (by, bx1, bx2, action) in self.hits.get("exts_btns", []):
                if by == y and bx1 <= x < bx2:
                    if action == "close":
                        self.show_exts = False
                    elif action == "all":
                        self._exts_set(COMMON_EXTS[:])
                    elif action == "common":
                        self._exts_set(["mp4", "mkv", "avi", "mov", "m4v", "ts"])
                    elif action == "invert":
                        rows = self._ext_rows()
                        self._exts_set([e for e, on in rows if not on])
                    return
            rows = self._ext_rows()
            for (ey, idx, ex1, ex2, part) in self.hits.get("exts", []):
                if ey == y and ex1 <= x < ex2 and idx < len(rows):
                    self.exts_sel = idx
                    self._toggle_ext(rows[idx][0])
                    return
            return
        if self.show_help:
            box = self.hits.get("help_box")
            if box and not (box[0] <= x < box[2] and box[1] <= y < box[3]):
                self.show_help = False
            return
        if self.show_settings:
            panel = self.hits.get("settings_panel")
            if panel and not (panel[0] <= x < panel[2] and panel[1] <= y < panel[3]):
                self.show_settings = False
                return
            rows = self._settings_rows()
            for (ry, idx, cx1, cx2, part) in self.hits.get("settings", []):
                if ry != y or not (cx1 <= x < cx2) or idx >= len(rows):
                    continue
                self.settings_sel = idx
                if part == "label":
                    self._clamp_settings()
                    return
                delta = -1 if (button == 3 or part == "dec") else 1
                self._change_setting(rows[idx], delta)
                self._clamp_settings()
                return
            return
        for (bx1, bx2, action) in self.hits.get("title", []):
            if y == 0 and bx1 <= x < bx2:
                self._open_settings()
                return
        if y in self.hits.get("gpu", {}):
            self._open_settings()
            return
        if y in self.hits.get("jobs", {}):
            self.sel_idx = self.hits["jobs"][y]
            self.sel_id = None
            self._clamp()
            return
        for (hy, bx1, bx2, action) in self.hits.get("footer", []):
            if hy == y and bx1 <= x < bx2:
                if action == "settings":
                    self._open_settings()
                else:
                    self.action(action)
                return

    # -------------------------------------------------------------- actions
    def action(self, key):
        if self.show_help:
            return self._help_key(key)
        if self.confirm:
            c = self.confirm
            act = c.get("action")
            self.confirm = None
            ok = key in ("\n", "\r")
            if not ok and c.get("key") and key.upper() == c.get("key", "").upper():
                ok = True
            if ok:
                self._run_confirmed(act)
            else:
                self.notify("已取消")
            return
        if self.input_mode:
            if key in ("\n", "\r"):
                kind, buf = self.input_kind, self.input_buf
                self.input_mode = False
                self.input_buf = ""
                if kind == "path":
                    self._do_scan_path(buf)
                elif kind in ("ffmpeg", "ffprobe"):
                    self._save_binary(kind, buf)
                elif kind == "scanpath":
                    self._save_scan_path(buf)
            elif key == "\x1b":
                self.input_mode = False
                self.input_buf = ""
            elif key == "\x15":          # Ctrl-U 清空
                self.input_buf = ""
            elif key in ("\x7f", "\b"):
                self.input_buf = self.input_buf[:-1]
            elif len(key) == 1 and key.isprintable():
                self.input_buf += key
            return
        if self.show_exts:
            return self._exts_key(key)
        if self.show_settings:
            return self._settings_key(key)
        if key in ("q", "Q"):
            return "quit"
        if key == "?":
            self.show_help = not self.show_help
        elif key == "\x1b":
            self.show_help = False
        elif key in (" ",):
            self.command("resume" if self.view.hb.get("paused") else "pause")
        elif key == "s":
            path = self.cfg.get("scan.path") or ""
            if not path:
                self.notify("还没有设置扫描路径，按 a 设置")
                return
            self.command("scan")
            if self.view.daemon_alive:
                self.notify("开始扫描: %s" % path)
            else:
                self.notify("扫描已排队；守护进程未运行，按 D 启动后自动执行")
        elif key in ("a", "A"):
            self.input_mode = True
            self.input_kind = "path"
            self.input_buf = ""
        elif key == "D":
            if self.view.daemon_alive:
                self._request_stop_daemon()
            else:
                self._start_daemon()
        elif key in ("C",):
            self._request_clear_queue()
        elif key == "x":
            job = self.selected()
            if job:
                self.command("cancel", {"id": job["id"]})
                self.notify("已请求取消 #%s" % job["id"])
        elif key == "r":
            job = self.selected()
            if job:
                self.command("retry", {"id": job["id"]})
                self.notify("已重新入队 #%s" % job["id"])
        elif key == "T":
            self._request_clear_finished()
        elif key == "m":
            self._open_settings()
        elif key == "d":
            self.show_detail = not self.show_detail
        elif key == "l":
            self.show_log = not self.show_log
        elif key in ("1", "2", "3", "4", "5", "6", "7", "8", "9"):
            idx = int(key) - 1
            if any(g["index"] == idx for g in self.view.gpus):
                disabled = set(self.cfg.get("gpu.exclude") or [])
                if str(idx) in disabled:
                    disabled.discard(str(idx))
                else:
                    disabled.add(str(idx))
                self.cfg.set("gpu.exclude", sorted(disabled))
                try:
                    self.cfg.save()
                except OSError:
                    pass
                self.command("set_gpu", {"index": idx, "enabled": str(idx) not in disabled})
                self.notify("GPU%d 已%s" % (idx, "启用" if str(idx) not in disabled else "停用"))
        elif key in ("\x1b[A", "k", "K"):
            self.sel_idx -= 1
        elif key in ("\x1b[B", "j", "J"):
            self.sel_idx += 1
        elif key == "KEY_PPAGE":
            self.sel_idx -= 10
        elif key == "KEY_NPAGE":
            self.sel_idx += 10
        elif key == "KEY_HOME":
            self.sel_idx = 0
        elif key == "KEY_END":
            self.sel_idx = 10 ** 6
        self._clamp()
        return None

    # -------------------------------------------------------------- ui actions
    def _request_clear_queue(self):
        n = int(self.view.counts.get("queued", 0))
        if n <= 0:
            self.notify("队列已经是空的")
            self.confirm = None
            return
        self.confirm = {"action": "clear_queue", "key": "C", "count": n,
                        "label": "清空 %d 个排队任务" % n, "ts": time.time()}
        self.notify("")

    def _request_stop_daemon(self):
        self.confirm = {"action": "stop_daemon", "key": "D", "count": 0,
                        "label": "停止守护进程（运行中的任务会继续转码，重启后接管）",
                        "ts": time.time()}
        self.notify("")

    def _stop_daemon(self):
        if self.demo:
            self.notify("demo 模式不停止守护")
            return
        if not self.view.daemon_alive:
            self.notify("守护进程未运行")
            return
        self.command("shutdown")
        self.notify("已通知守护进程退出（运行中的任务继续，重启后接管）")

    def _request_clear_finished(self):
        n = sum(int(self.view.counts.get(s, 0)) for s in
                ("done", "kept", "failed", "canceled", "interrupted"))
        if n <= 0:
            self.notify("没有可清零的累计记录")
            self.confirm = None
            return
        self.confirm = {"action": "clear_finished", "key": "T", "count": n,
                        "label": "清零累计统计（%d 条记录，不动文件）" % n, "ts": time.time()}
        self.notify("")

    def _run_confirmed(self, act):
        if act == "clear_queue":
            self._clear_queue()
        elif act == "clear_finished":
            self._clear_finished()
        elif act == "stop_daemon":
            self._stop_daemon()

    def _clear_finished(self):
        if self.demo:
            self.notify("demo 模式不清理")
            return
        n = sum(int(self.view.counts.get(s, 0)) for s in
                ("done", "kept", "failed", "canceled", "interrupted"))
        if self.view.daemon_alive:
            self.command("clear_finished", {})
            self.notify("已通知守护进程清零 %d 条累计记录" % n)
            return
        db = self.db_write()
        if not db:
            self.notify("无法连接任务库")
            return
        try:
            n = db.clear_finished()
        finally:
            db.close()
        self.notify("已清除 %d 条记录，累计统计已归零（文件没动）" % n)
        self.reload()

    def _clear_queue(self):
        if self.demo:
            self.notify("demo 模式不清理")
            return
        n = int(self.view.counts.get("queued", 0))
        if self.view.daemon_alive:
            self.command("clear_queue", {})
            self.notify("已通知守护进程清空 %d 个排队任务（运行中的继续）" % n)
            return
        db = self.db_write()
        if not db:
            self.notify("无法连接任务库")
            return
        try:
            n = db.clear_queue()
        finally:
            db.close()
        self.notify("已清空 %d 个排队任务（运行中的继续）" % n)
        self.reload()

    def _save_scan_path(self, raw):
        raw = (raw or "").strip()
        if not raw:
            self.notify("扫描路径不能为空")
            return
        path = os.path.abspath(os.path.expanduser(raw))
        if not os.path.isdir(path):
            self.notify("目录不存在: %s" % path)
            return
        if self.demo:
            self.notify("demo 模式不保存")
            return
        self.cfg.set("scan.path", path)
        if not self._save_cfg():
            return
        self.notify("扫描路径已保存: %s（按 s 扫描）" % path)
        self.reload()

    def _save_binary(self, kind, raw):
        raw = (raw or "").strip()
        if not raw:
            self.notify("路径为空，未修改")
            return
        path = os.path.expanduser(raw)
        if not os.path.isabs(path):
            path = shutil.which(path) or ""
        if not path or not os.path.isfile(path) or not os.access(path, os.X_OK):
            self.notify("找不到可执行文件: %s" % raw)
            return
        try:
            res = subprocess.run([path, "-version"], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, timeout=8)
            first = (res.stdout or b"").decode("utf-8", "replace").splitlines()
            version = first[0][:60] if first else ""
            if res.returncode != 0:
                self.notify("%s 无法运行 (rc=%s)" % (kind, res.returncode))
                return
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.notify("%s 无法运行: %r" % (kind, exc))
            return
        if self.demo:
            self.notify("demo 模式不保存")
            return
        key = "paths.%s" % kind
        self.cfg.set(key, path)
        # keep the pair in sync when they live in the same bin directory
        other = "ffprobe" if kind == "ffmpeg" else "ffmpeg"
        other_key = "paths.%s" % other
        sibling = os.path.join(os.path.dirname(path), other)
        cur_other = self.cfg.get(other_key) or ""
        if os.path.isfile(sibling) and os.access(sibling, os.X_OK):
            if not cur_other or not os.path.isfile(cur_other):
                self.cfg.set(other_key, sibling)
        if not self._save_cfg():
            return
        self.notify("%s 已设置: %s%s" % (
            "FFmpeg" if kind == "ffmpeg" else "FFprobe", path,
            ("  (" + version + ")") if version else ""))
        self.reload()

    def _do_scan_path(self, raw):
        if self.demo:
            self.notify("demo 模式不扫描")
            return
        raw = (raw or "").strip()
        if not raw:
            path = self.cfg.get("scan.path") or ""
            if not path:
                self.notify("还没有设置扫描路径，按 a 输入一个目录")
                return
        else:
            path = os.path.abspath(os.path.expanduser(raw))
            if not os.path.isdir(path):
                self.notify("目录不存在: %s" % path)
                return
            # one persistent path: setting it again replaces the old one
            self.cfg.set("scan.path", path)
            try:
                self.cfg.save()
            except OSError as exc:
                self.notify("保存失败: %r" % exc)
                return
        self.command("scan")
        if not self.view.daemon_alive and not self.demo:
            self.notify("扫描路径已设为 %s；扫描已排队，按 D 启动后执行" % path)
        else:
            self.notify("扫描路径已设为 %s，开始扫描" % path)

    def _start_daemon(self):
        if self.demo:
            self.notify("demo 模式不启动守护")
            return
        env = dict(os.environ)
        env["TCTUI_HOME"] = self.cfg.home
        env["PYTHONPATH"] = self.cfg.py_path + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUNBUFFERED"] = "1"
        util.ensure_dir(self.cfg.logs_dir)
        try:
            logf = open(os.path.join(self.cfg.logs_dir, "daemon.log"), "a", encoding="utf-8")
        except OSError as exc:
            self.notify("无法写日志: %r" % exc)
            return
        try:
            subprocess.Popen(
                [sys.executable, "-m", "tctui", "daemon", "--start"],
                cwd=self.cfg.home, env=env, stdout=logf, stderr=subprocess.STDOUT,
                start_new_session=True)
        except OSError as exc:
            self.notify("启动守护失败: %r" % exc)
            return
        finally:
            logf.close()
        self.notify("正在启动守护进程… 队列会自动开始（2 秒后刷新）")

    def command(self, cmd, args=None):
        if self.demo:
            return
        db = self.db_write()
        if not db:
            self.notify("无法连接任务库")
            return
        try:
            db.push_command(cmd, args or {})
        except Exception as exc:  # noqa: BLE001
            self.notify("命令未写入: %r" % exc)
        finally:
            db.close()


# ------------------------------------------------------------------ entry points


def init_locale():
    """ncurses needs a UTF-8 locale to accept wide chars (Chinese paths)."""
    for loc in ("", "en_US.UTF-8", "zh_CN.UTF-8", "C.UTF-8"):
        try:
            got = locale.setlocale(locale.LC_ALL, loc)
        except locale.Error:
            continue
        if "utf" in (got or "").lower():
            return got
    try:
        return locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        return "C"


def run_ui(cfg, demo=False, no_color=False):
    app = App(cfg, demo=demo, no_color=no_color)
    init_locale()

    def loop(stdscr):
        curses.curs_set(0)
        stdscr.nodelay(False)
        stdscr.keypad(True)
        mouse_on = bool(cfg.get("ui.mouse", True))
        masks = mouse_masks()
        mask_left, mask_right = masks["left"], masks["right"]
        mask_up, mask_down = masks["up"], masks["down"]
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS if mouse_on else 0)
            curses.mouseinterval(0)
        except curses.error:
            pass
        backend = CursesBackend(stdscr)
        stdscr.timeout(250)
        app.reload()
        app.last_refresh = time.time()
        while True:
            try:
                key = stdscr.get_wch()
            except curses.error:
                key = None
            except KeyboardInterrupt:
                break
            if key is not None:
                keyname = ""
                if isinstance(key, str):
                    keyname = key
                elif key == curses.KEY_UP:
                    keyname = "\x1b[A"
                elif key == curses.KEY_DOWN:
                    keyname = "\x1b[B"
                elif key == curses.KEY_PPAGE:
                    keyname = "KEY_PPAGE"
                elif key == curses.KEY_NPAGE:
                    keyname = "KEY_NPAGE"
                elif key == curses.KEY_HOME:
                    keyname = "KEY_HOME"
                elif key == curses.KEY_END:
                    keyname = "KEY_END"
                elif key == curses.KEY_LEFT:
                    keyname = "\x1b[D"
                elif key == curses.KEY_RIGHT:
                    keyname = "\x1b[C"
                elif key == curses.KEY_RESIZE:
                    keyname = ""
                elif key == curses.KEY_MOUSE:
                    try:
                        _, mx, my, _, bstate = curses.getmouse()
                    except curses.error:
                        bstate = 0
                    btn = 0
                    if bstate & (mask_left):
                        btn = 1
                    elif bstate & (mask_right):
                        btn = 3
                    elif bstate & mask_up:
                        btn = 4
                    elif bstate & mask_down:
                        btn = 5
                    if btn:
                        try:
                            app.on_mouse(mx, my, btn)
                        except Exception as exc:  # noqa: BLE001
                            app.notify("鼠标操作失败: %r" % exc)
                    keyname = ""
                elif 0 <= key < 256:
                    keyname = chr(key)
                if keyname:
                    try:
                        if app.action(keyname) == "quit":
                            break
                    except Exception as exc:  # noqa: BLE001
                        app.notify("操作失败: %r" % exc)
            if time.time() - app.last_refresh >= app.refresh_seconds:
                app.reload()
                app.last_refresh = time.time()
            w, h = backend.size()
            if w < 40 or h < 10:
                stdscr.erase()
                stdscr.addstr(0, 0, "终端太小 (%dx%d)" % (w, h))
                stdscr.refresh()
                continue
            scr = Screen(w, h)
            app.draw(scr)
            backend.draw(scr)

    curses.wrapper(loop)


def dump_ui(cfg, width=120, height=42, color=True, demo=False, settings=False):
    app = App(cfg, demo=demo)
    app.reload()

    if settings:
        app._last_h = height
        app._open_settings()
    scr = Screen(width, height)
    app.draw(scr)
    out = DumpBackend(width, height, color=color).draw(scr)
    return out
