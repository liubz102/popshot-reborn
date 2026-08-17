#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_death.py —— 「角色为什么死不掉 / 死了为什么不重生」的现场探针（会话 15 新增）

用户实机反馈：**血量归零后角色不死也不重生，只是变成无法操作的状态卡住**；
掉进岩浆同样不死。FINDINGS §90 曾判定「闯关模式死亡不发任何包」，
但那份静态分析把 `GameContext::vf_a4` 读错了（它在 `[ctx+0x384]==0` 时
返回 **0x7fffffff** 而不是 0），所以结论需要现场数据来定案。

`Character::Die()` = 角色类虚表槽 55（偏移 **0xDC**）= `0x4ffbb7`，
是一个横跨 `0x4ffbb7`..`0x501a60+` 的大函数。它做的事：

    0x4ffbce  [char+0x150] = 0            ★ HP 清零
    0x4ffbd4  [char+0x2b4] = 1            ★ 「已死，等重生」标记
    ...
    0x50195f  if ([char+0x2ac] != [LobbyStage+0x1cc]) -> 别人的角色，走另一条路
    0x501967  if ([LobbyStage+0x1c] == 0)      -> [char+0x2d8] = -1  永不重生
    0x501976  if (ctx->vf_a4(charId) == 0)     -> [char+0x2d8] = -1  永不重生（生命耗尽）
    0x5019a8  [char+0x2d8] = now + 5000/timescale   ★ 5 秒后重生

每帧 `Character::Update` 在 `0x4fe338` 见 `[char+0x2b4]!=0` 就调 `0x4fe78f`：

    0x4fe7b9  descriptor.type == 4 ? 本地重生（不发包） : 发 0x0413 请求服务端
    0x4fe70e  真正决定「现在能不能重生」，任何一条不满足就直接返回：
                [LobbyStage+0x3da] != 0            -> 不重生
                [char+0x2ac] != [LobbyStage+0x1cc] -> 不重生（不是我的角色）
                [GameContext+0x04] != 0            -> 不重生
                [char+0x2d8] < 0                   -> 不重生
                now < [char+0x2d8]                 -> 还在等 5 秒

本探针把上面每一个判据都读出来，死一次就能看出卡在哪一条。

用法：
    python tools/probe_death.py <pid>                 # 打一次快照
    python tools/probe_death.py <pid> 120             # 盯 120 秒，只在变化时打印
    python tools/probe_death.py <pid> 120 0.2         # 自定采样间隔

★ 窗口不在前台时主循环基本不跑，读到的值会「冻住」，别据此下结论。
"""
import ctypes as C
import json
import os
import sys
import time
from ctypes import wintypes as W

k32 = C.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.restype = W.HANDLE
k32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
k32.ReadProcessMemory.argtypes = [W.HANDLE, W.LPCVOID, W.LPVOID,
                                  C.c_size_t, C.POINTER(C.c_size_t)]

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400

APP = 0x72E2A4
LOBBY_STAGE = 0x72E29C
WORLD = 0x72E2D4
GAME_CONTEXT = 0x72E2DC
#: 0x4fe78f 往 +0x48 写 byte 的那个对象；0x4f57fe/0x4f581f/0x4f579b 也吃它
RESPAWN_UI = 0x72E2EC
#: 0x409fdd = [[[0x72e2b4]+8]+0xd4]，游戏内部时钟（毫秒，受 timescale 影响）
CLOCK_ROOT = 0x72E2B4
#: 0x5019a8 用的除数：真实毫秒 / 它 = 游戏毫秒
TIME_SCALE = 0x6DC528

SEAT_COUNT = 6
OBJ_BASE = 0x1D0

_VFT_NAMES = {}


def load_vftables():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "re", "vftables.json")
    try:
        with open(path, encoding="utf-8") as f:
            for k, v in json.load(f).items():
                _VFT_NAMES[int(k, 16)] = v
    except OSError:
        pass


def read(h, addr, n):
    buf = (C.c_ubyte * n)()
    got = C.c_size_t(0)
    if not addr or not k32.ReadProcessMemory(h, C.c_void_p(addr), buf, n,
                                             C.byref(got)):
        return None
    return bytes(buf[:got.value])


def u32(h, addr):
    raw = read(h, addr, 4)
    return None if raw is None else int.from_bytes(raw, "little")


def i32(h, addr):
    raw = read(h, addr, 4)
    return None if raw is None else int.from_bytes(raw, "little", signed=True)


def u8(h, addr):
    raw = read(h, addr, 1)
    return None if raw is None else raw[0]


def f32(h, addr):
    raw = read(h, addr, 4)
    if raw is None:
        return None
    return C.cast(C.pointer(C.c_uint32(int.from_bytes(raw, "little"))),
                  C.POINTER(C.c_float)).contents.value


def cls_of(h, obj):
    """按对象头部的 vftable 指针认类名。"""
    if not obj:
        return "NULL"
    vft = u32(h, obj)
    if vft is None:
        return "<读不到>"
    return _VFT_NAMES.get(vft, f"vft={vft:08x}")


def game_clock(h):
    """0x409fdd：游戏内部时钟。读不到就返回 None。"""
    root = u32(h, CLOCK_ROOT)
    if not root:
        return None
    obj = u32(h, root + 8)
    if not obj:
        return None
    return u32(h, obj + 0xD4)


def snapshot(h):
    s = {}
    lobby = u32(h, LOBBY_STAGE) or 0
    ctx = u32(h, GAME_CONTEXT) or 0
    s["lobby"] = lobby
    s["ctx"] = ctx
    s["ctx_cls"] = cls_of(h, ctx)
    s["now"] = game_clock(h)
    s["timescale"] = u32(h, TIME_SCALE)

    # ---- 三个「全局否决项」 --------------------------------------------
    s["desc_type"] = i32(h, lobby + 0x1C) if lobby else None      # 2 = 闯关
    s["my_id"] = i32(h, lobby + 0x1CC) if lobby else None
    s["lobby_3da"] = u8(h, lobby + 0x3DA) if lobby else None      # !=0 -> 不重生
    s["ctx_04"] = u8(h, ctx + 0x04) if ctx else None              # !=0 -> 不重生

    # ---- 生命管理对象（ctx->vf_a4 的真身）------------------------------
    rule = u32(h, ctx + 0x384) if ctx else None
    s["rule"] = rule
    s["rule_cls"] = cls_of(h, rule) if rule else "NULL(=> vf_a4 返回 0x7fffffff)"
    # `QuestVictoryCondition` 的每座位战绩表。布局从构造函数 0x55e018 和
    # 剩余生命函数 0x55e0a3 逐指令读出，**六个座位、步长 0x2c**：
    #   [rule + seat*0x2c + 0x54] 座位有人（byte）  [+0x5c] 分数  [+0x60] 死亡次数
    #   [rule + seat*0x2c + 0x64] 最大生命（构造时给有人的座位填 3）
    #   [rule + seat*0x2c + 0x70] 队伍号（0 = 没分队）
    #   [rule + seat*0x2c + 0x7c] 0x0415 下发的分数
    #
    # ★ 会话 31 这里写的是 `[rule + seat*4 + 0x198]`，**错的** —— 那是
    #   `DeathMatchVictoryCondition` 的「分数上限」标量（`0x55be71`），
    #   在生存局里读出来是一堆垃圾，害得 bug调查/8_2 第一轮判断跑偏。
    #
    # 剩余生命 = vf10（`0x55e0a3`）= vf_c(座位) - 死亡次数，下界 0。
    # 生存模式的 vf_c 是 `0x55db69`：`push 3 / pop eax / ret 4` ——
    # **恒等于 3，根本不读 +0x64**。所以判「还有没有命」只能看死亡次数。
    s["stats"] = []
    if rule:
        survival = s["rule_cls"].startswith("Survival")
        for i in range(SEAT_COUNT):
            rec = rule + i * 0x2C
            deaths = i32(h, rec + 0x60)
            maxlives = i32(h, rec + 0x64)
            occupied = u8(h, rec + 0x54)
            if deaths is None or maxlives is None:
                continue
            if not occupied and not maxlives and not deaths:
                continue
            effective = 3 if survival else maxlives
            s["stats"].append({
                "seat": i, "score": i32(h, rec + 0x5C), "deaths": deaths,
                "team": i32(h, rec + 0x70), "score415": i32(h, rec + 0x7C),
                "occupied": occupied,
                "max_lives": maxlives, "effective_max": effective,
                "lives": max(0, effective - deaths),
            })
    # GameContextQuest 的每座位分数（只有 0x0415 下发时才会被写，0x4a3f1e）
    # ★ 只有 GameContextQuest* 才有这个字段；别的上下文那个偏移是别的东西，
    #   读出来是一堆浮点垃圾，会误导人。
    s["ctx_scores"] = ([i32(h, ctx + 0x3B8 + i * 4) for i in range(SEAT_COUNT)]
                       if ctx and s["ctx_cls"].startswith("GameContextQuest") else [])

    # ---- 我的角色 ------------------------------------------------------
    chars = []
    for i in range(SEAT_COUNT):
        obj = u32(h, lobby + OBJ_BASE + i * 4) if lobby else None
        if not obj:
            continue
        chars.append((i, obj))
    s["chars"] = []
    for seat, obj in chars:
        s["chars"].append({
            "seat": seat,
            "obj": obj,
            "cls": cls_of(h, obj),
            "hp": i32(h, obj + 0x150),
            # [char+0xd0] = World 里的对象句柄，0x0408 的第一个字段就是它。
            # `gs_ctl.py kill <handle>` 要用这个值。
            "handle": u32(h, obj + 0xD0),
            "id": i32(h, obj + 0x2AC),
            "spawn_idx": i32(h, obj + 0x2B0),
            "dead": u8(h, obj + 0x2B4),
            "b5": u8(h, obj + 0x2B5),
            "b6": u8(h, obj + 0x2B6),
            "respawn_at": i32(h, obj + 0x2D8),
            "f614": i32(h, obj + 0x614),
            # [char+0x34]/[+0x38] 就是 OnHpZero 里当作 float X/Y 传给 vf_c0 的那对。
            "x": f32(h, obj + 0x34),
            "y": f32(h, obj + 0x38),
        })
    return s


def key(s):
    """只在这些字段变化时才打印，避免刷屏。"""
    me = [(c["seat"], c["hp"], c["dead"], c["respawn_at"], c["spawn_idx"])
          for c in s["chars"]]
    stats = [(t["seat"], t["score"], t["deaths"], t["lives"], t["score415"])
             for t in s["stats"]]
    return (s["ctx_cls"], s["desc_type"], s["lobby_3da"], s["ctx_04"],
            s["rule"], tuple(me), tuple(stats), tuple(s["ctx_scores"]))


def verdict(s):
    """按 0x4fe70e 的判据逐条给结论。"""
    out = []
    # ★ 先看最要命的那条（bug调查/8_2 §212）：队伍号 > 2 会让客户端把队伍
    #   记账写进别人的战绩，死亡次数被越写越大 -> 剩余生命归零 ->
    #   活着被切进观战、死了 respawn_at = -1 永不重生。
    bad_team = [t for t in s["stats"] if t["team"] > 2]
    if bad_team:
        seats = ", ".join(f"座位{t['seat']}=队伍{t['team']}" for t in bad_team)
        out.append(f"✗✗ 队伍号越界：{seats} —— 服务端发了 >= 3 的队伍号，"
                   "客户端 vf34(0x55c696) 会踩掉别人的战绩记录"
                   "（bug调查/8_2 §212；修好的服务端个人战一律发 0）")
    dead_broke = [t for t in s["stats"] if t["occupied"] and t["lives"] <= 0]
    if dead_broke:
        seats = ", ".join(f"座位{t['seat']}(死亡{t['deaths']}次)"
                          for t in dead_broke)
        out.append(f"· 剩余生命已归零：{seats} —— 这些座位不会再重生"
                   "（生存模式恒定三条命，`0x55db69`）")
    if s["lobby_3da"]:
        out.append(f"✗ [LobbyStage+0x3da]={s['lobby_3da']} != 0 -> 0x4fe70e 直接返回")
    if s["ctx_04"]:
        out.append(f"✗ [GameContext+0x04]={s['ctx_04']} != 0 -> 0x4fe70e 直接返回")
    if s["desc_type"] == 0:
        out.append("✗ descriptor.type == 0 -> Die() 里就把 respawn_at 写成 -1")
    for c in s["chars"]:
        if c["id"] != s["my_id"]:
            continue
        if not c["dead"]:
            out.append(f"· 座位 {c['seat']} 还活着 (HP={c['hp']})")
            continue
        out.append(f"★ 座位 {c['seat']} 处于死亡态 (HP={c['hp']}, +0x2b4=1)")
        if c["f614"]:
            # bug调查/8 记的那道守卫：`0x4fe78f` 的发送链上有一处
            # `[char+0x614] != 0 -> 不发 0x0413`（`0x0419` 复活时会清零）。
            # **还没在现场确认过**，所以这里只把值亮出来，不下断言。
            out.append(f"  ? [char+0x614]={c['f614']} 非 0"
                       f"（bug调查/8 记的那道守卫，待现场坐实）")
        if c["respawn_at"] is None:
            continue
        if c["respawn_at"] < 0:
            mine = [t for t in s["stats"] if t["seat"] == c["seat"]]
            why = ""
            if mine and mine[0]["lives"] <= 0:
                why = (f"；本座位死亡次数={mine[0]['deaths']} >= "
                       f"{mine[0]['effective_max']} 条命 —— 就是这条"
                       "（`0x501976` 的 `ctx->vf_a4(charId) == 0`）")
            out.append("  ✗ respawn_at = -1 -> 永不重生"
                       "（Die() 判定 type==0 或生命耗尽，或已经发过一次重生）"
                       + why)
        elif s["now"] is not None:
            left = c["respawn_at"] - s["now"]
            out.append(f"  · respawn_at={c['respawn_at']} now={s['now']} "
                       f"还差 {left} ms")
            if left <= 0:
                out.append(f"  · 应该已经在发 0x0413 了（spawn_idx={c['spawn_idx']}）"
                           if c["spawn_idx"] != -1 else
                           "  ✗ spawn_idx 仍是 -1 -> 0x4fe78f 在 0x4fe81a 直接返回")
    return out


def dump(s):
    print(f"  ctx={s['ctx']:08x} {s['ctx_cls']}  desc.type={s['desc_type']} "
          f"my_id={s['my_id']}  now={s['now']} timescale={s['timescale']}")
    print(f"  [LobbyStage+0x3da]={s['lobby_3da']}  [GameContext+0x04]={s['ctx_04']}"
          f"  [GameContext+0x384]={s['rule'] and f'{s['rule']:08x}'} "
          f"{s['rule_cls']}")
    for c in s["chars"]:
        print(f"  座位{c['seat']} obj={c['obj']:08x} {c['cls']:<14} "
              f"handle={c['handle']:08x} "
              f"HP={c['hp']:<6} id={c['id']} dead={c['dead']} "
              f"spawn_idx={c['spawn_idx']} respawn_at={c['respawn_at']} "
              f"+0x614={c['f614']} pos=({c['x']:.0f},{c['y']:.0f})")
    for t in s["stats"]:
        # 队伍号 > 2 = 服务端发了越界的队伍号，客户端 vf34（0x55c696）会拿
        # `this + 40*(队伍号-1)` 写出两格的队伍数组、踩进别人的战绩记录
        # —— bug调查/8_2 §212 的现场判据，一眼就能认出来。
        flag = "  ★队伍号越界(>2)：会踩别人的战绩" if t["team"] > 2 else ""
        print(f"  战绩[{t['seat']}] 分数={t['score']:<6} 死亡={t['deaths']} "
              f"最大生命={t['effective_max']}(存={t['max_lives']}) "
              f"-> 剩余生命={t['lives']}  "
              f"队伍={t['team']} 0x0415分数={t['score415']}{flag}")
    if any(s["ctx_scores"]):
        print(f"  GameContext+0x3b8 每座位分数（只有 0x0415 会写）= {s['ctx_scores']}")
    for line in verdict(s):
        print("  " + line)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    load_vftables()
    pid = int(sys.argv[1])
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
    interval = float(sys.argv[3]) if len(sys.argv) > 3 else 0.25

    h = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        print(f"OpenProcess 失败：{C.get_last_error()}")
        return 1

    s = snapshot(h)
    print(f"[{time.strftime('%H:%M:%S')}] 快照")
    dump(s)
    if secs <= 0:
        return 0

    last = key(s)
    end = time.time() + secs
    while time.time() < end:
        time.sleep(interval)
        s = snapshot(h)
        k = key(s)
        if k != last:
            last = k
            print(f"[{time.strftime('%H:%M:%S')}] 变化")
            dump(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
