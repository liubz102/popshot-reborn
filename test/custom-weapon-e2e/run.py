#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run.py —— 18 把自定义武器 · 参数端到端验证的**编排器**。

    python run.py --selftest        # 只跑离线部分（不碰客户端）
    python run.py                   # 跑全量 T0~T10（要独占这台机器）
    python run.py --only T1 T2      # 只跑其中几条
    python run.py --restore         # 只做「还原用户原来的 weapons.json」

## 它在验什么

`weapons.json` → `weaponcfg.effective()` → `0x0F01` → bshook 写内存 —— 这条链
只有最后一跳没法用单测覆盖。所以这里**批量设参数 → 真开一局 → 从外部读客户端
内存逐格核对**。判据不是「看起来对不对」，是那条 596 字节记录里的每一格字节。

## 期望值怎么算（★ 三层，别搞混）

| 情况 | 期望值 |
|---|---|
| 这一格**填了** | 就是填进去的那个数（**不经过 `effective()`**，那样会变成自己验自己）|
| 留空、但原版 `weapon.ini` 里有 | 参考值（`weaponcfg.reference()`，来自 `bot_weapons.json`）|
| 留空、`weapon.ini` 里也没有 | **T0 基线**（mask=0 ⇒ hook 写回它自己缓存的 ini 原值）|

T0 基线是「18 把全部清空 + 真进一局」之后读到的整张表，同时它自己也要过一道
独立校验：凡是 `weapon.ini` 里写了的键，内存值必须等于参考值。

## 收工后会把 `weapons.json` 还原回跑之前的样子（备份在 logs/e2e-backup-*）。
"""
import argparse
import functools
import json
import os
import shutil
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
for _p in (os.path.join(ROOT, "server"), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")

#: 输出重定向到文件时 Python 默认全缓冲 —— 那样跑完才吐第一个字节，中途看不到进度，
#: 会被误当成卡死。这里一律逐行刷。
print = functools.partial(print, flush=True)  # noqa: A001

import shopdata           # noqa: E402
import weaponcfg          # noqa: E402

import adminapi           # noqa: E402
import drive              # noqa: E402
import params as paramsets  # noqa: E402
import wtab               # noqa: E402

DUMP_DIR = os.path.join(ROOT, "logs", "e2e-dumps")
DATA_DIR = os.path.join(ROOT, "server", "data")
KEYS = [f["key"] for f in wtab.LAYOUT.fields]
OFFSET_OF = {f["key"]: f["off"] for f in wtab.LAYOUT.fields}
IS_FLOAT = {f["key"]: f["is_float"] for f in wtab.LAYOUT.fields}


def f32(value):
    return struct.unpack("<f", struct.pack("<f", float(value)))[0]


def ammo_ids():
    """{物品id: 武器Id}，现读 shop_items.json。"""
    out = {}
    for item_id in weaponcfg.custom_item_ids():
        item = shopdata.get(item_id)
        if item is not None and item.ammo_id:
            out[int(item_id)] = int(item.ammo_id)
    return out


AMMO_OF = ammo_ids()
CUSTOM_WEAPON_IDS = set(AMMO_OF.values())


# --------------------------------------------------------------------------
# 快照 / 比对
# --------------------------------------------------------------------------

def dump(tag, with_holders=False):
    os.makedirs(DUMP_DIR, exist_ok=True)
    snap = wtab.snapshot(with_holders=with_holders)
    path = os.path.join(DUMP_DIR, "%s.json" % tag)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(snap, fp, ensure_ascii=False, indent=1, sort_keys=True)
    snap["_path"] = path
    return snap


def expected_of(item_id, mode, table, baseline):
    """一把武器在某个模式下、14 格各自应该是多少。见模块头那张三层表。"""
    filled = (table.get(item_id) or {}).get(mode) or {}
    reference = weaponcfg.reference(item_id)
    base = baseline.get(AMMO_OF[item_id], {})
    out = {}
    for key in KEYS:
        if filled.get(key) is not None:
            value = filled[key]
            source = "设定"
        elif key in reference:
            value = reference[key]
            source = "参考"
        else:
            value = base.get(key)
            source = "基线"
        out[key] = (f32(value) if IS_FLOAT[key] and value is not None else value, source)
    return out


def compare(snapshot, table, mode, baseline):
    """逐格比对 18 把 × 14 格。返回 (比了多少格, [不一致的])。"""
    bad, total = [], 0
    for item_id in sorted(AMMO_OF):
        wid = AMMO_OF[item_id]
        rec = snapshot["table"].get(wid)
        if rec is None:
            bad.append({"item": item_id, "weapon": wid, "key": "(整条)",
                        "want": "在表里", "got": "查不到", "source": "-"})
            continue
        want = expected_of(item_id, mode, table, baseline)
        for key in KEYS:
            total += 1
            wanted, source = want[key]
            got = rec[key]
            if wanted is None:
                continue                       # 基线里也没有 ⇒ 没有可比的期望
            if IS_FLOAT[key]:
                ok = f32(got) == f32(wanted)
            else:
                ok = int(got) == int(wanted)
            if not ok:
                bad.append({"item": item_id, "weapon": wid, "key": key,
                            "want": wanted, "got": got, "source": source,
                            "off": "+0x%02x" % OFFSET_OF[key],
                            "type": "f32" if IS_FLOAT[key] else "i32"})
    return total, bad


def compare_untouched(dumps, label):
    """**同一种场景**的多次快照里，原版武器那 14 格必须逐次一模一样。

    ★★ 为什么不拿「一次基线」去比所有快照：**闯关关卡自带武器覆盖 ini**
      （`Data/Quest/QuestNN/weapon-<难度>.ini` —— §44 那条 `0x48adb0` 合并路径读的
      就是它），里面改的是怪物武器的数值 ⇒ **闯关图和对战图的原版记录本来就不一样**。
      拿闯关里取的基线去核对战图，会冒出一堆假阳性：2026-09-20 实测 26 格，
      逐条对上了 `Quest03/weapon-1.ini` 里 `Soldier-Melee` / `Soldier-Pistol` …
      那几个小节 —— **那是原版设计，不是串改。**
    ★ 同场景内逐次比反而**更强**：这几次之间自定义武器的值换过好几套
      （EMPTY / A / B / C / D），原版记录只要被蹭到一格就会露馅。

    `dumps` = [(标签, 快照), …]，至少两份才有意义。
    """
    bad, total = [], 0
    if len(dumps) < 2:
        return 0, []
    (base_tag, base_snap), rest = dumps[0], dumps[1:]
    base = base_snap["table"]
    for tag, snap in rest:
        for wid, rec in sorted(snap["table"].items()):
            if wid in CUSTOM_WEAPON_IDS or wid not in base:
                continue
            for key in KEYS:
                total += 1
                got, want = rec[key], base[wid][key]
                ok = (f32(got) == f32(want)) if IS_FLOAT[key] else (int(got) == int(want))
                if not ok:
                    bad.append({"item": "-", "weapon": wid, "key": key,
                                "want": want, "got": got,
                                "source": "%s（对照 %s）" % (tag, base_tag),
                                "off": "+0x%02x" % OFFSET_OF[key],
                                "type": "f32" if IS_FLOAT[key] else "i32"})
    return total, bad


def check_baseline_against_ini(baseline):
    """T0 的独立校验：`weapon.ini` 里写了的键，内存值必须等于参考值。

    ★ 这一条是整套测试的**地基**：它不依赖 hook 也不依赖 `effective()`，
      比的是「内存」和「离线从 weapon.ini 抽出来的 bot_weapons.json」。
      它绿了，后面拿基线当期望值才站得住。
    """
    bad, total = [], 0
    for item_id, wid in sorted(AMMO_OF.items()):
        rec = baseline.get(wid)
        if rec is None:
            continue
        for key, value in sorted(weaponcfg.reference(item_id).items()):
            total += 1
            got = rec[key]
            ok = (f32(got) == f32(value)) if IS_FLOAT[key] else (int(got) == int(value))
            if not ok:
                bad.append({"item": item_id, "weapon": wid, "key": key,
                            "want": value, "got": got, "source": "weapon.ini 参考值",
                            "off": "+0x%02x" % OFFSET_OF[key],
                            "type": "f32" if IS_FLOAT[key] else "i32"})
    return total, bad


def compare_holders(snapshot, table, mode, baseline):
    """T8：进图时快照进「持枪器」的弹匣容量，必须等于这一局该用的 `magazine`。

    弹匣容量 `[[持枪器+0x48] + 槽*4]`，槽下标由武器 Id 拆位得出（§41）。
    只核**当前角色已装备**的那几把。
    """
    bad, total = [], 0
    equipped = account_equipped()
    rows = [r for r in snapshot.get("holders", []) if r.get("max_ammo")]
    if not rows:
        return 0, [{"item": "-", "weapon": "-", "key": "持枪器",
                    "want": "读得到", "got": "不在局内或没有角色对象", "source": "-"}]
    mine = rows[0]
    for item_id in equipped:
        if item_id not in AMMO_OF:
            continue
        wid = AMMO_OF[item_id]
        slot = wtab.weapon_slot_index(wid)
        if not 0 <= slot < len(mine["max_ammo"]):
            continue
        total += 1
        want = expected_of(item_id, mode, table, baseline)["magazine"][0]
        got = mine["max_ammo"][slot]
        if want is not None and int(got) != int(want):
            bad.append({"item": item_id, "weapon": wid, "key": "弹匣快照(槽%d)" % slot,
                        "want": want, "got": got, "source": "持枪器+0x48",
                        "off": "[+0x48]+%d*4" % slot, "type": "i32"})
    return total, bad


def account_equipped():
    """测试账号当前角色身上装备着的自定义武器物品 id。"""
    with open(os.path.join(DATA_DIR, "accounts.json"), "r", encoding="utf-8") as fp:
        data = json.load(fp)
    acc = data["accounts"][drive.ACCOUNT]
    character = int(acc.get("character", 0))
    out = []
    for item_id in acc.get("equipped", []):
        item = shopdata.get(int(item_id))
        if item is not None and getattr(item, "custom", False) \
                and int(getattr(item, "character", -1)) == character:
            out.append(int(item_id))
    return out


# --------------------------------------------------------------------------
# 备份 / 还原
# --------------------------------------------------------------------------

def latest_backup():
    marker = os.path.join(ROOT, "logs", ".e2e-backup-latest")
    if not os.path.exists(marker):
        return None
    with open(marker, "r", encoding="utf-8") as fp:
        stamp = fp.read().strip()
    path = os.path.join(ROOT, "logs", "e2e-backup-%s" % stamp)
    return path if os.path.isdir(path) else None


def backup_now():
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(ROOT, "logs", "e2e-backup-%s" % stamp)
    os.makedirs(path, exist_ok=True)
    for name in ("weapons.json", "accounts.json"):
        shutil.copy2(os.path.join(DATA_DIR, name), os.path.join(path, name))
    with open(os.path.join(ROOT, "logs", ".e2e-backup-latest"), "w", encoding="utf-8") as fp:
        fp.write(stamp)
    return path


def restore(report=print):
    """把用户原来的 `weapons.json` 放回去。

    ★ `serial` 取「原值」和「测试期间见过的最大值 + 1」中的**较大者** ——
      序号倒退会让客户端那边的去重闩把真正的新表当成看过的。
      `accounts.json` **不动**（跑测试期间玩家自己的金币 / 经验是真变了的，
      拿旧快照盖回去等于吞掉用户的进度）。
    """
    src = latest_backup()
    if not src:
        report("⚠ 找不到备份目录，没有还原")
        return False
    with open(os.path.join(src, "weapons.json"), "r", encoding="utf-8") as fp:
        old = json.load(fp)
    live_path = os.path.join(DATA_DIR, "weapons.json")
    try:
        with open(live_path, "r", encoding="utf-8") as fp:
            now = json.load(fp)
        old["serial"] = max(int(old.get("serial", 0)), int(now.get("serial", 0)) + 1)
    except (OSError, ValueError):
        pass
    tmp = live_path + ".e2e-tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(old, fp, ensure_ascii=False, indent=2)
    os.replace(tmp, live_path)
    report("已还原 weapons.json（serial=%s，备份在 %s）" % (old["serial"], src))
    return True


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------

class Runner(object):
    def __init__(self, client, only=None):
        self.client = client
        self.only = set(only or [])
        self.baseline = {}
        self.results = []
        self.sets = {}
        #: 按场景分开攒快照：闯关图和对战图的**原版**记录本来就不一样
        #: （关卡自带 weapon-<难度>.ini 覆盖怪物武器），只能同场景内互比。
        self.scene_dumps = {"pve": [], "pvp": []}

    def want(self, name):
        return not self.only or name in self.only

    def ensure_set(self, name):
        """保证这一套参数**已经推上去了**。

        ★ `--only T2` 这种跳着跑的用法下，SET-A 可能还没被推过 —— 那时 T2 拿
          `self.sets["A"]` 会 KeyError，或者更糟：拿到一份**没推上去**的表当期望值，
          于是比出一堆假的「不一致」。这里统一走「没推过就现推」。
        """
        if name not in self.sets:
            table = paramsets.build(name)
            print("  推 SET-%s …" % name)
            self.client.apply_table(table)
            self.sets[name] = table
        return self.sets[name]

    def record(self, name, title, total, bad, note=""):
        self.results.append({"name": name, "title": title, "total": total,
                             "bad": bad, "note": note})
        flag = "✅" if not bad else "❌"
        print("%s %-4s %-28s 比对 %4d 格，不一致 %d 格 %s"
              % (flag, name, title, total, len(bad), note))
        for row in bad[:8]:
            print("      %s %s %s 期望 %s(%s) 实际 %s"
                  % (row["weapon"], row.get("off", ""), row["key"],
                     row["want"], row["source"], row["got"]))
        if len(bad) > 8:
            print("      …… 还有 %d 条，见报告" % (len(bad) - 8))

    # ---- 一次「设参数 → 开一局 → 读内存」------------------------------

    def round_trip(self, set_name, mode, tag, with_holders=False, push=True):
        table = self.sets.get(set_name)
        if push or table is None:
            table = paramsets.build(set_name)
            self.sets[set_name] = table
            print("  推 SET-%s …" % set_name)
            self.client.apply_table(table)
        room = "quest" if mode == "pve" else "pvp"
        if drive.room_type() != (drive.SESSION_TYPE_QUEST if room == "quest"
                                 else drive.SESSION_TYPE_NORMAL):
            drive.create_room(room)
        drive.start_round(room)
        snap = dump(tag, with_holders=with_holders)
        self.scene_dumps[mode].append((tag, snap))
        return table, snap

    def finish(self, mode):
        drive.end_round("quest" if mode == "pve" else "pvp")


def offline_selftest():
    """离线自检（T9 + 参数集自检 + 解析器自检），不碰客户端。"""
    bad = []
    bad += ["参数集：" + p for p in paramsets.check()]

    # T9：服务端把设定值打进 0x0F01 之后，再解回来必须一模一样。
    table = paramsets.build("A")
    for mode, hook_mode in (("pve", weaponcfg.HOOK_MODE_PVE),
                            ("pvp", weaponcfg.HOOK_MODE_PVP)):
        frame = weaponcfg.build_hook_frame(hook_mode=hook_mode, table={
            "format": 1, "serial": 1,
            "custom": {str(k): v for k, v in table.items()}, "desc": {}})
        # `parse_hook_frame` 回的是 (格式, serial, 模式, [(武器Id, {模式: {字段: 值}})])
        fmt, _serial, got_mode, records = weaponcfg.parse_hook_frame(frame)
        if fmt != weaponcfg.WIRE_FORMAT or got_mode != hook_mode:
            bad.append("T9：帧头对不上（format=%s mode=%s）" % (fmt, got_mode))
        by_id = dict(records)
        for item_id, block in table.items():
            wid = AMMO_OF[item_id]
            rec = by_id.get(wid)
            if rec is None:
                bad.append("T9：%s（武器 %s）没出现在 0x0F01 里" % (item_id, wid))
                continue
            got = rec[mode]
            for key in KEYS:
                want = block[mode][key]
                have = got.get(key)
                ok = (f32(have) == f32(want)) if IS_FLOAT[key] else (have == want)
                if not ok:
                    bad.append("T9：%s/%s %s 设 %r，线格式里是 %r"
                               % (item_id, mode, key, want, have))
    print("离线自检：%d 条问题" % len(bad))
    for line in bad[:20]:
        print("  ✗", line)
    return bad


#: 哪份快照是在哪种场景里取的。★ 闯关图和对战图的**原版**记录本来就不一样
#: （关卡自带 `weapon-<难度>.ini` 覆盖怪物武器），所以只能同场景内互比。
SCENE_OF = {
    "T0-baseline": "pve", "T1-pve-setA": "pve", "T3-pve-again": "pve",
    "T4a-during-round": "pve", "T4b-next-round": "pve", "T5-pve-setC": "pve",
    "T6-pve-min": "pve",
    "T2-pvp-setA": "pvp", "T5-pvp-setC": "pvp", "T6-pvp-max": "pvp",
    "T10-pvp-team": "pvp",
}


def analyze_dumps():
    """拿 `logs/e2e-dumps/` 里已有的快照重算「原版武器不串扰」。不碰游戏。"""
    scenes = {"pve": [], "pvp": []}
    for tag, mode in sorted(SCENE_OF.items()):
        path = os.path.join(DUMP_DIR, "%s.json" % tag)
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as fp:
            snap = json.load(fp)
        # ★ JSON 的对象键一律是**字符串**，而内存里那份是 int —— 不转回来的话
        #   `wid in CUSTOM_WEAPON_IDS` 永远为假，自定义武器会被当成原版武器比，
        #   于是「不串扰」这条满屏假阳性（2026-09-20 踩过）。
        snap["table"] = {int(k): v for k, v in snap["table"].items()}
        scenes[mode].append((tag, snap))
    rc = 0
    for mode, title in (("pve", "闯关图"), ("pvp", "对战图")):
        dumps = scenes[mode]
        n, bad = compare_untouched(dumps, title)
        print("%s %s：%d 份快照（%s），比对 %d 格，不一致 %d 格"
              % ("✅" if not bad else "❌", title, len(dumps),
                 "、".join(t for t, _ in dumps), n, len(bad)))
        for row in bad[:12]:
            print("      %s %s %s 基线 %s -> %s" % (row["weapon"], row["off"],
                                                    row["key"], row["want"], row["got"]))
        rc |= 1 if bad else 0
    return rc


def write_report(runner, path):
    lines = ["# 18 把自定义武器 · 参数端到端验证报告", "",
             "跑完时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"), ""]
    total_bad = sum(len(r["bad"]) for r in runner.results)
    lines += ["## 总览", "",
              "| 用例 | 验什么 | 比对格数 | 不一致 | 结果 |", "|---|---|---|---|---|"]
    for r in runner.results:
        lines.append("| %s | %s%s | %d | %d | %s |"
                     % (r["name"], r["title"], r.get("note", ""), r["total"],
                        len(r["bad"]), "✅ 通过" if not r["bad"] else "❌ **有问题**"))
    lines += ["", "**合计不一致 %d 格。**" % total_bad, ""]
    for r in runner.results:
        if not r["bad"]:
            continue
        lines += ["## %s %s —— 不一致明细" % (r["name"], r["title"]), "",
                  "| 武器Id | 物品id | 字段 | 偏移 | 类型 | 期望 | 来源 | 内存实际 |",
                  "|---|---|---|---|---|---|---|---|"]
        for row in r["bad"]:
            lines.append("| %s | %s | %s | %s | %s | %s | %s | %s |"
                         % (row["weapon"], row["item"], row["key"], row.get("off", ""),
                            row.get("type", ""), row["want"], row["source"], row["got"]))
        lines.append("")
    with open(path, "w", encoding="utf-8", newline="\n") as fp:
        fp.write("\n".join(lines) + "\n")
    return path


#: 实机部分有没有开始跑 —— 决定「中途炸了要不要还原 weapons.json」。
#: `--selftest` / `--analyze` 根本不碰那个文件，炸了也不该去动它。
_LIVE_STARTED = False


def main(argv=None):
    ap = argparse.ArgumentParser(description="自定义武器参数端到端验证")
    ap.add_argument("--selftest", action="store_true", help="只跑离线部分")
    ap.add_argument("--restore", action="store_true", help="只还原 weapons.json")
    ap.add_argument("--only", nargs="*", default=None, help="只跑这几条用例")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--analyze", action="store_true",
                    help="不跑游戏，只拿 logs/e2e-dumps/ 里已有的快照重算「不串扰」那条")
    args = ap.parse_args(argv)

    if args.analyze:
        return analyze_dumps()
    if args.restore:
        return 0 if restore() else 1
    if args.selftest:
        return 1 if offline_selftest() else 0

    problems = offline_selftest()
    if problems:
        print("离线自检没过，先修它 —— 实机部分不跑了。")
        return 1

    if not args.no_backup and not latest_backup():
        print("备份到", backup_now())

    global _LIVE_STARTED
    _LIVE_STARTED = True          # 从这一刻起，weapons.json 已经会被改了
    drive.check_resolution()
    drive.login()
    drive.dismiss_notice()

    client = adminapi.AdminClient().login()
    runner = Runner(client, args.only)
    t0 = time.monotonic()

    # ---- T0 基线 ------------------------------------------------------
    print("\n== T0 基线（18 把全部清空 → 开一局 PVE）==")
    table, snap = runner.round_trip("EMPTY", "pve", "T0-baseline", with_holders=True)
    runner.baseline = {wid: {k: rec[k] for k in KEYS}
                       for wid, rec in snap["table"].items()}
    n, bad = check_baseline_against_ini(runner.baseline)
    runner.record("T0", "基线 == weapon.ini 参考值", n, bad)
    runner.finish("pve")

    # ---- T1 PVE 全格 --------------------------------------------------
    if runner.want("T1"):
        print("\n== T1 PVE 全格（SET-A）==")
        table, snap = runner.round_trip("A", "pve", "T1-pve-setA", with_holders=True)
        n, bad = compare(snap, table, "pve", runner.baseline)
        runner.record("T1", "PVE 18×14 == SET-A", n, bad)
        n3, bad3 = compare_holders(snap, table, "pve", runner.baseline)
        runner.record("T8", "弹匣快照进持枪器", n3, bad3)
        runner.finish("pve")

    # ---- T2 切 PVP ----------------------------------------------------
    if runner.want("T2"):
        print("\n== T2 PVE→PVP（同一套 SET-A 的 PVP 那半边）==")
        runner.ensure_set("A")        # ★ 必须在开局**之前**推，否则这一局跑的是旧值
        drive.create_room("pvp")
        drive.start_round("pvp")
        snap = dump("T2-pvp-setA"); runner.scene_dumps["pvp"].append(("T2-pvp-setA", snap))
        n, bad = compare(snap, runner.ensure_set("A"), "pvp", runner.baseline)
        runner.record("T2", "PVP 18×14 == SET-A", n, bad)
        runner.finish("pvp")

    # ---- T3 切回 PVE ---------------------------------------------------
    if runner.want("T3"):
        print("\n== T3 PVP→PVE 回切 ==")
        runner.ensure_set("A")        # ★ 同上：推在开局之前
        drive.create_room("quest")
        drive.start_round("quest")
        snap = dump("T3-pve-again"); runner.scene_dumps["pve"].append(("T3-pve-again", snap))
        n, bad = compare(snap, runner.ensure_set("A"), "pve", runner.baseline)
        runner.record("T3", "回切 PVE 仍 == SET-A", n, bad)

        # ---- T4a 局内改，这一局不许变 ---------------------------------
        if runner.want("T4"):
            print("  局内推 SET-B …")
            setb = paramsets.build("B")
            runner.sets["B"] = setb
            client.apply_table(setb)
            snap2 = dump("T4a-during-round")
            n, bad = compare(snap2, runner.sets["A"], "pve", runner.baseline)
            runner.record("T4a", "局内改参数，这一局不变", n, bad)
        runner.finish("pve")

        # ---- T4b 下一局生效 --------------------------------------------
        if runner.want("T4"):
            print("\n== T4b 下一局生效 ==")
            drive.start_round("quest")
            snap3 = dump("T4b-next-round"); runner.scene_dumps["pve"].append(("T4b-next-round", snap3))
            n, bad = compare(snap3, runner.sets["B"], "pve", runner.baseline)
            runner.record("T4b", "下一局变成 SET-B", n, bad)
            runner.finish("pve")

    # ---- T5 留空回退 ---------------------------------------------------
    if runner.want("T5"):
        print("\n== T5 留空回退参考值（SET-C）==")
        table, snap = runner.round_trip("C", "pve", "T5-pve-setC")
        n, bad = compare(snap, table, "pve", runner.baseline)
        runner.record("T5a", "PVE 留空格回退", n, bad)
        runner.finish("pve")
        drive.create_room("pvp")
        drive.start_round("pvp")
        snap = dump("T5-pvp-setC"); runner.scene_dumps["pvp"].append(("T5-pvp-setC", snap))
        n, bad = compare(snap, runner.sets["C"], "pvp", runner.baseline)
        runner.record("T5b", "PVP 留空格回退", n, bad)
        runner.finish("pvp")

    # ---- T6 边界 -------------------------------------------------------
    if runner.want("T6"):
        print("\n== T6 边界（SET-D：PVE 下界 / PVP 上界）==")
        table = paramsets.build("D")
        runner.sets["D"] = table
        client.apply_table(table)
        drive.create_room("quest")
        drive.start_round("quest")
        snap = dump("T6-pve-min"); runner.scene_dumps["pve"].append(("T6-pve-min", snap))
        n, bad = compare(snap, table, "pve", runner.baseline)
        runner.record("T6a", "PVE 全取下界", n, bad)
        runner.finish("pve")
        drive.create_room("pvp")
        drive.start_round("pvp")
        snap = dump("T6-pvp-max"); runner.scene_dumps["pvp"].append(("T6-pvp-max", snap))
        n, bad = compare(snap, table, "pvp", runner.baseline)
        runner.record("T6b", "PVP 全取上界", n, bad)
        runner.finish("pvp")

    # ---- T10 组队战 PVP（1v1，真正的对战模式）---------------------------
    if runner.want("T10"):
        print("\n== T10 组队战 PVP（bot 换到 2 队做 1v1）==")
        runner.ensure_set("A")
        drive.create_room("pvp_team")
        drive.start_round("pvp_team")
        snap = dump("T10-pvp-team")
        runner.scene_dumps["pvp"].append(("T10-pvp-team", snap))
        n, bad = compare(snap, runner.sets["A"], "pvp", runner.baseline)
        runner.record("T10", "组队战 PVP == SET-A 的 PVP", n, bad)
        runner.finish("pvp")

    # ---- T7 不串扰（同场景跨轮比对，放最后一次算完）-------------------
    for mode, title in (("pve", "闯关图"), ("pvp", "对战图")):
        n, bad = compare_untouched(runner.scene_dumps[mode], title)
        runner.record("T7-" + mode, "原版武器在%s里逐轮不变" % title, n, bad,
                      note="（%d 份快照）" % len(runner.scene_dumps[mode]))

    print("\n实机部分用时 %.0f 分钟" % ((time.monotonic() - t0) / 60.0))
    restore()
    path = write_report(runner, os.path.join(ROOT, "logs",
                                             "e2e-report-%s.md" % time.strftime("%Y%m%d-%H%M%S")))
    print("报告:", path)
    return 1 if any(r["bad"] for r in runner.results) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException:
        # ★ 中途炸了（含 Ctrl+C）也要把**用户原来的 weapons.json** 放回去 ——
        #   `server/data/` 是 .gitignore 的，git 救不回来。
        if _LIVE_STARTED:
            try:
                restore()
            except Exception as err:           # 还原失败也不能盖住原始异常
                print("⚠ 自动还原失败：%s —— 手动跑 run.py --restore" % err)
        raise
