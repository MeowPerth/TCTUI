"""Local unit tests (run with:  python3 -m unittest discover -s tests)."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from tctui import config as cfgmod  # noqa: E402
from tctui import scan, util  # noqa: E402
from tctui.db import DB  # noqa: E402
from tctui.ui import S, Screen  # noqa: E402


class TestUtil(unittest.TestCase):
    def test_fmt_bytes(self):
        self.assertEqual(util.fmt_bytes(0), "0 B")
        self.assertEqual(util.fmt_bytes(1023), "1023 B")
        self.assertEqual(util.fmt_bytes(1024), "1.0 KiB")
        self.assertEqual(util.fmt_bytes(1536), "1.5 KiB")
        self.assertEqual(util.fmt_bytes(4.6 * 1024 ** 4), "4.6 TiB")

    def test_duration(self):
        self.assertEqual(util.fmt_duration(0), "0:00")
        self.assertEqual(util.fmt_duration(3725), "1:02:05")
        self.assertEqual(util.fmt_eta(None), "-")

    def test_width_cjk(self):
        self.assertEqual(util.disp_width("abc"), 3)
        self.assertEqual(util.disp_width("白蛇：缘起"), 10)
        self.assertEqual(util.disp_width("RTX 2080 Ti"), 11)

    def test_truncate(self):
        self.assertLessEqual(util.disp_width(util.truncate_disp("白蛇：缘起.mp4", 8)), 8)
        self.assertLessEqual(util.disp_width(util.truncate_left("白蛇：缘起.mp4", 8)), 8)
        self.assertIn("…", util.truncate_disp("白蛇：缘起.mp4", 8))
        self.assertEqual(util.truncate_disp("abc", 10), "abc")
        self.assertEqual(util.pad_disp("白蛇", 6), "白蛇  ")

    def test_parse_speed(self):
        self.assertEqual(util.parse_speed("1.5x"), 1.5)
        self.assertIsNone(util.parse_speed(None))
        self.assertIsNone(util.parse_speed("N/A"))

    def test_tail(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            for i in range(100):
                fh.write("line %d\n" % i)
            path = fh.name
        lines = util.tail_lines(path, 3)
        self.assertEqual(lines, ["line 97", "line 98", "line 99"])
        os.unlink(path)


class TestConfig(unittest.TestCase):
    def test_merge_and_save(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "config.json")
        with open(path, "w") as fh:
            json.dump({"encode": {"cq": 30}, "scan": {"path": "/x"}}, fh)
        cfg = cfgmod.Config(home=tmp, path=path)
        self.assertEqual(cfg.get("encode.cq"), 30)
        self.assertEqual(cfg.get("encode.preset"), "p7")      # default kept
        self.assertEqual(cfg.get("scan.path"), "/x")
        cfg.set("gpu.prefer", "least-loaded")
        cfg.save()
        cfg2 = cfgmod.Config(home=tmp, path=path)
        self.assertEqual(cfg2.get("gpu.prefer"), "least-loaded")
        self.assertEqual(cfg2.get("encode.cq"), 30)

    def test_missing_keys_do_not_crash(self):
        cfg = cfgmod.Config(home=tempfile.mkdtemp())
        self.assertIsNone(cfg.get("nope.nothing"))
        self.assertEqual(cfg.get("daemon.poll_seconds"), 0.5)


class TestDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = DB(os.path.join(self.tmp, "s.db")).init()

    def tearDown(self):
        self.db.close()

    def test_jobs(self):
        jid = self.db.add_job({"src": "/v/a.mp4", "src_size": 100, "duration": 60})
        self.assertEqual(self.db.counts(), {"queued": 1})
        self.db.update_job(jid, status="running", gpu=0, progress=0.5)
        self.assertEqual(self.db.per_gpu_active(), {0: 1})
        self.db.update_job(jid, status="done", bytes_in=100, bytes_out=40, speed=2.0)
        tot = self.db.totals()
        self.assertEqual(tot["saved"], 60)
        self.assertEqual(self.db.counts(), {"done": 1})

    def test_processed(self):
        self.assertFalse(self.db.is_processed("/v/a.mkv"))
        self.db.mark_processed("/v/a.mkv", "/v/a.mkv", "done")
        self.assertTrue(self.db.is_processed("/v/a.mkv"))
        self.db.clear_processed("/v/a.mkv")
        self.assertFalse(self.db.is_processed("/v/a.mkv"))

    def test_commands(self):
        self.db.push_command("pause", {})
        cmds = self.db.pending_commands()
        self.assertEqual(cmds[0]["cmd"], "pause")
        self.db.complete_command(cmds[0]["id"])
        self.assertEqual(self.db.pending_commands(), [])

    def test_media_cache(self):
        self.assertIsNone(self.db.media_cached("/v/a.mp4", 10, 100.0))
        self.db.media_put("/v/a.mp4", 10, 100.0, {"vcodec": "h264"})
        info = self.db.media_cached("/v/a.mp4", 10, 100.0)
        self.assertEqual(info["vcodec"], "h264")
        self.assertIsNone(self.db.media_cached("/v/a.mp4", 11, 100.0))


class TestPlanner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = cfgmod.Config(home=self.tmp,
                                 overrides={"scan": {"path": self.tmp, "min_size_mb": 0}})
        self.db = DB(self.cfg.db_path()).init()

    def tearDown(self):
        self.db.close()

    def _touch(self, name, size=5 * 1024 * 1024):
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"0" * size)
        return path

    def test_safe_target_path(self):
        src = self._touch("a.mp4")
        self.assertEqual(scan.safe_target_path(src, "mkv"), os.path.join(self.tmp, "a.mkv"))
        self._touch("a.mkv")
        self.assertEqual(scan.safe_target_path(src, "mkv"), os.path.join(self.tmp, "a_1.mkv"))
        src_mkv = self._touch("b.mkv")
        self.assertEqual(scan.safe_target_path(src_mkv, "mkv"), src_mkv)

    def test_series_grouping(self):
        paths = [self._touch("S-1.mp4"), self._touch("S-2.mp4"), self._touch("S-3.mp4"),
                 self._touch("other.mp4"), self._touch("S-4.mp4")]
        groups = scan.group_series(paths, self.cfg)
        bases = sorted(g[1] for g in groups)
        self.assertEqual(bases, ["S"])
        self.assertEqual(len(groups[0][2]), 4)

    def test_series_needs_two(self):
        paths = [self._touch("one-1.mp4")]
        self.assertEqual(scan.group_series(paths, self.cfg), [])

    def test_plan_skips_hevc_and_tagged(self):
        import tctui.scan as scan_mod

        good = self._touch("keep.mp4")
        hevc = self._touch("already_hevc.mkv")
        tagged = self._touch("tagged.mp4")
        media = {
            good: {"has_video": True, "vcodec": "h264", "size": 10, "duration": 10,
                   "width": 1920, "height": 1080, "bit_depth": 8, "hdr": False,
                   "comment": "", "audio_codecs": ["aac"], "sub_codecs": []},
            hevc: {"has_video": True, "vcodec": "hevc", "size": 10, "duration": 10,
                   "width": 1920, "height": 1080, "bit_depth": 8, "hdr": False,
                   "comment": "", "audio_codecs": [], "sub_codecs": []},
            tagged: {"has_video": True, "vcodec": "h264", "size": 10, "duration": 10,
                     "width": 1920, "height": 1080, "bit_depth": 8, "hdr": False,
                     "comment": "Transcoded By HServer", "audio_codecs": [], "sub_codecs": []},
        }
        orig = scan_mod.probe_media
        scan_mod.probe_media = lambda cfg, path, db=None: media.get(path)
        try:
            planner = scan.Planner(self.cfg, self.db)
            jobs = planner.plan([self.tmp])
            self.assertEqual([j["src"] for j in jobs], [good])
            self.assertEqual(len(planner.skips), 2)
            self.assertTrue(any("HEVC" in s["reason"] for s in planner.skips))
        finally:
            scan_mod.probe_media = orig


class TestScreen(unittest.TestCase):
    def test_put_and_width(self):
        scr = Screen(20, 3)
        scr.put(0, 0, "白蛇 缘起", S["norm"])
        self.assertEqual(scr.cells[0][0][0], "白")
        self.assertEqual(scr.cells[0][1][0], "")       # continuation cell
        self.assertEqual(scr.cells[0][5][0], "缘")
        scr.put(1, 0, "abcdefghij", S["norm"], maxw=4)
        self.assertEqual("".join(c[0] for c in scr.cells[1][:5]), "abcd ")

    def test_bar(self):
        scr = Screen(10, 1)
        scr.bar(0, 0, 10, 0.5, S["progress"], S["bar_bg"], "█", "░")
        row = "".join(c[0] for c in scr.cells[0])
        self.assertEqual(row, "█████░░░░░")

    def test_row_runs(self):
        scr = Screen(6, 1)
        scr.put(0, 0, "ab", S["ok"])
        scr.put(0, 2, "cd", S["err"])
        runs = scr.row(0)
        self.assertEqual(runs[0], (S["ok"], "ab"))
        self.assertEqual(runs[1], (S["err"], "cd"))
        self.assertEqual(runs[2], (S["norm"], "  "))

    def test_clip(self):
        scr = Screen(5, 1)
        scr.put(0, 4, "宽字符", S["norm"])
        self.assertEqual(scr.cells[0][4][0], " ")


class TestSettingsPanel(unittest.TestCase):
    """Mouse hit-testing for the settings panel (no terminal needed)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = cfgmod.Config(home=self.tmp, overrides={"encode": {"cq": 27}})
        from tctui import ui
        self.ui = ui
        self.app = ui.App(self.cfg)
        self.app.view.gpus = [
            {"index": i, "uuid": "u%d" % i, "name": "NVIDIA GeForce RTX 2080 Ti",
             "mem_total_mb": 22528, "mem_used_mb": 2000, "mem_free_mb": 20000,
             "util": 5, "temp": 50, "power": 100, "power_limit": 250, "enc_sessions": 0}
            for i in range(3)
        ]
        self.app.draw(ui.Screen(120, 44))

    def _hits(self):
        self.app.draw(self.ui.Screen(120, 44))
        return self.app.hits["settings"]

    def test_gpu_row_opens_panel(self):
        y = sorted(self.app.hits["gpu"])[0]
        self.app.on_mouse(10, y, 1)
        self.assertTrue(self.app.show_settings)

    def _hits_for(self, key):
        self.app.draw(self.ui.Screen(120, 44))
        rows = self.app._settings_rows()
        idx = [i for i, r in enumerate(rows) if r.get("key") == key][0]
        return idx, [h for h in self.app.hits["settings"] if h[1] == idx]

    def test_stepper_and_right_click(self):
        self.app._open_settings()
        idx, hits = self._hits_for("encode.cq")
        inc = [h for h in hits if h[4] == "inc"][0]
        dec = [h for h in hits if h[4] == "dec"][0]
        self.app.on_mouse(inc[2], inc[0], 1)
        self.assertEqual(self.cfg.get("encode.cq"), 28)
        self.app.on_mouse(dec[2], dec[0], 1)
        self.assertEqual(self.cfg.get("encode.cq"), 27)
        self.app.on_mouse(inc[2], inc[0], 3)          # right click = decrement
        self.assertEqual(self.cfg.get("encode.cq"), 26)

    def test_bool_toggle(self):
        self.app._open_settings()
        hits = self._hits()
        rows = self.app._settings_rows()
        idx = [i for i, r in enumerate(rows) if r.get("key") == "_skip_hevc"][0]
        tg = [h for h in hits if h[1] == idx and h[4] == "toggle"][0]
        self.app.on_mouse(tg[2], tg[0], 1)
        self.assertEqual(self.cfg.get("scan.skip_if_codec"), [])

    def test_click_outside_closes(self):
        self.app._open_settings()
        self.app.draw(self.ui.Screen(120, 44))
        self.app.on_mouse(2, 43, 1)
        self.assertFalse(self.app.show_settings)

    def test_footer_and_title_buttons_exist(self):
        actions = [h[3] for h in self.app.hits["footer"]]
        self.assertIn("settings", actions)
        self.assertTrue(self.app.hits["title"])
        self.app.on_mouse(self.app.hits["title"][0][0] + 1, 0, 1)
        self.assertTrue(self.app.show_settings)

    def test_video_codec_choices(self):
        self.app._open_settings()
        rows = self.app._settings_rows()
        codec = [r for r in rows if r.get("key") == "encode.video_codec"][0]
        self.assertEqual(codec["values"], ["hevc_nvenc", "h264_nvenc"])   # no AV1 on Turing
        self.app._change_setting(codec, 1)
        self.assertEqual(self.cfg.get("encode.video_codec"), "h264_nvenc")
        self.assertIn("H.264", self.app._setting_text(
            [r for r in self.app._settings_rows() if r.get("key") == "encode.video_codec"][0]))

    def test_scan_format_panel(self):
        self.app._open_settings()
        self.app.draw(self.ui.Screen(120, 44))
        rows = self.app._settings_rows()
        idx = [i for i, r in enumerate(rows) if r.get("key") == "_scan_exts"][0]
        hit = [h for h in self.app.hits["settings"] if h[1] == idx][0]
        self.app.on_mouse(hit[2] + 1, hit[0], 1)
        self.assertTrue(self.app.show_exts)
        self.app.draw(self.ui.Screen(120, 44))
        self.assertEqual(len(self.app.hits["exts"]), len(self.app._ext_rows()))
        i = [k for k, (e, _on) in enumerate(self.app._ext_rows()) if e == "avi"][0]
        self.app.on_mouse(self.app.hits["exts"][i][2] + 1, self.app.hits["exts"][i][0], 1)
        self.assertNotIn("avi", self.cfg.get("scan.exts"))
        common = [b for b in self.app.hits["exts_btns"] if b[3] == "common"][0]
        self.app.on_mouse(common[1] + 1, common[0], 1)
        self.assertIn("avi", self.cfg.get("scan.exts"))

    def test_keyboard_drives_panel(self):
        self.app.action("m")
        self.assertTrue(self.app.show_settings)
        rows = self.app._settings_rows()
        self.app.settings_sel = [i for i, r in enumerate(rows) if r.get("key") == "encode.cq"][0]
        self.app.action("\x1b[C")
        self.assertEqual(self.cfg.get("encode.cq"), 28)
        self.app.action("q")
        self.assertFalse(self.app.show_settings)


class TestClearQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = cfgmod.Config(home=self.tmp, overrides={})
        from tctui import ui
        self.ui = ui
        self.db = DB(self.cfg.db_path()).init()
        self.ids = [self.db.add_job({"src": "/v/%d.mp4" % i, "src_size": 10}) for i in range(2)]
        self.app = ui.App(self.cfg)
        self.app.view.counts = {"queued": 2}
        self.app.view.hb = {}
        self.app.view.daemon_alive = False

    def tearDown(self):
        self.db.close()

    def test_press_twice_clears_queue(self):
        self.app.action("C")
        self.assertTrue(self.app.confirm)
        self.app.action("C")
        self.assertIsNone(self.app.confirm)
        counts = self.db.counts()
        self.assertEqual(counts.get("queued", 0), 0)
        self.assertEqual(counts.get("canceled", 0), 2)

    def test_escape_cancels(self):
        self.app.action("C")
        self.app.action("\x1b")
        self.assertIsNone(self.app.confirm)
        self.assertEqual(self.db.counts().get("queued", 0), 2)

    def test_empty_queue_message(self):
        self.app.view.counts = {"queued": 0}
        self.app.action("C")
        self.assertIsNone(self.app.confirm)
        self.assertIn("空", self.app.toast[0])

    def test_db_clear_queue_marks_skipped(self):
        from tctui import util
        before = self.db.processed_count()
        self.db.clear_queue(mark_skipped=True)
        self.assertGreater(self.db.processed_count(), before)
        self.assertTrue(self.db.is_processed("/v/0.mp4", outcomes=("skipped",)))

    def test_clear_finished(self):
        self.db.update_job(self.ids[0], status="done", finished=1)
        self.assertEqual(self.db.clear_finished(), 1)
        self.assertEqual(self.db.counts().get("done", 0), 0)


class TestManualLimitMigration(unittest.TestCase):
    def test_zero_limit_becomes_four(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "config.json")
        with open(path, "w") as fh:
            fh.write('{"gpu": {"max_parallel_per_gpu": 0}}')
        cfg = cfgmod.Config(home=tmp)
        self.assertEqual(cfg.get("gpu.max_parallel_per_gpu"), 4)


class TestScanPathMigration(unittest.TestCase):
    def test_old_watch_list_becomes_last_path(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "config.json")
        with open(path, "w") as fh:
            json.dump({"watch": ["/old/a", "/old/b"]}, fh)
        cfg = cfgmod.Config(home=tmp)
        self.assertEqual(cfg.get("scan.path"), "/old/b")
        self.assertIsNone(cfg.get("watch"))


class TestTruncateMiddle(unittest.TestCase):
    def test_middle_keeps_both_ends(self):
        s = "/volume2/video/动画/白蛇：缘起/白蛇：缘起.mp4"
        for w in (20, 30, 40):
            out = util.truncate_middle(s, w)
            self.assertLessEqual(util.disp_width(out), w)
            self.assertIn("…", out)
            self.assertTrue(s.startswith(out.split("…")[0]))
            self.assertTrue(s.endswith(out.split("…")[1]))

    def test_short_string_untouched(self):
        self.assertEqual(util.truncate_middle("abc.mp4", 20), "abc.mp4")

    def test_cjk_width(self):
        out = util.truncate_middle("/volume2/视频/文件名.mp4", 16)
        self.assertLessEqual(util.disp_width(out), 16)
        self.assertTrue(out.startswith("/vol"))
        self.assertTrue(out.endswith(".mp4"))


class TestBinaryPathSetting(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        self.tmp = tempfile.mkdtemp()
        self.cfg = cfgmod.Config(home=self.tmp, overrides={})
        from tctui import ui
        self.ui = ui
        self.app = ui.App(self.cfg)
        self.app.view.gpus = []
        self.app.view.counts = {}
        self.app.view.hb = {}
        # /tmp on the NAS is noexec, so put the fake binaries on the repo mount
        self.bin = tempfile.mkdtemp(dir=os.getcwd())
        self.addCleanup(shutil.rmtree, self.bin, True)
        for name in ("ffmpeg", "ffprobe"):
            path = os.path.join(self.bin, name)
            with open(path, "w") as fh:
                fh.write("#!/bin/sh\necho \"%s version test\"\nexit 0\n" % name)
            os.chmod(path, 0o755)
        # /tmp on the NAS is noexec; validate the logic without executing
        self.run_patch = mock.patch(
            "tctui.ui.subprocess.run",
            return_value=mock.Mock(returncode=0, stdout=b"ffmpeg version test\n"))
        self.run_patch.start()
        self.addCleanup(self.run_patch.stop)

    def _ffmpeg_row(self):
        rows = self.app._settings_rows()
        return [r for r in rows if r.get("key") == "paths.ffmpeg"][0]

    def test_set_ffmpeg_path_and_sibling_ffprobe(self):
        self.cfg.set("paths.ffmpeg", "/nonexistent/ffmpeg")
        self.cfg.set("paths.ffprobe", "/nonexistent/ffprobe")
        row = self._ffmpeg_row()
        self.app._change_setting(row, 1)
        self.assertTrue(self.app.input_mode)
        self.assertEqual(self.app.input_kind, "ffmpeg")
        self.app.input_buf = os.path.join(self.bin, "ffmpeg")
        self.app.action("\n")
        self.assertEqual(self.cfg.get("paths.ffmpeg"), os.path.join(self.bin, "ffmpeg"))
        self.assertEqual(self.cfg.get("paths.ffprobe"), os.path.join(self.bin, "ffprobe"))
        self.assertIn("已设置", self.app.toast[0])

    def test_invalid_path_rejected(self):
        before = self.cfg.get("paths.ffmpeg")
        row = self._ffmpeg_row()
        self.app._change_setting(row, 1)
        self.app.input_buf = "/definitely/not/ffmpeg"
        self.app.action("\n")
        self.assertEqual(self.cfg.get("paths.ffmpeg"), before)
        self.assertIn("找不到", self.app.toast[0])

    def test_ctrl_u_clears_input(self):
        self.app.action("a")
        for ch in "abc":
            self.app.action(ch)
        self.app.action("\x15")
        self.assertEqual(self.app.input_buf, "")


class TestUiConfigHotReload(unittest.TestCase):
    def test_ui_sees_external_config_change(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "config.json")
        with open(path, "w") as fh:
            json.dump({"policy": {"keep_original_min_savings_pct": 10}}, fh)
        cfg = cfgmod.Config(home=tmp)
        from tctui import ui
        app = ui.App(cfg)
        self.assertEqual(cfg.get("policy.keep_original_min_savings_pct"), 10)
        time.sleep(1.05)
        with open(path, "w") as fh:
            json.dump({"policy": {"keep_original_min_savings_pct": 0}}, fh)
        app.reload()
        self.assertEqual(cfg.get("policy.keep_original_min_savings_pct"), 0)


class TestTaskSequence(unittest.TestCase):
    def test_seq_is_stable_and_monotonic(self):
        tmp = tempfile.mkdtemp()
        db = DB(os.path.join(tmp, "s.db")).init()
        try:
            a = db.add_job({"src": "/a.mp4"})
            b = db.add_job({"src": "/b.mp4"})
            self.assertEqual(db.get_job(a)["seq"], 1)
            self.assertEqual(db.get_job(b)["seq"], 2)
            db.update_job(a, status="done", finished=1)
            self.assertEqual(db.get_job(a)["seq"], 1)   # follows the task
            c = db.add_job({"src": "/c.mp4"})
            self.assertEqual(db.get_job(c)["seq"], 3)
        finally:
            db.close()


class TestSummaryJump(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        cfg = cfgmod.Config(home=tmp, overrides={})
        from tctui import ui
        self.ui = ui
        self.app = ui.App(cfg, demo=True)
        self.app.reload()
        self.app.draw(ui.Screen(120, 44))

    def _click(self, label):
        hit = [h for h in self.app.hits["summary"] if h[3] == label][0]
        self.app.on_mouse(hit[0] + 1, self.app.hits["summary_y"], 1)
        return self.app.selected()

    def test_queue_chip_jumps_to_first_queued(self):
        job = self._click("队列")
        self.assertEqual(job["status"], "queued")

    def test_kept_chip_jumps_to_first_kept(self):
        job = self._click("保留")
        self.assertEqual(job["status"], "kept")

    def test_failed_chip_jumps_to_first_failed(self):
        job = self._click("失败")
        self.assertIn(job["status"], ("failed", "interrupted"))


class TestSelectionFollow(unittest.TestCase):
    def _app(self):
        tmp = tempfile.mkdtemp()
        cfg = cfgmod.Config(home=tmp, overrides={})
        from tctui import ui
        app = ui.App(cfg)
        app.view.gpus = []
        return app

    def test_stays_on_row_when_selected_task_finishes(self):
        app = self._app()
        app.view.jobs = [{"id": 1, "status": "running"}, {"id": 2, "status": "queued"}]
        app.sel_id, app.sel_idx = 1, 0
        app.view.jobs = [{"id": 2, "status": "running"}, {"id": 1, "status": "done"}]
        app._update_selection({1: "running"})
        self.assertEqual(app.sel_id, 2)      # next row, not the finished job
        self.assertEqual(app.sel_idx, 0)

    def test_follows_running_task_when_list_reorders(self):
        app = self._app()
        app.view.jobs = [{"id": 1, "status": "running"}, {"id": 2, "status": "queued"}]
        app.sel_id, app.sel_idx = 1, 0
        app.view.jobs = [{"id": 3, "status": "running"}, {"id": 1, "status": "running"}]
        app._update_selection({1: "running"})
        self.assertEqual(app.sel_id, 1)
        self.assertEqual(app.sel_idx, 1)


class TestUiScan(unittest.TestCase):
    def test_scan_runs_in_ui_process_without_daemon(self):
        tmp = tempfile.mkdtemp()
        cfg = cfgmod.Config(home=tmp, overrides={"scan": {"path": tmp}})
        from tctui import ui
        app = ui.App(cfg)
        app.view.daemon_alive = False
        app.view.hb = {}

        class FakePlanner:
            def __init__(self, cfg, db):
                self.skips = []

            def plan(self, roots, progress=None):
                if progress:
                    progress("fake")
                return [{"src": os.path.join(roots[0], "x.mp4"), "src_size": 1}]

        orig = ui.scanmod.Planner
        ui.scanmod.Planner = FakePlanner
        try:
            app.start_scan()
            for _ in range(100):
                if app.scan_state.get("done"):
                    break
                time.sleep(0.01)
            self.assertTrue(app.scan_state.get("done"))
            self.assertEqual(app.scan_state.get("added"), 1)
            app.reload()
            self.assertIn("扫描完成", app.toast[0])
            db = DB(cfg.db_path()).init()
            self.assertEqual(len(db.jobs()), 1)
            db.close()
        finally:
            ui.scanmod.Planner = orig


class TestStatusBadges(unittest.TestCase):
    def test_title_and_footer_show_states(self):
        tmp = tempfile.mkdtemp()
        cfg = cfgmod.Config(home=tmp, overrides={"scan": {"path": "/volume4/NSFW/Other"}})
        from tctui import ui
        app = ui.App(cfg, demo=True)
        app.reload()
        app.view.daemon_alive = True
        app.view.hb = {"paused": True, "busy": "scanning"}
        scr = ui.Screen(120, 24)
        app.draw(scr)
        text = ui.DumpBackend(120, 24, color=False).draw(scr)
        self.assertIn("/volume4/NSFW/Other", text)      # scan path in title
        self.assertIn("扫描中", text)
        self.assertIn("已暂停", text)
        self.assertIn("暂停中", text)                    # Space badge
        self.assertIn("守护已启动", text)                 # D badge
        app.view.daemon_alive = False
        app.view.hb = {}
        scr = ui.Screen(120, 24)
        app.draw(scr)
        text = ui.DumpBackend(120, 24, color=False).draw(scr)
        self.assertIn("守护未运行", text)
        self.assertIn("守护已关闭", text)


class TestGpuPanel(unittest.TestCase):
    def test_no_other_vram_column_and_no_dots(self):
        tmp = tempfile.mkdtemp()
        cfg = cfgmod.Config(home=tmp, overrides={})
        from tctui import ui
        app = ui.App(cfg, demo=True)
        app.reload()
        scr = ui.Screen(120, 20)
        app.draw(scr)
        text = ui.DumpBackend(120, 20, color=False).draw(scr)
        gpu_panel = "\n".join(text.split("\n")[2:6])
        self.assertNotIn("其他", gpu_panel)
        self.assertNotIn("●", gpu_panel)
        self.assertNotIn("○", gpu_panel)
        self.assertIn("编码并发", text)
        self.assertIn("3/4", text)
        self.assertNotIn("m8+", text)


class TestFooterHelp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = cfgmod.Config(home=self.tmp, overrides={})
        from tctui import ui
        self.ui = ui
        self.app = ui.App(self.cfg)
        self.app.view.gpus = []
        self.app.view.hb = {}
        self.app.view.counts = {}

    def _actions(self, w=120):
        rows = self.app._footer_layout(w)
        return [a for row in rows for (_x, _k, _l, a, _f) in row], rows

    def test_footer_lists_every_shortcut(self):
        actions, rows = self._actions(120)
        for act in ("q", "?", " ", "a", "s", "C", "T", "D", "settings", "x", "r",
                    "d", "l"):
            self.assertIn(act, actions)
        self.assertLessEqual(len(rows), 2, "footer should fit in two rows at 120 cols")
        actions80, rows80 = self._actions(80)
        self.assertLessEqual(len(rows80), 3)
        self.assertIn("C", actions80)

    def test_help_opens_and_scrolls(self):
        self.app.action("?")
        self.assertTrue(self.app.show_help)
        self.app.draw(self.ui.Screen(120, 44))
        self.assertIn("help_box", self.app.hits)
        before = self.app.help_scroll
        self.app.action("KEY_NPAGE")
        self.assertGreaterEqual(self.app.help_scroll, before)
        self.app.action("?")
        self.assertFalse(self.app.show_help)

    def test_help_blocks_other_keys(self):
        self.app.action("?")
        self.app.action("C")
        self.assertIsNone(self.app.confirm)
        self.assertFalse(self.app.show_settings)

    def test_question_mark_in_settings_opens_help(self):
        self.app._open_settings()
        self.app.action("?")
        self.assertTrue(self.app.show_help)

    def test_help_does_not_cover_footer(self):
        self.app.show_help = True
        self.app.draw(self.ui.Screen(100, 24))
        box = self.app.hits["help_box"]
        footer_rows = self.app.hits["footer_rows"]
        self.assertLessEqual(box[3], min(footer_rows))
        self.assertGreaterEqual(len(footer_rows), 2)

    def test_footer_click_runs_action(self):
        self.app.draw(self.ui.Screen(120, 44))
        hit = [h for h in self.app.hits["footer"] if h[3] == "?"][0]
        self.app.on_mouse(hit[1] + 1, hit[0], 1)
        self.assertTrue(self.app.show_help)


class TestWorkerArgs(unittest.TestCase):
    def _worker(self, dec=("h264", "hevc"), bit=8, hdr=False, width=1920, height=1080):
        from tctui.worker import Worker
        tmp = tempfile.mkdtemp()
        spec = {
            "job_id": 1, "src": "/x/in.mp4", "out": os.path.join(tmp, "out.mkv"),
            "gpu": 0, "duration": 10,
            "progress": os.path.join(tmp, "p.json"), "result": os.path.join(tmp, "r.json"),
            "log": os.path.join(tmp, "l.log"),
            "ffmpeg": "/usr/bin/ffmpeg", "ffprobe": "/usr/bin/ffprobe",
            "dec_codecs": list(dec),
            "source_info": {"vcodec": "h264", "pix_fmt": "yuv420p" if bit == 8 else "yuv420p10le",
                            "width": width, "height": height, "bit_depth": bit, "hdr": hdr,
                            "color_transfer": "smpte2084" if hdr else "",
                            "color_primaries": "bt2020" if hdr else ""},
            "encode": {"video_codec": "hevc_nvenc", "preset": "p7", "cq": 27, "out_ext": "mkv",
                       "audio": "copy", "subs": "copy", "max_width": 0, "hdr": "auto"},
        }
        return Worker(spec)

    def test_hw_path_uses_scale_cuda_filtergraph(self):
        w = self._worker()
        args, hw = w.build_transcode_args("/x/in.mp4", "gpu", 1)
        self.assertTrue(hw)
        self.assertEqual(args[args.index("-threads") + 1], "4")
        self.assertIn("-noautoscale", args)
        fc = args[args.index("-filter_complex") + 1]
        self.assertIn("[0:V:0]scale_cuda=iw:-2", fc)
        self.assertEqual(args[args.index("-map") + 1], "[out]")
        self.assertNotIn("-vf", args)

    def test_hw_path_10bit_uses_p010(self):
        w = self._worker(bit=10, hdr=True)
        args, hw = w.build_transcode_args("/x/in.mp4", "gpu", 1)
        fc = args[args.index("-filter_complex") + 1]
        self.assertIn("format=p010le", fc)

    def test_hwdec_setup_failure_is_recognized(self):
        from tctui.worker import Worker
        self.assertTrue(Worker._hwdec_setup_failed(
            "Failed setup for format cuda: hwaccel initialisation returned error."))
        self.assertTrue(Worker._hwdec_setup_failed(
            "cuvidCreateDecoder(&decoder->decoder, params) failed"))
        self.assertFalse(Worker._hwdec_setup_failed("OpenEncodeSessionEx failed"))

    def test_sw_fallback_has_no_cuda_filters(self):
        w = self._worker()
        args, hw = w.build_transcode_args("/x/in.mp4", "gpu", 2, sw_decode=True)
        self.assertFalse(hw)
        self.assertNotIn("-noautoscale", args)
        self.assertNotIn("-filter_complex", args)
        self.assertNotIn("-hwaccel", args)
        self.assertIn("-map", args)

    def test_scale_max_width(self):
        w = self._worker(width=3840, height=2160)
        w.spec["encode"]["max_width"] = 1920
        args, hw = w.build_transcode_args("/x/in.mp4", "gpu", 1)
        fc = args[args.index("-filter_complex") + 1]
        self.assertIn("scale_cuda=1920:1080", fc)

    def test_cpu_mode_uses_vf(self):
        w = self._worker(dec=())
        w.spec["encode"]["fallback_to_cpu"] = True
        args, hw = w.build_transcode_args("/x/in.mp4", "cpu", 1)
        self.assertFalse(hw)
        self.assertIn("-c:v", args)
        self.assertNotIn("-filter_complex", args)


class TestCommandDedupe(unittest.TestCase):
    def test_duplicate_scan_is_superseded(self):
        tmp = tempfile.mkdtemp()
        db = DB(os.path.join(tmp, "s.db")).init()
        try:
            a = db.push_command("scan", {"roots": ["/a"]})
            b = db.push_command("scan", {"roots": ["/a"]})
            self.assertNotEqual(a, b)
            self.assertEqual(len(db.pending_commands()), 1)
            self.assertEqual(db.pending_commands()[0]["id"], b)
            db.push_command("scan", {"roots": ["/b"]})
            self.assertEqual(len(db.pending_commands()), 2)
        finally:
            db.close()


class TestClearStats(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = cfgmod.Config(home=self.tmp, overrides={})
        from tctui import ui
        self.ui = ui
        self.db = DB(self.cfg.db_path()).init()
        self.ids = []
        for st in ("done", "done", "kept", "failed"):
            jid = self.db.add_job({"src": "/v/%s_%d.mp4" % (st, len(self.ids)), "src_size": 100})
            self.db.update_job(jid, status=st, finished=1, bytes_in=100, bytes_out=40)
            self.ids.append(jid)
        self.app = ui.App(self.cfg)
        self.app.view.counts = {"done": 2, "kept": 1, "failed": 1, "queued": 0}
        self.app.view.hb = {}
        self.app.view.daemon_alive = False

    def tearDown(self):
        self.db.close()

    def test_T_twice_resets_stats(self):
        self.app.action("T")
        self.assertTrue(self.app.confirm)
        self.assertEqual(self.app.confirm["action"], "clear_finished")
        self.app.action("T")
        self.assertIsNone(self.app.confirm)
        self.assertEqual(self.db.counts(), {})

    def test_T_empty(self):
        self.app.view.counts = {"done": 0}
        self.app.action("T")
        self.assertIsNone(self.app.confirm)
        self.assertIn("没有", self.app.toast[0])

    def test_settings_row_clear_stats(self):
        self.app._open_settings()
        rows = self.app._settings_rows()
        row = [r for r in rows if r.get("action") == "clear_finished"][0]
        self.assertIn("清零累计统计", row["label"])
        self.app._change_setting(row, 1)       # first click -> confirm
        self.assertTrue(self.app.confirm)
        row2 = [r for r in self.app._settings_rows() if r.get("action") == "clear_finished"][0]
        self.assertIn("再点一次", row2["label"])
        self.app._change_setting(row2, 1)      # second click -> execute
        self.assertEqual(self.db.counts(), {})


class TestUiInput(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = cfgmod.Config(home=self.tmp, overrides={})
        from tctui import ui
        self.ui = ui
        self.app = ui.App(self.cfg)
        self.sent = []
        self.app.command = lambda cmd, args=None: self.sent.append((cmd, args))
        self.app.view.gpus = []
        self.app.view.hb = {}

    def _type(self, text):
        for ch in text:
            self.app.action(ch)

    def test_path_scan_sets_persistent_path(self):
        d = os.path.join(self.tmp, "电影", "合集")
        os.makedirs(d)
        self.app.start_scan = lambda: self.sent.append(("scan", None))
        self.app.action("a")
        self.assertTrue(self.app.input_mode)
        self._type(d)
        self.app.action("\n")
        self.assertFalse(self.app.input_mode)
        self.assertEqual(self.cfg.get("scan.path"), d)
        self.assertEqual(self.sent[-1][0], "scan")

    def test_path_scan_replaces_previous(self):
        first = os.path.join(self.tmp, "first")
        second = os.path.join(self.tmp, "second")
        os.makedirs(first); os.makedirs(second)
        self.cfg.set("scan.path", first)
        self.app.action("a")
        self._type(second)
        self.app.action("\n")
        self.assertEqual(self.cfg.get("scan.path"), second)

    def test_path_scan_missing_dir(self):
        self.app.action("a")
        self._type("/definitely/not/here")
        self.app.action("\n")
        self.assertEqual(self.sent, [])
        self.assertIn("不存在", self.app.toast[0])

    def test_empty_path_scans_configured(self):
        self.cfg.set("scan.path", self.tmp)
        self.app.start_scan = lambda: self.sent.append(("scan", None))
        self.app.action("a")
        self.app.action("\n")
        self.assertEqual(self.sent[-1][0], "scan")
        self.assertEqual(self.cfg.get("scan.path"), self.tmp)

    def test_scan_path_setting(self):
        d = os.path.join(self.tmp, "library")
        os.makedirs(d)
        self.app._open_settings()
        rows = self.app._settings_rows()
        row = [r for r in rows if r.get("key") == "scan.path"][0]
        self.app._change_setting(row, 1)
        self.assertEqual(self.app.input_kind, "scanpath")
        self.app.input_buf = d
        self.app.action("\n")
        self.assertEqual(self.cfg.get("scan.path"), d)

    def test_scan_without_path_warns(self):
        self.app.action("s")
        self.assertEqual(self.sent, [])
        self.assertIn("还没有设置", self.app.toast[0])

    def test_D_toggles_daemon(self):
        started = []
        self.app._start_daemon = lambda: started.append(1)
        self.app.view.daemon_alive = False
        self.app.action("D")
        self.assertEqual(started, [1])
        self.app.view.daemon_alive = True
        self.app.action("D")            # first press -> confirm
        self.assertTrue(self.app.confirm)
        self.assertEqual(self.app.confirm["action"], "stop_daemon")
        self.app.action("D")            # second press -> send shutdown
        self.assertTrue(any(cmd == "shutdown" for cmd, _ in self.sent))

    def test_start_daemon_command(self):
        calls = []

        class FakeProc:
            pid = 1234

        def fake_popen(argv, **kw):
            calls.append((argv, kw))
            return FakeProc()

        real = self.ui.subprocess.Popen
        self.ui.subprocess.Popen = fake_popen
        try:
            self.app.action("D")
        finally:
            self.ui.subprocess.Popen = real
        self.assertEqual(len(calls), 1)
        argv = calls[0][0]
        self.assertIn("daemon", argv)
        self.assertIn("--start", argv)
        self.assertEqual(calls[0][1].get("cwd"), self.cfg.home)


class TestMouseMasks(unittest.TestCase):
    def test_missing_button5_is_tolerated(self):
        import curses
        from tctui import ui as uimod
        saved = getattr(curses, "BUTTON5_PRESSED", None)
        had = hasattr(curses, "BUTTON5_PRESSED")
        if had:
            del curses.BUTTON5_PRESSED
        try:
            masks = uimod.mouse_masks()
            self.assertTrue(masks["down"])
            self.assertEqual(masks["down"], masks["up"] << 5 if masks["up"] else masks["down"])
            self.assertTrue(masks["left"])
            self.assertTrue(masks["right"])
        finally:
            if had:
                curses.BUTTON5_PRESSED = saved

    def test_all_masks_non_zero(self):
        from tctui import ui as uimod
        masks = uimod.mouse_masks()
        for key in ("left", "right", "up", "down"):
            self.assertTrue(masks[key], key)


class TestGpuMath(unittest.TestCase):
    def test_estimate_and_limit(self):
        from tctui import gpu
        tmp = tempfile.mkdtemp()
        cfg = cfgmod.Config(home=tmp)
        self.assertEqual(gpu.estimate_vram_mb(cfg, 1920, 1080), 640)
        self.assertEqual(gpu.estimate_vram_mb(cfg, 3840, 2160), 1800)
        self.assertEqual(gpu.estimate_vram_mb(cfg, 640, 360), 320)
        self.assertEqual(gpu.per_gpu_limit(cfg), 4)
        cfg.set("gpu.max_parallel_per_gpu", 2)
        self.assertEqual(gpu.per_gpu_limit(cfg), 2)
        cfg.set("gpu.max_parallel_per_gpu", 0)
        self.assertEqual(gpu.per_gpu_limit(cfg), 4)   # 0 falls back to 4

    def test_choose_gpu_uses_manual_limit(self):
        from tctui import gpu
        cfg = cfgmod.Config(home=tempfile.mkdtemp())
        cfg.set("gpu.max_parallel_per_gpu", 2)
        live = [{"index": 0, "uuid": "u0", "name": "A", "mem_free_mb": 10000, "mem_total_mb": 20000},
                {"index": 1, "uuid": "u1", "name": "B", "mem_free_mb": 10000, "mem_total_mb": 20000}]
        g, limit = gpu.choose_gpu(cfg, live, {0: 2}, {"est_vram_mb": 500})
        self.assertEqual(g["index"], 1)
        self.assertEqual(limit, 2)
        g, limit = gpu.choose_gpu(cfg, live, {0: 2, 1: 2}, {"est_vram_mb": 500})
        self.assertIsNone(g)


if __name__ == "__main__":
    unittest.main()
