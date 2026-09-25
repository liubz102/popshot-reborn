#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""botmove.py —— **角色走路 / 起跳 / 腾空**的服务端复现（V0.3 M5；X_Mod §102 / §104 / §105 起逐指令照抄客户端）。

`ballistics.py` 是子弹的运动，这个文件是**人**的运动。三处用的是**同一套**，都是「本人那台客户端」
（`MyCharacter`）一个逻辑帧的样子：bot 自己走（服务端就是它的本机）、外推真人（服务端替他自己那台算，
`bot._advance_humans`）、可达图（`botnav` 拿它逐格模拟建边）。

## 一帧的顺序（X_Mod §105）

    ① `[+0x518]`（按 ↓ 穿白线的计数）> 0 就减 1（`0x4fe329`）；还 > 0 时白线不挡（`vft+0x100` 为假）
    ② 踩地（`[+0x128]`）才走路：dist = S × 方向 × 装备走速 × 冲刺 1.5 × 蹲 ⅓ → `0x50d9a7`
       （余量 `[+0x130]` 累加、跨帧保留，1 px 一列推进：上坎 ≤ 20、下坎 ≤ 10，每步花 √(1+dy²)）
    ③ 物理：vx += 空中操控 → `0x50d404`（踩地：vy < 0 按 v 挪一次、否则 v 清零；脚下空 ⇒ 离地、
       同一帧再腾空一步）→ vx −= 操控
    ④ 弹跳台（`JumpingObj::Tick`，排在角色之后）：踩地、←/→/↓ 都没按 ⇒ 写 v、**不清踩地位**
    ⑤ 本人输入 `0x51558f`：踩地操控清 0；腾空按左 / 右 ⇒ 操控 ±步长；按着 ↓ ⇒ `[+0x518] = 8`
    ⑥ `rpJump` 回环执行（起跳 `0x501d57`）：vy = −√(2g·h)、vx = ±¼S、当场离地

S = `vft+0x128` = 状态倍率 × ChrSpeed（不含冲刺 / 蹲 / 装备）。

## 坐标

**y 往下为正**（和 `mapdata` / 心跳一致）。`Body.(x, y)` 是**脚**，客户端把它放在实心第一行的
**上面一行**（走路 `0x50da83`、落地 `0x50f1d2`；1682 发静态地面心跳全是这样）—— V0.3 那套放在
站立面那一行上，差 1 px。主线程 x87 是 24 位精度（D3D 没带 `FPU_PRESERVE`）⇒ 每一步按 f32 舍入。

## 只用标准库；CPython 3.8 也要能跑（Win7 运行时）
"""
from __future__ import annotations

import math
import struct

import ballistics
import mapdata

#: 逻辑步长（毫秒）—— 人和子弹用的是同一套（§47）。
TICK_MS = ballistics.TICK_MS
TICKS_PER_SECOND = ballistics.TICKS_PER_SECOND


_F32 = struct.Struct("<f")
_F32_PACK = _F32.pack
_F32_UNPACK = _F32.unpack


def _f32(value):
    """按 f32 存一次（主线程 x87 是 24 位精度，D3D 没带 FPU_PRESERVE —— 每一步都这么舍入）。

    ★ 热路径（可达图预热每帧几十次）：绑定好的 `Struct` 方法，线程安全（预热线程和房间线程都在用）。
    """
    return _F32_UNPACK(_F32_PACK(value))[0]


#: 重力，单位 / tick²。`[0x693784] = 1.2f`，`0x40a04f` 乘上 stage 的重力系数（恒 1.0）返回 ——
#: 子弹的 `1.2 × GravityFactor` 用的也是这一句，人和子弹共用同一个重力。
GRAVITY = 1.2
G32 = _f32(GRAVITY)

#: ★★★ 起跳高度（X_Mod §102）：一段 `0x501d71` 180、二段 `0x501d7a` 240。
JUMP_HEIGHT = 180.0
DOUBLE_JUMP_HEIGHT = 240.0


def launch_speed(stage=1):
    """起跳初速的大小：`f32(√(2·g·h))`（`0x501ee1`：`g×h` 存 f32 → `fadd st0,st0` → sqrt → f32）。"""
    height = DOUBLE_JUMP_HEIGHT if stage == 2 else JUMP_HEIGHT
    return _f32(math.sqrt(2.0 * _f32(G32 * height)))


#: 一段跳初速（向上）= **20.784611**。V0.3 语料量到的「−20」是心跳**截断**的结果（`0x5040f1`），
#: 「起跳后第一格不加重力、位移正好 −20」同样是 y 截断的假象（X_Mod §102 订正 §87）。
JUMP_SPEED = launch_speed(1)

#: ★★ **第二段跳**的初速 = √(2·1.2·240) = **24.0**。第二段是把 `v.y` **重新置成**这个数（不是叠加）。
DOUBLE_JUMP_SPEED = launch_speed(2)

#: ★★ 起跳那一刻的水平速度 = ±¼ × S（`0x501e81` −0.25 / `0x501e9f` +0.25）；
#: 侧边那一格（x±1, y）是 2/3、或者没按左右键 ⇒ 0。**两段都重算**。
JUMP_VX_RATIO = 0.25

#: ★★ **空中操控**（`[+0x4c4]`，只有本人那台的输入处理 `0x51558f` 有）：腾空、没上锁 `[+0x5d5]`、
#: 侧格是 0/1 时按住左 / 右，每帧 ∓/± 步长；步长从 2.0 起每用一次减 0.02、最低 0.2
#: （`0x515ee1` / `0x515ef8`）；两键都没按 ⇒ 步长回 2.0、解锁（`0x515f67` / `0x515f6e`）；钳到 ±S。
AIR_CONTROL_STEP = 2.0
AIR_CONTROL_STEP_DECAY = _f32(0.02)       # [0x693908]
AIR_CONTROL_STEP_MIN = _f32(0.2)          # [0x693748]

#: ★★ 上升计时器 `[+0x4f4]` 还在跑时撞上**任何东西**：`vx_eff × 0.75`、`vy = 0`、停表，
#: 不落地不反弹不上锁（`0x502f42`：`0x5d5eb0` 在跑 ⇒ `[0x6938c8]`，再 `0x5d5e54` 停表）。
RISE_BUMP_KEEP_VX = 0.75


def rise_ticks(speed):
    """起跳后有几帧在上升计时器里（`0x501f0d`：时长 `ftol(|v0 / g|)` 个逻辑帧）。

    计时器在起跳那一帧（输入处理里）起跑、`elapsed < 时长` 算在跑 ⇒ 之后第 1 ~ 时长−1 帧在计时器里。
    除法在 24 位精度下做：一段 17.32 → 17、二段 `24/1.2f` 舍成 20.0 → 20。
    """
    return max(0, int(abs(_f32(speed / G32))) - 1)


#: 按着右键冲刺跑：`GameProps.ini` 的 `FastRunRate`（`0x507567` 读它；体力不够那一帧就不乘，
#: 还把冲刺位清掉 —— 体力那一本账在 `bot._regen_stamina`）。
FAST_RUN_RATE = 1.5

#: 蹲着走：`0x507607` 乘 `[0x69387c]` = 0.33333334f。
CROUCH_FACTOR = _f32(1.0 / 3.0)

#: 装备走速（`GetEquipBonus` 第 4 格，百分比）：`(x + 100) × [0x693724]`（`0x5074f9`）。
EQUIP_PERCENT = _f32(0.01)

#: 走路一列最多往上爬 / 往下落几格（`vft+0x108` = `0x501b7c` / `vft+0x10c` = `0x501b92`）；
#: 冲刺攻击的计时器 `[+0x5c0]` 在跑时换成后两个。
WALK_UP_MAX = 20
WALK_DOWN_MAX = 10
DASH_UP_MAX = 10
DASH_DOWN_MAX = 0

#: 走出崖边那一步踩地时顺手写的 `vy = 8g`（`0x50dac9`）—— 紧接着 `0x50d404` 见「踩地且 vy ≥ 0」
#: 就清零，所以只会顶掉弹跳台刚写、还没挪的那一份速度。
WALK_OFF_VY = _f32(8.0 * G32)

#: 按着 ↓ 每帧把 `[+0x518]` 置成这个数（`0x516207`），角色那一格先减 1 ⇒ 松开后还有 7 帧白线不挡。
DROP_HOLD_FRAMES = 8

#: 只给**启发式**用的「一步能爬多陡」（`bot` 挑站位、`botnav` 的依赖区）；物理本身是逐列的
#: `WALK_UP_MAX` / `WALK_DOWN_MAX`。语料 88875 发上坡心跳的 p99 = 2.0。
CLIMB_SLOPE = 2.0

#: 一发心跳等于几个 tick。★ 只在「没有真实时间可依据」的地方当兜底用。
TICKS_PER_BEAT = 4

#: ★★★ **弹跳台**的作用半径（V0.3 §99）：`JumpingObj` 构造 `0x510ade` 写的 20.0，
#: 判定是它和**角色的碰撞圆**静态相交（`0x50f410`，和子弹撞人同一个函数）。
JUMP_PAD_RADIUS = 20.0

#: ★ 台子给的目标点还要再减这一项 × `[char+0x3c4]`（= 2·ChrSizeLegs + ChrSizeBody，`0x4fc49a`）：
#: `0x510e68 … fmul 0.25 … fsubp`（X_Mod §104）。以前当成「× 重力」减 0.3 是读错了。
JUMP_PAD_HEIGHT_BIAS = 0.25

#: ★★ 按着这几个键台子**不弹**（`0x510dd3` ← / `0x510de0` → / `0x510ded` ↓，X_Mod §104）；↑ 不拦。
#: 值是心跳按键掩码那几位（`botsync.KEY_LEFT` / `KEY_RIGHT` / `KEY_DOWN`）。
PAD_BLOCK_LEFT = 0x01
PAD_BLOCK_RIGHT = 0x04
PAD_BLOCK_DOWN = 0x08
PAD_BLOCK_KEYS = PAD_BLOCK_LEFT | PAD_BLOCK_RIGHT | PAD_BLOCK_DOWN

#: 腾空撞上东西时，速度（模）不超过它才算落地：`Character` vft+0xa4 = `0x4febe0`（常态 35）。
CLIENT_LAND_SPEED = 35.0

#: 撞上后的反射（`0x50f240`，弹体用的也是这个函数）：切向留 1 − 0.5（vft+0x98），法向 × −0.2（vft+0x94）。
CLIENT_BOUNCE_FRICTION = 0.5
CLIENT_BOUNCE_RESTITUTION = 0.2

#: `Character` 撞后响应 vf+0xa8 = `0x502df4`：计时器不在跑那一支，`0x50efd2` 之后再
#: `vx *= [0x6937e4]`（0.3f）、`[+0x5d5] = 1`（上锁）、`0x4face2`（操控复位）。
CLIENT_BOUNCE_KEEP_VX = _f32(0.3)

#: 反射前量朝向的 7×7 投票（`0x473b36`，§110）。
CLIENT_VOTE_WINDOW = 3


class Body(object):
    """一个角色此刻的运动状态（本人那台客户端上的）。**不可变**：每一帧返回一个新的。

    * `vx / vy`：`[+0x120 / +0x124]`，**不含**空中操控；踩地时恒 0（台子刚弹的那一格除外，见 `pad`）；
    * `ctl` / `ctl_step` / `ctl_lock`：空中操控量 `[+0x4c4]`、步长 `[+0x4c0]`、锁 `[+0x5d5]`。
      物理里水平速度用 `vx + ctl`，心跳报 `trunc(vx + ctl)`（`reported_vx`）。
      锁缺省：踩地的身体当作「刚落过地」= 上锁（每次落地 `0x502f93` 都置 1，只有起跳 / 腾空时松开
      左右键才解开）；腾空的身体缺省不锁（Init `0x4fb6f2` 清 0）；
    * `rise`：起跳后还有几帧在上升计时器里（`[+0x4f4]`）；
    * `pad`：弹跳台刚写了速度、**踩地位还没清**（`0x510e91` 不写 `[+0x128]`）—— 下一帧先按踩地分支
      挪一次、再腾空（「走两步」）。这时 `on_ground` 记成假，但心跳那一位要报真（`reported_on_ground`）；
    * `rest`：走路余量 `[+0x130]`（跨帧保留，撞墙清零）；
    * `drop`：按 ↓ 穿白线的计数 `[+0x518]`；
    * `air_jumped`：这一段腾空里第二段跳用掉了没有（`rpJump` 的段号只有 1 / 2）。
    """

    __slots__ = ("x", "y", "vx", "vy", "on_ground", "air_jumped",
                 "ctl", "ctl_step", "ctl_lock", "rise", "pad", "rest", "drop")

    def __init__(self, x, y, vx=0.0, vy=0.0, on_ground=True,
                 air_jumped=False, ctl=0.0, ctl_step=AIR_CONTROL_STEP,
                 ctl_lock=None, rise=0, pad=False, rest=0.0, drop=0):
        self.x = float(x)
        self.y = float(y)
        self.on_ground = bool(on_ground)
        self.vx = 0.0 if on_ground else float(vx)
        self.vy = 0.0 if on_ground else float(vy)
        self.air_jumped = False if on_ground else bool(air_jumped)
        self.ctl = 0.0 if on_ground else float(ctl)
        self.ctl_step = float(ctl_step)
        self.ctl_lock = self.on_ground if ctl_lock is None else bool(ctl_lock)
        self.rise = 0 if on_ground else int(rise)
        self.pad = False if on_ground else bool(pad)
        self.rest = float(rest)
        self.drop = int(drop)

    def moved(self, x, y, vx=0.0, vy=0.0, on_ground=True, air_jumped=None,
              **extra):
        """派生一个新状态。`air_jumped` 和那几格本人状态不给就**沿用自己的**（`pad` 恒不沿用）。"""
        return Body(x, y, vx, vy, on_ground,
                    self.air_jumped if air_jumped is None else air_jumped,
                    ctl=extra.get("ctl", self.ctl),
                    ctl_step=extra.get("ctl_step", self.ctl_step),
                    ctl_lock=extra.get("ctl_lock", self.ctl_lock),
                    rise=extra.get("rise", self.rise),
                    pad=extra.get("pad", False),
                    rest=extra.get("rest", self.rest),
                    drop=extra.get("drop", self.drop))

    @property
    def reported_on_ground(self):
        """心跳位域 bit2 该报什么（`[+0x128]`）：台子刚弹、还没挪的那一格报**踩地**（X_Mod §104）。"""
        return self.on_ground or self.pad

    @property
    def reported_vx(self):
        """心跳里的 vx 那一格之前的值：`[+0x4c4] + [+0x120]`（`0x5040dc`，发包时再截断）。"""
        return _f32(self.vx + self.ctl)

    def _key(self):
        return (self.x, self.y, self.vx, self.vy, self.on_ground,
                self.air_jumped, self.ctl, self.ctl_step, self.ctl_lock,
                self.rise, self.pad, self.rest, self.drop)

    def __eq__(self, other):
        return isinstance(other, Body) and self._key() == other._key()

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self):
        return ("<Body (%.1f, %.1f) v=(%.1f, %.1f) %s%s>"
                % (self.x, self.y, self.vx + self.ctl, self.vy,
                   "地上" if self.on_ground else "空中",
                   " 台" if self.pad else ""))


def walk_speed(character, fast_run=False, crouched=False, scale=1.0):
    """一帧**大约**走多远（启发式用：寻路代价、挑站位）。真走路是 `walk_distance` + 逐列推进。"""
    speed = float(getattr(character, "speed", 7.0) or 7.0)
    if fast_run:
        speed *= FAST_RUN_RATE
    if crouched:
        speed *= CROUCH_FACTOR
    return speed * float(scale)


def control_speed(character, scale=1.0):
    """S = `vft+0x128` = 状态倍率 × ChrSpeed（**不含**冲刺 / 蹲 / 装备）：起跳 vx、空中操控的上限、走路的底数。"""
    return _f32(float(getattr(character, "speed", 7.0) or 7.0) * float(scale))


def walk_distance(character, direction, fast_run=False, crouched=False,
                  scale=1.0, bonus=0):
    """这一帧交给 `0x50d9a7` 的路程（`0x5074ef` ~ `0x507676`）。

        倍率 = (装备走速 + 100) × 0.01 ；冲刺 × FastRunRate ；蹲 × ⅓
        dist = f32(f32(S × 方向) × 倍率)
    """
    mult = _f32((int(bonus) + 100) * EQUIP_PERCENT)
    if fast_run:
        mult = _f32(mult * FAST_RUN_RATE)
    if crouched:
        mult = _f32(mult * CROUCH_FACTOR)
    return _f32(_f32(control_speed(character, scale) * direction) * mult)


def jump_apex():
    """一次跳最高能上升多少（`v² / 2g`）= 起跳高度 180（`0x501d71`）。"""
    return JUMP_SPEED * JUMP_SPEED / (2.0 * GRAVITY)


# ---------------------------------------------------------------------------
# 地形查询
# ---------------------------------------------------------------------------
def _solid(terrain, x, y):
    """挡得住**人**吗（单向平台算挡，图外算挡）。"""
    return terrain.is_solid(int(x), int(y))


def _client_cell(terrain, x, y):
    """角色这一路问的格子（`0x473969`：静态格、破坏物、这一刻的移动平台取 max）。

    ★ 出界**四面都是 2**（`0x472fe0`：x < 0 / y < 0 / x ≥ 宽 / y ≥ 高 一律 `mov al, 2`，X_Mod §105）。
      V0.3 §192 的「图顶不挡头」是把弹体的实测（V0.3 §83）挪过来的：真人脚到过 y = 38、头伸出图顶，
      是**走路**只问脚那一列、不问头；腾空扫掠头一碰到 y < 0 就被挡（远端那份脚 y = 72.9 就顶住了）。
    """
    return terrain.cell(x, y)


def surface_near(terrain, x, y, reach):
    """第 `x` 列上离 `y` 最近、上下各 `reach` 以内的站立面（实心第一行）；没有返回 `None`。

    ★ 这是**启发式**（挑站位、火墙铺火），不是走路 —— 走路是 `_client_walk` 逐列推进。
    """
    best = None
    for sy in terrain.surfaces(int(x)):
        gap = sy - y
        if -reach <= gap <= reach:
            if best is None or abs(gap) < abs(best - y):
                best = sy
        elif sy > y + reach:
            break                      # surfaces 是自上而下的，再往下更远
    return best


# ---------------------------------------------------------------------------
# 走路（`0x50d9a7`）
# ---------------------------------------------------------------------------
#: 一步 `(±1, dy)` 花掉的路程 `f32(√(1 + dy²))`（`0x50db84` / `0x50dd9a`）。
_WALK_COST = dict((dy, _f32(math.sqrt(1.0 + dy * dy)))
                  for dy in range(-max(WALK_UP_MAX, DASH_UP_MAX),
                                  max(WALK_DOWN_MAX, DASH_DOWN_MAX) + 1))


def _client_walk(terrain, x, y, rest, dist, up=WALK_UP_MAX, down=WALK_DOWN_MAX):
    """`0x50d9a7(dist)`：余量 += dist，1 px 一列推进；返回 `(x, y, 余量, 走没走出崖边)`。

    * 余量 ≤ −1 往左、≥ 1 往右；下一列**脚那一行**是空的 ⇒ 往下找 1..`down` 第一格非空 i ⇒ 这一步
      `(±1, i−1)`，找不到 ⇒ `(±1, 0)`（走出崖边，悬空那几列照样水平走完）；
    * 不是空的（白线也算挡）⇒ 往上找 1..`up` 第一格空 i ⇒ `(±1, −i)`；找不到 = 撞墙：**余量清零**、停；
    * 每步花 √(1+dy²)；这一步会把余量越过 0 ⇒ 这一步不走、余量不变、停（所以陡坡走得慢，
      20 px 的坎要攒够余量才迈得上去）。
    """
    f32 = _f32
    cell = terrain.cell
    costs = _WALK_COST
    rest = f32(rest + dist)
    off = False
    while True:
        if rest <= -1.0:
            sx = -1
        elif rest >= 1.0:
            sx = 1
        else:
            return x, y, rest, off
        fx, fy = int(x), int(y)
        col = fx + sx
        if cell(col, fy) == 0:
            i = 1
            while i <= down and cell(col, fy + i) == 0:
                i += 1
            if i <= down:
                dy = i - 1
            else:
                dy = 0
                off = True
        else:
            i = 1
            while i <= up and cell(col, fy - i) != 0:
                i += 1
            if i > up:
                return x, y, 0.0, off              # 撞墙（`0x50db3a` / `0x50dd47`）
            dy = -i
        cost = costs[dy]
        if sx < 0:
            new = f32(rest + cost)
            if new > 0.0:
                return x, y, rest, off             # 越过 0：这一步不走（`0x50dbc0`）
        else:
            new = f32(rest - cost)
            if new < 0.0:
                return x, y, rest, off             # `0x50ddd0`
        x = f32(x + sx)
        y = f32(y + dy)
        rest = new


def walk_by(terrain, body, dist, up=WALK_UP_MAX, down=WALK_DOWN_MAX):
    """让 `0x50d9a7` 走一段**不是按键走出来的**路程（挨打乙档滑 `push.x × 3`、被抓住拖着走）。

    余量是同一格 `[+0x130]`，踩没踩地不在这里改（下一帧的物理自己看脚下）。
    """
    if terrain is None or not dist:
        return body
    x, y, rest, _off = _client_walk(terrain, body.x, body.y, body.rest, dist,
                                    up, down)
    return body.moved(x, y, body.vx, body.vy, on_ground=body.on_ground,
                      rest=rest, pad=body.pad)


# ---------------------------------------------------------------------------
# 腾空（`Character` vf+0x70 = `0x50d58a` → `0x50e759`；撞上了 vf+0xa8 = `0x502df4`）
# ---------------------------------------------------------------------------
def _cdiv(a, b):
    """C 的整数除法（`cdq / idiv`）：**向零截断**。`b` 不为 0。"""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b > 0) else -q


#: `id(角色) -> (角色, {蹲没蹲: 形状表})`。chrprops 的角色对象有 `__slots__`、挂不上属性，
#: 按对象身份缓存（值里留着对象本身，id 不会被复用）。
_SHAPE_CACHE = {}


def _shape_table(character, crouched):
    """形状表 `[(圆心dx, 圆心dy, 半径, 掩码), …]`，相对脚点，按客户端的顺序（腿 → 身 → 头）。"""
    entry = _SHAPE_CACHE.get(id(character))
    if entry is None or entry[0] is not character:
        entry = (character, {})
        _SHAPE_CACHE[id(character)] = entry
    table = entry[1].get(crouched)
    if table is None:
        shapes = getattr(character, "hit_shapes", None)
        if shapes is not None:
            table = tuple((cx, cy, r, flags)
                          for cx, cy, r, _region, flags in shapes(0.0, 0.0, crouched))
        else:
            # 只给了尺寸的假角色（单测）：同一种摆法（从脚底往上依次相切），掩码同上。
            legs, body, head = _shape_sizes(character)
            table = ((0.0, -legs, legs, 4),
                     (0.0, -2.0 * legs - body, body, 0),
                     (0.0, -2.0 * (legs + body) - head, head, 2))
        entry[1][crouched] = table
    return table


def _shape_sizes(character):
    """`(腿, 身, 头)` 三个半径，带缺省（同 `fits()`）。"""
    return (float(getattr(character, "size_legs", 12.0) or 12.0),
            float(getattr(character, "size_body", 13.0) or 13.0),
            float(getattr(character, "size_head", 10.0) or 10.0))


def client_probes(character, vx, vy, crouched=False, holds=True):
    """腾空扫掠的探针表 `((dx, dy, 单向平台挡不挡), …)`，相对脚点（`0x50e759` 开头那一段）。

    * 探针 0 = 脚底（`vft+0x104` = `(0, 0)`）。单向平台**只挡它**：位集 `0x737ebc` 里它那一位
      写死 1，每个圆那一位取自掩码 `+0xc` 的最低位 —— 腿 4 / 身 0 / 头 2 全是 0；
    * 之后按形状表的顺序（腿 → 身 → 头）各取一个前沿点 `ftol(圆心 + r·v̂)`；
    * `holds=False`（按 ↓ 穿白线那几帧，`vft+0x100` 为假）⇒ 谁都不认白线。

    `(vx, vy)` 不能是零向量（调用方先拦，`0x50e798` 也是先判 |v| == 0 就不扫）。
    """
    speed = math.hypot(vx, vy)
    ux, uy = vx / speed, vy / speed
    holds = bool(holds)
    probes = [(0, 0, holds)]
    for cx, cy, r, flags in _shape_table(character, crouched):
        probes.append((int(cx + r * ux), int(cy + r * uy),
                       holds and bool(flags & 1)))
    return tuple(probes)


def _client_sweep(terrain, x, y, vx, vy, probes):
    """角色腾空一格的扫掠：`0x50e759`（整数 DDA，弹体用的也是它，X_Mod §79），探针是 `client_probes()`。

    撞上返回 `(空点x, 空点y, 挡住的格x, 格y)`：空点 = 第 i−1 步的线点（不带探针偏移）；
    **起点**的探针全被挡住才算起点就撞（空点 = 起点、格 = 起点 + 探针 0）。一路通畅返回 `None`。

    挡得住 = 格值 2 / 3；格值 1（单向平台）只在**往下走**时挡「挡得住它」的探针
    （起点那一问看 Δy ≥ 0，逐步那一问看 Δy > 0 —— `0x50ea8f` / `0x50ebec` 各一句）。

    ★ 快速路径：整段（所有探针的起终点外接框）在粗网格上保证**连一格非空都没有**
      （`coarse_empty`，单向平台也算）⇒ 谁都撞不上，直接返回 —— 结果和逐格扫完全一样。
    """
    x0, y0 = int(x), int(y)
    dx = int(x + vx) - x0
    dy = int(y + vy) - y0
    ddy = dy if (dx or dy) else 1
    empty = getattr(terrain, "coarse_empty", None)
    if empty is not None:
        lo_x = hi_x = probes[0][0]
        lo_y = hi_y = probes[0][1]
        for ox, oy, _ow in probes:
            if ox < lo_x:
                lo_x = ox
            elif ox > hi_x:
                hi_x = ox
            if oy < lo_y:
                lo_y = oy
            elif oy > hi_y:
                hi_y = oy
        if empty(x0 + min(0, dx) + lo_x, y0 + min(0, ddy) + lo_y,
                 x0 + max(0, dx) + hi_x, y0 + max(0, ddy) + hi_y):
            return None

    cell = terrain.cell
    down = dy >= 0
    for ox, oy, ow in probes:
        c = cell(x0 + ox, y0 + oy)
        if not (c >= 2 or (c == 1 and ow and down)):
            break
    else:
        return (x0, y0, x0 + probes[0][0], y0 + probes[0][1])
    dy = ddy                            # `0x50eda8`：两轴都没挪满一格 ⇒ 看脚下那一格
    down = dy > 0
    if abs(dx) > abs(dy):
        step = 1 if dx > 0 else -1
        for i in range(step, dx + step, step):
            q = dy if i == dx else _cdiv(dy * i, dx)
            bx, by = x0 + i, y0 + q
            for ox, oy, ow in probes:
                c = cell(bx + ox, by + oy)
                if c >= 2 or (c == 1 and ow and down):
                    j = i - step
                    return (x0 + j, y0 + _cdiv(j * dy, dx), bx + ox, by + oy)
        return None
    step = 1 if dy > 0 else -1
    for i in range(step, dy + step, step):
        q = dx if i == dy else _cdiv(dx * i, dy)
        bx, by = x0 + q, y0 + i
        for ox, oy, ow in probes:
            c = cell(bx + ox, by + oy)
            if c >= 2 or (c == 1 and ow and down):
                j = i - step
                return (x0 + _cdiv(j * dx, dy), y0 + j, bx + ox, by + oy)
    return None


def _client_vote(terrain, x, y):
    """`(x, y)` 周围 7×7 里非空格的偏移之和（`0x473b36`），**指向实心那一侧**。"""
    sx = sy = 0
    n = CLIENT_VOTE_WINDOW
    cell = terrain.cell
    for ddy in range(-n, n + 1):
        for ddx in range(-n, n + 1):
            if cell(x + ddx, y + ddy) != 0:
                sx += ddx
                sy += ddy
    return sx, sy


def _client_reflect(vx, vy, facing):
    """按朝向反射一次（`0x50f240`，同 `bot._reflect_velocity`，只是弹性换成角色的 0.2）。"""
    theta = math.atan2(facing[0], facing[1])
    c, s = math.cos(theta), math.sin(theta)
    u = (c * vx - s * vy) * (1.0 - CLIENT_BOUNCE_FRICTION)
    w = (s * vx + c * vy) * -CLIENT_BOUNCE_RESTITUTION
    return (c * u + s * w, -s * u + c * w)


def _client_ground_below(terrain, x, y, holds=True):
    """落地那一问（`0x50efd2` 的循环体）：脚 `(x, y)` 能不能踩住。

    脚下 `(x, y+1)` 是 2 / 3 ⇒ 能；是 1（单向平台）⇒ 要白线挡人（`vft+0x100`，没在按 ↓ 穿）
    **而且**脚这一格不是 1（人在单向平台里面往下掉的时候不会被它自己接住）。
    """
    ix, iy = int(x), int(y)
    below = _client_cell(terrain, ix, iy + 1)
    if below >= 2:
        return True
    return holds and below == 1 and _client_cell(terrain, ix, iy) != 1


#: `_client_air` 的结局。
FLEW, BUMPED, LANDED, STOPPED, BOUNCED = "flew", "bumped", "landed", "stopped", "bounced"


def _client_air(terrain, x, y, vx, vy, character, crouched=False, holds=True,
                rising=False):
    """腾空一步（`0x50d58a`）：`vx` 是**含操控**的那份。返回 `(x, y, vx, vy, 结局)`。

    1. `vy += 1.2`（空气阻力 vft+0xa0 = 0）；扫掠；一路通畅 ⇒ 位置 += v；
    2. 撞上了（`0x502df4`），**这一步位置不动**、只改速度：
       * 上升计时器在跑（`rising`）⇒ `vx × 0.75`、`vy = 0`（`BUMPED`，调用方停表）；
       * 否则 `0x50efd2`：`ftol(vy) ≥ 0` 且 |v| ≤ 35 ⇒ 从原位往下逐格问 `_client_ground_below`（最多
         `max(5, ftol(vy))` 格），踩得住就放到扫掠的**空点**、踩地（`LANDED`）；问满了就停在往下挪到的
         那一格、仍腾空（`STOPPED`）；两种速度都清零。和哪个探针撞上无关（`[hit+0x20]` 对地形恒 −1）。
         否则按挡住那一格的 7×7 投票反射（`BOUNCED`）。这三种调用方还要 `vx × 0.3`、上锁、操控复位。
    """
    vy = _f32(vy + G32)
    if vx == 0.0 and vy == 0.0:
        return x, y, vx, vy, FLEW
    hit = _client_sweep(terrain, x, y, vx, vy,
                        client_probes(character, vx, vy, crouched, holds))
    if hit is None:
        return _f32(x + vx), _f32(y + vy), vx, vy, FLEW
    if rising:
        return x, y, _f32(vx * RISE_BUMP_KEEP_VX), 0.0, BUMPED
    free_x, free_y, cell_x, cell_y = hit
    if int(vy) >= 0 and math.hypot(vx, vy) <= CLIENT_LAND_SPEED:
        yy = y
        for _ in range(max(5, int(vy))):
            if _client_ground_below(terrain, x, yy, holds):
                return float(free_x), float(free_y), 0.0, 0.0, LANDED
            yy += 1.0
        return x, yy, 0.0, 0.0, STOPPED
    nvx, nvy = _client_reflect(vx, vy, _client_vote(terrain, cell_x, cell_y))
    return x, y, _f32(nvx), _f32(nvy), BOUNCED


def client_air_tick(terrain, body, character, crouched=False, holds=True):
    """只跑**腾空物理**那一步（不走路、不看台子、不读键），给外推 / 单测对照用。

    和 `step()` 里的第 ③ 步同一段：`vx + 操控` 进去、撞后响应（停表 / `× 0.3` + 上锁 + 操控复位）、
    再减回操控。
    """
    running = body.rise > 0
    rise = body.rise - 1 if running else 0
    ctl, cstep, lock = body.ctl, body.ctl_step, body.ctl_lock
    x, y, vxe, vy, outcome = _client_air(terrain, body.x, body.y,
                                         _f32(body.vx + ctl), body.vy,
                                         character, crouched, holds, running)
    if outcome == BUMPED:
        rise = 0
    elif outcome != FLEW:
        vxe = _f32(vxe * CLIENT_BOUNCE_KEEP_VX)
        ctl, cstep, lock = 0.0, AIR_CONTROL_STEP, True
    return body.moved(x, y, _f32(vxe - ctl), vy, on_ground=outcome == LANDED,
                      ctl=ctl, ctl_step=cstep, ctl_lock=lock, rise=rise)


# ---------------------------------------------------------------------------
# 起跳 / 弹跳台 / 空中操控
# ---------------------------------------------------------------------------
def pad_velocity(pad, x, y, character):
    """弹跳台写给角色的速度 `(vx, vy)`；台子往下弹（原版没有这种数据）返回 `None`（X_Mod §104）。

        tx = 台dx + (台x − 人x)                                      ← 0x510e5e
        ty = 台dy + (台y − 人y) − 0.25 × (2·ChrSizeLegs + ChrSizeBody) ← 0x510e68（`[char+0x3c4]`）
        vy = −√(2·g·|ty|) ; t = |vy| / g ; vx = tx / t             ← 0x5111ca
    """
    px, py, dx, dy = pad
    legs = float(getattr(character, "size_legs", 12.0) or 12.0)
    trunk = float(getattr(character, "size_body", 13.0) or 13.0)
    tx = _f32(dx + (px - x))
    ty = _f32(_f32(dy + (py - y)) - _f32(JUMP_PAD_HEIGHT_BIAS * (2.0 * legs + trunk)))
    if ty >= 0.0:
        return None
    vy = -_f32(math.sqrt(_f32(2.0 * _f32(G32 * abs(ty)))))
    ticks = _f32(abs(vy) / G32)
    vx = _f32(tx / ticks) if ticks else 0.0
    return vx, vy


def _pad_hit(terrain, x, y, character, crouched=False):
    """脚在 `(x, y)` 时哪块弹跳台够得着、给多少速度：`(vx, vy)`；没有返回 `None`。

    判据 `0x50f410(mask 0)`：台圆（偏移 0、半径 20）× 角色每个碰撞圆，距离 ≤ r1 + r2 就中。
    """
    pads = getattr(terrain, "jump_pads", ())
    if not pads:
        return None
    circles_of = getattr(character, "circles", None)
    if circles_of is not None:
        circles = [(cx, cy, r) for cx, cy, r, _region
                   in circles_of(x, y, crouched)]
    else:
        # 只给了尺寸的假角色（单测）：腿那一个圆就够 —— 台子贴着地面。
        legs = float(getattr(character, "size_legs", 12.0) or 12.0)
        circles = [(x, y - legs, legs)]
    for pad in pads:
        px, py = pad[0], pad[1]
        if not any(math.hypot(px - cx, py - cy) <= JUMP_PAD_RADIUS + r
                   for cx, cy, r in circles):
            continue
        got = pad_velocity(pad, x, y, character)
        if got is not None:
            return got
    return None


def jump_pad_launch(terrain, body, character, keys=0, crouched=False):
    """踩在弹跳台上、**没按 ←/→/↓** 就被台子写上速度（V0.3 §99 / X_Mod §104）；没弹返回 `None`。

    `JumpingObj::Tick`（`0x510d05`，排在角色那一格**之后**）：本机角色、不是球形态、←/→/↓ 都没按、
    踩地 ⇒ `[char+0x120/+0x124] = pad_velocity(…)`、`0x4face2` 清空中操控、**不写 `[+0x128]`**。
    ⇒ 返回的 `Body` 带 `pad=True`（下一帧「走两步」，见 `step()`）。
    """
    if terrain is None or not (body.on_ground or body.pad):
        return None
    if keys & PAD_BLOCK_KEYS:
        return None
    got = _pad_hit(terrain, body.x, body.y, character, crouched)
    if got is None:
        return None
    return body.moved(body.x, body.y, got[0], got[1], on_ground=False,
                      pad=True, ctl=0.0, ctl_step=AIR_CONTROL_STEP)


def _launch(body, stage, vx):
    """`0x501d57` 的后半段：vy 置成第 `stage` 段初速、当场离地、操控复位解锁、上升计时器起跑。"""
    speed = DOUBLE_JUMP_SPEED if stage == 2 else JUMP_SPEED
    return body.moved(body.x, body.y, float(vx), -speed, on_ground=False,
                      air_jumped=(stage == 2), ctl=0.0,
                      ctl_step=AIR_CONTROL_STEP, ctl_lock=False,
                      rise=rise_ticks(speed))


def jump(body, vx=0.0):
    """一段跳的低层原语（`vx` 由调用方给）。已经在空中就原样返回。带按键口径的是 `takeoff`。"""
    if not (body.on_ground or body.pad):
        return body
    return _launch(body, 1, vx)


def double_jump(body, vx=None):
    """★★ **第二段跳**的低层原语：`v.y` 重新置成 24（§124）。`vx` 不给就沿用。不能跳就原样返回。"""
    if body.on_ground or body.pad or body.air_jumped:
        return body
    return _launch(body, 2, body.reported_vx if vx is None else vx)


def takeoff(terrain, body, character, direction=0, speed_scale=1.0, stage=None):
    """按下跳 = 客户端 `0x501d57`（本人按键和收方执行 `rpJump` 是同一个函数，X_Mod §102）。

    * `stage` 不给就按状态定：踩地（或台子刚弹、踩地位还没清）⇒ 一段；腾空且二段没用过 ⇒ 二段；
      否则原样返回。外推真人时照 `rpJump` 包里的段号给；
    * `vx = ±¼·S`，没按左右键或侧边那一格（x±1, y）是 2/3 ⇒ 0（`0x501e81` / `0x501e9f` / `0x501ecc`）。
    """
    grounded = body.on_ground or body.pad
    if stage is None:
        if grounded:
            stage = 1
        elif not body.air_jumped:
            stage = 2
        else:
            return body
    vx = 0.0
    if direction:
        side = (_client_cell(terrain, int(body.x) + direction, int(body.y))
                if terrain is not None else 0)
        if side < 2:
            vx = _f32(control_speed(character, speed_scale) * JUMP_VX_RATIO)
            if direction < 0:
                vx = -vx
    return _launch(body, stage, vx)


# ---------------------------------------------------------------------------
# 一帧
# ---------------------------------------------------------------------------
class Frame(object):
    """`frame()` 的结果：这一帧之后的身体，外加这一帧**发生了什么**（给上报 / 运动锚用的事实）。

    * `aired`：跑没跑过腾空那一步；
    * `jumped`：这一帧末起跳了第几段（0 = 没跳）—— `rpJump` 的段号；
    * `outcome`：腾空那一步的结局（`FLEW` / `BUMPED` / `LANDED` / `STOPPED` / `BOUNCED`，没腾空是 `None`）；
    * `padded`：弹跳台这一帧写了速度。
    """

    __slots__ = ("body", "aired", "jumped", "outcome", "padded")

    def __init__(self, body, aired=False, jumped=0, outcome=None, padded=False):
        self.body = body
        self.aired = aired
        self.jumped = jumped
        self.outcome = outcome
        self.padded = padded


def frame(terrain, body, character, direction=0, fast_run=False,
          crouched=False, want_jump=False, want_drop=False, speed_scale=1.0,
          keys=None, frozen=False, walk_bonus=0, dashing=False, jump_stage=None,
          supported=None):
    """本人那台客户端的**一个逻辑帧**（32 ms），返回 `Frame`。

    * `direction`：−1 左 / 0 / +1 右（两键都按时**左键优先**，`0x5073c2`，调用方先定好）；
    * `want_jump`：这一帧末执行一次起跳（`rpJump` 回环排在输入之后）；`jump_stage` 是包里的段号；
    * `want_drop`：按着 ↓（`[+0x518] = 8`，下一帧起白线不挡）；
    * `keys`：心跳按键掩码，只给弹跳台那道门用；不给就按 `direction` / `want_drop` 拼；
    * `frozen`：冻住（属性 0xc，`0x515639` 跳过整段读键）—— 不走、不跳、不按 ↓、空中操控原样；
    * `walk_bonus`：装备走速（百分比）；`dashing`：冲刺攻击计时器在跑（走路上下限换成 10 / 0）；
    * `supported(x, y, walked)`：踩地的人**脚下问出来是空的**（又没在按 ↓ 穿白线）时再问一句「他自己
      那台上他还踩着吗」—— 只给外推真人用（服务端的鱼相位 / 地形和他那台差一点，见
      `bot._advance_humans`）；`walked` = 这一帧走路挪没挪 x。
    """
    if terrain is None:
        return Frame(body)
    if frozen:
        direction, want_jump, want_drop, keys = 0, False, False, 0
    x, y, vx, vy = body.x, body.y, body.vx, body.vy
    grounded = body.on_ground or body.pad
    pad = body.pad
    if want_jump and jump_stage is None:
        # ★ 段号是**按下那一刻**定的、写进 `rpJump` 包（`0x501d57` 照包里的段号执行）：按下在上一帧末，
        #   执行在这一帧末 ⇒ 看这一帧**开头**的状态。这一帧先走路走出了崖边也还是一段（h = 180）。
        if grounded:
            jump_stage = 1
        elif not body.air_jumped:
            jump_stage = 2
        else:
            want_jump = False
    ctl, cstep, lock = body.ctl, body.ctl_step, body.ctl_lock
    running = body.rise > 0
    rise = body.rise - 1 if running else 0
    rest, drop = body.rest, body.drop
    air_jumped = body.air_jumped
    # ① `0x4fe329`
    if drop > 0:
        drop -= 1
    holds = drop <= 0
    # ② 走路（`0x506fed`：踩地才走）
    if grounded and (direction or rest <= -1.0 or rest >= 1.0):
        dist = (walk_distance(character, direction, fast_run, crouched,
                              speed_scale, walk_bonus) if direction else 0.0)
        up, down = ((DASH_UP_MAX, DASH_DOWN_MAX) if dashing
                    else (WALK_UP_MAX, WALK_DOWN_MAX))
        x, y, rest, off = _client_walk(terrain, x, y, rest, dist, up, down)
        if off:
            vy = WALK_OFF_VY
    # ③ 物理（`0x507685` ~ `0x50778d`）
    vxe = _f32(vx + ctl)
    aired = False
    outcome = None
    padded = False
    if grounded:
        if vy < 0.0:
            # 台子写的速度：踩地分支直接挪一次（`0x50d44b`，不加重力、不扫掠）。
            x = _f32(x + vxe)
            y = _f32(y + vy)
        else:
            vxe = vy = 0.0              # `0x50d42d`
            pad = False
        below = terrain.cell(int(x), int(y) + 1)
        if ((below == 0 or (below == 1 and not holds))
                and not (supported is not None and holds and not pad
                         and supported(x, y, x != body.x))):
            grounded = False            # `0x50d4d0`，落进腾空分支再走一步
            pad = False
    if not grounded:
        aired = True
        x, y, vxe, vy, outcome = _client_air(terrain, x, y, vxe, vy, character,
                                             crouched, holds, running)
        if outcome == BUMPED:
            rise = 0
        elif outcome != FLEW:
            vxe = _f32(vxe * CLIENT_BOUNCE_KEEP_VX)
            ctl, cstep, lock = 0.0, AIR_CONTROL_STEP, True
            grounded = outcome == LANDED
    vx = _f32(vxe - ctl)
    # ④ 弹跳台（排在角色之后）
    if keys is None:
        keys = ((PAD_BLOCK_LEFT if direction < 0 else 0)
                | (PAD_BLOCK_RIGHT if direction > 0 else 0)
                | (PAD_BLOCK_DOWN if want_drop else 0))
    if grounded and not keys & PAD_BLOCK_KEYS:
        got = _pad_hit(terrain, x, y, character, crouched)
        if got is not None:
            vx, vy = got
            pad = padded = True
            ctl, cstep = 0.0, AIR_CONTROL_STEP
    # ⑤ 本人输入（`0x51558f`）
    if not frozen:
        if grounded:
            ctl = 0.0
        else:
            if direction:
                if not lock and terrain.cell(int(x) + direction,
                                             int(y)) in (0, 1):
                    ctl = _f32(ctl + (cstep if direction > 0 else -cstep))
                    cstep = max(AIR_CONTROL_STEP_MIN,
                                _f32(cstep - AIR_CONTROL_STEP_DECAY))
            else:
                cstep, lock = AIR_CONTROL_STEP, False
            if ctl:
                limit = control_speed(character, speed_scale)   # 钳到 ±S（`0x51605d`）
                if ctl < -limit:
                    ctl = -limit
                elif ctl > limit:
                    ctl = limit
        if want_drop:
            drop = DROP_HOLD_FRAMES
    current = Body(x, y, vx, vy, on_ground=grounded and not pad,
                   air_jumped=air_jumped, ctl=ctl, ctl_step=cstep,
                   ctl_lock=lock, rise=rise, pad=grounded and pad,
                   rest=rest, drop=drop)
    # ⑥ `rpJump`
    jumped = 0
    if want_jump:
        launched = takeoff(terrain, current, character, direction, speed_scale,
                           stage=jump_stage)
        if launched is not current:
            jumped = 2 if launched.air_jumped else 1
            current = launched
    return Frame(current, aired, jumped, outcome, padded)


def step(terrain, body, character, direction=0, fast_run=False,
         crouched=False, want_jump=False, want_drop=False, speed_scale=1.0,
         **extra):
    """`frame()` 的简写：返回 `(新 Body, 这一帧跑没跑过腾空那一步)`。"""
    got = frame(terrain, body, character, direction=direction,
                fast_run=fast_run, crouched=crouched, want_jump=want_jump,
                want_drop=want_drop, speed_scale=speed_scale, **extra)
    return got.body, got.aired


def tick(terrain, body, character, direction=0, fast_run=False,
         crouched=False, want_jump=False, want_drop=False, speed_scale=1.0,
         **extra):
    """走一帧（32 ms），返回**新的** `Body`。"""
    return frame(terrain, body, character, direction=direction,
                 fast_run=fast_run, crouched=crouched, want_jump=want_jump,
                 want_drop=want_drop, speed_scale=speed_scale, **extra).body


def advance(terrain, body, character, ticks, direction=0, fast_run=False,
            crouched=False, want_jump=False, want_drop=False, speed_scale=1.0):
    """连走 `ticks` 帧。起跳 / 按 ↓ 只在第一帧上按，方向键一直按着。"""
    for i in range(max(0, int(ticks))):
        body = tick(terrain, body, character, direction=direction,
                    fast_run=fast_run, crouched=crouched,
                    want_jump=want_jump and i == 0,
                    want_drop=want_drop and i == 0,
                    speed_scale=speed_scale)
    return body


def ticks_for(seconds):
    """一段真实时间对应几个 tick（至少 1）。"""
    return max(1, int(float(seconds) * TICKS_PER_SECOND))


def fall_ticks(terrain):
    """从图顶自由落到图底要几个 tick —— 「掉不到底」的**几何上界**（`h = ½ g t²`，再多给两个）。

    ★ 这是**地图有多高**这个几何事实，不是「等多久算超时」那类阈值。没有地形时退回老值。
    """
    height = getattr(terrain, "height", None)
    if not height:
        return TICKS_PER_BEAT * 8
    return int(math.sqrt(2.0 * float(height) / GRAVITY)) + 2


def out_of_world(terrain, body):
    """人**落在图底那一圈出界实心上**了：`CheckFallDown` 的几何那一半（`0x50d520`：脚 y + 5 ≥ 图高）。

    客户端出界四面都是 2（`0x472fe0`），掉进坑的人会被图底接住、踩在最后一行上 —— 有 `FallDown`
    的图这一下就判死（`bot._fell_out_of_the_world`），没有的图也是个回不来的地方。规划 / 前瞻里
    「落在这儿」一律算**没落住**（旧模型干脆让图底接不住人，口径一样）。
    """
    height = getattr(terrain, "height", None)
    return (height is not None
            and body.y + mapdata.FALL_DOWN_MARGIN >= float(height))


def settle(terrain, body, character, ticks=None):
    """让人落到地上（新出生 / 刚接管位置时用）。落不到就原样返回最后那一格。"""
    if ticks is None:
        ticks = fall_ticks(terrain)
    for _ in range(max(0, int(ticks))):
        if body.on_ground:
            return body
        body = tick(terrain, body, character)
    return body


def blocked(terrain, body, character, direction, fast_run=False,
            crouched=False, speed_scale=1.0):
    """朝 `direction` 按住走会不会**撞在墙上**（一步都挪不动）。

    客户端走路的余量跨帧攒（`_client_walk`）：高坎前头几帧原地不动、攒够了才一步迈上去，所以不能只看
    一帧 —— 一直按着走，直到挪动了（不是墙）或者余量被清零（`0x50db3a`：真撞墙）。每帧都攒进
    正的路程，最高的坎 √(1+20²) 攒得满，这个循环一定会停。
    """
    if not direction or not body.on_ground or terrain is None:
        return False
    if not walk_distance(character, direction, fast_run, crouched, speed_scale):
        return False
    current = body
    while True:
        nxt = tick(terrain, current, character, direction=direction,
                   fast_run=fast_run, crouched=crouched,
                   speed_scale=speed_scale)
        if nxt.x != body.x or not nxt.on_ground:
            return False
        if nxt.rest == 0.0:
            return True
        current = nxt


def leaves_ground(terrain, body, character, direction, fast_run=False,
                  crouched=False, speed_scale=1.0):
    """朝 `direction` 走一步会不会**踩空**（走出崖边）。"""
    if not direction or not body.on_ground:
        return False
    return not tick(terrain, body, character, direction=direction,
                    fast_run=fast_run, crouched=crouched,
                    speed_scale=speed_scale).on_ground


def jump_lands(terrain, body, character, direction, fast_run=False,
               crouched=False, ticks=None, speed_scale=1.0):
    """原地起跳、空中一路按着 `direction`（空中操控），**落在哪**；落不到返回 `None`。

    ★ `speed_scale` 要和真起跳那一刻的一致（V0.3 §151）：起跳的 ¼S 和操控上限都乘它。
    """
    if ticks is None:
        # 起跳先上去、再落到图底：升段 `v/g` 个 tick，落段见 `fall_ticks()`。
        ticks = fall_ticks(terrain) + int(JUMP_SPEED / GRAVITY) + 2
    body = tick(terrain, body, character, direction=direction,
                fast_run=fast_run, crouched=crouched, want_jump=True,
                speed_scale=speed_scale)
    for _ in range(max(0, int(ticks))):
        if body.on_ground:
            return None if out_of_world(terrain, body) else body
        body = tick(terrain, body, character, direction=direction,
                    fast_run=fast_run, crouched=crouched,
                    speed_scale=speed_scale)
    return body if body.on_ground and not out_of_world(terrain, body) else None


def at_apex(body):
    """这一 tick 是不是**这段腾空的顶点** —— 第二段跳该按下去的那一刻。

    ★ 判据是「已经不再上升了」这个**物理事实**（`v.y >= 0`，y 向下为正），不是「起跳后第 N 个
    tick」这种阈值（铁律 10）。规划（`botnav`）、执行（`bot._route_intent`）和兜底（`bot._walk_to`）
    **必须用同一句**，所以它住在这里。
    """
    return (body is not None and not body.on_ground and not body.pad
            and not body.air_jumped and body.vy >= 0.0)


def double_jump_lands(terrain, body, character, direction, fast_run=False,
                      crouched=False, ticks=None, speed_scale=1.0):
    """★★ 起跳 + **在顶点再按一次**、一路按着 `direction`，落在哪；落不到返回 `None`（§124）。

    ★ 第二段也按键重算 vx（¼S），所以方向键要一直按着 —— 松开的话第二段是竖直跳。
    ★ 没跳成第二段（比如起跳那一下就落回地面）一律返回 `None`。
    """
    if ticks is None:
        ticks = (fall_ticks(terrain)
                 + int((JUMP_SPEED + DOUBLE_JUMP_SPEED) / GRAVITY) + 2)
    current = tick(terrain, body, character, direction=direction,
                   fast_run=fast_run, crouched=crouched, want_jump=True,
                   speed_scale=speed_scale)
    if current.on_ground:
        return None                        # 压根没离地
    jumped = False
    for _ in range(max(0, int(ticks))):
        if current.on_ground:
            break
        want = not jumped and at_apex(current)
        if want:
            jumped = True
        current = tick(terrain, current, character, direction=direction,
                       crouched=crouched, want_jump=want,
                       speed_scale=speed_scale)
    if not current.on_ground or not jumped or out_of_world(terrain, current):
        return None
    return current


def drop_below(terrain, body, character, direction, fast_run=False,
               crouched=False, ticks=None, speed_scale=1.0):
    """走出崖边之后会掉多深（掉不到底返回 `None`；没踩空返回 0）。

    ⚠ 它只看**下一步**。「这份意图握着的这几格里会不会踩进无底洞」要问 `bottomless_ahead()`。
    """
    nxt = tick(terrain, body, character, direction=direction,
               fast_run=fast_run, crouched=crouched, speed_scale=speed_scale)
    if nxt.on_ground:
        return 0.0
    landed = settle(terrain, nxt, character, ticks)
    if not landed.on_ground or out_of_world(terrain, landed):
        return None
    return landed.y - body.y


def bottomless_ahead(terrain, body, character, direction, fast_run=False,
                     crouched=False, speed_scale=1.0, ticks=1):
    """接下来这 `ticks` 格照这个方向走，**会不会踩进掉不到底的坑**（V0.3 §151）。

    `ticks` 就是这份意图的寿命（调用方拿自己的决策周期传进来），判据仍是「照这么走会不会掉进
    无底洞」这个物理事实，只是放在整段区间上问。
    """
    if not direction:
        return False
    current = body
    for _ in range(max(1, int(ticks))):
        if not current.on_ground:
            break
        nxt = tick(terrain, current, character, direction=direction,
                   fast_run=fast_run, crouched=crouched,
                   speed_scale=speed_scale)
        if not nxt.on_ground:
            landed = settle(terrain, nxt, character)
            return not landed.on_ground or out_of_world(terrain, landed)
        if nxt.x == current.x and nxt.rest == 0.0:
            return False               # 撞墙了，再往后推也是原地
        current = nxt
    return False


def fits(terrain, x, y, character, crouched=False):
    """脚站在 `(x, y)` 时，**角色的碰撞体塞得进去吗**（V0.3 §152）。

    可达图里一条 1 像素宽的裂缝不能当落脚点：客户端用的是三个碰撞圆（最宽 26 像素），人卡在缝口
    出不来。只查腿圆和身圆的**水平净空**（卡死人的缝全是「窄」不是「矮」）。
    """
    if terrain is None:
        return True
    legs = float((getattr(character, "size_legs_crouch", 7.0) if crouched
                  else getattr(character, "size_legs", 12.0)) or 12.0)
    body_r = float(getattr(character, "size_body", 13.0) or 13.0)
    legs_y = y - legs
    body_y = legs_y - legs - body_r
    return (_clearance_ok(terrain, x, legs_y, legs)
            and _clearance_ok(terrain, x, body_y, body_r))


def _clearance_ok(terrain, x, y, radius):
    """`(x, y)` 这一行上，左右加起来有没有 `2 × radius` 的净空（一侧的富余可以补另一侧）。"""
    iy = int(y)
    if iy < 0:
        return True                    # 伸到图外：图外不是墙，照原版
    ix = int(x)
    if _solid(terrain, ix, iy):
        return False
    need = 2.0 * radius
    limit = int(math.ceil(need))
    left = 0
    while left < limit and not _solid(terrain, ix - left - 1, iy):
        left += 1
    if left + 1 >= need:
        return True
    right = 0
    while (left + right + 1 < need and right < limit
           and not _solid(terrain, ix + right + 1, iy)):
        right += 1
    return left + right + 1 >= need
