#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""botaim.py —— **往哪儿瞄**：移动目标的提前量，以及像真人一样的失误
（V0.3 M5-D）。

## 提前量是解出来的，不是拍的

弹体飞到目标要 `t` 个 tick（`ballistics.solve()` 直接给），目标这段时间里
会走 `v · t`。所以瞄的不是他现在站的地方，而是 `p + v · t`。但 `t` 又取决于
瞄哪儿 —— 这是一条不动点方程，迭代两三轮就收敛（弹速远大于人速时收敛极快）。

`v` 从心跳轨迹上量：相邻两个采样点差一发心跳 = **4 个 tick**（§71 的语料
结论），所以 `v = Δp / 4`，单位和弹道完全一致（单位 / tick）。腾空段直接有
包里的速度，但为了口径统一这里一律用位移差 —— 收方替远端角色走路时用的
也是位置（§39）。

## 失误：偏一个「差一点点」的量，不是乱打一气

真人打不中通常是**擦过去**，不是朝天上放。所以失误建模成**在目标那儿横向
偏开 1~3 倍目标范围**（包住整个角色的碰撞圆 + 弹体半径），再叠一个把提前量
算错的系数。这样：

* 站着不动的目标也会被打偏（否则「必中」和难度设定矛盾）；
* 偏出去的量和距离无关地保持「差一点」的观感，不会变成朝天乱放。

失误**概率**由难度给（`bot.BOT_DIFFICULTY_PROFILES` 的 `aim_error`），
这里只负责「失误的时候偏成什么样」。

## 随机数

一律走调用方传进来的 `roll(n) -> 0..n-1`（就是 `BotConn.roll`，
默认 `random.randrange`）。单测把它钉死就能逐发复现。

只用标准库；发布运行时是 CPython 3.8。
"""
from __future__ import annotations

import math

import botmove


#: 相邻两个心跳采样点之间隔几个 tick（§71：语料里腾空段 `dx ≡ 4·vx`）。
TICKS_PER_SAMPLE = botmove.TICKS_PER_BEAT

#: 求提前量的迭代轮数。第一轮就已经把飞行时间算对到几个百分点，
#: 第三轮之后的变化远小于一个像素。★ 这是**数值迭代的收敛轮数**，
#: 不是「重试 N 次就放弃」那种时序阈值（铁律 10）。
LEAD_ROUNDS = 3

#: 目标速度大到这个程度就当采样有问题（瞬移 / 重生 / 换图），不算提前量。
#: 依据：走速上限是 `ChrSpeed × FastRunRate` ≈ 8 × 1.5 = 12 单位 / tick，
#: 被打飞时垂直速度也就 20 上下（`botmove.JUMP_SPEED`）。40 已经是它的三倍多。
MAX_SAMPLE_SPEED = 40.0

#: 失误时横向偏开几倍完整目标范围：`[1, 3]` 之间。
#: 1 倍以下会「歪打正着」，3 倍以上就不像人打的了。
MISS_SPREAD_MIN = 1.0
MISS_SPREAD_MAX = 3.0

#: 失误时提前量乘的系数范围（× 1/100）：−50% ~ +200%。
#: 覆盖真人最常见的两种错：**没带提前量**（≈ 0）和**带过头**（> 1）。
MISS_LEAD_MIN = -50
MISS_LEAD_MAX = 200

#: `roll()` 取小数时的分辨率。
ROLL_RESOLUTION = 1000


def _unit(roll):
    """`[0, 1)` 的随机小数 —— 只用 `roll(n)`，测试钉得住。"""
    return roll(ROLL_RESOLUTION) / float(ROLL_RESOLUTION)


def sample_velocity(points, ticks_per_sample=TICKS_PER_SAMPLE):
    """从一串位置采样点量出**单位 / tick** 的速度；量不出来返回 `(0, 0)`。

    `points` 是 `[(x, y), …]`（最后一个是最新的）。只用最后两个点 ——
    再往前平均会把「刚掉头」抹平，而掉头正是提前量最容易错的时刻。
    """
    if points is None or len(points) < 2:
        return (0.0, 0.0)
    (x0, y0), (x1, y1) = points[-2][:2], points[-1][:2]
    span = max(1.0, float(ticks_per_sample))
    vx = (float(x1) - float(x0)) / span
    vy = (float(y1) - float(y0)) / span
    if math.hypot(vx, vy) > MAX_SAMPLE_SPEED:
        return (0.0, 0.0)
    return (vx, vy)


def lead_point(solve, muzzle, target, velocity, rounds=LEAD_ROUNDS):
    """求提前量：返回 `(瞄准点, Shot)`；这把枪够不着返回 `(None, None)`。

    `solve(dx, dy)` 是调用方绑好武器和力度的闭式解（`ballistics.solve` 那层），
    `muzzle` / `target` 是绝对坐标，`velocity` 是目标的单位 / tick 速度。

    先按「他站着不动」解一发拿到飞行时间，再把目标往前推那么多 tick 重解，
    重复 `rounds` 轮。任意一轮解不出来就**退回上一轮的解** —— 那说明提前量
    把点推到射程外去了，这时候朝原地打仍然比不打强。
    """
    mx, my = float(muzzle[0]), float(muzzle[1])
    tx, ty = float(target[0]), float(target[1])
    vx, vy = float(velocity[0]), float(velocity[1])
    shot = solve(tx - mx, ty - my)
    if shot is None:
        return (None, None)
    point = (tx, ty)
    for _ in range(max(0, int(rounds))):
        ahead = (tx + vx * shot.ticks, ty + vy * shot.ticks)
        nxt = solve(ahead[0] - mx, ahead[1] - my)
        if nxt is None:
            break
        point, shot = ahead, nxt
    return (point, shot)



class Miss(object):
    """一次**已经掷好**的瞄准失误 —— 在真正开火之前就定下来，不逐帧重掷。

    为什么要「定下来」：准星位置每一帧都要填进心跳（身体朝向跟着它走，
    §37），逐帧重掷的话 bot 会原地抽搐，而且「失误概率」的语义会从
    「每一发」滑成「每一帧」。调用方在**打出一发之后**把它清掉，
    下一发自然重掷 —— 判据是「开了一枪」这个事件，不是时间（铁律 10）。
    """

    __slots__ = ("lead_factor", "offset_ratio")

    def __init__(self, lead_factor, offset_ratio):
        #: 提前量乘的系数（真人「没带提前量」/「带过头」两种错）。
        self.lead_factor = float(lead_factor)
        #: 横向偏开几倍命中窗口（带符号）。
        self.offset_ratio = float(offset_ratio)

    def __repr__(self):
        return ("<Miss lead×%.2f offset×%.2f>"
                % (self.lead_factor, self.offset_ratio))


def roll_error(roll, error_chance):
    """掷一次「这一发准不准」。不失误返回 `None`，失误返回一份 `Miss`。"""
    if _unit(roll) >= float(error_chance):
        return None
    span = MISS_LEAD_MAX - MISS_LEAD_MIN
    lead = (MISS_LEAD_MIN + roll(span + 1)) / 100.0
    spread = MISS_SPREAD_MIN + _unit(roll) * (MISS_SPREAD_MAX
                                              - MISS_SPREAD_MIN)
    if roll(2):
        spread = -spread
    return Miss(lead, spread)


def _offset_point(point, muzzle, distance):
    """把 `point` 沿**垂直于弹道**的方向推开 `distance`。"""
    ax, ay = float(point[0]), float(point[1])
    dx, dy = ax - float(muzzle[0]), ay - float(muzzle[1])
    span = math.hypot(dx, dy)
    if span <= 1e-6:
        return (ax, ay)
    return (ax - dy / span * distance, ay + dx / span * distance)


def aim(solve, muzzle, target, velocity, hit_radius, miss=None):
    """一次完整的瞄准：返回 `(瞄准点, Shot)`；打不到返回 `(None, None)`。

    `miss` 给了就按它把这一发弄歪：先把提前量乘错，解完再横着推开
    `offset_ratio × hit_radius`。这里的 `hit_radius` 必须包住完整目标，
    不能只给身体圆的半径；否则「失误」会变成打头或打腿。

    ⚠ 推歪之后**必须拿新的点重解一次弹道** —— 否则包里带的角度还是正确
    瞄准点的角度，弹体照样打中人，屏幕上看不出任何「打偏」。
    推歪之后反而解不出来（推到射程外了）就退回没推歪的那一发：
    宁可这一发打中，也不要因为「本该失误」而干脆不开枪。
    """
    used = velocity
    if miss is not None:
        used = (float(velocity[0]) * miss.lead_factor,
                float(velocity[1]) * miss.lead_factor)
    point, shot = lead_point(solve, muzzle, target, used)
    if point is None or miss is None:
        return (point, shot)
    # 一单位余量避免相切被算中；近距离还要补偿「偏移点」与射线的夹角。
    # 对半径 R、距离 D 的圆，切线在目标截面上的偏移是 R*D/sqrt(D²-R²)。
    radius = max(1.0, float(hit_radius)) + 1.0
    offset = radius * miss.offset_ratio
    span = math.hypot(point[0] - float(muzzle[0]),
                      point[1] - float(muzzle[1]))
    if span > radius:
        offset *= span / math.sqrt(span * span - radius * radius)
    skewed = _offset_point(point, muzzle, offset)
    nxt = solve(skewed[0] - float(muzzle[0]), skewed[1] - float(muzzle[1]))
    if nxt is None:
        return (point, shot)
    return (skewed, nxt)
