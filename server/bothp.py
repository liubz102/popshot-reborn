#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bothp.py —— 场上每个座位的**血量估计**（V0.3 M5-C）。

## 为什么服务端可以有这本账

原版**没有任何血量同步包**：心跳 body 里没有这一格（§24 / §25 那张表逐字段
都对过），`0x0408` 只在归零那一刻报一次。血量是**每台机器各自算**的 ——
收到 `rpExplode +24` / `rpSplashDamaged +8` 就原样扣进 `Character::OnHit`
（§42：收方不重算伤害，包里填多少扣多少），别人头顶那条血条画的就是这份
本机估计。

所以「知道谁还剩多少血」不是我们发明的能力，是**每一台客户端本来就在做的
同一件事**。服务端把同样的账再记一份：

* bot 自己打出去的伤害 —— 服务端本来就是射手（D28），数值就是它填进包里的；
* 真人打出去的伤害 —— 那两种包每一发都经过 `forward_peer_data()`
  （`gameserver.BOT_PEER_HIT` 的调用点），照着念一遍就行。

⚠ 它是**估计**，会和受害者本机的账有出入（弹道各算各的，§98 / bug调查/8）。
这正是真人也有的偏差：别人血条读数本来就可能和他自己看到的不一样。
判「该逼近还是该拉远」用它足够；**谁死没死仍然只认本人上报**，一个字都不改。

## 谁把它清零

* **重生**：`respawn_due` 从有到无那一下（`bot._lying_dead()` 的翻转）；
* **新一局 / 换图**：`0x0400` / `0x0417` 广播那一刻（和 `report_bots_loaded`
  同一个事件，D4）。

两处都是**事件**，不是定时器（铁律 10）。

只用标准库；发布运行时是 CPython 3.8。
"""
from __future__ import annotations


class Ledger(object):
    """一局里每个座位吃进去的**净伤害**。

    只存「扣了多少」而不存「还剩多少」：满血是角色属性（`chrprops` 的
    `hp`），换角色时不用回来改这本账。
    """

    __slots__ = ("taken", "lying")

    def __init__(self):
        #: 座位 -> 已经吃进去的净伤害（治疗会把它减回去，下限 0）。
        self.taken = {}
        #: 座位 -> 上一次看到的「躺着没有」，用来认出重生那一下翻转。
        self.lying = {}

    def clear(self):
        """整本账清空（新一局 / 换图）。"""
        self.taken.clear()
        self.lying.clear()

    def reset(self, seat):
        """这个座位回满血（重生）。"""
        self.taken.pop(int(seat), None)

    def note_damage(self, seat, amount):
        """记一发伤害。`amount` <= 0 一律忽略。"""
        value = float(amount)
        if value <= 0.0:
            return
        key = int(seat)
        self.taken[key] = self.taken.get(key, 0.0) + value

    def note_heal(self, seat, amount):
        """记一次治疗（`Status.ini[8]` 每秒 10 点那一类）。下限是满血。"""
        value = float(amount)
        if value <= 0.0:
            return
        key = int(seat)
        left = self.taken.get(key, 0.0) - value
        if left <= 0.0:
            self.taken.pop(key, None)
        else:
            self.taken[key] = left

    def taken_by(self, seat):
        return self.taken.get(int(seat), 0.0)

    def remaining(self, seat, max_hp):
        """还剩多少血（下限 0）。`max_hp` 由调用方从 `chrprops` 取。"""
        return max(0.0, float(max_hp) - self.taken_by(seat))

    def fraction(self, seat, max_hp):
        """还剩几成血（0.0 ~ 1.0）。满血或者查不到都返回 1.0。"""
        top = float(max_hp)
        if top <= 0.0:
            return 1.0
        return max(0.0, min(1.0, self.remaining(seat, top) / top))

    def note_lying(self, seat, lying):
        """记「这个座位躺着没有」，**返回它是不是刚站起来**。

        判据是**状态翻转**（铁律 10 的口径）：躺 -> 站 = 重生那一下。
        调用方拿 `True` 去 `reset()` 就行。
        """
        key = int(seat)
        was = self.lying.get(key, False)
        self.lying[key] = bool(lying)
        return bool(was) and not lying
