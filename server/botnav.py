#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""botnav.py —— 用真实角色物理按需生成可达图并跑 A*（V0.3 M5-B）。

这里不从碰撞图“猜”某个平台能不能跳上去。每一条边都实际调用
``botmove.tick()`` 逐 tick 模拟，只有最终重新落在地面上的动作才进入图：

* ``walk``：左右走一小段；走出边缘后确实能落地才算边；
* ``jump``：普通/冲刺起跳，完整飞行到落地；
* ``drop``：按 ↓ 穿脚下值-1 单向平台；
* ``pad``：原地踩中的弹跳台，仍由 ``botmove.jump_pad_launch()`` 解弹道。

因此高台、坑、斜坡、白线和弹跳台没有第二套近似几何。角色物理以后修正，
可达图会自动跟着变。

图是**按请求懒生成**的：A* 展开一个落脚点时才模拟它的邻边。地图宽通常只有
几千单位，固定走 8 tick 的宏步后，一次规划只有几十到几百个状态；不需要把
174 张逐像素地形预先膨胀成庞大静态图。

只用标准库；发布运行时是 CPython 3.8。
"""
from __future__ import annotations

import collections
import heapq
import itertools
import math

import botmove


ACTION_WALK = "walk"
ACTION_JUMP = "jump"
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

#: 防损坏地图/异常状态把一帧拖死的空间上限。正常合成图和真图远达不到它。
MAX_EXPANSIONS = 1200


class Step(collections.namedtuple(
        "Step", "action x y direction fast_run cost")):
    """A* 路径中的一条可执行边；``x/y`` 是这条边模拟得到的落脚点。"""

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


def _pad_edge(terrain, body, character):
    current = botmove.tick(terrain, body, character)
    if current.on_ground:                 # 脚下没有能触发的台
        return None
    landed = _finish_air(terrain, current, character)
    if landed is None:
        return None
    current, used = landed
    if _state_key(current) == _state_key(body):
        return None
    return current, Step(ACTION_PAD, current.x, current.y,
                         0, False, float(used + 1))


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
    edge = _drop_edge(terrain, body, character)
    if edge is not None:
        out.append(edge)
    edge = _pad_edge(terrain, body, character)
    if edge is not None:
        out.append(edge)
    return tuple(out)


def plan(terrain, start, character, goal, max_expansions=MAX_EXPANSIONS):
    """从 ``start`` 到 ``goal`` 跑 A*，返回 ``tuple[Step, ...]``。

    只接受已落地的起点。找不到返回空 tuple；起点本来就在目标附近也返回空。
    调用方可以安全地退回原来的“撞墙就跳/坑前停下”行为。
    """
    if terrain is None or start is None or character is None:
        return ()
    if not start.on_ground or _at_goal(start, goal):
        return ()
    speed = botmove.walk_speed(character)
    start_key = _state_key(start)
    bodies = {start_key: start}
    costs = {start_key: 0.0}
    parents = {}
    serial = itertools.count()
    queue = [(_heuristic(start, goal, speed), next(serial), start_key)]
    closed = set()
    expansions = 0

    while queue and expansions < max(0, int(max_expansions)):
        _priority, _serial, key = heapq.heappop(queue)
        if key in closed:
            continue
        closed.add(key)
        body = bodies[key]
        expansions += 1
        if _at_goal(body, goal):
            path = []
            while key != start_key:
                previous, step = parents[key]
                path.append(step)
                key = previous
            path.reverse()
            return tuple(path)

        for next_body, step in neighbors(terrain, body, character):
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
    return ()


def step_reached(body, step):
    """运行时身体是否已经到达一条规划边的落脚点。"""
    return (body is not None and body.on_ground
            and abs(body.x - step.x) <= KEY_X
            and abs(body.y - step.y) <= KEY_Y)
