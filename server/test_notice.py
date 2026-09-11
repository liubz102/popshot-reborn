#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""登录界面那段反倒卖公告：盯着「明文别泄出去」和「两边算法别漂」。

## 为什么值得一个专门的测试文件

公告的全部价值就在于**倒卖者改不掉它**。它有两种改不掉的失效方式，
共同点还是老毛病 —— **症状不是报错，是它悄悄不成立了**：

1. **明文泄出去**：有人图省事在 `bshook.c` 里写一句公告原文当兜底，
   或者把 `hook/notice.zh.txt` 拷进了发布包 —— 于是记事本就能改，
   前面那一整套混淆白做。
2. **两边算法漂了**：`tools/gen_notice_h.py` 的混淆和 `bshook.c` 的
   `notice_decode()` 必须逐位同构。改了一边没改另一边，编译照过、
   游戏照跑，只是公告框里显示一堆乱码 —— 而这只有实机才看得见。

`hook/build.bat` 已经在编译前重新生成、编译后扫一遍 DLL；这里补的是
**提交进仓库的这几个文件之间**的一致性（`hook/ports.h` 一脉相承）。

⚠ 这些用例依赖仓库布局（`hook/`、`tools/`），发布包里没有它们 ——
   而发布包里也没有 `test_*.py`（`tools/build-common.ps1` 会剔掉），所以不冲突。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOLS = os.path.join(ROOT, "tools")

SOURCE = os.path.join(ROOT, "hook", "notice.zh.txt")
HEADER = os.path.join(ROOT, "hook", "notice_blob.h")
BSHOOK = os.path.join(ROOT, "hook", "bshook.c")
GENERATOR = os.path.join(TOOLS, "gen_notice_h.py")


def repo_file(path, binary=False):
    if not os.path.exists(path):
        raise unittest.SkipTest(f"不在源码仓库里（缺 {path}），跳过")
    if binary:
        with open(path, "rb") as f:
            return f.read()
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def load_generator():
    """`import gen_notice_h`。仓库布局不在就跳过整个文件。"""
    if not os.path.exists(GENERATOR):
        raise unittest.SkipTest(f"不在源码仓库里（缺 {GENERATOR}），跳过")
    if TOOLS not in sys.path:
        sys.path.insert(0, TOOLS)
    import gen_notice_h                                        # noqa: E402
    return gen_notice_h


class GeneratedHeaderTests(unittest.TestCase):
    def test_the_header_matches_the_plaintext_source(self):
        """★ `hook/notice_blob.h` 必须和 `hook/notice.zh.txt` 一致。

        红了就是有人改了文案没重新生成 —— 跑一次
        `python tools/gen_notice_h.py` 即可（`hook/build.bat` 也会自己跑）。
        """
        gen = load_generator()
        repo_file(SOURCE)
        _blocks, _html, header, _blob = gen.build()
        have = repo_file(HEADER).replace("\r\n", "\n")
        self.assertEqual(
            have, header,
            "notice_blob.h 和 notice.zh.txt 对不上，"
            "请跑一次 python tools/gen_notice_h.py")

    def test_the_header_is_crlf_like_the_rest_of_the_c_sources(self):
        """生成的 .h 和仓库里别的 .c/.h 一样是 CRLF（同 `hook/ports.h`）。"""
        raw = repo_file(HEADER, binary=True)
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""),
                         "notice_blob.h 里有裸 LF，应该全是 CRLF")

    def test_the_obfuscation_round_trips(self):
        """混淆 -> 解混淆必须原样回来（生成器自己也查，这里再钉一道）。"""
        gen = load_generator()
        for payload in (b"", b"x", b"\x00" * 64,
                        "公告一二三".encode("utf-8"),
                        bytes(range(256)) * 3):
            seed, iv, blob = gen.obfuscate(payload)
            self.assertEqual(gen.deobfuscate(seed, iv, blob), payload)
            self.assertEqual(len(blob), len(payload))

    def test_the_blob_has_no_repeating_structure(self):
        """同一个字在密文里不该重复出现同一个字节 —— 滚动密钥+链接的意义就在这。

        判据用最朴素的一条：把明文里出现最多的那个字节挑出来，它在密文里
        对应的那些位置**不能全都是同一个值**。纯 XOR 固定密钥会当场红。
        """
        gen = load_generator()
        _blocks, html, _header, blob = gen.build()
        data = html.encode("utf-8")
        common = max(set(data), key=data.count)
        cipher_bytes = {blob[i] for i, b in enumerate(data) if b == common}
        self.assertGreater(
            len(cipher_bytes), 1,
            f"明文里出现 {data.count(common)} 次的字节 {common:#04x} "
            f"在密文里只对应 {cipher_bytes} —— 混淆退化成固定密钥了")


class NoPlaintextTests(unittest.TestCase):
    """明文只许待在 `hook/notice.zh.txt` 里，别处一个字都不许有。"""

    def _words(self):
        gen = load_generator()
        blocks, _html, _header, _blob = gen.build()
        words = gen.secret_words(blocks)
        self.assertTrue(words, "从原稿里一个特征词都没抠出来，测试本身失效了")
        return words

    def _assert_clean(self, path, label):
        raw = repo_file(path, binary=True)
        hits = []
        for word in self._words():
            for enc in ("utf-8", "utf-16-le", "gbk"):
                try:
                    needle = word.encode(enc)
                except UnicodeEncodeError:
                    continue
                if needle in raw:
                    hits.append(f"{enc}: {word}")
        self.assertEqual(hits, [], f"{label} 里有公告明文：{hits}")

    def test_the_generated_header_has_no_plaintext(self):
        self._assert_clean(HEADER, "notice_blob.h")

    def test_the_hook_source_has_no_plaintext(self):
        """★ 别在 bshook.c 里写公告原文当兜底 —— 那等于把混淆绕过去了。"""
        self._assert_clean(BSHOOK, "bshook.c")


class DecoderStaysInSyncTests(unittest.TestCase):
    """`bshook.c` 的解码必须和生成器同构，而且只认头文件里的种子。"""

    def test_the_hook_includes_the_generated_header(self):
        self.assertIn('#include "notice_blob.h"', repo_file(BSHOOK))

    def test_the_decoder_uses_the_generated_constants(self):
        """种子/IV/长度只能来自头文件，不许在 C 里另写一份。"""
        text = repo_file(BSHOOK)
        for macro in ("NOTICE_SEED", "NOTICE_IV", "NOTICE_BLOB_LEN",
                      "NOTICE_BLOB"):
            self.assertIn(macro, text, f"bshook.c 没用到 {macro}")
        self.assertNotIn("#define NOTICE_SEED", text,
                         "bshook.c 自己定义了 NOTICE_SEED —— 种子只能来自"
                         " notice_blob.h，否则和生成器一漂就是满屏乱码")

    def test_the_xorshift_constants_match_the_generator(self):
        """xorshift32 的 13/17/5 两边必须一致。

        这三个数漂了的症状是「公告框里一堆乱码」，编译和运行都不会报错，
        只有实机看得见 —— 所以在这里钉死。
        """
        text = repo_file(BSHOOK)
        for expr in ("x ^= x << 13;", "x ^= x >> 17;", "x ^= x << 5;"):
            self.assertIn(expr, text,
                          f"bshook.c 的 notice_decode 少了 `{expr}`，"
                          f"和 tools/gen_notice_h.py 的 _keystream 对不上")

    def test_the_hook_has_an_escape_hatch(self):
        """项目惯例：每组 patch 都要有 `BSHOOK_KEEP_*` 逃生门。"""
        self.assertIn("BSHOOK_KEEP_NOTICE", repo_file(BSHOOK))


class PlaintextStaysOutOfThePackagesTests(unittest.TestCase):
    """原稿和生成物都不许随包发出去 —— 发出去就等于没混淆。"""

    def test_no_packaging_script_copies_the_plaintext(self):
        for name in ("build-portable.ps1", "build-server-package.ps1",
                     "build-common.ps1"):
            path = os.path.join(TOOLS, name)
            text = repo_file(path)
            for leaked in ("notice.zh.txt", "notice_blob.h"):
                self.assertNotIn(
                    leaked, text,
                    f"{name} 提到了 {leaked} —— 明文/blob 不该进发布包")

    def test_the_client_package_only_takes_named_files_from_hook(self):
        """客户端包只拿 `hook\\bin\\`，不是整个 `hook\\` 递归拷。

        这一条红了说明有人把 `hook` 改成整目录拷了 —— 那样 `notice.zh.txt`
        就随包出门了。
        """
        text = repo_file(os.path.join(TOOLS, "build-portable.ps1"))
        self.assertIn("hook\\bin", text)
        self.assertNotIn("Copy-TreeFiltered (Join-Path $Root 'hook')", text)


if __name__ == "__main__":
    unittest.main()
