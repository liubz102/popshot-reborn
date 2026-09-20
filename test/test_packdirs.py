#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端资源目录名（Pack / Pack_develop / Pack_publish）只有**一个**来源 —— 盯着别长出第二个。

和 `test_ports.py` 同一个道理：`server/config.py` 是源，`hook/pack.h` 由
`tools/gen_pack_h.py` 生成，PowerShell / 打包器都来问它。分叉的症状不是报错：
hook 重定向到了一个目录、打包器写到了另一个目录，游戏起来什么资源都读不到。

⚠ 这些用例依赖仓库布局（`hook/`、`tools/`），发布包里没有它们。
"""
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")   # 被测代码在隔壁
for _path in (HERE, SERVER):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import config as server_config                                  # noqa: E402

ROOT = os.path.dirname(HERE)
HEADER = os.path.join(ROOT, "hook", "pack.h")
BSHOOK = os.path.join(ROOT, "hook", "bshook.c")
GENERATOR = os.path.join(ROOT, "tools", "gen_pack_h.py")
PKN = os.path.join(ROOT, "tools", "pkn.py")
BUILD_COMMON = os.path.join(ROOT, "tools", "build-common.ps1")
GITATTRIBUTES = os.path.join(ROOT, ".gitattributes")
GITIGNORE = os.path.join(ROOT, ".gitignore")

#: 这些脚本里不许出现带引号的目录名字面量（注释行不算）：要用就问 config.py。
SCRIPTS_WITHOUT_LITERALS = [
    "tools/launch.ps1", "tools/build-common.ps1", "tools/build-menu.ps1",
    "tools/build-portable.ps1", "tools/build-pack.ps1", "tools/update-gamedata.ps1",
]
#: 五个数据提取器 + 审计脚本：源在 Pack_develop，不许再写死 Pack_decrypt。
EXTRACTORS = [
    "tools/mapdata.py", "tools/weapondata.py", "tools/chrprops.py",
    "tools/shopdata.py", "tools/shopicons.py", "tools/audit_items.py",
]


def repo_file(path):
    if not os.path.exists(path):
        raise unittest.SkipTest(f"不在源码仓库里（缺 {path}），跳过")
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def code_lines(text, comment_prefix):
    """去掉整行注释和行尾注释（粗略：够拦字面量，不求解析语言）。"""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(comment_prefix):
            continue
        idx = line.find(comment_prefix)
        out.append(line if idx < 0 else line[:idx])
    return out


class ConstantsTests(unittest.TestCase):
    def test_the_three_names_are_what_the_client_and_the_plan_expect(self):
        self.assertEqual(server_config.PACK_LEGACY_DIR, "Pack",
                         "客户端写死的是 Pack（镜像 0x6936b0），这个名字是密钥前缀，不能改")
        self.assertEqual(sorted(server_config.pack_dir_table()),
                         ["PACK_DEVELOP_DIR", "PACK_LEGACY_DIR", "PACK_PUBLISH_DIR"])
        names = list(server_config.pack_dir_table().values())
        self.assertEqual(len(set(n.lower() for n in names)), 3, "三个目录名不分大小写要互异")
        for n in names:
            self.assertTrue(n.isascii() and re.fullmatch(r"[A-Za-z0-9_]+", n), n)

    def test_publish_dir_does_not_look_like_the_legacy_prefix(self):
        """hook 的判据是「Pack 后面紧跟 / 或 \\」；Pack_publish 的第 5 个字符是 _，
        所以改写过的路径不会被二次改写。这条把这个前提钉住。"""
        legacy = server_config.PACK_LEGACY_DIR
        pub = server_config.PACK_PUBLISH_DIR
        self.assertTrue(pub.lower().startswith(legacy.lower()))
        self.assertNotIn(pub[len(legacy)], "/\\")


class GeneratedHeaderTests(unittest.TestCase):
    def test_the_header_matches_config_py(self):
        """★ `hook/pack.h` 必须和 `server/config.py` 一致。红了就跑 `python tools/gen_pack_h.py`。"""
        text = repo_file(HEADER)
        found = dict(re.findall(r'#define\s+POPSHOT_(PACK_\w+?)\s+"([^"]*)"', text))
        found_w = dict(re.findall(r'#define\s+POPSHOT_(PACK_\w+?)_W\s+L"([^"]*)"', text))
        want = server_config.pack_dir_table()
        self.assertEqual(found, want)
        self.assertEqual(found_w, want)

    def test_the_generator_reports_it_is_up_to_date(self):
        """★ 输出要**收下来放进断言消息**，不能 DEVNULL 一扔了事。

        2026-09-20 CI 上这条红过，报的是「hook/pack.h 不是最新的」——
        而头文件其实好好的，真正的死因是生成器打那句中文提示时
        `UnicodeEncodeError`（见 `tools/gen_pack_h.py` 的 `__main__`）。
        输出被 DEVNULL 吞了，于是报告只剩一句把人往反方向带的话。
        """
        if not os.path.exists(GENERATOR):
            raise unittest.SkipTest("不在源码仓库里")
        result = subprocess.run([sys.executable, GENERATOR, "--check"],
                                capture_output=True)
        self.assertEqual(result.returncode, 0,
                         "hook/pack.h 不是最新的：python tools/gen_pack_h.py\n"
                         + (result.stdout + result.stderr).decode("utf-8", "replace"))

    def test_config_py_prints_the_table_for_powershell(self):
        out = subprocess.check_output([sys.executable, os.path.join(SERVER, "config.py"), "--pack-dirs"])
        pairs = dict(line.split("=", 1) for line in out.decode("utf-8").split())
        self.assertEqual(pairs, server_config.pack_dir_table())


class NoSecondCopyTests(unittest.TestCase):
    def test_bshook_includes_the_header_and_has_no_literal(self):
        text = repo_file(BSHOOK)
        self.assertIn('#include "pack.h"', text)
        pub = server_config.PACK_PUBLISH_DIR
        # 注释里提到 "Pack/" 是说明，不算；只看代码。把块注释换成等长的空行保住行号。
        code = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
        code = re.sub(r"//[^\n]*", "", code)
        # ★ 不用 assertNotRegex：它失败时会把整个 8000 行的 bshook.c 打进断言消息。
        for pattern, why in ((r'L?"%s' % re.escape(pub), "Pack_publish 字面量"),
                             (r'L?"%s[\\/]' % re.escape(server_config.PACK_LEGACY_DIR), '"Pack/" 字面量')):
            m = re.search(pattern, code)
            if m:
                line_no = code.count("\n", 0, m.start()) + 1
                line = text.splitlines()[line_no - 1].strip()
                self.fail("bshook.c:%d 出现了%s —— 用 pack.h 的宏：%s" % (line_no, why, line))

    def test_powershell_scripts_ask_config_py(self):
        # 区分大小写：'pack' 是 pkn.py 的子命令名，不是目录名。
        pattern = re.compile(r"""['"](%s)['"]""" % "|".join(
            re.escape(v) for v in server_config.pack_dir_table().values()))
        for rel in SCRIPTS_WITHOUT_LITERALS:
            path = os.path.join(ROOT, rel)
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                lines = code_lines(f.read(), "#")
            for line in lines:
                self.assertIsNone(pattern.search(line), "%s: %s" % (rel, line.strip()))

    def test_extractors_no_longer_hardcode_pack_decrypt(self):
        for rel in EXTRACTORS:
            path = os.path.join(ROOT, rel)
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = code_lines(f.read(), "#")
            for line in lines:
                self.assertNotIn('"Pack_decrypt"', line, "%s: %s" % (rel, line.strip()))
                self.assertNotIn("'Pack_decrypt'", line, "%s: %s" % (rel, line.strip()))


class PackToolEncodingTests(unittest.TestCase):
    """`tools\\pkn.py` 的中文输出：编码端和解码端都必须钉死 utf-8。

    2026-09-14 的症状是**只有 python 打的那几行乱码**，PowerShell 自己
    `Write-Host` 的中文却是好的 —— 因为 `build-common.ps1` 的
    `Invoke-PknTool` 用 `| Out-Host` 捕获输出，一捕获 stdout 就成了管道，
    CPython 随即放弃 `WriteConsoleW` 改用 `GetACP()` = cp936；而入口 bat 的
    `chcp 65001` 让 PowerShell 按 utf-8 去解那串 GBK 字节。

    两端缺一不可，所以两条都钉着。**判据是「文件里有没有这两个事实」**，
    不是「跑一遍看看乱不乱」—— 乱码落在哪一行取决于缓冲边界，跑通一次不算数。
    """

    def test_pkn_pins_utf8_on_its_own_stdout(self):
        text = repo_file(PKN)
        m = re.search(r"\.reconfigure\(([^)]*)\)", text)
        self.assertIsNotNone(m, "pkn.py 不再 reconfigure stdout 了？")
        self.assertIn('encoding="utf-8"', m.group(1),
                      "pkn.py 的 reconfigure 必须带 encoding=\"utf-8\" —— "
                      "只写 errors 挡不住「编错了」，只挡「编不出来」")

    def test_build_common_pins_the_decoding_side(self):
        lines = code_lines(repo_file(BUILD_COMMON), "#")
        self.assertTrue(
            any("[Console]::OutputEncoding" in line for line in lines),
            "build-common.ps1 必须钉一次 [Console]::OutputEncoding —— "
            "不经入口 bat 直接跑 .ps1 时控制台是 936，捕获 python 的 utf-8 会乱码")


class GitRulesTests(unittest.TestCase):
    def test_gitattributes_keeps_both_trees_binary(self):
        text = repo_file(GITATTRIBUTES)
        for d in (server_config.PACK_DEVELOP_DIR, server_config.PACK_PUBLISH_DIR):
            self.assertRegex(text, r"(?m)^game_patched/%s/\*\*\s+(-text|binary)" % re.escape(d))

    def test_gitignore_keeps_the_legacy_dir_and_tmp_files_out(self):
        text = repo_file(GITIGNORE)
        self.assertIn("/game_patched/%s/" % server_config.PACK_LEGACY_DIR, text)
        self.assertIn("/game_patched/%s/*.tmp" % server_config.PACK_PUBLISH_DIR, text)
        self.assertNotIn("/game_patched/%s/" % server_config.PACK_PUBLISH_DIR + "\n", text)


if __name__ == "__main__":
    unittest.main()
