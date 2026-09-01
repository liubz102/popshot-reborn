#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`asynclog` 的测试 —— 异步日志出口。

要盯死的四件事：

1. **没 `start()` 时和改造前逐字一致**（同步写 stdout）。全仓库别的测试
   都靠这一条，破了会连锁红一大片。
2. **顺序**。单队列的全部意义就是「谁先记谁先出现」，多线程并发也一样。
3. **`drain()` 真的等到写完**。抓包文件关闭前靠它，测试里也靠它。
4. **队列满了丢日志而不是阻塞**。磁盘卡住时宁可少几行，绝不许卡住房间线程。
"""
from __future__ import annotations

import io
import os
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import asynclog


class _Sink:
    """记下每次 `write` / `flush` 的假文件。"""

    def __init__(self):
        self.chunks = []
        self.flushes = 0

    def write(self, data):
        self.chunks.append(data)

    def flush(self):
        self.flushes += 1

    def text(self):
        return "".join(self.chunks)


class _AsyncCase(unittest.TestCase):
    """跑完自动把写线程收干净，免得漏到别的用例里去。"""

    def tearDown(self):
        asynclog.stop()


class SyncModeTests(_AsyncCase):
    """没 `start()` 的时候必须是同步写 —— 别的测试全靠这一条。"""

    def test_without_start_it_writes_straight_through(self):
        self.assertFalse(asynclog.running())
        sink = _Sink()
        asynclog.emit("第一行", sink)
        # 没有写线程，所以**这一句返回时**内容已经在里面了。
        self.assertEqual(sink.text(), "第一行\n")
        self.assertEqual(sink.flushes, 1)

    def test_without_start_stdout_still_goes_to_the_current_stdout(self):
        buf = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(buf):
            asynclog.emit("到屏幕上")
        self.assertEqual(buf.getvalue(), "到屏幕上\n")

    def test_drain_on_a_stopped_writer_is_a_no_op(self):
        self.assertTrue(asynclog.drain(timeout=0.1))


class OrderingTests(_AsyncCase):

    def test_lines_come_out_in_the_order_they_went_in(self):
        sink = _Sink()
        asynclog.start()
        for i in range(500):
            asynclog.emit(f"第 {i} 行", sink)
        self.assertTrue(asynclog.drain(timeout=10))
        expected = "".join(f"第 {i} 行\n" for i in range(500))
        self.assertEqual(sink.text(), expected)

    def test_many_threads_never_interleave_within_a_line(self):
        """并发入队时**每一行本身**必须完整 —— 半行混进另一半是最难查的日志 bug。"""
        sink = _Sink()
        asynclog.start()
        threads = []
        for t in range(8):
            def work(tag=t):
                for i in range(200):
                    asynclog.emit(f"线程{tag}-{i:03d}", sink)
            thread = threading.Thread(target=work)
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(asynclog.drain(timeout=10))
        lines = sink.text().splitlines()
        self.assertEqual(len(lines), 8 * 200)
        # 每一行都得是完整的一行，且每条线程内部保序。
        seen = {}
        for line in lines:
            tag, index = line.split("-")
            seen.setdefault(tag, []).append(int(index))
        self.assertEqual(len(seen), 8)
        for tag, indexes in seen.items():
            self.assertEqual(indexes, sorted(indexes), tag)

    def test_different_targets_keep_their_relative_order(self):
        """两个目标是同一串事件的两份抄本，先后关系不该被「按目标归并」打乱。"""
        a, b = _Sink(), _Sink()
        asynclog.start()
        asynclog.emit("a1", a)
        asynclog.emit("b1", b)
        asynclog.emit("a2", a)
        self.assertTrue(asynclog.drain(timeout=10))
        self.assertEqual(a.text(), "a1\na2\n")
        self.assertEqual(b.text(), "b1\n")
        # a 被分成两组写（中间隔了 b），所以 flush 了两次 —— 这正是
        #「连续分组」而不是「按目标归并」的可观察证据。
        self.assertEqual(a.flushes, 2)


class BatchingTests(_AsyncCase):

    def test_a_whole_batch_costs_one_write_and_one_flush(self):
        """攒批的意义就在这儿：1000 行日志不该是 1000 次系统调用。"""
        sink = _Sink()
        asynclog.start()
        # 先把写线程堵在 wait 上，再一口气灌进去，保证它们进同一批。
        with asynclog._cv:
            for i in range(1000):
                asynclog._queue.append((sink, f"{i}\n"))
            asynclog._cv.notify()
        self.assertTrue(asynclog.drain(timeout=10))
        self.assertEqual(len(sink.chunks), 1)
        self.assertEqual(sink.flushes, 1)
        self.assertEqual(sink.text().count("\n"), 1000)


class BinaryTests(_AsyncCase):

    def test_bytes_targets_get_bytes(self):
        sink = _Sink()
        asynclog.start()
        asynclog.emit_bytes(sink, b"\x01\x02")
        asynclog.emit_bytes(sink, bytearray(b"\x03"))
        self.assertTrue(asynclog.drain(timeout=10))
        self.assertEqual(b"".join(sink.chunks), b"\x01\x02\x03")


class FailureTests(_AsyncCase):

    def test_a_target_that_raises_never_escapes(self):
        """磁盘满 / 句柄已关绝不能把写线程弄死 —— 它死了所有日志就都没了。"""

        class Broken:
            def write(self, data):
                raise OSError("磁盘满了")

            def flush(self):
                pass

        good = _Sink()
        asynclog.start()
        asynclog.emit("坏的", Broken())
        asynclog.emit("好的", good)
        self.assertTrue(asynclog.drain(timeout=10))
        self.assertEqual(good.text(), "好的\n")
        self.assertTrue(asynclog.running())      # 线程还活着

    def test_callable_targets_are_resolved_on_the_writer_thread(self):
        """`eventlog` 用回调式目标，好把跨天切名搬到写线程上做。"""
        sink = _Sink()
        where = []

        def target():
            where.append(threading.current_thread().name)
            return sink

        asynclog.start()
        asynclog.emit_text("一行\n", target)
        self.assertTrue(asynclog.drain(timeout=10))
        self.assertEqual(sink.text(), "一行\n")
        self.assertEqual(where, ["asynclog"])

    def test_a_callable_target_returning_none_is_skipped(self):
        asynclog.start()
        asynclog.emit_text("丢掉\n", lambda: None)
        self.assertTrue(asynclog.drain(timeout=10))     # 不抛就算过


class OverflowTests(_AsyncCase):

    def test_a_full_queue_drops_instead_of_blocking(self):
        """★ 磁盘卡住时宁可少几行，**绝不许**把房间线程堵死。"""
        sink = _Sink()
        asynclog.start()
        original = asynclog.MAX_PENDING
        try:
            asynclog.MAX_PENDING = 3
            with asynclog._cv:              # 攥住锁，写线程动不了
                for i in range(50):
                    asynclog._submit(sink, f"{i}\n")
                self.assertEqual(len(asynclog._queue), 3)
                self.assertEqual(asynclog._dropped, 47)
        finally:
            asynclog.MAX_PENDING = original
        self.assertTrue(asynclog.drain(timeout=10))

    def test_the_drop_count_is_reported_once_and_then_cleared(self):
        """按**状态翻转**补报（丢过 → 说一次 → 清零），不按次数也不按时间。"""
        out = _Sink()
        asynclog.start()
        with asynclog._cv:
            asynclog._dropped = 7
            asynclog._queue.append((out, "正常一行\n"))
            asynclog._cv.notify()
        self.assertTrue(asynclog.drain(timeout=10))
        self.assertEqual(asynclog._dropped, 0)
        # 补报那一行走 stdout，这里只验计数被清零、正常那行照写。
        self.assertEqual(out.text(), "正常一行\n")


class LifecycleTests(_AsyncCase):

    def test_start_twice_reuses_the_same_thread(self):
        first = asynclog.start()
        second = asynclog.start()
        self.assertIs(first, second)

    def test_stop_flushes_what_is_still_queued(self):
        sink = _Sink()
        asynclog.start()
        with asynclog._cv:                 # 让它来不及自己写
            asynclog._queue.append((sink, "临别一行\n"))
        asynclog.stop()
        self.assertEqual(sink.text(), "临别一行\n")
        self.assertFalse(asynclog.running())

    def test_after_stop_it_falls_back_to_writing_synchronously(self):
        asynclog.start()
        asynclog.stop()
        sink = _Sink()
        asynclog.emit("停了以后", sink)
        self.assertEqual(sink.text(), "停了以后\n")

    def test_stop_without_start_is_harmless(self):
        asynclog.stop()
        self.assertFalse(asynclog.running())


if __name__ == "__main__":
    unittest.main()
