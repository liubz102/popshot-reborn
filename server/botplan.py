#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""botplan.py —— 把 A\\* 整个挪到**后台线程**上跑（V0.3 §137）。

## 为什么非挪不可

bot 的帧是挂在**真人的同步转发路径**上的（`gameserver._relay_battle_tick`
-> `bot.tick_room`，D17）—— 那条路径慢多少，真人屏幕上就卡多少。
会话 41 已经把边缓存做了，一次「够得着的目标」只要 0.14 ms；
但**够不着的目标**仍然要把整个可达分量泛洪一遍：

    Quest03_1（11400 宽，2535 个落脚点，缓存全热）
        目标在图里     中位 0.14 ms  p95 0.38 ms  max 28.4 ms
        目标够不着     中位 22 ms    max 24 ms      ← 每一帧、每个 bot

三个 bot 就是 66 ms 压在真人的转发上，实机日志里那些
`转发耗时 max=47~54ms` 的尖峰正是它。而「目标够不着」在闯关房里是**常态**
（带头的真人往往站在 bot 这一片走不到的地方）。

⇒ 这个模块把 `botnav.plan()` 搬到一条独立的后台线程上。游戏线程只做两件
   O(1) 的事：**递一张单子**、**看看上一张单子算好了没有**。
   转发路径上从此一条 A\\* 都不跑。

## 为什么不违反 D17 / 铁律 10

D17 禁的是「**用定时器代替事件**去推 bot 的帧」；铁律 10 禁的是
「拿固定次数 / 固定时间当判据」。这里两样都没有：

* 后台线程**没有节拍**，它只在有单子时醒来（`Condition.wait()`），算完就睡；
* bot 的帧还是由真人的同步包驱动，一帧不多一帧不少；
* 「这条路线能不能用」的判据是**空间事实**（起点和目标都还在 A\\* 自己
  认的「到了」窗口 `botnav.GOAL_X/GOAL_Y` 里），不是「算了多久」。

## 算好之前 bot 干什么

和以前 `plan()` 返回空时一模一样：退回 `_walk_to()` 的老兜底
（朝目标直着走、撞墙就跳、坑前停下）。所以它**一直在动**，只是这一帧
没有精算过的路线。后台线程闲着的时候一张单子 0.1~25 ms 就回来了，
而一帧是 125 ms —— 实际上**下一帧**就拿得到。

## 单测怎么办

`settle()` 会等到「队列空且没有在算的」。测试夹具在每发心跳之前调它一次，
于是「这一帧递单、下一帧用上」这个真实节奏在单测里被完整复现，
不需要给测试单开一条同步分支。

只用标准库；发布运行时是 CPython 3.8。
"""
from __future__ import annotations

import collections
import threading
import time

import botmove
import botnav


class RouteChoice(collections.namedtuple(
        "RouteChoice",
        "path reached cost gap blocker prefix shortcut")):
    """后台规划器选定的路。

    `blocker` 是捷径上第一件存活破坏物的下标；`prefix` 是到
    它之前可在完整地形上安全执行的路径前缀。`shortcut=True`
    表示这份答案来自“先假定破坏物已打碎”的地形。
    """

    __slots__ = ()


class Ticket(object):
    """一张规划单子。游戏线程只读 `ready` / `path`，后台线程只写它们。

    ★ 起点 `body` 和目标 `goal` 都留着：单子回来时要拿它们和**此刻**的
      事实比一比，差得比 A\\* 自己认的「到了」窗口还远就作废重递。
    """

    __slots__ = ("terrain", "open_terrain", "body", "character", "goal",
                 "path", "choice", "ready", "abandoned")

    def __init__(self, terrain, body, character, goal, open_terrain=None):
        self.terrain = terrain
        self.open_terrain = open_terrain
        self.body = body
        self.character = character
        self.goal = (float(goal[0]), float(goal[1]))
        self.path = ()
        self.choice = RouteChoice((), False, 0.0, float("inf"),
                                  None, (), False)
        self.ready = False
        self.abandoned = False

    def matches(self, body, goal):
        """这条算好的路线**现在**还成立吗 —— 纯空间判据，没有时间。

        两把尺子都是**图自己的**，不是新拍的常量：

        * 起点：一条**步行边**的长度（`WALK_TICKS × 冲刺走速`）。挪得比
          一条边还少，等于「还站在这条路线的第一条边上」，照走就是；
        * 目标：A\\* 自己认的「到了」窗口（`GOAL_X/GOAL_Y`）。目标挪得比
          它还少，A\\* 会算出同一条路线。
        """
        reach = botnav.WALK_TICKS * botmove.walk_speed(self.character,
                                                       fast_run=True)
        return (abs(body.x - self.body.x) <= reach
                and abs(body.y - self.body.y) <= reach
                and abs(goal[0] - self.goal[0]) <= botnav.GOAL_X
                and abs(goal[1] - self.goal[1]) <= botnav.GOAL_Y)


class Planner(object):
    """一条后台线程 + 一个先进先出的单子队列。

    ★ 线程**懒启动**：没有 bot 的进程（单测里绝大多数）一条线程都不多起。
    ★ 队列里放的是 `Ticket`；主人换了目标就把旧单子标成 `abandoned`，
      轮到它时直接扔掉，不白算。
    """

    def __init__(self):
        self._cv = threading.Condition()
        self._queue = collections.deque()
        self._busy = 0
        self._thread = None
        #: 只为诊断：算过几张、扔掉几张。日志里看得见就够了。
        self.planned = 0
        self.dropped = 0

    # -- 游戏线程这一侧 -----------------------------------------------------

    def submit(self, ticket):
        with self._cv:
            self._queue.append(ticket)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="botnav-plan", daemon=True)
                self._thread.start()
            self._cv.notify()
        return ticket

    def settle(self, timeout=10.0):
        """等到队列空、也没有正在算的。**给单测和收工检查用**。

        ⚠ 战斗路径上一次都不许调它 —— 那就是把异步又变回同步。
        """
        deadline = time.monotonic() + timeout
        with self._cv:
            while self._queue or self._busy:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(left)
            return True

    def pending(self):
        with self._cv:
            return len(self._queue) + self._busy

    # -- 后台线程这一侧 -----------------------------------------------------

    def _run(self):
        while True:
            with self._cv:
                while not self._queue:
                    self._cv.wait()
                ticket = self._queue.popleft()
                self._busy += 1
            try:
                if ticket.abandoned:
                    self.dropped += 1
                    ticket.path = ()
                else:
                    current = botnav.plan_result(
                        ticket.terrain, ticket.body, ticket.character,
                        ticket.goal)
                    chosen = RouteChoice(
                        current.path, current.reached, current.cost,
                        current.gap, None, current.path, False)
                    opened = None
                    if (ticket.open_terrain is not None
                            and ticket.open_terrain is not ticket.terrain):
                        opened = botnav.plan_result(
                            ticket.open_terrain, ticket.body,
                            ticket.character, ticket.goal)
                    use_open = False
                    if opened is not None:
                        if opened.reached and not current.reached:
                            use_open = True
                        elif opened.reached and current.reached:
                            # 用户选的是“捷径优先打碎”：只比较移动成本，
                            # 不把打碎所需的时间加进去。相等时不浪费弹药。
                            use_open = opened.cost < current.cost
                        elif not opened.reached and not current.reached:
                            use_open = opened.gap < current.gap
                    if use_open:
                        blocker, prefix = botnav.first_breakable_on_path(
                            ticket.terrain, ticket.open_terrain, ticket.body,
                            ticket.character, opened.path)
                        # ★★★ 认不出挡路的是**哪一件**时，绝不能把开放地形
                        #   那条路交出去 —— 它是穿过实心罐子画的，拿到真
                        #   地形上执行就是一路撞墙。当作「没有捷径」，
                        #   退回完整地形那条已经验证过的答案。
                        if blocker is None:
                            use_open = False
                        else:
                            chosen = RouteChoice(
                                opened.path, opened.reached, opened.cost,
                                opened.gap, blocker, prefix, True)
                    ticket.choice = chosen
                    ticket.path = chosen.path
                    self.planned += 1
            except Exception:               # noqa: BLE001
                # ★ 纯计算，出什么事都不许把这条线程弄死 —— 它死了所有
                #   bot 就再也拿不到路线。当作「没找到路」，主人退回兜底。
                ticket.path = ()
                ticket.choice = RouteChoice((), False, 0.0, float("inf"),
                                            None, (), False)
            finally:
                ticket.ready = True
                with self._cv:
                    self._busy -= 1
                    self._cv.notify_all()


#: 全进程唯一的一条规划线程。可达图缓存本来就是进程级的（`botnav._EDGE_CACHE`），
#: 一条线程既够用又省得几个 bot 互相抢着算同一批边。
PLANNER = Planner()


def ask(machine, terrain, body, character, goal, open_terrain=None):
    """替 `machine` 递一张单子；已经有一张**同一个目标**的就不重复递。

    返回这一帧有没有真的递出去（只为诊断/单测）。
    """
    old = getattr(machine, "nav_ticket", None)
    if old is not None and not old.ready:
        if (abs(goal[0] - old.goal[0]) <= botnav.GOAL_X
                and abs(goal[1] - old.goal[1]) <= botnav.GOAL_Y):
            return False                    # 已经在算同一件事了
        old.abandoned = True                # 目标换了，旧的作废
    machine.nav_ticket = PLANNER.submit(
        Ticket(terrain, body, character, goal, open_terrain=open_terrain))
    return True


def take_result(machine, body, goal):
    """取回完整 :class:`RouteChoice`；`None` = 还没好 / 空间事实已变。"""
    ticket = getattr(machine, "nav_ticket", None)
    if ticket is None or not ticket.ready:
        return None
    machine.nav_ticket = None
    if ticket.abandoned or not ticket.matches(body, goal):
        return None
    return ticket.choice


def take(machine, body, goal):
    """兼容旧接口：只取路径。"""
    result = take_result(machine, body, goal)
    return None if result is None else result.path


def forget(machine):
    """主人不要这条路线了（换图 / 重生 / 被打飞）—— 把单子作废。"""
    ticket = getattr(machine, "nav_ticket", None)
    if ticket is not None:
        ticket.abandoned = True
        machine.nav_ticket = None
