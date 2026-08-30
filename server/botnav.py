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
"""
from __future__ import annotations

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


def _finish_air(terrain, body, character, ticks=AIR_TICKS):
    """把腾空状态推到落地，返回 ``(Body, 用掉的 tick)``；落不到返回 None。"""
    used = 0
    while not body.on_ground and used < ticks:
        body = botmove.tick(terrain, body, character)
        used += 1
    return (body, used) if body.on_ground else None


def _walk_edge(terrain, body, character, direction):
    current = body
    used = 0
    for _ in range(WALK_TICKS):
        nxt = botmove.tick(terrain, current, character, direction=direction)
        used += 1
        if nxt == current:
            break
        current = nxt
    if not current.on_ground:
        landed = _finish_air(terrain, current, character)
        if landed is None:
            return None
        current, air_used = landed
        used += air_used
    if _state_key(current) == _state_key(body):
        return None
    return current, Step(ACTION_WALK, current.x, current.y,
                         direction, False, float(used))


def _jump_edge(terrain, body, character, direction, fast_run):
    current = botmove.tick(terrain, body, character, direction=direction,
                           fast_run=fast_run, want_jump=True)
    if current.on_ground:
        return None
    landed = _finish_air(terrain, current, character)
    if landed is None:
        return None
    current, used = landed
    if _state_key(current) == _state_key(body):
        return None
    return current, Step(ACTION_JUMP, current.x, current.y,
                         direction, bool(fast_run), float(used + 1))


def at_apex(body):
    """这一 tick 是不是**这段腾空的顶点** —— 二段跳该按下去的那一刻。

    ★ 判据是「已经不再上升了」这个**物理事实**（`v.y >= 0`，y 向下为正），
    不是「起跳后第 N 个 tick」这种阈值（铁律 10）。站着起跳时它算出来正好
    是第 17 个 tick（初速 20 ÷ 重力 1.2），被弹跳台弹上去时自动往后挪 ——
    两种情形都不用另写一条规则。

    ★★ 图（`_double_jump_edge`）和执行层（`bot._route_intent`）**必须用
    同一句**，否则规划出来的落点和真跑出来的对不上。
    """
    return (body is not None and not body.on_ground
            and not body.air_jumped and body.vy >= 0.0)


def _double_jump_edge(terrain, body, character, direction, fast_run):
    """★ 二段跳边：起跳 -> 飞到顶点 -> 再跳一次 -> 落地（§124）。

    没有它的话，凡是要两段才上得去的平台在图里就是「不可达」——
    `Iceria02` 上整整一层（84 个落脚点）因此从来没进过 bot 的可达图，
    实机表现就是用户报的「我站高台上，bot 只会在下面来回跳」。
    """
    current = botmove.tick(terrain, body, character, direction=direction,
                           fast_run=fast_run, want_jump=True)
    if current.on_ground:
        return None
    used = 1
    jumped = False
    while not current.on_ground and used < AIR_TICKS:
        want = not jumped and at_apex(current)
        if want:
            jumped = True
        current = botmove.tick(terrain, current, character, want_jump=want)
        used += 1
    if not current.on_ground or not jumped:
        return None                        # 没落地 / 压根没跳成第二段
    if _state_key(current) == _state_key(body):
        return None
    return current, Step(ACTION_DOUBLE_JUMP, current.x, current.y,
                         direction, bool(fast_run), float(used), True)


def _drop_edge(terrain, body, character):
    current = botmove.tick(terrain, body, character, want_drop=True)
    if current.on_ground:
        return None
    landed = _finish_air(terrain, current, character)
    if landed is None:
        return None
    current, used = landed
    if current.y <= body.y or _state_key(current) == _state_key(body):
        return None
    return current, Step(ACTION_DROP, current.x, current.y,
                         0, False, float(used + 1))


def _pad_edge(terrain, body, character, double=False):
    """★★ **弹跳台**：站着不动，台子自己把人弹出去（§99）。

    `double=True` 时在弹起来的**顶点**再补一段跳 —— 台子把人送到 `dy` 那么
    高，第二段再叠 `24²/2.4 = 240`。用户 2026-08-30 要的「主动用跳高台跳
    上去」缺的正是这一条：光靠台子够不着的那一层，台子 + 二段跳够得着。

    ⚠ 起跳那一下**不能按跳**：`botmove.tick(want_jump=True)` 会走
    `jump()` 这一支，人先离地 ⇒ `jump_pad_launch()` 那一句根本轮不到，
    台子白站了。所以第一 tick 必须是「什么都不按」。
    """
    current = botmove.tick(terrain, body, character)
    if current.on_ground:                 # 脚下没有能触发的台
        return None
    used = 1
    jumped = False
    while not current.on_ground and used < AIR_TICKS:
        want = double and not jumped and at_apex(current)
        if want:
            jumped = True
        current = botmove.tick(terrain, current, character, want_jump=want)
        used += 1
    if not current.on_ground or (double and not jumped):
        return None
    if _state_key(current) == _state_key(body):
        return None
    return current, Step(ACTION_PAD, current.x, current.y,
                         0, False, float(used), bool(double))


def neighbors(terrain, body, character):
    """从一个落脚点实际模拟所有动作边，产出 ``(下一身体, Step)``。"""
    if terrain is None or body is None or not body.on_ground:
        return ()
    out = []
    # 平地优先；A* 在同一代价时会先沿地面找，不会无缘无故一路蹦。
    for direction in (-1, 1):
        edge = _walk_edge(terrain, body, character, direction)
        if edge is not None:
            out.append(edge)
    # 竖直跳能从下方穿过白线并落在其上；左右两档再覆盖高台/坑。
    for fast_run in (False, True):
        for direction in (-1, 0, 1):
            edge = _jump_edge(terrain, body, character, direction, fast_run)
            if edge is not None:
                out.append(edge)
            # ★ 同一组方向再来一条**二段跳**边：一段跳顶点 167，两段能到
            #   400 上下，很多高台只有它上得去。
            edge = _double_jump_edge(terrain, body, character, direction,
                                     fast_run)
            if edge is not None:
                out.append(edge)
    edge = _drop_edge(terrain, body, character)
    if edge is not None:
        out.append(edge)
    # ★★ 弹跳台两条：光弹上去、以及**弹上去之后在顶点再补一段跳**。
    #    后者是「主动用跳高台上高处」真正缺的那一条（用户 2026-08-30）。
    for double in (False, True):
        edge = _pad_edge(terrain, body, character, double=double)
        if edge is not None:
            out.append(edge)
    return tuple(out)


# ---------------------------------------------------------------------------
# ★★★ 边缓存 —— 可达图是 (地形, 角色尺度) 的**静态事实**，算一次就够
# ---------------------------------------------------------------------------
#: `地形对象 -> {角色尺度: {落脚点格: (代表身体, 边元组)}}`。
#:
#: ★ 用**弱引用键**而不是图名：`mapdata.load()` 缓存的地形对象在进程里是
#:   稳定的，而单测里的合成地形名字全叫 `Tiny` —— 按名字缓存会串味。
#:   地形被丢掉时这一份自然跟着没。
_EDGE_CACHE = weakref.WeakKeyDictionary()


def _scale_key(character):
    """角色身上**唯一影响可达图**的两个量：走速和腿的碰撞半径。

    走速决定走 / 跳的水平位移，腿半径决定够不够得着弹跳台
    （`botmove.jump_pad_launch`）。其余属性（血量、招式、武器）和地形无关，
    所以 17 个角色只落在 9 种组合上，缓存自然共用。
    """
    return (float(getattr(character, "speed", 7.0) or 7.0),
            float(getattr(character, "size_legs", 12.0) or 12.0))


def graph_of(terrain, character):
    """取（必要时新建）这张图 + 这类角色的边缓存。"""
    per_scale = _EDGE_CACHE.get(terrain)
    if per_scale is None:
        per_scale = {}
        _EDGE_CACHE[terrain] = per_scale
    key = _scale_key(character)
    graph = per_scale.get(key)
    if graph is None:
        graph = {}
        per_scale[key] = graph
    return graph


def node(graph, terrain, character, body):
    """一个落脚点在图里的那一条记录：`(代表身体, 边元组)`。

    ★ 同一个 8×4 格里的身体共用一条记录 —— 这**正是 A\\* 本来就做的近似**
    （`_state_key` 去重），这里只是把它固化下来，顺便让同一次规划里
    「谁先到这一格」不再影响后续展开。
    """
    key = _state_key(body)
    got = graph.get(key)
    if got is None:
        got = (body, neighbors(terrain, body, character))
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
        _representative, edges = node(graph, terrain, character, body)
        for next_body, _step in edges:
            if _state_key(next_body) not in seen:
                stack.append(next_body)
    return done


def plan(terrain, start, character, goal, max_expansions=MAX_EXPANSIONS):
    """从 ``start`` 到 ``goal`` 跑 A*，返回 ``tuple[Step, ...]``。

    只接受已落地的起点；起点本来就在目标附近返回空。

    ★★ **够不着的目标不再空手而归**：泛洪完之后挑「可达点里离目标最近的
    那个」走过去 —— 一步都靠近不了才返回空，调用方这时才退回
    「撞墙就跳 / 坑前停下」的老兜底。理由见文件头第 3 条。
    """
    if terrain is None or start is None or character is None:
        return ()
    if not start.on_ground or _at_goal(start, goal):
        return ()
    graph = graph_of(terrain, character)
    speed = botmove.walk_speed(character)
    goal_x, goal_y = _goal_xy(goal)

    def gap(body):
        return math.hypot(body.x - goal_x, body.y - goal_y)

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
        body, edges = node(graph, terrain, character, bodies[key])
        expansions += 1
        if _at_goal(body, goal):
            return route(key)
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
        return route(nearest[1])
    return ()


def step_reached(body, step):
    """运行时身体是否已经到达一条规划边的落脚点。"""
    return (body is not None and body.on_ground
            and abs(body.x - step.x) <= KEY_X
            and abs(body.y - step.y) <= KEY_Y)
