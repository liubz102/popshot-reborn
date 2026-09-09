#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端崩溃日志上传 —— **接收**那一半（`crashstore.py` + `/api/crash-report`）。

发送那一半在 `test_crashwatch.py`。

这里钉住的五件事：

1. **目录名是客户端传上来的、直接拼进路径** ⇒ 恶意 / 畸形的一律 400，
   而且磁盘上**什么都不许建出来**；
2. **大小写撞名**要真的处理 —— 云服是 Linux，`os.path.exists` 判不出来；
3. 单包大小上限、按 IP 频率限制、sha256 校验；
4. **原子落位**：要么没有这个目录，要么它连回执一起齐全，没有中间态；
5. 按天清理，以及「收到一半就断了」的残骸永远该清。
"""
import http.client
import io
import json
import os
import tempfile
import threading
import time
import unittest

import account_store
import config as server_config
import crashstore
from web import server as web_server


class CheckIdTests(unittest.TestCase):
    """★ 这个字符串会直接拼进路径。**只校验，不清洗。**"""

    GOOD = ("testuser1_a1b2c3d4_20260909-013642",
            "a_00000000_20200101-000000",
            "A-b_C_ffffffff_20261231-235959",
            "CON_a1b2c3d4_20260909-013642",       # 保留名带了后缀就不是保留名
            "unknown_a1b2c3d4_20260909-013642")

    BAD = ("../../etc/passwd",
           "..%2F..%2Fetc",
           "../a_a1b2c3d4_20260909-013642",
           "a/b_a1b2c3d4_20260909-013642",
           "a\\b_a1b2c3d4_20260909-013642",
           "CON",
           "小明_a1b2c3d4_20260909-013642",
           "  ab  _a1b2c3d4_20260909-013642",
           "ab._a1b2c3d4_20260909-013642",
           "a" * 17 + "_a1b2c3d4_20260909-013642",   # 账号名超长
           "ab_a1b2c3d4_20260909-013642-2",          # 撞名后缀是服务端加的
           "ab_A1B2C3D4_20260909-013642",            # 安装码要小写十六进制
           "ab_a1b2c3d_20260909-013642",             # 安装码只有 7 位
           "ab_a1b2c3d4_2026099-013642",             # 日期位数不对
           "ab_a1b2c3d4",
           "",
           None,
           123)

    def test_good_ids_pass(self):
        for crash_id in self.GOOD:
            self.assertEqual(crash_id, crashstore.check_id(crash_id))

    def test_bad_ids_are_rejected_with_400(self):
        for crash_id in self.BAD:
            with self.assertRaises(crashstore.CrashUploadError,
                                   msg=repr(crash_id)) as caught:
                crashstore.check_id(crash_id)
            self.assertEqual(400, caught.exception.status)


class PickNameTests(unittest.TestCase):
    """大小写撞名 —— `os.path.exists` 在 Linux 上判不出来，必须显式比。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def test_no_collision_keeps_the_original_name(self):
        self.assertEqual("abc_a1b2c3d4_20260909-013642",
                         crashstore.pick_name(self.dir,
                                              "abc_a1b2c3d4_20260909-013642"))

    def test_case_insensitive_collision_gets_a_suffix_on_the_account(self):
        os.makedirs(os.path.join(self.dir, "Abc_a1b2c3d4_20260909-013642"))
        # ★ 后缀加在**账号名那一段后面**：撞名的成因就是账号名，标在那里一眼
        #   看得懂；排序时两条仍然挨着，不破坏「同一客户端聚在一起」。
        self.assertEqual("abc-2_a1b2c3d4_20260909-013642",
                         crashstore.pick_name(self.dir,
                                              "abc_a1b2c3d4_20260909-013642"))

    def test_suffix_keeps_climbing(self):
        for name in ("Abc_a1b2c3d4_20260909-013642",
                     "abc-2_a1b2c3d4_20260909-013642"):
            os.makedirs(os.path.join(self.dir, name))
        self.assertEqual("abc-3_a1b2c3d4_20260909-013642",
                         crashstore.pick_name(self.dir,
                                              "abc_a1b2c3d4_20260909-013642"))

    def test_a_half_written_tmp_dir_also_counts_as_taken(self):
        os.makedirs(os.path.join(self.dir, crashstore.TMP_PREFIX
                                 + "abc_a1b2c3d4_20260909-013642"))
        self.assertEqual("abc-2_a1b2c3d4_20260909-013642",
                         crashstore.pick_name(self.dir,
                                              "abc_a1b2c3d4_20260909-013642"))

    def test_the_suffixed_name_is_still_a_legal_directory_name(self):
        # 服务端自己加的后缀不必过 ID_RE（那条只管客户端传上来的），
        # 但它必须仍然是个能建出来的目录名。
        os.makedirs(os.path.join(self.dir, "Abc_a1b2c3d4_20260909-013642"))
        name = crashstore.pick_name(self.dir, "abc_a1b2c3d4_20260909-013642")
        os.makedirs(os.path.join(self.dir, name))
        self.assertTrue(os.path.isdir(os.path.join(self.dir, name)))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, crashstore.DIRNAME)
        self.lines = []
        self.store = crashstore.Store(directory=self.dir,
                                      log=self.lines.append)

    def put(self, crash_id, payload=b"PK\x03\x04zip"):
        tmp, name = self.store.begin(crash_id)
        with open(os.path.join(tmp, crash_id + ".zip"), "wb") as fp:
            fp.write(payload)
        return tmp, name

    def test_place_is_atomic(self):
        tmp, name = self.put("abc_a1b2c3d4_20260909-013642")
        # 落位之前：只有半成品，正式目录还不存在。
        self.assertTrue(os.path.isdir(tmp))
        self.assertFalse(os.path.isdir(os.path.join(self.dir, name)))
        target = self.store.place_one(tmp, name, {"bytes": 7, "from": "1.2.3.4"})
        self.assertFalse(os.path.exists(tmp))
        self.assertEqual(sorted(os.listdir(target)),
                         ["abc_a1b2c3d4_20260909-013642.zip", "receipt.json"])

    def test_receipt_is_written_before_the_rename(self):
        """要么没有这个目录，要么它连回执一起齐全 —— 没有中间态。"""
        tmp, name = self.put("abc_a1b2c3d4_20260909-013642")
        target = self.store.place_one(tmp, name, {"bytes": 7, "from": "ip"})
        with open(os.path.join(target, "receipt.json"), encoding="utf-8") as fp:
            got = json.load(fp)
        self.assertEqual("ip", got["from"])

    def test_abandon_leaves_nothing_behind(self):
        tmp, _name = self.put("abc_a1b2c3d4_20260909-013642")
        self.store.abandon(tmp)
        self.assertEqual([], os.listdir(self.dir))

    def test_begin_rejects_a_bad_id_before_touching_the_disk(self):
        with self.assertRaises(crashstore.CrashUploadError):
            self.store.begin("../../etc/passwd")
        self.assertFalse(os.path.exists(self.dir))

    def test_a_stale_half_written_dir_is_thrown_away_not_appended_to(self):
        crash_id = "abc_a1b2c3d4_20260909-013642"
        tmp, _ = self.store.begin(crash_id)
        with open(os.path.join(tmp, "junk"), "wb") as fp:
            fp.write("上一次收到一半就断了".encode("utf-8"))
        # 同一个 id 再来一次：`pick_name` 看见 `.tmp-` 就换名字，
        # 所以这次拿到的是 `-2`，上次那份残骸留给清理。
        tmp2, name2 = self.store.begin(crash_id)
        self.assertNotEqual(tmp, tmp2)
        self.assertTrue(name2.startswith("abc-2_"))

    def test_queue_full_drops_instead_of_blocking(self):
        """★ 绝不反压到请求线程：上传把同进程的游戏拖住比丢一份包严重得多。"""
        store = crashstore.Store(directory=self.dir, log=self.lines.append)
        store._queue = __import__("queue").Queue(1)
        tmp1, name1 = self.put("abc_a1b2c3d4_20260909-013642")
        tmp2, name2 = self.put("abd_a1b2c3d4_20260909-013642")
        self.assertTrue(store.submit(tmp1, name1, {}))
        self.assertFalse(store.submit(tmp2, name2, {}))
        self.assertFalse(os.path.exists(tmp2))      # 丢掉的那份不留垃圾

    def test_background_thread_places_what_is_submitted(self):
        self.store.start()
        self.addCleanup(self.store.stop)
        tmp, name = self.put("abc_a1b2c3d4_20260909-013642")
        self.assertTrue(self.store.submit(tmp, name,
                                          {"bytes": 7, "from": "1.2.3.4"}))
        self.store.stop()
        self.assertTrue(os.path.isdir(os.path.join(self.dir, name)))
        self.assertTrue(any("已保存" in line for line in self.lines),
                        self.lines)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, crashstore.DIRNAME)
        os.makedirs(self.dir)

    def make(self, name, days_ago=0.0, size=16):
        path = os.path.join(self.dir, name)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "x.zip"), "wb") as fp:
            fp.write(b"x" * size)
        when = time.time() - days_ago * 86400
        os.utime(path, (when, when))
        return path

    def test_only_directories_older_than_keep_days_go(self):
        self.make("a_a1b2c3d4_20260101-000000", days_ago=9)
        self.make("b_a1b2c3d4_20260909-000000", days_ago=1)
        store = crashstore.Store(directory=self.dir, keep_days=3)
        removed, freed = store.cleanup()
        self.assertEqual(1, removed)
        self.assertEqual(16, freed)
        self.assertEqual(["b_a1b2c3d4_20260909-000000"],
                         os.listdir(self.dir))

    def test_keep_days_zero_keeps_everything(self):
        self.make("a_a1b2c3d4_20260101-000000", days_ago=999)
        store = crashstore.Store(directory=self.dir, keep_days=0)
        self.assertEqual((0, 0), store.cleanup())
        self.assertEqual(1, len(os.listdir(self.dir)))

    def test_half_written_leftovers_are_always_swept(self):
        """★ 残骸和「保留几天证据」无关 —— 关掉保留期也照清。"""
        self.make(crashstore.TMP_PREFIX + "a_a1b2c3d4_20260101-000000")
        store = crashstore.Store(directory=self.dir, keep_days=0)
        removed, _ = store.cleanup()
        self.assertEqual(1, removed)
        self.assertEqual([], os.listdir(self.dir))

    def test_missing_directory_is_not_an_error(self):
        store = crashstore.Store(directory=os.path.join(self.tmp.name, "nope"))
        self.assertEqual((0, 0), store.cleanup())


class UploadEndpointTests(unittest.TestCase):
    """走真的 HTTP：`/api/crash-report` 的行为。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, crashstore.DIRNAME)
        self.store = crashstore.Store(directory=self.dir)
        self.store.start()
        self.addCleanup(self.store.stop)
        accounts = account_store.AccountStore(
            os.path.join(self.tmp.name, "accounts.json"))
        self.httpd = web_server.make_server(
            0, accounts, host="127.0.0.1", crash=self.store,
            crash_max_mb=1, crash_cooldown=0)
        self.port = self.httpd.server_address[1]
        # `serve_forever` 默认 0.5 秒才看一次退出标志，照默认值走光关服务器
        # 就要等半天（和 `test_web_admin.py` 同一个理由）。
        threading.Thread(
            target=lambda: self.httpd.serve_forever(poll_interval=0.02),
            daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def post(self, crash_id, body=b"PK\x03\x04zip", sha=None, headers=None):
        import hashlib

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        head = {"Content-Type": "application/zip",
                "Content-Length": str(len(body)),
                "X-Crash-Client": crash_id,
                "X-Crash-Time": "09/09/26, 01:36:42"}
        if sha is None:
            sha = hashlib.sha256(body).hexdigest()
        if sha:
            head["X-Crash-Sha256"] = sha
        head.update(headers or {})
        try:
            conn.request("POST", "/api/crash-report", body=body, headers=head)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read().decode("utf-8"))
        finally:
            conn.close()

    def settle(self):
        """等后台落位线程把队列吃干净。"""
        self.store.stop()
        self.store.start()

    def test_a_good_upload_lands_on_disk(self):
        status, got = self.post("abc_a1b2c3d4_20260909-013642")
        self.assertEqual(200, status)
        self.assertTrue(got["ok"])
        self.settle()
        target = os.path.join(self.dir, "abc_a1b2c3d4_20260909-013642")
        self.assertEqual(
            sorted(os.listdir(target)),
            ["abc_a1b2c3d4_20260909-013642.zip", "receipt.json"])
        with open(os.path.join(target, "receipt.json"), encoding="utf-8") as fp:
            receipt = json.load(fp)
        self.assertEqual(len(b"PK\x03\x04zip"), receipt["bytes"])
        self.assertTrue(receipt["sha256_verified"])
        self.assertEqual("09/09/26, 01:36:42", receipt["crash_time_text"])

    def test_the_zip_is_stored_byte_for_byte_and_not_unpacked(self):
        body = os.urandom(4096)
        self.post("abc_a1b2c3d4_20260909-013642", body=body)
        self.settle()
        path = os.path.join(self.dir, "abc_a1b2c3d4_20260909-013642",
                            "abc_a1b2c3d4_20260909-013642.zip")
        with open(path, "rb") as fp:
            self.assertEqual(body, fp.read())

    def test_malicious_ids_are_refused_and_nothing_is_created(self):
        for crash_id in CheckIdTests.BAD:
            if not isinstance(crash_id, str):
                continue                    # HTTP 头只能是字符串
            if not crash_id.isascii():
                # 非 ASCII 根本进不了 HTTP 头（`check_id` 那组用例管这一类）。
                continue
            status, got = self.post(crash_id)
            self.assertEqual(400, status, crash_id)
            self.assertFalse(got["ok"])
        self.settle()
        # ★ 一个目录都不许建出来（`logs_client_crash/` 自己可以在，但必须是空的）。
        self.assertEqual([], os.listdir(self.dir) if os.path.isdir(self.dir)
                         else [])

    def test_a_traversal_id_cannot_write_outside_the_crash_dir(self):
        self.post("../../pwned_a1b2c3d4_20260909-013642")
        self.settle()
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "pwned")))
        self.assertFalse(os.path.exists(
            os.path.join(os.path.dirname(self.tmp.name), "pwned")))

    def test_too_big_is_refused_with_413(self):
        # 只超一点点：服务端会把「已经在路上」的那部分读掉（上限 = 它本来就
        # 愿意收的那个大小），对方因此能真正读到这一发 413，而不是看到
        # 一个连接错误。上限 1 MB，这里发 1 MB + 4 KB。
        status, got = self.post("abc_a1b2c3d4_20260909-013642",
                                body=b"x" * (1048576 + 4096))
        self.assertEqual(413, status)
        self.assertIn("太大", got["message"])
        self.settle()
        self.assertEqual([], os.listdir(self.dir) if os.path.isdir(self.dir)
                         else [])

    def test_a_broken_checksum_is_refused_and_leaves_nothing(self):
        status, got = self.post("abc_a1b2c3d4_20260909-013642",
                                sha="0" * 64)
        self.assertEqual(400, status)
        self.assertIn("校验和", got["message"])
        self.settle()
        self.assertEqual([], os.listdir(self.dir))

    def test_missing_content_length_is_refused(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", "/api/crash-report", body=b"",
                         headers={"X-Crash-Client":
                                  "abc_a1b2c3d4_20260909-013642"})
            resp = conn.getresponse()
            self.assertEqual(400, resp.status)
        finally:
            conn.close()

    def test_the_1mb_global_body_cap_does_not_apply_here(self):
        """★ 注册页那条 `MAX_BODY_BYTES = 1 MB` 不该把崩溃包挡掉。"""
        self.assertLess(web_server.MAX_BODY_BYTES, 1048576 + 1)
        big = os.urandom(900 * 1024)        # 比 MAX_BODY_BYTES 的用途大得多
        status, _ = self.post("abc_a1b2c3d4_20260909-013642", body=big)
        self.assertEqual(200, status)


class UploadDisabledTests(unittest.TestCase):
    def test_no_store_means_403(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        accounts = account_store.AccountStore(
            os.path.join(tmp.name, "accounts.json"))
        httpd = web_server.make_server(0, accounts, host="127.0.0.1")
        self.addCleanup(httpd.server_close)
        port = httpd.server_address[1]
        threading.Thread(
            target=lambda: httpd.serve_forever(poll_interval=0.02),
            daemon=True).start()
        self.addCleanup(httpd.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("POST", "/api/crash-report", body=b"x",
                         headers={"Content-Length": "1",
                                  "X-Crash-Client":
                                  "abc_a1b2c3d4_20260909-013642"})
            self.assertEqual(403, conn.getresponse().status)
        finally:
            conn.close()


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = crashstore.Store(
            directory=os.path.join(self.tmp.name, crashstore.DIRNAME))
        self.store.start()
        self.addCleanup(self.store.stop)
        accounts = account_store.AccountStore(
            os.path.join(self.tmp.name, "accounts.json"))
        self.httpd = web_server.make_server(
            0, accounts, host="127.0.0.1", crash=self.store,
            crash_max_mb=8, crash_cooldown=60)
        self.port = self.httpd.server_address[1]
        # `serve_forever` 默认 0.5 秒才看一次退出标志，照默认值走光关服务器
        # 就要等半天（和 `test_web_admin.py` 同一个理由）。
        threading.Thread(
            target=lambda: self.httpd.serve_forever(poll_interval=0.02),
            daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def post(self, crash_id):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", "/api/crash-report", body=b"PK",
                         headers={"Content-Length": "2",
                                  "X-Crash-Client": crash_id})
            resp = conn.getresponse()
            resp.read()
            return resp.status
        finally:
            conn.close()

    def test_the_second_upload_from_one_ip_is_throttled(self):
        self.assertEqual(200, self.post("abc_a1b2c3d4_20260909-013642"))
        self.assertEqual(429, self.post("abc_a1b2c3d4_20260909-013643"))

    def test_a_refused_upload_does_not_start_the_cooldown(self):
        """★ 极性和注册页一致：被拒的那一发不该把人锁住。"""
        self.assertEqual(400, self.post("../../etc/passwd"))
        self.assertEqual(200, self.post("abc_a1b2c3d4_20260909-013642"))


class LayoutTests(unittest.TestCase):
    """落地目录的位置 —— **客户端包和服务端包必须是同一个相对位置**。

    客户端包里也带着完整的 `server/`，可以当服务器让别人连（铁律 8：两个包是
    同一套代码）。所以「别人传上来的崩溃包落在哪」这件事不能依赖包的种类，
    只能依赖「`server/` 的上一级」。
    """

    def test_the_crash_dir_sits_next_to_logs_at_the_package_root(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(
            crashstore.__file__)))
        self.assertEqual(os.path.join(root, "logs_client_crash"),
                         crashstore.DEFAULT_CRASH_DIR)
        # `logs/` 就在旁边 —— 两个包的这一层布局是一样的。
        import logcleanup

        self.assertEqual(os.path.dirname(logcleanup.DEFAULT_LOGDIR),
                         os.path.dirname(crashstore.DEFAULT_CRASH_DIR))

    def test_nothing_is_created_just_by_importing_or_starting(self):
        """★ 打包自检会把包里的服务端跑一遍：**不该因此在包里长出目录**。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        target = os.path.join(tmp.name, crashstore.DIRNAME)
        store = crashstore.Store(directory=target, keep_days=3)
        store.start()
        self.addCleanup(store.stop)
        store.cleanup()
        self.assertFalse(os.path.exists(target))


class ConfigTests(unittest.TestCase):
    def test_the_shipped_template_carries_the_crash_keys(self):
        values, warnings = server_config.parse_text(
            server_config.DEFAULT_CONFIG_TEXT)
        self.assertEqual([], warnings)
        for key in ("crash_upload", "crash_max_upload_mb",
                    "crash_upload_cooldown_seconds", "crash_keep_days"):
            self.assertEqual(server_config.DEFAULTS[key], values[key], key)

    def test_bad_values_fall_back_to_defaults_instead_of_failing(self):
        values, warnings = server_config.parse_text(
            "crash_upload = 也许\ncrash_max_upload_mb = 99999\n"
            "crash_keep_days = -1\n")
        self.assertEqual(3, len(warnings))
        self.assertEqual(server_config.DEFAULT_CRASH_UPLOAD,
                         values["crash_upload"])
        self.assertEqual(server_config.DEFAULT_CRASH_MAX_UPLOAD_MB,
                         values["crash_max_upload_mb"])
        self.assertEqual(server_config.DEFAULT_CRASH_KEEP_DAYS,
                         values["crash_keep_days"])

    def test_zero_is_a_meaningful_value_everywhere(self):
        values, warnings = server_config.parse_text(
            "crash_upload = 0\ncrash_max_upload_mb = 0\n"
            "crash_upload_cooldown_seconds = 0\ncrash_keep_days = 0\n")
        self.assertEqual([], warnings)
        self.assertEqual(0, values["crash_upload"])
        self.assertEqual(0, values["crash_max_upload_mb"])
        self.assertEqual(0, values["crash_upload_cooldown_seconds"])
        self.assertEqual(0, values["crash_keep_days"])


if __name__ == "__main__":
    unittest.main()
