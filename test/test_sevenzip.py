#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小 7z 写入器（`server/sevenzip.py`，X17 / D101 / §142）。

三层核对，防的是「写入器和读取器都是自己写的，互相对得上、一起错」（test/README.md 里 pkn 的教训）：

1. `testsupport.read_7z` 是**严格**读取器：字段顺序、记录长度、每个 CRC 都查。
   它自己先拿**真 7-Zip 打的夹具**（`test/data/sevenzip/`）核一遍（`FixtureTests`）。
2. 金样头部字节（`GoldenHeaderTests`）：照 7zFormat.txt 手推的一串字节，会话 58 用 7-Zip 19.00 `t` 核过。
3. 本机有 7-Zip 就真跑 `7z t` / `x` / `l -slt`（`RealSevenZipTests`，找不到就跳过）。
"""
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
import zlib
from unittest import mock

import sevenzip
import testsupport
from testsupport import read_7z

try:
    import lzma
except ImportError:
    lzma = None

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.join(os.path.dirname(HERE), "server")
FIXTURE_DIR = os.path.join(HERE, "data", "sevenzip")

#: 子进程（真 7-Zip / 没有 lzma 的 Python）的**保险丝**（秒）。★ 不是判据：判据是
#: 子进程自己退出、退出码和输出；这个数只防它真挂住时测试永远不回来。
SUBPROCESS_FUSE_S = 120.0

#: 找 7-Zip 的地方（和 `tools/build-common.ps1` 打包时那张表一样）。
SEVEN_ZIP_CANDIDATES = (
    r"C:\SSD\Program\7-Zip\7z.exe",
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
)


def find_7zip():
    for name in ("7z", "7zz", "7za"):
        found = shutil.which(name)
        if found:
            return found
    for path in SEVEN_ZIP_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


SEVEN_ZIP = find_7zip()

TEXT = "".join("[2026-09-30 10:00:%02d.%03d] [app] 服务端日志 第 %d 行\n" % (i % 60, i % 1000, i)
               for i in range(4000)).encode("utf-8")
MTIME_NS = 1758000000123456700          # 100 ns 整数倍：FILETIME 存得下，往返不丢


def build(members, solid=True, chunk=65536, level=sevenzip.HIGH):
    """`members` = `[(名字, 数据, 压不压), ...]` → 7z 字节。"""
    buf = io.BytesIO()
    writer = sevenzip.Writer(buf, level=level, solid=solid)
    for name, data, compress in members:
        member = writer.begin(name, compress)
        for start in range(0, len(data), chunk):
            writer.write(member, data[start:start + chunk])
        writer.end(member, MTIME_NS)
    size = writer.close()
    blob = buf.getvalue()
    assert size == len(blob), (size, len(blob))
    return blob


def crc(data):
    return struct.pack("<I", zlib.crc32(data))


def num(value):
    return sevenzip.encode_number(value)


# ----------------------------------------------------------------- 编码
class NumberTests(unittest.TestCase):
    VECTORS = (
        (0, "00"), (0x7F, "7f"), (0x80, "80 80"), (0x3FFF, "bf ff"),
        (0x4000, "c0 00 40"), (0x200000, "e0 00 00 20"),
        (1 << 56, "ff 00 00 00 00 00 00 00 01"),
        ((1 << 64) - 1, "ff ff ff ff ff ff ff ff ff"),
    )

    def test_known_vectors(self):
        for value, text in self.VECTORS:
            encoded = sevenzip.encode_number(value)
            self.assertEqual(bytes.fromhex(text), encoded, hex(value))
            self.assertEqual(value, testsupport._Cursor(encoded).number(), hex(value))

    def test_every_width_round_trips(self):
        for bits in range(64):
            for value in ((1 << bits) - 1, 1 << bits):
                encoded = sevenzip.encode_number(value)
                cursor = testsupport._Cursor(encoded)
                self.assertEqual(value, cursor.number(), value)
                cursor.done()

    def test_out_of_range_is_refused(self):
        for value in (-1, 1 << 64):
            with self.assertRaises(ValueError):
                sevenzip.encode_number(value)


class BitsTests(unittest.TestCase):
    def test_high_bit_first_and_padded(self):
        self.assertEqual(b"", sevenzip.encode_bits([]))
        self.assertEqual(b"\xa0", sevenzip.encode_bits([True, False, True]))
        self.assertEqual(b"\x00", sevenzip.encode_bits([False] * 8))
        self.assertEqual(b"\xff\x80", sevenzip.encode_bits([True] * 9))


class Lzma2PropTests(unittest.TestCase):
    TABLE = ((4096, 0), (6144, 1), (1 << 20, 16), (3 << 19, 17), ((1 << 20) + 1, 17),
             (4 << 20, 20), (64 << 20, 28))

    def test_the_formula(self):
        for dict_size, prop in self.TABLE:
            self.assertEqual(prop, sevenzip.lzma2_dict_prop(dict_size), dict_size)

    @unittest.skipIf(lzma is None, "这个 Python 没带 lzma")
    def test_it_matches_liblzma(self):
        # zipfile 用的也是这个私有函数；这里只拿来对账。
        for dict_size, _prop in self.TABLE:
            want = lzma._encode_filter_properties({"id": lzma.FILTER_LZMA2,
                                                   "dict_size": dict_size})
            self.assertEqual(want[0], sevenzip.lzma2_dict_prop(dict_size), dict_size)

    def test_the_lzma2_coder_says_its_own_dictionary(self):
        self.assertEqual(b"\x01\x21\x21\x01\x10",
                         sevenzip._Folder(sevenzip.LZMA2, dict_size=1 << 20).coder_bytes())
        self.assertEqual(b"\x01\x21\x21\x01\x18",
                         sevenzip._Folder(sevenzip.LZMA2, dict_size=16 << 20).coder_bytes())
        self.assertEqual(b"\x01\x01\x00", sevenzip._Folder(sevenzip.COPY).coder_bytes())


@unittest.skipIf(lzma is None, "这个 Python 没带 lzma")
class LevelTests(unittest.TestCase):
    """两档压缩等级（用户 2026-09-30，D102 / §143）。往返测试对档位不敏感 —— 档位被改掉
    照样全绿 ⇒ 这里直接看交给 liblzma 的是什么、头里写的是什么、管理页小字的数对不对。"""

    def spy(self, level):
        seen = []
        real = lzma.LZMACompressor

        def record(**kwargs):
            seen.append(kwargs)
            return real(**kwargs)

        with mock.patch.object(sevenzip.lzma, "LZMACompressor", record):
            blob = build([("a.log", TEXT, True)], level=level)
        return seen, read_7z(blob)

    def test_each_level_hands_liblzma_what_the_table_says(self):
        # 低 = X17 第一版的 -1 档 + 1 MiB；高 = 7-Zip「标准」-mx5（BT4 / normal / 单词 32 = preset 5）+ 16 MiB。
        for level, preset, dict_size, prop in ((sevenzip.LOW, 1, 1 << 20, b"\x10"),
                                              (sevenzip.HIGH, 5, 16 << 20, b"\x18")):
            seen, archive = self.spy(level)
            self.assertEqual([{"format": lzma.FORMAT_RAW, "filters": [
                {"id": lzma.FILTER_LZMA2, "preset": preset, "dict_size": dict_size}]}], seen, level)
            # ★ 头里的字典码和交给编码器的是同一个数（写小了解压端直接报数据错）。
            self.assertEqual([prop], [folder[1] for folder in archive.folders], level)
            self.assertEqual(TEXT, archive.read("a.log"))

    def test_an_unknown_level_is_refused_before_a_byte_is_written(self):
        buf = io.BytesIO()
        for bad in ("max", "", None, "HIGH"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                sevenzip.Writer(buf, level=bad)
        self.assertEqual(b"", buf.getvalue())

    def test_the_memory_estimate_is_what_liblzma_really_allocates(self):
        """管理页小字「压缩时服务器约多占 N MB」照 `Level.memory` 写（§143）—— 拿真分配量核，差 10% 以内。
        ★ `_lzma` 走 `PyMem_RawMalloc` ⇒ tracemalloc 看得见 liblzma 的那几大块。"""
        for level, spec in sevenzip.LEVELS.items():
            writer = sevenzip.Writer(io.BytesIO(), level=level)
            member = writer.begin("a.log")
            tracemalloc.start()
            try:
                writer.write(member, b"x")                  # 第一块数据才建编码器
                _now, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            writer.end(member, MTIME_NS)
            writer.close()                                  # 编码器收尾、还内存
            self.assertLess(abs(peak - spec.memory), spec.memory // 10,
                            "%s 档实际分配 %.1f MB，表里写的 %.1f MB"
                            % (level, peak / 1048576.0, spec.memory / 1048576.0))


class NameTests(unittest.TestCase):
    def test_good_names_become_utf16(self):
        for name in ("a", "logs/server.out", "中文/名字.txt", "emoji/\U0001F600.txt"):
            self.assertEqual(name.encode("utf-16-le"), sevenzip.check_name(name))

    def test_bad_names_are_refused(self):
        for name in ("", "/abs", "a\\b", "a\0b", "a//b", "a/./b", "../x", "x/..", "x/",
                     "bad\ud800", None):
            with self.assertRaises(ValueError, msg=repr(name)):
                sevenzip.check_name(name)

    def test_a_refused_name_writes_nothing(self):
        buf = io.BytesIO()
        writer = sevenzip.Writer(buf)
        before = writer.tell()
        with self.assertRaises(ValueError):
            writer.begin("../escape")
        self.assertEqual(before, writer.tell())
        writer.add_bytes("ok.txt", b"fine", MTIME_NS, compress=False)     # 之后照常能写
        writer.close()
        self.assertEqual(["ok.txt"], read_7z(buf.getvalue()).names)


# ----------------------------------------------------------------- 金样
class GoldenHeaderTests(unittest.TestCase):
    def test_a_copy_only_archive_has_exactly_these_header_bytes(self):
        """照 7zFormat.txt 手推（会话 58 用 7-Zip 19.00 `7z t` 核过这个包）：
        Copy folder 一个；`a` = "hi"、`e` 是空文件；两个 mtime 都是 FILETIME 0x019DB1DED53E8000。"""
        filetime = 0x019DB1DED53E8000
        mtime_ns = (filetime - 116444736000000000) * 100
        buf = io.BytesIO()
        writer = sevenzip.Writer(buf)
        writer.add_bytes("a", b"hi", mtime_ns, compress=False)
        writer.add_bytes("e", b"", mtime_ns, compress=False)
        writer.close()
        blob = buf.getvalue()
        self.assertEqual(bytes.fromhex(
            "01 04 06 00 01 09 02 00 07 0B 01 00 01 01 00 0C 02 00 08 0A 01 AC 2A 93 D8 00 00"
            "05 02 0E 01 40 0F 01 80 11 09 00 61 00 00 00 65 00 00 00"
            "14 12 01 00 00 80 3E D5 DE B1 9D 01 00 80 3E D5 DE B1 9D 01 00 00"),
            read_7z(blob).header)
        self.assertEqual(sevenzip.SIGNATURE + b"\x00\x04", blob[:8])
        self.assertEqual(b"hi", blob[32:34])                    # 数据区紧跟签名头

    def test_an_empty_archive_is_just_the_signature_header(self):
        buf = io.BytesIO()
        self.assertEqual(32, sevenzip.Writer(buf).close())
        self.assertEqual(bytes(20), buf.getvalue()[12:])        # 偏移 / 长度 / CRC 全 0
        self.assertEqual([], read_7z(buf.getvalue()).entries)


# ----------------------------------------------------------------- 往返
@unittest.skipIf(lzma is None, "这个 Python 没带 lzma")
class RoundTripTests(unittest.TestCase):
    def check(self, members, solid=True):
        blob = build(members, solid)
        archive = read_7z(blob)
        self.assertEqual([name for name, _d, _c in members], archive.names)
        for name, data, compress in members:
            entry = archive.entry(name)
            self.assertEqual(data, entry.data, name)
            self.assertFalse(entry.is_dir, name)
            if data:
                self.assertEqual("lzma2" if compress else "copy", entry.method, name)
            else:
                self.assertIsNone(entry.method, name)
            self.assertEqual(MTIME_NS, entry.mtime_ns, name)
        return archive

    def test_solid_lzma2_puts_three_files_in_one_folder(self):
        manifest = "清单\n".encode("utf-8")
        archive = self.check([("logs/a.log", TEXT, True), ("logs/b.log", TEXT[:5000], True),
                              ("MANIFEST.txt", manifest, True)])
        self.assertEqual(1, len(archive.folders))
        # SubStreamsInfo：08 0D 03 09 <a> <b> 0A 01 <crc×3> 00，再跟 StreamsInfo 的 00。
        # ★ 0D → 09 → 0A 的顺序是 libarchive 要的（§142）。
        self.assertIn(bytes([0x08, 0x0D, 0x03, 0x09]) + num(len(TEXT)) + num(5000)
                      + bytes([0x0A, 0x01]) + crc(TEXT) + crc(TEXT[:5000]) + crc(manifest)
                      + bytes([0x00, 0x00]), archive.header)

    def test_one_copy_and_one_lzma2(self):
        blob = os.urandom(3000)
        archive = self.check([("x/crash.zip", blob, False), ("MANIFEST.txt", b"m", True)])
        self.assertEqual(["copy", "lzma2"], [folder[0] for folder in archive.folders])
        # 两个 folder 各一个子流：没有 0D、没有 09。
        self.assertIn(bytes([0x08, 0x0A, 0x01]) + crc(blob) + crc(b"m") + bytes([0x00, 0x00]),
                      archive.header)

    def test_two_copies_and_one_lzma2(self):
        first, second = os.urandom(300), os.urandom(100)
        archive = self.check([("x/a.zip", first, False), ("x/b.zip", second, False),
                              ("MANIFEST.txt", b"m", True)])
        self.assertIn(bytes([0x08, 0x0D, 0x02, 0x01, 0x09]) + num(300) + bytes([0x0A, 0x01])
                      + crc(first) + crc(second) + crc(b"m") + bytes([0x00, 0x00]),
                      archive.header)

    def test_one_copy_and_two_lzma2(self):
        blob = os.urandom(300)
        archive = self.check([("x/a.zip", blob, False), ("x/receipt.json", b"{}", True),
                              ("MANIFEST.txt", b"m", True)])
        self.assertIn(bytes([0x08, 0x0D, 0x01, 0x02, 0x09]) + num(2) + bytes([0x0A, 0x01])
                      + crc(blob) + crc(b"{}") + crc(b"m") + bytes([0x00, 0x00]),
                      archive.header)

    def test_empty_files_anywhere_come_back_as_files(self):
        # ★ 空流要带 kEmptyFile，否则 7-Zip / libarchive 都解成文件夹（§142）。
        self.check([("logs/empty1.err", b"", True), ("logs/mid.log", TEXT[:1000], True),
                    ("logs/empty2.err", b"", True), ("logs/empty.zip", b"", False),
                    ("MANIFEST.txt", b"m", True), ("zz_last.err", b"", True)])

    def test_only_empty_files_means_no_streams_at_all(self):
        archive = self.check([("a.err", b"", True), ("b/c.err", b"", False)])
        self.assertEqual([], archive.folders)
        self.assertEqual(0x05, archive.header[1])               # kHeader 后面直接是 FilesInfo

    def test_chinese_and_non_bmp_names(self):
        self.check([("logs_client_crash/张三_a1b2c3d4_20260909-013642/说明.txt",
                     "中文内容\n".encode("utf-8") * 30, True),
                    ("emoji/\U0001F600.txt", b"smile", True)])

    def test_more_than_the_dictionary_in_odd_sized_chunks(self):
        # 比高档的字典（16 MiB）还长（本机 ~2 秒）。
        big = TEXT * (sevenzip.LEVELS[sevenzip.HIGH].dict_size // len(TEXT) + 2)
        blob = build([("big.log", big, True)], chunk=100001)
        self.assertEqual(big, read_7z(blob).read("big.log"))

    def test_switching_method_in_solid_mode_opens_a_new_folder(self):
        archive = self.check([("a.log", TEXT[:3000], True), ("b.zip", os.urandom(500), False),
                              ("c.log", TEXT[:4000], True)])
        self.assertEqual(["lzma2", "copy", "lzma2"], [folder[0] for folder in archive.folders])

    def test_non_solid_gives_every_member_its_own_folder(self):
        archive = self.check([("Dump/x.mdmp", TEXT * 2, True), ("logs/relay.err", b"", True),
                              ("a.zip", os.urandom(700), False), ("meta.json", b"{}", True)],
                             solid=False)
        self.assertEqual(["lzma2", "copy", "lzma2"], [folder[0] for folder in archive.folders])

    def test_non_solid_tell_is_exact_after_each_member(self):
        buf = io.BytesIO()
        writer = sevenzip.Writer(buf, solid=False)
        told = [writer.tell()]
        for name, data in (("a.log", TEXT), ("b.log", TEXT[:777]), ("c.bin", os.urandom(999))):
            writer.add_bytes(name, data, MTIME_NS, compress=not name.endswith(".bin"))
            told.append(writer.tell())
        writer.close()
        archive = read_7z(buf.getvalue())
        packed = [folder[2] for folder in archive.folders]
        self.assertEqual(32, told[0])
        self.assertEqual([32 + sum(packed[:i + 1]) for i in range(3)], told[1:])

    def test_add_bytes_is_begin_write_end(self):
        buf = io.BytesIO()
        writer = sevenzip.Writer(buf)
        writer.add_bytes("MANIFEST.txt", b"hello", MTIME_NS)
        writer.close()
        self.assertEqual(b"hello", read_7z(buf.getvalue()).read("MANIFEST.txt"))


@unittest.skipIf(lzma is None, "这个 Python 没带 lzma")
class RollbackTests(unittest.TestCase):
    def test_non_solid_rollback_drops_the_member_completely(self):
        buf = io.BytesIO()
        writer = sevenzip.Writer(buf, solid=False)
        writer.add_bytes("keep.log", TEXT[:2000], MTIME_NS)
        before = writer.tell()
        member = writer.begin("broken.log")
        writer.write(member, TEXT)
        writer.rollback(member)                                  # 读到一半坏了
        self.assertEqual(before, writer.tell())
        writer.add_bytes("after.log", b"after", MTIME_NS)
        writer.close()
        archive = read_7z(buf.getvalue())
        self.assertEqual(["keep.log", "after.log"], archive.names)
        self.assertEqual(b"after", archive.read("after.log"))

    def test_rollback_inside_a_shared_copy_folder(self):
        buf = io.BytesIO()
        writer = sevenzip.Writer(buf)
        writer.add_bytes("a.zip", b"A" * 100, MTIME_NS, compress=False)
        member = writer.begin("b.zip", compress=False)
        writer.write(member, b"B" * 50)
        writer.rollback(member)
        writer.add_bytes("c.zip", b"C" * 10, MTIME_NS, compress=False)
        writer.close()
        archive = read_7z(buf.getvalue())
        self.assertEqual(["a.zip", "c.zip"], archive.names)
        self.assertEqual(1, len(archive.folders))
        self.assertEqual(b"C" * 10, archive.read("c.zip"))

    def test_rollback_right_after_switching_keeps_the_previous_stream_tail(self):
        # 起点要在「换 folder、把上一个 LZMA2 流的尾巴 flush 出来」之后才记。
        buf = io.BytesIO()
        writer = sevenzip.Writer(buf)
        writer.add_bytes("a.log", TEXT, MTIME_NS)
        member = writer.begin("b.zip", compress=False)
        writer.write(member, b"half")
        writer.rollback(member)
        writer.close()
        self.assertEqual(TEXT, read_7z(buf.getvalue()).read("a.log"))

    def test_solid_lzma2_cannot_roll_back(self):
        writer = sevenzip.Writer(io.BytesIO())
        member = writer.begin("a.log")
        writer.write(member, TEXT[:100])
        with self.assertRaises(RuntimeError):
            writer.rollback(member)

    def test_an_empty_member_rolls_back_trivially(self):
        buf = io.BytesIO()
        writer = sevenzip.Writer(buf)
        writer.rollback(writer.begin("nothing.log"))
        writer.close()
        self.assertEqual([], read_7z(buf.getvalue()).entries)


class MisuseTests(unittest.TestCase):
    def test_one_member_at_a_time(self):
        writer = sevenzip.Writer(io.BytesIO())
        first = writer.begin("a.zip", compress=False)
        with self.assertRaises(RuntimeError):
            writer.begin("b.zip", compress=False)
        other = sevenzip.Member("x", "x".encode("utf-16-le"), sevenzip.COPY)
        with self.assertRaises(RuntimeError):
            writer.write(other, b"data")
        with self.assertRaises(RuntimeError):
            writer.close()                                       # 还有成员没 end
        writer.end(first, 0)
        with self.assertRaises(RuntimeError):
            writer.end(first, 0)                                 # end 两次

    def test_nothing_after_close(self):
        writer = sevenzip.Writer(io.BytesIO())
        writer.close()
        with self.assertRaises(RuntimeError):
            writer.begin("late.txt")
        with self.assertRaises(RuntimeError):
            writer.close()


# ----------------------------------------------------------------- 独立核对
class FixtureTests(unittest.TestCase):
    """先核**读取器**：真 7-Zip 19.00 打的包（带目录项、属性、kDummy 对齐），它得读对。"""

    def test_the_reader_reads_what_real_7zip_wrote(self):
        with open(os.path.join(FIXTURE_DIR, "expected.json"), encoding="utf-8") as fp:
            expected = json.load(fp)
        path = os.path.join(FIXTURE_DIR, expected["archive"])
        self.assertTrue(os.path.isfile(path),
                        "夹具不在了：%s —— 它守着 read_7z，别删（test/README.md）" % path)
        if lzma is None:
            self.skipTest("这个 Python 没带 lzma")
        with open(path, "rb") as fp:
            archive = read_7z(fp.read())
        got = {entry.name: entry for entry in archive.entries}
        self.assertEqual(sorted(item["name"] for item in expected["entries"]), sorted(got))
        for item in expected["entries"]:
            entry = got[item["name"]]
            self.assertEqual(item["dir"], entry.is_dir, item["name"])
            self.assertEqual(item["size"], len(entry.data), item["name"])
            self.assertEqual(item["sha256"], hashlib.sha256(entry.data).hexdigest(), item["name"])
            self.assertEqual(item["mtime_ns"], entry.mtime_ns, item["name"])


class NoLzmaTests(unittest.TestCase):
    def test_a_python_without_lzma_still_imports_logpack(self):
        """自己编译、缺 liblzma 的 Python：`app.py` 顶层 import 链不能崩；Copy 照写，LZMA2 才报错。"""
        code = "\n".join((
            "import io, sys",
            "sys.modules['lzma'] = None",
            "sys.path.insert(0, %r)" % SERVER_DIR,
            "import sevenzip, logpack",
            "assert sevenzip.AVAILABLE is False",
            "w = sevenzip.Writer(io.BytesIO())",
            "w.add_bytes('a.zip', b'x', 0, compress=False)",
            "w.close()",
            "w = sevenzip.Writer(io.BytesIO())",
            "m = w.begin('a.txt')",
            "try:",
            "    w.write(m, b'x')",
            "except RuntimeError:",
            "    print('refused')",
        ))
        result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                timeout=SUBPROCESS_FUSE_S)
        self.assertEqual(0, result.returncode, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(b"refused", result.stdout.strip())


@unittest.skipIf(SEVEN_ZIP is None, "本机没有 7-Zip（PATH 和 build-common.ps1 那几个路径都没找到）")
@unittest.skipIf(lzma is None, "这个 Python 没带 lzma")
class RealSevenZipTests(unittest.TestCase):
    """再核**写入器**：真 7-Zip 校验、解开、逐字节比对。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_7z(self, *args):
        return subprocess.run([SEVEN_ZIP] + list(args), capture_output=True,
                              timeout=SUBPROCESS_FUSE_S)

    def check(self, name, members, solid, level=sevenzip.HIGH):
        path = os.path.join(self.tmp.name, name + ".7z")
        with open(path, "wb") as fp:
            fp.write(build(members, solid, level=level))
        tested = self.run_7z("t", path)
        self.assertEqual(0, tested.returncode, tested.stdout.decode("utf-8", "replace"))
        self.assertIn(b"Everything is Ok", tested.stdout)
        out = os.path.join(self.tmp.name, name + "_x")
        extracted = self.run_7z("x", "-y", "-o" + out, path)
        self.assertEqual(0, extracted.returncode, extracted.stdout.decode("utf-8", "replace"))
        for arcname, data, _compress in members:
            target = os.path.join(out, *arcname.split("/"))
            self.assertTrue(os.path.isfile(target), "%s 没解成文件" % arcname)
            with open(target, "rb") as fp:
                self.assertEqual(data, fp.read(), arcname)
            self.assertEqual(MTIME_NS // 100, os.stat(target).st_mtime_ns // 100, arcname)
        listing = self.run_7z("l", "-slt", "-sccUTF-8", path).stdout.decode("utf-8", "replace")
        return {line.split("=", 1)[1].strip() for line in listing.splitlines()
                if line.startswith("Method = ")}

    def test_a_solid_log_archive(self):
        methods = self.check("solid", [
            ("logs/server.out", TEXT * 3, True), ("logs/server-boot.err", b"", True),
            ("logs_client_crash/张三_a1b2c3d4_20260909-013642/crash.7z", os.urandom(5000), False),
            ("MANIFEST.txt", "清单\n".encode("utf-8"), True)], solid=True)
        self.assertIn("LZMA2:24", methods)                  # 高档：字典 2^24 = 16 MiB
        self.assertIn("Copy", methods)

    def test_a_low_level_log_archive(self):
        methods = self.check("low", [("logs/server.out", TEXT * 3, True),
                                     ("MANIFEST.txt", b"m", True)], solid=True, level=sevenzip.LOW)
        self.assertEqual({"LZMA2:20"}, methods)             # 低档：字典 2^20 = 1 MiB

    def test_a_non_solid_crash_package(self):
        methods = self.check("crash", [
            ("Dump/LastCrashReport.txt", b"report", True), ("Dump/x.mdmp", TEXT * 2, True),
            ("logs/relay.err", b"", True), ("logs/server.out", TEXT, True),
            ("meta.json", b'{"format": 1}', True)], solid=False)
        self.assertIn("LZMA2:24", methods)                  # 崩溃包固定高档（D102）


if __name__ == "__main__":
    unittest.main()
