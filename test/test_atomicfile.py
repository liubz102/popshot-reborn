#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`atomicfile.replace()` —— 顶住 Windows 上那一瞬的「文件被别人占着」。

这一组钉的是**两种占用必须分开**（见 `atomicfile.py` 文件头）：

* 一瞬的（杀软 / 索引器 / 同步盘扫一眼）→ 重试，调用方根本不该知道；
* 一直的（编辑器独占打开）→ 到点把**原来那个** `PermissionError` 抛出去，
  `web/admin.py` 那句「这个文件多半正被别的程序占着」才还在。

★ 一个 `time.sleep` 都不真等：钟和 sleep 全从参数注入，跑得完全确定。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")   # 被测代码在隔壁
for _path in (HERE, SERVER):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import atomicfile                                              # noqa: E402


class FakeClock(object):
    """一台只有被 `sleep` 推的时候才会走的钟。"""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def clock(self):
        return self.now


class ReplaceTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.src = os.path.join(self.dir.name, "x.tmp")
        self.dst = os.path.join(self.dir.name, "x.json")
        with open(self.src, "w", encoding="utf-8") as fp:
            fp.write("新的")
        with open(self.dst, "w", encoding="utf-8") as fp:
            fp.write("旧的")
        self.fake = FakeClock()

    def call(self, **kwargs):
        return atomicfile.replace(self.src, self.dst,
                                  _sleep=self.fake.sleep,
                                  _clock=self.fake.clock, **kwargs)

    # ------------------------------------------------------------ 正常那条
    def test_it_really_replaces(self):
        self.call()
        with open(self.dst, encoding="utf-8") as fp:
            self.assertEqual("新的", fp.read())
        self.assertFalse(os.path.exists(self.src))

    def test_nobody_waits_when_it_works_the_first_time(self):
        self.call()
        self.assertEqual([], self.fake.slept)

    # ------------------------------------------------- 一瞬的占用：重试吃掉
    def test_a_momentary_lock_is_absorbed(self):
        """★ 杀软扫一眼就是这样：头两次拒绝访问，第三次就过了。"""
        real = os.replace
        calls = []

        def flaky(src, dst):
            calls.append(1)
            if len(calls) <= 2:
                raise PermissionError(13, "拒绝访问。")
            return real(src, dst)

        with mock.patch("os.replace", flaky):
            self.call()                       # 不抛 = 过
        self.assertEqual(3, len(calls))
        with open(self.dst, encoding="utf-8") as fp:
            self.assertEqual("新的", fp.read())

    def test_the_wait_backs_off_and_is_capped(self):
        """退避递增是为了别在扫描没完时空转，封顶是为了别一睡就是一秒。"""
        with mock.patch("os.replace", side_effect=PermissionError(13, "占着")):
            with self.assertRaises(PermissionError):
                self.call(patience=10.0)
        self.assertEqual(sorted(self.fake.slept), self.fake.slept)
        self.assertEqual(atomicfile._BACKOFF_MIN, self.fake.slept[0])
        self.assertEqual(atomicfile._BACKOFF_MAX, max(self.fake.slept))

    # --------------------------------------------- 一直的占用：原样抛出去
    def test_a_permanent_lock_still_raises_the_original_error(self):
        """★★ 编辑器独占打开的那种。吞掉它 = 用户以为存上了，其实没有。"""
        boom = PermissionError(13, "拒绝访问。")
        with mock.patch("os.replace", side_effect=boom):
            with self.assertRaises(PermissionError) as caught:
                self.call()
        self.assertIs(boom, caught.exception)

    def test_it_gives_up_at_the_deadline_not_after_a_fixed_count(self):
        """判据是「等够了没有」，不是「试够几次没有」（铁律 10）。"""
        with mock.patch("os.replace", side_effect=PermissionError(13, "占着")):
            with self.assertRaises(PermissionError):
                self.call(patience=0.5)
        short = list(self.fake.slept)
        self.assertLessEqual(sum(short), 0.5 + atomicfile._BACKOFF_MAX)

        self.fake = FakeClock()
        with mock.patch("os.replace", side_effect=PermissionError(13, "占着")):
            with self.assertRaises(PermissionError):
                self.call(patience=5.0)
        # 耐心给得多就真的等得久、试得多 —— 判据是钟，不是写死的次数。
        self.assertGreater(sum(self.fake.slept), sum(short))
        self.assertGreater(len(self.fake.slept), len(short))

    # ------------------------------------------------------- 别的错不许吞
    def test_other_errors_go_straight_up(self):
        """源文件不在 = 调用方写错了，立刻炸，别拿一秒去重试一个必然失败。"""
        missing = os.path.join(self.dir.name, "没有这个文件")
        with self.assertRaises(OSError):
            atomicfile.replace(missing, self.dst, _sleep=self.fake.sleep,
                               _clock=self.fake.clock)
        self.assertEqual([], self.fake.slept)

    # --------------------------------------------------------- 目录版同一套
    def test_rename_moves_a_whole_directory(self):
        """★ `rename()` 和 `replace()` 的区别只在语义：Windows 上
        `os.replace` 覆盖不了已存在的目录，而 `crashstore` / `databackup`
        的落位靠的正是「目标必须不存在」。"""
        src = os.path.join(self.dir.name, ".tmp-pack")
        os.makedirs(src)
        with open(os.path.join(src, "x.zip"), "wb") as fp:
            fp.write(b"1234567")
        dst = os.path.join(self.dir.name, "pack")
        atomicfile.rename(src, dst, _sleep=self.fake.sleep,
                          _clock=self.fake.clock)
        self.assertTrue(os.path.isfile(os.path.join(dst, "x.zip")))
        self.assertFalse(os.path.exists(src))

    def test_rename_absorbs_a_momentary_lock_too(self):
        real = os.rename
        calls = []

        def flaky(src, dst):
            calls.append(1)
            if len(calls) <= 2:
                raise PermissionError(13, "拒绝访问。")
            return real(src, dst)

        src = os.path.join(self.dir.name, ".tmp-pack")
        os.makedirs(src)
        dst = os.path.join(self.dir.name, "pack")
        with mock.patch("os.rename", flaky):
            atomicfile.rename(src, dst, _sleep=self.fake.sleep,
                              _clock=self.fake.clock)
        self.assertEqual(3, len(calls))
        self.assertTrue(os.path.isdir(dst))

    # ------------------------------------------------------ 只在 Windows 上
    def test_posix_never_retries(self):
        """POSIX 的 `rename()` 不看别人开没开，那边的拒绝访问是真的没权限。"""
        with mock.patch.object(atomicfile.os, "name", "posix"):
            with mock.patch("os.replace",
                            side_effect=PermissionError(13, "真的没权限")):
                with self.assertRaises(PermissionError):
                    self.call()
        self.assertEqual([], self.fake.slept)


if __name__ == "__main__":
    unittest.main()
