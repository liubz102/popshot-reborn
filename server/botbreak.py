#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""botbreak.py —— 一局里**可破坏物**（冰块 / 木箱）的账（V0.3 §138）。

## 原版是怎么做的（全都逆出来了，没有一条是编的）

* **形状 / 血量 / 恢复延迟**都在 `.map` 里：`BreakableObj::Deserialize`
  （`0x4fa3e7`）读的第一个 i32 是血量（10~100，众数 40），第二个写进
  `[this+0x2cc]` = **碎了多久原样长回来**（5.5~25 秒，众数 15000 ms，
  构造函数 `0x4fa379` 的缺省值也是 15000）。
* **碎**：挨够伤害就碎（`IsDestroyed` = `0x4fa4d0`：血 ≤ 0 且记过碎掉的时刻）。
* **长回来**：`BreakableObj::Tick`（`0x4fa4e9`）——
  `if (碎掉时刻 + 恢复延迟 < GetTickCount()) { 清标记; 血量恢复满 }`。

## ★★★ 一发同步包都没有

全镜像扫过：`BreakableObj` 那一段代码里**唯一**一处调进发包区的
（`0x4faad`）是 `0x4939c0` —— 那是**放特效**，不是 `UdpPacket`
（整个函数里一次 `0x5bbe1b` 都没有）。内层 opcode 表里也没有任何一个是
破坏物。

⇒ 原版靠的是**确定性**：每台客户端都收到同一批 `rpExplode`，各自在本地
   扣血、各自按本地时钟计时恢复。**服务端要跟上，就得自己算同一份账**
   —— 这个模块干的就是这件事，喂给它的是服务端本来就要过一遍的爆炸。

## 恢复为什么可以用时钟（铁律 10 的豁免）

「长回来」在原版里**本来就是个定时器**（`GetTickCount`），常量还写在地图
文件里。物理上没有事件可等 —— 没有任何一方会宣布「它长回来了」。
这条和 D13 的重生倒计时同一类豁免，而且连数都不是我们拍的。

只用标准库；发布运行时是 CPython 3.8。
"""
from __future__ import annotations

import time


class Ledger(object):
    """一张图上那几件可破坏物**此刻**的状态。

    `hp[i]` 是第 i 件的剩余血量；`broken_at[i]` 是它碎掉的时刻
    （`time.monotonic()`）。两张表都只装**偏离完好状态**的那几件，
    所以完好的一局里它们恒空，一点开销都没有。
    """

    __slots__ = ("hp", "broken_at")

    def __init__(self):
        self.hp = {}
        self.broken_at = {}

    def clear(self):
        self.hp.clear()
        self.broken_at.clear()

    def alive(self, terrain, now=None):
        """现在还立着的那几件的下标（`frozenset`），顺便把该长回来的收掉。

        ★ 恢复是**在这儿判的**，不另起定时器线程：这个函数每一帧都会被
          问到（`bot.tick_room`），而帧本身是真人的同步包驱动的。
        """
        if terrain is None or not getattr(terrain, "breakables", ()):
            return frozenset()
        if not self.broken_at:
            return frozenset(range(len(terrain.breakables)))
        now = time.monotonic() if now is None else now
        for index in list(self.broken_at):
            item = _item(terrain, index)
            if item is None:
                self.broken_at.pop(index, None)
                self.hp.pop(index, None)
                continue
            if now - self.broken_at[index] >= item.regen_ms / 1000.0:
                # 长回来了：血量满、标记清 —— 和 `0x4fa4e9` 一字不差。
                self.broken_at.pop(index, None)
                self.hp.pop(index, None)
        broken = set(self.broken_at)
        return frozenset(i for i in range(len(terrain.breakables))
                         if i not in broken)

    def damage(self, terrain, index, amount, now=None):
        """给第 `index` 件扣血；这一下把它打碎了就返回 `True`。"""
        item = _item(terrain, index)
        if item is None or amount <= 0 or index in self.broken_at:
            return False
        left = self.hp.get(index, item.hp) - amount
        if left > 0:
            self.hp[index] = left
            return False
        self.hp[index] = 0
        self.broken_at[index] = time.monotonic() if now is None else now
        return True

    def blast(self, terrain, x, y, radius, amount, now=None):
        """一发爆炸落在 (x, y)：范围内的破坏物一起扣血。

        返回**这一发打碎的**那几件的下标。

        ★ 判据是「爆点到这件东西的**外接矩形**有多远」——
          和服务端算角色溅射时那条（爆点到身体表面）同一个口径。
          逐格查形状对冰柱这种一团一团的东西没有意义，还慢。
        """
        broken = []
        for item in getattr(terrain, "breakables", ()):
            if item.index in self.broken_at:
                continue
            if item.distance_to(x, y) > radius:
                continue
            if self.damage(terrain, item.index, amount, now=now):
                broken.append(item.index)
        return broken

    def __repr__(self):
        return "<botbreak.Ledger 受损 %d 碎 %d>" % (len(self.hp),
                                                   len(self.broken_at))


def _item(terrain, index):
    items = getattr(terrain, "breakables", ())
    if 0 <= index < len(items):
        return items[index]
    return None
