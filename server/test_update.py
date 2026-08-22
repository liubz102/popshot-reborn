#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动更新链的测试（tools/update_client.py + tools/update_manifest.py +
versioning.load_own_version + gameserver 的拒绝文案）。

重点盯四类事：

* **探针协议**：更新器连游戏服重演握手 —— 加密流、0xFE 帧、拒绝文案里
  抠版本号，一个字节都不能错位（SimpleCipher 是有状态流密码）。
* **玩家数据绝不能被更新碰**：PROTECTED_PATHS 的匹配 + 应用流程里真的
  保留旧文件（打包侧过滤是主保护，这里是第二道的证明）。
* **BUILD.ver 是提交点**：sha256 不符 / zip 损坏时绝不能把 BUILD.ver
  写掉 —— 否则下次启动上报的还是新版本号，坏包就「上线」了。
* **manifest 幂等**：同一版本反复打包要原位替换（保留手写 notes），
  新版本前插 —— 开发期重打包是常态。
"""
import datetime
import hashlib
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
TOOLS = os.path.join(os.path.dirname(HERE), "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import gameserver                                                  # noqa: E402
import simple                                                      # noqa: E402
import update_client                                               # noqa: E402
import update_manifest                                             # noqa: E402
import versioning                                                  # noqa: E402


def build_ctrl_frame(result, message=None):
    """gameserver.build_ctrl(w_i32(result)[+w_wstr(message)]) 的镜像，
    造一个服务端会发出来的 0xFE 帧（明文，加密在测试里做）。"""
    payload = struct.pack("<i", result)
    if message is not None:
        payload += struct.pack("<H", len(message)) + message.encode("utf-16-le")
    return bytes([0xFE, 0]) + struct.pack("<H", len(payload)) + payload


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
#  拒绝文案：机器可读的版本号锚点
# ---------------------------------------------------------------------------

class RejectMessageTests(unittest.TestCase):
    def test_message_carries_own_version(self):
        with mock.patch.object(versioning, "load_own_version",
                               return_value=((0, 2, 8), [])):
            message = gameserver.version_reject_message()
        self.assertIn("V0.2.8", message)
        self.assertEqual(update_client.parse_wanted_version(message), (0, 2, 8))

    def test_own_version_not_filter_minimum(self):
        # ★ 用户钉死的语义（D155）：文案带**服务器自己的版本**（BUILD.ver），
        #   不是门禁最低版本（server-ClientFilter.config）。
        #   例：服务器 0.2.15 / 最低 0.2.10 / 客户端 0.2.7 被拒，
        #   客户端要升到 0.2.15（和服务器同批次），不是踩线 0.2.10。
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, versioning.BUILD_VER_FILENAME),
                      "wb") as f:
                f.write(b'{"version": "V0.2.15", "buildId": "x"}')
            with open(os.path.join(tmp,
                                   versioning.CLIENT_FILTER_FILENAME),
                      "w", encoding="utf-8") as f:
                f.write("0.2.10\n")
            message = gameserver.version_reject_message(root=tmp)
        self.assertIn("V0.2.15", message)
        self.assertNotIn("0.2.10", message)
        self.assertEqual(update_client.parse_wanted_version(message),
                         (0, 2, 15))

    def test_fallback_message_has_no_version(self):
        with mock.patch.object(versioning, "load_own_version",
                               return_value=(None, ["没有找到"])):
            message = gameserver.version_reject_message()
        self.assertEqual(message, gameserver.VERSION_REJECT_MESSAGE)
        # 兜底文案里抠不出版本号 -> 更新器按 manifest 最新版处理
        self.assertIsNone(update_client.parse_wanted_version(message))

    def test_parse_wanted_version_shapes(self):
        for text, expected in (
                ("请更新到 V0.10.3 后再连接。", (0, 10, 3)),
                ("请更新到 v1.0 后再连接。", (1, 0, 0)),
                ("abc", None),
                ("V", None),
                ("", None),
                (None, None)):
            self.assertEqual(update_client.parse_wanted_version(text),
                             expected, text)


# ---------------------------------------------------------------------------
#  探针：真实 socket 上的握手重演
# ---------------------------------------------------------------------------

class ProbeTests(unittest.TestCase):
    def serve_once(self, respond_with):
        """起一个说游戏服协议的服务端，返回 (port, thread)。连一次就退。"""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def run():
            try:
                conn, _ = srv.accept()
                cin = simple.SimpleCipher.client_to_server()
                data = b""
                while len(data) < 4:
                    data += conn.recv(64)
                seen_version = struct.unpack("<i", cin.decrypt(data))[0]
                self.seen_versions.append(seen_version)
                cout = simple.SimpleCipher.server_to_client()
                conn.sendall(cout.encrypt(respond_with))
                conn.close()
            except OSError:
                pass
            finally:
                srv.close()

        self.seen_versions = []
        thread = threading.Thread(target=run)
        thread.start()
        return port, thread

    def test_rejected_with_wanted_version(self):
        port, thread = self.serve_once(
            build_ctrl_frame(1, "客户端版本过旧，请更新到 V0.2.8 后再连接。"))
        status, wanted, message = update_client.probe_server(
            "127.0.0.1", (0, 2, 6), port=port)
        thread.join(timeout=5)
        self.assertEqual(status, "rejected")
        self.assertEqual(wanted, (0, 2, 8))
        self.assertEqual(message, "客户端版本过旧，请更新到 V0.2.8 后再连接。")
        # 服务端解密看到的必须是 0.2.6 的线上编码 —— 方向/初态都对了
        self.assertEqual(self.seen_versions, [2006])

    def test_accepted_result_zero(self):
        port, thread = self.serve_once(build_ctrl_frame(0))
        status, wanted, _ = update_client.probe_server(
            "127.0.0.1", (0, 2, 8), port=port)
        thread.join(timeout=5)
        self.assertEqual(status, "ok")
        self.assertIsNone(wanted)
        self.assertEqual(self.seen_versions, [2008])

    def test_unreachable_port(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        free_port = sock.getsockname()[1]
        sock.close()
        status, wanted, message = update_client.probe_server(
            "127.0.0.1", (0, 2, 8), port=free_port, timeout=0.5)
        self.assertEqual(status, "unreachable")
        self.assertIsNone(wanted)
        self.assertIsNone(message)

    def test_no_host_is_unreachable(self):
        status, _, _ = update_client.probe_server("", (0, 2, 8))
        self.assertEqual(status, "unreachable")

    def test_frame_parser_edges(self):
        with self.assertRaises(ValueError):
            update_client.parse_handshake_response(b"")          # 太短
        with self.assertRaises(ValueError):
            update_client.parse_handshake_response(b"\x01\x00\x00\x00")  # 非 FE
        with self.assertRaises(ValueError):
            update_client.parse_handshake_response(b"\xfe\x00\x40\x00")  # 帧不完整


# ---------------------------------------------------------------------------
#  manifest：结构、选目标、幂等合并
# ---------------------------------------------------------------------------

_DEFAULT = object()


def make_entry(version, sha="00" * 32, url=_DEFAULT, size=1, **extra):
    entry = {"version": version,
             "date": "2026-08-22",
             "url": (f"https://x/download/V{version}/p.zip"
                     if url is _DEFAULT else url),
             "size": size, "sha256": sha}
    entry.update(extra)
    return entry


class ManifestTests(unittest.TestCase):
    def test_validate_ok(self):
        manifest = {"format": 1, "repo": "https://x",
                    "releases": [make_entry("0.2.8"), make_entry("0.2.7")]}
        self.assertEqual(update_client.validate_manifest(manifest), manifest)

    def test_validate_rejects_broken(self):
        self.assertIsNone(update_client.validate_manifest(None))
        self.assertIsNone(update_client.validate_manifest({"releases": []}))
        self.assertIsNone(update_client.validate_manifest(
            {"releases": [make_entry("0.2.8", url=None)]}))
        self.assertIsNone(update_client.validate_manifest(
            {"releases": [{"version": "???", "url": "u", "sha256": "x"}]}))
        self.assertIsNone(update_client.validate_manifest(
            {"releases": [make_entry("0.2.8", sha=None)]}))

    def test_pick_target_prefers_server_wanted(self):
        manifest = {"releases": [make_entry("0.2.9"), make_entry("0.2.8"),
                                 make_entry("0.2.7")]}
        # 服务器点名 0.2.8 -> 取 0.2.8 而不是最新 0.2.9（成对发布）
        target = update_client.pick_target(manifest, (0, 2, 6), (0, 2, 8))
        self.assertEqual(target["version"], "0.2.8")

    def test_pick_target_local_already_wanted(self):
        manifest = {"releases": [make_entry("0.2.9"), make_entry("0.2.8")]}
        self.assertIsNone(
            update_client.pick_target(manifest, (0, 2, 8), (0, 2, 8)))

    def test_pick_target_wanted_missing_falls_back_to_latest(self):
        manifest = {"releases": [make_entry("0.2.9")]}
        target = update_client.pick_target(manifest, (0, 2, 6), (0, 2, 8))
        self.assertEqual(target["version"], "0.2.9")

    def test_pick_target_no_wanted_uses_latest(self):
        manifest = {"releases": [make_entry("0.2.9"), make_entry("0.2.8")]}
        target = update_client.pick_target(manifest, (0, 2, 8), None)
        self.assertEqual(target["version"], "0.2.9")
        self.assertIsNone(update_client.pick_target(manifest, (0, 2, 9), None))

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


# ---------------------------------------------------------------------------
#  下载校验（file:// 本地 URL，不碰网络）
# ---------------------------------------------------------------------------

class DownloadTests(unittest.TestCase):
    def make_source(self, tmp, content):
        path = os.path.join(tmp, "src.zip")
        with open(path, "wb") as f:
            f.write(content)
        return path, "file:///" + path.replace("\\", "/"), \
            hashlib.sha256(content).hexdigest()

    def test_download_and_verify_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, url, sha = self.make_source(tmp, b"hello-zip")
            dest = os.path.join(tmp, "dest.zip")
            update_client.download_file(url, dest, expected_sha256=sha,
                                        expected_size=9)
            with open(dest, "rb") as f:
                self.assertEqual(f.read(), b"hello-zip")

    def test_bad_sha_deletes_and_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, url, _ = self.make_source(tmp, b"hello-zip")
            dest = os.path.join(tmp, "dest.zip")
            with self.assertRaises(IOError):
                update_client.download_file(url, dest,
                                            expected_sha256="ff" * 32,
                                            expected_size=9)
            self.assertFalse(os.path.exists(dest))     # 坏包不留现场

    def test_bad_size_deletes_and_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, url, sha = self.make_source(tmp, b"hello-zip")
            dest = os.path.join(tmp, "dest.zip")
            with self.assertRaises(IOError):
                update_client.download_file(url, dest, expected_sha256=sha,
                                            expected_size=999)
            self.assertFalse(os.path.exists(dest))


# ---------------------------------------------------------------------------
#  玩家数据保护 + 应用流程
# ---------------------------------------------------------------------------

class ProtectionTests(unittest.TestCase):
    def test_protected_paths(self):
        for rel, expected in (
                ("server.config", True),
                (r"server.config", True),
                ("game_patched/UserConfig.ini", True),
                (r"game_patched\UserConfig.ini", True),
                ("server/data/accounts.json", True),
                ("logs", True),
                ("logs/bshook_1.log", True),
                ("logs/nested/deep.log", True),
                ("game_patched/Dump/x.dmp", True),
                ("game_patched/Debug/d.txt", True),
                ("game_patched/BigShot.rpt", True),
                ("BUILD.ver", False),
                ("hook/bin/bshook.dll", False),
                ("game_patched/BigShot.exe", False),
                ("tools/update_client.py", False),
                # 靠前缀混进来的伪装路径
                ("server.config.bak", False),
                ("logs2/x.log", False)):
            self.assertEqual(update_client.is_protected(rel), expected, rel)


class ApplyTests(unittest.TestCase):
    TOP = "PopShot-portable-win64_V0-2-8"

    def make_zip(self, tmp, entries):
        """entries: {相对路径: bytes}，zip 里带一层顶层目录（同打包脚本）。"""
        import zipfile
        zip_path = os.path.join(tmp, "update.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            for rel, content in entries.items():
                zf.writestr(f"{self.TOP}/{rel}", content)
        return zip_path

    def make_old_root(self, tmp):
        """玩家的旧包：有要被更新的文件，也有玩家自己的数据。"""
        root = os.path.join(tmp, "root")
        for rel, content in (
                ("BUILD.ver", b'{"version": "V0.2.7"}'),
                ("start.bat", b"old-start"),
                ("hook/bin/bshook.dll", b"old-dll"),
                ("game_patched/BigShot.exe", b"old-exe"),
                # ↓ 玩家数据：更新后必须原样
                ("server.config", b"server_address = players.server"),
                ("game_patched/UserConfig.ini", b"LastLoginId=player"),
                ("server/data/accounts.json", b'{"player": "x"}'),
                ("logs/x.log", b"player log"),
                ("game_patched/Dump/crash.dmp", b"dump")):
            path = os.path.join(root, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(content)
        return root

    def test_apply_updates_files_keeps_player_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_old_root(tmp)
            zip_path = self.make_zip(tmp, {
                "BUILD.ver": b'{"version": "V0.2.8"}',
                "start.bat": b"new-start",
                "hook/bin/bshook.dll": b"new-dll",
                "game_patched/BigShot.exe": b"new-exe",
                # 打包失误把玩家数据带进 zip 的极端情况也要被拦住
                "server.config": b"server_address = template",
                "game_patched/UserConfig.ini": b"template",
                "server/data/accounts.json": b'{"template": 1}',
                "logs/y.log": b"should-not-land",
            })
            update_client.apply_update(zip_path, root=root,
                                       on_progress=lambda *a: None)
            self.assertEqual(update_client.read_local_version(root), (0, 2, 8))
            with open(os.path.join(root, "start.bat"), "rb") as f:
                self.assertEqual(f.read(), b"new-start")
            with open(os.path.join(root, "hook", "bin", "bshook.dll"), "rb") as f:
                self.assertEqual(f.read(), b"new-dll")
            # 玩家数据原样
            for rel, want in (("server.config",
                               b"server_address = players.server"),
                              ("game_patched/UserConfig.ini",
                               b"LastLoginId=player"),
                              ("server/data/accounts.json", b'{"player": "x"}'),
                              ("logs/x.log", b"player log"),
                              ("game_patched/Dump/crash.dmp", b"dump")):
                with open(os.path.join(root, rel.replace("/", os.sep)), "rb") as f:
                    self.assertEqual(f.read(), want, rel)
            # zip 里的 logs/y.log 是「目录前缀保护」命中，不能落盘
            self.assertFalse(os.path.exists(os.path.join(root, "logs", "y.log")))

    def test_apply_requires_build_ver(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_old_root(tmp)
            zip_path = self.make_zip(tmp, {"start.bat": b"new"})
            with self.assertRaises(IOError):
                update_client.apply_update(zip_path, root=root,
                                           on_progress=lambda *a: None)
            # BUILD.ver 没动 —— 还是旧版本号，坏包不可能「上线」
            self.assertEqual(update_client.read_local_version(root), (0, 2, 7))

    def test_apply_corrupt_zip_keeps_build_ver(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_old_root(tmp)
            zip_path = os.path.join(tmp, "broken.zip")
            with open(zip_path, "wb") as f:
                f.write(b"this is not a zip file at all")
            with self.assertRaises(IOError):
                update_client.apply_update(zip_path, root=root,
                                           on_progress=lambda *a: None)
            self.assertEqual(update_client.read_local_version(root), (0, 2, 7))

    def test_apply_leaves_no_staging_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_old_root(tmp)
            zip_path = self.make_zip(tmp, {
                "BUILD.ver": b'{"version": "V0.2.8"}'})
            update_client.apply_update(zip_path, root=root,
                                       on_progress=lambda *a: None)
            leftovers = [n for n in os.listdir(root)
                         if n.startswith(".popshot-apply")]
            self.assertEqual(leftovers, [])


# ---------------------------------------------------------------------------
#  杂项：提权参数 / 单实例锁 / 停本机服务端的路径匹配 / 覆盖重试
# ---------------------------------------------------------------------------

class PackagePythonMatchTests(unittest.TestCase):
    def test_match_rules(self):
        root = os.path.join(os.sep, "game", "PopShot")
        yes = (os.path.join(root, "runtime", "python", "python.exe"),
               os.path.join(root, "runtime-win7", "python", "python.exe"),
               # 大小写 / 斜杠方向不敏感
               os.path.join(root, "Runtime", "Python", "PYTHON.EXE").replace(
                   os.sep, "/"))
        no = (os.path.join(os.sep, "dev", "runtime", "python", "python.exe"),  # 别的包
              os.path.join(root, "runtime", "python", "python3.dll"),          # 不是 python.exe
              os.path.join(root, "server", "app.py"),
              None, "")
        for path in yes:
            self.assertTrue(update_client.is_package_python_image(path, root),
                            path)
        for path in no:
            self.assertFalse(update_client.is_package_python_image(path, root),
                             path)


class CopyRetryTests(unittest.TestCase):
    def test_readonly_destination_still_replaced(self):
        # 玩家手贱设了只读位的目标文件也要能覆盖（chmod 兜底分支）
        with tempfile.TemporaryDirectory() as tmp:
            dst = os.path.join(tmp, "target.bin")
            with open(dst, "wb") as f:
                f.write(b"old")
            os.chmod(dst, 0o444)
            src = os.path.join(tmp, "src.bin")
            with open(src, "wb") as f:
                f.write(b"new")
            update_client.copy_with_retry(src, dst)
            with open(dst, "rb") as f:
                self.assertEqual(f.read(), b"new")

    def test_sweep_removes_leftover_update_old(self):
        # 上次更新「改名让位」的遗留：能删的删掉；logs/ 里的东西再像也不碰
        with tempfile.TemporaryDirectory() as tmp:
            stale = os.path.join(tmp, "runtime", "python")
            os.makedirs(stale)
            for name in ("python.exe.update_old", "python3.dll.update_old"):
                with open(os.path.join(stale, name), "wb") as f:
                    f.write(b"x")
            os.makedirs(os.path.join(tmp, "logs"))
            with open(os.path.join(tmp, "logs", "x.log.update_old"),
                      "wb") as f:
                f.write(b"player")
            removed = update_client.sweep_update_old(tmp,
                                                     on_progress=lambda *a: None)
            self.assertEqual(removed, 2)
            self.assertFalse(os.path.exists(
                os.path.join(stale, "python.exe.update_old")))
            self.assertTrue(os.path.exists(
                os.path.join(tmp, "logs", "x.log.update_old")))


class ElevateParamsTests(unittest.TestCase):
    def test_full_form(self):
        params = update_client.build_elevate_params(r"C:\t\new.zip", 1234,
                                                    script=r"C:\t\update_client.py")
        self.assertEqual(params,
                         r'"C:\t\update_client.py" --elevated'
                         r' --zip "C:\t\new.zip" --procid 1234')

    def test_minimal_form(self):
        params = update_client.build_elevate_params("", 0,
                                                    script="uc.py")
        self.assertEqual(params, '"uc.py" --elevated')


class SingleInstanceTests(unittest.TestCase):
    def test_lock_release_and_stale_takeover(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = update_client.SingleInstance(root=tmp)
            self.assertTrue(first.acquire())
            second = update_client.SingleInstance(root=tmp)
            self.assertFalse(second.acquire())         # 有一个在跑
            first.release()
            third = update_client.SingleInstance(root=tmp)
            self.assertTrue(third.acquire())
            third.release()

            # 上一次崩了没清的陈旧锁：直接抢
            os.makedirs(os.path.join(tmp, "logs"), exist_ok=True)
            stale = os.path.join(tmp, "logs", "update.lock")
            with open(stale, "w") as f:
                f.write("0")
            old = time_old_stamp()
            os.utime(stale, (old, old))
            fourth = update_client.SingleInstance(root=tmp)
            self.assertTrue(fourth.acquire())
            fourth.release()


def time_old_stamp():
    import time
    return time.time() - update_client.LOCK_STALE_S - 60


if __name__ == "__main__":
    unittest.main(verbosity=2)
