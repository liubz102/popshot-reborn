#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chrprops.py —— 从原版 `Data/ChrProps.ini` 离线提取**角色属性表**（V0.3 M5）。

    python tools\\chrprops.py                # 提取到 server\\bot_chrprops.json
    python tools\\chrprops.py --dump 2       # 顺便打印某几个角色，人工核对

## 为什么要提取（D42）

服务端得**自己判命中**（D28：bot 没有本机，没有一台客户端会替它算爆炸），
而判命中要知道「人有多大」。原版把它写在这个 ini 里，三个圆：

    ChrSizeLegs        腿的碰撞圆半径      ← 打中它算 `LegsDamage`
    ChrSizeBody        身体的碰撞圆半径    ← 打中它算 `Damage`
    ChrSizeHead        头的碰撞圆半径      ← 打中它算 `HeadDamage`
    ChrSizeLegsCrouch  蹲下时腿那个圆的半径

★ 三个圆**从脚往上依次相切**这件事是本工程的模型，不是 ini 里写的
（见 `server/chrprops.py` 的说明和那里的自检）—— 依据是「三个圆叠起来的
总高 ≈ `DisplayHeight`」在 17 个角色上全部成立。

顺带把**冲刺攻击**（双击左右方向键的近身攻击，§64）的参数一起带走：
`DashNN-Damage` / `-SpCost` / `-DamageSize` / `-TotalFrame` …

## 服务端为什么不直接读 `ChrProps.ini`

服务端包里**没有** `Pack_decrypt/` —— 那是 368 MB 客户端安装包解出来的资源，
云端根本没有这个文件。和地形（D19）/ 武器表（D29）同一个道理。

## 产物

`server/bot_chrprops.json`（约 10 KB，进 git、进两个发布包）：

    characters   {角色id(str): {hp, sp, speed, display_height,
                                size_head/body/legs/legs_crouch,
                                weapon_base, dashes: {"0": {...}, …}}}

读它的是 `server/chrprops.py`（只用标准库，CPython 3.8 也能跑）。
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: 产物格式版本。加/改字段时 +1，加载器拿它判「这份产物是不是我认识的」。
#: ★ 2（会话 19）：加了 `game` 段（`GameProps.ini` 的体力常量）。
FORMAT = 2

#: `GameProps.ini` 里和**体力**有关的常量 —— 冲刺攻击要花体力，
#: 而「花多少、回多快」这两个数原版都写在那儿，一个都不用自己编。
_GAME_FIELDS = (
    ("SpMax",          "sp_max",            float),
    ("SpCharging",     "sp_charging",       float),   # 每 tick 回多少
    ("FastRunSpCost",  "fast_run_sp_cost",  float),   # 冲刺跑每 tick 花多少
    ("GuardSpCost",    "guard_sp_cost",     float),
    ("AssultSpCost",   "assault_sp_cost",   float),
)

#: 节名就是 0..16 的序号，**不是角色 id** —— 角色 id 在 `ChrIndex` 里
#: （节 `[3]` 的 `ChrIndex` 是 100，节 `[14]` 的是 3）。
_SECTION = re.compile(r"^\d+$")

#: `DashNN-键名` / `JabNN-键名`。
_MOVE_KEY = re.compile(r"^(Dash|Jab)(\d+)-(.+)$", re.IGNORECASE)

#: 角色级字段：`ini 里的键 -> (产物里的键, 转换函数)`。
_FIELDS = (
    ("ChrIndex",          "id",                int),
    ("ChrName",           "name",              str),
    ("ChrHp",             "hp",                int),
    ("ChrSp",             "sp",                int),
    ("ChrSpeed",          "speed",             float),
    ("DisplayHeight",     "display_height",    float),
    ("ChrSizeHead",       "size_head",         float),
    ("ChrSizeBody",       "size_body",         float),
    ("ChrSizeLegs",       "size_legs",         float),
    ("ChrSizeLegsCrouch", "size_legs_crouch",  float),
    ("WeaponIdxBase",     "weapon_base",       int),
)

#: 冲刺 / 近身攻击的字段（`DashNN-` / `JabNN-` 后面那一截）。
_MOVE_FIELDS = (
    ("Damage",         "damage",       float),
    ("SpCost",         "sp_cost",      float),
    ("DamageSize",     "damage_size",  float),
    #: ★ 有 `DamagingObjBone` 的招式（角色 0 的 `Dash00` 就是）**没有**
    #: `DamageSize`，伤害圈跟着骨骼走、半径写在这一格里。
    ("DamagingObjSize", "obj_size",    float),
    ("CastEndFrame",   "cast_end",     int),
    ("DamageEndFrame", "damage_end",   int),
    ("TotalFrame",     "total_frame",  int),
    ("MoveFrame",      "move_frame",   int),
    ("Move",           "move",         float),
    ("MoveGamma",      "move_gamma",   float),
    ("PosDeltaInitX",  "delta_x",      float),
    ("PosDeltaInitY",  "delta_y",      float),
    ("StartDegree",    "start_degree", float),
    ("MaxDegree",      "max_degree",   float),
    ("MultiForX",      "multi_x",      float),
    ("MultiForY",      "multi_y",      float),
    ("DontPush",       "dont_push",    int),
)


class ChrPropsError(Exception):
    pass


def read_ini(path):
    """读 `ChrProps.ini`，返回 `OrderedDict{节名: {键: 值}}`。

    ★ 这个文件**没有 BOM**，正文是韩文 —— 按 CP949 读；读不动退 CP936。
    键名和数值全是 ASCII，所以就算角色名解码歪了也不影响提取。
    ★ 手写解析而不是 `configparser`：原版里 `#` 开头的注释满天飞，
    还有 `Dash05-PosDeltaaInitY`（原版自己拼错的键）这种东西。
    """
    with open(path, "rb") as fp:
        raw = fp.read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16", "replace")
    elif raw[:3] == b"\xef\xbb\xbf":
        text = raw.decode("utf-8-sig", "replace")
    else:
        try:
            text = raw.decode("cp949")
        except (UnicodeDecodeError, LookupError):
            text = raw.decode("cp936", "replace")

    sections = collections.OrderedDict()
    current = None
    for line in text.splitlines():
        # ★ 这个 ini 用 `#` 当注释，而且**行尾注释**也用它
        #   （`Dash00-Move=9.0    # 이동력`）—— 先截掉。
        line = line.split("#", 1)[0].strip().lstrip("\ufeff")
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            sections[current] = collections.OrderedDict()
            continue
        if "=" not in line:
            continue
        if current is None:
            # ★ `GameProps.ini` **整个文件一个节都没有**（全是裸的 key=value）。
            #   给它一个空节名，调用方（`build_game`）把所有节 flatten 掉。
            current = ""
            sections.setdefault(current, collections.OrderedDict())
        key, value = line.split("=", 1)
        sections[current][key.strip()] = value.strip()
    if not sections:
        raise ChrPropsError("%s 里一个键都没有" % path)
    return sections


def _say(text):
    """打印一行。★ 角色名是韩文，而这台机器的控制台是 CP936 —— 直接
    `print()` 会 `UnicodeEncodeError` 把整个提取脚本带走。转不出去的字符
    换成 `?`，产物里的名字**不受影响**（那是 UTF-8 的 json）。"""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(encoding, "replace").decode(encoding, "replace"))


def _num(text, cast):
    """ini 里的值 -> 数字；转不动返回 `None`（当这个字段不存在）。"""
    try:
        if cast is str:
            return text
        return cast(float(text))
    except (TypeError, ValueError):
        return None


def build_character(fields):
    """一个节 -> 一条角色记录；没有 `ChrIndex` 的返回 `None`。"""
    record = collections.OrderedDict()
    for key, name, cast in _FIELDS:
        value = _num(fields.get(key), cast)
        if value is not None:
            record[name] = value
    if "id" not in record:
        return None
    moves = collections.OrderedDict()
    for key, value in fields.items():
        matched = _MOVE_KEY.match(key)
        if matched is None:
            continue
        kind, index, tail = matched.group(1).lower(), matched.group(2), matched.group(3)
        for ini_key, name, cast in _MOVE_FIELDS:
            if tail.lower() != ini_key.lower():
                continue
            number = _num(value, cast)
            if number is None:
                continue
            moves.setdefault("%s%s" % (kind, int(index)),
                             collections.OrderedDict())[name] = number
            break
    if moves:
        record["moves"] = moves
    return record


def build_table(sections):
    """全部节 -> `{角色id(str): 记录}`。重复 id 后面的覆盖前面的。"""
    out = collections.OrderedDict()
    for name, fields in sections.items():
        if not _SECTION.match(name):
            continue
        record = build_character(fields)
        if record is None:
            continue
        out[str(record["id"])] = record
    return out


def build_game(sections):
    """`GameProps.ini` 的体力常量 -> 一个小字典。文件不在就返回 `{}`。"""
    flat = {}
    for fields in sections.values():
        flat.update(fields)
    out = collections.OrderedDict()
    for key, name, cast in _GAME_FIELDS:
        value = _num(flat.get(key), cast)
        if value is not None:
            out[name] = value
    return out


def find_ini(explicit=None, name="ChrProps.ini"):
    """找 `Pack_decrypt/Data/<name>`（和 `weapondata.py` 同一套口径）。

    `explicit` 只对 `ChrProps.ini` 有意义；别的文件按同一个目录去找。
    找不到时 `required=False` 的调用方自己判 `None`。
    """
    candidates = []
    if explicit:
        candidates.append(explicit)
        candidates.append(os.path.join(os.path.dirname(explicit), name))
    candidates.append(os.path.join(ROOT, "Pack_decrypt", "Data", name))
    candidates.append(os.path.abspath(os.path.join(
        ROOT, "..", "..", "main", "Pack_decrypt", "Data", name)))
    for cand in candidates:
        if os.path.isfile(cand) and os.path.basename(cand).lower() == name.lower():
            return cand
    return None


def require_ini(explicit=None):
    path = find_ini(explicit, "ChrProps.ini")
    if path is not None:
        return path
    raise SystemExit(
        "找不到 ChrProps.ini。用 --ini 指定，例如"
        " --ini D:\\git\\popshot-reborn\\main\\Pack_decrypt\\Data\\ChrProps.ini")


def main(argv=None):
    ap = argparse.ArgumentParser(description="从原版 ChrProps.ini 提取角色属性表")
    ap.add_argument("--ini", help="ChrProps.ini 的路径")
    ap.add_argument("--out", help="输出文件（默认 server\\bot_chrprops.json）")
    ap.add_argument("--dump", nargs="*", metavar="角色id",
                    help="打印这几个角色；不给就全打印")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    path = require_ini(args.ini)
    out_path = args.out or os.path.join(ROOT, "server", "bot_chrprops.json")
    try:
        sections = read_ini(path)
    except ChrPropsError as exc:
        raise SystemExit("解析 %s 失败：%s" % (path, exc))
    characters = build_table(sections)
    if not characters:
        raise SystemExit("%s 里一个带 ChrIndex 的角色都没有" % path)

    # ★ 体力常量在**另一个** ini 里（`GameProps.ini`）。它和角色表一起用
    #   —— 冲刺攻击花 `DashNN-SpCost`、回复速度是 `SpCharging` ——
    #   所以塞进同一份产物，省一个文件、少一处「忘了拷进包」。
    game_path = find_ini(args.ini, "GameProps.ini")
    game = build_game(read_ini(game_path)) if game_path else {}

    table = collections.OrderedDict((
        ("format", FORMAT),
        ("source", os.path.basename(path)),
        ("game", game),
        ("count", len(characters)),
        ("characters", characters),
    ))
    out_dir = os.path.dirname(out_path)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    # ★ `newline="\n"`：铁律 3 —— `.json` 一律 LF 无 BOM（服务端包跑 Linux）。
    with open(out_path, "w", encoding="utf-8", newline="\n") as fp:
        json.dump(table, fp, ensure_ascii=False, separators=(",", ":"))
        fp.write("\n")

    if args.dump is not None:
        wanted = args.dump or sorted(characters, key=lambda k: int(k))
        for cid in wanted:
            record = characters.get(str(cid))
            _say("--- 角色 %s ---" % cid)
            if record is None:
                _say("    表里没有这个角色")
                continue
            for key, value in record.items():
                if key == "moves":
                    _say("    %-18s %s" % (key, ", ".join(sorted(value))))
                else:
                    _say("    %-18s %s" % (key, value))

    if not args.quiet:
        size = os.path.getsize(out_path)
        print("完成：%d 个角色 -> %s（%.1f KB）"
              % (len(characters), out_path, size / 1024.0))
        bad = [c for c, r in characters.items()
               if not all(k in r for k in
                          ("size_head", "size_body", "size_legs"))]
        if bad:
            print("   ⚠ 这些角色缺碰撞圆尺寸，命中判定会退回默认值：%s"
                  % ", ".join(sorted(bad, key=int)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
