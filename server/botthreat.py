#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""botthreat.py —— **躲子弹**：预测在飞的敌弹会不会打到自己，选一个躲得开的动作
（V0.3 M5-E）。

## 素材本来就有

服务端手上已经有场上每一发弹的**完整弹道**：

* 真人打的 —— `rpFire` 里带武器 / 发射点 / 角度 / 力度，`bot.note_peer_fire()`
  早就把它解成 `ballistics.Shot` 存着了（§92 拿它反推击退用的就是这一份）；
* bot 打的 —— 那本来就是服务端自己算的（D28），`BotConn.pending_shots` 里
  躺着的就是弹体本身。

所以「预估敌方弹道」不需要任何新的逆向，只要把这两份合起来往前推。

## 躲避动作是**模拟**出来的，不是查表

候选动作（走、疾跑、跳、二段跳、蹲、按 ↓ 穿平台）逐个丢进 `botmove.tick()`
跑满 `HORIZON` 个 tick，再问「这条弹道还打得到我吗」。躲得开的就是躲得开 ——
掩体、弹跳台、平台边缘这些不用单独写规则，它们本来就在地形物理里：
往掩体后面走一步之所以有效，是因为**那条弹道会先撞上地形**。

## 难度：允许判断错

`dodge_error` 那一档（`bot.BOT_DIFFICULTY_PROFILES`）掷中时，bot **不去挑
最优动作**，而是随便挑一个 —— 真人预估错弹道的样子就是这个：要么没反应过来
（挑中「站着」），要么往错的方向躲。★ 掷骰子的判据是「**这一波威胁**」，
不是每一帧（同 `botaim` 的口径，D79）。

只用标准库；发布运行时是 CPython 3.8。
"""
from __future__ import annotations

import collections
import math

import ballistics
import botmove


#: 往前推几个 tick。★ 这是**模拟的前瞻上界**，不是「等多久算超时」那类阈值
#: （同 `botnav.AIR_TICKS` 的性质）。取 24 tick（0.77 秒）的依据是一次跳跃
#: 的上升段就要 `JUMP_SPEED / GRAVITY ≈ 17` 个 tick —— 前瞻短于它的话，
#: 「跳起来能不能躲开」这个问题在窗口里根本还没有答案。
HORIZON = 24

#: 采样弹道时，超过这个 tick 数还没到就当它这一波管不着我们了。
#: 和 `HORIZON` 是同一件事的两端，分开写是因为弹道从**发射时刻**起算。
MAX_FLIGHT_TICKS = 400


class Threat(object):
    """一发**在飞的**敌弹（真人的和别的 bot 的走同一个结构）。"""

    __slots__ = ("seat", "weapon", "x", "y", "shot", "at", "key")

    def __init__(self, seat, weapon, x, y, shot, at, key):
        #: 谁打的（座位号）。
        self.seat = seat
        self.weapon = weapon
        #: 发射点。
        self.x = float(x)
        self.y = float(y)
        #: `ballistics.Shot`。
        self.shot = shot
        #: 发射时刻（`time.monotonic()`）。
        self.at = float(at)
        #: 认它是「同一发」的钥匙 —— 掷闪避骰子按它去重。
        self.key = key

    def danger_radius(self, body_radius):
        """离这条弹道多近就算危险（世界单位）。

        直击要靠碰撞圆相加（`0x50f410`），溅射则是炸点周围一整圈都吃伤害
        （§90 的衰减式，边缘上还有 1 点）——所以带溅射的弹要按整个溅射半径
        避开，这不是保守估计，是它真的能打到那么远。
        """
        reach = float(body_radius) + float(self.weapon.size or 0.0)
        splash = float(self.weapon.splash_range or 0.0)
        return reach + splash

    def position_at(self, ticks):
        """发射后第 `ticks` 个 tick 时弹体在哪。"""
        return ballistics.position_at(self.x, self.y, self.shot, ticks)

    def __repr__(self):
        return ("<Threat 座位%s %s @(%.0f, %.0f) %r>"
                % (self.seat, getattr(self.weapon, "id", "?"),
                   self.x, self.y, self.shot))


class Option(collections.namedtuple(
        "Option", "name direction want_jump crouched want_drop fast_run")):
    """一个候选躲避动作 —— 就是「这几个键按着」。"""

    __slots__ = ()

    def keys(self):
        return {"direction": self.direction, "want_jump": self.want_jump,
                "crouched": self.crouched, "want_drop": self.want_drop,
                "fast_run": self.fast_run}


#: 候选动作。**顺序就是偏好顺序**：能不动就不动（真人 39% 的心跳是站着的，
#: §71），其次是走位，再次才是跳 / 蹲 / 下落这些代价更大的。
#: 全部来自用户列的那张单子：走位、跳跃、二段跳、下蹲、疾跑、下落。
#: ★ 「躲在掩体后面」「利用弹跳平台」不在这张表里 —— 它们是**走位的结果**，
#:   由地形物理自己给出（走到掩体后面那条弹道就撞墙了；走上台子人就被弹飞）。
OPTIONS = (
    Option("stand", 0, False, False, False, False),
    Option("crouch", 0, False, True, False, False),
    Option("left", -1, False, False, False, False),
    Option("right", 1, False, False, False, False),
    Option("dash-left", -1, False, False, False, True),
    Option("dash-right", 1, False, False, False, True),
    Option("jump", 0, True, False, False, False),
    Option("jump-left", -1, True, False, False, False),
    Option("jump-right", 1, True, False, False, False),
    Option("drop", 0, False, False, True, False),
)

STAND = OPTIONS[0]


def elapsed_ticks(threat, now):
    """这一发已经飞了几个 tick（浮点）。"""
    return max(0.0, (float(now) - threat.at) * ballistics.TICKS_PER_SECOND)


def _blocked(terrain, x, y):
    return terrain is not None and terrain.blocks_bullet(int(x), int(y))


def impact_tick(terrain, threat, now, centers, radius, horizon=HORIZON):
    """这条弹道会在**未来第几个 tick** 碰到我；碰不到返回 `None`。

    `centers[i]` 是第 `i` 个 tick 时自己身体圆心的位置（调用方按候选动作
    模拟出来）。弹体撞上地形就不再往前算 —— 那正是「躲到掩体后面」为什么
    有效：那条弹道根本走不到我这儿。
    """
    start = elapsed_ticks(threat, now)
    span = min(int(horizon), len(centers))
    for step in range(span):
        flight = start + step
        if flight > MAX_FLIGHT_TICKS:
            return None
        bx, by = threat.position_at(flight)
        if _blocked(terrain, bx, by):
            return None
        cx, cy = centers[step]
        if math.hypot(bx - cx, by - cy) <= radius:
            return step
    return None


def simulate(terrain, body, character, option, ticks=HORIZON):
    """按住这套键走 `ticks` 个 tick，返回每一 tick 的**身体圆心**位置。

    起跳 / 按 ↓ 只在第一个 tick 上生效（和 `botmove.advance()` 一个口径）；
    但**第二段跳**留到腾空之后再按一次 —— 那正是「跳起来还不够，再补一段」
    这个动作，不补的话候选表里就没有二段跳了。
    """
    centers = []
    current = body
    used_air_jump = False
    for step in range(max(1, int(ticks))):
        want_jump = option.want_jump and (step == 0 or (
            not current.on_ground and not used_air_jump
            and not current.air_jumped))
        if want_jump and step > 0:
            used_air_jump = True
        current = botmove.tick(
            terrain, current, character,
            direction=option.direction, fast_run=option.fast_run,
            crouched=option.crouched, want_jump=want_jump,
            want_drop=option.want_drop and step == 0)
        centers.append(character.center(current.x, current.y, option.crouched))
    return centers


def _worst_impact(terrain, threats, now, centers, character, option):
    """这套动作下，最早被哪一发打到（返回 tick 数）；一发都打不到返回 `None`。"""
    radius_base = character.size_body
    earliest = None
    for threat in threats:
        step = impact_tick(terrain, threat, now, centers,
                           threat.danger_radius(radius_base))
        if step is not None and (earliest is None or step < earliest):
            earliest = step
    return earliest


def choose(terrain, body, character, threats, now, blind_pick=None):
    """挑一个躲得开的动作；不用躲（或者躲不掉）返回 `None`。

    `blind_pick` 给了就**不挑了**，直接用它 —— 那是难度掷中「预估失误」时
    的那一手（可能是站着不动，也可能是往错的方向躲）。

    挑法：站着不动就已经安全 ⇒ 返回 `None`（不要为了躲而乱动，真人也不会）。
    否则按 `OPTIONS` 的顺序找第一个**一发都挨不着**的；一个都没有就退而求
    其次，挑「最晚才被打到」的那个 —— 多活几个 tick 也是躲。
    """
    if terrain is None or body is None or not threats:
        return None
    if blind_pick is not None:
        return None if blind_pick is STAND else blind_pick
    safe_now = _worst_impact(
        terrain, threats, now,
        simulate(terrain, body, character, STAND), character, STAND)
    if safe_now is None:
        return None                      # 本来就打不到我，别乱动
    best = None
    for option in OPTIONS:
        if option is STAND:
            continue
        centers = simulate(terrain, body, character, option)
        hit = _worst_impact(terrain, threats, now, centers, character, option)
        if hit is None:
            return option
        if best is None or hit > best[0]:
            best = (hit, option)
    if best is not None and best[0] > safe_now:
        return best[1]                   # 躲不干净，但能多撑几个 tick
    return None


def roll_blind(roll, chance, resolution=1000):
    """掷一次「这一波威胁我判断错了没有」。错了返回一个**随便挑的**动作。

    ★ 和 `botaim.roll_error()` 一样，这是**每一波威胁掷一次**，不是每一帧
    —— 逐帧掷会让 bot 在原地乱抖，而且「失误概率」的语义会变味。
    """
    if roll(resolution) / float(resolution) >= float(chance):
        return None
    return OPTIONS[roll(len(OPTIONS))]
