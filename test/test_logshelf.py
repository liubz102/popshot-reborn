#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「待下载 7z 包」（`server/logshelf.py`，X17 / D101）：后台压缩、进度、删除、名字校验；
压缩等级（D102）：必填、传到包里、默认按总内存、开压前看可用内存、`MemoryError` 说人话。

★ 全部事件驱动：等「压完 / 进度变了」靠 `LogShelf.wait()` 的条件变量；要让后台线程停在
  某一步，就让假的 `write_7z` 等一个 `threading.Event`（测试自己放行）。固定秒数只当保险丝。
★ 内存一律用假的（`self.total` / `self.free`）：真机器有多少内存不该决定用例红不红。
  排队 / 进度这些和档位无关的用例压低档（快、只要 9 MB）。
"""
import io
import os
import tempfile
import threading
import time
import unittest

import logpack
import logshelf
import sevenzip
from testsupport import read_7z

#: 等后台线程的**保险丝**（秒）。★ 不是判据：判据是版本号变了 / 线程退了；
#: 这个数只防真出毛病（死锁、线程没起来）时测试永远不回来。
FUSE_S = 60.0


@unittest.skipUnless(sevenzip.AVAILABLE, "这个 Python 没带 lzma")
class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logdir = os.path.join(self.tmp.name, "logs")
        self.crash = os.path.join(self.tmp.name, "logs_client_crash")
        self.shelf_dir = os.path.join(self.tmp.name, "logs_7z")
        os.makedirs(self.logdir)
        os.makedirs(self.crash)
        self.packer = logpack.LogPacker(self.logdir, self.crash)
        self.lines = []
        self.total = 4 << 30                    # 总内存（默认档按它）
        self.free = None                        # 现在可用（None = 查不到，开压前那一关不拦）
        self.shelf = logshelf.LogShelf(self.packer, self.shelf_dir, audit=self.lines.append,
                                       memory=lambda: self.total, available=lambda: self.free)
        # ★ 后进先出：stop 排在 tmp.cleanup 之前，线程先退、文件先关，Windows 才删得掉目录。
        self.addCleanup(self.shelf.stop, FUSE_S)

    def log(self, name, data=b"line\n" * 200):
        with open(os.path.join(self.logdir, name), "wb") as fp:
            fp.write(data)

    def settle(self):
        """等到没活了，返回最后那份快照。"""
        snap = self.shelf.snapshot()
        while snap["busy"]:
            newer = self.shelf.wait(snap["version"], timeout=FUSE_S)
            if newer["version"] == snap["version"]:
                self.fail("后台压缩 %g 秒没有任何进展 —— 这是死锁 / 线程没起来，"
                          "不是「压得慢」" % FUSE_S)
            snap = newer
        return snap

    def wait_until(self, predicate):
        """等到快照满足 `predicate`（事件驱动：每次版本变了才看一眼）。"""
        snap = self.shelf.snapshot()
        while not predicate(snap):
            newer = self.shelf.wait(snap["version"], timeout=FUSE_S)
            if newer["version"] == snap["version"]:
                self.fail("等了 %g 秒快照都没变（保险丝）" % FUSE_S)
            snap = newer
        return snap

    def gate(self, before=None):
        """把 `write_7z` 换成「先停在闸门上、放行后再真压」。返回闸门（`set()` 放行）。"""
        opened = threading.Event()
        real = self.packer.write_7z

        def gated(fp, plan, level, meta=None, progress=None):
            if before is not None:
                before(plan, progress)
            if not opened.wait(FUSE_S):
                raise RuntimeError("闸门 %g 秒没被放行（保险丝）" % FUSE_S)
            return real(fp, plan, level, meta=meta, progress=progress)

        self.packer.write_7z = gated
        self.addCleanup(opened.set)            # 用例半路失败也别把后台线程卡死
        return opened


class SubmitTests(_Case):
    def test_nothing_exists_until_the_first_job(self):
        snap = self.shelf.snapshot()
        self.assertEqual(([], [], False), (snap["files"], snap["jobs"], snap["busy"]))
        self.assertEqual("logs_7z", snap["dirname"])
        # ★ 打包自检会把包里的服务端跑一遍：光是看一眼不许凭空建出 logs_7z/。
        self.assertFalse(os.path.exists(self.shelf_dir))

    def test_a_job_packs_in_the_background_and_the_file_appears(self):
        self.log("server.out")
        self.log("server-boot.err", b"")
        job, new = self.shelf.submit("server", "all", level=sevenzip.HIGH,
                                     meta={"by": "admin"}, who="'admin'")
        self.assertTrue(new)
        snap = self.settle()
        self.assertEqual([], snap["jobs"])
        (item,) = snap["files"]
        self.assertEqual(job.name, item["name"])
        self.assertEqual(job.plan.stem + ".7z", item["name"])
        self.assertEqual("服务端日志（全量）", item["label"])
        with open(os.path.join(self.shelf_dir, item["name"]), "rb") as fp:
            archive = read_7z(fp.read())
        self.assertEqual(["logs/server-boot.err", "logs/server.out", "MANIFEST.txt"],
                         archive.names)
        manifest = archive.read("MANIFEST.txt").decode("utf-8")
        self.assertIn("压缩者: admin", manifest)
        self.assertIn("压缩等级: 高（LZMA2，字典 16 MB", manifest)
        self.assertEqual([b"\x18"], [folder[1] for folder in archive.folders])  # 16 MiB 字典
        self.assertEqual([item["name"]], os.listdir(self.shelf_dir))     # 没有 .part 残留
        self.assertEqual(1, len(self.lines))
        self.assertIn("'admin' 压缩好了服务端日志（全量）：2 个文件", self.lines[0])
        self.assertIn("（压缩等级 高）", self.lines[0])

    def test_the_same_request_is_not_queued_twice(self):
        self.log("server.out")
        gate = self.gate()
        first, new = self.shelf.submit("server", "all", level=sevenzip.LOW)
        self.assertTrue(new)
        again, new = self.shelf.submit("server", "all", level=sevenzip.LOW)
        self.assertFalse(new)
        self.assertIs(first, again)
        # ★ 不管档（D102）：同一份还在压时点另一档，拿到的是正在压的那个（admin.py 的回话里说是哪一档）。
        other_level, new = self.shelf.submit("server", "all", level=sevenzip.HIGH)
        self.assertFalse(new)
        self.assertIs(first, other_level)
        self.assertEqual(sevenzip.LOW, other_level.level)
        other, new = self.shelf.submit("server", "recent", level=sevenzip.LOW)   # 另一份：照排
        self.assertTrue(new)
        self.assertEqual(2, len(self.shelf.snapshot()["jobs"]))
        gate.set()
        self.assertEqual(2, len(self.settle()["files"]))

    def test_queued_jobs_know_how_many_are_ahead(self):
        self.log("server.out")
        self.crash_dir = os.path.join(self.crash, "alice_a1b2c3d4_20260909-013642")
        os.makedirs(self.crash_dir)
        with open(os.path.join(self.crash_dir, "receipt.json"), "wb") as fp:
            fp.write(b"{}")
        gate = self.gate()
        self.shelf.submit("server", "all", level=sevenzip.LOW)
        self.shelf.submit("server", "recent", level=sevenzip.LOW)
        self.shelf.submit("client_crash", level=sevenzip.LOW)
        snap = self.wait_until(lambda s: s["jobs"] and s["jobs"][-1]["state"] == "packing")
        # 新的在上：第三份前面 2 份，第二份前面 1 份，最早那份正在压。
        self.assertEqual([("queued", 2), ("queued", 1), ("packing", 0)],
                         [(job["state"], job["ahead"]) for job in snap["jobs"]])
        gate.set()
        self.assertEqual(3, len(self.settle()["files"]))

    def test_same_second_names_get_a_suffix(self):
        self.log("server.out")
        self.packer._clock = lambda: 1758000000.0      # 两次 plan() 落在同一秒
        plan = self.packer.plan("server")
        os.makedirs(self.shelf_dir)
        with open(os.path.join(self.shelf_dir, plan.stem + ".7z"), "wb") as fp:
            fp.write(b"older")
        job, _new = self.shelf.submit("server", level=sevenzip.LOW)
        self.assertEqual(plan.stem + "-2.7z", job.name)
        self.settle()

    def test_bad_requests_carry_their_http_status(self):
        with self.assertRaises(logpack.LogPackError) as caught:
            self.shelf.submit("nope", level=sevenzip.LOW)
        self.assertEqual(400, caught.exception.status)
        with self.assertRaises(logpack.NothingToPack) as caught:
            self.shelf.submit("server", "recent", level=sevenzip.LOW)  # logs/ 是空的
        self.assertEqual(404, caught.exception.status)
        self.assertEqual([], self.shelf.snapshot()["jobs"])

    def test_the_level_must_be_one_of_the_two(self):
        self.log("server.out")
        for bad in ("", "max", "HIGH", None):
            with self.assertRaises(logpack.LogPackError, msg=repr(bad)) as caught:
                self.shelf.submit("server", level=bad)
            self.assertEqual(400, caught.exception.status)
        self.assertEqual([], self.shelf.snapshot()["jobs"])

    def test_without_lzma_it_is_a_503(self):
        self.log("server.out")
        real = sevenzip.AVAILABLE
        sevenzip.AVAILABLE = False
        self.addCleanup(setattr, sevenzip, "AVAILABLE", real)
        with self.assertRaises(logpack.SevenZipUnavailable) as caught:
            self.shelf.submit("server", level=sevenzip.LOW)
        self.assertEqual(503, caught.exception.status)


class ProgressTests(_Case):
    def test_progress_wakes_the_long_poll(self):
        self.log("server.out", b"x" * 1000)

        def half(plan, progress):
            progress(plan.total_bytes // 2)             # 报一半，然后停在闸门上

        gate = self.gate(before=half)
        self.shelf.submit("server", level=sevenzip.LOW)
        snap = self.wait_until(lambda s: s["jobs"] and s["jobs"][0]["percent"] == 50)
        self.assertEqual("packing", snap["jobs"][0]["state"])
        self.assertTrue(snap["busy"])
        gate.set()
        snap = self.settle()
        self.assertEqual(1, len(snap["files"]))

    def test_percent_never_claims_100_before_it_is_done(self):
        self.log("server.out", b"x" * 1000)

        def overshoot(plan, progress):
            progress(plan.total_bytes * 3)              # 文件在压的时候长了三倍

        gate = self.gate(before=overshoot)
        self.shelf.submit("server", level=sevenzip.LOW)
        snap = self.wait_until(lambda s: s["jobs"] and s["jobs"][0]["percent"] > 0)
        self.assertEqual(99, snap["jobs"][0]["percent"])
        gate.set()
        self.settle()

    def test_wait_returns_at_once_when_nothing_is_running(self):
        version = self.shelf.version
        done = []
        waiter = threading.Thread(target=lambda: done.append(self.shelf.wait(version)))
        waiter.start()
        waiter.join(FUSE_S)
        self.assertFalse(waiter.is_alive(), "没活的时候长轮询不该挂着")
        self.assertEqual(version, done[0]["version"])

    def test_stop_wakes_every_waiter(self):
        self.log("server.out")
        gate = self.gate()
        self.shelf.submit("server", level=sevenzip.LOW)
        snap = self.wait_until(lambda s: s["jobs"] and s["jobs"][0]["state"] == "packing")
        woke = []
        waiter = threading.Thread(target=lambda: woke.append(self.shelf.wait(snap["version"])))
        waiter.start()
        stopper = threading.Thread(target=self.shelf.stop, args=(FUSE_S,))
        stopper.start()
        waiter.join(FUSE_S)
        self.assertFalse(waiter.is_alive(), "stop() 没叫醒挂着的长轮询")
        gate.set()                                      # 让正在压的那一份收尾，线程才退得掉
        stopper.join(FUSE_S)
        self.assertFalse(stopper.is_alive())
        with self.assertRaises(logshelf.Busy):
            self.shelf.submit("server", "recent", level=sevenzip.LOW)


class FailureTests(_Case):
    def test_a_failed_job_stays_with_its_reason_until_removed(self):
        self.log("server.out")

        def broken(fp, plan, level, meta=None, progress=None):
            fp.write(b"half")
            raise logpack.PackAborted("读 logs/server.out 读到一半出错：读盘出错")

        self.packer.write_7z = broken
        job, _new = self.shelf.submit("server", level=sevenzip.LOW, who="'admin'")
        snap = self.settle()
        (failed,) = snap["jobs"]
        self.assertEqual(("failed", job.id), (failed["state"], failed["id"]))
        self.assertIn("读到一半", failed["error"])
        self.assertEqual([], snap["files"])
        self.assertEqual([], os.listdir(self.shelf_dir))          # .part 删掉了
        self.assertIn("⚠ [admin] 'admin' 压缩服务端日志（全量）（压缩等级 低）失败", self.lines[-1])
        gate = self.gate()                                         # 让重排的那一份停在闸门上
        retry, new = self.shelf.submit("server", level=sevenzip.LOW)   # 失败的不挡同一份重排
        self.assertTrue(new)
        with self.assertRaises(logshelf.Busy):
            self.shelf.remove(job=retry.id)                        # 在排 / 在压的删不了
        gate.set()
        self.assertEqual("服务端日志（全量）", self.shelf.remove(job=job.id))
        with self.assertRaises(logshelf.NotFound):
            self.shelf.remove(job=job.id)


class LevelTests(_Case):
    """压缩等级（用户 2026-09-30，D102）：默认按**总**内存、两档各自进包、弹窗要的文字。"""

    def test_the_default_follows_total_memory(self):
        # 用户的原话：「内存低于 800MB 的服务器自动选择低，否则自动选择高」—— 800 本身算「否则」。
        for total, want in ((512 << 20, "low"), ((800 << 20) - 1, "low"), (800 << 20, "high"),
                            (4 << 30, "high"), (None, "high")):
            self.total = total
            self.assertEqual(want, self.shelf.level_info()["default"], total)

    def test_level_info_carries_every_number_the_page_prints(self):
        self.total = (15 << 30) + (800 << 20)
        info = self.shelf.level_info()
        self.assertEqual(self.total, info["memory"])
        self.assertEqual("15.8 GB", info["memory_text"])
        self.assertEqual("800 MB", info["low_memory_text"])
        self.assertEqual({"low", "high"}, set(info["levels"]))
        self.assertEqual(("低", "9 MB"), (info["levels"]["low"]["label"],
                                          info["levels"]["low"]["memory_text"]))
        self.assertEqual(("高", "186 MB"), (info["levels"]["high"]["label"],
                                            info["levels"]["high"]["memory_text"]))
        self.assertIn("标准压缩", info["levels"]["high"]["like"])
        self.total = None
        self.assertEqual(("", None), (self.shelf.level_info()["memory_text"],
                                      self.shelf.level_info()["memory"]))

    def test_memory_text(self):
        self.assertEqual("9 MB", logshelf.memory_text(9 << 20))
        self.assertEqual("1023 MB", logshelf.memory_text((1 << 30) - 1))
        self.assertEqual("1.0 GB", logshelf.memory_text(1 << 30))

    def test_the_low_level_writes_a_one_mebibyte_dictionary(self):
        self.log("server.out")
        self.shelf.submit("server", level=sevenzip.LOW)
        (item,) = self.settle()["files"]
        with open(os.path.join(self.shelf_dir, item["name"]), "rb") as fp:
            archive = read_7z(fp.read())
        self.assertEqual([b"\x10"], [folder[1] for folder in archive.folders])
        self.assertIn("压缩等级: 低（LZMA2，字典 1 MB", archive.read("MANIFEST.txt").decode("utf-8"))
        self.assertIn("（压缩等级 低）", self.lines[-1])

    def test_this_machine_reports_its_memory(self):
        # 冒烟：真去查一次（Windows 走 GlobalMemoryStatusEx，别的平台 sysconf / meminfo）。
        total = logshelf.total_memory()
        free = logshelf.available_memory()
        self.assertIsInstance(total, int)
        self.assertGreater(total, 64 << 20)
        self.assertIsInstance(free, int)
        self.assertGreater(free, 0)
        self.assertLessEqual(free, total)


class MemoryGuardTests(_Case):
    """开压前那一关 + 压的途中 `MemoryError`（D102）：失败要落在任务上、说人话、不留半截文件。"""

    def test_too_little_free_memory_fails_before_a_byte_is_written(self):
        self.log("server.out")
        called = []
        self.packer.write_7z = lambda *args, **kwargs: called.append(args)
        self.free = 100 << 20
        job, _new = self.shelf.submit("server", level=sevenzip.HIGH, who="'admin'")
        snap = self.settle()
        (failed,) = snap["jobs"]
        self.assertEqual(("failed", job.id), (failed["state"], failed["id"]))
        self.assertEqual("服务器现在可用内存只有 100 MB，「高」档压缩要约 186 MB —— 换「低」再试",
                         failed["error"])
        self.assertEqual([], called)                               # 编码器根本没建
        self.assertFalse(os.path.exists(self.shelf_dir))           # 目录都没建
        self.assertIn("（压缩等级 高）失败（服务器现在可用内存只有 100 MB", self.lines[-1])

    def test_the_low_level_still_fits_where_the_high_one_does_not(self):
        self.log("server.out")
        self.free = 100 << 20
        self.shelf.submit("server", level=sevenzip.LOW)
        snap = self.settle()
        self.assertEqual([], snap["jobs"])
        self.assertEqual(1, len(snap["files"]))

    def test_even_the_low_level_can_run_out(self):
        self.log("server.out")
        self.free = 1 << 20
        self.shelf.submit("server", level=sevenzip.LOW)
        (failed,) = self.settle()["jobs"]
        self.assertEqual("服务器现在可用内存只有 1 MB，「低」档压缩要约 9 MB —— 等内存空出来再试",
                         failed["error"])

    def test_a_memory_error_mid_way_reads_like_a_sentence(self):
        self.log("server.out")

        def starved(fp, plan, level, meta=None, progress=None):
            fp.write(b"half")
            raise MemoryError()                 # liblzma 分配失败时就是这样：消息是空串

        self.packer.write_7z = starved
        self.shelf.submit("server", level=sevenzip.HIGH)
        (failed,) = self.settle()["jobs"]
        self.assertEqual("服务器内存不够（「高」档压缩要约 186 MB）—— 换「低」再试", failed["error"])
        self.assertEqual([], os.listdir(self.shelf_dir))          # .part 删掉了


class FileTests(_Case):
    def make_file(self, name="logs_server_20260930-101530.7z", data=b"7z-bytes"):
        os.makedirs(self.shelf_dir, exist_ok=True)
        with open(os.path.join(self.shelf_dir, name), "wb") as fp:
            fp.write(data)
        return name

    def test_labels_come_from_the_name(self):
        self.assertEqual("服务端日志（最近 12 小时）",
                         logshelf.label_of("logs_server_12h_20260930-101530.7z"))
        self.assertEqual("服务端日志（全量）", logshelf.label_of("logs_server_20260930-101530-2.7z"))
        self.assertEqual("客户端崩溃包（全部）",
                         logshelf.label_of("logs_client_crash_20260930-101530.7z"))
        self.assertEqual("崩溃包 alice_a1b2c3d4_20260909-013642", logshelf.label_of(
            "logs_client_crash_alice_a1b2c3d4_20260909-013642_20260930-101530.7z"))
        self.assertEqual("mine.7z", logshelf.label_of("mine.7z"))

    def test_open_file_serves_a_listed_name(self):
        name = self.make_file()
        fp, size, label = self.shelf.open_file(name)
        with fp:
            self.assertEqual(b"7z-bytes", fp.read())
        self.assertEqual((8, "服务端日志（全量）"), (size, label))

    def test_names_must_match_a_listed_file_exactly(self):
        name = self.make_file()
        for bad in ("", "../" + name, name + "\n", "x/" + name, name.upper(),
                    "." + name + ".part", "logs_server_20260930-101531.7z"):
            with self.assertRaises(logshelf.NotFound, msg=repr(bad)):
                self.shelf.open_file(bad)
            with self.assertRaises(logshelf.NotFound, msg=repr(bad)):
                self.shelf.remove(name=bad)
        self.assertEqual([name], [item["name"] for item in self.shelf.snapshot()["files"]])

    def test_remove_deletes_the_file_and_wakes_the_page(self):
        name = self.make_file()
        before = self.shelf.version
        self.assertEqual("服务端日志（全量）", self.shelf.remove(name=name))
        self.assertGreater(self.shelf.version, before)
        self.assertEqual([], os.listdir(self.shelf_dir))
        with self.assertRaises(logshelf.NotFound):
            self.shelf.remove(name=name)

    def test_a_file_being_downloaded(self):
        name = self.make_file()
        fp, _size, _label = self.shelf.open_file(name)
        self.addCleanup(fp.close)
        if os.name == "nt":
            with self.assertRaises(logshelf.Busy):            # Windows：开着的文件删不掉
                self.shelf.remove(name=name)
        else:
            self.shelf.remove(name=name)                        # POSIX：删得掉，下载照样读完
            self.assertEqual(b"7z-bytes", fp.read())

    def test_leftover_parts_are_swept_on_first_use(self):
        name = self.make_file()
        part = os.path.join(self.shelf_dir, ".logs_server_20260930-090000.7z.part")
        with open(part, "wb") as fp:
            fp.write(b"half")
        snap = self.shelf.snapshot()
        self.assertFalse(os.path.exists(part))
        self.assertEqual([name], [item["name"] for item in snap["files"]])

    def test_files_are_listed_newest_first(self):
        old = self.make_file("logs_server_20260929-080000.7z")
        new = self.make_file("logs_server_12h_20260930-080000.7z")
        now = time.time()
        os.utime(os.path.join(self.shelf_dir, old), (now - 3600, now - 3600))
        snap = self.shelf.snapshot()
        self.assertEqual([new, old], [item["name"] for item in snap["files"]])
        self.assertEqual(2, snap["total"]["count"])


if __name__ == "__main__":
    unittest.main()
