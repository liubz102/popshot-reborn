#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""botarms.py —— **换哪把枪**：把每把枪在此刻这个局面下折算成「每秒有效伤害」
（V0.3 M5-C）。

## 口径：不排武器的座次，只算这一刻的期望收益

这里**没有**「狙击枪适合远战、手雷适合近战」这种人写死的偏好表 ——
那种表是替玩家做的决定（铁律 11 / D50）。这里做的是一道算术题，
每一项的出处都在原版数据或者几何里：

| 项 | 怎么来的 |
|---|---|
| 打不打得到 | `ballistics.solve()` 解不出来 / 引信先炸 / 地形挡住 ⇒ 这把枪此刻**不可用**，不是「分低」 |
| 每秒几发 | `MagazineCount × CoolingTime + ReloadTime` 那条节奏（和 `bot._reload_after_shot()` 同一套原版数据）；蓄力武器还要加上按住的那段时间（`ballistics.charge_ticks`）|
| 命中概率 | **几何**：弹飞 `t` 个 tick 期间目标最多挪 `v·t`，命中窗口是「目标碰撞圆 + 弹体半径」⇒ `p = min(1, R / (v·t))`。慢弹打移动目标自然吃亏，快弹自然占便宜 |
| 溅射 | 打偏了也炸 —— 用**逆出来**的衰减式（§90）按「预计偏多远」结算 |
| 自伤 | 溅射分不清敌我（§69）。近距离扔手雷会炸到自己，**照实扣进分数**里（D50 说的「把代价如实结算」）|
| 夺分 ×2 | `bot._damage_scale()` 传进来，直击和溅射都乘 |

得分 = `(命中概率×直击 + 打偏概率×溅射 − 期望自伤) × 弹丸数 × 每秒发数`。
单位是「每秒有效伤害」，所以三把枪的分数可以直接比大小。
★ 直击和溅射**不相加** —— 被直接命中的那个人不吃溅射（`_splash_targets()`
把他跳过），两者是互斥的两种结局。

## 换枪是有代价的，所以要一道**迟滞**

换枪要发一发 `rpChangeWeapon`，而且**弹匣从头开始**（`bot._declare_weapon()`
把 `rounds_left` 清成 None）。分数只高一丁点就换，bot 会在两把枪之间反复横跳，
一发都打不出来。`SWITCH_MARGIN` 要求新枪至少高出这么多**倍**才换。
★ 它不是时间阈值（铁律 10）—— 判据是「分数事实」，和挂钟无关。

只用标准库；发布运行时是 CPython 3.8。
"""
from __future__ import annotations

import ballistics


#: 换枪的迟滞：新枪的分要高出当前枪这么多倍才值得换。
#:
#: 代价是实打实的：一次换枪丢掉半个弹匣，而弹匣最长的那把（`ch02-01`，
#: 14 发 × 140 ms）打完一整匣要 2 秒。1.25 = 「多两成半以上才动手」。
SWITCH_MARGIN = 1.25

#: 溅射半径上还要加的那 35 —— `0x485831` 把双方的 `vft+0x7c` 相加，
#: 目标那一边恒 35（和 `bot.SPLASH_BODY_RADIUS` 是同一个数）。
SPLASH_BODY_RADIUS = 35.0

#: 算不出目标速度时按「站着」算。★ 不是猜的默认值，是「没有第二个采样点
#: 就没有速度这件事实」——- 站着的人 `v = 0`，命中概率因此是 1。
DEFAULT_TARGET_SPEED = 0.0


def shots_per_second(weapon, shot=None):
    """这把枪**持续**打的话每秒几发。全部取自 `weapon.ini`。

    和 `bot._reload_after_shot()` 同一套账：有弹匣的按
    「`magazine` 发 × `cooling` + 一次 `reload`」摊平，没弹匣的按
    `fire_interval_ms` 一发一发算。

    ★ 蓄力武器（`PowerControl=2`）还要按住 `charge_ticks(power)` 个 tick
    才放得出去（§73），那段时间也占着这一枪 —— 不算的话手雷的账面射速
    会比真人手快好几倍。

    ★ 带溅射的武器再多一道闸：`bot._may_fire()` 要求上一发的 `rpExplode`
    发完才开下一枪（§43 的句柄记账），所以间隔至少是一次飞行时间。
    """
    interval_ms = _interval_ms(weapon)
    if shot is not None:
        if weapon.power_control == ballistics.MODE_CHARGE:
            interval_ms += ballistics.charge_ticks(shot.power) * \
                ballistics.TICK_MS
        if weapon.splash_range:
            interval_ms = max(interval_ms, shot.ticks * ballistics.TICK_MS)
    if interval_ms <= 0.0:
        return 0.0
    return 1000.0 / interval_ms


def _interval_ms(weapon):
    """摊平到「每发多少毫秒」。"""
    magazine = weapon.magazine
    if magazine:
        cooling = weapon.cooling_ms or weapon.fire_interval_ms or 0
        reload_ms = weapon.reload_ms or weapon.fire_interval_ms or 0
        cycle = magazine * float(cooling) + float(reload_ms)
        if cycle > 0.0:
            return cycle / magazine
    return float(weapon.fire_interval_ms or weapon.reload_ms or 0)


def hit_chance(shot, target_speed, hit_radius):
    """这一发打得中的概率 —— 纯几何，没有经验系数。

    弹飞 `shot.ticks` 个 tick，这段时间里目标最多挪 `speed × ticks`。
    瞄的是**预测点**（`botaim`），所以真正的偏差是「他有没有按预测那样动」；
    最坏情况就是掉头，偏差量级仍是 `speed × ticks`。命中窗口是
    `hit_radius`（目标碰撞圆 + 弹体半径）⇒

        p = min(1, hit_radius / (speed × ticks))

    站着不动的目标（`speed = 0`）恒为 1；弹速越慢、目标越快，分越低。
    """
    spread = float(target_speed) * max(0.0, float(shot.ticks))
    if spread <= 0.0:
        return 1.0
    return min(1.0, max(0.0, float(hit_radius)) / spread)


def splash_damage_at(weapon, span, damage_ratio=1.0):
    """溅射在 `span` 这个距离上还剩多少伤害（**逆出来**的式子，§90）。

    `伤害 = int((1 − span / (SplashRange + 35)) × (SplashDamage − 1) + 1)`，
    超出半径返回 0。没有溅射的武器恒 0。
    """
    reach = float(weapon.splash_range or 0.0)
    if reach <= 0.0:
        return 0.0
    reach += SPLASH_BODY_RADIUS
    if span >= reach:
        return 0.0
    full = int(weapon.splash_damage * float(damage_ratio))
    return float(int((1.0 - span / reach) * (full - 1) + 1))


def expected_damage(weapon, shot, target_speed, hit_radius, self_span,
                    damage_scale=1, damage_ratio=1.0):
    """这一发（**一次扣扳机**）的期望净伤害，已经扣掉期望自伤。

    * 打中 -> `Damage`（身体那一档；头 / 腿是运气，不进期望）。
      ★ **直接命中的人不吃溅射**（`_splash_targets()` 把他跳过），
      所以这两档不能相加；
    * 打偏 -> 偏差按 `speed × ticks` 估，落在溅射半径里就还有溅射；
    * 自己 -> 爆点离自己 `self_span`，溅射分不清敌我，照样吃（§69）。

    ★ `SpreadFrags > 1` 的枪一次造好几颗弹体，服务端这边它们**同角度同速度**
      （`bot._try_fire()` 那个 `for offset in range(weapon.shots)`）⇒ 要么一起
      中要么一起丢，所以三档都乘 `shots`。
    """
    scale = float(damage_scale)
    pellets = max(1, int(weapon.shots))
    chance = hit_chance(shot, target_speed, hit_radius)
    direct = int(weapon.damage_for("body") * float(damage_ratio)) * scale
    miss_span = float(target_speed) * max(0.0, float(shot.ticks))
    splash_miss = splash_damage_at(weapon, miss_span, damage_ratio) * scale
    gain = chance * direct + (1.0 - chance) * splash_miss
    cost = splash_damage_at(weapon, float(self_span), damage_ratio) * scale
    return (gain - cost) * pellets


def score(weapon, shot, target_speed, hit_radius, self_span,
          damage_scale=1, damage_ratio=1.0):
    """这把枪此刻的**每秒有效伤害**。`shot is None` = 打不到，返回 `None`。"""
    if shot is None:
        return None
    rate = shots_per_second(weapon, shot)
    if rate <= 0.0:
        return None
    return expected_damage(weapon, shot, target_speed, hit_radius, self_span,
                           damage_scale, damage_ratio) * rate


def better(current_score, candidate_score, margin=SWITCH_MARGIN):
    """值不值得从当前这把换成候选那把（迟滞见模块头）。"""
    if candidate_score is None:
        return False
    if current_score is None:
        return candidate_score > 0.0
    return candidate_score > current_score * float(margin)
