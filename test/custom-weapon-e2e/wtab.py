#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wtab.py —— 从**外部**读客户端进程里的武器表（`WeaponTable`）。

    python wtab.py                    # 人看的表格（只列自定义武器）
    python wtab.py --all              # 连原版 118 把一起列
    python wtab.py --json out.json    # 机器读的整表快照
    python wtab.py --holders          # 另外读「持枪器」里的弹匣快照（要在局内）

## 这是在解决什么

`0x0F01` 把数值推给 bshook、bshook 写进客户端内存 —— 但 **hook 日志只回读 2 格**
（第一条记录的 `Damage` / `MagazineCount`），另外 12 格靠日志根本验不了。
要确认「每个参数都真正生效」，只能自己 `ReadProcessMemory` 逐格读回来。

## 怎么找到一条记录

武器表是**哈希链容器**，记录在堆上逐条 `new`（`0x254` B），**彼此不连续**，
所以没有「基址 + stride × 下标」这回事。查表函数 `0x004157bf` 的算法（本会话
逐指令解出来，`test_patchsites_wtab` 钉着这 10 个字节）：

    buckets_begin = u32[表 + 4]        buckets_end = u32[表 + 8]
    nbucket       = (end - begin) // 4
    node          = u32[begin + 4 * (武器Id % nbucket)]
    while node and i32[node + 4] != 武器Id:  node = u32[node + 0]
    记录          = u32[node + 8]

⇒ 节点布局 `{ next(+0), key=武器Id(+4), 记录指针(+8) }`。
本模块**遍历所有桶**而不是按 id 查 —— 一次就把原版 118 条和自定义 18 条全拿到，
顺带能核「原版记录一个字节都没被串改」。

## 常量从哪来

★ **一个都不在这里写死**：表地址 / 记录大小 / 14 格偏移与类型全部现读
`hook/bshook.c` 的 `WTAB_*`，字段名和顺序现读 `server/weaponcfg.py` 的 `FIELDS`。
以后加第 15 格，这里自动跟着变；对不上会当场抛错，而不是静默少读一格。

## 前提

- 客户端**无 ASLR**（`DllCharacteristics=0x0000`，ImageBase 固定 `0x400000`），
  所以 `0x0072e788` 这种全局地址直接可用。
- 只读：全程 `PROCESS_VM_READ | PROCESS_QUERY_INFORMATION`，一个写操作都没有。
- **登录框阶段表是空的**（启动那次 `Load` 进大厅才跑，§36），读到 0 条不是错。
"""
import argparse
import ctypes as C
import json
import os
import re
import struct
import sys
from ctypes import wintypes as W

#: 中文输出走 UTF-8（和 `tools/gs_ctl.py` 一个口径）。控制台是 936 时靠
#: `[Console]::OutputEncoding = UTF8` 或 `chcp 65001` 配合，报告文件一律 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
BSHOOK_C = os.path.join(ROOT, "hook", "bshook.c")

if os.path.join(ROOT, "server") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "server"))

import weaponcfg  # noqa: E402  （要 sys.path 先就位）

#: `LobbyStage` 全局，用来找角色对象（抄 `tools/probe_death.py:58`）。
LOBBY_STAGE_VA = 0x72E29C
#: 角色对象数组在 `LobbyStage` 里的偏移，6 个座位（`tools/probe_death.py:69`）。
CHAR_OBJ_BASE = 0x1D0
SEAT_COUNT = 6
#: 持枪器挂在角色对象的哪一格 —— `0x517229: mov ecx,[edi+0x7a8]` 之后就 `call 0x48b96e`
#: （那个函数把弹匣容量一次性灌进 4 格 vector，§42）。本会话逐指令查出来的。
CHAR_WEAPON_HOLDER = 0x7A8
#: 持枪器里两个 4 格 vector 的**首指针**（`0x48bb1c`：`[持枪器+0x54] + 索引*4`）。
HOLDER_MAX_AMMO = 0x48
HOLDER_CUR_AMMO = 0x54
AMMO_SLOTS = 4


# --------------------------------------------------------------------------
# 常量：现读 bshook.c，不在这里写第二份
# --------------------------------------------------------------------------

def _bshook_source():
    with open(BSHOOK_C, "r", encoding="utf-8", errors="replace") as fp:
        return fp.read()


def c_define(src, name):
    """抠一个 `#define <name> <整数字面量>`（允许 0x 前缀和 u/U 后缀）。"""
    m = re.search(r"^#define\s+%s\s+(0[xX][0-9a-fA-F]+|\d+)[uU]?\b" % re.escape(name),
                  src, re.M)
    if not m:
        raise RuntimeError("bshook.c 里找不到 #define %s —— 常量改名了？" % name)
    return int(m.group(1), 0)


def wtab_fields(src):
    """抠 `WTAB_FIELD[]` 的 14 行 `{ 0x34, 0, "Damage" }`，返回 [(偏移, 是否浮点, ini键名)]。"""
    m = re.search(r"WTAB_FIELD\s*\[\s*WTAB_FIELDS\s*\]\s*=\s*\{(.*?)\n\s*\};",
                  src, re.S)
    if not m:
        raise RuntimeError("bshook.c 里找不到 WTAB_FIELD[] 的初始化块")
    rows = re.findall(r"\{\s*(0[xX][0-9a-fA-F]+)\s*,\s*([01])\s*,\s*\"([^\"]+)\"\s*\}",
                      m.group(1))
    return [(int(off, 0), bool(int(isf)), name) for off, isf, name in rows]


class Layout(object):
    """把「服务端字段表」和「hook 偏移表」对齐成一张可用的表。

    两边**必须逐格同序**（bshook 的 `WTAB_FIELD[i]` 就是 `weaponcfg.FIELDS[i]`，
    `i` 同时是线格式 mask 的 bit i）。对不上就抛错 —— 这正是「加了第 15 格却漏改
    一处」时最该炸的地方。
    """

    def __init__(self):
        src = _bshook_source()
        self.table_va = c_define(src, "WTAB_TABLE_VA")
        self.record_size = c_define(src, "WTAB_RECORD_SIZE")
        self.lookup_va = c_define(src, "WTAB_LOOKUP_VA")
        n_c = c_define(src, "WTAB_FIELDS")
        rows = wtab_fields(src)
        if len(rows) != n_c:
            raise RuntimeError("WTAB_FIELD[] 有 %d 行，但 WTAB_FIELDS = %d" % (len(rows), n_c))
        if len(rows) != len(weaponcfg.FIELDS):
            raise RuntimeError(
                "hook 有 %d 格、weaponcfg.FIELDS 有 %d 格 —— 两边不同步了，先对齐再跑"
                % (len(rows), len(weaponcfg.FIELDS)))
        self.fields = []
        for i, (off, is_float, ini_name) in enumerate(rows):
            spec = weaponcfg.FIELDS[i]
            key, label, unit, _src_key, cast, low, high = spec
            want_float = (cast is float)
            if want_float != is_float:
                raise RuntimeError(
                    "第 %d 格 %s：weaponcfg 说 %s，bshook 说 %s —— 类型不一致"
                    % (i, key, "float" if want_float else "int",
                       "float" if is_float else "int"))
            self.fields.append({
                "index": i, "key": key, "label": label, "unit": unit,
                "ini": ini_name, "off": off, "is_float": is_float,
                "min": low, "max": high,
            })

    def keys(self):
        return [f["key"] for f in self.fields]


LAYOUT = Layout()


# --------------------------------------------------------------------------
# 读内存
# --------------------------------------------------------------------------

k32 = C.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
k32.OpenProcess.restype = W.HANDLE
k32.ReadProcessMemory.argtypes = [W.HANDLE, C.c_void_p, C.c_void_p,
                                  C.c_size_t, C.POINTER(C.c_size_t)]
k32.ReadProcessMemory.restype = W.BOOL
k32.CloseHandle.argtypes = [W.HANDLE]

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400


def find_pid(exe="BigShot.exe"):
    """找游戏进程（抄 `tools/probe_equip_bonus.py` 的做法：走 tasklist，不额外依赖）。"""
    import subprocess
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq " + exe, "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace").stdout
    for line in out.splitlines():
        cells = [c.strip('"') for c in line.strip().split('","')]
        if len(cells) >= 2 and cells[0].lower() == exe.lower():
            try:
                return int(cells[1])
            except ValueError:
                continue
    return None


class Mem(object):
    """一个只读的进程内存句柄。"""

    def __init__(self, pid):
        self.pid = pid
        self.h = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
        if not self.h:
            raise OSError("OpenProcess(pid=%d) 失败，错误码 %d" % (pid, C.get_last_error()))

    def close(self):
        if self.h:
            k32.CloseHandle(self.h)
            self.h = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def read(self, addr, n):
        if not addr:
            return None
        buf = C.create_string_buffer(n)
        got = C.c_size_t(0)
        ok = k32.ReadProcessMemory(self.h, C.c_void_p(addr), buf, n, C.byref(got))
        if not ok or got.value != n:
            return None
        return buf.raw[:n]

    def u32(self, addr):
        raw = self.read(addr, 4)
        return None if raw is None else struct.unpack("<I", raw)[0]

    def i32(self, addr):
        raw = self.read(addr, 4)
        return None if raw is None else struct.unpack("<i", raw)[0]


def decode_record(raw):
    """把 0x254 字节的记录按 14 格解出来。int 走 `<i`、float 走 `<f`。"""
    out = {}
    for f in LAYOUT.fields:
        chunk = raw[f["off"]:f["off"] + 4]
        out[f["key"]] = struct.unpack("<f" if f["is_float"] else "<i", chunk)[0]
    return out


def dump_table(mem):
    """遍历整张哈希表，返回 {武器Id: {字段: 值}}，外加一份诊断信息。

    ★ 走全部桶而不是按 id 查：一次拿全，而且能发现「同一个 id 出现两次」
      这类本来查不出来的怪事（§44 当年就怀疑过有第二张表）。
    """
    info = {"table_va": LAYOUT.table_va, "buckets": 0, "records": 0,
            "duplicate_ids": [], "unreadable": []}
    begin = mem.u32(LAYOUT.table_va + 4)
    end = mem.u32(LAYOUT.table_va + 8)
    if not begin or not end or end <= begin or (end - begin) > 0x100000:
        info["error"] = "桶数组读不到或不合理：begin=%s end=%s" % (begin, end)
        return {}, info
    nbucket = (end - begin) // 4
    info["buckets"] = nbucket
    raw_buckets = mem.read(begin, end - begin)
    if raw_buckets is None:
        info["error"] = "桶数组整块读不出来"
        return {}, info

    table = {}
    for i in range(nbucket):
        node = struct.unpack_from("<I", raw_buckets, i * 4)[0]
        # 链长没有理论上界，但一条链长到这个地步只能是读坏了 —— 当保险丝用，
        # 不是判据：真被触发会在 info 里留痕，不会静默截断。
        guard = 0
        while node:
            head = mem.read(node, 12)
            if head is None:
                info["unreadable"].append({"node": node, "bucket": i})
                break
            nxt, key, rec = struct.unpack("<3I", head)
            raw = mem.read(rec, LAYOUT.record_size)
            if raw is None:
                info["unreadable"].append({"node": node, "id": key, "rec": rec})
            else:
                if key in table:
                    info["duplicate_ids"].append(key)
                table[key] = decode_record(raw)
                table[key]["_rec"] = rec
                info["records"] += 1
            node = nxt
            guard += 1
            if guard > 4096:
                info["unreadable"].append({"bucket": i, "why": "链表长度超过 4096，疑似读坏"})
                break
    return table, info


def dump_holders(mem):
    """读 6 个座位的「持枪器」弹匣快照（§42 那条链的末端）。

    角色对象 = `[LobbyStage + 0x1d0 + 座位*4]`；持枪器 = `[角色 + 0x7a8]`；
    弹匣容量 = `u32[ u32[持枪器+0x48] + 槽*4 ]`，当前弹药同理在 `+0x54`。
    不在局内时角色对象是 0，返回空列表 —— 这不是错。
    """
    lobby = mem.u32(LOBBY_STAGE_VA) or 0
    out = []
    if not lobby:
        return out
    for seat in range(SEAT_COUNT):
        obj = mem.u32(lobby + CHAR_OBJ_BASE + seat * 4)
        if not obj:
            continue
        holder = mem.u32(obj + CHAR_WEAPON_HOLDER)
        row = {"seat": seat, "char": obj, "holder": holder,
               "char_id": mem.i32(obj + 0x2AC), "hp": mem.i32(obj + 0x150)}
        for name, off in (("max_ammo", HOLDER_MAX_AMMO), ("cur_ammo", HOLDER_CUR_AMMO)):
            base = mem.u32(holder + off) if holder else None
            row[name] = ([mem.i32(base + i * 4) for i in range(AMMO_SLOTS)]
                         if base else None)
        out.append(row)
    return out


def weapon_slot_index(weapon_id):
    """武器 Id 拆位拿弹药数组下标（`0x40a138`，§41）。

        系列 = id % 10 ; 索引 = (id // 10) % 100
        if 3 <= 系列 <= 5 and 索引 < 50:  索引 %= 10
    """
    series = weapon_id % 10
    index = (weapon_id // 10) % 100
    if 3 <= series <= 5 and index < 50:
        index %= 10
    return index


def snapshot(pid=None, with_holders=False):
    """一次取全：整表 + 诊断 +（可选）弹匣快照。"""
    pid = pid or find_pid()
    if pid is None:
        raise RuntimeError("没找到 BigShot.exe —— 游戏没在跑？")
    with Mem(pid) as mem:
        table, info = dump_table(mem)
        holders = dump_holders(mem) if with_holders else []
    return {"pid": pid, "table": table, "info": info, "holders": holders}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _custom_ammo_ids():
    """18 把自定义武器的 `ammo_id` → 物品 id（现读 shop_items.json，不写死）。"""
    import shopdata
    out = {}
    for item_id in weaponcfg.custom_item_ids():
        item = shopdata.get(item_id)
        if item is not None and item.ammo_id:
            out[int(item.ammo_id)] = int(item_id)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="从外部读客户端的武器表")
    ap.add_argument("--pid", type=int, default=None)
    ap.add_argument("--all", action="store_true", help="连原版武器一起列")
    ap.add_argument("--json", metavar="文件", default=None, help="整表写成 JSON")
    ap.add_argument("--holders", action="store_true", help="另读持枪器弹匣快照")
    args = ap.parse_args(argv)

    snap = snapshot(args.pid, with_holders=args.holders)
    table, info = snap["table"], snap["info"]
    print("pid=%d  表 @ %08X  桶=%d  记录=%d"
          % (snap["pid"], info["table_va"], info["buckets"], info["records"]))
    if info.get("error"):
        print("  ⚠", info["error"])
    if info["duplicate_ids"]:
        print("  ⚠ 同一个 id 出现多次:", info["duplicate_ids"])
    if info["unreadable"]:
        print("  ⚠ 有 %d 个节点读不出来" % len(info["unreadable"]))

    custom = _custom_ammo_ids()
    keys = LAYOUT.keys()
    ids = sorted(table) if args.all else sorted(i for i in table if i in custom)
    if ids:
        print("%-9s %-9s %s" % ("武器Id", "物品id", " ".join("%-11s" % k for k in keys)))
        for wid in ids:
            rec = table[wid]
            print("%-9d %-9s %s" % (wid, custom.get(wid, "-"),
                                    " ".join("%-11s" % rec[k] for k in keys)))
    else:
        print("（表里没有要列的记录 —— 还停在登录框的话这是正常的）")

    if args.holders:
        print()
        if snap["holders"]:
            for row in snap["holders"]:
                print("座位%d 角色=%08X 持枪器=%s 弹匣容量=%s 当前弹药=%s"
                      % (row["seat"], row["char"],
                         ("%08X" % row["holder"]) if row["holder"] else "NULL",
                         row["max_ammo"], row["cur_ammo"]))
        else:
            print("（没有角色对象 —— 不在局内时这是正常的）")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fp:
            json.dump(snap, fp, ensure_ascii=False, indent=1, sort_keys=True)
        print("\n已写", args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
