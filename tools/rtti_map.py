#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rtti_map.py —— 从 MSVC RTTI 把 Packet_* 类还原成「类名 → vftable → 构造函数」

镜像是平坦内存转储，file offset == RVA，ImageBase=0x400000。

MSVC RTTI 链：
    TypeDescriptor  { void* pVFTable; void* spare; char name[]; }   TD = name_va - 8
    COL             { u32 sig; u32 off; u32 cdOff; TD* ; CHD* }     COL = (指向TD的dword处) - 0x0C
    vftable         前面 4 字节 = COL*                              vft = (指向COL的dword处) + 4
"""
import struct, sys, re

BASE = 0x400000
IMG = open(r"D:\work\popshot\re\BigShot_22524.img", "rb").read()


def va2off(va):
    return va - BASE


def off2va(o):
    return o + BASE


def rd32(va):
    o = va2off(va)
    if o < 0 or o + 4 > len(IMG):
        return None
    return struct.unpack_from("<I", IMG, o)[0]


def find_all(needle, start=0):
    res = []
    i = IMG.find(needle, start)
    while i != -1:
        res.append(i)
        i = IMG.find(needle, i + 1)
    return res


def dwords_pointing_to(va):
    """返回所有 (dword 所在 va) 使得 *va == 目标"""
    return [off2va(o) for o in find_all(struct.pack("<I", va))]


def enum_typedescriptors(prefix=None):
    """扫全镜像找 RTTI 名字串，回 [(td_va, name)]。类 .?AV 与结构体 .?AU 都要。"""
    out = []
    offs = []
    for p in ([prefix] if prefix else [b".?AV", b".?AU"]):
        offs += find_all(p)
    for o in sorted(offs):
        end = IMG.find(b"\x00", o)
        if end == -1 or end - o > 512:
            continue
        name = IMG[o:end]
        if not name.endswith(b"@@") and b"@@" not in name:
            continue
        td = off2va(o) - 8
        # 校验：TD 的第一个 dword 应该是 type_info 的 vftable（一个合理的 va）
        v = rd32(td)
        if v is None or not (BASE <= v < BASE + len(IMG)):
            continue
        out.append((td, name.decode("latin1")))
    return out


def col_of(td):
    """找引用该 TD 的 COL"""
    cols = []
    for loc in dwords_pointing_to(td):
        col = loc - 0x0C
        if rd32(col) == 0 and rd32(col + 0x10) is not None:
            cols.append(col)
    return cols


def vftables_of(col):
    return [loc + 4 for loc in dwords_pointing_to(col)]


def main():
    want = sys.argv[1] if len(sys.argv) > 1 else "Packet_"
    tds = enum_typedescriptors()
    rows = []
    for td, name in tds:
        if want not in name:
            continue
        for col in col_of(td):
            for vft in vftables_of(col):
                rows.append((name, td, col, vft))
    rows.sort(key=lambda r: r[0])
    for name, td, col, vft in rows:
        cls = name[4:-2] if name.startswith(".?AV") else name
        print(f"{cls:<44} td={td:08x} col={col:08x} vft={vft:08x}")
    print(f"# 共 {len(rows)} 条")


if __name__ == "__main__":
    main()
