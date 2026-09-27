#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mutudata.py —— 格斗模式（무투전）招式表的离线提取器（X_Mod · X16 · B1）。

    python tools\\mutudata.py                # 提取到 server\\bot_mutu.json
    python tools\\mutudata.py --dump 0       # 顺便打印某几个角色的 10 招，人工核对

## 为什么要提取（D84）

格斗招式打没打中**只有出招者本机判**（X_Mod §121）—— bot 没有本机，没有任何一台客户端会替它判，
服务端得自己判。而判定体（Damager）是**挂在骨骼上的**：每个逻辑帧瞬移到那根骨头的位置，
所以得先把每招每个判定体「第 k 个逻辑帧在角色脚底前后多远」烘出来。服务端包里没有 540 MB 的明文
资源树、也不带 numpy，所以照地形（D19）/ 角色属性（D42）的老路：离线提取成 JSON，服务端只读 JSON。

## 客户端怎么算的（§121，🔍逐指令）

- 招式表 `Data/NewMutu.ini`（**CP949**，160 节 = 16 角色 × 10 招）。招式号 = 该 ChrIdx 在文件里的**出现顺序**。
- 动作 `Models/Characters/chNN/chNN@<Motion>.mtn`，时长 D（秒）；采样数 `N = 2·(⌊30D+0.1⌋+1)`；
  引擎帧 `f = ⌊min(t·(N−1)/D, N−1)⌋`，t = 动画秒数 = 实时 × MotionFrameRate（顿帧时停走）。
- Damager 的起止帧 n → `2n−2`（不小于 0，`0x4f8e02`）；Damagee / Move 的帧号原样。结束帧为 0 = 到动作结束（`N`）。
- 判定体存在于 `起始 ≤ f ≤ 结束`；位置 = 骨头世界矩阵上的点（+ BoneOffset，160 节都没写）→ 投影 → + 角色坐标
  + (OffsetX × 朝向, OffsetY)。半径 = `Size`（缺省 5）。
- 招式位移（`0x4f8294`）：`S ≤ f < E` 时每逻辑帧走 `trunc(Dist·pow((f−S+1)/(E−S), 1/γ)) − trunc(Dist·pow(prev/(E−S), 1/γ))`。
- 出招吃 `DamagerCount + DamageeCount + 1` 个弹体句柄（多出来那 1 个是推挤体）。

## 坐标换算（🤔 待 B2 实机探针核对）

动作里角色**朝 −Z、Y 朝上**（泰尔右直拳 `Bip01_R_Forearm` 从 z≈1 伸到 z≈−31、高 y≈47.5），
按「1 场景单位 = 1 px、模型原点 = 角色坐标（脚底）」换成**朝右时**的屏幕偏移：`dx = −z`、`dy = −y`；
朝左时 dx 取反。没应用 `ChrSpineScale`（多数角色 0.9）—— 客户端算判定体位置时带不带它，要 B2 核。
换算常量写在产物的 `units` 里，核完改这里重跑即可。

## 产物

`server/bot_mutu.json`（进 git、进两个发布包）：

    format / logical_frame_ms / units / config（NewMutuConfig.ini）/ guard（GameProps.ini）
    characters   {ChrIdx(str): [招式 × 10（文件顺序）]}
        name / index / motion / rate / rate_raw / duration / samples(N) / timer_ms / frames_total
        damage / kind(2/3) / skill_type(1 站 2 蹲 3 空中 0 不可用) / prev(-1 无 / -2 找不到) / input
        handles / move{dist,gamma,start,end} / move_track[[k,步长px], …]（朝右）
        damagers / damagees: [{bone, size, start, end, offset[x,y], bone_missing, track[[k,f,dx,dy], …]}]

读它的是 `server/mutudata.py`（只用标准库，CPython 3.8 也能跑）。
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: 产物格式版本。改了布局就 +1，`server/mutudata.py` 会拒绝不认识的版本。
FORMAT = 1

#: 客户端逻辑帧（X_Mod §81：固定 32 ms 网格）。判定体每个逻辑帧摆一次、判一次。
LOGICAL_FRAME_MS = 32

#: ★ 坐标换算（见模块说明「坐标换算」，🤔 待 B2 核）。
PX_PER_UNIT = 1.0

#: `NewMutu.ini` 的 `SkillType`（`0x495cba`）：1 站 / 2 蹲 / 3 空中，其它 0 = 不可用。
SKILL_TYPES = {"Stand": 1, "Crouch": 2, "Jump": 3}

#: 招式伤害类型（`0x4f9c82`）：`BounceHit` 非 0 → 3（打飞），否则 2（滑退）。
KIND_SLIDE = 2
KIND_FLY = 3

#: 判定体没写 `Size` 时客户端的缺省 5（§121）。
DEFAULT_SIZE = 5.0
#: `Damage` 缺省 10（`0x4f8b6a`）、`MoveDist` 缺省 10、`MoveGamma` 缺省 5（§121）。
DEFAULT_DAMAGE = 10.0
DEFAULT_MOVE_DIST = 10.0
DEFAULT_MOVE_GAMMA = 5.0

#: 从 `NewMutuConfig.ini` 原样带走的键（§120 / §122）。
CONFIG_KEYS = ("GravityFactor", "FirstJumpHeight", "SecondJumpHeight",
               "DamagedFlyGravityFactor", "DamageBounceVectorMultiplierX",
               "DamageBounceVectorMultiplierY")
#: 从 `GameProps.ini` 带走的格挡参数（§122）。
GUARD_KEYS = (("GuardSpCost", "sp_cost"), ("GuardDamageRate", "damage_rate"))


def _f32(x):
    """按 f32 截一下 —— 客户端这一路的中间量都先存成 f32 再进 `pow`。"""
    return struct.unpack("<f", struct.pack("<f", x))[0]


_MTNTOOL = []


def _mtntool():
    """按文件路径加载同目录的 `mtntool.py`（要 numpy，只在提取时用）。

    ★ 不往 `sys.path` 里塞 `tools\\`：`tools\\` 和 `server\\` 有同名模块（mapdata / chrprops …），
      测试进程里塞了会把别的测试带歪。
    """
    if not _MTNTOOL:
        import importlib.util
        spec = importlib.util.spec_from_file_location("tools_mtntool_x16", os.path.join(HERE, "mtntool.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MTNTOOL.append(mod)
    return _MTNTOOL[0]


def read_ini(path):
    """读客户端的 ini：返回 `[(节名 或 None, OrderedDict(键 -> 值)), …]`（按文件顺序）。

    编码按 BOM 嗅：UTF-16（带 BOM）/ UTF-8（带 BOM）/ 否则 **CP949**（`NewMutu.ini` 就是）。
    `#` 开头的行是注释；值里 `#` 之后的也砍掉（原版有「值 \\t\\t# 说明」这种写法）。
    """
    with open(path, "rb") as fp:
        raw = fp.read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16")
    elif raw[:3] == b"\xef\xbb\xbf":
        text = raw[3:].decode("utf-8")
    else:
        text = raw.decode("cp949")
    sections = [(None, collections.OrderedDict())]
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]") and "=" not in line:
            sections.append((line[1:-1].strip(), collections.OrderedDict()))
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        sections[-1][1][key.strip()] = value.split("#", 1)[0].strip()
    return sections


def _num(entry, key, default):
    value = entry.get(key, "")
    try:
        return float(value) if value != "" else float(default)
    except ValueError:
        return float(default)


def _int(entry, key, default=0):
    return int(_num(entry, key, default))


def develop_root():
    """明文资源树 `game_patched\\Pack_develop`（目录名只在 `server/config.py` 里定一次）。"""
    server_dir = os.path.join(ROOT, "server")
    if server_dir not in sys.path:
        sys.path.append(server_dir)
    import config
    return os.path.join(ROOT, "game_patched", config.PACK_DEVELOP_DIR)


def motion_path(pack, chr_idx, motion):
    """`Models/Characters/chNN/chNN@<Motion>.mtn`（`ch%02d`：0 → ch00，3 → ch03，100 → ch100）。"""
    folder = "ch%02d" % chr_idx
    return os.path.join(pack, "Models", "Characters", folder, "%s@%s.mtn" % (folder, motion))


def sample_count(duration):
    """动作的采样数 N = 2·(⌊30D+0.1⌋+1)（§121，🤔 B2 按探针打出的 f 核）。"""
    return 2 * (int(math.floor(30.0 * duration + 0.1)) + 1)


def engine_frame(t, duration, samples):
    """动画时刻 t（秒）落在第几个引擎帧：⌊min(t·(N−1)/D, N−1)⌋。"""
    if duration <= 0:
        return 0
    return int(math.floor(min(t * (samples - 1) / duration, samples - 1)))


def move_steps(dist, gamma, start, end, frames):
    """每个逻辑帧的位移（朝右，px）：`0x4f8294` 那条曲线逐帧照抄。

    `frames` = 每个逻辑帧的引擎帧 f。`end` 已经把 0 换成了 N。返回 `[[k, 步长], …]`（只收走动的帧）。
    """
    span = end - start
    if span <= 0:
        return []
    inv = _f32(1.0 / _f32(gamma)) if gamma else 0.0
    fspan = _f32(float(span))
    prev = 0
    out = []
    for k, f in enumerate(frames):
        if not (start <= f < end):
            continue
        cur = f - start + 1
        before = int(dist * math.pow(_f32(prev / fspan), inv)) if prev > 0 else 0
        after = int(dist * math.pow(_f32(cur / fspan), inv))
        prev = cur
        if after - before:
            out.append([k, after - before])
    return out


def _bone_point(world, bone, bone_offset):
    """骨头 `bone` 上偏移 `bone_offset`（骨局部）的点在模型空间里的坐标；没有这根骨返回 None。"""
    mat = world.get(bone)
    if mat is None:
        return None
    x, y, z = bone_offset
    return (x * mat[0][0] + y * mat[1][0] + z * mat[2][0] + mat[3][0],
            x * mat[0][1] + y * mat[1][1] + z * mat[2][1] + mat[3][1],
            x * mat[0][2] + y * mat[1][2] + z * mat[2][2] + mat[3][2])


def _hit_objects(entry, prefix, convert_frames, samples, times, frames, pose_at):
    """`DamagerN-…` / `DamageeN-…` 一组判定体。`convert_frames` = 起止帧要不要 2n−2（只有 Damager 要）。"""
    count = _int(entry, prefix + "Count", 0)
    out = []
    for i in range(1, count + 1):
        head = "%s%d-" % (prefix, i)
        start = _int(entry, head + "StartFrame", 0)
        end = _int(entry, head + "EndFrame", 0)
        if convert_frames:
            start, end = max(0, 2 * start - 2), max(0, 2 * end - 2)
        if end == 0:
            end = samples                       # 运行时把 0 改写成 N（`0x4f834f`）
        bone = entry.get(head + "Bone", "")
        bone_offset = (_num(entry, head + "BoneOffsetX", 0), _num(entry, head + "BoneOffsetY", 0),
                       _num(entry, head + "BoneOffsetZ", 0))
        offset = (_num(entry, head + "OffsetX", 0), _num(entry, head + "OffsetY", 0))
        track, missing = [], False
        for k, (t, f) in enumerate(zip(times, frames)):
            if not (start <= f <= end):
                continue
            point = _bone_point(pose_at(t), bone, bone_offset) if bone else None
            if point is None:
                missing = True                  # 骨名拼错 / 没写：客户端退回角色坐标（§119）
                dx, dy = 0.0, 0.0
            else:
                dx, dy = -float(point[2]) * PX_PER_UNIT, -float(point[1]) * PX_PER_UNIT
            track.append([k, f, round(dx + offset[0], 2), round(dy + offset[1], 2)])
        out.append(collections.OrderedDict((
            ("bone", bone), ("size", _num(entry, head + "Size", DEFAULT_SIZE)),
            ("start", start), ("end", end), ("offset", [offset[0], offset[1]]),
            ("bone_missing", missing), ("track", track))))
    return out, count


def build_skill(pack, name, entry, index, mtn_cache):
    """一节 `NewMutu.ini` → 产物里的一招。`mtn_cache` 按路径缓存解析好的动作。"""
    mtntool = _mtntool()

    chr_idx = _int(entry, "ChrIdx", -1)
    motion = entry.get("Motion", "")
    path = motion_path(pack, chr_idx, motion)
    if path not in mtn_cache:
        if not os.path.isfile(path):
            raise SystemExit("%s 要的动作不存在：%s" % (name, path))
        mtn_cache[path] = mtntool.parse(path)
    mtn = mtn_cache[path]
    duration = float(mtn.duration)
    samples = sample_count(duration)
    rate_raw = _num(entry, "MotionFrameRate", 1.0)
    # ★ 速率 0（9 个商城角色的 `MutuStand-K01`）：客户端把速度恢复成上次存的值（初值 1.0），
    #   计时器 ⌊⌊D×1000⌋/0⌋ 溢出成 0x80000000 —— 按 1.0 播、计时器记 None（§119 / §121）。
    rate = rate_raw if rate_raw > 0 else 1.0
    timer_ms = int(int(duration * 1000) / rate_raw) if rate_raw > 0 else None
    step = LOGICAL_FRAME_MS / 1000.0 * rate
    frames_total = int(math.ceil(duration / step - 1e-9)) if step > 0 else 0
    times = [k * step for k in range(frames_total)]
    frames = [engine_frame(t, duration, samples) for t in times]

    poses = {}

    def pose_at(t):
        if t not in poses:
            poses[t] = mtntool.world_mats(mtn, min(t, duration) * mtn.ticks_per_sec)
        return poses[t]

    damagers, n_damagers = _hit_objects(entry, "Damager", True, samples, times, frames, pose_at)
    damagees, n_damagees = _hit_objects(entry, "Damagee", False, samples, times, frames, pose_at)
    move_start = _int(entry, "MoveStartFrame", 0)
    move_end = _int(entry, "MoveEndFrame", 0) or samples
    dist = _num(entry, "MoveDist", DEFAULT_MOVE_DIST)
    gamma = _num(entry, "MoveGamma", DEFAULT_MOVE_GAMMA)
    return collections.OrderedDict((
        ("name", name), ("index", index), ("motion", motion),
        ("rate", rate), ("rate_raw", rate_raw), ("duration", round(duration, 6)),
        ("samples", samples), ("timer_ms", timer_ms), ("frames_total", frames_total),
        ("damage", _num(entry, "Damage", DEFAULT_DAMAGE)),
        ("kind", KIND_FLY if _int(entry, "BounceHit", 0) else KIND_SLIDE),
        ("skill_type", SKILL_TYPES.get(entry.get("SkillType", ""), 0)),
        ("prev", entry.get("PrevSkill", "")),        # build_table 里换成下标
        ("input", entry.get("InputKey", "")),
        ("handles", n_damagers + n_damagees + 1),
        ("move", collections.OrderedDict((("dist", dist), ("gamma", gamma),
                                          ("start", move_start), ("end", move_end)))),
        ("move_track", move_steps(dist, gamma, move_start, move_end, frames)),
        ("damagers", damagers), ("damagees", damagees)))


def build_table(pack):
    """整张表：`{ChrIdx(str): [招式…]}`，外加 `NewMutuConfig.ini` / `GameProps.ini` 那几格。"""
    data = os.path.join(pack, "Data")
    characters = collections.OrderedDict()
    mtn_cache = {}
    for name, entry in read_ini(os.path.join(data, "NewMutu.ini")):
        if name is None:
            continue
        chr_idx = _int(entry, "ChrIdx", -1)
        if chr_idx < 0:
            raise SystemExit("NewMutu.ini [%s] 没写 ChrIdx" % name)
        skills = characters.setdefault(str(chr_idx), [])
        skills.append(build_skill(pack, name, entry, len(skills), mtn_cache))
    for skills in characters.values():
        by_name = {s["name"]: s["index"] for s in skills}
        for s in skills:
            s["prev"] = -1 if not s["prev"] else by_name.get(s["prev"], -2)
    config = collections.OrderedDict()
    cfg = dict(read_ini(os.path.join(data, "NewMutuConfig.ini"))[0][1])
    for key in CONFIG_KEYS:
        config[key] = _num(cfg, key, 0)
    guard = collections.OrderedDict()
    props = {}
    for _name, entry in read_ini(os.path.join(data, "GameProps.ini")):
        props.update(entry)
    for key, out_key in GUARD_KEYS:
        guard[out_key] = _num(props, key, 0)
    return collections.OrderedDict((
        ("format", FORMAT), ("logical_frame_ms", LOGICAL_FRAME_MS),
        ("units", collections.OrderedDict((("px_per_unit", PX_PER_UNIT), ("forward", "-z"),
                                           ("up", "y"), ("spine_scale", False)))),
        ("config", config), ("guard", guard), ("characters", characters)))


def dump(table, chr_ids):
    for cid in chr_ids:
        for s in table["characters"].get(str(cid), []):
            reach = [max((abs(p[2]) for p in d["track"]), default=0) for d in s["damagers"]]
            print("ch%02d #%d %-20s %s 伤害%-4g 类型%d 速率%-4g %d帧 句柄%d prev=%d 判定体%s 最远%s"
                  % (int(cid), s["index"], s["motion"], s["input"], s["damage"], s["kind"],
                     s["rate"], s["frames_total"], s["handles"], s["prev"],
                     [(d["bone"], d["start"], d["end"], len(d["track"])) for d in s["damagers"]],
                     reach))


# ---------------------------------------------------------------------------
# B2：拿 bshook 的判定体探针（`BSHOOK_MUTU_DIAG=1`）逐帧核这张表（X16，临时）
# ---------------------------------------------------------------------------
_CHECK_REC = re.compile(r"MUTU=\s+招式表 ([0-9A-F]{8}) 座位 (-?\d+) MoveDist (-?[\d.]+) γ (-?[\d.]+)"
                        r" 起 (-?\d+) 止 (-?\d+) 判定体 (\d+) 受击体 (\d+)")
_CHECK_FRAME = re.compile(r"MUTU\.\s+座位 (-?\d+) 招式 ([0-9A-F]{8}) 表 ([0-9A-F]{8}) 帧 (-?\d+)"
                          r" 朝 ([+-]?\d+) 脚 \((-?[\d.]+), (-?[\d.]+)\) 判定(.*?) 受击(.*)$")
_CHECK_OBJ = re.compile(r" (\d+):\((-?[\d.]+),(-?[\d.]+)\)")


def _check_candidates(skills, rec):
    """招式表记录 → 这个角色里对得上的招（MoveDist / γ / 起止 / 判定体数 / 受击体数全对上）。"""
    _seat, dist, gamma, start, end, nd, ne = rec
    out = []
    for skill in skills:
        move = skill.get("move") or {}
        if (abs(float(move.get("dist", 0.0)) - dist) < 1e-3
                and abs(float(move.get("gamma", 0.0)) - gamma) < 1e-3
                and int(move.get("start", 0)) == start and int(move.get("end", 0)) == end
                and len(skill.get("damagers", ())) == nd and len(skill.get("damagees", ())) == ne):
            out.append(skill)
    return out


def check(log_path, table_path, char_of_seat, default_char=None, out=sys.stdout):
    """逐帧比：日志里每个判定体 / 受击体相对脚底的偏移 vs 表里同一引擎帧的 (dx·朝向, dy)。返回 0 = 跑完。"""
    with open(table_path, "r", encoding="utf-8") as fp:
        table = json.load(fp)
    recs, frames = {}, []
    with open(log_path, "r", encoding="utf-8", errors="replace") as fp:
        for line in fp:
            m = _CHECK_REC.search(line)
            if m:
                recs[m.group(1)] = (int(m.group(2)), float(m.group(3)), float(m.group(4)),
                                    int(m.group(5)), int(m.group(6)), int(m.group(7)), int(m.group(8)))
                continue
            m = _CHECK_FRAME.search(line)
            if m:
                frames.append((int(m.group(1)), m.group(3), int(m.group(4)), int(m.group(5)),
                               [(int(i), float(x), float(y)) for i, x, y in _CHECK_OBJ.findall(m.group(8))],
                               [(int(i), float(x), float(y)) for i, x, y in _CHECK_OBJ.findall(m.group(9))]))
    print("日志：%d 张招式表、%d 帧" % (len(recs), len(frames)), file=out)
    stats = {}
    unknown = collections.Counter()
    for seat, rec_id, f, facing, dmg, dme in frames:
        rec = recs.get(rec_id)
        char = char_of_seat.get(seat, default_char)
        if rec is None or char is None:
            unknown["没有招式表那一行 / 不知道座位的角色"] += 1
            continue
        skills = table.get("characters", {}).get(str(char), [])
        cands = _check_candidates(skills, rec)
        if not cands:
            unknown["角色 %s 里对不上招式表 %s" % (char, rec_id)] += 1
            continue
        skill = cands[0]
        row = stats.setdefault(skill["name"], {"n": 0, "err": [], "mirror_err": [], "sum_lt": 0.0,
                                               "sum_tt": 0.0, "only_log": 0, "only_table": 0})
        for kind, logged, defs in (("判定", dmg, skill.get("damagers", ())),
                                   ("受击", dme, skill.get("damagees", ()))):
            seen = {}
            for i, x, y in logged:
                seen[i] = (x, y)
            for i, d in enumerate(defs):
                point = next((p for p in d.get("track", ()) if int(p[1]) == f), None)
                got = seen.get(i)
                if point is None and got is None:
                    continue
                if point is None:
                    row["only_log"] += 1
                    continue
                if got is None:
                    row["only_table"] += 1
                    continue
                tx, ty = float(point[2]) * (1 if facing >= 0 else -1), float(point[3])
                row["n"] += 1
                row["err"].append(math.hypot(got[0] - tx, got[1] - ty))
                row["mirror_err"].append(math.hypot(got[0] + tx, got[1] - ty))
                row["sum_lt"] += got[0] * tx + got[1] * ty
                row["sum_tt"] += tx * tx + ty * ty
    for why, n in unknown.most_common():
        print("  跳过 %d 帧：%s" % (n, why), file=out)
    print("%-28s %5s %7s %7s %7s %7s %6s %6s" % ("招式", "点数", "中位差", "最大差", "镜像差", "比例",
                                                "只日志", "只表"), file=out)
    for name in sorted(stats):
        row = stats[name]
        errs = sorted(row["err"])
        mid = errs[len(errs) // 2] if errs else float("nan")
        worst = errs[-1] if errs else float("nan")
        mirror = sorted(row["mirror_err"])
        mmid = mirror[len(mirror) // 2] if mirror else float("nan")
        scale = row["sum_lt"] / row["sum_tt"] if row["sum_tt"] else float("nan")
        print("%-28s %5d %7.2f %7.2f %7.2f %7.3f %6d %6d" % (name, row["n"], mid, worst, mmid, scale,
                                                           row["only_log"], row["only_table"]), file=out)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="从 NewMutu.ini + 动作文件提取格斗招式表")
    ap.add_argument("--pack", help="明文资源树目录（默认 game_patched\\Pack_develop）")
    ap.add_argument("--out", help="输出文件（默认 server\\bot_mutu.json）")
    ap.add_argument("--dump", nargs="*", metavar="ChrIdx", help="打印这几个角色的招式（不给就是 0 1 2 3）")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--check", metavar="LOG", help="B2：拿 bshook 日志里的 MUTU= / MUTU. 两种行逐帧核表（不重新提取）")
    ap.add_argument("--table", help="--check 用哪张表（默认 server\\bot_mutu.json）")
    ap.add_argument("--char", action="append", default=[], metavar="[座位=]角色id",
                    help="--check：座位上是哪个角色（可以给多次；只给一个数就是所有座位）")
    args = ap.parse_args(argv)

    if args.check:
        char_of_seat, default_char = {}, None
        for item in args.char:
            if "=" in item:
                seat, cid = item.split("=", 1)
                char_of_seat[int(seat)] = int(cid)
            else:
                default_char = int(item)
        return check(args.check, args.table or os.path.join(ROOT, "server", "bot_mutu.json"),
                     char_of_seat, default_char)

    pack = args.pack or develop_root()
    table = build_table(pack)
    out_path = args.out or os.path.join(ROOT, "server", "bot_mutu.json")
    # ★ `newline="\n"`：铁律 3 —— `.json` 一律 LF 无 BOM（服务端包跑 Linux）。
    with open(out_path, "w", encoding="utf-8", newline="\n") as fp:
        json.dump(table, fp, ensure_ascii=False, separators=(",", ":"))
        fp.write("\n")
    if args.dump is not None:
        dump(table, args.dump or ["0", "1", "2", "3"])
    if not args.quiet:
        n = sum(len(v) for v in table["characters"].values())
        print("完成：%d 个角色 %d 招 -> %s（%.1f KB）"
              % (len(table["characters"]), n, out_path, os.path.getsize(out_path) / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
