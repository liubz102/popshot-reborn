#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端 hook 完整性校验（D85）：钉住算法、钉住四条判定路径。

## 为什么值得一个专门的测试文件

握手上报的那 4 个字节现在同时承载「版本号」和「`bshook.dll` 的完整性校验位」。
它有两种坏法，**症状都不是报错**：

1. **两边算法漂了** —— `hook/bshook.c` 的 `hsver_compute_tag()` 和
   `versioning.hook_tag()` 必须逐位同构。漂了就是**谁都登不上**
   （所有人校验位对不上 → 全体被要求重新更新）。下面用**固定向量**钉死，
   那个向量是 2026-09-11 拿真客户端实测出来、和 C 侧逐位对过的。
2. **判定路径搞反了** —— 「清单里没有这个版本」分两档：比清单里最老的还老
   = 那一版发布时还没有这项功能，交给版本门禁；否则一律拒（服务端保证先于
   客户端更新）。而**服务端自己的版本不在清单里**时必须整项关掉，否则被拒的
   客户端下回来的还是同一版 -> **死循环**。这几条各有一个用例钉着。
"""
import io
import json
import os
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import versioning                                              # noqa: E402

ROOT = os.path.dirname(HERE)
BSHOOK = os.path.join(ROOT, "hook", "bshook.c")

#: ★ C 侧实测向量（2026-09-11，真客户端日志）：
#:   bshook.dll 的 sha256 = 下面这个，BUILD.ver = V0.3.1（版本码 3001）
#:   -> C 侧算出校验位 123，补丁进握手的 4 字节是 0xFB000BB9。
VECTOR_SHA256 = "fc75d4e954b9b2601dcb026e52aeea5867e86adbe194bb2207d2a4117d3d0860"
VECTOR_VERSION = (0, 3, 1)
VECTOR_TAG = 123
VECTOR_WIRE = 0xFB000BB9


def as_int32(unsigned):
    """握手那边是按 `<i` 解的，测试要走同一条路（新编码一定是负数）。"""
    return struct.unpack("<i", struct.pack("<I", unsigned))[0]


class AlgorithmIsPinnedTests(unittest.TestCase):
    def test_the_fixed_vector_from_the_real_client(self):
        """★ 改了算法这条必红。红了先问「C 侧改了吗」，两边必须一起改。"""
        self.assertEqual(versioning.hook_tag(VECTOR_SHA256, 3001), VECTOR_TAG)
        self.assertEqual(
            versioning.encode_wire_v2(VECTOR_VERSION, VECTOR_SHA256),
            VECTOR_WIRE)

    def test_the_wire_layout(self):
        """bit31 标记 / bit30..24 校验位 / bit23..0 版本号。"""
        self.assertTrue(VECTOR_WIRE & versioning.WIRE_V2_FLAG)
        self.assertEqual(
            (VECTOR_WIRE >> versioning.WIRE_V2_TAG_SHIFT)
            & versioning.WIRE_V2_TAG_MASK, VECTOR_TAG)
        self.assertEqual(VECTOR_WIRE & versioning.WIRE_V2_VERSION_MASK,
                         versioning.encode_wire(VECTOR_VERSION))

    def test_the_hook_source_uses_the_same_algorithm(self):
        """`bshook.c` 里那几个常量必须和这边对得上（同 test_notice 的守法）。"""
        if not os.path.exists(BSHOOK):
            raise unittest.SkipTest("不在源码仓库里，跳过")
        with open(BSHOOK, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        for token in ("#define HS_V2_FLAG        0x80000000u",
                      "#define HS_V2_VER_MASK    0x00FFFFFFu",
                      "#define HS_V2_TAG_SHIFT   24",
                      "#define HS_V2_TAG_MASK    0x7Fu"):
            self.assertIn(token, text, f"bshook.c 少了 `{token}`")
        # 「32 个 hash 字节 ‖ 版本号 4 字节小端」再 sha256 —— 缓冲区就是 36 字节
        self.assertIn("unsigned char buf[36];", text,
                      "bshook.c 的 hsver_compute_tag 不再是 32+4 的布局？")


class DecodeTests(unittest.TestCase):
    def test_new_encoding_round_trips_through_int32(self):
        version, tag = versioning.decode_wire_ex(as_int32(VECTOR_WIRE))
        self.assertEqual(version, VECTOR_VERSION)
        self.assertEqual(tag, VECTOR_TAG)

    def test_old_encoding_still_decodes_and_has_no_tag(self):
        """★ 向后兼容：老客户端（V0.3.1 及以前）必须照样认得出来。"""
        version, tag = versioning.decode_wire_ex(3001)
        self.assertEqual(version, (0, 3, 1))
        self.assertIsNone(tag)

    def test_the_original_client_is_still_nobody(self):
        self.assertEqual(
            versioning.decode_wire_ex(versioning.LEGACY_WIRE_VERSION),
            (None, None))

    def test_decode_wire_still_works_for_callers_that_only_want_the_version(self):
        self.assertEqual(versioning.decode_wire(as_int32(VECTOR_WIRE)),
                         VECTOR_VERSION)
        self.assertEqual(versioning.decode_wire(3001), (0, 3, 1))

    def test_major_too_big_for_the_new_encoding_is_refused_at_build_time(self):
        """低 24 位装不下就得在打包时炸，不许悄悄上线一个解不开的值。"""
        with self.assertRaises(ValueError):
            versioning.encode_wire_v2((20, 0, 0), VECTOR_SHA256)

    def test_garbage_with_the_flag_bit_set_stays_garbage(self):
        """★ 实测踩过：`-1` 曾被解成一个「合法」的 V16.777.215。

        乱值必须当乱值 —— 合法版本码只到 15.999.999，别让整个 24 位都算数。
        """
        self.assertEqual(versioning.decode_wire_ex(-1), (None, None))
        self.assertEqual(versioning.decode_wire_ex(as_int32(0xFFFFFFFF)),
                         (None, None))
        # 标记位在、但版本码是 0 -> 也认不出
        self.assertEqual(versioning.decode_wire_ex(as_int32(0x80000000)),
                         (None, None))


class VerdictTests(unittest.TestCase):
    """★ 用例全部自带一个临时的「服务端自身版本」（BUILD.ver），不蹭仓库根 ——
    `verify_client_hook` 现在要看服务端自己的版本在不在清单里（安全阀）。"""

    MANIFEST = {VECTOR_VERSION: VECTOR_SHA256}

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(self._rmtree, self.root)
        self._write_own_version(versioning.format_version(VECTOR_VERSION))

    @staticmethod
    def _rmtree(path):
        import shutil
        shutil.rmtree(path, ignore_errors=True)

    def _write_own_version(self, text):
        with io.open(os.path.join(self.root, "BUILD.ver"), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"version": text}) + "\n")
        versioning.load_own_version(self.root, _reload=True)

    def verify(self, version, tag, manifest=None):
        return versioning.verify_client_hook(
            version, tag,
            self.MANIFEST if manifest is None else manifest, self.root)[0]

    def test_matching_tag_passes(self):
        self.assertEqual(self.verify(VECTOR_VERSION, VECTOR_TAG),
                         versioning.HOOK_OK)

    def test_wrong_tag_is_a_mismatch(self):
        self.assertEqual(self.verify(VECTOR_VERSION, (VECTOR_TAG + 1) & 0x7F),
                         versioning.HOOK_MISMATCH)

    def test_no_tag_at_all_is_a_mismatch_when_the_version_is_known(self):
        """算 hash 那段被 patch 掉 -> 退回旧编码 -> 必须当成「对不上」。"""
        self.assertEqual(self.verify(VECTOR_VERSION, None),
                         versioning.HOOK_MISMATCH)

    def test_a_newer_version_that_is_not_listed_is_a_mismatch(self):
        """★ 用户 2026-09-11 拍板：服务端一定先于客户端更新，所以「清单里
        没有」只可能是手改出来的版本号 —— 拒，走强制更新。"""
        self.assertEqual(self.verify((9, 9, 9), 5), versioning.HOOK_MISMATCH)

    def test_a_version_older_than_the_whole_manifest_is_left_to_the_gate(self):
        """★ 比清单里最老的还老 = 那一版**发布时还没有这项功能**，
        「清单里没有」证明不了它被改过。

        不这么分的话 `server-ClientFilter.config` 填 `0`（不限制）就失去意义了 ——
        运营者明说要放老客户端进来，却被完整性校验挡住。
        （既有的 `test_online.VersionGateTests` 当场抓到过这个误伤。）
        """
        self.assertEqual(self.verify((0, 2, 7), None), versioning.HOOK_UNKNOWN)
        self.assertEqual(self.verify((0, 2, 7), 5), versioning.HOOK_UNKNOWN)

    def test_no_version_at_all_passes(self):
        """原版客户端（没上报版本）交给版本门禁去管，不归完整性校验 ——
        不然 server-ClientFilter.config 填 0（不限制）就失去意义了。"""
        self.assertEqual(self.verify(None, None), versioning.HOOK_UNKNOWN)

    def test_an_empty_manifest_disables_the_whole_check(self):
        """fail-open：清单还没生成时，谁都不该被挡在外面。"""
        self.assertEqual(self.verify(VECTOR_VERSION, VECTOR_TAG, {}),
                         versioning.HOOK_UNKNOWN)

    def test_the_valve_when_the_server_is_not_in_its_own_manifest(self):
        """★★ 安全阀，别拿掉。

        被拒的客户端会去下载**服务端自己那个版本**（拒绝文案里写的就是它）。
        所以服务端自己的版本都不在清单里时，客户端下完还是不在清单里 ——
        **死循环，而且解不开**。这种状态下整项校验必须自动关掉。
        """
        self._write_own_version("V9.9.9")            # 服务端自己不在清单里
        self.assertIsNotNone(
            versioning.hook_check_disabled_reason(self.MANIFEST, self.root))
        # 连一个本该被拒的客户端也得放行 —— 宁可不校验，不能全服锁死
        self.assertEqual(self.verify(VECTOR_VERSION, (VECTOR_TAG + 1) & 0x7F),
                         versioning.HOOK_UNKNOWN)
        self.assertEqual(self.verify((9, 9, 9), 5), versioning.HOOK_UNKNOWN)

    def test_the_valve_is_open_when_everything_lines_up(self):
        self.assertIsNone(
            versioning.hook_check_disabled_reason(self.MANIFEST, self.root))


class ManifestLoadingTests(unittest.TestCase):
    def _write(self, text):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return path

    def test_a_good_manifest_loads(self):
        path = self._write(json.dumps({"hooks": [
            {"version": "V0.3.2", "sha256": VECTOR_SHA256}]}))
        table, warnings = versioning.load_hook_manifest(path, _reload=True)
        self.assertEqual(table, {(0, 3, 2): VECTOR_SHA256})
        self.assertEqual(warnings, [])

    def test_a_missing_file_is_fail_open(self):
        table, warnings = versioning.load_hook_manifest(
            os.path.join(tempfile.gettempdir(), "no-such-manifest.json"),
            _reload=True)
        self.assertEqual(table, {})
        self.assertTrue(warnings)

    def test_broken_json_is_fail_open(self):
        """★ 实测踩过：文件被写坏时**绝不能**把玩家挡在外面。"""
        path = self._write("{ this is not json")
        table, warnings = versioning.load_hook_manifest(path, _reload=True)
        self.assertEqual(table, {})
        self.assertTrue(warnings)

    def test_one_bad_entry_does_not_kill_the_whole_table(self):
        path = self._write(json.dumps({"hooks": [
            {"version": "V0.3.2", "sha256": VECTOR_SHA256},
            {"version": "认不出", "sha256": "zz"},
            {"version": "V0.3.3", "sha256": "nope"}]}))
        table, warnings = versioning.load_hook_manifest(path, _reload=True)
        self.assertEqual(table, {(0, 3, 2): VECTOR_SHA256})
        self.assertEqual(len(warnings), 2)

    def test_the_repo_manifest_is_loadable(self):
        """仓库里那份必须是合法的（哪怕是空表）。"""
        path = versioning.hook_manifest_path()
        if not os.path.exists(path):
            raise unittest.SkipTest(f"没有 {path}，跳过")
        table, warnings = versioning.load_hook_manifest(path, _reload=True)
        self.assertEqual(warnings, [], f"{path} 里有认不出的条目")
        for version, digest in table.items():
            self.assertEqual(len(digest), 64)
            bytes.fromhex(digest)
            self.assertIsNotNone(versioning.format_version(version))


class PackagingRebuildsTheHookTests(unittest.TestCase):
    """★ 打包必须**先重编 hook 再打**（D87）。

    上面那一整套校验的前提是「包里的 DLL 和包里记的 SHA 出自同一次编译」。
    编 hook 和打包本来是两件分开的事 —— 改完 `hook/*.c` 忘了重编就打包，
    打出来的包**自洽**（DLL 旧、SHA 也旧），却和源码对不上，**没有任何报错**。
    所以这几条钉的是打包脚本里那个调用，以及它的位置。
    """

    TOOLS = os.path.join(ROOT, "tools")

    def script(self, name):
        path = os.path.join(self.TOOLS, name)
        if not os.path.exists(path):
            raise unittest.SkipTest(f"不在源码仓库里（缺 {path}），跳过")
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            return f.read()

    def test_the_helper_lives_in_build_common(self):
        """两个打包脚本共用同一份实现（铁律 8 的调子）。"""
        text = self.script("build-common.ps1")
        self.assertIn("function Invoke-HookBuild", text)
        self.assertIn("function Assert-HookWritable", text)

    def test_being_locked_aborts_instead_of_warning(self):
        """hook 被占用是**中止**，不是黄字提醒。

        警告一下照打的话，打出来的正是要根治的那种包：旧 DLL + 新 SHA。
        """
        text = self.script("build-common.ps1")
        head = text.index("function Assert-HookWritable")
        tail = text.index("function Invoke-HookBuild")
        body = text[head:tail]
        self.assertIn("throw", body)
        self.assertIn("被占用", body)

    def test_both_builders_rebuild_before_they_delete_anything(self):
        """★ 位置也要钉：必须在 `Assert-EmptyTarget` **之前**。

        那一句带 `-Force` 会把上一次的成果物整个删掉。编不动 hook 的时候
        （最常见：游戏开着，bshook.dll 注在 BigShot.exe 里）这一次本来就打不成，
        旧成果物不该陪葬。
        """
        for name in ("build-portable.ps1", "build-server-package.ps1"):
            text = self.script(name)
            self.assertIn("Invoke-HookBuild -Root $Root", text,
                          f"{name} 没有在打包前重编 hook")
            self.assertLess(
                text.index("Invoke-HookBuild -Root $Root"),
                text.index("Assert-EmptyTarget -Path"),
                f"{name} 把重编排在了删除旧成果物之后")

    def test_the_menu_rebuilds_before_it_clears_the_previous_output(self):
        """菜单那一层同理：`Clear-Stale` 之前就得知道 hook 编不编得动。

        两条分支（带参数 / 走菜单）各有一次 `Clear-Stale`，所以比的是
        **最后一次**调用的先后 —— 谁被挪到了删除动作之后，这条就红。
        """
        text = self.script("build-menu.ps1")
        self.assertIn("Invoke-HookBuildOrExit", text)
        self.assertEqual(text.count("Invoke-HookBuildOrExit"),
                         text.count("Clear-Stale -Paths") + 1,   # +1 = 函数定义
                         "build-menu.ps1 有一条 Clear-Stale 前面没重编 hook")
        self.assertLess(text.rindex("Invoke-HookBuildOrExit"),
                        text.rindex("Clear-Stale -Paths"),
                        "build-menu.ps1 把重编排在了 Clear-Stale 之后")


class ReproducibleBuildTests(unittest.TestCase):
    """★ `hook/build.bat` 必须带 `/Brepro`（D87 六 / §92）。

    少了它，link.exe 会把**当前时间**戳进 PE 头，于是同一份源码重编出来的
    DLL 字节不同、SHA-256 也不同。而打包现在每次都重编 hook 并把这个 SHA
    记进 `server/manifest-hook.json` ⇒ 后果有两条，**都不是报错**：

    1. 每打一次包，`hook/bin/*` + 清单三个文件无缘无故变成改动状态；
    2. **同版本先前发出去的客户端**，对着新打的服务端包一律校验不过。

    带上 `/Brepro` 时间戳变成内容哈希，同源码 = 同字节 = 同 SHA，两条一起没有。
    """

    def build_bat_lines(self):
        path = os.path.join(ROOT, "hook", "build.bat")
        if not os.path.exists(path):
            raise unittest.SkipTest(f"不在源码仓库里（缺 {path}），跳过")
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return [ln.strip() for ln in f if ln.strip().startswith("cl ")]

    def test_every_compile_line_is_deterministic(self):
        lines = self.build_bat_lines()
        self.assertEqual(len(lines), 2,
                         "hook/build.bat 的 cl 行数变了，这条要跟着改")
        for line in lines:
            self.assertIn("/link", line)
            compile_part, link_part = line.split("/link", 1)
            self.assertIn("/Brepro", compile_part,
                          f"编译那半截少了 /Brepro：{line}")
            self.assertIn("/Brepro", link_part,
                          f"链接那半截少了 /Brepro：{line}")

    def test_the_batch_file_stays_ascii(self):
        """★ 顺手守住铁律 3：`.bat` 里一个多字节字符都不许有。

        `chcp 65001` 下 cmd.exe 按**字符**计数却按**字节**定位，中文一个字差
        2 字节，攒够了就把某一行命令拦腰截断（D074 / V0.2 §135）——
        症状是「'xxx' 不是内部或外部命令」，或者某一段莫名其妙跑第二遍。
        判据是「字节数 == 字符数」，不是「这次跑通了没有」。
        """
        path = os.path.join(ROOT, "hook", "build.bat")
        if not os.path.exists(path):
            raise unittest.SkipTest(f"不在源码仓库里（缺 {path}），跳过")
        with open(path, "rb") as f:
            raw = f.read()
        self.assertEqual(len(raw), len(raw.decode("utf-8")),
                         "hook/build.bat 里混进了非 ASCII 字符")


if __name__ == "__main__":
    unittest.main()
