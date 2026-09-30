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

★★ 记的是收方 `Character::OnHit` **真正扣掉的**（X_Mod §92 / §93）：截断、格挡、
免伤窗口、中毒、回复剂按原版节奏滴、心和图腾的回血。规则都在 `bot.py`
（它手上有时刻和房间），这里只放账本身。

## 谁把它清零

* **重生**：`respawn_due` 从有到无那一下（`bot._lying_dead()` 的翻转）；
* **新一局**：`0x0402` 那一刻 `room.quest` 整个换新，账跟着是一本新的。
* ★ **换图不清**（X_Mod §94）：闯关换图时客户端把同一批角色对象摘下来再挂回去，
  HP、状态、死没死全部带过去 —— 以前这里在 `0x0417` 那一刻清零，是错的。

都是**事件**，不是定时器（铁律 10）。

只用标准库；发布运行时是 CPython 3.8。
"""
from __future__ import annotations

import math


class Ledger(object):
    """一局里每个座位吃进去的**净伤害**。

    只存「扣了多少」而不存「还剩多少」：满血是角色属性（`chrprops` 的
    `hp`），换角色时不用回来改这本账。
    """

    __slots__ = ("taken", "lying", "immune", "poison_until", "poison_next",
                 "charge_next", "charge_drips")

    def __init__(self):
        #: 座位 -> 已经吃进去的净伤害（治疗会把它减回去，下限 0）。
        self.taken = {}
        #: 座位 -> 上一次看到的「躺着没有」，用来认出重生那一下翻转。
        self.lying = {}
        #: 座位 -> {状态号: 到哪一刻}（`time.monotonic()` 口径）。客户端
        #: `Character::OnHit` 进门那几道状态门（X_Mod §92）：0 复活 2 秒、
        #: 0x10 夺分被打死复活 7 秒、0x14 闯关达人 8 秒。**按状态分开记**，
        #: 因为有东西只撤其中一道（捡心会把状态 0 撤掉，§94）。
        self.immune = {}
        #: 座位 -> 中毒到哪一刻（X_Mod §93）。没中毒就不在表里。
        self.poison_until = {}
        #: 座位 -> 下一跳毒什么时候到期（客户端的 `[char+0x68c]`）。★ 毒解了它也
        #: 留着：客户端那一格只有跳过之后才改，下一次中毒的第一跳要等它。
        self.poison_next = {}
        #: 座位 -> 回复剂下一轮什么时候装（客户端 `[char+0x690]`，X_Mod §94）。
        #: 和 `poison_next` 同一个道理：药效没了它也留着。
        self.charge_next = {}
        #: 座位 -> 这一轮还剩几滴（客户端 `[char+0x6e0]`）。
        self.charge_drips = {}

    def clear(self):
        """整本账清空。"""
        self.taken.clear()
        self.lying.clear()
        self.immune.clear()
        self.poison_until.clear()
        self.poison_next.clear()
        self.charge_next.clear()
        self.charge_drips.clear()

    def reset(self, seat):
        """这个座位回满血（重生）。免伤由调用方按事件另给（`grant_immunity`）。"""
        self.taken.pop(int(seat), None)

    def drop_statuses(self, seat):
        """属性表整个清掉（`Die` / `Respawn`）：毒解、免伤门全撤、回复剂这一轮作废。

        ★ `poison_next` / `charge_next` 不动：那两格在客户端是角色身上的普通字段，
        只有 Init 清零（§93 / §94）。
        """
        key = int(seat)
        self.poison_until.pop(key, None)
        self.immune.pop(key, None)
        self.charge_drips.pop(key, None)

    def poison(self, seat, now, duration):
        """这个座位中毒（或者再中一次）：到期时刻续成 `now + duration`。

        ★ 跳的节奏不重排（`0x401bd6` 只改到期时刻）：下一跳已经排在将来的
        就等它；排在过去的（从没中过毒 / 上次毒早解了）就是**马上跳一下**。
        """
        key = int(seat)
        self.poison_until[key] = now + duration
        if self.poison_next.get(key, float("-inf")) < now:
            self.poison_next[key] = now

    def cure(self, seat):
        """毒解了（死了 / 复活：客户端 `Die` / `Respawn` 把属性表整个清掉）。"""
        self.poison_until.pop(int(seat), None)

    def poisoned(self, seat):
        return int(seat) in self.poison_until

    def due_poison_ticks(self, now, interval):
        """到 `now` 为止该跳的毒：`[(座位, 这一跳的时刻), …]`，按时间排好。

        照客户端 `0x509cea` 那道门：还在中毒（到期时刻没过）且下一跳到了才跳，
        跳完下一跳往后推 `interval`；过了到期时刻就解毒。
        """
        ticks = []
        for key in list(self.poison_until):
            until = self.poison_until[key]
            due = self.poison_next.get(key, now)
            while due <= now and due <= until:
                ticks.append((key, due))
                due += interval
            self.poison_next[key] = due
            if due > until and until <= now:
                del self.poison_until[key]
        ticks.sort(key=lambda pair: pair[1])
        return ticks

    def grant_immunity(self, seat, until, state=0):
        """这个座位的状态 `state` 挂到 `until`（再挂一次就是覆盖，`0x401bd6`）。"""
        self.immune.setdefault(int(seat), {})[int(state)] = until

    def revoke_immunity(self, seat, state):
        """撤掉一道免伤门（`Add(状态, 0 格)` 就是撤，§94）。"""
        gates = self.immune.get(int(seat))
        if gates:
            gates.pop(int(state), None)

    def immune_at(self, seat, now):
        gates = self.immune.get(int(seat))
        return bool(gates) and any(now < until for until in gates.values())

    def note_damage(self, seat, amount):
        """记一发伤害，返回记上了没有。

        ★ 先**朝零截断**：客户端收包时 `0x5f895c`（`_ftol2`）就是这么把
        `rpExplode +24` / `rpSplashDamaged +8` 变成整数再扣的（X_Mod §92）。
        截完 <= 0、或者根本不是个有限数（坏包）都不记。
        """
        value = float(amount)
        if not math.isfinite(value):
            return False
        value = float(int(value))
        if value <= 0.0:
            return False
        key = int(seat)
        self.taken[key] = self.taken.get(key, 0.0) + value
        return True

    def note_heal(self, seat, amount):
        """记一次治疗（回复剂的一滴、心、图腾那一类）。下限是满血。"""
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
