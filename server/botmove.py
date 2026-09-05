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
| 空中水平 | ★ **方向键管不着**：一直是起跳那一刻的走速 | `0x5073a6`（腾空整段跳过读键）+ 语料，§93 |
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

import math

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

#: ★★ **第二段跳**的初速（单位 / tick，向上）。同样是语料量的（V0.3 §124）：
#: 380 份上行流里 `rpJump` 的 `+1` 段号分成两拨，各取「起跳后第一发心跳的
#: `vy`」——
#:
#:     第 1 段  n=37147   峰值 **−20**（9712 发），后面 19 / 18 / 17 是采样滞后
#:     第 2 段  n=20988   峰值 **−24**（5445 发），后面 22 / 21 / 20 同理
#:
#: 分布只有四个桶、而且每桶都上千发 ⇒ 它是**常量**，而且第二段跳是**把
#: `v.y` 重新置成这个数**（不是在当前速度上叠加 —— 叠加的话分布会散开）。
DOUBLE_JUMP_SPEED = 24.0

#: 按着右键冲刺跑：`GameProps.ini` 的 `FastRunRate`。
FAST_RUN_RATE = 1.5

#: 蹲着走：`0x507607` 乘的那个常量。
CROUCH_FACTOR = 1.0 / 3.0

#: 走路能爬的最陡坡（`|dy / dx|`）。语料 88875 发上坡心跳的 p99 = 2.0
#: （中位 0.23、p90 0.85）——**这是真人走得动的坡**，不是我挑的数。
CLIMB_SLOPE = 2.0

#: 一发心跳等于几个 tick。语料：腾空段相邻两发的 `dx` 恒等于 `4 × vx`。
#: ★ 只在「没有真实时间可依据」的地方当兜底用（`bot.py` 按流逝时间算）。
TICKS_PER_BEAT = 4

#: ★★★ **弹跳台**的作用半径（V0.3 §99）。
#:
#: `JumpingObj` 构造函数 `0x510ade` 把碰撞形状的半径写成 **20.0**
#: （`[obj+0x13c]` 的 `+0x18`），判定是它和**角色的碰撞圆**相交
#: （`0x50f410`，和子弹撞人是同一个函数）。角色最下面那个圆（腿）半径 12，
#: 所以水平方向大约 32 个单位以内会被弹 —— 实机 12 次弹飞的水平距离
#: 全部 ≤ 29.7，最近的一次「没被弹」是 51.5，和这个口径对得上。
JUMP_PAD_RADIUS = 20.0

#: ★ 台子给的目标点还要再减这一项：`0x510e75` 把**角色重力**乘上它。
JUMP_PAD_GRAVITY_BIAS = 0.25


class Body(object):
    """一个角色此刻的运动状态。**不可变**：每个 tick 返回一个新的。

    `on_ground` 为真时 `vx / vy` 恒为 0 —— 和心跳的口径一致（§35：
    踩在地上时真人报的速度就是 0，是收方自己按按键把他走过去的）。
    """

    __slots__ = ("x", "y", "vx", "vy", "on_ground", "air_jumped")

    def __init__(self, x, y, vx=0.0, vy=0.0, on_ground=True,
                 air_jumped=False):
        self.x = float(x)
        self.y = float(y)
        self.vx = 0.0 if on_ground else float(vx)
        self.vy = 0.0 if on_ground else float(vy)
        self.on_ground = bool(on_ground)
        #: ★ 这一段腾空里**第二段跳用掉了没有**。落地自动清 —— `rpJump` 的
        #:   段号只有 1 和 2（§23），所以一次腾空只能再跳一下。
        self.air_jumped = False if on_ground else bool(air_jumped)

    def moved(self, x, y, vx=0.0, vy=0.0, on_ground=True, air_jumped=None):
        """派生一个新状态。`air_jumped` 不给就**沿用自己的**。"""
        return Body(x, y, vx, vy, on_ground,
                    self.air_jumped if air_jumped is None else air_jumped)

    def __eq__(self, other):
        return (isinstance(other, Body)
                and (self.x, self.y, self.vx, self.vy, self.on_ground,
                     self.air_jumped)
                == (other.x, other.y, other.vx, other.vy, other.on_ground,
                    other.air_jumped))

    def __repr__(self):
        return ("<Body (%.1f, %.1f) v=(%.1f, %.1f) %s>"
                % (self.x, self.y, self.vx, self.vy,
                   "地上" if self.on_ground else "空中"))


def walk_speed(character, fast_run=False, crouched=False, scale=1.0):
    """一个 tick 走多远（世界单位）。

    `ChrSpeed × 倍率`，倍率来自那两个开关（都是原版常量，见文件头）。

    ★ `scale` 是**状态效果**那一档（`Status.ini` 的 `SpeedRatio`）：
    减速胶水踩上去是 0.3、加速道具是 2.0。原版是 `UseItemEffect` 把它加进
    角色的属性表，走路那一句再乘上去；服务端这边只能自己乘（V0.3 §101）。
    """
    speed = float(getattr(character, "speed", 7.0) or 7.0)
    if fast_run:
        speed *= FAST_RUN_RATE
    if crouched:
        speed *= CROUCH_FACTOR
    return speed * float(scale)


def jump_apex():
    """一次跳最高能上升多少（`v² / 2g`）。语料量到的中位是 170。"""
    return JUMP_SPEED * JUMP_SPEED / (2.0 * GRAVITY)


def jump_pad_launch(terrain, body, character):
    """踩在弹跳台上就把人弹出去（V0.3 §99）；没踩着返回 `None`。

    ## 原版怎么做的（`JumpingObj::Tick`，`0x510d05`）

    台子**自己**每帧扫一遍场上的角色，对每一个：

        cmp byte [char+0x128], 0 ; je 跳过      ← ★ 必须**踩在地上**
        call 0x50f410（台子 vs 角色的碰撞圆）    ← 半径 20 vs 角色那三个圆
        tx = 台dx + (台x − 人x)                 ← 0x510e5e
        ty = 台dy + (台y − 人y) − 0.25×角色重力  ← 0x510e68
        (vx, vy) = 0x5111ca(tx, ty)             ← 解抛物线
        [char+0x120] = vx ; [char+0x124] = vy
        [台+0x2a4] = 20                          ← 只是压缩动画的计时

    `0x5111ca` 就三行：

        vy = −sqrt(2 × g × |ty|)     ← 升到 |ty| 那么高要多大初速
        t  = |vy| / g                ← 到顶点要几个 tick
        vx = tx / t                  ← 这段时间正好横移 tx

    **没有冷却**：弹完角色就离地，下一帧 `[char+0x128]` 已经是 0，
    自然不会连着弹第二下。

    ## 台子那两个数是**落点偏移**，不是速度

    `Iceria_b` 的两个台子分别是 `(41, −416)` 和 `(−24, −395)`。
    实机验：真人站在 `(1742, 904)`、台子 `(1743, 895)` ⇒
    `ty = −395 − 9 − 0.3 = −404.3` ⇒ `vy = −31.15`，
    心跳里报出来的正是 **−31**；`vx = −23 / 25.96 = −0.89`，报的是 **0**。
    另一个台子同样对得上（预测 −31.9 / 实测 −29，差的是采样滞后的 2 个 tick）。
    """
    if terrain is None or not body.on_ground:
        return None
    pads = getattr(terrain, "jump_pads", ())
    if not pads:
        return None
    # ★ 角色那三个圆里最下面那个（腿）是唯一够得着台子的 —— 台子贴着地面。
    #   （和 `walk_speed()` 一样对「只给了走速的假角色」留一条兜底。）
    legs = float(getattr(character, "size_legs", 12.0) or 12.0)
    for px, py, dx, dy in pads:
        span = math.hypot(px - body.x, py - (body.y - legs))
        if span > JUMP_PAD_RADIUS + legs:
            continue
        tx = dx + (px - body.x)
        ty = dy + (py - body.y) - JUMP_PAD_GRAVITY_BIAS * GRAVITY
        if ty >= 0.0:
            continue                  # 台子往下弹？原版没有这种数据，跳过
        vy = -math.sqrt(2.0 * GRAVITY * abs(ty))
        ticks = abs(vy) / GRAVITY
        vx = tx / ticks if ticks else 0.0
        return body.moved(body.x, body.y, vx, vy, on_ground=False)
    return None


def double_jump(body):
    """★★ **第二段跳**：腾空中再按一次跳（§124）。不能跳就原样返回。

    * 只在**腾空**时有效（踩着地的那一下是第一段，走 `jump()`）；
    * 一段腾空只能用一次（`rpJump` 的段号只有 1 / 2）；
    * 把 `v.y` **重新置成** `DOUBLE_JUMP_SPEED`（语料实证是「置」不是「叠」），
      水平速度一点不动 —— 腾空里方向键管不着水平速度（§93）。
    """
    if body.on_ground or body.air_jumped:
        return body
    return body.moved(body.x, body.y, body.vx, -DOUBLE_JUMP_SPEED,
                      on_ground=False, air_jumped=True)


def jump(body, vx=0.0):
    """起跳：把垂直速度置成初速，人离地。已经在空中就原样返回。

    ★★ `vx` = **起跳那一刻的水平走速**（`走速 × 方向 × 倍率`）。腾空之后
    方向键就管不着水平速度了（§93），所以这一刻带上去多少，整段弧线就是
    多少 —— 站着起跳的人是**竖直**上下的，语料里那 11256 发「腾空 + 按着
    方向键但 `vx` 恒 0」就是他们。
    """
    if not body.on_ground:
        return body
    return body.moved(body.x, body.y, float(vx), -JUMP_SPEED, on_ground=False)


def drop_through(terrain, body):
    """按 ↓ 穿过脚下的**单向平台**；不能下落就原样返回。

    原版行为已经由实机确认（§29），但“关掉单向碰撞一帧”的内部标志尚未逆到。
    服务端没有那个角色对象可写，所以这里做它在物理上的等价操作：把脚移到
    当前连续值-1 带的下沿之后，再让普通重力/落地链继续算。没有向下初速；
    下一次 `_air_tick()` 会照常加 `GRAVITY`。

    ★ 只认**脚下这一格就是 1**。实心地面按 ↓ 不能穿，站在空气里也不能。
    """
    if terrain is None or not body.on_ground:
        return body
    x = int(body.x)
    y = int(body.y)
    if not terrain.is_one_way(x, y):
        return body
    # 白线通常一像素厚，但按连续带扫描，不把产物形状假定成固定厚度。
    below = y
    while below < terrain.height and terrain.is_one_way(x, below):
        below += 1
    # ★★ 穿出去的那一格必须是**空的**（V0.3 §136）。白线底下紧贴着实心
    #    （最常见的是**冰块**罩着一根白线）的时候，原版按 ↓ 是纹丝不动的
    #    —— 不查这一句的话人会一头钻进地形里面。
    if terrain.is_solid(x, below):
        return body
    return body.moved(body.x, float(below), 0.0, 0.0, on_ground=False)


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


def _walk_tick(terrain, body, character, direction, fast_run, crouched,
               scale=1.0):
    """踩在地上走一个 tick。"""
    if not direction:
        return body
    speed = walk_speed(character, fast_run, crouched, scale)
    nx = body.x + (speed if direction > 0 else -speed)
    reach = speed * CLIMB_SLOPE
    sy = surface_near(terrain, nx, body.y, reach)
    if sy is not None:
        return body.moved(nx, sy)
    if _solid(terrain, nx, body.y - 1):
        # 前面是墙（够不着的高台、图外）—— 真人也是走不过去的，原地不动。
        #
        # ★★★ 但要先问一句：这一步**跨过去的那几列**里，有没有先来一道
        #   崖边（V0.3 §177）。收方是一格一格推进的（`0x50d9a7` /
        #   `0x50e4e9`，§169 里引的就是这几处），走到崖边人就掉下去了，
        #   根本走不到后面那面墙上。只问落点的话会得出「原地不动」——
        #   `Iceria03` 那个 1 像素夹层就是靠这一条把 bot 锁死 45.8 秒的：
        #   脚下那块 1 像素的冰檐左边紧接着就是空的，可一整步（8 像素）
        #   跨过去正好落在冰体里面，判据说「墙」，于是永远挪不动。
        ledge = _ledge_within_step(terrain, body, nx, reach)
        if ledge is None:
            return body
        return body.moved(ledge, body.y,
                          (speed if direction > 0 else -speed), 0.0,
                          on_ground=False)
    # ★ 走出崖边：人离地、水平速度保持这一步的走速，垂直速度从 0 开始
    #   （原版就是这样掉下去的，不是「不许走过去」）。
    return body.moved(nx, body.y, nx - body.x, 0.0, on_ground=False)


def _ledge_within_step(terrain, body, nx, reach):
    """这一步跨过的那几列里，第一处**脚下没路**的列；一路有路返回 `None`。

    ★ 只在「落点撞墙」那一支上问 —— 那一支今天的结果是**原地不动**，
      所以这里只可能把「不动」变成「掉下去」，一步走得动的都不碰。
      实测 8 张真图：受影响的走位占 0.4%（`Iceria03`）~0.0%，
      而且**全部**来自今天那 0.5%~10.2% 的「撞墙 = 不动」。

    先撞上墙（这一列脚下是实心、又够不着站立面）就返回 `None`：墙在崖边
    前面时人是真的走不过去。
    """
    step = 1 if nx > body.x else -1
    span = abs(nx - body.x)
    col = int(body.x)
    while abs(col + step - body.x) <= span:
        col += step
        if surface_near(terrain, col, body.y, reach) is not None:
            continue                   # 这一列还站得住，接着往前
        if _solid(terrain, col, body.y - 1):
            return None                # 先撞上墙 —— 走不到崖边
        return float(col)
    return None


def _is_ledge(terrain, x, y):
    """`(x, y)` 这个实心点是不是某个**站立面本身**（台阶的上沿）。

    ★★ 拿它把「头顶的板」和「台阶的边」分开（§95）。

    腾空往上走时，脚**掠过一个站立面**说明人正翻过一个坎的边缘 ——
    站立面按定义上面就是空气，那不是天花板。真正的天花板（板的**下沿**）
    不是站立面：板顶那个站立面在更上面，脚够不着。

    实机代价：用户 2026-08-28 那张图里 bot 站在 `(581, 651)`，左边一列的
    地面是 **646**（高 5 个像素）。旧代码把「脚升到 646.5」当成撞天花板，
    于是 `v.y` 当场清零、人卡在原地不动 —— 一次强度 15 的击退**位移 0**。
    """
    return int(y) in terrain.surfaces(int(x))


def _ceiling_between(terrain, x0, y0, x1, y1):
    """(x0, y0) -> (x1, y1) 这一段**往上**的路上撞没撞到天花板。

    撞上了返回「撞之前最后一个安全点」`(x, y)`；一路畅通返回 `None`。

    ## ★★★ 为什么不能只判落点（V0.3 §169）

    一个 tick 最多往上走 **24 个单位**（二段跳初速），而天花板可以只有
    几个像素厚。只问落点那一格的话，脚从板底下**穿过去**、落点又正好在板
    上面的空气里 —— 判据说「没撞」，人就这么钻过去了。

    收方是**逐像素**推进的：`0x50e40a` 把 `(dx, dy)` 归一化成单位向量，
    一格一格加上去，每加一格问一次 `0x473969`（就是 `mapdata.cell()`，
    返回 0 空 / 1 白线 / 2+ 实心），**头一格挡住就整个停下**；
    `0x50d9a7`（走路的爬坎/下坎）和 `0x50e4e9` 也都是一格一格扫的。
    ⇒ 这里照着扫：沿线段逐**整数行**采样，x 按线段线性插值
    （单位向量步进的就是这条线），第一处挡得住的格子之前那一格就是终点。

    ⚠ 不是把整条 `_air_tick` 换成客户端那套：横向那一段（撞墙 / 蹭上坎）
      是 §95 用实机日志两轮收口的，这一发只补**往上**这一条。

    ★ 采样点从 `int(y0) - 1` 起 —— `y0` 那一格是人**已经在**的地方
      （起跳那一刻脚下就是实心的站立面），再问一遍必然自己挡自己。

    ## ★★★ 「人已经嵌在地形里」不算撞天花板

    脚下那一格实心、**而且不是站立面** = 这个人陷在地形里了（斜坡上按
    整数坐标摆位置、复活点埋在坡里都会这样）。这时候头顶那一片实心是
    **他自己陷进去的那一块**，不是板 —— 把它当天花板的话人**永远**跳不
    出来（`Quest02_1` 的岩浆坑左沿就是这样：地面在 444、身体在 453，
    一跳被 452 挡住，原地不动，下一帧接着跳，一辈子过不了那个坑）。
    ⇒ 先跳过「一路连着的实心」，从**第一格空气**起才开始认天花板。
    只判落点的旧代码天然就是这个行为（落点在空气里 ⇒ 放行），这里是把它
    保住，不是新加的宽容。

    ★ 站在正经站立面上的人不受这一条影响：站立面按定义**上面就是空气**，
      第一格采样必然是空的。
    """
    span = y0 - y1
    if span <= 0:
        return None
    top = int(y1)
    first = int(y0) - 1
    if first < top:
        return None                 # 这一 tick 连一整格都没升出去
    # ★ 绝大多数上升 tick 头顶是开阔的。粗网格（`bullet_coarse`，谓词就是
    #   `blocks_bullet`）一次几个字节就能证明「这一小段整个是空的」——
    #   证不了才逐格扫。不做这一步的话整张图泛洪要慢一倍（实测 1034 -> 2334 ms）。
    clear = getattr(terrain, "coarse_clear", None)
    if clear is not None and clear(x0, top, x1, first):
        return None
    # 「人已经嵌在地形里」—— 见上面那一段。
    digging = (_blocks_up(terrain, x0, y0)
               and not _is_ledge(terrain, x0, y0))
    prev_x, prev_y = x0, y0
    dx = x1 - x0
    for row in range(first, top - 1, -1):
        ratio = (y0 - row) / span
        if ratio > 1.0:
            ratio = 1.0
        col = x0 + dx * ratio
        if not _blocks_up(terrain, col, row):
            digging = False             # 出土了，从这里起才认天花板
        elif digging:
            continue                    # 还在自己陷进去的那一块里
        elif not _is_ledge(terrain, col, row):
            return prev_x, prev_y
        prev_x, prev_y = col, float(row)
    return None


def _air_tick(terrain, body):
    """腾空走一个 tick：先加重力，再走，撞上什么就停什么。

    ★★★ **方向键在这里一点用都没有**（§93）。原来这儿按 §71 抄了一句
    「按方向键 -> 水平速度 = 走速 × 1.5」，出处是 `0x507473` —— 可那一段
    整个挂在 `0x493d00()` 为真的分支下面（`0x5073f5` / `0x507615` 各一道
    门），正常对局里它是假的。真正的分支在 `0x5073a6`：**腾空 ⇒ 直接跳到
    `0x50767e`**，读键、算走路方向、按走速挪那三件事整段跳过。

    代价是实打实的：击退把 `vx` 设成 `+12` 之后，下一帧这句就按「朝着敌人」
    把它改写成 `−11`，bot 于是**朝开枪的人飘过去**（模拟：位移 −157 而不是
    +172）。用户 2026-08-28 报的「打 bot 它不会被击退，只是原地跳一下」
    就是这个。
    """
    vx = body.vx
    vy = body.vy + GRAVITY
    nx = body.x + vx
    ny = body.y + vy
    #: 「这一 tick 是**蹭上了一个坎**」——蹭上坎会把脚抬到坎顶（比自然落点
    #:  还高），那一段是**贴着地形爬**的，不能再拿它当往上飞的路去扫天花板。
    climbed = False
    if vx and _solid(terrain, nx, ny - 1):
        # ★★★★ 目标点在地形里。先问一句：这一列上**够不够得着一个站立面**？
        #
        # 够得着 = 那只是个**坎**，不是墙 —— 蹭上去、接着飞。判据和走路
        # 完全同一条（`CLIMB_SLOPE`，`_walk_tick` 用的就是它）：走路一步迈得
        # 上去的坎，被顶飞时更不该被它挡住。
        #
        # ⚠ 这一条是**分两轮**才补齐的（§95），两轮的实机现象不一样：
        #
        # 1. 第一轮（用户 2026-08-28）：采样点原来用的是**出发时**的脚下
        #    高度 `body.y - 1`，而人正在往上升 ⇒ 4~5 像素的坎就算「墙」；
        #    而且撞上就把 `vx` **永久清零**，整段飞行再也没有水平速度。
        #    改成采样**落点**高度 `ny - 1`、并且**不清零速度**（撞上只是
        #    这一 tick 不挪，升过去下一 tick 接着走）。
        # 2. 第二轮（用户 2026-08-29，同一张图同一个位置）：**弱击退还是
        #    卡住**。`Forest_b` 那一带是缓上坡 `654→653→651→650→647→645`，
        #    而强度 8 的击退抬升顶点只有 **1.8~3.3 个像素**，够不着前面那个
        #    4 像素的坎 —— 它自己**永远**升不过去。强度 15 那一发抬升 28，
        #    所以只有弱击退才卡。⇒ 必须像走路一样**蹭上去**。
        # ★ 锚在**出发时**的脚下高度（和 `_walk_tick` 完全一样）：
        #   这样「贴着崖壁往下掉」不会被上面很远的崖顶勾上去。
        step = surface_near(terrain, nx, body.y, abs(vx) * CLIMB_SLOPE)
        if step is not None and step < ny:
            # ★★★★★ **掉着掉着蹭上坡 = 落地**，不是接着飞（V0.3 §181）。
            #
            #   「蹭上坎」这一支是 §95 给**往上飞**的人补的（弱击退顶着缓坡
            #   往上走）。可它没分上下：一个正在**下落**的人从斜坡上方掠过时
            #   同样命中这里，于是脚被抬到坡面上、`on_ground` 却还是 0、
            #   `v.y` 接着按重力空转 —— 人**贴着地面滑行**，报出去的下落速度
            #   一路涨到 40 开外。
            #
            #   收方对腾空角色是拿包里的速度**逐帧积分推位置**的
            #   （`packet_api §5.6`），于是它把角色按 40/tick 往地底下拽，
            #   一发心跳（4 帧）拽出 170 像素，下一发再拽回来 ——
            #   **每 128 ms 一次的大幅上下抽动**，就是用户 2026-09-04 报的
            #   「在空中还是会有卡顿和瞬移感，尤其在空中很明显」。
            #
            #   `Forest02` (569,597) 那条弧线实测：从 tick 28 起滑了 20 多个
            #   tick，`v.y` 从 16 一路涨到 41，收方偏差峰值 **178 像素**。
            #
            #   ★ 落地判据本来只问 `ground_below(nx, 出发时的 y)` —— 它是
            #     **往下**找的，而这里地面是**升上来迎着人**，所以永远问不到。
            #     `surface_near()` 已经把那个面找出来了，falling 时它就是落点。
            #   ★ 往上飞（`v.y <= 0`）那一支一个字没动，§95 照旧。
            if vy > 0:
                return body.moved(nx, float(step))      # 落地
            ny = float(step)            # 蹭上坎：脚抬到坎顶，**仍然腾空**
            climbed = True
        else:
            # 真的够不着 = 墙。这一 tick 横向过不去，**速度留着**：
            # 踩地时 `Body` 会把速度归零（§35），落地那一下自然收尾。
            nx = body.x
    if vy > 0:
        landing = terrain.ground_below(int(nx), int(body.y))
        if landing is not None and landing <= ny:
            return body.moved(nx, landing)          # 落地
        if landing is None and ny >= terrain.height:
            # 掉出图外（陷阱）—— 停在图底，别让坐标一路跑到无穷。
            # ★ 死不死由客户端上报（`0x0409`），服务端不替它判。
            return body.moved(nx, terrain.height - 1, vx, vy, on_ground=False)
    elif vy < 0 and not climbed:
        # ★ 撞天花板：**整条上升路线**都要扫，不能只问落点（§169）。
        #   收方一格一格推进，头一格挡住就停 —— 停在挡住之前那一点，
        #   横向也跟着停（两个轴是一起推进的，不是各走各的）。
        hit = _ceiling_between(terrain, body.x, body.y, nx, ny)
        if hit is not None:
            return body.moved(hit[0], hit[1], vx, 0.0, on_ground=False)
    return body.moved(nx, ny, vx, vy, on_ground=False)


def step(terrain, body, character, direction=0, fast_run=False,
         crouched=False, want_jump=False, want_drop=False, speed_scale=1.0):
    """走一个 tick，返回 `(新 Body, 这一格跑没跑过空中积分)`。

    参数和 :func:`tick` 完全一样 —— `tick()` 就是它丢掉第二个返回值的简写。

    ## ★★★★★ 第二个返回值是什么、给谁用的（V0.3 §185）

    它回答的是**唯一**一个问题：**「某一轴位置没动」这件事，是不是地形钉住的
    证据？**

    只有 :func:`_air_tick` 会钉住某一轴（撞墙那一支 `nx = body.x`、撞顶那一支
    把 `v.y` 截成 0）。它跑过 ⇒ `True`：位置真按空中速度推过了，推完还没动就是
    地形挡的。它没跑 ⇒ `False`：

    * **弹跳台**（`jump_pad_launch`）—— 原版 `JumpingObj::Tick` 是**本格末尾写
      速度、下一格才按速度挪位置**，所以「刚离地、位置没变、`vy≈−31`」是完全
      合法的一格。这时候位置没动**不能**当成撞墙；
    * 踩在地上走（含走出崖边）—— 那一步的位移来自走速，不是空中积分；
    * 没有地形（`terrain is None`）—— 什么都没算。

    `bot._reportable_speed()` 拿它分流：`False` 时速度原样报，`True` 时才套
    §181 那条「被钉住的那一轴报 0」。**判据由算物理的这一方说出来**，不让上层
    按 `before.on_ground` 之类的代理去猜 —— 那个代理在「贴着墙从地面起跳」
    这一格上是错的（`jump()` 之后 `_air_tick` 照跑，x 会被墙钉住）。
    """
    if terrain is None:
        return body, False
    if not body.on_ground and want_jump:
        # ★ 腾空中按跳 = 第二段跳（§124）。用掉了就什么都不做。
        body = double_jump(body)
    elif body.on_ground and want_drop:
        body = drop_through(terrain, body)
    elif body.on_ground and want_jump:
        # ★ 起跳带走**这一刻的走速**：腾空之后就再也改不了了（§93）。
        speed = walk_speed(character, fast_run, crouched, speed_scale)
        if direction > 0:
            body = jump(body, speed)
        elif direction < 0:
            body = jump(body, -speed)
        else:
            body = jump(body)               # 站着起跳 —— 竖直上下
    if body.on_ground:
        body = _walk_tick(terrain, body, character, direction,
                          fast_run, crouched, speed_scale)
        # ★★★ 弹跳台（§99）：台子每帧扫一遍**踩在地上**的角色，够得着就把人
        #   弹出去。排在走路**之后** —— 实机那一发心跳里人是「又走了一步、
        #   同时被弹起来」的（`(1742,904) -> (1721,905) v=(0,−31)`）。
        launched = jump_pad_launch(terrain, body, character)
        if launched is None:
            return body, False
        return launched, False
    return _air_tick(terrain, body), True


def tick(terrain, body, character, direction=0, fast_run=False,
         crouched=False, want_jump=False, want_drop=False, speed_scale=1.0):
    """走一个 tick（32 ms），返回**新的** `Body`。

    `direction`：−1 左 / 0 不按 / +1 右，就是心跳里那个方向键掩码（§39）。
    ★ 它**只在踩着地的时候有意义**（§93）—— 腾空那一段收方根本不读键。
    `want_jump`：这一 tick 要不要起跳（只在踩着地时有效）。
    `want_drop`：这一 tick 要不要按 ↓ 穿过脚下单向平台；和跳同时给时下落优先。

    ★ 这是 :func:`step` 只取新 `Body` 的简写。要**报心跳**的地方用 `step()`
      —— 它多告诉你「位置这一格积分了没有」（§185）；寻路 / 预演那些只关心
      落点的地方用这个就行。
    """
    return step(terrain, body, character, direction=direction,
                fast_run=fast_run, crouched=crouched, want_jump=want_jump,
                want_drop=want_drop, speed_scale=speed_scale)[0]


def advance(terrain, body, character, ticks, direction=0, fast_run=False,
            crouched=False, want_jump=False, want_drop=False, speed_scale=1.0):
    """连走 `ticks` 个 tick。起跳/下落只在第一个 tick 上生效。"""
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
    """从图顶自由落到图底要几个 tick —— 「掉不到底」的**几何上界**。

    `h = ½ g t²  ⇒  t = √(2h/g)`，再多给两个 tick 兜底。

    ★★ 会话 41 补的。以前这几个「会不会掉下去」的判据统一用
    `TICKS_PER_BEAT * 8 = 32` 个 tick —— 那只够落 **614** 个单位。
    `Megatron01` 高 2048、`Megatron00` 高 2048，从上层往下掉一趟远不止 614
    ⇒ `drop_below()` 返回 `None`，`bot._walk_to()` 把它当成**无底洞**，
    于是「站在高处的 bot 死活不肯往下走」——用户 2026-08-30 报的
    「只能看到一个 bot，另外两个像是在图外」和闯关那条「总有 bot 待在最左边
    不往前走」都有它的份。

    ★ 这是**地图有多高**这个几何事实，不是「等多久算超时」那类阈值
    （和 `BOT_SHELL_MAX_TRAVEL` 取图的对角线是同一个道理）。
    没有地形时退回老值，行为一个字节不变。
    """
    height = getattr(terrain, "height", None)
    if not height:
        return TICKS_PER_BEAT * 8
    return int(math.sqrt(2.0 * float(height) / GRAVITY)) + 2


def settle(terrain, body, character, ticks=None):
    """让人落到地上（新出生 / 刚接管位置时用）。落不到就原样返回。"""
    if ticks is None:
        ticks = fall_ticks(terrain)
    for _ in range(max(0, int(ticks))):
        if body.on_ground:
            return body
        body = tick(terrain, body, character)
    return body


def blocked(terrain, body, character, direction, fast_run=False,
            crouched=False, speed_scale=1.0):
    """朝 `direction` 走一步会不会**撞在墙上**（原地不动）。"""
    if not direction or not body.on_ground:
        return False
    return tick(terrain, body, character, direction=direction,
                fast_run=fast_run, crouched=crouched,
                speed_scale=speed_scale).x == body.x


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
    """原地起跳、空中一路按着 `direction`，**落在哪**；落不到返回 `None`。

    这就是「跳跃弧线」：要不要跳过这个坑、够不够得着那个台子，
    问它就行 —— 弧线是真跑出来的，不是拿公式估的。

    ★ `speed_scale` 要和真起跳那一刻的一致（V0.3 §151）：起跳带走的是**这一刻
      的走速**（§93），被冻住（0.0）/ 踩了减速胶水（0.3）时不传的话，
      预测出来的是一条满速弧线，而真跑出来的是原地竖直跳。
    """
    if ticks is None:
        # 起跳先上去、再落到图底：升段 `v/g` 个 tick，落段见 `fall_ticks()`。
        ticks = fall_ticks(terrain) + int(JUMP_SPEED / GRAVITY) + 2
    body = tick(terrain, body, character, direction=direction,
                fast_run=fast_run, crouched=crouched, want_jump=True,
                speed_scale=speed_scale)
    for _ in range(max(0, int(ticks))):
        if body.on_ground:
            return body
        body = tick(terrain, body, character, direction=direction,
                    fast_run=fast_run, crouched=crouched,
                    speed_scale=speed_scale)
    return body if body.on_ground else None


def at_apex(body):
    """这一 tick 是不是**这段腾空的顶点** —— 第二段跳该按下去的那一刻。

    ★ 判据是「已经不再上升了」这个**物理事实**（`v.y >= 0`，y 向下为正），
    不是「起跳后第 N 个 tick」这种阈值（铁律 10）。

    ★★ 规划（`botnav._double_jump_edge`）、执行（`bot._route_intent`）和
    兜底（`bot._walk_to` 跨坑那一条）**必须用同一句** —— 三边不一致的话
    规划出来的落点和真跑出来的对不上。所以它住在这里，另外两处都来问它。
    """
    return (body is not None and not body.on_ground
            and not body.air_jumped and body.vy >= 0.0)


def double_jump_lands(terrain, body, character, direction, fast_run=False,
                      crouched=False, ticks=None, speed_scale=1.0):
    """★★ 起跳 + **在顶点再按一次**，落在哪；落不到返回 `None`（§124）。

    和 `jump_lands()` 是一对：那个问「一段跳够不够」，这个问「两段够不够」。
    坑宽到一段跳过不去、两段跳过得去时，缺了它 bot 只会一遍遍地一段跳
    掉进坑里 —— 用户 2026-08-30：「经过岩浆时，bot 似乎不会用二段跳来跳到
    对面平台，只会用一段跳，然后反复掉进岩浆。」

    ★ 没跳成第二段（比如起跳那一下就落回地面）一律返回 `None`：调用方拿它
      当「二段跳能不能过去」的答案，跳不成就不算数。
    """
    if ticks is None:
        # 两段的升段各 `v/g` 个 tick，再加从图顶落到图底那一趟。
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
        current = tick(terrain, current, character, want_jump=want)
    if not current.on_ground or not jumped:
        return None
    return current


def drop_below(terrain, body, character, direction, fast_run=False,
               crouched=False, ticks=None, speed_scale=1.0):
    """走出崖边之后会掉多深（掉不到底返回 `None`）。

    给决策层用：真人不会主动跳进无底洞，但**从一米高的台阶走下去**
    再正常不过 —— 判据是「掉多深」，不是「许不许离地」。

    ⚠ 它只看**下一步**。「这份意图握着的这几格里会不会踩进无底洞」要问
    `bottomless_ahead()`，别拿这个凑（V0.3 §151）。
    """
    step = tick(terrain, body, character, direction=direction,
                fast_run=fast_run, crouched=crouched, speed_scale=speed_scale)
    if step.on_ground:
        return 0.0
    landed = settle(terrain, step, character, ticks)
    if not landed.on_ground:
        return None
    return landed.y - body.y


def bottomless_ahead(terrain, body, character, direction, fast_run=False,
                     crouched=False, speed_scale=1.0, ticks=1):
    """接下来这 `ticks` 格照这个方向走，**会不会踩进掉不到底的坑**。

    ## ★★★ 为什么要看不止一格（V0.3 §151）

    `drop_below()` 只推**一格**，而崖边那个「下一步就踩空」的窗口在真图上
    只有**一个走步宽**（`Quest02_1#Normal` 实测 8~11 像素）。意图不是每格
    重算的 —— 它由 `bot._decide()` 产出、要**握着用 `BOT_DECISION_TICKS`
    格**。于是约一半的接近位置整个跳过这个窗口：bot 一步走出崖边，等下一次
    决策时人已经腾空往坑里掉了，一次跳都没按。实测掉坑率 **50%**；把前瞻
    改成「这份意图要用几格」之后是 **0.1%**。

    ★ 这**不是**「跳过头 N 格」那类阈值（铁律 10）：`ticks` 就是这份意图的
      寿命，调用方拿自己的决策周期传进来。判据仍然是「照这么走会不会掉进
      无底洞」这个物理事实，只是把它放在**整段区间**上问，而不是只问第一格。

    ★ 决策频率一个字没改 —— §146 里用户明确否掉过「即将踩空就地重问」。
      改的是**看多远**，不是**多久看一次**。
    """
    if not direction:
        return False
    current = body
    for _ in range(max(1, int(ticks))):
        if not current.on_ground:
            break
        step = tick(terrain, current, character, direction=direction,
                    fast_run=fast_run, crouched=crouched,
                    speed_scale=speed_scale)
        if not step.on_ground:
            return not settle(terrain, step, character).on_ground
        if step.x == current.x:
            return False               # 撞墙了，再往后推也是原地
        current = step
    return False


def fits(terrain, x, y, character, crouched=False):
    """脚站在 `(x, y)` 时，**角色的碰撞体塞得进去吗**（V0.3 §152）。

    ## ★★★ 为什么需要它

    这个文件其余部分把角色当**一个点**（`_solid()` 只问一个像素），于是
    一条 1 像素宽的裂缝在模型里是完全合法的通路，`botnav` 用同一套物理建边
    ⇒ 裂缝里的点成了合法 A\\* 节点，A\\* 会**主动**把 bot 送进去。
    而真客户端用的是 `ChrProps.ini` 的三个碰撞圆（最宽 26 像素），人卡在
    缝口出不来 —— 用户 2026-09-01 报的 `Iceria03` (1174, 864) 就是它，
    两个 bot 先后卡在同一个像素上，一个 59 秒一个 13 秒。

    ★ 判据是「碰撞体塞不进去」这个**几何事实**，不是人工维护的坑位黑名单
      （铁律 10 / 铁律 11）。实测 6 张图：净空 < 24 的落脚点只占 0.3%~6%，
      而且几乎全部 < 12 —— 要么开阔要么发丝缝，中间没有灰区。

    圆心高度照抄 `chrprops.Character.circles()`（腿 / 身自下而上）。

    ⚠ **只查腿圆和身圆，不查头圆**：卡死人的缝全是「窄」不是「矮」，而
    低矮的通道（`CamelCulvert` 那种下水道）在原版里是走得过去的。原版的
    地形碰撞本身是什么形状我们没逆出来，所以只用「水平放不下最宽的那一圈」
    这条**看得见的事实**，不往上加推测。
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
    """`(x, y)` 这一行上，左右加起来有没有 `2 × radius` 的净空。

    只量**水平**方向：竖直方向由站立面本身保证（`surfaces()` 给的就是
    站得住的地方）。
    ★ 一侧的富余可以补另一侧的不足 —— **贴着墙站是合法的**，只要整条空隙
      放得下这一圈。所以两边各最多看 `2 × radius`，够了就提前收工。
    ★ 圆心那一格是实心时直接判塞不进去（人已经嵌在地形里了）。
    """
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
