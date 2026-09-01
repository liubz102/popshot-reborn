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

## ★★★ 「碎」是**广播**的，「长回来」不是（§139）

* **碎**：伤害走的是所有伤害对象共用的那条通用路 `0x480dfb`，而它在扣血
  **之前**先发一发 **`rpSplashDamaged`**（`0x480f2e -> 0x492b63`），
  `+4` 填的就是破坏物的**世界句柄**。语料实证：`Desert03` 上 11 发
  `rpSplashDamaged` 的受害者句柄正是那张图里破坏物的对象 id，
  受击点全部落在它们的掩码上。
  ⇒ **真人打碎的那一下，服务端读包就知道，一个数都不用算。**
* **长回来**：`BreakableObj::Tick`（`0x4fa4e9`）——
  `if (碎掉时刻 + 恢复延迟 < GetTickCount()) { 清标记; 血量恢复满 }`。
  **这一半没有任何包**，每台客户端各自计时。

## 谁能打碎它

`0x480469` 是 `DamagingObj` 的 `+0x11c`，**12 个伤害对象类共用**
（`SplashDamage` / `Flame` / `DashDamage` / `LaserDamage` / …）。
它按 `i = −1 … 9` 取 **11 个采样点**（爆点本身 + 半径 `SplashRange` 的圆上
10 个等分点，角步长 `2π/10`），每个点问一遍「在不在某件破坏物身上」
（`BreakableObj::HitTest` = `0x4fa7b5`，3×3 邻域）。
⇒ **不是「离得近就掉血」，是「炸出来的那一圈点得真的碰到它」**。

★ 那条遍历外面还有一道门 `0x50d294` = 「这颗弹是我的 / 中立的吗」。
所以 **bot 打碎的那一下别人机器上算不出来** —— 服务端必须替它补发
`rpSplashDamaged`，否则服务端和客户端会各看各的。

## 恢复为什么可以用时钟（铁律 10 的豁免）

「长回来」在原版里**本来就是个定时器**（`GetTickCount`），常量还写在地图
文件里。物理上没有事件可等 —— 没有任何一方会宣布「它长回来了」。
这条和 D13 的重生倒计时同一类豁免，而且连数都不是我们拍的。

只用标准库；发布运行时是 CPython 3.8。
"""
from __future__ import annotations

import math
import time


#: 那一圈采样点有几个 + 角步长。★ 都是原版写死的：`0x480648` 的
#: `cmp [ebp-0x10], 0xa` 给出 10 个，`[0x693c30] = 0.6283185` = 2π/10。
SPLASH_SAMPLES = 10
SPLASH_STEP = 2.0 * math.pi / SPLASH_SAMPLES


def preview_damage(item, x, y, splash_range, splash_damage, mult=1):
    """一发在 `(x,y)` 爆开会给 `item` 多少伤害；打不到返回 `None`。

    这是 `Ledger.blast()` 里原版 11 点命中 + 衰减公式的纯函数版。
    AI 选“多久能打碎挡路物”时可以先算、不动真血条。
    返回 `(伤害, 命中采样点)`。
    """
    if item is None or splash_damage <= 0:
        return None
    points = [(x, y)]
    if splash_range > 0:
        for i in range(SPLASH_SAMPLES):
            angle = i * SPLASH_STEP
            points.append((x + math.cos(angle) * splash_range,
                           y + math.sin(angle) * splash_range))
    where = next((point for point in points if item.hit(point[0], point[1])),
                 None)
    if where is None:
        return None
    span = math.hypot(x - item.x, y - item.y)
    reach = splash_range + item.radius
    if reach <= 0.0:
        return None
    ratio = span / reach
    if ratio > 1.0:
        return None
    hurt = int((1.0 - ratio) * (splash_damage - 1.0) + 1.0) * int(mult)
    return (hurt, where) if hurt > 0 else None


class Ledger(object):
    """一张图上那几件可破坏物**此刻**的状态。

    `hp[i]` 是第 i 件的剩余血量；`broken_at[i]` 是它碎掉的时刻
    （`time.monotonic()`）。两张表都只装**偏离完好状态**的那几件，
    所以完好的一局里它们恒空，一点开销都没有。
    """

    __slots__ = ("hp", "broken_at", "_alive_memo", "_alive_until")

    def __init__(self):
        self.hp = {}
        self.broken_at = {}
        #: ★ `alive()` 上一次的答案，以及它**什么时候会失效**。
        #:
        #: 为什么要缓存（用户 2026-09-01 的卡顿）：`alive()` 每次都新建一个
        #: `frozenset(range(N))`，而 `bot._terrain(room)` 一格里被调 6~10 次
        #: × 每个 bot；`MapTerrain.variant()` 拿到它之后还要再建一个
        #: 才去查 memo。62 件破坏物的图上，这是每秒两万多次白建集合。
        #:
        #: 失效判据是**事件**，不是定时器（铁律 10）：`broken_at` 变了
        #: （`damage` / `note_broken` / `clear` 会动它），或者时间越过了
        #: 「下一件该长回来的时刻」—— 后者是原版自己就用时钟的那一半
        #: （`0x4fa4e9`），见文件头的豁免说明。
        self._alive_memo = None
        self._alive_until = 0.0

    def _forget(self):
        """`broken_at` 变了 —— `alive()` 的答案跟着作废。"""
        self._alive_memo = None

    def clear(self):
        self.hp.clear()
        self.broken_at.clear()
        self._forget()

    def alive(self, terrain, now=None):
        """现在还立着的那几件的下标（`frozenset`），顺便把该长回来的收掉。

        ★ 恢复是**在这儿判的**，不另起定时器线程：这个函数每一帧都会被
          问到（`bot.tick_room`），而帧本身是真人的同步包驱动的。
        """
        if terrain is None or not getattr(terrain, "breakables", ()):
            return frozenset()
        if not self.broken_at:
            memo = self._alive_memo
            if memo is None or len(memo) != len(terrain.breakables):
                memo = frozenset(range(len(terrain.breakables)))
                self._alive_memo = memo
                self._alive_until = float("inf")   # 没碎的，等不到恢复
            return memo
        now = time.monotonic() if now is None else now
        memo = self._alive_memo
        if memo is not None and now < self._alive_until:
            return memo
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
        memo = frozenset(i for i in range(len(terrain.breakables))
                         if i not in broken)
        # 缓存到「下一件该长回来的那一刻」为止 —— 在那之前答案不可能变
        # （除非有人再打碎一件，那条路会 `_forget()`）。
        soonest = float("inf")
        for index, at in self.broken_at.items():
            item = _item(terrain, index)
            if item is None:
                soonest = 0.0
                break
            due = at + item.regen_ms / 1000.0
            if due < soonest:
                soonest = due
        self._alive_memo = memo
        self._alive_until = soonest
        return memo

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
        self._forget()                  # 存活集合变了，`alive()` 的缓存作废
        return True

    def blast(self, terrain, x, y, splash_range, splash_damage, mult=1,
              now=None):
        """一发爆炸落在 (x, y)：照原版那 11 个采样点判，命中的一起扣血。

        返回 `[(破坏物, 伤害, 命中点, 这一下碎没碎), …]`。调用方拿它补发
        `rpSplashDamaged` —— 别人机器上算不出 bot 的伤害（`0x50d294`
        那道「这颗弹是我的吗」的门），不补发两边就各看各的。

        ## 判据全是逐指令逆出来的（§139）

        * **打没打到**：`i = −1 … 9` 共 11 个采样点 —— 爆点本身，加上
          半径 `SplashRange` 的圆上 10 个等分点（`0x480577` 那个循环，
          角步长 `2π/10` = `[0x693c30]`）。每个点问 `Breakable.hit()`
          （`0x4fa7b5` 的 3×3 邻域）。
        * **掉多少血**：和打人**同一条**衰减（`0x4857aa`，§90），
          只是把「目标半径 35」换成破坏物自己的 `(宽+高)/2`：

              r  = |爆点 → 破坏物中心| / (SplashRange + (宽+高)/2)
              r > 1                 -> 一点都不掉
              伤害 = int((1−r) × (SplashDamage − 1) + 1)   ★ 朝零截断
              再 × 模式倍率（夺分 / 模式 5 是 ×2，`0x4806f1`）
        """
        items = [b for b in getattr(terrain, "breakables", ())
                 if b.index not in self.broken_at]
        if not items or splash_damage <= 0:
            return []
        out = []
        for item in items:
            preview = preview_damage(item, x, y, splash_range,
                                     splash_damage, mult=mult)
            if preview is None:
                continue
            hurt, where = preview
            out.append((item, hurt, where,
                        self.damage(terrain, item.index, hurt, now=now)))
        return out

    def apply_broadcast(self, terrain, handle, amount, now=None):
        """真人那边报过来的一发（`rpSplashDamaged`）：照它说的扣（§139）。

        ★ 一个数都不用算 —— 包里的伤害就是他那台机器**已经扣掉**的那个。
        不是破坏物的句柄返回 `None`。
        """
        item = None if terrain is None else terrain.breakable_by_handle(handle)
        if item is None:
            return None
        return (item, self.damage(terrain, item.index, int(amount), now=now))

    def __repr__(self):
        return "<botbreak.Ledger 受损 %d 碎 %d>" % (len(self.hp),
                                                   len(self.broken_at))


def _item(terrain, index):
    items = getattr(terrain, "breakables", ())
    if 0 <= index < len(items):
        return items[index]
    return None
