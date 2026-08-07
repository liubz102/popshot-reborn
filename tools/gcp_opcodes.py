#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gcp_opcodes.py —— 从脱壳镜像还原「客户端 -> 服务端」的 gcp opcode 表

原理（FINDINGS §43/§45）：
    游戏帧 0xFF 的 opcode 存在包缓冲 +8，由 `RawPacket::SetType(u16)` = 0x5bba0a 写入。
    每个 Packet_gcp* 的序列化函数开头都是固定形状：
        push <opcode>
        mov  ecx, <packet>
        call 0x5bba0a
    所以枚举 0x5bba0a 的全部调用点、往前找最近的 `push imm`，就是完整 opcode 表。
    类名靠调用点附近（前 0x120 / 后 0x40 字节内）出现的 Packet_* vftable 立即数对上。

用法：
    python tools/gcp_opcodes.py            # 打印全表
    python tools/gcp_opcodes.py --py       # 打印成 Python dict，可直接贴进 gameserver.py
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import re_bs
import rtti_map

SETTYPE_VA = 0x5bba0a


def build_vft_name_map():
    """Packet_* 的 vftable -> 类名"""
    out = {}
    for td, name in rtti_map.enum_typedescriptors():
        if "Packet_" not in name:
            continue
        cls = name[4:-2] if name.startswith((".?AV", ".?AU")) else name
        for col in rtti_map.col_of(td):
            for vft in rtti_map.vftables_of(col):
                out[vft] = cls
    return out


def scan():
    vft2name = build_vft_name_map()
    rows = []
    for loc, _ in re_bs.callers(SETTYPE_VA):
        # 往前反汇编，取最后一个 push imm（遇到别的 call 就停，避免串到上一个包）
        op = None
        seq = [(a, m, o) for a, m, o, sz, bs in re_bs.dis(loc - 40, 30) if a < loc]
        for a, m, o in reversed(seq):
            if m == "push" and (o.startswith("0x") or o.isdigit()):
                op = int(o, 0)
                break
            if m == "call":
                break
        # 附近的 vftable 赋值 = 这个包的类
        name = None
        for a, m, o, sz, bs in re_bs.dis(loc - 0x120, 90):
            if a > loc + 0x40:
                break
            for v, n in vft2name.items():
                if struct.pack("<I", v) in bs:
                    name = n
        if op is not None:
            rows.append((op, loc, name))
    return sorted(rows)


def main():
    rows = scan()
    if "--py" in sys.argv:
        seen = {}
        for op, loc, name in rows:
            if name and op not in seen:
                seen[op] = name
        print("GCP_NAMES = {")
        for op in sorted(seen):
            print(f'    0x{op:04x}: "{seen[op]}",')
        print("}")
        return
    print(f"# SetType(0x{SETTYPE_VA:x}) 调用点中取到 opcode 的共 {len(rows)} 处")
    for op, loc, name in rows:
        print(f"  0x{op:04x} ({op:5d})  @{loc:08x}  {name or ''}")


if __name__ == "__main__":
    main()
