"""Command line entry point:  tctui [ui|daemon|scan|doctor|status|worker|config]."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time

from . import config as cfgmod
from . import gpu, scan, util
from .db import DB


def _cfg(args):
    return cfgmod.load(args.config, home=getattr(args, "home", None))


# ------------------------------------------------------------------ ui

def cmd_ui(args):
    cfg = _cfg(args)
    from . import ui
    if args.dump or not sys.stdout.isatty():
        w, h = 120, 42
        if args.size:
            try:
                w, h = [int(x) for x in args.size.lower().split("x")]
            except ValueError:
                pass
        out = ui.dump_ui(cfg, w, h, color=not args.no_color, demo=args.demo,
                         settings=args.settings)
        sys.stdout.write(out + "\n")
        return 0
    ui.run_ui(cfg, demo=args.demo)
    return 0


# ------------------------------------------------------------------ daemon

def preflight(cfg):
    """Turn the usual permission mistakes into a readable message."""
    import sqlite3
    try:
        db = DB(cfg.db_path()).init()
        db.close()
    except (sqlite3.OperationalError, OSError) as exc:
        import pwd
        try:
            me = pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            me = str(os.getuid())
        owner = ""
        try:
            st = os.stat(cfg.db_path())
            try:
                them = pwd.getpwuid(st.st_uid).pw_name
            except KeyError:
                them = str(st.st_uid)
            owner = "（文件属主 %s，当前用户 %s）" % (them, me)
        except OSError:
            owner = "（当前用户 %s）" % me
        return ("【错误】无法打开任务库 %s%s: %s\n"
                "  请用同一个用户运行（sudo 与非 sudo 不要混用），或执行：\n"
                "  sudo chown -R %s %s" % (cfg.db_path(), owner, exc, me, cfg.home))
    try:
        probe = os.path.join(cfg.logs_dir, ".write-test")
        with open(probe, "w") as fh:
            fh.write("x")
        os.unlink(probe)
    except OSError as exc:
        return ("【错误】日志目录不可写 %s: %s\n  请检查属主/权限（是否用不同用户跑过？）"
                % (cfg.logs_dir, exc))
    return ""


def daemon_pid(cfg):
    hb = util.read_json(cfg.heartbeat_path(), {}) or {}
    pid = hb.get("pid")
    if pid and util.pid_alive(pid):
        return int(pid), hb
    return None, hb


def cmd_daemon(args):
    cfg = _cfg(args)
    if args.stop:
        pid, _ = daemon_pid(cfg)
        if not pid:
            print("守护进程未运行")
            return 0
        db = DB(cfg.db_path()).init()
        db.push_command("shutdown")
        db.close()
        for _ in range(30):
            if not util.pid_alive(pid):
                print("守护进程已停止")
                return 0
            time.sleep(0.2)
        try:
            os.kill(pid, signal.SIGTERM)
            print("已发送 SIGTERM")
        except OSError:
            pass
        return 0
    if args.status:
        pid, hb = daemon_pid(cfg)
        print(json.dumps({"running": bool(pid), "pid": pid, "heartbeat": hb},
                         ensure_ascii=False, indent=2))
        return 0
    if args.start:
        pid, _ = daemon_pid(cfg)
        if pid:
            print("守护进程已在运行 pid=%s" % pid)
            return 0
        err = preflight(cfg)
        if err:
            print(err)
            return 2
        util.ensure_dir(cfg.logs_dir)
        logf = open(os.path.join(cfg.logs_dir, "daemon.log"), "a", encoding="utf-8")
        env = dict(os.environ)
        env["TCTUI_HOME"] = cfg.home
        env["PYTHONPATH"] = cfg.py_path + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen([sys.executable, "-m", "tctui", "daemon", "--foreground"],
                                cwd=cfg.home, env=env, stdout=logf, stderr=subprocess.STDOUT,
                                start_new_session=True)
        logf.close()
        print("守护进程已启动 pid=%s  (日志: %s)" % (proc.pid, os.path.join(cfg.logs_dir, "daemon.log")))
        return 0
    missing = cfg.check_binaries()
    if missing:
        print("【错误】找不到可执行文件: " + "; ".join(missing))
        print("请检查 config.json 的 paths 配置")
        return 2
    err = preflight(cfg)
    if err:
        print(err)
        return 2
    from . import scheduler
    scheduler.run_daemon(cfg)
    return 0


# ------------------------------------------------------------------ scan / add

def cmd_scan(args):
    cfg = _cfg(args)
    missing = cfg.check_binaries()
    if missing:
        print("【错误】找不到可执行文件: " + "; ".join(missing))
        return 2
    roots = [os.path.abspath(p) for p in (args.paths or cfg.get("watch") or [])]
    if not roots:
        print("没有扫描路径，请用 `tctui scan /path` 或设置 config.json watch")
        return 2
    db = DB(cfg.db_path()).init()
    planner = scan.Planner(cfg, db)
    jobs = planner.plan(roots, progress=lambda m: print("  " + m, flush=True))
    added = 0
    for j in jobs:
        jid = db.add_job(j)
        added += 1
        print("  + #%s %s" % (jid, j["src"]))
    print("扫描完成: 新增 %d 个任务, 跳过 %d 个" % (added, len(planner.skips)))
    for s in planner.skips[:50]:
        print("  skip %s: %s" % (s["path"], s["reason"]))
    db.close()
    return 0


# ------------------------------------------------------------------ control

def _push(cfg, cmd, args=None):
    db = DB(cfg.db_path()).init()
    try:
        db.push_command(cmd, args or {})
    finally:
        db.close()


def cmd_ctl(args):
    cfg = _cfg(args)
    if args.action in ("pause", "resume"):
        _push(cfg, args.action)
        print(("已暂停派发" if args.action == "pause" else "已恢复派发") +
              "（守护进程在 0.5s 内生效）")
        return 0
    if args.action == "retry":
        for jid in args.ids or []:
            _push(cfg, "retry", {"id": int(jid)})
            print("已重新入队 #%s" % jid)
        return 0
    if args.action == "cancel":
        for jid in args.ids or []:
            _push(cfg, "cancel", {"id": int(jid)})
            print("已请求取消 #%s" % jid)
        return 0
    return 2


def cmd_clear(args):
    cfg = _cfg(args)
    db = DB(cfg.db_path()).init()
    counts = db.counts()
    scope = "all" if args.all else ("finished" if args.finished else "queued")
    labels = {"queued": ("queued",), "finished": ("done", "kept", "failed", "canceled", "interrupted"),
              "all": ("queued", "done", "kept", "failed", "canceled", "interrupted")}
    n = sum(counts.get(s, 0) for s in labels[scope])
    if n == 0:
        print("没有需要清理的任务（%s）" % scope)
        db.close()
        return 0
    print("将要清理 %d 条：%s" % (n, scope))
    if not args.yes:
        if not sys.stdin.isatty():
            print("非交互环境请加 --yes 确认")
            db.close()
            return 2
        ans = input("确认？[y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消")
            db.close()
            return 1
    pid, _ = daemon_pid(cfg)
    if pid:
        if scope in ("queued", "all"):
            _push(cfg, "clear_queue", {"mark_skipped": bool(args.mark_skipped)})
        if scope in ("finished", "all"):
            _push(cfg, "clear_finished")
        print("已通知守护进程清理")
    else:
        if scope in ("queued", "all"):
            cleared = db.clear_queue(bool(args.mark_skipped))
            print("清空排队任务 %d 个%s" % (cleared, "（已标记为跳过）" if args.mark_skipped else ""))
        if scope in ("finished", "all"):
            print("清除已完成记录 %d 条" % db.clear_finished())
    if scope in ("queued", "all") and not args.mark_skipped:
        print("提示：清掉的任务下次扫描还会重新入队；不想再入队请加 --mark-skipped")
    db.close()
    return 0


def cmd_logs(args):
    cfg = _cfg(args)
    path = os.path.join(cfg.logs_dir, "jobs", "%s.log" % args.id)
    if not os.path.isfile(path):
        path = os.path.join(cfg.logs_dir, "daemon.log")
    lines = util.tail_lines(path, args.lines)
    print("\n".join(lines))
    return 0


# ------------------------------------------------------------------ worker

def cmd_worker(args):
    from . import worker
    return worker.main(["worker", args.spec])


# ------------------------------------------------------------------ doctor

def cmd_doctor(args):
    cfg = _cfg(args)
    ok = True
    print("== 环境 ==")
    print("  home       : %s" % cfg.home)
    print("  state      : %s" % cfg.state_dir)
    print("  ffmpeg     : %s (%s)" % (cfg.ffmpeg, "ok" if util.which(cfg.ffmpeg) else "缺失"))
    print("  ffprobe    : %s (%s)" % (cfg.ffprobe, "ok" if util.which(cfg.ffprobe) else "缺失"))
    print("  磁盘(state): %s" % (util.fmt_bytes(shutil.disk_usage(cfg.state_dir).free) + " 可用"))
    missing = cfg.check_binaries()
    if missing:
        ok = False
        print("  【错误】" + "; ".join(missing))

    print("\n== 显卡 ==")
    live = gpu.list_gpus(cfg)
    if not live:
        ok = False
        print("  未检测到 NVIDIA 显卡或 nvidia-smi 不可用")
    else:
        drv = gpu.driver_version(cfg)
        print("  驱动 %s，共 %d 张卡" % (drv or "?", len(live)))
        for g in live:
            print("  GPU%-2d %-28s %5.0f MiB  单卡并发上限=%s" % (
                g["index"], g["name"], g["mem_total_mb"], gpu.per_gpu_limit(cfg)))
            if g.get("foreign_mem_mb"):
                print("        其他进程占用: %s MiB (%s)" % (
                    g["foreign_mem_mb"], ", ".join(p["name"] for p in g.get("procs") or [])))
    print("\n== 任务库 ==")
    if os.path.isfile(cfg.db_path()):
        db = DB(cfg.db_path()).init()
        counts = db.counts()
        totals = db.totals()
        print("  %s" % ", ".join("%s=%s" % (k, v) for k, v in sorted(counts.items())))
        print("  已节省 %s / 已处理记录 %d" % (util.fmt_bytes(totals.get("saved")), db.processed_count()))
        db.close()
    else:
        print("  (尚未创建)")

    pid, hb = daemon_pid(cfg)
    print("\n== 守护进程 ==")
    print("  %s" % ("运行中 pid=%s  paused=%s busy=%s" % (pid, hb.get("paused"), hb.get("busy"))
                    if pid else "未运行  (tctui daemon --start)"))

    print("\n== 残留临时文件 ==")
    orphans = find_orphans(cfg)
    if not orphans:
        print("  无")
    else:
        for p, age in orphans[:20]:
            print("  %s (%.0fh)" % (p, age / 3600.0))
        if not args.yes:
            print("  使用 `tctui clean --yes` 清理")
    return 0 if ok else 1


def find_orphans(cfg, min_age_hours=0.0):
    jobs_dir = cfg.jobs_dir()
    known = set()
    out = []
    now = util.now()
    roots = cfg.get("watch") or []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith("@")]
            for name in filenames:
                if not name.startswith(".tctui-"):
                    continue
                p = os.path.join(dirpath, name)
                try:
                    age = now - os.path.getmtime(p)
                except OSError:
                    continue
                if age >= min_age_hours * 3600:
                    out.append((p, age))
    for name in os.listdir(jobs_dir):
        p = os.path.join(jobs_dir, name)
        if os.path.isfile(p):
            try:
                age = now - os.path.getmtime(p)
            except OSError:
                continue
            if age > 3600 and age >= min_age_hours * 3600:
                out.append((p, age))
    return sorted(out, key=lambda x: -x[1])


def cmd_clean(args):
    cfg = _cfg(args)
    orphans = find_orphans(cfg, min_age_hours=args.min_age)
    if not orphans:
        print("没有残留临时文件")
        return 0
    for p, age in orphans:
        print("%s %s (%.1fh)" % ("删除" if args.yes else "将删除", p, age / 3600.0))
        if args.yes:
            try:
                os.unlink(p)
            except OSError as exc:
                print("  失败: %r" % exc)
    if not args.yes:
        print("加上 --yes 执行删除")
    return 0


# ------------------------------------------------------------------ status / config

def cmd_status(args):
    cfg = _cfg(args)
    pid, hb = daemon_pid(cfg)
    print("守护进程: %s" % ("运行中 pid=%s" % pid if pid else "未运行"))
    if hb:
        print("  心跳: %.1fs 前  paused=%s busy=%s" % (time.time() - float(hb.get("ts") or 0),
                                                    hb.get("paused"), hb.get("busy")))
    live = gpu.list_gpus(cfg)
    for g in live:
        print("  GPU%-2d %-26s %3.0f%%  %5.0f/%5.0f MiB  并发上限=%s" % (
            g["index"], (g.get("name") or "")[:26], g.get("util") or 0,
            g.get("mem_used_mb") or 0, g.get("mem_total_mb") or 0,
            gpu.per_gpu_limit(cfg)))
    if os.path.isfile(cfg.db_path()):
        db = DB(cfg.db_path()).init()
        counts = db.counts()
        totals = db.totals()
        print("任务: " + ", ".join("%s=%s" % (k, v) for k, v in sorted(counts.items())))
        saved = float(totals.get("saved") or 0)
        print(("节省: %s" if saved >= 0 else "体积增大: %s") % util.fmt_bytes(abs(saved)))
        for j in db.jobs(statuses=("running",), limit=20, order="id ASC"):
            print("  #%-4s GPU%s %5.1f%% %s %s" % (j["id"], j["gpu"], (j["progress"] or 0) * 100,
                                                   j["src"], ("%.2fx" % j["speed"]) if j["speed"] else ""))
        db.close()
    return 0


def _flatten(node, prefix=""):
    out = {}
    for k, v in (node or {}).items():
        key = "%s.%s" % (prefix, k) if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        else:
            out[key] = v
    return out


def _fmt_value(v):
    return json.dumps(v, ensure_ascii=False)


def cmd_config(args):
    cfg = _cfg(args)
    if args.where:
        print("config : %s%s" % (cfg.path, "" if os.path.isfile(cfg.path) else "  (尚未创建)"))
        print("home   : %s" % cfg.home)
        print("state  : %s" % cfg.state_dir)
        print("db     : %s" % cfg.db_path())
        print("gpu缓存: %s" % cfg.gpu_cache_path())
        print("日志   : %s" % cfg.logs_dir)
        print("默认值 : tctui config --example 会导出完整默认配置")
        return 0
    if args.example:
        path = os.path.join(cfg.home, "config.example.json")
        cfgmod.write_example(path)
        print("已写入 %s" % path)
        return 0
    if args.keys:
        flat = _flatten(cfg.data)
        for key in sorted(flat):
            print("%-38s = %s" % (key, _fmt_value(flat[key])))
        return 0
    if args.get:
        flat = _flatten(cfg.data)
        for key in args.get:
            if key in flat:
                print(_fmt_value(flat[key]))
            else:
                print("未知配置项: %s" % key, file=sys.stderr)
                return 2
        return 0
    if args.set:
        known = set(_flatten(cfgmod.DEFAULTS))
        flat_before = _flatten(cfg.data)
        pending = []
        for pair in args.set:
            key, sep, value = pair.partition("=")
            if not sep or not key:
                print("格式: key=value  例如: encode.cq=25", file=sys.stderr)
                return 2
            if key not in known:
                import difflib
                near = difflib.get_close_matches(key, sorted(known), n=3)
                print("未知配置项: %s%s" % (key, ("，你是不是想写: " + ", ".join(near)) if near else ""),
                      file=sys.stderr)
                print("用 `tctui config --keys` 查看全部可设置项", file=sys.stderr)
                return 2
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = value
            pending.append((key, parsed))
        for key, parsed in pending:
            cfg.set(key, parsed)
        cfg.save()
        flat_after = _flatten(cfg.data)
        print("已保存 %s" % cfg.path)
        for key in sorted(set(flat_before) | set(flat_after)):
            if flat_before.get(key) != flat_after.get(key):
                print("  %-34s %s  ->  %s" % (key, _fmt_value(flat_before.get(key)),
                                             _fmt_value(flat_after.get(key))))
        print("守护进程会在 2 秒内热加载")
        return 0
    print(json.dumps(cfg.data, ensure_ascii=False, indent=2))
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="tctui", description="多卡 NVENC 转码调度器 (nvtop 风格界面)")
    p.add_argument("--config", help="config.json 路径")
    p.add_argument("--home", help="安装目录 (默认脚本所在目录)")
    sub = p.add_subparsers(dest="cmd")

    u = sub.add_parser("ui", help="打开 TUI (默认)")
    u.add_argument("--demo", action="store_true", help="用假数据预览界面")
    u.add_argument("--dump", action="store_true", help="输出一帧到 stdout")
    u.add_argument("--size", help="dump 尺寸, 例如 120x42")
    u.add_argument("--no-color", action="store_true")
    u.add_argument("--settings", action="store_true", help="打开设置面板（配合 --dump 出图）")
    u.set_defaults(func=cmd_ui)

    d = sub.add_parser("daemon", help="调度守护进程")
    d.add_argument("--foreground", action="store_true")
    d.add_argument("--start", action="store_true", help="后台启动")
    d.add_argument("--stop", action="store_true", help="停止")
    d.add_argument("--status", action="store_true")
    d.set_defaults(func=cmd_daemon)

    s = sub.add_parser("scan", help="扫描目录并把任务加入队列")
    s.add_argument("paths", nargs="*")
    s.set_defaults(func=cmd_scan)
    a = sub.add_parser("add", help="scan 的别名")
    a.add_argument("paths", nargs="*")
    a.set_defaults(func=cmd_scan)

    w = sub.add_parser("worker", help="执行单个任务(内部使用)")
    w.add_argument("spec")
    w.set_defaults(func=cmd_worker)

    doc = sub.add_parser("doctor", help="环境/显卡/任务库自检")
    doc.add_argument("--yes", action="store_true")
    doc.set_defaults(func=cmd_doctor)

    c = sub.add_parser("clean", help="清理残留临时文件")
    c.add_argument("--min-age", type=float, default=24.0, help="只清理超过 N 小时的残留")
    c.add_argument("--yes", action="store_true")
    c.set_defaults(func=cmd_clean)

    for name, action, helptext in (("pause", "pause", "暂停派发"), ("resume", "resume", "恢复派发"),
                                   ("retry", "retry", "重试任务"), ("cancel", "cancel", "取消任务")):
        ctl = sub.add_parser(name, help=helptext)
        ctl.add_argument("ids", nargs="*")
        ctl.set_defaults(func=cmd_ctl, action=action)
        ctl.set_defaults()

    cl = sub.add_parser("clear", help="清空排队/已完成任务")
    cl.add_argument("--queued", action="store_true", help="只清排队（默认）")
    cl.add_argument("--finished", action="store_true", help="只清完成/失败记录")
    cl.add_argument("--all", action="store_true", help="排队 + 已完成记录")
    cl.add_argument("--mark-skipped", action="store_true", help="清排队的同时标记为跳过（以后不再自动入队）")
    cl.add_argument("--yes", action="store_true", help="不再询问")
    cl.set_defaults(func=cmd_clear)

    lg = sub.add_parser("logs", help="查看任务日志")
    lg.add_argument("id")
    lg.add_argument("--lines", type=int, default=40)
    lg.set_defaults(func=cmd_logs)

    st = sub.add_parser("status", help="文本状态")
    st.set_defaults(func=cmd_status)

    cf = sub.add_parser("config", help="查看/修改配置")
    cf.add_argument("--set", action="append", help="key=value，可重复")
    cf.add_argument("--get", action="append", help="只打印某个配置项")
    cf.add_argument("--keys", action="store_true", help="列出所有可设置项及当前值")
    cf.add_argument("--where", action="store_true", help="打印配置/状态/日志路径")
    cf.add_argument("--example", action="store_true", help="导出完整默认配置")
    cf.set_defaults(func=cmd_config)
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["ui"]
    elif argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version"):
        argv = ["ui"] + argv
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    if args.cmd == "worker":
        return args.func(args)
    return args.func(args)
