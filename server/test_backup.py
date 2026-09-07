#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据备份（`databackup.py`，管理页「数据备份」页，V0.3商店）。

全部用临时目录，不起 HTTP 服务器（接口那一层在 `test_web_admin`）。
`server.config` 的写回（`config.save_keys` / `ensure_keys`）也在这儿钉。
"""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

import config as server_config
import databackup
import logcleanup
import shopcfg
from account_store import AccountStore

#: 四份默认配置只生成一次（约半秒），之后每个用例复制一份。
_TEMPLATE = None


def _template_dir():
    global _TEMPLATE
    if _TEMPLATE is None:
        _TEMPLATE = tempfile.TemporaryDirectory()
        shopcfg.ensure_files(_TEMPLATE.name)
    return _TEMPLATE.name


def _read(path):
    with open(path, "rb") as fp:
        return fp.read()


class _BackupCase(unittest.TestCase):
    """一份临时 `server/data/`（四份配置 + 存档）+ 一份临时 `server.config`。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = os.path.join(self.tmp.name, "data")
        os.makedirs(self.data_dir)
        for filename in shopcfg.config_filenames():
            shutil.copyfile(os.path.join(_template_dir(), filename),
                            os.path.join(self.data_dir, filename))
        self.accounts = AccountStore(os.path.join(self.data_dir, "accounts.json"))
        self.accounts.ensure_item_fields()        # 建出默认管理员 admin
        self.config_path = os.path.join(self.tmp.name, "server.config")
        server_config.ensure_exists(self.config_path)
        self.lines = []
        self.svc = databackup.BackupService(
            data_dir=self.data_dir, config_path=self.config_path,
            accounts=self.accounts, log=self.lines.append)
        # `shopcfg.shop()` 之类要读**这份**目录，才能验「回滚后热重载读到旧值」。
        saved = shopcfg.DATA_DIR
        shopcfg.DATA_DIR = self.data_dir
        shopcfg.invalidate()
        self.addCleanup(shopcfg.invalidate)
        self.addCleanup(setattr, shopcfg, "DATA_DIR", saved)

    def backup_path(self, backup_id, *parts):
        return os.path.join(self.svc.backup_dir(), backup_id, *parts)

    def shop_price(self):
        table, warnings = shopcfg.shop()
        self.assertEqual([], warnings)
        item_id = sorted(table)[0]
        return item_id, table[item_id]["price"]

    def set_shop_price(self, item_id, price):
        path = os.path.join(self.data_dir, shopcfg.SHOP_FILENAME)
        raw = json.loads(_read(path).decode("utf-8"))
        for entry in raw["items"]:
            if entry["id"] == item_id:
                entry["price"] = price
        shopcfg.write_json(path, raw)
        shopcfg.invalidate()


class DataFilesTests(unittest.TestCase):
    def test_only_top_level_json_files_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("shop.json", "accounts.json", "new_thing.json",
                         "shop.json.bak-20260906-225552", "accounts.json.123.tmp",
                         ".hidden.json", "notes.txt"):
                with open(os.path.join(tmp, name), "w") as fp:
                    fp.write("{}")
            os.makedirs(os.path.join(tmp, "backups", "20260907-040000-auto"))
            with open(os.path.join(tmp, "backups", "x.json"), "w") as fp:
                fp.write("{}")
            self.assertEqual(["accounts.json", "new_thing.json", "shop.json"],
                             databackup.data_files(tmp))

    def test_a_missing_directory_is_just_empty(self):
        self.assertEqual([], databackup.data_files(
            os.path.join(tempfile.gettempdir(), "no-such-dir-for-backup")))


class IdAndLabelTests(unittest.TestCase):
    def test_ids_do_not_collide_within_a_second(self):
        now = time.mktime((2026, 9, 7, 4, 0, 0, 0, 0, -1))
        first = databackup.make_id("auto", now, set())
        self.assertEqual("20260907-040000-auto", first)
        second = databackup.make_id("auto", now, {first})
        self.assertEqual("20260907-040000-auto-2", second)
        third = databackup.make_id("auto", now, {first, second})
        self.assertEqual("20260907-040000-auto-3", third)
        # 正在建的 `.tmp-<id>` 也算占着这个名字。
        self.assertEqual("20260907-040000-auto-2", databackup.make_id(
            "auto", now, {databackup.TMP_PREFIX + first}))

    def test_ids_from_the_page_must_look_like_ours(self):
        for good in ("20260907-040000-auto", "20260907-235959-manual-12",
                     "20260907-000000-prerollback"):
            self.assertEqual(good, databackup.check_id(good))
        for bad in ("../accounts", "20260907-040000-weekly", "", None,
                    "20260907-040000-auto/..", "backups"):
            with self.assertRaises(databackup.BackupError):
                databackup.check_id(bad)

    def test_labels_are_single_line_and_capped(self):
        self.assertEqual("手动备份", databackup.clean_label("  ", "手动备份"))
        self.assertEqual("调价前 第二稿",
                         databackup.clean_label("调价前\n第二稿\t\x00"))
        self.assertEqual(databackup.LABEL_MAX,
                         len(databackup.clean_label("长" * 500)))


class CreateAndListTests(_BackupCase):
    def test_a_backup_copies_every_json_and_writes_a_manifest(self):
        manifest = self.svc.create("manual", "调价前", "admin")
        backup_id = manifest["id"]
        self.assertTrue(backup_id.endswith("-manual"))
        expected = sorted(shopcfg.config_filenames() + ["accounts.json"])
        self.assertEqual(expected, [f["name"] for f in manifest["files"]])
        for name in expected:
            self.assertEqual(_read(os.path.join(self.data_dir, name)),
                             _read(self.backup_path(backup_id, name)), name)
        on_disk = json.loads(_read(self.backup_path(backup_id, "manifest.json"))
                             .decode("utf-8"))
        self.assertEqual("调价前", on_disk["label"])
        self.assertEqual("admin", on_disk["created_by"])
        self.assertEqual("manual", on_disk["kind"])
        self.assertEqual(1, on_disk["format"])
        listed = self.svc.list_backups()
        self.assertEqual([backup_id], [entry["id"] for entry in listed])
        # 没有 `.tmp-*` 残留。
        self.assertEqual([backup_id], os.listdir(self.svc.backup_dir()))
        self.assertTrue(any("已备份" in line for line in self.lines))

    def test_a_failed_copy_leaves_no_half_backup(self):
        with mock.patch("databackup.shutil.copyfile",
                        side_effect=OSError(13, "拒绝访问。")):
            with self.assertRaises(OSError):
                self.svc.create("manual", "x", "admin")
        self.assertEqual([], os.listdir(self.svc.backup_dir()))
        self.assertEqual([], self.svc.list_backups())

    def test_newest_first_and_foreign_directories_are_ignored(self):
        base = time.mktime((2026, 9, 1, 4, 0, 0, 0, 0, -1))
        older = self.svc._create_unlocked("auto", None, "系统", now=base)
        newer = self.svc._create_unlocked("manual", "后来", "admin",
                                          now=base + 86400)
        root = self.svc.backup_dir()
        os.makedirs(os.path.join(root, "20260901-000000-manual"))      # 没 manifest
        os.makedirs(os.path.join(root, databackup.TMP_PREFIX + "20260901-000000-auto"))
        os.makedirs(os.path.join(root, "somebody-else"))
        with open(os.path.join(root, "20260901-000000-auto"), "w") as fp:
            fp.write("not a directory")
        self.assertEqual([newer["id"], older["id"]],
                         [entry["id"] for entry in self.svc.list_backups()])
        self.assertEqual("每日自动备份", older["label"])

    def test_describe_groups_config_files_together(self):
        manifest = self.svc.create("manual", "x", "admin")
        row = databackup.describe(manifest, databackup.data_files(self.data_dir))
        self.assertEqual(len(shopcfg.config_filenames()) + 1, row["file_count"])
        self.assertTrue(row["size"] > 0)
        self.assertEqual("手动", row["kind_zh"])
        keys = [group["key"] for group in row["groups"]]
        self.assertEqual(["config", "accounts"], keys)
        config, accounts = row["groups"]
        self.assertTrue(config["checked"])
        self.assertEqual(sorted(shopcfg.config_filenames()), sorted(config["files"]))
        self.assertIn("物品库", config["label"])
        self.assertIn("材料掉落", config["label"])
        self.assertFalse(accounts["checked"])
        self.assertEqual(["accounts.json"], accounts["files"])
        self.assertIn("管理员账号", accounts["warn"])

    def test_unknown_files_get_their_own_checkbox(self):
        with open(os.path.join(self.data_dir, "events.json"), "w") as fp:
            fp.write("{}")
        manifest = self.svc.create("manual", "x", "admin")
        os.remove(os.path.join(self.data_dir, "events.json"))
        row = databackup.describe(manifest, databackup.data_files(self.data_dir))
        extra = [g for g in row["groups"] if g["key"] == "events.json"][0]
        # 备份里有、现在没有 ⇒ 默认不勾，且说明为什么。
        self.assertFalse(extra["checked"])
        self.assertIn("没有这个文件", extra["warn"])


class CleanupTests(_BackupCase):
    def make(self, kind, days_ago):
        now = time.time() - days_ago * 86400
        return self.svc._create_unlocked(kind, None, "x", now=now)["id"]

    def test_backups_older_than_keep_days_go_regardless_of_kind(self):
        old_auto = self.make("auto", 8)
        old_manual = self.make("manual", 7.5)
        old_pre = self.make("prerollback", 30)
        fresh = self.make("manual", 6.5)
        removed, failed = self.svc.cleanup()
        self.assertEqual(sorted([old_auto, old_manual, old_pre]), sorted(removed))
        self.assertEqual([], failed)
        self.assertEqual([fresh], [entry["id"] for entry in self.svc.list_backups()])
        self.assertTrue(any("清掉 3 份" in line for line in self.lines))

    def test_zero_keep_days_never_deletes(self):
        server_config.save_keys(self.config_path, {"backup_keep_days": 0})
        ancient = self.make("auto", 3650)
        self.assertEqual(([], []), self.svc.cleanup())
        self.assertEqual([ancient], [entry["id"] for entry in self.svc.list_backups()])

    def test_leftover_tmp_directories_are_swept(self):
        root = self.svc.backup_dir()
        os.makedirs(os.path.join(root, databackup.TMP_PREFIX + "20260901-000000-auto"))
        os.makedirs(os.path.join(root, databackup.TMP_PREFIX + "del-20260901-000000-auto"))
        self.svc.cleanup()
        self.assertEqual([], os.listdir(root))

    def test_nothing_to_do_says_nothing(self):
        self.make("manual", 1)
        self.svc.cleanup()
        self.assertEqual([], [line for line in self.lines if "清掉" in line])


class RemoveTests(_BackupCase):
    def test_remove_deletes_the_directory(self):
        backup_id = self.svc.create("manual", "x", "admin")["id"]
        self.svc.remove(backup_id)
        self.assertEqual([], os.listdir(self.svc.backup_dir()))

    def test_remove_refuses_unknown_or_malformed_ids(self):
        with self.assertRaises(databackup.BackupError):
            self.svc.remove("20260101-000000-manual")
        with self.assertRaises(databackup.BackupError):
            self.svc.remove("../data")


class SettingsTests(_BackupCase):
    def test_defaults_come_from_the_template(self):
        self.assertEqual({"enabled": True, "time": "04:00", "keep_days": 7},
                         self.svc.settings())

    def test_update_writes_back_and_keeps_the_comments(self):
        settings, removed, failed = self.svc.update_settings(False, "5:07", 3)
        self.assertEqual({"enabled": False, "time": "05:07", "keep_days": 3}, settings)
        self.assertEqual(([], []), (removed, failed))
        text = _read(self.config_path).decode("utf-8")
        self.assertIn("backup_enabled = 0\n", text)
        self.assertIn("backup_time = 05:07\n", text)
        self.assertIn("backup_keep_days = 3\n", text)
        self.assertIn("远程服务器地址", text)          # 原注释一字未动
        self.assertEqual(settings, self.svc.settings())

    def test_bad_values_are_refused_before_touching_the_file(self):
        before = _read(self.config_path)
        for enabled, clock, days in ((True, "25:00", 7), (True, "四点", 7),
                                     (True, "04:00", -1), (True, "04:00", "七"),
                                     (True, "04:00", 99999)):
            with self.assertRaises(databackup.BackupError, msg=(clock, days)):
                self.svc.update_settings(enabled, clock, days)
        self.assertEqual(before, _read(self.config_path))

    def test_a_hand_edited_file_is_picked_up_on_the_next_read(self):
        self.svc.settings()
        with open(self.config_path, "a", encoding="utf-8") as fp:
            fp.write("\nbackup_time = 23:45\nbackup_keep_days = 1\n")
        self.assertEqual({"enabled": True, "time": "23:45", "keep_days": 1},
                         self.svc.settings())

    def test_a_shorter_retention_cleans_up_immediately(self):
        old = self.svc._create_unlocked("manual", "old", "x",
                                        now=time.time() - 4 * 86400)["id"]
        _settings, removed, _failed = self.svc.update_settings(True, "04:00", 3)
        self.assertEqual([old], removed)


class ScheduleTests(_BackupCase):
    def test_next_run_honours_the_minute(self):
        for hour, minute, expected_day in ((3, 0, 7), (4, 29, 7), (4, 30, 8), (23, 59, 8)):
            now = time.mktime((2026, 9, 7, hour, minute, 0, 0, 0, -1))
            landed = time.localtime(databackup.next_run_at(now, 4, 30))
            self.assertEqual((expected_day, 4, 30),
                             (landed.tm_mday, landed.tm_hour, landed.tm_min),
                             (hour, minute))

    def test_next_run_crosses_the_month(self):
        now = time.mktime((2026, 9, 30, 5, 0, 0, 0, 0, -1))
        landed = time.localtime(databackup.next_run_at(now, 4, 15))
        self.assertEqual((10, 1, 4, 15), (landed.tm_mon, landed.tm_mday,
                                          landed.tm_hour, landed.tm_min))

    def test_when_due_and_enabled_an_auto_backup_appears(self):
        manifest = self.svc.run_due_once()
        self.assertEqual("auto", manifest["kind"])
        self.assertEqual("每日自动备份", manifest["label"])
        self.assertEqual("系统", manifest["created_by"])
        status = self.svc.status()
        self.assertTrue(status["last_auto"]["ok"])
        self.assertEqual(manifest["id"], status["newest_auto"]["id"])
        self.assertIsNotNone(status["next_at"])

    def test_when_disabled_only_cleanup_runs(self):
        self.svc.update_settings(False, "04:00", 1)
        stale = self.svc._create_unlocked("manual", "old", "x",
                                          now=time.time() - 2 * 86400)["id"]
        self.assertIsNone(self.svc.run_due_once())
        self.assertEqual([], self.svc.list_backups())
        self.assertNotIn(stale, os.listdir(self.svc.backup_dir()))
        status = self.svc.status()
        self.assertFalse(status["enabled"])
        self.assertIsNone(status["next_at"])

    def test_a_failed_auto_backup_is_reported_not_raised(self):
        with mock.patch("databackup.shutil.copyfile",
                        side_effect=OSError(28, "磁盘满了")):
            self.assertIsNone(self.svc.run_due_once())
        status = self.svc.status()
        self.assertFalse(status["last_auto"]["ok"])
        self.assertIn("磁盘满了", status["last_auto"]["message"])

    def test_the_thread_starts_and_can_be_stopped(self):
        thread = self.svc.start()
        self.assertTrue(thread.is_alive())
        self.assertIs(thread, self.svc.start())        # 不会起第二条
        self.assertEqual("running", self.svc.status()["thread"])
        self.svc.stop()
        thread.join(10)
        self.assertFalse(thread.is_alive())

    def test_wake_makes_the_thread_reschedule_instead_of_running(self):
        # 醒来发现是被叫醒的（设置变了）就只重排，不该当成「到点了」去备份。
        ran = threading.Event()
        original = self.svc.run_due_once

        def spy(now=None):
            ran.set()
            return original(now)

        self.svc.run_due_once = spy
        thread = self.svc.start()
        self.svc.update_settings(True, "04:00", 7)      # 里面会 wake()
        self.svc.stop()
        thread.join(10)
        self.assertFalse(ran.is_set())
        self.assertEqual([], self.svc.list_backups())


class RestoreTests(_BackupCase):
    def config_files(self):
        return list(shopcfg.config_filenames())

    def test_restoring_the_config_group_takes_effect_without_a_restart(self):
        item_id, old_price = self.shop_price()
        before = self.svc.create("manual", "原价", "admin")
        self.set_shop_price(item_id, old_price + 1)
        self.assertEqual(old_price + 1, self.shop_price()[1])
        result = self.svc.restore(before["id"], self.config_files(),
                                  expect_admin="admin", created_by="admin")
        self.assertEqual(sorted(self.config_files()), sorted(result["restored"]))
        self.assertEqual([], result["failed"])
        self.assertEqual(old_price, self.shop_price()[1])
        # 回滚前留了一份，里面是回滚**前**的状态（改过价的那份）。
        pre = [entry for entry in self.svc.list_backups()
               if entry["kind"] == "prerollback"]
        self.assertEqual(1, len(pre))
        self.assertEqual(result["pre_backup_id"], pre[0]["id"])
        self.assertIn("回滚前自动备份", pre[0]["label"])
        self.assertIn("原价", pre[0]["label"])
        kept = json.loads(_read(self.backup_path(pre[0]["id"], "shop.json"))
                          .decode("utf-8"))
        prices = [e["price"] for e in kept["items"] if e["id"] == item_id]
        self.assertEqual([old_price + 1], prices)
        # 存档没被碰。
        self.assertNotIn("accounts.json", result["restored"])

    def test_restore_invalidates_the_hot_reload_cache(self):
        # 缓存键是 (mtime, size)；回滚回来的旧文件很可能大小一样。这条钉住
        # 「回滚必须清缓存」，不赌 mtime 的粒度。
        before = self.svc.create("manual", "x", "admin")
        with mock.patch.object(shopcfg, "invalidate") as invalidate:
            self.svc.restore(before["id"], self.config_files(), expect_admin="admin")
        self.assertTrue(invalidate.called)

    def test_the_config_group_cannot_be_split(self):
        before = self.svc.create("manual", "x", "admin")
        item_id, old_price = self.shop_price()
        self.set_shop_price(item_id, old_price + 1)
        with self.assertRaises(databackup.BackupError) as caught:
            self.svc.restore(before["id"], ["shop.json"], expect_admin="admin")
        self.assertIn("一起回滚", str(caught.exception))
        self.assertEqual(old_price + 1, self.shop_price()[1])       # 没动
        self.assertEqual(1, len(self.svc.list_backups()))          # 没留回滚前

    def test_a_backup_missing_one_config_file_restores_the_rest(self):
        before = self.svc.create("manual", "x", "admin")
        os.remove(self.backup_path(before["id"], shopcfg.DROPS_FILENAME))
        manifest_path = self.backup_path(before["id"], "manifest.json")
        manifest = json.loads(_read(manifest_path).decode("utf-8"))
        manifest["files"] = [f for f in manifest["files"]
                             if f["name"] != shopcfg.DROPS_FILENAME]
        shopcfg.write_json(manifest_path, manifest)
        row = databackup.describe(self.svc.list_backups()[0],
                                  databackup.data_files(self.data_dir))
        config = row["groups"][0]
        self.assertNotIn(shopcfg.DROPS_FILENAME, config["files"])
        self.assertIn("材料掉落", config["warn"])
        result = self.svc.restore(before["id"], config["files"], expect_admin="admin")
        self.assertEqual(sorted(config["files"]), sorted(result["restored"]))

    def test_a_corrupt_file_in_the_backup_refuses_the_whole_restore(self):
        before = self.svc.create("manual", "x", "admin")
        with open(self.backup_path(before["id"], "recipe.json"), "w") as fp:
            fp.write("{ oops")
        snapshot = dict((name, _read(os.path.join(self.data_dir, name)))
                        for name in databackup.data_files(self.data_dir))
        with self.assertRaises(databackup.BackupError) as caught:
            self.svc.restore(before["id"], self.config_files(), expect_admin="admin")
        self.assertIn("recipe.json", str(caught.exception))
        for name, raw in snapshot.items():
            self.assertEqual(raw, _read(os.path.join(self.data_dir, name)), name)
        self.assertEqual(1, len(self.svc.list_backups()))

    def test_an_old_format_backup_is_refused_by_todays_validator(self):
        before = self.svc.create("manual", "x", "admin")
        path = self.backup_path(before["id"], "recipe.json")
        raw = json.loads(_read(path).decode("utf-8"))
        raw["recipes"][0]["materials"] = [
            {"id": 10001, "count": 1}, {"id": 10002, "count": 1},
            {"id": 10003, "count": 1}, {"id": 10004, "count": 1},
            {"id": 20007, "count": 1}]              # 5 种 > UI 的 4 个槽
        shopcfg.write_json(path, raw)
        with self.assertRaises(databackup.BackupError) as caught:
            self.svc.restore(before["id"], self.config_files(), expect_admin="admin")
        self.assertIn("过不了现在的校验", str(caught.exception))
        self.assertEqual([], [e for e in self.svc.list_backups()
                              if e["kind"] == "prerollback"])

    def test_restoring_accounts_needs_the_admin_to_survive(self):
        before = self.svc.create("manual", "只有 admin", "admin")
        self.accounts.admin_add("carol", "SecretPw", "system")
        with self.assertRaises(databackup.BackupError) as caught:
            self.svc.restore(before["id"], ["accounts.json"], expect_admin="carol")
        self.assertIn("锁在管理页外面", str(caught.exception))
        self.assertIn("carol", self.accounts.admin_names())      # 没动

    def test_restoring_accounts_reports_back_while_holding_the_lock(self):
        before = self.svc.create("manual", "x", "admin")
        self.accounts.admin_add("carol", "SecretPw", "operator")
        seen = []

        def after_write(restored):
            seen.append(list(restored))
            # 还持着存档锁 —— 同线程再进存档层必须不卡死（RLock）。
            seen.append(self.accounts.admin_names())

        result = self.svc.restore(before["id"], ["accounts.json"],
                                  expect_admin="admin", after_write=after_write)
        self.assertEqual(["accounts.json"], result["restored"])
        self.assertEqual([["accounts.json"], ["admin"]], seen)
        self.assertEqual(["admin"], self.accounts.admin_names())

    def test_unknown_ids_and_files_are_refused(self):
        before = self.svc.create("manual", "x", "admin")
        with self.assertRaises(databackup.BackupError):
            self.svc.restore("20260101-000000-manual", ["shop.json"])
        with self.assertRaises(databackup.BackupError):
            self.svc.restore("../x", ["shop.json"])
        with self.assertRaises(databackup.BackupError):
            self.svc.restore(before["id"], ["nope.json"])
        with self.assertRaises(databackup.BackupError):
            self.svc.restore(before["id"], [])

    def test_a_busy_target_is_caught_before_anything_is_written(self):
        before = self.svc.create("manual", "x", "admin")
        real_open = open

        def busy_open(path, mode="r", *args, **kwargs):
            if mode == "r+b" and str(path).endswith("shop.json"):
                raise PermissionError(13, "拒绝访问。")
            return real_open(path, mode, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=busy_open):
            with self.assertRaises(databackup.BackupError) as caught:
                self.svc.restore(before["id"], self.config_files(), expect_admin="admin")
        self.assertIn("编辑器打开了它", str(caught.exception))
        self.assertEqual(1, len(self.svc.list_backups()))          # 没留回滚前


class ConfigWriteTests(unittest.TestCase):
    """`config.save_keys` / `ensure_keys` / `present_keys`：写回 `server.config`。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "server.config")

    def write(self, text, encoding="utf-8"):
        with open(self.path, "w", encoding=encoding, newline="") as fp:
            fp.write(text)

    def test_present_keys_sees_only_what_is_written(self):
        self.assertEqual({"local_register_port", "udp_sync"},
                         server_config.present_keys(
                             "# 注释\n\nLOCAL_REGISTER_PORT = 1\n; x\nudp_sync=0\nnonsense\n"))

    def test_replacing_keeps_every_other_line(self):
        self.write("# 顶上的注释\nlocal_register_port = 27810\n\n"
                   "# 备份\nbackup_time = 04:00\nbackup_keep_days = 7\n")
        replaced, appended = server_config.save_keys(
            self.path, {"backup_time": "5:07", "backup_keep_days": 3})
        self.assertEqual((["backup_keep_days", "backup_time"], []),
                         (replaced, appended))
        self.assertEqual(
            "# 顶上的注释\nlocal_register_port = 27810\n\n"
            "# 备份\nbackup_time = 05:07\nbackup_keep_days = 3\n",
            _read(self.path).decode("utf-8"))

    def test_missing_keys_are_appended_with_their_comments(self):
        self.write("local_register_port = 27810")          # 末尾连换行都没有
        _replaced, appended = server_config.save_keys(
            self.path, {"backup_enabled": 0})
        self.assertEqual(["backup_enabled"], appended)
        text = _read(self.path).decode("utf-8")
        self.assertTrue(text.startswith("local_register_port = 27810\n\n# "))
        self.assertTrue(text.endswith("\nbackup_enabled = 0\n"))
        self.assertIn("数据自动备份", text)
        values, warnings = server_config.load(self.path)
        self.assertEqual(0, values["backup_enabled"])
        self.assertEqual([], warnings)

    def test_duplicate_keys_are_all_replaced(self):
        # `parse_text` 取最后一次；只换一处会被另一处顶掉。
        self.write("backup_time = 01:00\n# ...\nbackup_time = 02:00\n")
        server_config.save_keys(self.path, {"backup_time": "03:00"})
        self.assertEqual("backup_time = 03:00\n# ...\nbackup_time = 03:00\n",
                         _read(self.path).decode("utf-8"))

    def test_crlf_and_bom_files_keep_crlf_and_lose_the_bom(self):
        self.write("﻿# 记事本存的\r\nudp_sync = 1\r\n", encoding="utf-8")
        server_config.save_keys(self.path, {"udp_sync": 0, "backup_time": "06:00"})
        raw = _read(self.path)
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))     # 全是 CRLF
        text = raw.decode("utf-8")
        self.assertIn("# 记事本存的\r\nudp_sync = 0\r\n", text)
        self.assertTrue(text.endswith("backup_time = 06:00\r\n"))

    def test_unknown_keys_are_refused(self):
        self.write("udp_sync = 1\n")
        with self.assertRaises(ValueError):
            server_config.save_keys(self.path, {"made_up": 1})

    def test_ensure_keys_is_idempotent(self):
        self.write("local_register_port = 27810\n")
        self.assertEqual(["backup_enabled", "backup_time", "backup_keep_days"],
                         server_config.ensure_keys(self.path))
        after_first = _read(self.path)
        values, warnings = server_config.load(self.path)
        self.assertEqual([], warnings)
        self.assertEqual(("04:00", 1, 7), (values["backup_time"],
                                           values["backup_enabled"],
                                           values["backup_keep_days"]))
        with mock.patch("config._write_text_atomic") as writer:
            self.assertEqual([], server_config.ensure_keys(self.path))
        self.assertFalse(writer.called)
        self.assertEqual(after_first, _read(self.path))

    def test_the_template_needs_nothing_added(self):
        server_config.ensure_exists(self.path)
        self.assertEqual([], server_config.ensure_keys(self.path))

    def test_format_value_round_trips_through_parse(self):
        self.assertEqual("1", server_config.format_value("backup_enabled", True))
        self.assertEqual("0", server_config.format_value("backup_enabled", "off"))
        self.assertEqual("04:05", server_config.format_value("backup_time", "4:5"))
        self.assertEqual("7", server_config.format_value("backup_keep_days", 7))


class DailyMinuteTests(unittest.TestCase):
    def test_seconds_until_daily_accepts_a_minute(self):
        now = time.mktime((2026, 9, 7, 4, 10, 0, 0, 0, -1))
        landed = time.localtime(now + logcleanup.seconds_until_daily(now, 4, 30))
        self.assertEqual((7, 4, 30), (landed.tm_mday, landed.tm_hour, landed.tm_min))
        landed = time.localtime(now + logcleanup.seconds_until_daily(now, 4, 5))
        self.assertEqual((8, 4, 5), (landed.tm_mday, landed.tm_hour, landed.tm_min))


if __name__ == "__main__":
    unittest.main()
