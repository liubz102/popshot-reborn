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
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

import asynclog
import config as server_config
import daylog
import eventlog
import logcleanup
import testsupport


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
            # ★ 启动脚本归档下来的上一次那份（`Move-LogAside` / `rotate_log`，
            #   用户 2026-09-01）。它们也必须到期被清掉，否则「不覆盖」就变成
            #   「只增不减」，比覆盖还糟。
            "server-20260901-013012.out", "server-20260901-013012.err",
            "relay-20260901-013012.out", "relay-20260901-013012.err",
            "bsloader-20260901-013012.out", "bsloader-20260901-013012.err",
            # ★ `daylog` 跨零点切出来的那一份（用户 2026-09-14）。**这才是
            #   真正会被删掉的东西** —— 今天那份 server.out 一直在写，
            #   mtime 永远是刚才，清理永远够不着它。
            "server-20260901.out", "server-20260901.err",
            "relay-20260901.out", "relay-20260901.err",
            # 启动脚本的重定向兜底（daylog 装好之前那一小段）。
            "server-boot.out", "server-boot.err",
            "relay-boot.out", "relay-boot.err",
            "bshook_20260813_142534_pid24332.log",
            "online.log", "online-20260810.log",
            # ★ 逐连接抓包现在名字里带 `logcleanup.RUN_STAMP`（同一天多次
            #   启动不再互相覆盖）。新旧两种写法都得认。
            "game_001_27799.txt", "game_001_27799.raw.bin",
            "game_001_27799.dec.bin",
            "game_20260901-013012_001_27799.txt",
            "game_20260901-013012_001_27799.raw.bin",
            "game_20260901-013012_001_27799.dec.bin",
            "auth_001_47611.txt", "auth_001_47611.bin",
            "auth_20260901-013012_001_47611.txt",
            "auth_20260901-013012_001_47611.bin",
            "conn_001_27799.txt", "conn_001_27799.bin",
        )]
        self.assertEqual(self.name(*made),
                         self.name(*logcleanup.find_stale(self.dir, 3)))

    def test_the_run_stamp_is_a_sortable_timestamp(self):
        """`RUN_STAMP` 进了文件名，格式错了会直接体现在磁盘上。"""
        self.assertRegex(logcleanup.RUN_STAMP, r"^\d{8}-\d{6}$")
        # 它必须在**模块导入**时就定死 —— 每条连接各算一次的话，
        # 同一次启动的抓包文件就分不到一组里去了。
        self.assertIs(logcleanup.RUN_STAMP, logcleanup.RUN_STAMP)

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


class LogTimestampTests(unittest.TestCase):
    """日志行的时间戳**必须带日期 + 时区**（用户 2026-09-14 / 2026-09-20）。

    * 日期：以前只有 `HH:MM:SS.mmm`，玩家贴回来几行、或者事后翻归档都判断
      不出是哪一天的；
    * 时区：崩溃包来自玩家机器、打包戳来自开发机、日志来自服务器，三台机器
      三个时区，不写出来就会比反（bug调查/25，见 `server/tzstamp.py` 文件头）。

    四份 `ts()` 必须**长得一模一样**，否则 `server.out` 和 `online.log` /
    逐连接抓包的行按时间对不上。
    """

    #: `2026-09-20 22:07:29.715 UTC+9`（半小时时区写成 `UTC+5:30`）
    TS_RE = r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} UTC[+-]\d+(:\d\d)?$"

    def test_all_four_timestamps_carry_a_full_date_and_a_zone(self):
        # 在函数里 import：test_logs 本来很轻，不值得为这一条把 gameserver
        # 拖进模块导入。
        import authserver
        import gameserver
        import relay
        for name, fn in (("gameserver", gameserver.ts), ("authserver", authserver.ts),
                         ("eventlog", eventlog.ts), ("relay", relay.ts)):
            with self.subTest(name):
                self.assertRegex(fn(), self.TS_RE)

    def test_the_four_agree_on_the_zone_suffix(self):
        # 四份日志会被并排读，后缀不一致等于没写。
        import authserver
        import gameserver
        import relay
        zones = {fn().rsplit(" ", 1)[1] for fn in
                 (gameserver.ts, authserver.ts, eventlog.ts, relay.ts)}
        self.assertEqual(1, len(zones), "四份 ts() 的时区后缀不一致：%s" % zones)


class DayLogNameTests(unittest.TestCase):
    """切出来叫什么名字。**这个名字同时是清理的入口** —— 起错了就永远清不掉。"""

    def test_the_date_goes_in_front_of_the_extension(self):
        self.assertEqual(os.path.join("logs", "server-20260913.out"),
                         daylog.dated_name(os.path.join("logs", "server.out"),
                                           (2026, 9, 13)))

    def test_the_rolled_name_is_one_the_cleaner_recognises(self):
        for stem in ("server.out", "server.err", "relay.out"):
            rolled = daylog.dated_name(stem, (2026, 9, 13))
            with self.subTest(stem):
                self.assertTrue(logcleanup.is_log_name(rolled))

    def test_it_does_not_collide_with_the_launchers_restart_archive(self):
        # 启动脚本归档 bsloader 那份用的是「`-<mtime 精确到秒>`」
        # （`Move-LogAside` / wincompat.ps1）。两种后缀长得不一样，
        # 同一天里两套并存也不会撞名。
        self.assertNotEqual("server-20260913-013012.out",
                            daylog.dated_name("server.out", (2026, 9, 13)))


class DayLogRotateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.dir, "server.out")

    def test_the_rolled_file_keeps_its_mtime_so_it_can_age_out(self):
        """切名**不能**让文件「变新」—— 那样清理还得再等满一个保留期。"""
        touch(self.path, days_ago=5)
        self.assertTrue(daylog.rotate_to_dated(self.path, (2026, 9, 1)))
        rolled = os.path.join(self.dir, "server-20260901.out")
        self.assertTrue(os.path.exists(rolled))
        self.assertFalse(os.path.exists(self.path))
        # 这一条才是整件事的目的：切完立刻就轮得到清理。
        self.assertEqual([rolled], logcleanup.find_stale(self.dir, 3))

    def test_an_existing_target_is_left_alone(self):
        touch(self.path)
        keep = touch(os.path.join(self.dir, "server-20260901.out"), size=7)
        self.assertFalse(daylog.rotate_to_dated(self.path, (2026, 9, 1)))
        self.assertTrue(os.path.exists(self.path))     # 原样接着写
        self.assertEqual(7, os.path.getsize(keep))     # 已有的那份没被顶掉

    def test_nothing_to_roll_is_not_an_error(self):
        self.assertFalse(daylog.rotate_to_dated(self.path, (2026, 9, 1)))
        self.assertFalse(daylog.rotate_to_dated(self.path, None))


class DaySinkTests(unittest.TestCase):
    """顶替 `sys.stdout` 的那个流。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.dir, "server.out")

    def read(self, name="server.out"):
        with open(os.path.join(self.dir, name), encoding="utf-8") as f:
            return f.read()

    def sink(self, **kw):
        """造一个流，并保证测完把句柄放掉 —— 不放 Windows 上临时目录删不掉。"""
        made = daylog.DaySink(self.path, **kw)
        self.addCleanup(made.close)
        return made

    def test_lines_land_in_the_file(self):
        sink = self.sink()
        sink.write("一行日志\n")
        sink.flush()
        self.assertEqual("一行日志\n", self.read())

    def test_crossing_midnight_rolls_yesterday_aside(self):
        """判据是「当前这个句柄是哪天开的」，不是任何定时器（铁律 10）。"""
        sink = self.sink()
        sink.write("昨天的\n")
        sink.flush()
        sink._day = (2026, 9, 1)          # 假装这个句柄是 9/1 开的
        sink.write("今天的\n")
        sink.flush()
        self.assertEqual("昨天的\n", self.read("server-20260901.out"))
        self.assertEqual("今天的\n", self.read())

    def test_a_leftover_file_from_another_day_is_rolled_on_first_write(self):
        """重启后接着写的那份可能是好几天前的 —— 不切就永远新鲜、永远清不掉。"""
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("上次那一段\n")
        old = time.time() - 3 * 86400
        os.utime(self.path, (old, old))
        stamp = time.strftime("%Y%m%d", time.localtime(old))

        sink = self.sink()
        sink.write("这次这一段\n")
        sink.flush()
        self.assertEqual("上次那一段\n", self.read("server-%s.out" % stamp))
        self.assertEqual("这次这一段\n", self.read())

    def test_a_same_day_restart_appends_instead_of_truncating(self):
        # 用户 2026-09-01：「日志不要被覆盖、只清理过期的」。
        first = self.sink()
        first.write("第一次启动\n")
        first.flush()
        second = self.sink()
        second.write("第二次启动\n")
        second.flush()
        self.assertEqual("第一次启动\n第二次启动\n", self.read())
        self.assertEqual(["server.out"], sorted(os.listdir(self.dir)))

    def test_a_file_it_cannot_open_falls_back_instead_of_losing_the_line(self):
        # 拿一个**目录**占住这个名字，`open()` 必然失败 —— 比造「磁盘满」
        # 稳，而且各平台都一样。
        os.mkdir(self.path)
        buf = io.StringIO()
        sink = self.sink(fallback=buf)
        sink.write("写不进文件也不许把这行弄丢\n")
        self.assertIn("不许把这行弄丢", buf.getvalue())

    def test_stderr_flushes_every_line(self):
        """traceback 攒在缓冲区里等于没记 —— err 那一路必须写一行刷一次。"""
        sink = self.sink(autoflush=True)
        sink.write("Traceback (most recent call last):\n")
        self.assertIn("Traceback", self.read())        # 没 flush() 就读到了


class LiveMidnightRollTests(unittest.TestCase):
    """★★ 用户 2026-09-14 问的就是这一条：**服务端一直不关，过零点会切吗？**

    上面 `DaySinkTests` 那条是手工把 `_day` 拨回去的，只验了切名那一段。
    这一条跑的是**线上唯一会发生的那条路**：`asynclog` 的写线程 + 被换掉的
    `sys.stdout`，中间**一次都不重开进程、不重开流**，只有钟往前走。

    云主机一开几个月，永远等不到「下次启动」—— 要是只有启动时才切，
    那就等于永远不切，和改之前一模一样。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.dir, "server.out")

    def read(self, name="server.out"):
        with open(os.path.join(self.dir, name), encoding="utf-8") as f:
            return f.read()

    def test_a_process_that_never_restarts_still_rolls_at_midnight(self):
        before = time.mktime((2026, 9, 13, 23, 59, 50, 0, 0, -1))
        after = time.mktime((2026, 9, 14, 0, 0, 10, 0, 0, -1))
        now = [before]

        def fake_localtime(when=None):
            # 只有「现在几点」是假的；问文件 mtime 还是照真的答。
            return time.localtime(now[0] if when is None else when)

        sink = daylog.DaySink(self.path, _localtime=fake_localtime)
        self.addCleanup(sink.close)
        real_out = sys.stdout
        asynclog.start()
        self.addCleanup(asynclog.stop)
        sys.stdout = sink
        try:
            asynclog.emit("[2026-09-13 23:59:50.000] 打烊前最后一句")
            self.assertTrue(asynclog.drain(timeout=testsupport.OUTBOX_FUSE_S))
            now[0] = after                 # ← 过零点。**进程什么都没做。**
            asynclog.emit("[2026-09-14 00:00:10.000] 新一天第一句")
            self.assertTrue(asynclog.drain(timeout=testsupport.OUTBOX_FUSE_S))
        finally:
            # 断言之前先换回来：断言失败时 unittest 要往 stdout/stderr 写。
            sys.stdout = real_out

        self.assertIn("打烊前最后一句", self.read("server-20260913.out"))
        self.assertIn("新一天第一句", self.read())
        # 零点之后那一行**不许**落进昨天那份 —— 切早了切晚了都算错。
        self.assertNotIn("新一天第一句", self.read("server-20260913.out"))
        self.assertNotIn("打烊前最后一句", self.read())

    def test_a_day_with_nothing_to_say_does_not_leave_an_empty_file(self):
        """空转两天再写一行也切得对，而且**不会**留下两个空文件。

        判据是「当前这个句柄是哪天开的」，不是「过了几个零点」——
        所以中间没人说话的那些天根本不存在，也就不该有文件。
        """
        now = [time.mktime((2026, 9, 13, 10, 0, 0, 0, 0, -1))]
        sink = daylog.DaySink(self.path,
                              _localtime=lambda when=None:
                              time.localtime(now[0] if when is None else when))
        self.addCleanup(sink.close)
        sink.write("9/13 说了一句\n")
        sink.flush()
        now[0] = time.mktime((2026, 9, 16, 10, 0, 0, 0, 0, -1))   # 空转 3 天
        sink.write("9/16 才又说一句\n")
        sink.flush()
        self.assertEqual(["server-20260913.out", "server.out"],
                         sorted(os.listdir(self.dir)))
        self.assertEqual("9/13 说了一句\n", self.read("server-20260913.out"))
        self.assertEqual("9/16 才又说一句\n", self.read())


class DayLogInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def test_install_takes_over_stdout_and_stderr(self):
        real_out, real_err = sys.stdout, sys.stderr
        self.addCleanup(setattr, daylog, "_installed", None)
        daylog._installed = None
        out_path, err_path = daylog.install(stem="server", logdir=self.dir,
                                            banner="服务端启动")
        # ★ `install()` 往 atexit 里挂了这两个流，它们不会被回收 ⇒ 句柄不放，
        #   Windows 上临时目录就删不掉。测完显式关掉。
        self.addCleanup(sys.stderr.close)
        self.addCleanup(sys.stdout.close)
        try:
            print("走 print 的那一行")
            sys.stdout.flush()
            sys.stderr.write("走 stderr 的那一行\n")
        finally:
            # ★ 先把流换回来再断言：断言失败时 unittest 要往 stderr 写，
            #   写进临时目录就等于把失败信息扔了。
            sys.stdout, sys.stderr = real_out, real_err
        with open(out_path, encoding="utf-8") as f:
            out = f.read()
        with open(err_path, encoding="utf-8") as f:
            err = f.read()
        self.assertIn("走 print 的那一行", out)
        self.assertIn("走 stderr 的那一行", err)
        # 分隔线：文件现在跨重启追加，没有它就分不清哪一段是哪次运行写的。
        self.assertIn("服务端启动", out)
        self.assertRegex(out, r"pid=\d+")

    def test_installing_twice_does_not_re_wrap_the_streams(self):
        real_out, real_err = sys.stdout, sys.stderr
        self.addCleanup(setattr, sys, "stdout", real_out)
        self.addCleanup(setattr, sys, "stderr", real_err)
        self.addCleanup(setattr, daylog, "_installed", None)
        daylog._installed = None
        first = daylog.install(stem="server", logdir=self.dir)
        second = daylog.install(stem="relay", logdir=self.dir)
        self.addCleanup(sys.stderr.close)
        self.addCleanup(sys.stdout.close)
        sys.stdout, sys.stderr = real_out, real_err
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
