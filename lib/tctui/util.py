"""Small helpers shared by every module.  Stdlib only, Python 3.8+."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import unicodedata

# ---------------------------------------------------------------- time / numbers


def now() -> float:
    return time.time()


def fmt_bytes(n) -> str:
    """1234 -> '1.2 KiB' (IEC, one decimal)."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    if n < 0:
        return "-"
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    i = 0
    while n >= 1024.0 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    if i == 0:
        return "%d B" % int(n)
    return "%.1f %s" % (n, units[i])


def fmt_bytes_short(n) -> str:
    """Compact form used in tables: 1.2G / 812M."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    for unit, div in (("T", 1024 ** 4), ("G", 1024 ** 3), ("M", 1024 ** 2), ("K", 1024)):
        if n >= div:
            v = n / div
            return ("%.1f" % v if v < 100 else "%.0f" % v) + unit
    return "%d" % int(n)


def fmt_duration(sec) -> str:
    """3725 -> '1:02:05'."""
    try:
        sec = int(float(sec))
    except (TypeError, ValueError):
        return "-"
    if sec < 0:
        return "-"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%d:%02d" % (m, s)


def fmt_eta(sec) -> str:
    if sec is None:
        return "-"
    try:
        sec = float(sec)
    except (TypeError, ValueError):
        return "-"
    if sec != sec or sec < 0 or sec > 365 * 86400:
        return "-"
    if sec < 60:
        return "%ds" % int(sec)
    if sec < 3600:
        return "%dm%02ds" % (int(sec // 60), int(sec % 60))
    if sec < 86400:
        return "%dh%02dm" % (int(sec // 3600), int((sec % 3600) // 60))
    return "%dd%02dh" % (int(sec // 86400), int((sec % 86400) // 3600))


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def parse_speed(text):
    """'2.34x' -> 2.34 ; handles None/''."""
    if text is None:
        return None
    m = re.search(r"([0-9.]+)", str(text))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def parse_si(text):
    """parse ffmpeg progress numbers."""
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- text / width


def disp_width(s: str) -> int:
    """Terminal display width (CJK aware)."""
    w = 0
    for ch in s:
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            w += 2
        elif unicodedata.combining(ch):
            w += 0
        else:
            w += 1
    return w


def truncate_disp(s: str, width: int, ellipsis: str = "…") -> str:
    if width <= 0:
        return ""
    if disp_width(s) <= width:
        return s
    ew = disp_width(ellipsis)
    if ew >= width:
        return ellipsis[:1] * width if width else ""
    out = []
    used = 0
    for ch in s:
        cw = disp_width(ch)
        if used + cw > width - ew:
            break
        out.append(ch)
        used += cw
    return "".join(out) + ellipsis


def truncate_middle(s: str, width: int, ellipsis: str = "…") -> str:
    """Keep the beginning and the end, put the ellipsis in the middle."""
    if width <= 0:
        return ""
    if disp_width(s) <= width:
        return s
    ew = disp_width(ellipsis)
    if ew >= width:
        return ellipsis[:1] * width
    keep = width - ew
    head_w = (keep + 1) // 2
    tail_w = keep - head_w
    head = []
    used = 0
    for ch in s:
        cw = disp_width(ch)
        if used + cw > head_w:
            break
        head.append(ch)
        used += cw
    tail = []
    used = 0
    for ch in reversed(s):
        cw = disp_width(ch)
        if used + cw > tail_w:
            break
        tail.append(ch)
        used += cw
    return "".join(head) + ellipsis + "".join(reversed(tail))


def truncate_left(s: str, width: int, ellipsis: str = "…") -> str:
    """Keep the tail (useful for paths)."""
    if width <= 0:
        return ""
    if disp_width(s) <= width:
        return s
    ew = disp_width(ellipsis)
    if ew >= width:
        return ellipsis[:1] * width if width else ""
    out = []
    used = 0
    for ch in reversed(s):
        cw = disp_width(ch)
        if used + cw > width - ew:
            break
        out.append(ch)
        used += cw
    return ellipsis + "".join(reversed(out))


def pad_disp(s: str, width: int, align: str = "left") -> str:
    """Pad with spaces using display width."""
    s = truncate_disp(s, width)
    gap = width - disp_width(s)
    if gap <= 0:
        return s
    if align == "right":
        return " " * gap + s
    if align == "center":
        left = gap // 2
        return " " * left + s + " " * (gap - left)
    return s + " " * gap


# ---------------------------------------------------------------- files / json


def ensure_dir(path):
    if path and not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
    return path


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def atomic_write_json(path, obj):
    d = os.path.dirname(os.path.abspath(path)) or "."
    ensure_dir(d)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=None, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path, text):
    d = os.path.dirname(os.path.abspath(path)) or "."
    ensure_dir(d)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def which(cmd):
    if not cmd:
        return None
    if os.path.isabs(cmd):
        return cmd if os.path.isfile(cmd) and os.access(cmd, os.X_OK) else None
    return shutil.which(cmd)


def rel_to(path, root):
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return path


def pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    # a zombie still answers kill(0) but is dead for our purposes
    try:
        with open("/proc/%d/stat" % pid, "r") as fh:
            state = fh.read().rsplit(") ", 1)[-1][:1]
        if state == "Z":
            return False
    except OSError:
        pass
    return True


def proc_cmdline(pid):
    try:
        with open("/proc/%d/cmdline" % int(pid), "rb") as fh:
            return fh.read().replace(b"\x00", b" ").decode("utf-8", "replace")
    except (OSError, ValueError):
        return ""


def is_our_worker(pid):
    """Guard against pid reuse before killpg()."""
    cmd = proc_cmdline(pid)
    return "tctui" in cmd and "worker" in cmd


def tail_lines(path, limit=20):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = 4096
            data = b""
            while size > 0 and data.count(b"\n") <= limit:
                step = min(block, size)
                size -= step
                fh.seek(size)
                data = fh.read(step) + data
            text = data.decode("utf-8", "replace")
            return [ln for ln in text.splitlines() if ln.strip()][-limit:]
    except OSError:
        return []
