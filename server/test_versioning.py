#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""版本号解析 / 线上编码 / 最低版本门禁的测试（versioning.py）。

重点盯三类事：

* **手改文件必须能吃下**：两个 config 都是给人用记事本改的 —— 前后空格、
  CRLF、UTF-8 BOM、甚至另存成 UTF-16、v/V 前缀大小写，认不出才叫 bug。
* **编码的唯一性**：线上只有 4 个字节，编不出来 / 编出来撞原版 311 的
  版本号必须当场报错，不许带上服务器。
* **门禁 fail-open**：配置写错最坏也只是「不限制」，绝不能让服务端起不来
  或把所有人挡在外面（server.config 的同一套哲学）。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gameserver                                                  # noqa: E402
import versioning                                                  # noqa: E402


class ParseTests(unittest.TestCase):
    def test_plain_and_prefixed(self):
        self.assertEqual(versioning.parse_version_text("0.2.7"), (0, 2, 7))
        self.assertEqual(versioning.parse_version_text("v0.2.7"), (0, 2, 7))
        self.assertEqual(versioning.parse_version_text("V5.12.23"), (5, 12, 23))
        self.assertEqual(versioning.parse_version_text("1.20.1"), (1, 20, 1))

    def test_whitespace_and_crlf(self):
        self.assertEqual(versioning.parse_version_text("  0.2.7\r\n"), (0, 2, 7))
        self.assertEqual(versioning.parse_version_text("\tV0.2.7 \n"), (0, 2, 7))

    def test_short_and_zero_forms(self):
        # 段数不足补 0；单独一个 0 = 明确的「不限制」
        self.assertEqual(versioning.parse_version_text("0.2"), (0, 2, 0))
        self.assertEqual(versioning.parse_version_text("7"), (7, 0, 0))
        self.assertEqual(versioning.parse_version_text("0"), (0, 0, 0))

    def test_comment_lines_are_skipped(self):
        # 文件里手写几行注释再写版本号，要能取到版本号那一行
        self.assertEqual(versioning.parse_version_text("# 说明\n; 也认分号\n0.2.7\n"),
                         (0, 2, 7))

    def test_garbage_returns_none(self):
        for bad in ("", "abc", "0.2.x", "0..7", "v", "1.2.3.4", "-1.2.3",
                    "1.2.3 修订", "null", "0.2.1000", "2147.0.0", None):
            self.assertIsNone(versioning.parse_version_text(bad), bad)

    def test_read_version_file_boms(self):
        # 记事本「另存为」的四种常见编码都要能读
        with tempfile.TemporaryDirectory() as tmp:
            cases = {
                "utf8.txt": "0.2.7".encode("utf-8"),
                "utf8bom.txt": b"\xef\xbb\xbf" + "0.2.7".encode("utf-8"),
                "utf16le.txt": "0.2.7".encode("utf-16"),       # 自带 FF FE
                "utf16be.txt": b"\xfe\xff" + "0.2.7".encode("utf-16-be"),
                "crlf.txt": "v0.2.7\r\n".encode("utf-8"),
                "comments.txt": "# 最低允许的客户端版本\n0.2.7\n".encode("utf-8"),
            }
            for name, data in cases.items():
                path = os.path.join(tmp, name)
                with open(path, "wb") as f:
                    f.write(data)
                self.assertEqual(versioning.read_version_file(path), (0, 2, 7),
                                 name)
            # 认不出的内容 / 不存在的文件 -> None，不抛异常
            bad = os.path.join(tmp, "bad.txt")
            with open(bad, "wb") as f:
                f.write("不是版本号".encode("utf-8"))
            self.assertIsNone(versioning.read_version_file(bad))
            self.assertIsNone(versioning.read_version_file(os.path.join(tmp, "nope")))


class WireTests(unittest.TestCase):
    def test_roundtrip(self):
        for version in ((0, 2, 7), (0, 1, 0), (5, 12, 23), (1, 20, 1),
                        (2146, 999, 999)):
            self.assertEqual(versioning.decode_wire(versioning.encode_wire(version)),
                             version, str(version))

    def test_encode_examples(self):
        self.assertEqual(versioning.encode_wire((0, 2, 7)), 2007)
        self.assertEqual(versioning.encode_wire((5, 12, 23)), 5_012_023)

    def test_reserved_and_overflow_rejected(self):
        with self.assertRaises(ValueError):
            versioning.encode_wire((0, 0, 311))     # == 原版 311
        with self.assertRaises(ValueError):
            versioning.encode_wire((0, 0, 999))     # 落在原版小数字区间
        with self.assertRaises(ValueError):
            versioning.encode_wire((2147, 0, 0))    # int32 装不下
        with self.assertRaises(ValueError):
            versioning.encode_wire((0, 1000, 0))
        with self.assertRaises(ValueError):
            versioning.encode_wire(None)

    def test_decode_legacy_and_garbage(self):
        self.assertIsNone(versioning.decode_wire(311))    # 原版 = 旧版
        # 310/312 这些是「原版客户端的其他小版本」，不在我们的编码区间里
        #（编码下限 1000），同样按「没上报复活版本的旧版」处理
        self.assertIsNone(versioning.decode_wire(312))
        self.assertIsNone(versioning.decode_wire(-1))
        self.assertIsNone(versioning.decode_wire(2_147_000_000))
        self.assertIsNone(versioning.decode_wire("abc"))

    def test_legacy_wire_matches_gameserver_constant(self):
        # 两个模块各有一份 311 是刻意的（gameserver 管协议考据，versioning
        # 管版本语义），但值必须一致 —— 这条测试盯着。
        self.assertEqual(versioning.LEGACY_WIRE_VERSION,
                         gameserver.CLIENT_VERSION)

    def test_format(self):
        self.assertEqual(versioning.format_version((0, 2, 7)), "V0.2.7")
        self.assertEqual(versioning.format_version(None), "?")


class ClientFilterTests(unittest.TestCase):
    def _write(self, tmp, text, name="server-ClientFilter.config"):
        path = os.path.join(tmp, name)
        with open(path, "wb") as f:
            f.write(text.encode("utf-8"))
        return path

    def test_missing_file_means_no_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "absent.config")
            version, warnings = versioning.load_client_filter(path)
            self.assertIsNone(version)
            self.assertTrue(warnings)

    def test_zero_means_no_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "0\r\n")
            version, warnings = versioning.load_client_filter(path)
            self.assertIsNone(version)
            self.assertEqual(warnings, [])

    def test_version_is_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            for text in ("0.2.7", "v0.2.7", " V0.2.7 \r\n"):
                path = self._write(tmp, text)
                version, warnings = versioning.load_client_filter(path, _reload=True)
                self.assertEqual(version, (0, 2, 7), text)
                self.assertEqual(warnings, [], text)

    def test_garbage_fails_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "明天再改\r\n")
            version, warnings = versioning.load_client_filter(path)
            self.assertIsNone(version)               # 认不出 -> 不限制
            self.assertTrue(warnings)                # 但必须在日志里喊出来

    def test_hot_reload(self):
        # 改完配置不用重启：下一次 load_client_filter 就按新值判
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "0.2.7")
            self.assertEqual(versioning.load_client_filter(path)[0], (0, 2, 7))
            # os.replace 保住 mtime 变化；连续两次写在 Windows 上可能落进
            # 同一个时间戳 tick，这里显式拉开 10 秒，模拟「人隔几秒改配置」
            self._write(tmp, "0.3.0", name="staged.config")
            os.replace(os.path.join(tmp, "staged.config"), path)
            st = os.stat(path)
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000_000))
            self.assertEqual(versioning.load_client_filter(path)[0], (0, 3, 0))
            # 删掉文件 -> 回到「不限制」
            os.remove(path)
            version, warnings = versioning.load_client_filter(path)
            self.assertIsNone(version)
            self.assertTrue(warnings)


if __name__ == "__main__":
    unittest.main()
