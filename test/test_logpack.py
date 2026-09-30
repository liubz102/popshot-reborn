#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日志打包下载的数据层（`logpack.py`，管理页「数据管理」→「下载日志」，V0.3商店）。

全部用临时目录，不起 HTTP 服务器（接口那一层在 `test_web_admin.AdminLogsApiTests`）。
输出对象一律用 `_Sink`：只有 `write()` / `flush()`，和 HTTP 响应体一个形状 ——
zipfile 往不能 `seek` 的流里写这条路，就是在这儿钉住的。
"""
import io
import os
import re
import tempfile
import time
import unittest
import zipfile

import logpack
import sevenzip
from testsupport import read_7z

HOUR = 3600.0


def touch(path, mtime, size=1):
    """造一个文件，把它的 mtime 拨到 `mtime`。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fp:
        fp.write(b"x" * size)
    os.utime(path, (mtime, mtime))
    return path


class _Sink:
    """只有 `write()` / `flush()` 的最小输出对象（HTTP 响应体长这样）。`write` 返回长度。"""

    def __init__(self):
        self.buf = io.BytesIO()
        self.calls = 0

    def write(self, data):
        self.calls += 1
        return self.buf.write(data)

    def flush(self):
        pass

    def zip(self):
        return zipfile.ZipFile(io.BytesIO(self.buf.getvalue()))


class _BrokenSink(_Sink):
    """写到第 `fail_at` 次就断（模拟浏览器取消下载）。"""

    def __init__(self, fail_at):
        super().__init__()
        self.fail_at = fail_at

    def write(self, data):
        if self.calls + 1 >= self.fail_at:
            self.calls += 1
            raise ConnectionResetError(10054, "对方关了连接")
        return super().write(data)


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logdir = os.path.join(self.tmp.name, "logs")
        self.crash = os.path.join(self.tmp.name, "logs_client_crash")
        os.makedirs(self.logdir)
        os.makedirs(self.crash)
        self.now = time.time()
        self.packer = logpack.LogPacker(self.logdir, self.crash, clock=lambda: self.now)

    def log(self, name, hours_ago=0.0, size=1):
        return touch(os.path.join(self.logdir, name), self.now - hours_ago * HOUR, size)

    def crash_dir(self, name, files=(("crash.zip", 10), ("receipt.json", 5))):
        root = os.path.join(self.crash, name)
        os.makedirs(root, exist_ok=True)
        for rel, size in files:
            touch(os.path.join(root, *rel.split("/")), self.now, size)
        return root


# ----------------------------------------------------------------- 清单
class ServerFilesTests(_Case):
    def names(self):
        return [name for name, _size, _mtime in self.packer.server_files()]

    def test_only_first_level_regular_files_count(self):
        self.log("server.out")
        self.log("online.log")
        # 子目录里的东西不算（开发机的 logs/investigation-deps/ 是逆向工具，300 MB）。
        touch(os.path.join(self.logdir, "investigation-deps", "big.bin"), self.now, 4096)
        self.assertEqual(["online.log", "server.out"], self.names())

    def test_dot_files_are_included(self):
        # `.server_mode` / `.relay_target` 是状态文件 —— 排查时它们说明「这次是哪种模式」，
        # 小到可以忽略，一并带上（和 logcleanup 「不删」它们是两回事）。
        self.log(".server_mode")
        self.log("server.out")
        self.assertEqual([".server_mode", "server.out"], self.names())

    def test_a_missing_directory_is_just_empty(self):
        packer = logpack.LogPacker(os.path.join(self.tmp.name, "nope"),
                                   os.path.join(self.tmp.name, "nope2"))
        self.assertEqual([], packer.server_files())
        self.assertEqual([], packer.crash_dirs())
        self.assertEqual(0, packer.overview()["server"]["total"]["files"])

    def test_sizes_and_mtimes_come_back(self):
        self.log("server.out", hours_ago=2, size=300)
        (name, size, mtime), = self.packer.server_files()
        self.assertEqual(("server.out", 300), (name, size))
        self.assertAlmostEqual(self.now - 2 * HOUR, mtime, delta=2)


class RecentTests(_Case):
    def recent(self):
        return [name for name, _s, _m in self.packer.recent_files()]

    def test_the_window_is_recent_hours_by_mtime(self):
        self.log("fresh.log", hours_ago=1)
        self.log("edge.log", hours_ago=logpack.RECENT_HOURS - 0.5)
        self.log("old.log", hours_ago=logpack.RECENT_HOURS + 0.5)
        self.log("ancient.out", hours_ago=72)
        self.assertEqual(["edge.log", "fresh.log"], self.recent())

    def test_the_window_is_twelve_hours(self):
        # 用户 2026-09-17 定的数；改它要改这里，也要改 README 里的说法。
        self.assertEqual(12, logpack.RECENT_HOURS)

    def test_a_file_still_open_for_writing_is_seen_fresh(self):
        """★ `server.out` 被 daylog 长期以追加模式持有。Windows 的目录项对开着的文件
        只在关闭时更新大小 / mtime —— `scandir().stat()` 会看到旧值，`os.stat` 不会。
        这条用例在 Linux 上平凡通过，在 Windows 上才是它的意义。"""
        path = self.log("server.out", hours_ago=30, size=1)      # 「昨天」就有的文件
        fh = open(path, "ab")
        # ★ 先注册 close 再让它活着：addCleanup 后进先出，close 会排在 tmp.cleanup
        #   之前跑，否则 Windows 上临时目录删不掉。
        self.addCleanup(fh.close)
        fh.write(b"y" * 5000)
        fh.flush()
        (_name, size, mtime), = self.packer.server_files()
        self.assertEqual(5001, size)
        self.assertGreater(mtime, self.now - HOUR)
        self.assertEqual(["server.out"], self.recent())


class CrashDirsTests(_Case):
    def names(self):
        return [name for name, _c, _s, _m in self.packer.crash_dirs()]

    def test_first_level_directories_sorted_by_name(self):
        self.crash_dir("bob_deadbeef_20260910-010203")
        self.crash_dir("alice_a1b2c3d4_20260909-013642")
        self.crash_dir("alice_a1b2c3d4_20260911-000000")
        self.assertEqual(["alice_a1b2c3d4_20260909-013642",
                          "alice_a1b2c3d4_20260911-000000",
                          "bob_deadbeef_20260910-010203"], self.names())

    def test_half_received_and_doomed_dirs_and_loose_files_are_skipped(self):
        self.crash_dir("alice_a1b2c3d4_20260909-013642")
        self.crash_dir(".tmp-bob_deadbeef_20260910-010203")     # 没收完的
        self.crash_dir(".tmp-del-1758000000000")                # 正在删的
        touch(os.path.join(self.crash, "stray.txt"), self.now)  # 不是目录
        self.assertEqual(["alice_a1b2c3d4_20260909-013642"], self.names())

    def test_counts_are_recursive(self):
        self.crash_dir("alice_a1b2c3d4_20260909-013642",
                       files=(("crash.zip", 100), ("receipt.json", 20),
                              ("extra/notes.txt", 7)))
        (name, count, size, _mtime), = self.packer.crash_dirs()
        self.assertEqual(("alice_a1b2c3d4_20260909-013642", 3, 127), (name, count, size))


class OverviewTests(_Case):
    def test_the_shape_the_popup_reads(self):
        self.log("server.out", hours_ago=1, size=1000)
        self.log("server-20260901.out", hours_ago=100, size=5000)
        self.crash_dir("alice_a1b2c3d4_20260909-013642", files=(("crash.zip", 300),))
        view = self.packer.overview()
        self.assertEqual(logpack.RECENT_HOURS, view["recent_hours"])
        self.assertEqual({"files": 2, "size": 6000, "size_text": "6 KB"},
                         view["server"]["total"])
        self.assertEqual({"files": 1, "size": 1000, "size_text": "1000 B"},
                         view["server"]["recent"])
        self.assertEqual(self.logdir, view["server"]["dir"])
        self.assertEqual("logs", view["server"]["dirname"])
        self.assertEqual({"files": 1, "size": 300, "size_text": "300 B", "dirs": 1},
                         view["crash"]["total"])
        row, = view["crash"]["dirs"]
        self.assertEqual("alice_a1b2c3d4_20260909-013642", row["name"])
        self.assertEqual((1, 300, "300 B"), (row["files"], row["size"], row["size_text"]))
        self.assertRegex(row["mtime_text"],
                         r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC[+-]\d+(:\d\d)?$")
        self.assertRegex(view["generated_text"],
                         r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC[+-]\d+(:\d\d)?$")


# ----------------------------------------------------------------- 计划
class PlanTests(_Case):
    NAME_RE = re.compile(r"^logs_(server|server_12h|client_crash|client_crash_[A-Za-z0-9_.-]+)"
                         r"_\d{8}-\d{6}\.zip$")

    def test_server_all_lists_every_first_level_file(self):
        self.log("server.out", hours_ago=1)
        self.log("old.out", hours_ago=50)
        plan = self.packer.plan("server", "all")
        self.assertEqual([("old.out", "logs/old.out"), ("server.out", "logs/server.out")],
                         [(os.path.basename(p), a) for p, a in plan.entries])
        self.assertEqual("服务端日志（全量）", plan.label)
        self.assertRegex(plan.filename, self.NAME_RE)
        self.assertTrue(plan.filename.startswith("logs_server_2"))

    def test_server_recent_only_lists_fresh_files(self):
        self.log("server.out", hours_ago=1)
        self.log("old.out", hours_ago=50)
        plan = self.packer.plan("server", "recent")
        self.assertEqual(["logs/server.out"], [a for _p, a in plan.entries])
        self.assertEqual("服务端日志（最近 12 小时）", plan.label)
        self.assertTrue(plan.filename.startswith("logs_server_12h_"), plan.filename)

    def test_the_stamp_is_the_server_local_time(self):
        self.log("server.out")
        self.now = time.mktime((2026, 9, 17, 21, 30, 45, 0, 0, -1))
        plan = self.packer.plan("server")
        self.assertEqual("logs_server_20260917-213045.zip", plan.filename)

    def test_crash_all_and_one_sub(self):
        self.crash_dir("alice_a1b2c3d4_20260909-013642", files=(("crash.zip", 3), ("receipt.json", 2)))
        self.crash_dir("bob_deadbeef_20260910-010203", files=(("crash.zip", 3),))
        whole = self.packer.plan("client_crash")
        self.assertEqual(["logs_client_crash/alice_a1b2c3d4_20260909-013642/crash.zip",
                          "logs_client_crash/alice_a1b2c3d4_20260909-013642/receipt.json",
                          "logs_client_crash/bob_deadbeef_20260910-010203/crash.zip"],
                         [a for _p, a in whole.entries])
        self.assertEqual("客户端崩溃包（全部 2 份）", whole.label)
        self.assertTrue(whole.filename.startswith("logs_client_crash_2"), whole.filename)
        one = self.packer.plan("client_crash", sub="bob_deadbeef_20260910-010203")
        self.assertEqual(["logs_client_crash/bob_deadbeef_20260910-010203/crash.zip"],
                         [a for _p, a in one.entries])
        self.assertEqual("崩溃包 bob_deadbeef_20260910-010203", one.label)
        self.assertTrue(one.filename.startswith(
            "logs_client_crash_bob_deadbeef_20260910-010203_"), one.filename)
        self.assertRegex(one.filename, self.NAME_RE)

    def test_a_sub_must_match_a_listed_directory_exactly(self):
        self.crash_dir("alice_a1b2c3d4_20260909-013642")
        for bad in ("..", "../data", "alice_a1b2c3d4_20260909-013642/..",
                    "nope_a1b2c3d4_20260909-013642",
                    "ALICE_a1b2c3d4_20260909-013642",             # 大小写不同也不算
                    ".tmp-alice_a1b2c3d4_20260909-013642",
                    "alice_a1b2c3d4_20260909-013642\\crash.zip"):
            with self.assertRaises(logpack.NothingToPack, msg=bad) as caught:
                self.packer.plan("client_crash", sub=bad)
            self.assertEqual(404, caught.exception.status)

    def test_bad_kind_or_scope_is_a_400(self):
        self.log("server.out")
        self.crash_dir("alice_a1b2c3d4_20260909-013642")
        for kind, scope, sub in (("", "all", ""), ("nope", "all", ""),
                                 ("server", "yesterday", ""), ("server", "all", "x"),
                                 ("client_crash", "recent", "")):
            with self.assertRaises(logpack.LogPackError, msg=(kind, scope)) as caught:
                self.packer.plan(kind, scope, sub)
            self.assertEqual(400, caught.exception.status)

    def test_nothing_to_pack_is_a_404(self):
        with self.assertRaises(logpack.NothingToPack):
            self.packer.plan("server", "all")
        self.log("old.out", hours_ago=50)
        with self.assertRaises(logpack.NothingToPack):
            self.packer.plan("server", "recent")
        with self.assertRaises(logpack.NothingToPack):
            self.packer.plan("client_crash")
        os.makedirs(os.path.join(self.crash, "empty_a1b2c3d4_20260909-013642"))
        with self.assertRaises(logpack.NothingToPack):
            self.packer.plan("client_crash", sub="empty_a1b2c3d4_20260909-013642")

    def test_a_strange_sub_name_still_makes_an_ascii_filename(self):
        self.crash_dir("张三_a1b2c3d4_20260909-013642")
        plan = self.packer.plan("client_crash", sub="张三_a1b2c3d4_20260909-013642")
        plan.filename.encode("ascii")            # 不能炸：HTTP 头是 latin-1
        self.assertTrue(plan.filename.startswith("logs_client_crash___"), plan.filename)


# ----------------------------------------------------------------- 打包
class WriteZipTests(_Case):
    def test_a_valid_zip_comes_out_of_a_write_only_sink(self):
        self.log("server.out", size=50000)
        self.log("online.log", size=10)
        sink = _Sink()
        stats = self.packer.write_zip(sink, self.packer.plan("server"),
                                      meta={"version": "V0.3.3", "by": "admin"})
        self.assertEqual({"files": 2, "bytes": 50010, "skipped": []}, stats)
        zf = sink.zip()
        self.assertIsNone(zf.testzip())
        self.assertEqual(["logs/online.log", "logs/server.out", "MANIFEST.txt"],
                         zf.namelist())
        self.assertEqual(b"x" * 50000, zf.read("logs/server.out"))
        # 不能 seek 的流 ⇒ 每个成员都带 data descriptor（大小写在数据后面）。
        self.assertTrue(all(info.flag_bits & 0x08 for info in zf.infolist()))
        manifest = zf.read("MANIFEST.txt").decode("utf-8")
        self.assertIn("服务端日志（全量）", manifest)
        self.assertIn("服务器版本: V0.3.3", manifest)
        self.assertIn("下载者: admin", manifest)
        self.assertIn("logs/server.out\t50000\t", manifest)
        self.assertNotIn("跳过", manifest)

    def test_compressed_members_are_stored_and_logs_are_deflated(self):
        self.crash_dir("alice_a1b2c3d4_20260909-013642",
                       files=(("crash.zip", 40), ("receipt.json", 40)))
        sink = _Sink()
        self.packer.write_zip(sink, self.packer.plan("client_crash"))
        zf = sink.zip()
        self.assertEqual(zipfile.ZIP_STORED,
                         zf.getinfo("logs_client_crash/alice_a1b2c3d4_20260909-013642/crash.zip")
                         .compress_type)
        self.assertEqual(zipfile.ZIP_DEFLATED,
                         zf.getinfo("logs_client_crash/alice_a1b2c3d4_20260909-013642/receipt.json")
                         .compress_type)
        self.assertEqual(zipfile.ZIP_DEFLATED, zf.getinfo("MANIFEST.txt").compress_type)

    def test_a_file_deleted_between_listing_and_packing_is_skipped_and_recorded(self):
        self.log("server.out", size=5)
        gone = self.log("server-20260901.out", size=5)
        plan = self.packer.plan("server")
        os.remove(gone)                                 # 清理线程刚好删了它
        sink = _Sink()
        stats = self.packer.write_zip(sink, plan)
        self.assertEqual(1, stats["files"])
        self.assertEqual(["logs/server-20260901.out"], [a for a, _why in stats["skipped"]])
        zf = sink.zip()
        self.assertIsNone(zf.testzip())
        self.assertEqual(["logs/server.out", "MANIFEST.txt"], zf.namelist())
        manifest = zf.read("MANIFEST.txt").decode("utf-8")
        self.assertIn("跳过: 1 个", manifest)
        self.assertIn("logs/server-20260901.out —— ", manifest)

    def test_a_broken_sink_raises_the_sinks_own_error_and_is_not_touched_again(self):
        # 浏览器取消下载 ⇒ sendall 抛 ConnectionResetError。要原样抛出去（审计日志
        # 要写是哪一种断法），而且之后**一个字节都不再写**（close() 会再抛一次）。
        self.log("server.out", size=600000)
        self.log("online.log", size=10)
        sink = _BrokenSink(fail_at=3)
        with self.assertRaises(ConnectionResetError):
            self.packer.write_zip(sink, self.packer.plan("server"))
        self.assertEqual(3, sink.calls)

    def test_a_sink_that_forgets_to_return_a_length_fails_loudly(self):
        class Forgetful(_Sink):
            def write(self, data):
                super().write(data)         # 返回 None

        self.log("server.out")
        with self.assertRaises(TypeError):
            self.packer.write_zip(Forgetful(), self.packer.plan("server"))

    def test_a_member_that_breaks_mid_copy_aborts_the_whole_zip(self):
        # 拷到一半读不下去（磁盘坏块那种）：半个成员已经进流，不能当「跳过」。
        self.log("server.out", size=100)
        self.log("online.log", size=100)
        plan = self.packer.plan("server")
        real_write = zipfile.ZipFile.write

        def poisoned(zf, filename, arcname=None, *args, **kwargs):
            if arcname == "logs/server.out":
                zf.writestr(zipfile.ZipInfo("logs/server.out"), b"half")   # 先漏半截进流
                raise OSError(5, "读盘出错")
            return real_write(zf, filename, arcname, *args, **kwargs)

        zipfile.ZipFile.write = poisoned
        self.addCleanup(setattr, zipfile.ZipFile, "write", real_write)
        with self.assertRaises(logpack.PackAborted) as caught:
            self.packer.write_zip(_Sink(), plan)
        self.assertIn("logs/server.out", str(caught.exception))


# ----------------------------------------------------------------- 7z（X17）
class _FailingFile(io.BytesIO):
    """写到第 `fail_at` 次就抛 ENOSPC（模拟临时文件那块盘满了）。"""

    def __init__(self, fail_at):
        super().__init__()
        self.fail_at = fail_at
        self.calls = 0

    def write(self, data):
        self.calls += 1
        if self.calls >= self.fail_at:
            raise OSError(28, "No space left on device")
        return super().write(data)


@unittest.skipUnless(sevenzip.AVAILABLE, "这个 Python 没带 lzma")
class Write7zTests(_Case):
    TEXT = "".join("[2026-09-30 10:00:00.%03d] [app] 服务端日志 %d\n" % (i % 1000, i)
                   for i in range(3000)).encode("utf-8")

    def write_log(self, name, data, hours_ago=0.0):
        path = self.log(name, hours_ago=hours_ago)
        with open(path, "wb") as fp:
            fp.write(data)
        when = self.now - hours_ago * HOUR
        os.utime(path, (when, when))
        return path

    def pack(self, plan, level=sevenzip.HIGH, **kwargs):
        buf = io.BytesIO()
        stats = self.packer.write_7z(buf, plan, level, **kwargs)
        self.assertEqual(len(buf.getvalue()), stats["size"])
        return stats, read_7z(buf.getvalue())

    def test_a_solid_7z_with_every_log_and_a_manifest(self):
        self.write_log("server.out", self.TEXT)
        self.write_log("online.log", b"online\n" * 10)
        self.write_log("server-boot.err", b"")                  # 空文件：进包、是文件
        stats, archive = self.pack(self.packer.plan("server"),
                                   meta={"version": "V0.5.0", "by": "admin"})
        self.assertEqual(3, stats["files"])
        self.assertEqual(len(self.TEXT) + 70, stats["bytes"])
        self.assertEqual([], stats["skipped"])
        self.assertEqual(["logs/online.log", "logs/server-boot.err", "logs/server.out",
                          "MANIFEST.txt"], archive.names)
        self.assertEqual(self.TEXT, archive.read("logs/server.out"))
        empty = archive.entry("logs/server-boot.err")
        self.assertFalse(empty.is_dir)
        self.assertEqual(b"", empty.data)
        self.assertEqual(1, len(archive.folders))               # 固实：一个 LZMA2 流
        self.assertEqual("lzma2", archive.entry("logs/server.out").method)
        manifest = archive.read("MANIFEST.txt").decode("utf-8")
        self.assertIn("服务端日志（全量）", manifest)
        self.assertIn("压缩者: admin", manifest)
        self.assertIn("压缩等级: 高（LZMA2，字典 16 MB，≈ 7-Zip「标准压缩」）", manifest)
        self.assertIn("7z 内路径", manifest)
        self.assertIn("logs/server.out\t%d\t" % len(self.TEXT), manifest)
        self.assertNotIn("跳过", manifest)

    def test_the_level_decides_the_dictionary(self):
        """弹窗选的档（D102）一路传到 7z 头里的字典码：高 16 MiB（`0x18`）、低 1 MiB（`0x10`）。"""
        self.write_log("server.out", self.TEXT)
        for level, prop, line in ((sevenzip.HIGH, b"\x18", "压缩等级: 高（LZMA2，字典 16 MB"),
                                  (sevenzip.LOW, b"\x10", "压缩等级: 低（LZMA2，字典 1 MB")):
            _stats, archive = self.pack(self.packer.plan("server"), level)
            self.assertEqual([prop], [folder[1] for folder in archive.folders], level)
            self.assertIn(line, archive.read("MANIFEST.txt").decode("utf-8"))
            self.assertEqual(self.TEXT, archive.read("logs/server.out"))

    def test_an_unknown_level_writes_nothing(self):
        self.write_log("server.out", self.TEXT)
        buf = io.BytesIO()
        with self.assertRaises(ValueError):
            self.packer.write_7z(buf, self.packer.plan("server"), "max")
        self.assertEqual(b"", buf.getvalue())

    def test_the_zip_manifest_has_no_level_line(self):
        # ★ zip 那一路一个字节不变（D101）：压缩等级只是 7z 的事。
        self.write_log("server.out", b"x")
        text = logpack.manifest_text(self.packer.plan("server"), [], [], {})
        self.assertNotIn("压缩等级", text)

    def test_the_mtime_survives(self):
        self.write_log("server.out", b"x" * 10, hours_ago=3)
        _stats, archive = self.pack(self.packer.plan("server"))
        mtime = archive.entry("logs/server.out").mtime_ns / 1e9
        self.assertAlmostEqual(self.now - 3 * HOUR, mtime, delta=2)

    def test_crash_packages_are_stored_first_and_the_rest_compressed(self):
        self.crash_dir("alice_a1b2c3d4_20260909-013642",
                       files=(("alice_a1b2c3d4_20260909-013642.7z", 400), ("receipt.json", 40)))
        self.crash_dir("bob_deadbeef_20260910-010203",
                       files=(("bob_deadbeef_20260910-010203.zip", 300), ("receipt.json", 40)))
        _stats, archive = self.pack(self.packer.plan("client_crash"))
        base = "logs_client_crash/"
        self.assertEqual("copy", archive.entry(
            base + "alice_a1b2c3d4_20260909-013642/alice_a1b2c3d4_20260909-013642.7z").method)
        self.assertEqual("copy", archive.entry(
            base + "bob_deadbeef_20260910-010203/bob_deadbeef_20260910-010203.zip").method)
        self.assertEqual("lzma2", archive.entry(
            base + "bob_deadbeef_20260910-010203/receipt.json").method)
        self.assertEqual(["copy", "lzma2"], [folder[0] for folder in archive.folders])
        # 清单照 plan 的顺序列（不是压缩的先后）。
        manifest = archive.read("MANIFEST.txt").decode("utf-8")
        self.assertLess(manifest.index("alice_a1b2c3d4_20260909-013642/receipt.json"),
                        manifest.index("bob_deadbeef_20260910-010203/bob_deadbeef"))

    def test_a_file_deleted_after_the_plan_is_skipped_and_recorded(self):
        self.write_log("server.out", b"keep")
        gone = self.write_log("server-20260901.out", b"gone")
        plan = self.packer.plan("server")
        os.remove(gone)
        stats, archive = self.pack(plan)
        self.assertEqual(1, stats["files"])
        self.assertEqual(["logs/server-20260901.out"], [name for name, _why in stats["skipped"]])
        self.assertEqual(["logs/server.out", "MANIFEST.txt"], archive.names)
        manifest = archive.read("MANIFEST.txt").decode("utf-8")
        self.assertIn("跳过: 1 个", manifest)
        self.assertIn("logs/server-20260901.out —— ", manifest)

    def test_a_file_that_grew_after_the_plan_is_packed_whole(self):
        path = self.write_log("server.out", b"a" * 10)
        plan = self.packer.plan("server")
        with open(path, "ab") as fp:
            fp.write(b"b" * 20)
        _stats, archive = self.pack(plan)
        self.assertEqual(b"a" * 10 + b"b" * 20, archive.read("logs/server.out"))

    def test_a_file_truncated_to_nothing_becomes_an_empty_file(self):
        path = self.write_log("server.out", b"a" * 10)
        plan = self.packer.plan("server")
        open(path, "wb").close()
        _stats, archive = self.pack(plan)
        self.assertFalse(archive.entry("logs/server.out").is_dir)
        self.assertEqual(b"", archive.read("logs/server.out"))

    def test_more_than_a_read_chunk_and_the_dictionary(self):
        # 比高档的字典（16 MiB）还长、跨几十块 READ_CHUNK（本机 ~2 秒）。
        big = self.TEXT * (sevenzip.LEVELS[sevenzip.HIGH].dict_size // len(self.TEXT) + 2)
        self.write_log("server.out", big)
        seen = []
        stats, archive = self.pack(self.packer.plan("server"), progress=seen.append)
        self.assertEqual(big, archive.read("logs/server.out"))
        self.assertEqual(len(big), seen[-1])
        self.assertEqual(sorted(seen), seen)                    # 只增不减
        self.assertGreater(len(seen), len(big) // logpack.READ_CHUNK)
        self.assertLess(stats["size"], len(big) // 5)           # 日志至少压到 1/5

    def test_a_read_error_mid_file_aborts_the_whole_7z(self):
        self.write_log("server.out", self.TEXT)
        plan = self.packer.plan("server")
        real_open = open

        class Broken:
            def __init__(self, fp):
                self.fp = fp
                self.reads = 0

            def read(self, size):
                self.reads += 1
                if self.reads > 1:
                    raise OSError(5, "读盘出错")
                return self.fp.read(size)

            def fileno(self):
                return self.fp.fileno()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.fp.close()

        logpack.open = lambda path, mode="r": Broken(real_open(path, mode))
        self.addCleanup(delattr, logpack, "open")
        with self.assertRaises(logpack.PackAborted) as caught:
            self.packer.write_7z(io.BytesIO(), plan, sevenzip.HIGH)
        self.assertIn("logs/server.out", str(caught.exception))
        self.assertIn("读到一半", str(caught.exception))

    def test_a_full_disk_is_reported_as_our_side(self):
        self.write_log("server.out", self.TEXT)
        with self.assertRaises(logpack.PackAborted) as caught:
            self.packer.write_7z(_FailingFile(fail_at=2), self.packer.plan("server"),
                                 sevenzip.HIGH)
        self.assertIn("临时文件写不进去", str(caught.exception))

    def test_a_name_7z_cannot_hold_is_skipped(self):
        self.write_log("server.out", b"fine")
        plan = self.packer.plan("server")
        plan.entries.append((plan.entries[0][0], "logs/../escape"))
        stats, archive = self.pack(plan)
        self.assertEqual(["logs/server.out", "MANIFEST.txt"], archive.names)
        self.assertEqual("logs/../escape", stats["skipped"][0][0])

    def test_without_lzma_it_says_so(self):
        self.write_log("server.out", b"x")
        real = sevenzip.AVAILABLE
        sevenzip.AVAILABLE = False
        self.addCleanup(setattr, sevenzip, "AVAILABLE", real)
        with self.assertRaises(logpack.SevenZipUnavailable) as caught:
            self.packer.write_7z(io.BytesIO(), self.packer.plan("server"), sevenzip.HIGH)
        self.assertEqual(503, caught.exception.status)

    def test_the_stem_is_shared_by_zip_and_7z(self):
        self.write_log("server.out", b"x")
        self.now = time.mktime((2026, 9, 17, 21, 30, 45, 0, 0, -1))
        plan = self.packer.plan("server", "recent")
        self.assertEqual("logs_server_12h_20260917-213045", plan.stem)
        self.assertEqual(plan.stem + ".zip", plan.filename)


if __name__ == "__main__":
    unittest.main()
