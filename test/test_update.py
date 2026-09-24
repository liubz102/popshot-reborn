#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动更新的**服务端契约**测试（update_manifest.py + versioning.load_own_version
+ gameserver 的拒绝文案）。

客户端侧（探针握手、manifest 校验、玩家数据保护、下载 sha256、BUILD.ver
提交点）的原 python 实现已随 tools/update_client.py 一起退役 —— 全部逻辑
移进了 C 版更新器（updater\\src\\），由两处接管：

* `updater\\src\\selftest.c`  —— 构建闸门：密码向量 / 0xFE 帧 / 版本号数学 /
  manifest 解析 / 保护清单 / sha256 / 嵌入资源；
* `updater\\scripts\\test_e2e.py` —— 端到端：真探针握手 + 下载 + 应用 +
  保护文件保留 + 运行中 exe 改名让位。

这里留下的是**服务器必须继续说对的话**：
* 拒绝文案带着服务器自己的版本号（更新器探针靠它对准成对发布批次）；
* BUILD.ver 的 version 读取（文案的版本来源）；
* manifest 生成幂等（同版本原位替换保留 notes、新版本前插）。
"""
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")   # 被测代码在隔壁
for _path in (HERE, SERVER):
    if _path not in sys.path:
        sys.path.insert(0, _path)
TOOLS = os.path.join(os.path.dirname(HERE), "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import gameserver                                                  # noqa: E402
import update_manifest                                             # noqa: E402
import versioning                                                  # noqa: E402


# ---------------------------------------------------------------------------
#  versioning.load_own_version —— 服务器自己是什么批次
# ---------------------------------------------------------------------------

class LoadOwnVersionTests(unittest.TestCase):
    def write_build_ver(self, tmp, content):
        path = os.path.join(tmp, versioning.BUILD_VER_FILENAME)
        with open(path, "wb") as f:
            f.write(content)
        return path

    def test_json_with_extras(self):
        # 打包脚本写的真实形态：version 第一键 + buildId 等杂项
        with tempfile.TemporaryDirectory() as tmp:
            self.write_build_ver(
                tmp, b'{"version": "V0.2.8", "buildId": "x", "notes": []}')
            version, warnings = versioning.load_own_version(root=tmp)
            self.assertEqual(version, (0, 2, 8))
            self.assertEqual(warnings, [])

    def test_bom_and_broken_json_fallback(self):
        # JSON 坏了但 "version" 键还在 —— bshook 同款扫描兜底
        with tempfile.TemporaryDirectory() as tmp:
            self.write_build_ver(
                tmp, b'\xef\xbb\xbf{"version": "V0.3.1", oops')
            version, _ = versioning.load_own_version(root=tmp)
            self.assertEqual(version, (0, 3, 1))

    def test_missing_file_returns_none_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            version, warnings = versioning.load_own_version(root=tmp)
            self.assertIsNone(version)
            self.assertTrue(warnings)

    def test_garbage_version_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_build_ver(tmp, b'{"version": "???"}')
            version, warnings = versioning.load_own_version(root=tmp)
            self.assertIsNone(version)
            self.assertTrue(warnings)

    def test_reload_after_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_build_ver(tmp, b'{"version": "V0.2.7"}')
            first, _ = versioning.load_own_version(root=tmp)
            self.assertEqual(first, (0, 2, 7))
            # 换内容再读就是新值。缓存的失效判据是 mtime+size（同
            # load_client_filter 的模式），所以用**长度不同**的内容 ——
            # 实际场景里 BUILD.ver 只随「换包 + 重启」变化，不存在
            # 同 tick 同长度的竞争。
            self.write_build_ver(
                tmp, b'{"version": "V0.2.10", "buildId": "zz"}')
            second, _ = versioning.load_own_version(root=tmp)
            self.assertEqual(second, (0, 2, 10))


# ---------------------------------------------------------------------------
#  拒绝文案：机器可读的版本号锚点（C 更新器的探针从这句话里抠目标版本，
#  抠法 = [vV]数字.数字[.数字]，updater\src\probe.c + selftest 钉着）
# ---------------------------------------------------------------------------

WANTED_VERSION_RE = re.compile(r"[vV](\d+(?:\.\d+){1,2})")


class RejectMessageTests(unittest.TestCase):
    def test_message_carries_own_version(self):
        with mock.patch.object(versioning, "load_own_version",
                               return_value=((0, 2, 8), [])):
            message = gameserver.version_reject_message()
        self.assertIn("V0.2.8", message)
        m = WANTED_VERSION_RE.search(message)
        self.assertIsNotNone(m)
        self.assertEqual(versioning.parse_version_text(m.group(1)), (0, 2, 8))

    def test_own_version_not_filter_minimum(self):
        # ★ 用户钉死的语义（D155）：文案带**服务器自己的版本**（BUILD.ver），
        #   不是门禁最低版本（server-ClientFilter.config）。
        #   例：服务器 0.2.15 / 最低 0.2.10 / 客户端 0.2.7 被拒，
        #   客户端要升到 0.2.15（和服务器同批次），不是踩线 0.2.10。
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, versioning.BUILD_VER_FILENAME),
                      "wb") as f:
                f.write(b'{"version": "V0.2.15", "buildId": "x"}')
            os.makedirs(os.path.dirname(versioning.client_filter_path(tmp)),
                        exist_ok=True)
            with open(versioning.client_filter_path(tmp),
                      "w", encoding="utf-8") as f:
                f.write("0.2.10\n")
            message = gameserver.version_reject_message(root=tmp)
        self.assertIn("V0.2.15", message)
        self.assertNotIn("0.2.10", message)
        m = WANTED_VERSION_RE.search(message)
        self.assertEqual(versioning.parse_version_text(m.group(1)),
                         (0, 2, 15))

    def test_fallback_message_has_no_version(self):
        with mock.patch.object(versioning, "load_own_version",
                               return_value=(None, ["没有找到"])):
            message = gameserver.version_reject_message()
        self.assertEqual(message, gameserver.VERSION_REJECT_MESSAGE)
        # 兜底文案里抠不出版本号 -> 更新器按 manifest 最新版处理
        self.assertIsNone(WANTED_VERSION_RE.search(message))


# ---------------------------------------------------------------------------
#  manifest 生成幂等（tools\update_manifest.py）
# ---------------------------------------------------------------------------

class ManifestMergeTests(unittest.TestCase):
    def test_merge_from_scratch_and_prepend(self):
        m1 = update_manifest.merge_manifest(None, "0.2.7", "u7", 100, "aa" * 32)
        self.assertEqual([r["version"] for r in m1["releases"]], ["V0.2.7"])
        m2 = update_manifest.merge_manifest(m1, "0.2.8", "u8", 200, "bb" * 32)
        self.assertEqual([r["version"] for r in m2["releases"]],
                         ["V0.2.8", "V0.2.7"])          # 新的在前

    def test_merge_same_version_replaces_in_place_keeps_notes(self):
        base = update_manifest.merge_manifest(
            None, "0.2.7", "u7", 100, "aa" * 32, date="2026-08-01")
        base["releases"][0]["notes"] = "手工写的发版说明"
        # 同版本重打包：url/size/sha256/date 全刷新，notes 原样保留
        again = update_manifest.merge_manifest(
            base, "V0.2.7", "u7-new", 300, "cc" * 32, date="2026-08-22")
        self.assertEqual(len(again["releases"]), 1)
        entry = again["releases"][0]
        self.assertEqual((entry["url"], entry["size"], entry["sha256"]),
                         ("u7-new", 300, "cc" * 32))
        self.assertEqual(entry["date"], "2026-08-22")
        self.assertEqual(entry["notes"], "手工写的发版说明")
        # 旧对象不能被就地改掉（纯函数）
        self.assertEqual(base["releases"][0]["size"], 100)

    def test_merge_bad_version_raises(self):
        with self.assertRaises(ValueError):
            update_manifest.merge_manifest(None, "x.y", "u", 1, "aa" * 32)

    def test_release_url_tag_dots_filename_dashes(self):
        # 用户仓库的实际格式：tag 带点（V0.2.7），文件名点转横杠（V0-2-7）
        url = update_manifest.release_url("V0.2.7",
                                          "PopShot-portable-win64_V0-2-7.zip")
        self.assertEqual(
            url, "https://github.com/liubz102/popshot-reborn"
                 "/releases/download/V0.2.7/PopShot-portable-win64_V0-2-7.zip")


class ShippedUpdateConfigTests(unittest.TestCase):
    """仓库里那份 `config/update.config` —— 两个包都拿它当默认更新源。

    它现在是**发布出去的默认值**：客户端包里那份是玩家的兜底，服务端包里
    那份是开服的人拿来改的起点。写错 / 忘了跟着 `REPO_URL` 改，症状是
    「所有新玩家都从旧仓库下包」，而且没有任何报错。
    """

    PATH = os.path.join(os.path.dirname(HERE), "config", "update.config")

    def read(self):
        if not os.path.exists(self.PATH):
            self.skipTest("不在源码仓库里")
        with open(self.PATH, encoding="utf-8") as fp:
            return fp.read()

    def manifest_url(self):
        for line in self.read().splitlines():
            line = line.strip()
            if line.startswith("#") or line.startswith(";") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip().lower() == "manifest_url":
                return value.strip()
        return None

    def test_it_ships_a_manifest_url_pointing_at_our_repo(self):
        url = self.manifest_url()
        self.assertIsNotNone(url, "config/update.config 里没有 manifest_url")
        self.assertTrue(url.startswith(update_manifest.REPO_URL + "/"),
                        f"manifest_url 和 update_manifest.REPO_URL 分叉了：{url}")
        self.assertTrue(url.endswith("/manifest.json"), url)

    def test_the_manifest_url_line_is_invisible_to_an_old_updater(self):
        """★ 老更新器（V0.4.2 及更早）只会把这一行丢进「认不出的行」。

        它的解析器只收「`http://`/`https://` 开头、中间无空白」的行，
        `manifest_url = https://…` 中间有空格 ⇒ 被忽略，**不会被当成一个
        代理地址去用**。新格式对老客户端安全，全靠这一条 —— 所以这一行
        永远不能写成 `manifest_url=https://…`（没空格）那种紧凑形式。
        """
        for line in self.read().splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("manifest_url"):
                self.assertRegex(stripped, r"^manifest_url\s+=\s+\S",
                                 "manifest_url 这一行 = 两边必须留空格，"
                                 "否则老更新器会把它当成代理地址")

    def test_every_proxy_line_is_a_bare_url(self):
        for line in self.read().splitlines():
            stripped = line.strip()
            if (not stripped or stripped.startswith("#")
                    or stripped.startswith(";") or "=" in stripped):
                continue
            self.assertRegex(stripped, r"^https?://\S+$",
                             f"既不是注释也不是配置行，却不像代理地址：{stripped!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
