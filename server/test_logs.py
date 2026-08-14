#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日志的两件事（会话 21）：**分级**（D112）和**自动清理**（D113）。

分级 —— `[online]` 是运营流水（两种启动方式都写），`[online-debug]` 是遥测
（只有 `start-debug.bat` / `--verbose` 才写）。
清理 —— `logs/` 里超过 N 天没动过的日志文件会在「服务端启动时」和
「每天凌晨 4 点」各清一次。
"""
import io
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout

import config as server_config
import eventlog
import logcleanup


def touch(path, days_ago=0.0, size=0):
    """造一个文件，并把它的 mtime 拨到 `days_ago` 天前。"""
    with open(path, "wb") as f:
        f.write(b"x" * size)
    when = time.time() - days_ago * 86400
    os.utime(path, (when, when))
    return path


class FindStaleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def name(self, *paths):
        return sorted(os.path.basename(p) for p in paths)

    def test_only_files_older_than_the_window_are_picked(self):
        touch(os.path.join(self.dir, "old.log"), days_ago=4)
        touch(os.path.join(self.dir, "fresh.log"), days_ago=1)
        touch(os.path.join(self.dir, "exactly.log"), days_ago=2.9)
        self.assertEqual(["old.log"],
                         self.name(*logcleanup.find_stale(self.dir, 3)))

    def test_every_kind_of_log_we_actually_produce_is_covered(self):
        made = [touch(os.path.join(self.dir, name), days_ago=5) for name in (
            "server.out", "server.err", "relay.out", "relay.err",
            "bsloader.out", "bsloader.err",
            "bshook_20260813_142534_pid24332.log",
            "online.log", "online-20260810.log",
            "game_001_27799.txt", "game_001_27799.raw.bin",
            "game_001_27799.dec.bin",
            "auth_001_47611.txt", "auth_001_47611.bin",
            "conn_001_27799.txt", "conn_001_27799.bin",
        )]
        self.assertEqual(self.name(*made),
                         self.name(*logcleanup.find_stale(self.dir, 3)))

    def test_state_files_and_stray_artifacts_are_never_touched(self):
        # ★ `.server_mode` / `.relay_target` 是启动脚本的状态文件，
        #   删了会让下一次启动白重启一遍中继；逆向留下的截图也不是日志。
        for name in (".server_mode", ".relay_target", "shot_lobby.png",
                     "accounts.json", "notes.md"):
            touch(os.path.join(self.dir, name), days_ago=99)
        self.assertEqual([], logcleanup.find_stale(self.dir, 3))

    def test_directories_are_never_picked(self):
        os.mkdir(os.path.join(self.dir, "old.log"))
        self.assertEqual([], logcleanup.find_stale(self.dir, 3))

    def test_zero_days_means_do_nothing(self):
        touch(os.path.join(self.dir, "ancient.log"), days_ago=999)
        self.assertEqual([], logcleanup.find_stale(self.dir, 0))
        self.assertEqual((0, 0), logcleanup.cleanup(self.dir, 0))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "ancient.log")))

    def test_a_missing_log_directory_is_not_an_error(self):
        self.assertEqual([], logcleanup.find_stale(
            os.path.join(self.dir, "nope"), 3))
        self.assertEqual((0, 0), logcleanup.cleanup(
            os.path.join(self.dir, "nope"), 3))

    def test_the_retention_window_is_configurable(self):
        touch(os.path.join(self.dir, "d5.log"), days_ago=5)
        touch(os.path.join(self.dir, "d20.log"), days_ago=20)
        self.assertEqual(["d20.log"],
                         self.name(*logcleanup.find_stale(self.dir, 10)))
        self.assertEqual(["d20.log", "d5.log"],
                         self.name(*logcleanup.find_stale(self.dir, 1)))


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.lines = []

    def test_it_deletes_and_reports_how_much_it_freed(self):
        touch(os.path.join(self.dir, "old.log"), days_ago=9, size=2048)
        touch(os.path.join(self.dir, "new.log"), days_ago=0, size=4096)
        removed, freed = logcleanup.cleanup(self.dir, 3, log=self.lines.append)
        self.assertEqual((1, 2048), (removed, freed))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "old.log")))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "new.log")))
        self.assertIn("删掉 1 个", self.lines[0])

    def test_a_file_that_cannot_be_deleted_is_skipped_not_raised(self):
        # Windows 上「别的进程正开着它」就是删不掉。清垃圾绝不能把服务端弄挂。
        path = touch(os.path.join(self.dir, "busy.log"), days_ago=9)
        real_remove = os.remove

        def refuse(target):
            if target == path:
                raise PermissionError(13, "被占用")
            return real_remove(target)

        os.remove = refuse
        self.addCleanup(setattr, os, "remove", real_remove)
        removed, _ = logcleanup.cleanup(self.dir, 3, log=self.lines.append)
        self.assertEqual(0, removed)
        self.assertTrue(os.path.exists(path))

    def test_a_file_being_written_right_now_is_never_stale(self):
        # 正在写的日志 mtime 就是刚才 —— 这就是「按 mtime 判」的全部理由。
        path = os.path.join(self.dir, "server.out")
        touch(path, days_ago=99)
        with open(path, "a", encoding="utf-8") as f:
            f.write("还在写\n")
            f.flush()
        self.assertEqual([], logcleanup.find_stale(self.dir, 3))

    def test_the_background_thread_cleans_once_immediately(self):
        touch(os.path.join(self.dir, "old.log"), days_ago=9)
        thread = logcleanup.start(self.dir, 3, log=self.lines.append)
        self.assertIsNotNone(thread)
        for _ in range(200):                    # 后台线程，等它跑完那一次
            if not os.path.exists(os.path.join(self.dir, "old.log")):
                break
            time.sleep(0.01)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "old.log")))

    def test_zero_days_does_not_even_start_a_thread(self):
        self.assertIsNone(logcleanup.start(self.dir, 0, log=self.lines.append))
        self.assertIn("已关闭", self.lines[0])


class DailyScheduleTests(unittest.TestCase):
    """第二个触发点：每天凌晨 4 点。"""

    def test_it_always_lands_on_the_next_four_am(self):
        for hour in (0, 3, 4, 5, 12, 23):
            now = time.mktime((2026, 8, 14, hour, 30, 0, 0, 0, -1))
            wait = logcleanup.seconds_until_daily(now)
            landed = time.localtime(now + wait)
            self.assertEqual(logcleanup.DAILY_HOUR, landed.tm_hour,
                             f"{hour} 点出发落到了 {landed.tm_hour} 点")
            self.assertEqual(0, landed.tm_min)
            self.assertLessEqual(wait, 86400 + 3600)
            self.assertGreater(wait, 0)

    def test_it_survives_the_end_of_a_month(self):
        # 31 号 5 点出发，下一次是 9 月 1 日 4 点 —— mktime 自己会进位。
        now = time.mktime((2026, 8, 31, 5, 0, 0, 0, 0, -1))
        landed = time.localtime(now + logcleanup.seconds_until_daily(now))
        self.assertEqual((2026, 9, 1, 4),
                         (landed.tm_year, landed.tm_mon, landed.tm_mday,
                          landed.tm_hour))


class ConfigTests(unittest.TestCase):
    def test_the_retention_days_key_is_parsed(self):
        cfg, warnings = server_config.parse_text("log_retention_days = 7")
        self.assertEqual(7, cfg["log_retention_days"])
        self.assertEqual([], warnings)

    def test_zero_is_a_legal_value(self):
        cfg, warnings = server_config.parse_text("log_retention_days = 0")
        self.assertEqual(0, cfg["log_retention_days"])
        self.assertEqual([], warnings)

    def test_a_missing_key_falls_back_to_three_days(self):
        cfg, _ = server_config.parse_text("")
        self.assertEqual(3, cfg["log_retention_days"])
        self.assertEqual(server_config.DEFAULT_LOG_RETENTION_DAYS,
                         cfg["log_retention_days"])

    def test_junk_values_warn_and_fall_back(self):
        for bad in ("abc", "-1", "999999"):
            cfg, warnings = server_config.parse_text(f"log_retention_days = {bad}")
            self.assertEqual(server_config.DEFAULT_LOG_RETENTION_DAYS,
                             cfg["log_retention_days"], bad)
            self.assertTrue(warnings, bad)

    def test_the_register_cooldown_default_is_twenty(self):
        # 用户 2026-08-14 拍板从 60 改成 20（D111）。模板和常量必须一起改，
        # 否则 `test_the_shipped_template_parses_to_the_defaults` 会红。
        self.assertEqual(20, server_config.DEFAULT_REGISTER_COOLDOWN_SECONDS)
        cfg, _ = server_config.parse_text(server_config.DEFAULT_CONFIG_TEXT)
        self.assertEqual(20, cfg["register_cooldown_seconds"])


class EventLogLevelTests(unittest.TestCase):
    """`online()` 两种模式都写；`debug()` 只有 `--verbose` 才写。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "online.log")
        # 用例跑完把 eventlog 恢复成「测试默认」：不落盘、不 verbose。
        self.addCleanup(eventlog.configure, eventlog.DEFAULT_PATH, False, True,
                        False)

    def read(self):
        if not os.path.exists(self.path):
            return ""
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    def test_debug_lines_are_dropped_in_the_plain_mode(self):
        eventlog.configure(self.path, to_file=True, to_stdout=False,
                           verbose=False)
        eventlog.online("玩家上线")
        eventlog.debug("转发耗时 0.1 ms")
        text = self.read()
        self.assertIn("玩家上线", text)
        self.assertNotIn("转发耗时", text)

    def test_debug_lines_appear_in_the_debug_mode(self):
        eventlog.configure(self.path, to_file=True, to_stdout=False,
                           verbose=True)
        eventlog.online("玩家上线")
        eventlog.debug("转发耗时 0.1 ms")
        text = self.read()
        self.assertIn("[online] 玩家上线", text)
        self.assertIn("[online-debug] 转发耗时", text)

    def test_the_two_levels_have_different_prefixes(self):
        # `grep '[online]'` 仍然只该捞到运营那一档。
        eventlog.configure(self.path, to_file=True, to_stdout=False,
                           verbose=True)
        eventlog.debug("遥测")
        self.assertNotIn("[online] ", self.read())

    def test_stdout_follows_the_same_rule(self):
        eventlog.configure(self.path, to_file=False, to_stdout=True,
                           verbose=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            eventlog.online("上线")
            eventlog.debug("遥测")
        self.assertIn("上线", buf.getvalue())
        self.assertNotIn("遥测", buf.getvalue())


class OnlineLogRotationTests(unittest.TestCase):
    """`online.log` 按天切分 —— 不切的话它永远「刚写过」，清理永远够不着。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "online.log")
        self.addCleanup(eventlog.configure, eventlog.DEFAULT_PATH, False, True,
                        False)

    def test_yesterdays_file_is_renamed_on_the_next_write(self):
        eventlog.configure(self.path, to_file=True, to_stdout=False)
        eventlog.online("昨天的事")
        eventlog.configure(self.path, to_file=True, to_stdout=False)  # 关掉句柄
        yesterday = time.time() - 3 * 86400
        os.utime(self.path, (yesterday, yesterday))
        day = time.localtime(yesterday)
        eventlog.online("今天的事")
        rolled = os.path.join(
            self.tmp.name,
            f"online-{day.tm_year:04d}{day.tm_mon:02d}{day.tm_mday:02d}.log")
        self.assertTrue(os.path.exists(rolled), os.listdir(self.tmp.name))
        with open(rolled, encoding="utf-8") as f:
            self.assertIn("昨天的事", f.read())
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("今天的事", text)
        self.assertNotIn("昨天的事", text)

    def test_the_rolled_file_is_something_the_cleaner_will_pick_up(self):
        rolled = "online-20260810.log"
        self.assertTrue(logcleanup.is_log_name(rolled))
        touch(os.path.join(self.tmp.name, rolled), days_ago=9)
        self.assertEqual([rolled],
                         [os.path.basename(p) for p in
                          logcleanup.find_stale(self.tmp.name, 3)])

    def test_same_day_restarts_keep_appending(self):
        eventlog.configure(self.path, to_file=True, to_stdout=False)
        eventlog.online("第一次启动")
        eventlog.configure(self.path, to_file=True, to_stdout=False)
        eventlog.online("第二次启动")
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("第一次启动", text)
        self.assertIn("第二次启动", text)
        # 同一天里重启不该切出任何 online-YYYYMMDD.log。
        self.assertEqual([], [n for n in os.listdir(self.tmp.name)
                              if n.startswith("online-")])


if __name__ == "__main__":
    unittest.main()
