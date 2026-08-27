#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""botmove.py —— **角色自己走路**的服务端复现（V0.3 M5）。

`ballistics.py` 是子弹的运动，这个文件是**人**的运动：给一个落脚点、一个
方向键，算出下一 tick 他站在哪、有没有踩空、跳起来能到多高。

## 为什么需要它

在这之前 bot 的位置全靠 `bot.trail_point()` **回放真人的轨迹**（D16）——
真人走过的点一定是合法地面，一行地形代码都不用写。代价是它**没有自己的
走位**：真人站着不动时轨迹推不进，bot 就一路挪到人身上（§52 实测 20 个
单位）。要让 bot 自己决定站哪，就得自己会走路。

## 尺度常量的出处（§71）

| 量 | 值 | 出处 |
|---|---|---|
| 逻辑步长 | **32 ms** | 和弹道同一套（§47 的 `ballistics.TICK_MS`）|
| 一发心跳 | **4 个 tick** | 语料：腾空段的 `dx` 恒等于 `4 × vx` |
| 走路速度 | **`ChrSpeed` 单位 / tick**（6.0~8.0）| `0x50766a`：`速度 × 方向 × 倍率` |
| 冲刺跑 | **× 1.5** | `GameProps.ini` 的 `FastRunRate`（`0x507567` 读它）|
| 蹲着走 | **× 1/3** | `0x507607` 乘 `[0x69387c]` = 0.3333 |
| 空中水平 | 按着方向键时 **× 1.5** | `0x507473` 乘 `[0x69375c]` = 1.5 |
| 重力 | **1.2 单位 / tick²** | `[0x693784]`，`0x40a04f` 返回它 —— **和子弹是同一个数** |
| 起跳初速 | **20 单位 / tick**（向上）| 语料 33971 段：起跳后第一发心跳 `vy` 中位 **−19**（p10 −20 / p90 −17）|
| 爬得动的坡 | `|dy/dx| ≤ **2**` | 语料 88875 发上坡心跳的 **p99** |

⚠ 起跳初速是**语料量的**，不是从代码里读出来的（那一句还没找到）。
两条交叉验证都对得上：顶点高 `v²/2g = 20²/2.4 = 167`，语料量到的中位
是 **170**；而 `1.2` 这个重力是代码里的常量，不是拟合出来的。

## 坐标系

**y 往下为正**（和 `mapdata` / 心跳一致）：跳起来 y 变小，落下 y 变大。
`Body` 记的 `(x, y)` 是**落脚点**，和心跳 body `+7..10` 是同一个点。

## 只用标准库；CPython 3.8 也要能跑（Win7 运行时）
"""
from __future__ import annotations

import ballistics

#: 逻辑步长（毫秒）—— 人和子弹用的是同一套（§47）。
TICK_MS = ballistics.TICK_MS
TICKS_PER_SECOND = ballistics.TICKS_PER_SECOND

#: 重力，单位 / tick²。`[0x693784] = 1.2`，`0x40a04f` 把它乘上
#: `[MyChar+0x344]`（恒 1.0）返回 —— 子弹的 `1.2 × GravityFactor` 用的
#: 也是这一句，所以人和子弹**共用同一个重力**。
GRAVITY = 1.2

#: 起跳初速（单位 / tick，向上）。★ 语料量的，见文件头那张表。
JUMP_SPEED = 20.0

#: 按着右键冲刺跑：`GameProps.ini` 的 `FastRunRate`。
FAST_RUN_RATE = 1.5

#: 蹲着走：`0x507607` 乘的那个常量。
CROUCH_FACTOR = 1.0 / 3.0

#: 腾空时按着方向键，水平速度的倍率（`0x69375c`）。
AIR_KEY_FACTOR = 1.5

#: 走路能爬的最陡坡（`|dy / dx|`）。语料 88875 发上坡心跳的 p99 = 2.0
#: （中位 0.23、p90 0.85）——**这是真人走得动的坡**，不是我挑的数。
CLIMB_SLOPE = 2.0

#: 一发心跳等于几个 tick。语料：腾空段相邻两发的 `dx` 恒等于 `4 × vx`。
#: ★ 只在「没有真实时间可依据」的地方当兜底用（`bot.py` 按流逝时间算）。
TICKS_PER_BEAT = 4


class Body(object):
    """一个角色此刻的运动状态。**不可变**：每个 tick 返回一个新的。

    `on_ground` 为真时 `vx / vy` 恒为 0 —— 和心跳的口径一致（§35：
    踩在地上时真人报的速度就是 0，是收方自己按按键把他走过去的）。
    """

    __slots__ = ("x", "y", "vx", "vy", "on_ground")

    def __init__(self, x, y, vx=0.0, vy=0.0, on_ground=True):
        self.x = float(x)
        self.y = float(y)
        self.vx = 0.0 if on_ground else float(vx)
        self.vy = 0.0 if on_ground else float(vy)
        self.on_ground = bool(on_ground)

    def moved(self, x, y, vx=0.0, vy=0.0, on_ground=True):
        return Body(x, y, vx, vy, on_ground)

    def __eq__(self, other):
        return (isinstance(other, Body)
                and (self.x, self.y, self.vx, self.vy, self.on_ground)
                == (other.x, other.y, other.vx, other.vy, other.on_ground))

    def __repr__(self):
        return ("<Body (%.1f, %.1f) v=(%.1f, %.1f) %s>"
                % (self.x, self.y, self.vx, self.vy,
                   "地上" if self.on_ground else "空中"))


def walk_speed(character, fast_run=False, crouched=False):
    """一个 tick 走多远（世界单位）。

    `ChrSpeed × 倍率`，倍率来自那两个开关（都是原版常量，见文件头）。
    """
    speed = float(getattr(character, "speed", 7.0) or 7.0)
    if fast_run:
        speed *= FAST_RUN_RATE
    if crouched:
        speed *= CROUCH_FACTOR
    return speed


def jump_apex():
    """一次跳最高能上升多少（`v² / 2g`）。语料量到的中位是 170。"""
    return JUMP_SPEED * JUMP_SPEED / (2.0 * GRAVITY)


def jump(body):
    """起跳：把垂直速度置成初速，人离地。已经在空中就原样返回。"""
    if not body.on_ground:
        return body
    return body.moved(body.x, body.y, 0.0, -JUMP_SPEED, on_ground=False)


# ---------------------------------------------------------------------------
# 地形查询
# ---------------------------------------------------------------------------
def _solid(terrain, x, y):
    """挡得住**人**吗（单向平台算挡，图外算挡）。"""
    return terrain.is_solid(int(x), int(y))


def _blocks_up(terrain, x, y):
    """往上撞得住吗 —— **单向平台不算**（§29：往上跳能穿过去）。

    `blocks_bullet` 恰好就是「格值 ≥ 2」这个谓词，两处口径一致。
    """
    return terrain.blocks_bullet(int(x), int(y))


def surface_near(terrain, x, y, reach):
    """第 `x` 列上，脚从 `y` 出发**够得着**的站立面；没有返回 `None`。

    上下各看 `reach`，取最接近 `y` 的那个 —— 上坡下坡走的都是这一条路。
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


def _walk_tick(terrain, body, character, direction, fast_run, crouched):
    """踩在地上走一个 tick。"""
    if not direction:
        return body
    speed = walk_speed(character, fast_run, crouched)
    nx = body.x + (speed if direction > 0 else -speed)
    reach = speed * CLIMB_SLOPE
    sy = surface_near(terrain, nx, body.y, reach)
    if sy is not None:
        return body.moved(nx, sy)
    if _solid(terrain, nx, body.y - 1):
        # 前面是墙（够不着的高台、图外）—— 真人也是走不过去的，原地不动。
        return body
    # ★ 走出崖边：人离地、水平速度保持这一步的走速，垂直速度从 0 开始
    #   （原版就是这样掉下去的，不是「不许走过去」）。
    return body.moved(nx, body.y, nx - body.x, 0.0, on_ground=False)


def _air_tick(terrain, body, character, direction, fast_run, crouched):
    """腾空走一个 tick：先加重力，再走，撞上什么就停什么。"""
    vx = body.vx
    if direction:
        # ★ 空中按方向键：收方 `0x507473` 拿按键覆写水平速度并 × 1.5。
        speed = walk_speed(character, fast_run, crouched) * AIR_KEY_FACTOR
        vx = speed if direction > 0 else -speed
    vy = body.vy + GRAVITY
    nx = body.x + vx
    ny = body.y + vy
    if vx and _solid(terrain, nx, body.y - 1):
        nx, vx = body.x, 0.0           # 撞墙：横向停住，继续该升该落
    if vy > 0:
        landing = terrain.ground_below(int(nx), int(body.y))
        if landing is not None and landing <= ny:
            return body.moved(nx, landing)          # 落地
        if landing is None and ny >= terrain.height:
            # 掉出图外（陷阱）—— 停在图底，别让坐标一路跑到无穷。
            # ★ 死不死由客户端上报（`0x0409`），服务端不替它判。
            return body.moved(nx, terrain.height - 1, vx, vy, on_ground=False)
    elif vy < 0 and _blocks_up(terrain, nx, ny):
        return body.moved(nx, body.y, vx, 0.0, on_ground=False)   # 撞天花板
    return body.moved(nx, ny, vx, vy, on_ground=False)


def tick(terrain, body, character, direction=0, fast_run=False,
         crouched=False, want_jump=False):
    """走一个 tick（32 ms），返回**新的** `Body`。

    `direction`：−1 左 / 0 不按 / +1 右，就是心跳里那个方向键掩码（§39）。
    `want_jump`：这一 tick 要不要起跳（只在踩着地时有效）。
    """
    if terrain is None:
        return body
    if body.on_ground and want_jump:
        body = jump(body)
    if body.on_ground:
        return _walk_tick(terrain, body, character, direction,
                          fast_run, crouched)
    return _air_tick(terrain, body, character, direction, fast_run, crouched)


def advance(terrain, body, character, ticks, direction=0, fast_run=False,
            crouched=False, want_jump=False):
    """连走 `ticks` 个 tick。起跳只在第一个 tick 上生效。"""
    for i in range(max(0, int(ticks))):
        body = tick(terrain, body, character, direction=direction,
                    fast_run=fast_run, crouched=crouched,
                    want_jump=want_jump and i == 0)
    return body


def ticks_for(seconds):
    """一段真实时间对应几个 tick（至少 1）。"""
    return max(1, int(float(seconds) * TICKS_PER_SECOND))


def settle(terrain, body, character, ticks=TICKS_PER_BEAT * 8):
    """让人落到地上（新出生 / 刚接管位置时用）。落不到就原样返回。"""
    for _ in range(max(0, int(ticks))):
        if body.on_ground:
            return body
        body = tick(terrain, body, character)
    return body


def blocked(terrain, body, character, direction, fast_run=False,
            crouched=False):
    """朝 `direction` 走一步会不会**撞在墙上**（原地不动）。"""
    if not direction or not body.on_ground:
        return False
    return tick(terrain, body, character, direction=direction,
                fast_run=fast_run, crouched=crouched).x == body.x


def leaves_ground(terrain, body, character, direction, fast_run=False,
                  crouched=False):
    """朝 `direction` 走一步会不会**踩空**（走出崖边）。"""
    if not direction or not body.on_ground:
        return False
    return not tick(terrain, body, character, direction=direction,
                    fast_run=fast_run, crouched=crouched).on_ground


def jump_lands(terrain, body, character, direction, fast_run=False,
               crouched=False, ticks=TICKS_PER_BEAT * 16):
    """原地起跳、空中一路按着 `direction`，**落在哪**；落不到返回 `None`。

    这就是「跳跃弧线」：要不要跳过这个坑、够不够得着那个台子，
    问它就行 —— 弧线是真跑出来的，不是拿公式估的。
    """
    body = tick(terrain, body, character, direction=direction,
                fast_run=fast_run, crouched=crouched, want_jump=True)
    for _ in range(max(0, int(ticks))):
        if body.on_ground:
            return body
        body = tick(terrain, body, character, direction=direction,
                    fast_run=fast_run, crouched=crouched)
    return body if body.on_ground else None


def drop_below(terrain, body, character, direction, fast_run=False,
               crouched=False, ticks=TICKS_PER_BEAT * 8):
    """走出崖边之后会掉多深（掉不到底返回 `None`）。

    给决策层用：真人不会主动跳进无底洞，但**从一米高的台阶走下去**
    再正常不过 —— 判据是「掉多深」，不是「许不许离地」。
    """
    step = tick(terrain, body, character, direction=direction,
                fast_run=fast_run, crouched=crouched)
    if step.on_ground:
        return 0.0
    landed = settle(terrain, step, character, ticks)
    if not landed.on_ground:
        return None
    return landed.y - body.y
