#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ballistics.py —— 原版弹道模型的**服务端复现**（V0.3 M3b-2）。

bot 没有客户端，所以「子弹往哪飞、什么时候到」这件事必须由服务端算 ——
收方只画包里说的东西，一点都不重算（D28 / §42）。这个模块就是把客户端
那几段浮点运算原样搬过来，让服务端能：

    1. 给定目标，解出 `rpFire` 要填的 **angle / power**；
    2. 算出子弹**飞多久**才到 —— `rpExplode` 按这个时间延后发（M3b-2）；
    3. 逐 tick 走一遍弹道，交给 `mapdata` 判**中途会不会撞地形**。

## 模型（§47，逐指令 + 语料双证）

收侧一共只有三段代码：

| 在哪 | 干什么 |
|---|---|
| `0x4920a1` 的三岔口 | 按 `PowerControl` 算**初速矢量** |
| `0x4921c9` | `[弹体+0x314] = 重力项 × GravityFactor` |
| `0x47f603`（弹体 `vft+0x158`，所有弹体类共用） | 每 tick：`v.y += dt × 重力项`；`pos += v × 1.0` |

    tick = 32 ms                       ← 语料回归：速度 = Velocity × 1000/32
    v.y += 1.2 × GravityFactor         ← `0x40a04f` 返回 [ctx+0x344]=1.0 × 1.2
    pos += v                           ← 每 tick 走「一个 v」，不是「v × dt」

⇒ **`Velocity` 的单位是「世界单位 / tick」**，不是「/秒」。这就是 §44 那个
「弹速 100 比人走路还慢」的谜底：100 单位/tick = 3125 单位/秒。

三种初速模式（`PowerControl`）：

| 模式 | 初速 | 备注 |
|---|---|---|
| 0 | `dir × power × Velocity` | 普通枪。`power` 恒 1.0 ⇒ 速度就是 `Velocity` |
| 1 | `dir × power`，超过 `MaxVelocity` 就按比例压回来 | ★ 压回来时**重力也跟着 × 比例²**（`0x492132`），所以 bot 一律取 `power ≤ MaxVelocity`，不进那条路 |
| 2 | `dir × Velocity × ((power + 15) × 0.04)` | 蓄力武器（手雷那一类）。`power = 10` 时恰好是 `Velocity` |

## 只用标准库

服务端的便携运行时里没有第三方包；CPython 3.8（Win7 运行时）也要能跑。
"""
from __future__ import annotations

import math

# ---------------------------------------------------------------------------
#  尺度常量（§47）—— 全部有出处，一个都不是拍脑袋的
# ---------------------------------------------------------------------------
#: 客户端跑弹道的**逻辑步长**，毫秒。
#:
#: 语料实测：把 `rpFire → rpExplode` 按几何配对（爆炸点必须落在开火射线上），
#: 4529 对里算 `距离 / 时间 / Velocity`，慢弹（`Velocity` 20~25、飞行时间长、
#: 延迟占比小）的上包络稳定停在 **31.2~31.3**，而 `1000 / 32 = 31.25`。
#: 换了 8 个不同 `Velocity`（5 / 6 / 20 / 25 / 100 / 130 / 150 / 180）都对得上。
#: ★ 测出来的值只会**偏小**（dt 里含网络和帧延迟），所以取上包络而不是中位。
TICK_MS = 32.0

#: 一秒钟走几个 tick（= 31.25）。把「每 tick」换算成「每秒」用它。
TICKS_PER_SECOND = 1000.0 / TICK_MS

#: 每 tick 加在 `v.y` 上的重力 = `GRAVITY_PER_TICK × GravityFactor`。
#:
#: `0x47f603` 里是 `v.y += GetFrameDt() × [弹体+0x314]`，而
#: `GetFrameDt()`（`0x40a04f`）= `[ctx+0x344] × 1.2`，`[ctx+0x344]` 是个恒 1.0
#: 的逻辑步长 ⇒ 每 tick 恒加 `1.2 × GravityFactor`。
#: 语料实测 `a / GravityFactor` = 1.1972 / 1.1546 / 1.1967 / **1.2000**
#: （四把 `PowerControl=2` 的手雷，共 202 对）。
GRAVITY_PER_TICK = 1.2

#: `PowerControl=2` 的初速系数：`(power + 15) × 0.04`。
#: 出处 `0x49214c: fadd [0x69381c]`（= 15.0）和 `0x492152: fmul [0x693bb8]`（= 0.04）。
POWER2_BIAS = 15.0
POWER2_SCALE = 0.04

#: `PowerControl=0` 的 `power` —— 语料里这一类**恒 1.0**（§43）。
POWER0_FIXED = 1.0

# ---------------------------------------------------------------------------
# ★★★ `PowerControl=2` = **长按鼠标蓄力**（§73）
# ---------------------------------------------------------------------------
#: 蓄力计数器每个逻辑 tick 加多少（`0x516694: add [char+0x594], 2`）。
POWER2_CHARGE_STEP = 2

#: 蓄满的上限（`0x51669d: cmp [char+0x594], 0x50`）。
POWER2_MAX = 80

#: 松手时的下限（`0x5167f1: cmp [char+0x594], 0xf`）——
#: 没蓄够就松手，收方按 15 算。
POWER2_MIN = 15

#: 按住的第一个 tick 就被抬到这里（`0x5166be: cmp …, 4` / `mov …, 4`），
#: 所以蓄力值恒为**偶数**：4, 6, 8, … 80。
POWER2_FLOOR = 4

#: `rpFire +22` 的 `count` 上限：`0x491f41` 里 `>= 30` 整包被丢弃。
MAX_SHOTS = 29

#: 认得的三种初速模式。`weapondata` 只放行这几种（`KNOWN_POWER_MODES`）。
MODE_PLAIN = 0
MODE_DIRECT_POWER = 1
MODE_CHARGE = 2


class Shot(object):
    """一次开火的解：往哪打、用多大力、飞多少个 tick。"""

    __slots__ = ("angle", "power", "speed", "ticks", "gravity", "accel", "cap")

    def __init__(self, angle, power, speed, ticks, gravity, accel=0.0,
                 cap=0.0):
        #: `rpFire +14`，弧度。
        self.angle = angle
        #: `rpFire +18`。
        self.power = power
        #: 初速大小，**单位 / tick**。
        self.speed = speed
        #: 飞到目标要几个 tick（浮点，调用方自己决定怎么取整）。
        self.ticks = ticks
        #: 每 tick 加在 `v.y` 上的量（0 = 直射弹）。
        self.gravity = gravity
        #: 每 tick 沿**飞行方向**加多少速（`Acceleration`，0 = 匀速）。
        self.accel = accel
        #: 加速的上限（`MaxVelocity`）。`accel` 为 0 时没有意义。
        self.cap = cap

    @property
    def seconds(self):
        """飞行时间，秒。"""
        return self.ticks * TICK_MS / 1000.0

    def __repr__(self):
        return ("<Shot %.3frad power=%.1f speed=%.1f/tick %.1ftick(%.2fs)"
                " g=%.2f a=%.2f>"
                % (self.angle, self.power, self.speed, self.ticks,
                   self.seconds, self.gravity, self.accel))


# ---------------------------------------------------------------------------
#  初速：把 `weapon.ini` 的三种模式原样搬过来
# ---------------------------------------------------------------------------

def max_speed(weapon):
    """这把枪打得出来的**最大初速**（单位 / tick）。

    * 模式 0：`power` 恒 1.0 ⇒ 就是 `Velocity`（`MaxVelocity` 对它没用，
      `0x4920a7` 那一支根本不读那一格）；
    * 模式 1：上限是 `MaxVelocity`（没填就退回 `Velocity`）；
    * ★ 模式 2：上限是 **`power = 80`（蓄满）那一档**，`MaxVelocity`
      **够不着**（§73）—— 蓄力计数器封顶在 80（`0x51669d`），
      所以初速最大只有 `Velocity × ((80 + 15) × 0.04)` = `Velocity × 3.8`。
      拿 `MaxVelocity` 当上限会让服务端以为这把枪打得比原版远：
      `ch00-02` 的 `MaxVelocity` 是 60，而真正的上限是 `10 × 3.8 = 38`。

    ★ 模式 1 取到 `MaxVelocity` 为止是有讲究的：再往上收方会把速度按
    `MaxVelocity / |v|` 压回来，**同时把重力乘上那个比例的平方**
    （`0x492132` 写 `[ebp-0x30]`，`0x4921ce` 拿它乘 `GravityFactor`）——
    那条路服务端也复现得了，但没必要走，多一个分支就多一处能算错的地方。
    """
    mode = weapon.power_control
    if mode == MODE_PLAIN:
        return weapon.velocity * POWER0_FIXED
    if mode == MODE_CHARGE:
        return speed_for_power(weapon, POWER2_MAX)
    top = weapon.max_velocity or weapon.velocity
    return float(top)


def charge_power(weapon, speed):
    """蓄力武器要扔出 `speed` 这么快，得蓄到几（返回**合法**的 `power`）。

    合法值只有 `{15} ∪ {16, 18, …, 80}`（语料 3036 发 `PowerControl=2` 的
    `rpFire` 里一个例外都没有，§73）：蓄力计数器每 tick `+2`、第一个 tick
    被抬到 4、松手时再夹进 `[15, 80]`。

    往**上**取整 —— 蓄不够就够不着，宁可多蓄一点点。
    """
    raw = power_for_speed(weapon, speed)
    if raw <= POWER2_MIN:
        return POWER2_MIN
    power = int(math.ceil(raw))
    if power % 2:
        power += 1                     # 只能是偶数
    return min(POWER2_MAX, max(POWER2_MIN, power))


def charge_ticks(power):
    """蓄到 `power` 要按住几个逻辑 tick（一个 tick = `TICK_MS`）。

    收方的计数器：第 k 个 tick 上是 `max(4, 2k)`，松手时再夹到 `>= 15`。
    ⇒ 蓄到偶数 `p >= 16` 要 `p / 2` 个 tick；`p = 15`（没蓄够就松手）
    最少按 **1** 个 tick。

    ★ 这不是我们定的节流阈值（铁律 10）—— 它是原版按键判定的直接换算，
    真人扔一颗蓄满的手雷就得按住 `80 / 2 = 40` 个 tick = 1.28 秒。
    """
    power = int(power)
    if power <= POWER2_MIN:
        return 1
    return power // POWER2_CHARGE_STEP


def power_for_speed(weapon, speed):
    """反解 `rpFire +18` 要填的 `power`，使初速正好是 `speed`。

    模式 0 只有一个合法值（1.0），`speed` 对不上也照填 —— 那一格对它没有意义。
    """
    mode = weapon.power_control
    if mode == MODE_PLAIN:
        return POWER0_FIXED
    if mode == MODE_DIRECT_POWER:
        return float(speed)
    # 模式 2：speed = Velocity × ((power + 15) × 0.04)
    velocity = weapon.velocity or 1.0
    return float(speed) / velocity / POWER2_SCALE - POWER2_BIAS


def speed_for_power(weapon, power):
    """正向：给定 `power` 算初速大小（单位 / tick）。`max_speed()` 的逆。

    ★ 模式 1 超过 `MaxVelocity` 的那一段这里**也照压**，好让
    `power_for_speed()` / `speed_for_power()` 在整个定义域上互为逆函数 ——
    单测拿它对拍。
    """
    mode = weapon.power_control
    if mode == MODE_PLAIN:
        return weapon.velocity * float(power)
    if mode == MODE_DIRECT_POWER:
        top = weapon.max_velocity
        speed = float(power)
        return min(speed, float(top)) if top else speed
    return weapon.velocity * ((float(power) + POWER2_BIAS) * POWER2_SCALE)


def accel_per_tick(weapon):
    """每 tick 沿飞行方向加多少速（`Acceleration`）；没这一格返回 0。

    出处 `0x47de6a`（`BulletObj` 的 `vft+0x24`，§49）：

        dir = normalize(v)                        ; `0x56976d`
        v += dir × [weapon+0x2c]                  ; ★ Acceleration
        if |v| > [weapon+0x28]: v *= [weapon+0x28] / |v|   ; 压回 MaxVelocity

    ★ 表里 7 把带 `Acceleration` 的武器**全是 `GravityFactor = 0` 的直射弹**，
    所以加速只改「飞多久」，不改方向 —— 弹道还是一条直线。
    带重力又带加速的武器一把都没有，真出现了这边直接当没加速处理
    （`solve()` 里那个 `if`）。
    """
    return float(getattr(weapon, "acceleration", 0.0) or 0.0)


def _travelled(speed, accel, cap, ticks):
    """加速弹飞 `ticks` 个 tick 走了多远（逐 tick 累加，和收方一模一样）。"""
    if not accel:
        return speed * ticks
    whole = int(ticks)
    total = 0.0
    velocity = speed
    for _ in range(whole):
        velocity = min(cap, velocity + accel) if cap else velocity + accel
        total += velocity
    frac = ticks - whole
    if frac > 0.0:
        velocity = min(cap, velocity + accel) if cap else velocity + accel
        total += velocity * frac
    return total


def _ticks_for_distance(speed, accel, cap, distance):
    """`_travelled()` 的反函数：走 `distance` 要几个 tick。够不着返回 `None`。"""
    if not accel:
        return distance / speed if speed > 0 else None
    total = 0.0
    velocity = speed
    for tick in range(1, MAX_FLIGHT_TICKS + 1):
        velocity = min(cap, velocity + accel) if cap else velocity + accel
        if total + velocity >= distance:
            return tick - 1 + (distance - total) / velocity
        total += velocity
    return None


def gravity_per_tick(weapon):
    """每 tick 加在 `v.y` 上的量。直射弹（`GravityFactor` 缺省 / 0）返回 0。

    ★ **+y 是重力的方向**（世界坐标里 y 往下增长）：语料拟合出来的
    `a` 是正的，和心跳里 `y` 越大越低是一致的（§24 / §36）。
    """
    return GRAVITY_PER_TICK * weapon.gravity


# ---------------------------------------------------------------------------
#  解弹道
# ---------------------------------------------------------------------------

def solve(weapon, dx, dy, speed=None):
    """从枪口打到 `(dx, dy)` 这个**相对**位移，返回 `Shot`；打不到返回 `None`。

    `dx > 0` 是往右，`dy > 0` 是往下（和包里的坐标系一致）。
    `speed` 不给就用 `max_speed()` —— 最平的那条弹道，飞得最快、最好看。

    ## 直射弹（`GravityFactor = 0`）

    一条直线，`angle = atan2(dy, dx)`，`ticks = 距离 / 速度`。

    ## 抛物线

    离散递推是 `v.y += a; pos += v`，所以 n 个 tick 之后

        x = n·s·cosθ
        y = n·s·sinθ + a·n(n+1)/2

    比连续模型多出来一个 `a·n/2`（第一个 tick 就已经带上了一整格重力）。
    把它挪到右边当成「目标点其实要低一点」，剩下的就是课本上那条
    抛射方程，对 `tanθ` 是一元二次：

        (a·dx² / 2s²)·T² + dx·T + (a·dx² / 2s² − dy') = 0

    解出 T 之后 n 跟着定，再拿新的 n 修正 `dy'`，迭代几轮就收敛
    （偏差是 O(a·n/2)，每轮缩一个数量级）。判别式 < 0 = **超出射程**。

    ★ 两个根分别是「低抛」和「高抛」。取**低抛**（|T| 小的那个）：飞行时间短、
    中途撞地形的机会少，也更像人打出来的。
    """
    gravity = gravity_per_tick(weapon)
    if speed is None:
        speed = max_speed(weapon)
    if speed <= 0:
        return None

    if not gravity:
        span = math.hypot(dx, dy)
        if span <= 0:
            return None
        # ★ 加速弹（`Acceleration`）走的还是直线，只是越飞越快。
        accel = accel_per_tick(weapon)
        cap = float(weapon.max_velocity or 0.0) if accel else 0.0
        ticks = _ticks_for_distance(speed, accel, cap, span)
        if ticks is None:
            return None
        return Shot(math.atan2(dy, dx), power_for_speed(weapon, speed),
                    speed, ticks, 0.0, accel, cap)

    # ★ 竖直方向的特例：dx = 0 时 tanθ 发散，直接按「垂直上抛 / 下抛」算。
    if abs(dx) < 1e-6:
        return _solve_vertical(weapon, dy, speed, gravity)

    flip = dx < 0
    span_x = abs(dx)
    coef = gravity * span_x * span_x / (2.0 * speed * speed)

    best = None
    ticks_guess = span_x / speed          # 先当水平飞，够近了
    for _ in range(6):
        # 离散递推比连续模型多掉 `a·n/2`，所以目标点等效地抬高这么多。
        target = dy - gravity * ticks_guess / 2.0
        disc = span_x * span_x - 4.0 * coef * (coef - target)
        if disc < 0:
            return None                    # 超出射程：这把枪够不着
        root = math.sqrt(disc)
        # 两个根取 |tanθ| 小的那个 = 低抛。
        t1 = (-span_x + root) / (2.0 * coef)
        t2 = (-span_x - root) / (2.0 * coef)
        tangent = t1 if abs(t1) <= abs(t2) else t2
        angle = math.atan(tangent)
        cos = math.cos(angle)
        if abs(cos) < 1e-9:
            return None
        ticks = span_x / (speed * cos)
        if ticks <= 0:
            return None
        if best is not None and abs(ticks - ticks_guess) < 1e-6:
            ticks_guess = ticks
            best = angle
            break
        ticks_guess = ticks
        best = angle
    if best is None:
        return None
    angle = best
    if flip:
        angle = math.pi - angle
    return Shot(_wrap(angle), power_for_speed(weapon, speed), speed,
                ticks_guess, gravity)


def _solve_vertical(weapon, dy, speed, gravity):
    """`dx = 0` 的退化情形：只能笔直往上或往下打。"""
    for direction in (-1.0, 1.0):          # 先试往上（-y），再试往下
        vy = direction * speed
        # y(n) = n·vy + a·n(n+1)/2 = 0 的正根
        aa = gravity / 2.0
        bb = vy + gravity / 2.0
        cc = -dy
        disc = bb * bb - 4.0 * aa * cc
        if disc < 0:
            continue
        ticks = (-bb + math.sqrt(disc)) / (2.0 * aa)
        if ticks > 0:
            return Shot(math.pi / 2.0 if direction > 0 else -math.pi / 2.0,
                        power_for_speed(weapon, speed), speed, ticks, gravity)
    return None


def launch(weapon, angle, power):
    """★ **不解目标**、直接按给定的角度和力度造一个 `Shot`（§81）。

    `solve()` 走的是「我要打到那一点，角度和力度该是多少」；有些弹体
    根本不瞄人 —— 分裂弹的碎片就是照着 `SliceAngle*` 那个扇形往外撒的
    （`0x47c9ae` 的循环），角度和力度都是**给定**的。

    `ticks` 填 0：碎片没有「飞到目标要几个 tick」这回事，它的上界由
    `bot._shell_max_ticks()`（图的对角线 / 引信）决定。
    """
    speed = speed_for_power(weapon, power)
    accel = accel_per_tick(weapon)
    cap = float(weapon.max_velocity or 0.0) if accel else 0.0
    return Shot(_wrap(float(angle)), float(power), speed, 0.0,
                gravity_per_tick(weapon), accel, cap)


def _wrap(angle):
    """把角度归到 `(-π, π]` —— 客户端 `atan2` 出来的就是这个范围。"""
    while angle <= -math.pi:
        angle += 2.0 * math.pi
    while angle > math.pi:
        angle -= 2.0 * math.pi
    return angle


# ---------------------------------------------------------------------------
#  走一遍弹道（给地形遮挡判定和单测用）
# ---------------------------------------------------------------------------

def position_at(x0, y0, shot, ticks):
    """开火 `ticks` 个 tick 之后弹体在哪。**逐 tick 递推的闭式解**。

        x = x0 + n·s·cosθ
        y = y0 + n·s·sinθ + a·n(n+1)/2

    加速弹（`Acceleration`，全是直射）另走一条：沿着同一个方向，
    走过的距离由 `_travelled()` 逐 tick 累出来。
    """
    if shot.accel:
        span = _travelled(shot.speed, shot.accel, shot.cap, ticks)
        return (x0 + span * math.cos(shot.angle),
                y0 + span * math.sin(shot.angle))
    vx = shot.speed * math.cos(shot.angle)
    vy = shot.speed * math.sin(shot.angle)
    return (x0 + ticks * vx,
            y0 + ticks * vy + shot.gravity * ticks * (ticks + 1.0) / 2.0)


def path_points(x0, y0, shot, segments=None):
    """把弹道切成一串点，**首尾都在里面**，供逐段做直线遮挡判定。

    直射弹只要首尾两点（那本来就是一条直线）。抛物线按飞行 tick 数切，
    每段不超过 `MAX_SEGMENT_TICKS` 个 tick —— 段内当直线看的误差是
    `a × (段长)² / 8`，取 4 个 tick 时约 `2.4 × GravityFactor` 个单位，
    比地形位图一个像素还小。
    """
    if not shot.gravity:
        return [(x0, y0), position_at(x0, y0, shot, shot.ticks)]
    if segments is None:
        segments = max(1, int(math.ceil(shot.ticks / MAX_SEGMENT_TICKS)))
    return [position_at(x0, y0, shot, shot.ticks * i / float(segments))
            for i in range(segments + 1)]


#: 抛物线切段时每段最多几个 tick（见 `path_points`）。
MAX_SEGMENT_TICKS = 4.0

#: 加速弹反解飞行时间时最多推几个 tick。
#: 128 秒 —— 到这儿还没飞到就是「够不着」，不是「再等等」。
#: ★ 这不是时序阈值，是数值迭代的收敛上界（防死循环）。
MAX_FLIGHT_TICKS = 4000
