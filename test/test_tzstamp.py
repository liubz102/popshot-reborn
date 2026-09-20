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

    ★★ 换完三个全局之后**必须自己把 `ZONE_TEXT` 重算一遍**：后缀是进程启动时
      算死的（用户 2026-09-20 第二轮），运行时改 `time.timezone` 对它没有影响。
      这正是被测行为本身 —— 所以重算这一下只能由夹具**显式**做，
      生产代码里没有、也不该有任何一个「重算时区」的入口。
    """

    def __init__(self, timezone, altzone=None, daylight=0):
        self.vals = (timezone, timezone if altzone is None else altzone, daylight)

    def __enter__(self):
        self.saved = (time.timezone, time.altzone, time.daylight, tzstamp.ZONE_TEXT)
        time.timezone, time.altzone, time.daylight = self.vals
        tzstamp.ZONE_TEXT = tzstamp._zone_text()
        return self

    def __exit__(self, *exc):
        (time.timezone, time.altzone, time.daylight,
         tzstamp.ZONE_TEXT) = self.saved
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

    def test_the_startup_moment_picks_standard_or_daylight(self):
        # 启动那一刻仍然看一眼 `tm_isdst` 选 `altzone` 还是 `timezone` ——
        # 不花运行时代价，万一真有人在用夏令时的地方开服，启动那刻是对的。
        summer = time.struct_time((2026, 7, 1, 12, 0, 0, 2, 182, 1))
        winter = time.struct_time((2026, 1, 1, 12, 0, 0, 3, 1, 0))
        saved = time.localtime
        try:
            time.localtime = lambda *_a: summer
            with _FakeZone(5 * 3600, altzone=4 * 3600, daylight=1):
                self.assertEqual("UTC-4", tzstamp.utc_offset_text(0))
            time.localtime = lambda *_a: winter
            with _FakeZone(5 * 3600, altzone=4 * 3600, daylight=1):
                self.assertEqual("UTC-5", tzstamp.utc_offset_text(0))
        finally:
            time.localtime = saved

    def test_the_zone_is_frozen_after_the_process_started(self):
        """★ 用户 2026-09-20 第二轮拍板的那一条：**算一次，之后一直沿用**。

        起来之后夏令时切换了、系统时区改了，后缀都停在启动时那个值 ——
        玩家和服务器全在中国（UTC+8、不用夏令时），为一个本项目里不存在的
        场景在每行日志上留一次判断不值当。换时区就重启服务端。
        """
        with _FakeZone(5 * 3600, altzone=4 * 3600, daylight=1):
            frozen = tzstamp.utc_offset_text(0)
            summer = time.struct_time((2026, 7, 1, 12, 0, 0, 2, 182, 1))
            winter = time.struct_time((2026, 1, 1, 12, 0, 0, 3, 1, 0))
            saved = time.localtime
            try:
                for fake in (summer, winter):
                    time.localtime = lambda *_a, _f=fake: _f
                    self.assertEqual(frozen, tzstamp.utc_offset_text(0))
                    self.assertEqual(frozen, tzstamp.stamp(0).rsplit(" ", 1)[1])
            finally:
                time.localtime = saved

    def test_the_zone_is_computed_exactly_once(self):
        # ★ 用户 2026-09-20 指出第一版每行现算一遍：实测 1.35 us/行，把一个
        #   时间戳从 1.48 抬到 3.38 us。现在热路径上**一次都不算** —— 连
        #   第二版那次元组比较也没有了，只剩一次全局读。
        calls = []
        real = tzstamp._format_offset
        tzstamp._format_offset = lambda s: (calls.append(s), real(s))[1]
        try:
            for _ in range(100):
                tzstamp.utc_offset_text(0)
                tzstamp.stamp(0)
        finally:
            tzstamp._format_offset = real
        self.assertEqual([], calls, "启动之后还在算时区：%d 次" % len(calls))

    def test_a_zone_change_at_runtime_is_ignored(self):
        # 只改 `time.timezone`、不重算 `ZONE_TEXT` ⇒ 输出一个字不变。
        # （上面那三条假时区用例之所以有效，全靠夹具自己补了那一下重算。）
        before = tzstamp.utc_offset_text()
        saved = (time.timezone, time.altzone, time.daylight)
        try:
            time.timezone, time.altzone, time.daylight = (12 * 3600, 12 * 3600, 0)
            self.assertEqual(before, tzstamp.utc_offset_text())
            self.assertEqual(before, tzstamp.stamp().rsplit(" ", 1)[1])
        finally:
            time.timezone, time.altzone, time.daylight = saved

    def test_stamp_only_looks_up_localtime_once(self):
        # 日期和时区后缀共用同一份 `localtime()` —— 调两次等于把这个函数
        # 的开销翻倍，而它是服务端每一行日志都要走的路。
        n = [0]
        real = time.localtime
        time.localtime = lambda *a: (n.__setitem__(0, n[0] + 1), real(*a))[1]
        try:
            tzstamp.stamp(0, millis=True)
        finally:
            time.localtime = real
        self.assertEqual(1, n[0], "stamp() 调了 %d 次 localtime()" % n[0])

    def test_it_is_computed_not_hardcoded(self):
        # 写死成 UTC+8 / UTC+9 就是这次要防的错：玩家可能在任何时区。
        src = open(os.path.join(ROOT, "server", "tzstamp.py"),
                   encoding="utf-8").read()
        self.assertNotIn('"UTC+8"', src)
        self.assertNotIn('"UTC+9"', src)
        self.assertIn("time.altzone", src)


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
        self.assertIn('"[%02u:%02u:%02u.%03u UTC%c%ld] "', src,
                      "bslog 的行前缀没带时区")
        self.assertIn('"[%02u:%02u:%02u.%03u UTC%c%ld:%02ld] "', src,
                      "半小时时区（+5:30 / +5:45）那一支没了")
        self.assertIn("bslog_zone_minutes(&st);", src)

    def test_bshook_computes_the_offset_exactly_once(self):
        # ★ 用户 2026-09-20 第二轮拍板：**算一次，之后一直沿用**。
        #   第一版每行都算一遍 GetSystemTime + 两次 SystemTimeToFileTime +
        #   64 位除法 + **一个额外的 _snprintf**；开弹体诊断时每帧每弹体一行。
        #   第二版按「第几分钟」失效（为了跟夏令时），被否掉 —— 玩家全在中国。
        src = open(self.hook, encoding="utf-8").read()
        body = src[src.index("static LONG bslog_zone_minutes"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("if (g_zone_ready) return g_zone_minutes;", body,
                      "没有「已经算过了」的快路 —— 又变回每行现算了")
        self.assertNotIn("wMinute", body,
                         "又按分钟失效了 —— 说好算一次就不再算")
        # 先写值后写旗：反过来的话别的线程可能看到旗立了、值还是 0。
        self.assertLess(body.index("g_zone_minutes = mins;"),
                        body.index("g_zone_ready = 1;"),
                        "先立了旗才写值 —— 别的线程会读到 0")

    def test_the_updater_computes_the_offset_exactly_once_too(self):
        # 三侧同一套取舍（`server/tzstamp.py` / `bshook.c` / 更新器）。
        src = open(self.util, encoding="utf-8").read()
        body = src[src.index("void utc_offset_text"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("if (!g_zone_ready) {", body,
                      "更新器还在每次现算 —— 三侧的写法漂了")
        self.assertLess(body.index("g_zone_mins = "),
                        body.index("g_zone_ready = 1;"),
                        "先立了旗才写值")

    def test_the_prefix_only_formats_once(self):
        # 第一版是「先 snprintf 出 "UTC+9"，再 snprintf 进行前缀」——
        # 两个 snprintf。现在把符号/时/分折进原来那一发。
        src = open(self.hook, encoding="utf-8").read()
        body = src[src.index("static void bslog_emit"):]
        body = body[:body.index("\n    hdr[0]")]
        self.assertEqual(2, body.count("_snprintf("),
                         "行前缀的 _snprintf 不是两支变体各一次")
        self.assertNotIn("char zone[", body, "还在先拼一个时区字符串")

    def test_the_updater_stamp_carries_the_zone(self):
        src = open(self.util, encoding="utf-8").read()
        self.assertIn("utc_offset_text(zone, 16);", src)
        self.assertIn('L"%04u-%02u-%02u %02u:%02u:%02u %s"', src)

    def test_both_derive_the_offset_instead_of_reading_the_registry(self):
        # 本地时刻减 UTC 时刻 ⇒ 启动那一刻的夏令时自动就对，不用读注册表。
        for path in (self.hook, self.util):
            src = open(path, encoding="utf-8").read()
            self.assertIn("GetSystemTime(&ut);", src, path)
            self.assertNotIn("GetTimeZoneInformation", src, path)


if __name__ == "__main__":
    unittest.main()
