#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`server/tzstamp.py` —— 日志时间戳的时区后缀。

为什么值得单开一个模块：这东西**只有出事的时候才有人看**（事后排查崩溃，
把三台机器的日志排到同一根时间轴上）。那时候再发现它写错了就晚了，所以
半小时时区、负偏移、夏令时切换这几档全部在这里钉死 —— 它们在开发机上
（UTC+9，整点、不用夏令时）一辈子跑不到。

出处：bug调查/25。当时拿开发机（UTC+9）的 `buildId` 去比玩家机器（UTC+8）
的崩溃时刻，把「崩溃在发版之后」比成了「在发版之前」。
"""
import os
import re
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))

import tzstamp  # noqa: E402

#: 四份 `ts()` 和这里必须认同一个形状。
ZONE_RE = re.compile(r"^UTC[+-]\d+(:\d\d)?$")


class _FakeZone:
    """临时把 `time.timezone` / `altzone` / `daylight` 换成指定的一组值。

    ★ 不去改 `TZ` 环境变量 + `time.tzset()`：Windows 上 `tzset()` 压根没有，
      而服务端要在 Windows 和 Linux 上都跑。
    """

    def __init__(self, timezone, altzone=None, daylight=0):
        self.vals = (timezone, timezone if altzone is None else altzone, daylight)

    def __enter__(self):
        self.saved = (time.timezone, time.altzone, time.daylight)
        time.timezone, time.altzone, time.daylight = self.vals
        return self

    def __exit__(self, *exc):
        time.timezone, time.altzone, time.daylight = self.saved
        return False


class OffsetTextTests(unittest.TestCase):

    def test_the_local_machine_gets_a_well_formed_suffix(self):
        self.assertRegex(tzstamp.utc_offset_text(), ZONE_RE)

    def test_whole_hour_zones_have_no_colon(self):
        # `time.timezone` 是「本地 + 它 = UTC」的秒数，所以东八区是 -8*3600。
        with _FakeZone(-8 * 3600):
            self.assertEqual("UTC+8", tzstamp.utc_offset_text(0))
        with _FakeZone(-9 * 3600):
            self.assertEqual("UTC+9", tzstamp.utc_offset_text(0))
        with _FakeZone(0):
            self.assertEqual("UTC+0", tzstamp.utc_offset_text(0))

    def test_western_zones_come_out_negative(self):
        with _FakeZone(5 * 3600):
            self.assertEqual("UTC-5", tzstamp.utc_offset_text(0))
        with _FakeZone(3 * 3600 + 30 * 60):
            self.assertEqual("UTC-3:30", tzstamp.utc_offset_text(0))

    def test_half_and_three_quarter_hour_zones_survive(self):
        # 印度 +5:30、尼泊尔 +5:45 —— 只写小时会把它们抹成 +5。
        with _FakeZone(-(5 * 3600 + 30 * 60)):
            self.assertEqual("UTC+5:30", tzstamp.utc_offset_text(0))
        with _FakeZone(-(5 * 3600 + 45 * 60)):
            self.assertEqual("UTC+5:45", tzstamp.utc_offset_text(0))

    def test_daylight_saving_uses_the_offset_of_that_moment(self):
        # 夏令时期间要用 altzone。缓存进程启动时的值就会在切换那天全错。
        with _FakeZone(5 * 3600, altzone=4 * 3600, daylight=1):
            summer = time.struct_time((2026, 7, 1, 12, 0, 0, 2, 182, 1))
            winter = time.struct_time((2026, 1, 1, 12, 0, 0, 3, 1, 0))
            saved = time.localtime
            try:
                time.localtime = lambda *_a: summer
                self.assertEqual("UTC-4", tzstamp.utc_offset_text(0))
                time.localtime = lambda *_a: winter
                self.assertEqual("UTC-5", tzstamp.utc_offset_text(0))
            finally:
                time.localtime = saved

    def test_it_is_computed_not_hardcoded(self):
        # 写死成 UTC+8 / UTC+9 就是这次要防的错：玩家可能在任何时区。
        src = open(os.path.join(ROOT, "server", "tzstamp.py"),
                   encoding="utf-8").read()
        body = src[src.index("def utc_offset_text"):]
        self.assertNotIn('"UTC+8"', body)
        self.assertNotIn('"UTC+9"', body)
        self.assertIn("time.altzone", body)


class StampTests(unittest.TestCase):

    def test_the_default_shape_is_date_time_zone(self):
        self.assertRegex(tzstamp.stamp(),
                         r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC[+-]\d+(:\d\d)?$")

    def test_millis_go_before_the_zone(self):
        # 后缀必须在**最后**：各家 `ts()` 都靠 rsplit(" ", 1) 把它切下来。
        text = tzstamp.stamp(millis=True)
        self.assertRegex(text,
                         r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} UTC[+-]\d+(:\d\d)?$")
        self.assertRegex(text.rsplit(" ", 1)[1], ZONE_RE)

    def test_a_custom_format_still_gets_the_suffix(self):
        text = tzstamp.stamp(0, fmt="%Y-%m-%d %H:%M")
        self.assertRegex(text, r"^\d{4}-\d\d-\d\d \d\d:\d\d UTC[+-]\d+(:\d\d)?$")

    def test_the_epoch_it_is_given_is_the_epoch_it_formats(self):
        # 传进来的时刻要用那一刻的偏移算，不是「现在」的 —— 夏令时前后差一小时。
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(0)),
                         tzstamp.stamp(0).rsplit(" ", 1)[0])


class EveryLogWriterUsesItTests(unittest.TestCase):
    """凡是写「给人看的时间戳」的地方都得走这里 —— 少一处就又能比反一次。"""

    #: (文件, 必须出现的调用)。★ 新加一处日志出口就往这张表里加一行。
    WIRED = [
        ("server/gameserver.py", "tzstamp.stamp(millis=True)"),
        ("server/authserver.py", "tzstamp.stamp(millis=True)"),
        ("server/relay.py", "tzstamp.stamp(millis=True)"),
        ("server/eventlog.py", "tzstamp.stamp(millis=True)"),
        ("server/daylog.py", "tzstamp.stamp()"),
        ("server/crashstore.py", "tzstamp.stamp(epoch)"),
        ("server/databackup.py", "tzstamp.stamp(epoch, pattern)"),
        ("server/crashwatch.py", "tzstamp.stamp(session_start)"),
        ("server/crashwatch.py", "tzstamp.utc_offset_text()"),
        ("server/logpack.py", "tzstamp.utc_offset_text(plan.now)"),
        ("server/capture_server.py", 'tzstamp.stamp(fmt="%H:%M:%S", millis=True)'),
    ]

    def test_every_known_log_writer_goes_through_tzstamp(self):
        for rel, needle in self.WIRED:
            with self.subTest(rel + " :: " + needle):
                src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
                self.assertIn(needle, src, "%s 没有走 tzstamp" % rel)

    def test_nobody_prints_a_bare_local_timestamp_any_more(self):
        # 只盯「行前缀」那一档：裸的 `%Y-%m-%d %H:%M:%S` 拼进日志行就是这次的病根。
        # 文件名用的 `%Y%m%d-%H%M%S` 不在此列（加时区会把文件名弄脏）。
        offenders = []
        for rel, _needle in self.WIRED:
            src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
            for m in re.finditer(r'strftime\("([^"]*%H[^"]*)"', src):
                fmt = m.group(1)
                if "%H:%M" in fmt:                 # 带冒号 = 给人看的，不是文件名
                    offenders.append("%s: %s" % (rel, fmt))
        self.assertEqual([], offenders,
                         "这些地方还在直接 strftime 人类可读时刻，应该走 tzstamp")


class ClientSideWritersTests(unittest.TestCase):
    """C 侧（注入 DLL / 更新器）也得写时区 —— 那两份日志正是**玩家机器**上的。

    ⚠ 依赖仓库布局（`hook/`、`updater/`），发布包里没有，自动跳过。
    """

    @classmethod
    def setUpClass(cls):
        cls.hook = os.path.join(ROOT, "hook", "bshook.c")
        cls.util = os.path.join(ROOT, "updater", "src", "util.c")
        for p in (cls.hook, cls.util):
            if not os.path.isfile(p):
                raise unittest.SkipTest("不在源码仓库里（缺 %s），跳过" % p)

    def test_bshook_puts_the_zone_in_every_line(self):
        src = open(self.hook, encoding="utf-8").read()
        self.assertIn('"[%02u:%02u:%02u.%03u %s] "', src,
                      "bslog 的行前缀没带时区")
        self.assertIn("bslog_zone(&st, zone, sizeof(zone));", src)

    def test_the_updater_stamp_carries_the_zone(self):
        src = open(self.util, encoding="utf-8").read()
        self.assertIn("utc_offset_text(zone, 16);", src)
        self.assertIn('L"%04u-%02u-%02u %02u:%02u:%02u %s"', src)

    def test_both_derive_the_offset_instead_of_reading_the_registry(self):
        # 本地时刻减 UTC 时刻 ⇒ 夏令时自动就对，也不用缓存。
        for path in (self.hook, self.util):
            src = open(path, encoding="utf-8").read()
            self.assertIn("GetSystemTime(&ut);", src, path)
            self.assertNotIn("GetTimeZoneInformation", src, path)


if __name__ == "__main__":
    unittest.main()
