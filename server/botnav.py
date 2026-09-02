#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""botnav.py —— 用真实角色物理按需生成可达图并跑 A*（V0.3 M5-B）。

这里不从碰撞图“猜”某个平台能不能跳上去。每一条边都实际调用
``botmove.tick()`` 逐 tick 模拟，只有最终重新落在地面上的动作才进入图：

* ``walk``：左右走一小段；走出边缘后确实能落地才算边；
* ``jump``：普通/冲刺起跳，完整飞行到落地；
* ``djump``：★ **二段跳** —— 起跳之后在顶点再按一次（§124）；
* ``drop``：按 ↓ 穿脚下值-1 单向平台；
* ``pad``：原地踩中的弹跳台，仍由 ``botmove.jump_pad_launch()`` 解弹道。

因此高台、坑、斜坡、白线和弹跳台没有第二套近似几何。角色物理以后修正，
可达图会自动跟着变。

图是**按请求懒生成**的：A* 展开一个落脚点时才模拟它的邻边，并且**记进
缓存**（见下）。

只用标准库；发布运行时是 CPython 3.8。

## ★★★ 会话 41：三件事一起改（都是实机报出来的）

### 1. 二段跳进图 —— 「bot 不会跳上高台」的真正原因

图里原来只有**一段**跳，顶点 `20²/2.4 = 167`。`Iceria02` 上从任何一个
出生点泛洪，**y 一路只到 396**：上面那一层（出生点表里写着 y=255）在模型
里根本不可达 —— 所以 bot 只能在下层来回蹦。加上二段跳边之后同一张图
泛洪到 **y=244**，上层多出 84 个落脚点。

二段跳按在**第一段的顶点**（`v.y >= 0` 的第一个 tick）。这不是「第 17 个
tick」这种阈值（铁律 10）：顶点是这段弧线上「再跳一次能上得最高」的那一刻，
是几何事实；而且**运行层用的是同一句判据**，图和执行天然一致。

### 2. 边缓存 —— 「画面卡顿 / bot 位置闪来闪去」的真正原因

一条边要跑几十到一百多个 `botmove.tick()`，一次 `neighbors()` 约 0.35 ms。
目标够不着时 A* 会把**整个可达分量**（真图 1000~2400 个落脚点）泛洪一遍
= **270~660 ms**，而 `bot._own_step()` 是**逐 tick**问路的 ⇒ 一帧最多问
16 次 × 每个 bot。实机日志里同步转发耗时 `max=4756 ms`、平均 60 ms ——
真人的位置包被堵在后面，屏幕上就是一卡一卡；bot 自己则因为「一帧攒了
半秒的位移」被一次性推出去，看着就是**闪现**（严重时穿墙）。

⇒ 可达图是 **(地形, 角色尺度) 的静态事实**，算一次就够。缓存之后同一次
泛洪 **270 ms → 4.6 ms**（实测）。缓存挂在**地形对象**上（弱引用），
换图 / 进程内多张图各自独立，测试里每个合成地形也天然互不干扰。

### 3. 找不到完整路线时**尽量靠近**，而不是空手而归

目标站在够不着的高台上是**常态**（一张图的可达分量往往只覆盖一半）。
原来这时候 `plan()` 返回空，调用方退回「墙根跳 / 坑前停」——闯关模式里
就是「bot 卡在最左边不往前走，拖着全队的进度」（用户 2026-08-30 实机）。
现在改成：泛洪完之后挑**离目标最近的那个可达落脚点**走过去，
只有「一步都靠近不了」时才真的返回空。真人也是这么做的。

## ★★★★★ 会话 55：**增量**边缓存 —— 变体从母地形继承（§170 / D129）

打碎一件破坏物换来的是一份新地形对象，会话 41 那份缓存是**按对象**做键的，
于是每碎一次就整张可达图从头重算（`Esperan03` 实测 `botplan` 冷 **832 ms**）
—— 那一下把每条连接的发送线程饿住，用户看到的就是「积压几秒的包一起爆发」
（§163 / D125）。可变体和母地形的差别**只在那一件罐子附近**。

⇒ 一个落脚点固定跑 **17 次尝试**（`_ATTEMPTS`），每次尝试记下自己摸过的
那一片（`_Trace`）；变体建这一格时逐条问「这一片被碎掉的那几件碰到了没有」，
没碰到就把母地形那条边整条搬过来。实测碎一件之后整图泛洪
**1576 -> 198 ms**（`Esperan03`）、**1617 -> 51 ms**（`Iceria02`）。
"""
from __future__ import annotations

import array
import collections
import heapq
import itertools
import math
import weakref

import botmove


ACTION_WALK = "walk"
ACTION_JUMP = "jump"
#: ★ 二段跳边：起跳之后在**顶点**再按一次跳（§124）。执行层认的是同一句
#: 判据（`bot._route_intent()`），所以图上算出来的落点和真跑出来的一致。
ACTION_DOUBLE_JUMP = "djump"
ACTION_DROP = "drop"
ACTION_PAD = "pad"

#: 一条步行边走几个逻辑 tick。8 tick = 256 ms；基础角色约走 48~64 单位。
#: 这是图的**空间分辨率**，不是行为定时阈值。
WALK_TICKS = 8

#: 一条腾空边最多模拟多久。普通跳完整往返约 34 tick；最高的现有弹跳台也
#: 远低于 160。到这里仍未落地就视作掉出地图/不可达，不把它放进图。
AIR_TICKS = 160

#: 连续落脚点的去重网格。它只影响 A* 状态数量；动作本身仍保留完整浮点落点。
KEY_X = 8.0
KEY_Y = 4.0

#: 到目标身体附近即算找到路线。真正“现在能不能开枪”仍由 bot._fire_target
#: 的弹道与遮挡判定决定；这里不复制武器射程规则。
GOAL_X = 64.0
GOAL_Y = 48.0

#: 防损坏地图/异常状态把一帧拖死的空间上限。
#:
#: ★ 缓存热了之后一次展开只要 ~5 µs（实测 905 次 4.6 ms），所以这个数不再
#: 是「性能刹车」而是**空间上界**：现有最大的一张图（`Quest03_1`，
#: 11400 宽）整个可达分量是 2364 个落脚点，留一倍余量。
MAX_EXPANSIONS = 5000


class Step(collections.namedtuple(
        "Step", "action x y direction fast_run cost double", defaults=(False,))):
    """A* 路径中的一条可执行边；``x/y`` 是这条边模拟得到的落脚点。

    ``double`` = 这条边在**腾空的顶点**要不要再按一次跳（§124 的第二段）。
    起跳那一下按哪个键由 ``action`` 决定（`jump` / `djump` 在地上按，
    `pad` 是站着让台子把人弹出去）—— 两件事分开记，所以「弹跳台弹上去之后
    再补一段跳」也表示得出来（用户 2026-08-30 要的那条）。
    """

    __slots__ = ()


class PlanResult(collections.namedtuple(
        "PlanResult", "path reached cost gap")):
    """A* 的完整答案。

    `path` 仍是老接口的 `tuple[Step, ...]`；`reached` 区分“真到了”
    和 D85 的“只能走到最近处”；`cost` 是路径上原版物理 tick 成本之和；
    `gap` 是终点到目标的几何距离。

    旧的 `plan()` 仍只返回 `path`，所有现有调用无需改。只有“完好
    地形 vs 打通捷径”的后台比较需要后三格。
    """

    __slots__ = ()


def _state_key(body):
    return (int(round(body.x / KEY_X)), int(round(body.y / KEY_Y)))


def _goal_xy(goal):
    if isinstance(goal, botmove.Body):
        return goal.x, goal.y
    return float(goal[0]), float(goal[1])


def _at_goal(body, goal):
    gx, gy = _goal_xy(goal)
    return abs(body.x - gx) <= GOAL_X and abs(body.y - gy) <= GOAL_Y


def _heuristic(body, goal, speed):
    gx, gy = _goal_xy(goal)
    # 用“约需多少 tick”作量纲；不要求可采纳，只负责让队列先看靠近目标的点。
    return math.hypot(body.x - gx, body.y - gy) / max(1.0, speed)


#: 依赖区存成「几号格」的格子边长（像素）。它只是**存储精度**，不是判据里
#: 的阈值：取整一律朝外，粗一点只会让多几条边被重算，不会算错。
_BOX = 16.0


def _block(value):
    """像素坐标 -> 格号，**朝下取整**（负数也是，所以左/上边界不会缩）。"""
    return int(math.floor(value / _BOX))


class _Trace(object):
    """一次尝试**摸过的那一片地形**：所有身体位置的外接矩形 + 最大 |vx|。

    ★★★ 它是**增量边缓存**的判据（V0.3 §170）：一条边只可能被它自己走过的
      那一片影响，那一片没变，这条边就一个字都不用重算。
    """

    __slots__ = ("x0", "x1", "y0", "y1", "vmax")

    def __init__(self, body):
        self.x0 = self.x1 = body.x
        self.y0 = self.y1 = body.y
        self.vmax = 0.0

    def see(self, body):
        x = body.x
        if x < self.x0:
            self.x0 = x
        elif x > self.x1:
            self.x1 = x
        y = body.y
        if y < self.y0:
            self.y0 = y
        elif y > self.y1:
            self.y1 = y
        v = body.vx
        if v < 0.0:
            v = -v
        if v > self.vmax:
            self.vmax = v

    def box(self, character):
        """外接矩形**再往外放一圈探针够得着的距离**，得到真正的依赖区。

        返回的是 **16 像素一格的格号** `(x0, x1, y0, y1)`（`_BOX`）——
        一律**朝外**取整，所以只会多算不会少算，多算的后果只是「多重跑一条
        边」。存格号而不是浮点：一个落脚点 17 次尝试，存浮点元组要 4 KB，
        存 4 个 `int` 只要 300 字节，而一张真图 1200 个落脚点 × 每个角色
        尺度一份（`Esperan03` 实测 9.4 -> 5.9 MB）。

        `botmove` 问地形的地方，探针最远伸出多少，全是**角色自己的尺寸**
        （不是拍出来的常量）：

        * `fits()` 的 `_clearance_ok` 左右各扫 `2 × 半径`，身圆那一行在脚
          上方 `2 × 腿半径 + 身半径`；
        * `surface_near()` 的 reach 是 `速度 × CLIMB_SLOPE` —— 走路那一步
          用走速，腾空那一步用 `|vx|`（弹跳台能给出比走速大的 `vx`，
          所以这里取「实际见过的最大值」和冲刺走速里大的那个）。

        ★ 列方向必须放够：`surfaces(x)` / `ground_below(x, y)` 问的是**整列**，
          只要那一列没变，这一列上的每一问答案都一样。
        """
        legs = float(getattr(character, "size_legs", 12.0) or 12.0)
        body_r = float(getattr(character, "size_body", 13.0) or 13.0)
        speed = botmove.walk_speed(character, True)
        margin = max(2.0 * legs, 2.0 * body_r, 2.0 * legs + body_r,
                     max(self.vmax, speed) * botmove.CLIMB_SLOPE) + 1.0
        return (_block(self.x0 - margin), _block(self.x1 + margin),
                _block(self.y0 - margin), _block(self.y1 + margin))


def _finish_air(terrain, body, character, trace, ticks=AIR_TICKS):
    """把腾空状态推到落地，返回 ``(Body, 用掉的 tick)``；落不到返回 None。"""
    used = 0
    while not body.on_ground and used < ticks:
        body = botmove.tick(terrain, body, character)
        trace.see(body)
        used += 1
    return (body, used) if body.on_ground else None


def _walk_edge(terrain, body, character, trace, direction):
    current = body
    used = 0
    for _ in range(WALK_TICKS):
        nxt = botmove.tick(terrain, current, character, direction=direction)
        trace.see(nxt)
        used += 1
        if nxt == current:
            break
        current = nxt
    if not current.on_ground:
        landed = _finish_air(terrain, current, character, trace)
        if landed is None:
            return None
        current, air_used = landed
        used += air_used
    if _state_key(current) == _state_key(body):
        return None
    return current, Step(ACTION_WALK, current.x, current.y,
                         direction, False, float(used))


def _jump_edge(terrain, body, character, trace, direction, fast_run):
    current = botmove.tick(terrain, body, character, direction=direction,
                           fast_run=fast_run, want_jump=True)
    trace.see(current)
    if current.on_ground:
        return None
    landed = _finish_air(terrain, current, character, trace)
    if landed is None:
        return None
    current, used = landed
    if _state_key(current) == _state_key(body):
        return None
    return current, Step(ACTION_JUMP, current.x, current.y,
                         direction, bool(fast_run), float(used + 1))


def at_apex(body):
    """这一 tick 是不是**这段腾空的顶点** —— 二段跳该按下去的那一刻。

    ★ 真正的判据住在 `botmove.at_apex()`：规划（这里）、执行
      （`bot._route_intent`）和跨坑兜底（`bot._walk_to`）**必须用同一句**，
      否则规划出来的落点和真跑出来的对不上。这里只是个转发，留着是因为
      图这一侧到处都在用这个名字。
    """
    return botmove.at_apex(body)


def _double_jump_edge(terrain, body, character, trace, direction, fast_run):
    """★ 二段跳边：起跳 -> 飞到顶点 -> 再跳一次 -> 落地（§124）。

    没有它的话，凡是要两段才上得去的平台在图里就是「不可达」——
    `Iceria02` 上整整一层（84 个落脚点）因此从来没进过 bot 的可达图，
    实机表现就是用户报的「我站高台上，bot 只会在下面来回跳」。
    """
    current = botmove.tick(terrain, body, character, direction=direction,
                           fast_run=fast_run, want_jump=True)
    trace.see(current)
    if current.on_ground:
        return None
    used = 1
    jumped = False
    while not current.on_ground and used < AIR_TICKS:
        want = not jumped and at_apex(current)
        if want:
            jumped = True
        current = botmove.tick(terrain, current, character, want_jump=want)
        trace.see(current)
        used += 1
    if not current.on_ground or not jumped:
        return None                        # 没落地 / 压根没跳成第二段
    if _state_key(current) == _state_key(body):
        return None
    return current, Step(ACTION_DOUBLE_JUMP, current.x, current.y,
                         direction, bool(fast_run), float(used), True)


def _drop_edge(terrain, body, character, trace):
    current = botmove.tick(terrain, body, character, want_drop=True)
    trace.see(current)
    if current.on_ground:
        return None
    landed = _finish_air(terrain, current, character, trace)
    if landed is None:
        return None
    current, used = landed
    if current.y <= body.y or _state_key(current) == _state_key(body):
        return None
    return current, Step(ACTION_DROP, current.x, current.y,
                         0, False, float(used + 1))


def _pad_edge(terrain, body, character, trace, double=False):
    """★★ **弹跳台**：站着不动，台子自己把人弹出去（§99）。

    `double=True` 时在弹起来的**顶点**再补一段跳 —— 台子把人送到 `dy` 那么
    高，第二段再叠 `24²/2.4 = 240`。用户 2026-08-30 要的「主动用跳高台跳
    上去」缺的正是这一条：光靠台子够不着的那一层，台子 + 二段跳够得着。

    ⚠ 起跳那一下**不能按跳**：`botmove.tick(want_jump=True)` 会走
    `jump()` 这一支，人先离地 ⇒ `jump_pad_launch()` 那一句根本轮不到，
    台子白站了。所以第一 tick 必须是「什么都不按」。
    """
    current = botmove.tick(terrain, body, character)
    trace.see(current)
    if current.on_ground:                 # 脚下没有能触发的台
        return None
    used = 1
    jumped = False
    while not current.on_ground and used < AIR_TICKS:
        want = double and not jumped and at_apex(current)
        if want:
            jumped = True
        current = botmove.tick(terrain, current, character, want_jump=want)
        trace.see(current)
        used += 1
    if not current.on_ground or (double and not jumped):
        return None
    if _state_key(current) == _state_key(body):
        return None
    return current, Step(ACTION_PAD, current.x, current.y,
                         0, False, float(used), bool(double))


def neighbors(terrain, body, character):
    """从一个落脚点实际模拟所有动作边，产出 ``(下一身体, Step)``。

    ## ★★★ 落点**塞不进去**的边不生成（V0.3 §152）

    这一层的物理（`botmove`）把角色当一个点，于是一条 1 像素宽的裂缝在图里
    是完全合法的落脚点 —— A\\* 会**主动**把 bot 送进去当捷径，而真客户端用的
    是三个碰撞圆（最宽 26 像素），人卡在缝口出不来。
    实机 `Iceria03`：(1174, 864) 有 **31 条 `jump` 入边、0 条出边**，
    两个 bot 先后卡在那一个像素上，一个 59 秒一个 13 秒。
    ⇒ 出图之前问一句 `botmove.fits()`。判据是几何事实，不是人工坑位表。
    """
    if terrain is None or body is None or not body.on_ground:
        return ()
    _boxes, edges = _run_attempts(terrain, body, character)
    return tuple(edge for edge in edges if edge is not None)


def _attempt_plan():
    """一个落脚点上**固定的那几次尝试**，顺序写死。

    ★ 顺序就是增量继承的对齐方式（V0.3 §170）：变体按下标一条条问
      「这一次的足迹被那件罐子碰到了没有」，没碰到就直接沿用母地形算好的
      那一条。所以**往里加动作只能往后加**，不能插在中间。
    """
    plan = [(_walk_edge, (-1,)), (_walk_edge, (1,))]
    # 竖直跳能从下方穿过白线并落在其上；左右两档再覆盖高台/坑。
    for fast_run in (False, True):
        for direction in (-1, 0, 1):
            plan.append((_jump_edge, (direction, fast_run)))
            # ★ 同一组方向再来一条**二段跳**边：一段跳顶点 167，两段能到
            #   400 上下，很多高台只有它上得去。
            plan.append((_double_jump_edge, (direction, fast_run)))
    plan.append((_drop_edge, ()))
    # ★★ 弹跳台两条：光弹上去、以及**弹上去之后在顶点再补一段跳**。
    #    后者是「主动用跳高台上高处」真正缺的那一条（用户 2026-08-30）。
    plan.append((_pad_edge, (False,)))
    plan.append((_pad_edge, (True,)))
    return tuple(plan)


#: ★ 走路排在最前面：A* 在同一代价时会先沿地面找，不会无缘无故一路蹦。
_ATTEMPTS = _attempt_plan()


def _box_hits(boxes, at, rects):
    """第 `at // 4` 次尝试的依赖区和「变了的那几块」碰上了吗。"""
    x0, x1, y0, y1 = boxes[at], boxes[at + 1], boxes[at + 2], boxes[at + 3]
    for rx0, rx1, ry0, ry1 in rects:
        if x0 <= rx1 and x1 >= rx0 and y0 <= ry1 and y1 >= ry0:
            return True
    return False


def _run_attempts(terrain, body, character, base=None, rects=()):
    """跑这个落脚点的每一次尝试，返回 ``(依赖区数组, 每次的边 | None)``。

    依赖区是一条 `array("i")`，每次尝试占 4 个格号（`_Trace.box`）；
    边是和 `_ATTEMPTS` 一一对应的元组。两个分开放是为了省内存 ——
    这份东西每个落脚点、每个角色尺度都要留一份。

    `base` 给定时**只重跑**「依赖区被 `rects` 碰到」的那几次，其余原样沿用
    母地形算好的答案 —— 这就是增量（§170）。
    """
    boxes = []
    edges = []
    old_boxes, old_edges = base if base else (None, None)
    for index, (build, args) in enumerate(_ATTEMPTS):
        if old_boxes is not None:
            at = index * 4
            if not _box_hits(old_boxes, at, rects):
                boxes.extend(old_boxes[at:at + 4])
                edges.append(old_edges[index])
                continue
        trace = _Trace(body)
        edge = build(terrain, body, character, trace, *args)
        # ★ 落点塞不进去的边不进图（§152），判在这里而不是最后统一过一遍
        #   —— 这样「这一次尝试的答案」是自足的，继承时整条搬走就行。
        if edge is not None and not botmove.fits(terrain, edge[0].x,
                                                 edge[0].y, character):
            edge = None
        boxes.extend(trace.box(character))
        edges.append(edge)
    return array.array("i", boxes), tuple(edges)


# ---------------------------------------------------------------------------
# ★★★ 边缓存 —— 可达图是 (地形, 角色尺度) 的**静态事实**，算一次就够
# ---------------------------------------------------------------------------
#: `地形对象 -> {角色尺度: {落脚点格: (代表身体, 边元组, 每次尝试的足迹)}}`。
#: 第三格是 `_run_attempts()` 的返回值，只给**增量继承**用（§170）。
#:
#: ★ 用**弱引用键**而不是图名：`mapdata.load()` 缓存的地形对象在进程里是
#:   稳定的，而单测里的合成地形名字全叫 `Tiny` —— 按名字缓存会串味。
#:   地形被丢掉时这一份自然跟着没。
_EDGE_CACHE = weakref.WeakKeyDictionary()


def _scale_key(character):
    """角色身上**影响可达图**的那几个量：走速 + 两个碰撞半径。

    走速决定走 / 跳的水平位移，腿半径决定够不够得着弹跳台
    （`botmove.jump_pad_launch`）。★ V0.3 §152 起**身圆半径也算一个**：
    「落点塞不塞得下」用的就是腿圆和身圆，两个角色胖瘦不同、可达图就不同，
    不带进 key 的话它们会共用错的边缓存。
    其余属性（血量、招式、武器）和地形无关，缓存自然共用。
    """
    return (float(getattr(character, "speed", 7.0) or 7.0),
            float(getattr(character, "size_legs", 12.0) or 12.0),
            float(getattr(character, "size_body", 13.0) or 13.0))


def graph_of(terrain, character):
    """取（必要时新建）这张图 + 这类角色的边缓存。

    ★ 一律用 `setdefault`：预热线程、后台规划线程（`botplan`）和游戏线程
      会同时问到同一张图。`setdefault` 在 CPython 里是一次原子操作，
      三方拿到的**一定是同一个 dict**；写进去的边本来就幂等，
      同时算只是白做一遍功。
    """
    per_scale = _EDGE_CACHE.setdefault(terrain, {})
    return per_scale.setdefault(_scale_key(character), {})


def _changed_rects(source, terrain):
    """两份地形差在哪几块 —— 就是**存活集合的对称差**那几件的外接矩形。

    `variant()` 只换「哪几件破坏物还在」，别的一个格子都不动，所以差异面
    完整地写在那几件破坏物自己身上。没有差异（或者压根不可比）返回空。

    ★ 上下各多放一格：`_compose()` 重算站立面时下界就是多看一格的
      —— 盖住 `row1` 之后，原来 `row1 + 1` 那个站立面不再满足「正上方是空」。
    """
    mine = getattr(terrain, "alive", None)
    theirs = getattr(source, "alive", None)
    if mine is None or theirs is None:
        return ()
    diff = mine ^ theirs
    if not diff:
        return ()
    return tuple((_block(item.col0 - 1.0), _block(item.col1 + 1.0),
                  _block(item.row0 - 1.0), _block(item.row1 + 1.0))
                 for item in terrain.breakables
                 if item.index in diff and item.col1 >= 0)


#: 「破坏物全碎」那一份变体的存活集合。开局预热的就是它和母地形两张
#: （`bot.warm_navigation`），所以继承时只在这两个里挑。
_ALL_BROKEN = frozenset()


def _inherit_source(terrain, character, key):
    """挑一份**已经算过**的地形来继承这一格 + 两边差在哪几块。

    候选只有两个：**母地形**和**「全碎」那一份** —— 开局预热的正是这两张
    （`bot.warm_navigation`），所以它们一定是热的。挑**差得少**的那个：
    刚碎一件时母地形只差 1 件，快碎完时「全碎那份」差得更少。

    拿不到（自己就是候选 / 候选还没算过这一格）返回 `(None, ())`。
    """
    root = getattr(terrain, "_root", None)
    if root is None:
        return None, ()
    variants = getattr(root, "_variants", None)
    opened = None if variants is None else variants.get(_ALL_BROKEN)
    scale = _scale_key(character)
    best = None
    for source in (root, opened):
        if source is None or source is terrain:
            continue
        rects = _changed_rects(source, terrain)
        if not rects or (best is not None and len(rects) >= len(best[1])):
            continue
        per_scale = _EDGE_CACHE.get(source)
        graph = None if not per_scale else per_scale.get(scale)
        entry = None if not graph else graph.get(key)
        if entry is None or not entry[2]:
            continue
        best = (entry, rects)
    return best if best is not None else (None, ())


def node(graph, terrain, character, body):
    """一个落脚点在图里的那一条记录：`(代表身体, 边元组, 每次尝试的足迹)`。

    ★ 同一个 8×4 格里的身体共用一条记录 —— 这**正是 A\\* 本来就做的近似**
    （`_state_key` 去重），这里只是把它固化下来，顺便让同一次规划里
    「谁先到这一格」不再影响后续展开。

    ## ★★★ 破坏物变体**从母地形继承**，只重算被碰到的那几条（V0.3 §170）

    打碎一件罐子换来的是一份新地形对象，以前整张可达图要从头重算一遍
    （`Esperan03` 实测 **832 ms 冷** / 0.5~8 ms 热）—— 那一下压在纯 Python 上，
    把每条连接的发送线程饿住，用户看到的就是「积压几秒的包一起爆发」
    （§163 / D125）。可是变体和母地形的差别**只在那一件罐子附近**。

    ⇒ 每次尝试都记着自己摸过的那一片（`_Trace`）。变体建这一格时，
    逐条问「这一片被碎掉的那几件碰到了没有」：没碰到就把母地形那条边整条
    搬过来，碰到了才重跑。判据是**几何事实**，不是「跳过前 N 个」那类阈值。

    ★ 继承时用母地形那份记录的**代表身体**：搬过来的边就是从它算出来的，
      换个身体就对不上了。
    """
    key = _state_key(body)
    got = graph.get(key)
    if got is not None:
        return got
    source, rects = _inherit_source(terrain, character, key)
    if source is not None:
        body = source[0]
        attempts = _run_attempts(terrain, body, character, source[2], rects)
    elif terrain is None or body is None or not body.on_ground:
        attempts = ()
    else:
        attempts = _run_attempts(terrain, body, character)
    got = (body, tuple(edge for edge in attempts[1] if edge is not None)
           if attempts else (), attempts)
    graph[key] = got
    return got


def warm(terrain, character, seeds, limit=MAX_EXPANSIONS):
    """把 `seeds` 能走到的整片落脚点全部算进缓存，返回算了几个。

    ★ 这不是行为，是**预热**：可达图迟早要算，放在开局加载那几秒里算完，
    战斗中就一次都不用现算了（实机日志里那些 300 ms ~ 4.7 秒的同步转发
    停顿全出在「战斗中现算」）。**幂等**，谁先算完都一样，所以后台线程
    和游戏线程同时算也只是白做一遍功，不会算错。
    """
    if terrain is None or character is None:
        return 0
    graph = graph_of(terrain, character)
    stack = [seed for seed in (seeds or ())
             if seed is not None and seed.on_ground]
    seen = set()
    done = 0
    while stack and done < max(0, int(limit)):
        body = stack.pop()
        key = _state_key(body)
        if key in seen:
            continue
        seen.add(key)
        done += 1
        _representative, edges, _boxes = node(graph, terrain,
                                              character, body)
        for next_body, _step in edges:
            if _state_key(next_body) not in seen:
                stack.append(next_body)
    return done


def plan_result(terrain, start, character, goal,
                max_expansions=MAX_EXPANSIONS):
    """从 ``start`` 到 ``goal`` 跑 A*，返回 :class:`PlanResult`。

    只接受已落地的起点；起点本来就在目标附近返回空。

    ★★ **够不着的目标不再空手而归**：泛洪完之后挑「可达点里离目标最近的
    那个」走过去 —— 一步都靠近不了才返回空，调用方这时才退回
    「撞墙就跳 / 坑前停下」的老兜底。理由见文件头第 3 条。
    """
    if terrain is None or start is None or character is None:
        return PlanResult((), False, 0.0, float("inf"))
    goal_x, goal_y = _goal_xy(goal)

    def gap(body):
        return math.hypot(body.x - goal_x, body.y - goal_y)

    if not start.on_ground:
        return PlanResult((), False, 0.0, gap(start))
    if _at_goal(start, goal):
        return PlanResult((), True, 0.0, gap(start))
    graph = graph_of(terrain, character)
    speed = botmove.walk_speed(character)

    start_key = _state_key(start)
    bodies = {start_key: start}
    costs = {start_key: 0.0}
    parents = {}
    serial = itertools.count()
    queue = [(_heuristic(start, goal, speed), next(serial), start_key)]
    closed = set()
    expansions = 0
    #: 「离目标最近的可达落脚点」—— 起点自己就是初值，只有真比它近才换。
    nearest = (gap(start), start_key)

    def route(key):
        path = []
        while key != start_key:
            previous, step = parents[key]
            path.append(step)
            key = previous
        path.reverse()
        return tuple(path)

    while queue and expansions < max(0, int(max_expansions)):
        _priority, _serial, key = heapq.heappop(queue)
        if key in closed:
            continue
        closed.add(key)
        body, edges, _boxes = node(graph, terrain, character,
                                   bodies[key])
        expansions += 1
        if _at_goal(body, goal):
            return PlanResult(route(key), True, costs[key], gap(body))
        span = gap(body)
        if span < nearest[0]:
            nearest = (span, key)

        for next_body, step in edges:
            next_key = _state_key(next_body)
            if next_key in closed:
                continue
            new_cost = costs[key] + step.cost
            if new_cost >= costs.get(next_key, float("inf")):
                continue
            costs[next_key] = new_cost
            bodies[next_key] = next_body
            parents[next_key] = (key, step)
            priority = new_cost + _heuristic(next_body, goal, speed)
            heapq.heappush(queue, (priority, next(serial), next_key))

    # 到不了目标 —— 那就走到**够得着的最近处**（真人也是这么做的）。
    # ★ 门槛就是图自己的分辨率（`KEY_X`）：比一个格子还小的「靠近」不算
    #   靠近，原地待着更好，免得为了两三个单位来回跑。
    if nearest[1] != start_key and nearest[0] + KEY_X < gap(start):
        path = route(nearest[1])
        return PlanResult(path, False, costs[nearest[1]], nearest[0])
    return PlanResult((), False, 0.0, gap(start))


def plan(terrain, start, character, goal, max_expansions=MAX_EXPANSIONS):
    """兼容旧接口：只返回 A* 的 ``tuple[Step, ...]``。"""
    return plan_result(terrain, start, character, goal,
                       max_expansions=max_expansions).path


def _commands_for_step(terrain, body, character, step):
    """把一条 :class:`Step` 重放成逐 tick 按键和身体。

    建边和执行层的口径原本就是这一套；这份显式记录只给
    “捷径穿过了哪件可破坏物”用。返回 `[(按键字典, 这格后身体), ...]`。
    """
    out = []
    current = body

    def push(**keys):
        nonlocal current
        current = botmove.tick(terrain, current, character, **keys)
        out.append((keys, current))

    if step.action == ACTION_WALK:
        for _ in range(WALK_TICKS):
            before = current
            push(direction=step.direction)
            if current == before:
                break
        while not current.on_ground and len(out) < WALK_TICKS + AIR_TICKS:
            push()
        return out

    if step.action in (ACTION_JUMP, ACTION_DOUBLE_JUMP):
        push(direction=step.direction, fast_run=step.fast_run, want_jump=True)
    elif step.action == ACTION_DROP:
        push(want_drop=True)
    elif step.action == ACTION_PAD:
        push()                              # 先让脚下的台子把人弹起
    else:
        return out

    jumped = False
    while not current.on_ground and len(out) < AIR_TICKS:
        again = bool(step.double and not jumped and at_apex(current))
        if again:
            jumped = True
        push(want_jump=again)
    return out


def _breakable_at_transition(terrain, before, intended, character):
    """开放地形这一格能走、完整地形走不成：找出头一件挡住的东西。"""
    alive = getattr(terrain, "alive", frozenset())
    items = [item for item in getattr(terrain, "breakables", ())
             if item.index in alive]
    if not items:
        return None
    radius = max(float(getattr(character, "size_body", 13.0) or 13.0),
                 float(getattr(character, "size_legs", 12.0) or 12.0))
    x0, x1 = sorted((before.x, intended.x))
    y0, y1 = sorted((before.y, intended.y))
    candidates = [item for item in items
                  if item.left <= x1 + radius
                  and item.left + item.width >= x0 - radius
                  and item.top <= y1 + radius
                  and item.top + item.height >= y0 - radius]
    if not candidates:
        return None
    # 一个 tick 最多二十多像素，逐像素沿这一小段扫不会成为热点；
    # 它在 botplan 后台线程上，且只重放**选中的**那条路。
    samples = max(1, int(math.ceil(max(abs(intended.x - before.x),
                                       abs(intended.y - before.y)))))
    ordered = sorted(candidates,
                     key=lambda item: item.distance_to(before.x, before.y))
    for index in range(samples + 1):
        ratio = float(index) / samples
        px = before.x + (intended.x - before.x) * ratio
        py = before.y + (intended.y - before.y) * ratio
        probes = [(px, py), (px, py + 1.0)]
        for cx, cy, cr, _name in character.circles(px, py, False):
            probes.extend(((cx, cy), (cx - cr, cy), (cx + cr, cy),
                           (cx, cy - cr), (cx, cy + cr)))
        for item in ordered:
            if any(item.hit(tx, ty) for tx, ty in probes):
                return item.index
    # `botmove` 查的是斜率/脚下邻域，极端边缘可能比上面的圆
    # 多一格。差异已经证明挡住它的只可能是这些相交外接矩形之一，
    # 退到运动终点最近的那件，不把整条捷径判丢。
    return min(candidates,
               key=lambda item: item.distance_to(intended.x, intended.y)).index


def first_breakable_on_path(terrain, open_terrain, start, character, path):
    """重放捷径，返回 `(首个挡路物下标, 挡住前的安全 Step 前缀)`。

    两边吃**同一串按键**。第一格身体不同就是第一个动态
    破坏物产生影响的地方；除了破坏物，两份地形的每一个格完全相同。
    """
    if terrain is None or open_terrain is None or start is None:
        return None, ()
    open_body = start
    live_body = start
    prefix = []
    for step in path or ():
        trace = _commands_for_step(open_terrain, open_body, character, step)
        if not trace:
            return None, tuple(prefix)
        for keys, intended in trace:
            before = live_body
            actual = botmove.tick(terrain, live_body, character, **keys)
            if actual != intended:
                return (_breakable_at_transition(terrain, before, intended,
                                                  character),
                        tuple(prefix))
            live_body = actual
        open_body = trace[-1][1]
        prefix.append(step)
    return None, tuple(prefix)


def step_reached(body, step):
    """运行时身体是否已经到达一条规划边的落脚点。"""
    return (body is not None and body.on_ground
            and abs(body.x - step.x) <= KEY_X
            and abs(body.y - step.y) <= KEY_Y)
