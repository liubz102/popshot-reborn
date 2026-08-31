#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""roomclock.py —— 进程级的**节拍器**（V0.3 D106）。

一条后台线程 + 一个按**绝对 deadline** 排序的最小堆。到点就把回调叫起来，
回调自己说下一次什么时候再叫（返回下一个绝对时刻）。

## 为什么要有它

V0.3 到会话 47 为止，bot 的帧是「真人的同步包到达时才走一格」（D17）。
真人的心跳约 128 ms 一发，而收方对**远端弹体**是每 32 ms 推一格的 ——
于是服务端替 bot 发的 `rpExplode` 系统性地晚一个帧距，收方那份弹体已经
自灭、包被静默丢弃，弹体句柄计数器从此永久错开（§147）。

⇒ 服务端必须自己有一个和收方同频的 32 ms 时钟。这个模块就是那个时钟。

## 三条硬约束

1. **绝对 deadline，不是「睡 32 ms」**。`sleep(0.032)` 会把每一次唤醒的
   误差累加起来，跑十分钟就漂出去好几百毫秒；而弹体的 `rpExplode` 迟到
   一格就是永久错账（§147）。所以下一次的时刻永远从**起点**算：
   `t0 + n × 32 ms`，不从「现在」算。
2. **一条线程管所有房间**，不给每个房间 / 每颗弹体开 `threading.Timer`。
   Timer 是「一次性对象 + 一条线程」，房间一多就是几十条线程互相抢 GIL，
   而且它天生就是相对延时（第 1 条禁的那种）。
3. **回调必须很快**。它跑在本模块这条线程上，慢一拍就是所有房间一起晚。
   真正的活儿（一帧物理、发包）要交给房间自己那条线程去做，这里只负责
   「叫醒它」（见 `gameserver.RoomLoop`）。

## 换代

每次 `start()` 都分配一个**新的代号**并返回。堆里属于旧代的项弹出时直接
丢掉 —— 换图 / 开新一局重新 `start()` 之后，上一代排着队的定时任务自动
全部作废，不需要谁去堆里翻出来删（§218 / D137 的同一套路子）。

## 单测怎么办

`pump(now)` 把「到点的都跑一遍」同步做完，不碰线程。测试因此可以拿一个
假时钟一格一格地推，结果完全确定 —— 不是给测试开后门，跑的是同一段代码。

只用标准库；发布运行时是 CPython 3.8。
"""
from __future__ import annotations

import heapq
import threading
import time

#: 收方的**逻辑步长**，秒。出处 `[0x6dc528] = 32`（`ballistics.TICK_MS` 同源，
#: 那边有语料回归的完整推导）。本模块不 import `ballistics` —— 它是通用的
#: 节拍器，不该认识弹道；两边对不上时由 `bot.py` 那道断言喊出来。
TICK_S = 0.032

#: 一次唤醒最多提前多少秒就当作「到点了」。
#:
#: ⚠ 这**不是**一个时序阈值（铁律 10 禁的那种），是**浮点比较的容差**：
#: `Condition.wait(timeout)` 醒来时 `clock()` 可能比 deadline 差最后几微秒，
#: 不给容差就会空转一圈再睡 —— 睡的还是同一个 deadline。
_EPSILON = 1e-6


class _Task(object):
    __slots__ = ("key", "gen", "fn", "deadline")

    def __init__(self, key, gen, fn, deadline):
        self.key = key
        self.gen = gen
        self.fn = fn
        self.deadline = deadline


class Scheduler(object):
    """按绝对 deadline 叫人的节拍器。线程**懒启动**（`pump()` 一路不起线程）。

    回调签名 `fn(gen, deadline, now) -> 下一个绝对时刻 | None`；
    返回 `None` = 这个任务结束了，从表里摘掉。
    """

    def __init__(self, clock=time.monotonic, name="roomclock"):
        self._clock = clock
        self._name = name
        self._cv = threading.Condition()
        #: `key -> _Task`。**这本表说了算**，堆只是「什么时候再看一眼」的索引。
        self._tasks = {}
        #: `[(deadline, gen, key)]`。同一个 key 可能有多项（改期 / 换代），
        #: 弹出时和 `_tasks[key].gen` 一比，对不上就丢掉。
        self._heap = []
        self._gen = 0
        self._thread = None
        self._stopping = False
        #: 只为诊断：叫醒过几次、丢过几项过期的。
        self.fired = 0
        self.stale = 0

    # -- 登记 ---------------------------------------------------------------
    def start(self, key, fn, first=None, threaded=True):
        """把 `key` 这个任务挂上，返回它这一代的**代号**。

        同一个 key 再 `start()` 一次 = 换代：旧代排在堆里的项自动作废。
        `first` 是第一次的绝对时刻，不给就是「现在」。
        """
        with self._cv:
            self._gen += 1
            gen = self._gen
            deadline = self._clock() if first is None else float(first)
            self._tasks[key] = _Task(key, gen, fn, deadline)
            heapq.heappush(self._heap, (deadline, gen, key))
            if threaded and self._thread is None and not self._stopping:
                self._thread = threading.Thread(target=self._run, daemon=True,
                                                name=self._name)
                self._thread.start()
            self._cv.notify_all()
            return gen

    def generation(self):
        """只要一个**新代号**，不往表里挂任何东西。

        给「这个房间的拍子由别人来数」的场合用（单测里的房间就是这种）：
        挂进去只会在堆里越积越多，而且一旦有人打开线程，那些早就结束的
        房间会被一起叫起来。
        """
        with self._cv:
            self._gen += 1
            return self._gen

    def stop(self, key):
        """摘掉一个任务。堆里那几项等弹出来时按代号自然作废。"""
        with self._cv:
            task = self._tasks.pop(key, None)
            self._cv.notify_all()
            return task is not None

    def stop_all(self):
        with self._cv:
            self._tasks.clear()
            del self._heap[:]
            self._cv.notify_all()

    def shutdown(self):
        with self._cv:
            self._stopping = True
            self._cv.notify_all()

    def wake(self):
        """把线程叫起来重新看一眼堆顶（改期 / 新任务之后用）。"""
        with self._cv:
            self._cv.notify_all()

    # -- 查询 ---------------------------------------------------------------
    def next_deadline(self):
        """堆里最早的那个有效 deadline；空表返回 ``None``。"""
        with self._cv:
            return self._peek_locked()

    def _peek_locked(self):
        while self._heap:
            deadline, gen, key = self._heap[0]
            task = self._tasks.get(key)
            if task is None or task.gen != gen or task.deadline != deadline:
                heapq.heappop(self._heap)
                self.stale += 1
                continue
            return deadline
        return None

    def pending(self):
        with self._cv:
            return len(self._tasks)

    # -- 跑一轮 -------------------------------------------------------------
    def pump(self, now=None):
        """把**到点的**任务全跑一遍，返回跑了几个。

        ★ 回调在**锁外**跑：它可能反过来调 `start()` / `stop()`，也可能
          慢（单测里它就是一整帧）。跑完再回来改期。
        """
        now = self._clock() if now is None else float(now)
        count = 0
        while True:
            with self._cv:
                deadline = self._peek_locked()
                if deadline is None or deadline > now + _EPSILON:
                    return count
                _d, gen, key = heapq.heappop(self._heap)
                task = self._tasks.get(key)
                if task is None or task.gen != gen:
                    self.stale += 1
                    continue
                fn = task.fn
            self.fired += 1
            count += 1
            try:
                nxt = fn(gen, deadline, now)
            except Exception:               # noqa: BLE001 —— 见类注释
                # 一个房间的回调炸了不能把整条节拍器带走（D1 的同一条口径）：
                # 摘掉它，别的房间照走。日志由回调自己那一层打。
                nxt = None
            with self._cv:
                task = self._tasks.get(key)
                if task is None or task.gen != gen:
                    continue                # 回调里自己 stop / 换代了
                if nxt is None:
                    self._tasks.pop(key, None)
                    continue
                task.deadline = float(nxt)
                heapq.heappush(self._heap, (task.deadline, gen, key))

    def _run(self):
        while True:
            with self._cv:
                if self._stopping:
                    return
                deadline = self._peek_locked()
                now = self._clock()
                if deadline is None:
                    self._cv.wait(1.0)
                    continue
                if deadline > now + _EPSILON:
                    self._cv.wait(deadline - now)
                    continue
            self.pump()


#: 全进程唯一的一条节拍器。和 `botplan.PLANNER` 同一个理由做成模块级单例：
#: 房间是跨连接的共享状态，节拍也是。
SCHEDULER = Scheduler()


def ticks_due(t0, now, tick_s=TICK_S):
    """从 `t0` 起到 `now` 为止，**deadline 已经到了**的 tick 有几个。

    tick `n` 的时刻是 `t0 + n × tick_s`（tick 0 就在 `t0` 那一瞬间），
    所以 `now` 时刻「该跑完的 tick 数」= `floor((now − t0) / tick_s) + 1`。
    还没到 `t0` 就是 0 个。

    ⚠ 那个 `_EPSILON` 是**浮点容差**，不是时序阈值（铁律 10 禁的那种）：
    `100.032 - 100.0` 在二进制里是 `0.03199999999999363`，直接除会算成
    0.99999… ⇒ 恰好踩在 deadline 上的那一格被漏掉、下一轮才补。
    """
    span = float(now) - float(t0)
    if span < 0.0:
        return 0
    return int((span + _EPSILON) / tick_s) + 1


def deadline_of(t0, tick, tick_s=TICK_S):
    """第 `tick` 个 tick 的**绝对**时刻。误差不累积就靠它（约束 1）。"""
    return float(t0) + int(tick) * tick_s
