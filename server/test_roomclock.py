#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`roomclock.py` 的单测 —— 节拍器（D106）。

全部用**假时钟**跑，一条线程都不起：判据是「到点了没有」这个事实本身，
不是「睡够了没有」（铁律 10）。
"""
import os
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import ballistics
import roomclock


class FakeClock(object):
    def __init__(self, now=1000.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, span):
        self.now += float(span)
        return self.now


class TickMathTests(unittest.TestCase):
    """`ticks_due` / `deadline_of` —— 绝对时刻那两条算式。"""

    def test_the_tick_length_matches_the_ballistics_model(self):
        """★ 两处对不上就是整套时序的地基塌了（§147）。"""
        self.assertEqual(ballistics.TICK_MS / 1000.0, roomclock.TICK_S)

    def test_tick_zero_is_due_at_the_start(self):
        self.assertEqual(1, roomclock.ticks_due(100.0, 100.0))

    def test_nothing_is_due_before_the_start(self):
        self.assertEqual(0, roomclock.ticks_due(100.0, 99.9))

    def test_one_more_tick_per_thirty_two_milliseconds(self):
        self.assertEqual(2, roomclock.ticks_due(100.0, 100.032))
        self.assertEqual(5, roomclock.ticks_due(100.0, 100.0 + 0.032 * 4))

    def test_a_long_stall_is_reported_in_full(self):
        """落后了就是落后了 —— 这个函数不许替调用方「跳过」（§147）。"""
        self.assertEqual(1 + 31, roomclock.ticks_due(100.0, 100.0 + 0.992))

    def test_deadlines_are_absolute_not_accumulated(self):
        """★ 一千格之后仍然精确对齐起点，这正是不用 `sleep(0.032)` 的理由。"""
        self.assertAlmostEqual(100.0 + 1000 * 0.032,
                               roomclock.deadline_of(100.0, 1000), places=9)


class SchedulerTests(unittest.TestCase):

    def setUp(self):
        self.clock = FakeClock()
        self.sched = roomclock.Scheduler(clock=self.clock)
        self.seen = []

    def every(self, span):
        """一个「每 `span` 秒再来一次」的回调，按绝对时刻排下一次。"""
        def fn(gen, deadline, now):
            self.seen.append((gen, round(deadline, 6), round(now, 6)))
            return deadline + span
        return fn

    # -- 基本 ---------------------------------------------------------------

    def test_nothing_runs_before_its_deadline(self):
        self.sched.start("a", self.every(0.032),
                         first=self.clock.now + 0.032, threaded=False)
        self.assertEqual(0, self.sched.pump())
        self.assertEqual([], self.seen)

    def test_it_runs_once_the_deadline_arrives(self):
        self.sched.start("a", self.every(0.032),
                         first=self.clock.now + 0.032, threaded=False)
        self.clock.advance(0.032)
        self.assertEqual(1, self.sched.pump())
        self.assertEqual(1, len(self.seen))

    def test_a_task_that_returns_none_is_dropped(self):
        self.sched.start("a", lambda gen, deadline, now: None, threaded=False)
        self.sched.pump()
        self.assertEqual(0, self.sched.pending())

    def test_stop_takes_it_out(self):
        self.sched.start("a", self.every(0.032), threaded=False)
        self.assertTrue(self.sched.stop("a"))
        self.sched.pump()
        self.assertEqual([], self.seen)

    # -- 不漂移 -------------------------------------------------------------

    def test_deadlines_do_not_drift_even_when_every_wake_up_is_late(self):
        """★★ 这条是本模块存在的理由：每次都晚 5 ms，跑 100 格之后**一点都不欠**。

        `sleep(0.032)` 那种写法会把 100 × 5 ms = 半秒的误差攒起来，而弹体
        的 `rpExplode` 晚一格（32 ms）就是永久错账（§147）。
        """
        start = self.clock.now
        self.sched.start("a", self.every(roomclock.TICK_S),
                         first=start, threaded=False)
        for step in range(1, 101):
            self.clock.now = start + step * roomclock.TICK_S + 0.005
            self.sched.pump()
        self.assertEqual(101, len(self.seen))
        for index, row in enumerate(self.seen):
            self.assertAlmostEqual(start + index * roomclock.TICK_S,
                                   row[1], places=9)

    def test_a_stall_fires_every_missed_deadline_not_just_the_last(self):
        """★ 追赶时**一个 tick 都不许跳** —— 跳掉的那一格里弹体不推进，
        它的 `rpExplode` 就又变成迟到（§147）。
        """
        start = self.clock.now
        self.sched.start("a", self.every(roomclock.TICK_S),
                         first=start, threaded=False)
        self.clock.now = start + 10 * roomclock.TICK_S
        self.sched.pump()
        self.assertEqual(11, len(self.seen))

    # -- 换代 ---------------------------------------------------------------

    def test_restarting_a_key_invalidates_the_old_generation(self):
        """★★ 换图 / 开新一局：上一代排着队的定时任务必须自动作废。"""
        first = self.sched.start("room", self.every(0.032),
                                 first=self.clock.now + 0.032, threaded=False)
        second = self.sched.start("room", self.every(0.032),
                                  first=self.clock.now + 0.100, threaded=False)
        self.assertNotEqual(first, second)
        self.clock.advance(0.050)
        self.sched.pump()
        self.assertEqual([], self.seen, "上一代的 deadline 还在叫人")
        self.clock.advance(0.060)
        self.sched.pump()
        self.assertEqual([second], [row[0] for row in self.seen])

    def test_every_start_hands_out_a_fresh_generation(self):
        seen = {self.sched.start("a", self.every(1.0), threaded=False),
                self.sched.start("b", self.every(1.0), threaded=False),
                self.sched.start("a", self.every(1.0), threaded=False)}
        self.assertEqual(3, len(seen))

    # -- 隔离 ---------------------------------------------------------------

    def test_one_blown_up_callback_does_not_take_the_metronome_down(self):
        """一个房间的回调炸了，别的房间照走（D1 的口径）。"""
        def boom(gen, deadline, now):
            raise RuntimeError("这个房间坏了")
        self.sched.start("bad", boom, threaded=False)
        self.sched.start("good", self.every(0.032), threaded=False)
        self.sched.pump()
        self.assertEqual(1, len(self.seen))
        self.assertEqual(1, self.sched.pending(), "炸掉的那个没被摘走")

    def test_a_callback_may_stop_itself(self):
        def suicide(gen, deadline, now):
            self.sched.stop("a")
            return deadline + 0.032
        self.sched.start("a", suicide, threaded=False)
        self.sched.pump()
        self.assertEqual(0, self.sched.pending())

    def test_next_deadline_skips_stale_entries(self):
        self.sched.start("a", self.every(1.0),
                         first=self.clock.now + 5.0, threaded=False)
        self.sched.stop("a")
        self.assertIsNone(self.sched.next_deadline())


class ThreadedSchedulerTests(unittest.TestCase):
    """★ 线程那条路的冒烟：真起一条节拍器线程，看它到点会不会叫人。

    上面那一大批用例都拿假时钟同步跑（确定性），所以线程本身一行都没被跑过 ——
    而生产上跑的就是它。这里只验「叫得到、停得掉」，不验时序精度
    （那个由 `test_deadlines_do_not_drift_even_when_every_wake_up_is_late` 管）。
    """

    def test_the_thread_fires_the_callback(self):
        sched = roomclock.Scheduler()
        self.addCleanup(sched.shutdown)
        fired = threading.Event()

        def fn(gen, deadline, now):
            fired.set()
            return None                 # 叫一次就够

        sched.start("smoke", fn, first=time.monotonic() + 0.02)
        self.assertTrue(fired.wait(5.0), "节拍器线程没把回调叫起来")

    def test_stop_takes_it_off_the_thread_too(self):
        sched = roomclock.Scheduler()
        self.addCleanup(sched.shutdown)
        count = []

        def fn(gen, deadline, now):
            count.append(tick_seen())
            return deadline + 0.01

        def tick_seen():
            return len(count)

        sched.start("smoke", fn, first=time.monotonic() + 0.01)
        deadline = time.monotonic() + 5.0
        while not count and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(count, "一次都没叫")
        sched.stop("smoke")
        settled = len(count)
        time.sleep(0.05)
        self.assertEqual(settled, len(count), "停了还在叫")


if __name__ == "__main__":
    unittest.main()
