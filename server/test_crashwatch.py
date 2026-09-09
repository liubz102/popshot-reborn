#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端崩溃后自动上传诊断日志 —— **发送**那一半（`crashwatch.py`）。

收包那一半在 `test_crashstore.py`。

这里钉住的四件事：

1. `LastLoginId` 清洗 —— 它**不是注册账号名**，是玩家在登录框里打的任意字符串，
   洗出来的东西必须能过服务端那条正则（所以本文件直接拿 `crashstore.ID_RE` 验）；
2. 崩溃现场的定位 —— `.mdmp` 按 `Dump File Name:` 找、`BigShot.rpt` 只截最后一段；
3. 崩了 vs 正常退出 —— 判据是崩溃报告的 mtime 落不落在本次会话里；
4. 先落盘再上传、失败重试、下次启动补传。

★ 整套都用注入进去的假 `resolve_pid` / `wait` / `sleep` 跑，
  所以**在非 Windows 上也能验**，也不用真的崩一次游戏。
"""
import io
import json
import os
import tempfile
import threading
import time
import unittest
import zipfile

import crashstore
import crashwatch


#: 一份真实的 `LastCrashReport.txt`（取自 2026-09-09 01:36:42 那次火焰蝙蝠
#: 闪退，FINDINGS §47），只把寄存器和调用栈截短了。
REPORT_TEXT = """\


==================   logged at 09/09/26, 01:36:42    ==========================

Version: 311
Dump File Name: D:\\git\\popshot-reborn\\main\\game_patched\\Dump\\BigShotV0311N001.mdmp
Exception code: C0000005 ACCESS_VIOLATION
Fault address:  0047EA6C 01:0007DA6C D:\\git\\popshot-reborn\\main\\game_patched\\BigShot.exe

Registers:
EAX:00000001
EDI:00000000

Call stack:
Address   Frame
0047EA6C  0019FB44  0001:0007DA6C D:\\...\\BigShot.exe

CPU : 11th Gen Intel(R) Core(TM) i7-11800H @ 2.30GHz (16 CPUs), ~2.3GHz
RAM : 65534MB RAM
OS : Windows 10 64-bit
"""

#: `BigShot.rpt` 是**追加式**的：一份文件里躺着历次崩溃。
RPT_TEXT = """\
==================   logged at 09/06/26, 21:47:06    ==========================
Version: 311
Exception code: C0000005 ACCESS_VIOLATION
Fault address:  005D2702

==================   logged at 09/09/26, 01:36:42    ==========================
Version: 311
Exception code: C0000005 ACCESS_VIOLATION
Fault address:  0047EA6C
"""


class SanitizeAccountTests(unittest.TestCase):
    """`UserConfig.ini` 的 `LastLoginId` 什么脏东西都可能有。"""

    def test_a_real_account_name_survives_unchanged(self):
        # ★ 这条是整套清洗的立足点：能真正登录成功的账号名只允许
        #   ^[A-Za-z0-9_-]{2,16}$，所以清洗**不丢任何真实信息**。
        for name in ("testuser1", "a", "A_b-C", "0123456789abcdef"):
            self.assertEqual(name, crashwatch.sanitize_account(name))

    def test_junk_is_dropped_not_replaced(self):
        # 全角/中文全被丢掉 -> 空 -> unknown，而不是变成一串没信息量的下划线。
        self.assertEqual("unknown", crashwatch.sanitize_account("小明"))
        self.assertEqual("unknown", crashwatch.sanitize_account("！@#￥%"))

    def test_path_separators_and_dots_cannot_survive(self):
        # 白名单里没有 `.` 和 `/`，所以 `..` 这种路径段根本构造不出来。
        self.assertEqual("etcpasswd",
                         crashwatch.sanitize_account("../../etc/passwd"))
        self.assertEqual("aab", crashwatch.sanitize_account("a\\a/b"))
        self.assertEqual("unknown", crashwatch.sanitize_account(".."))
        self.assertEqual("unknown", crashwatch.sanitize_account("."))

    def test_spaces_and_edge_shapes(self):
        self.assertEqual("abcd", crashwatch.sanitize_account("  ab cd  "))
        self.assertEqual("x", crashwatch.sanitize_account("-x-"))
        self.assertEqual("unknown", crashwatch.sanitize_account("---"))
        self.assertEqual("unknown", crashwatch.sanitize_account(""))
        self.assertEqual("unknown", crashwatch.sanitize_account(None))

    def test_long_names_are_cut_to_the_account_limit(self):
        self.assertEqual("a" * 16, crashwatch.sanitize_account("a" * 200))

    def test_windows_reserved_words_need_no_special_case(self):
        # `CON` 本身合法，但最终目录名一定是 `CON_<安装码>_<时刻>` ——
        # 保留名只在「第一个点之前的整段」等于保留字时才生效，而这个名字
        # 既没有点、后面又必然跟着别的段。所以不需要维护一张保留名表。
        self.assertEqual("CON", crashwatch.sanitize_account("CON"))

    def test_every_sanitized_name_passes_the_server_side_regex(self):
        """★ 这条是两边的接缝：客户端洗出来的，服务端必须收得下。"""
        raw = ["testuser1", "小明", "../../etc/passwd", "  ab cd  ", "",
               "-x-", "a" * 200, "CON", "a\\a/b", "！@#￥%", "..",
               "\xff\xfe\x80乱码", "a b\tc\nd"]
        for text in raw:
            crash_id = "%s_%s_%s" % (crashwatch.sanitize_account(text),
                                     "a1b2c3d4", "20260909-013642")
            self.assertRegex(crash_id, crashstore.ID_RE,
                             "洗完的 %r 过不了服务端那条正则" % (text,))


class ReadLastLoginIdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "UserConfig.ini")

    def write(self, data: bytes):
        with open(self.path, "wb") as fp:
            fp.write(data)

    def test_reads_the_key(self):
        self.write(b"BgmVol=80\r\nLastLoginId=testuser1\r\nVer=311\r\n")
        self.assertEqual("testuser1", crashwatch.read_last_login_id(self.path))

    def test_cp936_bytes_do_not_raise(self):
        # ★ 原版是按 ANSI（中文机器上是 CP936）写的，不是 UTF-8。
        #   拿 UTF-8 去读会抛 UnicodeDecodeError，那才是真正会炸的地方。
        self.write("LastLoginId=小明\r\n".encode("cp936"))
        got = crashwatch.read_last_login_id(self.path)
        self.assertEqual("unknown", crashwatch.sanitize_account(got))

    def test_missing_key_and_missing_file(self):
        self.write(b"BgmVol=80\r\n")
        self.assertEqual("", crashwatch.read_last_login_id(self.path))
        self.assertEqual("", crashwatch.read_last_login_id(
            os.path.join(self.tmp.name, "nope.ini")))


class ReportParsingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.game = os.path.join(self.tmp.name, "game_patched")
        os.makedirs(os.path.join(self.game, "Dump"))
        with open(os.path.join(self.game, "Dump", "LastCrashReport.txt"),
                  "w", encoding="latin-1", newline="") as fp:
            fp.write(REPORT_TEXT)

    def test_fields_come_out_of_the_real_report(self):
        report = crashwatch.read_crash_report(self.game)
        self.assertEqual("09/09/26, 01:36:42", report.logged_at_text)
        self.assertEqual("20260909-013642", report.stamp)
        self.assertEqual("C0000005 ACCESS_VIOLATION", report.exception)
        self.assertTrue(report.fault.startswith("0047EA6C"))
        # 原版客户端自己的版本号，和我们的 BUILD.ver 是两回事。
        self.assertEqual("311", report.client_version)

    def test_dump_name_keeps_only_the_basename(self):
        # ★ 报告里写的是**绝对路径**，而 rpt 里躺着别的机器 / 别的目录留下的
        #   历史路径。只取 basename、在本机的 Dump\ 里找，才不会指到别处。
        report = crashwatch.read_crash_report(self.game)
        self.assertEqual("BigShotV0311N001.mdmp", report.dump_name)

    def test_missing_report_is_not_an_error(self):
        os.remove(os.path.join(self.game, "Dump", "LastCrashReport.txt"))
        self.assertIsNone(crashwatch.read_crash_report(self.game))

    def test_malformed_timestamp_falls_back_to_mtime(self):
        # 认不出的时刻**绝不**拿去拼目录名 —— 退回文件 mtime。
        # ★ 用 CP936 字节写：这个文件在真机上就是 ANSI 的，测试也照着来。
        path = os.path.join(self.game, "Dump", "LastCrashReport.txt")
        with open(path, "wb") as fp:
            fp.write("=========   logged at 乱七八糟   =========\r\n"
                     "Version: 311\r\n".encode("cp936"))
        when = time.mktime((2026, 1, 2, 3, 4, 5, 0, 0, -1))
        os.utime(path, (when, when))
        report = crashwatch.read_crash_report(self.game)
        self.assertEqual("20260102-030405", report.stamp)

    def test_no_record_header_at_all(self):
        path = os.path.join(self.game, "Dump", "LastCrashReport.txt")
        with open(path, "wb") as fp:
            fp.write("完全不是崩溃报告".encode("cp936"))
        report = crashwatch.read_crash_report(self.game)
        self.assertEqual("", report.logged_at_text)
        self.assertEqual("", report.dump_name)


class BuildInfoTests(unittest.TestCase):
    """`BUILD.ver` 有三种形态，三种都要认；读不到要**明说**读不到。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, text):
        with open(os.path.join(self.tmp.name, "BUILD.ver"), "w",
                  encoding="utf-8") as fp:
            fp.write(text)

    def test_the_rich_package_build_ver(self):
        """包里那份带 buildId 和几个 hash —— 查崩溃最想知道的就是这些。"""
        self.write(json.dumps({
            "version": "V0.3.0", "versionWire": 3000, "kind": "客户端包",
            "buildId": "20260909-005031", "time": "2026-09-09 00:50:32",
            "machine": "MSI-GP76",
            "bshookHash": "FB" * 32, "bsloaderHash": "1B" * 32,
            "serverCodeHash": "2B" * 32,
            "notes": ["一大段中文说明", "对查崩溃毫无用处"],
        }, ensure_ascii=False))
        got = crashwatch.read_build_info(self.tmp.name)
        self.assertEqual("V0.3.0", got["version"])
        self.assertEqual("20260909-005031", got["buildId"])
        self.assertEqual("FB" * 32, got["bshookHash"])
        # ★ notes 和打包机器名不该进包 —— 前者几百字节没用，后者是噪声。
        self.assertNotIn("notes", got)
        self.assertNotIn("machine", got)

    def test_the_thin_worktree_build_ver(self):
        self.write('{"version": "V0.3.0"}')
        self.assertEqual({"version": "V0.3.0"},
                         crashwatch.read_build_info(self.tmp.name))

    def test_missing_says_so_instead_of_going_quiet(self):
        got = crashwatch.read_build_info(self.tmp.name)
        self.assertIn("error", got)
        self.assertIn("读不到", got["error"])

    def test_garbage_keeps_a_snippet_of_the_original(self):
        self.write("这不是 JSON")
        got = crashwatch.read_build_info(self.tmp.name)
        self.assertIn("error", got)
        self.assertIn("这不是 JSON", got["raw"])

    def test_a_bom_does_not_break_it(self):
        # 有人拿记事本改过就会带 BOM。
        with open(os.path.join(self.tmp.name, "BUILD.ver"), "wb") as fp:
            fp.write(b"\xef\xbb\xbf" + b'{"version": "V0.3.1"}')
        self.assertEqual({"version": "V0.3.1"},
                         crashwatch.read_build_info(self.tmp.name))


class RptSplitTests(unittest.TestCase):
    def test_only_the_last_record_is_taken(self):
        records = crashwatch.split_records(RPT_TEXT)
        self.assertEqual(2, len(records))
        tail = crashwatch.last_record(RPT_TEXT)
        self.assertIn("09/09/26, 01:36:42", tail)
        self.assertNotIn("09/06/26", tail)

    def test_empty_and_headerless_text(self):
        self.assertEqual("", crashwatch.last_record(""))
        self.assertEqual("", crashwatch.last_record("随便什么内容\n"))


class PickLogsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = self.tmp.name
        self.start = time.time() - 60

    def touch(self, name, ago=0.0):
        path = os.path.join(self.logs, name)
        with open(path, "wb") as fp:
            fp.write(b"x")
        when = time.time() - ago
        os.utime(path, (when, when))
        return path

    def names(self, *args, **kwargs):
        return sorted(os.path.basename(p)
                      for p in crashwatch.pick_logs(*args, **kwargs))

    def test_current_run_files_and_this_pid_only(self):
        self.touch("relay.out")
        self.touch("bsloader.out")
        self.touch("server.out")
        self.touch("bshook_20260909_013047_pid41612.log")
        self.touch("bshook_20260909_012742_pid16040.log", ago=99999)
        # 归档的（上一次运行）：mtime 比本次会话早，判据自然排除掉它。
        self.touch("relay-20260908-235959.out", ago=99999)
        got = self.names(self.logs, self.start, 41612, time.time())
        self.assertIn("relay.out", got)
        self.assertIn("bsloader.out", got)
        self.assertIn("server.out", got)
        self.assertIn("bshook_20260909_013047_pid41612.log", got)
        self.assertNotIn("bshook_20260909_012742_pid16040.log", got)
        self.assertNotIn("relay-20260908-235959.out", got)

    def test_fresh_files_from_this_run_are_included(self):
        # 「本次运行的全都传」：mtime ≥ 本次进程创建时刻的日志都算。
        self.touch("auth_20260909-013000_001_47611.txt")
        self.touch("game_20260909-013000_001_27799.bin")
        got = self.names(self.logs, self.start, 0, time.time())
        self.assertIn("auth_20260909-013000_001_47611.txt", got)
        self.assertIn("game_20260909-013000_001_27799.bin", got)

    def test_non_log_junk_in_the_logs_dir_is_left_alone(self):
        # logs\ 里还躺着逆向时手工留下的产物，那些不是日志。
        self.touch("bot_motion_test.dll")
        self.touch("shot_lobby.png")
        self.touch(".client_id")
        got = self.names(self.logs, self.start, 0, time.time())
        self.assertEqual([], [n for n in got
                              if n in ("bot_motion_test.dll", "shot_lobby.png",
                                       ".client_id")])


class InstallIdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_generated_once_then_stable(self):
        first = crashwatch.install_id(self.tmp.name)
        self.assertRegex(first, r"^[0-9a-f]{8}$")
        self.assertEqual(first, crashwatch.install_id(self.tmp.name))

    def test_garbage_in_the_file_is_regenerated(self):
        path = os.path.join(self.tmp.name, crashwatch.CLIENT_ID_FILENAME)
        with open(path, "wb") as fp:
            fp.write("这不是一个安装码".encode("cp936"))
        self.assertRegex(crashwatch.install_id(self.tmp.name),
                         r"^[0-9a-f]{8}$")


class _Fixture:
    """一份完整的假客户端目录树：`game_patched/` + `logs/`。"""

    def __init__(self, base, dump_bytes=4096):
        self.root = base
        self.game = os.path.join(base, "game_patched")
        self.logs = os.path.join(base, "logs")
        os.makedirs(os.path.join(self.game, "Dump"))
        os.makedirs(os.path.join(self.game, "Debug"))
        os.makedirs(self.logs)
        with open(os.path.join(self.game, "Dump", "LastCrashReport.txt"),
                  "w", encoding="latin-1", newline="") as fp:
            fp.write(REPORT_TEXT)
        with open(os.path.join(self.game, "Dump", "BigShotV0311N001.mdmp"),
                  "wb") as fp:
            fp.write(b"MDMP" + b"\0" * (dump_bytes - 4))
        with open(os.path.join(self.game, "BigShot.rpt"),
                  "w", encoding="latin-1", newline="") as fp:
            fp.write(RPT_TEXT)
        with open(os.path.join(self.game, "UserConfig.ini"), "wb") as fp:
            fp.write(b"LastLoginId=testuser1\r\nVer=311\r\n")
        # ★ `BUILD.ver` 在**包根**，不在 game_patched 里。
        with open(os.path.join(base, "BUILD.ver"), "w", encoding="utf-8") as fp:
            json.dump({"version": "V0.3.0", "buildId": "20260909-005031",
                       "bshookHash": "FB" * 32,
                       "notes": ["不该进包的一大段说明"]}, fp,
                      ensure_ascii=False)
        with open(os.path.join(self.logs, "relay.out"), "w",
                  encoding="utf-8") as fp:
            fp.write("中继日志\n")
        with open(os.path.join(self.game, "Debug", "2026-09-09.txt"),
                  "w", encoding="latin-1") as fp:
            fp.write("PlayEff(...) Failed\n")
        # 崩溃报告的 mtime 定在「崩溃那一刻」，会话判定要用它。
        when = time.mktime((2026, 9, 9, 1, 36, 42, 0, 0, -1))
        os.utime(os.path.join(self.game, "Dump", "LastCrashReport.txt"),
                 (when, when))
        self.crash_epoch = when


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fx = _Fixture(self.tmp.name)

    def collector(self, max_bytes=0):
        return crashwatch.Collector(root=self.tmp.name, logdir=self.fx.logs,
                                    gamedir=self.fx.game, max_bytes=max_bytes)

    def build(self, max_bytes=0):
        col = self.collector(max_bytes)
        report = crashwatch.read_crash_report(self.fx.game)
        out = os.path.join(self.tmp.name, "out.zip")
        meta = col.build(report, out, session_start=self.fx.crash_epoch - 600,
                         pid=41612, exit_code=0xC0000005)
        return out, meta, col.crash_id(report)

    def test_crash_id_shape(self):
        _out, _meta, crash_id = self.build()
        self.assertRegex(crash_id,
                         r"^testuser1_[0-9a-f]{8}_20260909-013642$")
        self.assertRegex(crash_id, crashstore.ID_RE)

    def test_zip_contents(self):
        out, meta, _ = self.build()
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
            self.assertIn("meta.json", names)
            self.assertIn("Dump/LastCrashReport.txt", names)
            self.assertIn("Dump/BigShotV0311N001.mdmp", names)
            self.assertIn("BigShot.rpt.last.txt", names)
            self.assertIn("Debug/2026-09-09.txt", names)
            self.assertIn("logs/relay.out", names)
            # ★ rpt 只截最后一段
            tail = zf.read("BigShot.rpt.last.txt").decode("latin-1")
            self.assertIn("09/09/26, 01:36:42", tail)
            self.assertNotIn("09/06/26", tail)
            got = json.loads(zf.read("meta.json").decode("utf-8"))
        self.assertEqual("C0000005 ACCESS_VIOLATION", got["exception"])
        self.assertEqual(41612, got["pid"])
        self.assertEqual(0xC0000005, got["exit_code"])
        self.assertEqual([], got["skipped"])
        self.assertEqual(meta["crash_time"], "20260909-013642")
        # ★ 构建信息要真的填上（曾经这里是个静默的空串），而且只挑有用的几项。
        self.assertEqual("V0.3.0", got["build"]["version"])
        self.assertEqual("20260909-005031", got["build"]["buildId"])
        self.assertEqual("FB" * 32, got["build"]["bshookHash"])
        self.assertNotIn("notes", got["build"])
        self.assertNotIn("error", got["build"])
        # 原版客户端自己的版本号（和上面那个是两回事）。
        self.assertEqual("311", got["client_version"])

    def test_a_missing_build_ver_says_so_instead_of_an_empty_string(self):
        os.remove(os.path.join(self.tmp.name, "BUILD.ver"))
        out, _meta, _ = self.build()
        with zipfile.ZipFile(out) as zf:
            got = json.loads(zf.read("meta.json").decode("utf-8"))
        self.assertIn("读不到", got["build"]["error"])

    def test_report_text_comes_from_memory_not_from_disk(self):
        """★ 原版客户端下次登录会把 `LastCrashReport.txt` 当 0x0103 的载荷发走
        并**删掉文件**（2026-09-09 实测）。所以打包必须用已经读进内存的那份。"""
        col = self.collector()
        report = crashwatch.read_crash_report(self.fx.game)
        os.remove(os.path.join(self.fx.game, "Dump", "LastCrashReport.txt"))
        out = os.path.join(self.tmp.name, "out.zip")
        col.build(report, out)
        with zipfile.ZipFile(out) as zf:
            text = zf.read("Dump/LastCrashReport.txt").decode("latin-1")
        self.assertIn("09/09/26, 01:36:42", text)

    def test_oversize_optional_entries_are_skipped_not_truncated(self):
        # 塞一个大日志，把预算调到刚好放不下它。必带的三样仍然齐全。
        with open(os.path.join(self.fx.logs, "bsloader.out"), "wb") as fp:
            fp.write(os.urandom(200000))        # 随机数据压不动
        out, meta, _ = self.build(max_bytes=80000)
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
        self.assertIn("Dump/LastCrashReport.txt", names)
        self.assertIn("Dump/BigShotV0311N001.mdmp", names)
        self.assertNotIn("logs/bsloader.out", names)
        self.assertTrue(any(item["name"] == "logs/bsloader.out"
                            for item in meta["skipped"]), meta["skipped"])

    def test_missing_dump_is_recorded_instead_of_crashing(self):
        os.remove(os.path.join(self.fx.game, "Dump", "BigShotV0311N001.mdmp"))
        out, meta, _ = self.build()
        self.assertTrue(any(item["why"] == "文件不在了"
                            for item in meta["skipped"]))
        with zipfile.ZipFile(out) as zf:
            self.assertIn("Dump/LastCrashReport.txt", zf.namelist())

    def test_rpt_mismatch_is_flagged_in_the_zip(self):
        with open(os.path.join(self.fx.game, "BigShot.rpt"), "wb") as fp:
            fp.write("=========   logged at 01/01/20, 00:00:00   =========\r\n"
                     "别的一次崩溃\r\n".encode("cp936"))
        out, meta, _ = self.build()
        self.assertTrue(meta.get("rpt_mismatch"))
        with zipfile.ZipFile(out) as zf:
            raw = zf.read("BigShot.rpt.last.txt")
        # ★ 包里那份必须和玩家机器上那份**逐字节相同** —— 正文里不掺任何
        #   我们自己的话，提醒放在 meta.json 里。
        with open(os.path.join(self.fx.game, "BigShot.rpt"), "rb") as fp:
            self.assertEqual(fp.read(), raw)


class _FakeServer:
    """一个只认 `/api/crash-report` 的假服务器，用来验重试和补传。"""

    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.received = []
        self.attempts = 0

    def connect(self, host, port):
        import socket

        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise OSError("连不上（假的）")
        left, right = socket.socketpair()
        threading.Thread(target=self._serve, args=(right,),
                         daemon=True).start()
        return left

    def _serve(self, sock):
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(65536)
                if not chunk:
                    return
                data += chunk
            head, _, body = data.partition(b"\r\n\r\n")
            headers = {}
            for line in head.split(b"\r\n")[1:]:
                key, _, value = line.partition(b":")
                headers[key.decode().strip().lower()] = value.decode().strip()
            length = int(headers.get("content-length", "0"))
            while len(body) < length:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                body += chunk
            self.received.append((headers, body))
            payload = json.dumps({"ok": True,
                                  "saved": headers.get("x-crash-client", "")})
            payload = payload.encode("utf-8")
            sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Content-Length: " + str(len(payload)).encode() +
                         b"\r\nConnection: close\r\n\r\n" + payload)
        except OSError:
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fx = _Fixture(self.tmp.name)
        self.lines = []
        self.slept = []

    def watcher(self, server, **kwargs):
        return crashwatch.CrashWatcher(
            host="example.invalid", port=27810, connect=server.connect,
            root=self.tmp.name, logdir=self.fx.logs, gamedir=self.fx.game,
            log=self.lines.append, sleep=self.slept.append, **kwargs)

    # ---------------------------------------------------------- 崩了 vs 正常退出
    def test_a_crash_inside_this_session_is_collected_and_uploaded(self):
        server = _FakeServer()
        watcher = self.watcher(server)
        crash_id = watcher.handle_exit(41612, self.fx.crash_epoch - 600,
                                       0xC0000005)
        self.assertIsNotNone(crash_id)
        self.assertEqual(1, len(server.received))
        headers, body = server.received[0]
        self.assertEqual(crash_id, headers["x-crash-client"])
        self.assertEqual("09/09/26, 01:36:42", headers["x-crash-time"])
        self.assertTrue(body.startswith(b"PK"))
        # 传成功了就不该在待传队列里留东西。
        self.assertEqual([], os.listdir(watcher.pending_dir))

    def test_a_normal_exit_uploads_nothing(self):
        """★ 这条是整个功能的证伪实验：崩溃报告比本次会话老 = 上次留下的。"""
        server = _FakeServer()
        watcher = self.watcher(server)
        self.assertIsNone(watcher.handle_exit(41612,
                                              self.fx.crash_epoch + 600, 0))
        self.assertEqual([], server.received)

    def test_no_report_at_all_uploads_nothing(self):
        os.remove(os.path.join(self.fx.game, "Dump", "LastCrashReport.txt"))
        server = _FakeServer()
        watcher = self.watcher(server)
        self.assertIsNone(watcher.handle_exit(41612, 0, 0))
        self.assertEqual([], server.received)

    def test_the_same_crash_is_never_uploaded_twice(self):
        """按**状态翻转**去重（记住上次处理的是哪一份），不按次数/时间窗。"""
        server = _FakeServer()
        watcher = self.watcher(server)
        watcher.handle_exit(41612, self.fx.crash_epoch - 600, 0xC0000005)
        self.assertIsNone(watcher.handle_exit(41613,
                                              self.fx.crash_epoch - 600, 0))
        self.assertEqual(1, len(server.received))

    # ------------------------------------------------------------ 重试 / 补传
    def test_retries_three_times_then_keeps_the_zip_for_next_start(self):
        server = _FakeServer(fail_times=99)
        watcher = self.watcher(server)
        crash_id = watcher.handle_exit(41612, self.fx.crash_epoch - 600,
                                       0xC0000005)
        self.assertEqual(crashwatch.RETRY_ATTEMPTS, server.attempts)
        # 两次重试之间各等一回（最后一次失败后不再等）。
        self.assertEqual([crashwatch.RETRY_DELAY_SECONDS] *
                         (crashwatch.RETRY_ATTEMPTS - 1), self.slept)
        # ★ 先落盘再上传 ⇒ 传不上去时现场还在。
        self.assertEqual([crash_id + ".zip"], os.listdir(watcher.pending_dir))
        self.assertTrue(any("连续 3 次失败" in line for line in self.lines))

    def test_a_later_start_resends_what_is_queued(self):
        watcher = self.watcher(_FakeServer(fail_times=99))
        watcher.handle_exit(41612, self.fx.crash_epoch - 600, 0xC0000005)
        # 换一台「已经修好」的服务器，走补传那条路。
        server = _FakeServer()
        again = self.watcher(server)
        again._resume()
        self.assertEqual(1, len(server.received))
        self.assertEqual([], os.listdir(again.pending_dir))

    def test_logs_are_written_whether_it_succeeds_or_not(self):
        for server, needle in ((_FakeServer(), "✓"), (_FakeServer(99), "✗")):
            self.lines.clear()
            # 第二轮要重新走一遍，所以先把「这一份处理过了」的状态清掉。
            state = os.path.join(self.fx.logs, crashwatch.STATE_FILENAME)
            if os.path.exists(state):
                os.remove(state)
            watcher = self.watcher(server)
            watcher.handle_exit(41612, self.fx.crash_epoch - 600, 0xC0000005)
            self.assertTrue(any("捕获到一次崩溃" in line
                                for line in self.lines), self.lines)
            self.assertTrue(any(needle in line for line in self.lines),
                            self.lines)

    def test_an_oversize_package_is_not_even_attempted(self):
        server = _FakeServer()
        watcher = self.watcher(server, max_bytes=100)
        watcher.handle_exit(41612, self.fx.crash_epoch - 600, 0xC0000005)
        self.assertEqual([], server.received)
        self.assertTrue(any(name.endswith(".toobig.zip")
                            for name in os.listdir(watcher.pending_dir)))
        self.assertTrue(any("超过上限" in line for line in self.lines))

    # -------------------------------------------------------------- 不阻塞
    def test_note_client_returns_immediately(self):
        """★ 它跑在中继的**转发线程**上，后面紧接着就是转发游戏字节。

        注入一个会睡很久的 `resolve_pid`：`note_client` 一旦把它同步跑掉，
        这条断言就会失败。
        """
        started = threading.Event()

        def slow_resolve(ip, port, local_port):
            started.set()
            time.sleep(30)
            return None

        watcher = self.watcher(_FakeServer(), resolve_pid=slow_resolve)
        watcher.start()
        self.addCleanup(watcher._queue.put, None)
        begin = time.monotonic()
        for _ in range(50):
            watcher.note_client(("127.0.0.1", 5555), 47621)
        cost = time.monotonic() - begin
        self.assertLess(cost, 1.0, "note_client 挡住了转发线程 %.3fs" % cost)
        self.assertTrue(started.wait(5), "后台线程没有接手")

    def test_disabled_watcher_does_nothing(self):
        server = _FakeServer()
        watcher = self.watcher(server, enabled=False)
        self.assertIsNone(watcher.start())
        watcher.note_client(("127.0.0.1", 5555), 47621)
        self.assertEqual([], server.received)

    def test_one_session_is_watched_once_even_with_three_connections(self):
        """中继有三个监听口，同一次会话会 accept 三条连接 —— 只该盯一次。

        `fake_wait` 一直挂着直到测试放行，模拟真实情形：游戏活着的整段时间里
        那条 watcher 线程都停在 `WaitForSingleObject` 上。
        """
        waited = []
        entered = threading.Event()
        release = threading.Event()

        def fake_wait(pid):
            waited.append(pid)
            entered.set()
            release.wait(5)
            return None

        watcher = self.watcher(_FakeServer(),
                               resolve_pid=lambda *a: 41612, wait=fake_wait)
        watcher.start()
        self.addCleanup(release.set)
        self.addCleanup(watcher._queue.put, None)
        for port in (47621, 27809, 27808):
            watcher.note_client(("127.0.0.1", 5555), port)
        self.assertTrue(entered.wait(5), "后台线程没有接手")
        # 三条 note 都排进去了；等队列被吃干净再看盯了几次。
        for _ in range(50):
            if watcher._queue.empty():
                break
            time.sleep(0.01)
        self.assertEqual([41612], waited)


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_old_queued_packages_are_dropped(self):
        watcher = crashwatch.CrashWatcher(host="h", port=1, connect=None,
                                          logdir=self.tmp.name, keep_days=3)
        os.makedirs(watcher.pending_dir)
        old = os.path.join(watcher.pending_dir, "a_00000000_20260101-000000.zip")
        new = os.path.join(watcher.pending_dir, "b_00000000_20260909-000000.zip")
        for path, ago in ((old, 10 * 86400), (new, 0)):
            with open(path, "wb") as fp:
                fp.write(b"PK")
            when = time.time() - ago
            os.utime(path, (when, when))
        self.assertEqual(1, watcher.prune_pending())
        self.assertEqual([os.path.basename(new)],
                         os.listdir(watcher.pending_dir))

    def test_keep_days_zero_never_deletes(self):
        watcher = crashwatch.CrashWatcher(host="h", port=1, connect=None,
                                          logdir=self.tmp.name, keep_days=0)
        os.makedirs(watcher.pending_dir)
        path = os.path.join(watcher.pending_dir, "a_00000000_20200101-000000.zip")
        with open(path, "wb") as fp:
            fp.write(b"PK")
        os.utime(path, (0, 0))
        self.assertEqual(0, watcher.prune_pending())
        self.assertEqual(1, len(os.listdir(watcher.pending_dir)))


if __name__ == "__main__":
    unittest.main()
