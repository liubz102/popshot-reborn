#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nmco_opcodes.py —— 从 nmconew.dll 还原 Nexon NMCO 的「opcode ↔ 消息类」全表

原理（会话 04 逆出来的）：
  * 所有线上消息类都派生自 CNMFunc。基类构造 `CNMFunc::CNMFunc(int opcode)`
    在 va=0x100980f0，它把 opcode 写进 `this+0x10`。
  * 派生类构造的形状固定：
        push <opcode> ; mov ecx,this ; call 0x100980f0 ; ... ; mov [this], <派生虚表>
  * 每个类的虚表**最后一槽是 GetName()**，函数体就是
        push ebp; mov ebp,esp; push ecx; mov [ebp-4],ecx; mov eax,<类名字符串>; ...
    —— 于是类名可以直接读出来。
  * 发送时 `this+0x10` 被写进帧的 off2..3（见 va=0x100982e3 附近）。

用法：
    python tools/nmco_opcodes.py [dll路径]
"""
import struct
import sys

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
from re_pe import PE

CTOR = 0x100980f0          # CNMFunc::CNMFunc(int opcode)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else \
        r"D:\work\popshot\game_org\Popshot\nmconew.dll"
    pe = PE(path)
    b = pe.b
    lo = pe.base + 0x1000
    hi = lo + 0xceaec

    def is_code(v):
        return lo <= v < hi

    # --- 1. 找所有 call CTOR ---
    sites = []
    i = 0
    while True:
        j = b.find(b"\xe8", i)
        if j < 0 or j + 5 > len(b):
            break
        i = j + 1
        va = pe.off2va(j)
        if va is None or not is_code(va):
            continue
        if va + 5 + struct.unpack_from("<i", b, j + 1)[0] == CTOR:
            sites.append((va, j))

    # --- 2. 每个调用点：往前找 push <opcode>，往后找 mov [reg], <虚表> ---
    def name_of_vtable(vt):
        o = pe.va2off(vt)
        if o is None:
            return None, 0
        slots = []
        k = 0
        while True:
            v = struct.unpack_from("<I", b, o + k * 4)[0]
            if not is_code(v):
                break
            slots.append(v)
            k += 1
            if k > 64:
                break
        if not slots:
            return None, 0
        # 最后一槽 = GetName：55 8b ec 51 89 4d fc b8 <imm32>
        fo = pe.va2off(slots[-1])
        if b[fo:fo + 3] == b"\x55\x8b\xec" and b[fo + 7] == 0xB8:
            sva = struct.unpack_from("<I", b, fo + 8)[0]
            so = pe.va2off(sva)
            if so is not None:
                end = b.find(b"\0", so)
                if 0 < end - so < 80:
                    return b[so:end].decode("latin1"), len(slots)
        return None, len(slots)

    rows = []
    for va, off in sites:
        opcode = None
        for back in range(2, 24):
            p = off - back
            if b[p] == 0x6A and back >= 2:                 # push imm8
                opcode = b[p + 1]
                break
            if b[p] == 0x68 and back >= 5:                 # push imm32
                v = struct.unpack_from("<I", b, p + 1)[0]
                if v < 0x400:
                    opcode = v
                    break
        vt = None
        for fwd in range(0, 48):
            p = off + 5 + fwd
            if b[p] == 0xC7 and b[p + 1] in (0x00, 0x01, 0x02, 0x03, 0x06, 0x07):
                v = struct.unpack_from("<I", b, p + 2)[0]
                if pe.va2off(v) is not None and not is_code(v):
                    vt = v
                    break
        nm, nslots = name_of_vtable(vt) if vt else (None, 0)
        rows.append((opcode, nm, vt, va, nslots))

    rows.sort(key=lambda r: (r[0] if r[0] is not None else 9999, r[3]))
    print(f"# nmconew.dll 的 NMCO 消息 opcode 表（从 CNMFunc::CNMFunc 调用点还原）")
    print(f"# 共 {len(rows)} 个派生类构造\n")
    print(f"{'opcode':>8}  {'类名':<34} {'虚表':<10} {'构造函数':<10} 槽数")
    for op, nm, vt, va, ns in rows:
        ops = f"0x{op:04x}" if op is not None else "  ??  "
        print(f"{ops:>8}  {(nm or '?'):<34} {vt and '%08x' % vt or '-':<10} "
              f"{va:08x}   {ns}")


if __name__ == "__main__":
    main()
