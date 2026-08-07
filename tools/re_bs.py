#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
re_bs.py —— 炮炮火枪手脱壳镜像分析工具箱（capstone + pefile）

用法（当模块或直接跑子命令）：
    python tools/re_bs.py xref  <va>            # 找所有引用该 va 的地方（call/imm）
    python tools/re_bs.py dis   <va> [count]    # 从 va 反汇编 count 条
    python tools/re_bs.py func  <va> [maxins]   # 从 va 起线性反汇编到 ret/jmp 收尾
    python tools/re_bs.py str   <substr>        # 搜字符串（ascii/utf16），打印 va
    python tools/re_bs.py callers <va>          # 找直接 call <va> 的调用点

镜像事实：
    IMG = re/BigShot_22524.img，平坦内存镜像，文件 offset == RVA
    ImageBase = 0x400000，所以 VA = 0x400000 + off
"""
import os, sys, struct

# Ghidra 自带的 Capstone wheel 安装在仓库内，避免依赖系统 Python 的全局包。
_LOCAL_DEPS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_pydeps")
if os.path.isdir(_LOCAL_DEPS):
    sys.path.insert(0, _LOCAL_DEPS)

import capstone

BASE = 0x400000
IMGP = r"D:\work\popshot\re\BigShot_22524.img"
_img = None

def img():
    global _img
    if _img is None:
        _img = open(IMGP, "rb").read()
    return _img

def va2off(va): return va - BASE
def off2va(off): return off + BASE
def valid_va(va): return BASE <= va < BASE + len(img())

def read(va, n):
    o = va2off(va)
    return img()[o:o+n]

_md = None
def md():
    global _md
    if _md is None:
        _md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        _md.detail = True
    return _md

def dis(va, count=40):
    """从 va 反汇编 count 条，返回 [(va,mnem,opstr,size,bytes)]"""
    code = read(va, count*15)
    out = []
    for insn in md().disasm(code, va):
        out.append((insn.address, insn.mnemonic, insn.op_str, insn.size, insn.bytes))
        if len(out) >= count:
            break
    return out

def print_dis(va, count=40):
    for a, m, o, sz, bs in dis(va, count):
        hexb = " ".join(f"{b:02x}" for b in bs)
        print(f"  {a:08x}: {hexb:<24} {m} {o}")

def func(va, maxins=400):
    """线性反汇编，遇到无条件 ret / jmp 且没有后续 label 时停。粗糙但够用。"""
    out = []
    cur = va
    end = None
    seen_addrs = set()
    for _ in range(maxins):
        d = dis(cur, 1)
        if not d:
            break
        a, m, o, sz, bs = d[0]
        out.append(d[0])
        seen_addrs.add(a)
        # 记录条件跳转/call 的目标，用来判断 ret 后是否还有落点
        if m == "ret" or (m == "jmp" and not o.startswith("dword") and not o.startswith("0x")):
            break
        if m == "jmp":
            # 无条件 jmp：若目标在函数外，通常是函数尾
            break
        cur = a + sz
        if cur - va > 0x2000:
            break
    return out

def print_func(va, maxins=400):
    for a, m, o, sz, bs in func(va, maxins):
        hexb = " ".join(f"{b:02x}" for b in bs)
        print(f"  {a:08x}: {hexb:<24} {m} {o}")

def find_bytes(needle):
    b = img()
    res = []
    i = b.find(needle)
    while i != -1:
        res.append(off2va(i))
        i = b.find(needle, i+1)
    return res

def find_str(substr):
    """搜 ascii 和 utf-16le 两种编码，返回 [(va, 'ascii'/'utf16', preview)]"""
    b = img()
    res = []
    asc = substr.encode("ascii", "ignore")
    for i in find_bytes(asc):
        o = va2off(i)
        # 回溯到字符串起点（前一个非可打印字节之后）
        st = o
        while st > 0 and 0x20 <= b[st-1] < 0x7f:
            st -= 1
        en = o
        while en < len(b) and b[en] != 0:
            en += 1
        res.append((off2va(st), "ascii", b[st:en][:64]))
    w = substr.encode("utf-16le")
    for i in find_bytes(w):
        res.append((i, "utf16", substr[:64].encode()))
    return res

def refs_to(va):
    """
    找所有引用绝对地址 va 的位置：
      - imm32 直接等于 va（push/mov/cmp 等，字节里出现 LE(va)）
      - call rel32 / jmp rel32 目标 == va
    返回 [(loc_va, kind, insn_text)]
    """
    b = img()
    target = struct.pack("<I", va)
    hits = []
    # 1) 立即数直接引用
    for loc in find_bytes(target):
        # 反汇编 loc 前后，找到覆盖这个立即数的指令
        found = None
        for back in range(0, 8):
            start = loc - back
            if not valid_va(start):
                continue
            for a, m, o, sz, bs in dis(start, 1):
                if a <= loc < a + sz and struct.pack("<I", va) in bs:
                    found = (a, m, o, sz)
                    break
            if found:
                break
        if found:
            a, m, o, sz = found
            hits.append((a, "imm", f"{m} {o}"))
        else:
            hits.append((loc, "data", f"(dword @ {loc:08x})"))
    # 2) call/jmp rel32 目标
    # E8 rel32 (call), E9 rel32 (jmp)
    for opc, kind in [(0xE8, "call"), (0xE9, "jmp")]:
        i = 0
        needle = bytes([opc])
        while True:
            j = b.find(needle, i)
            if j == -1:
                break
            i = j + 1
            if j + 5 > len(b):
                continue
            rel = struct.unpack_from("<i", b, j+1)[0]
            tgt = off2va(j) + 5 + rel
            if tgt == va:
                hits.append((off2va(j), kind+"_rel", f"{kind} {va:08x}"))
    return hits

def callers(va):
    return [(a, t) for a, k, t in refs_to(va) if k == "call_rel"]

def _va(s): return int(s, 16) if s.startswith("0x") else int(s, 16)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(0)
    cmd = sys.argv[1]
    if cmd == "xref":
        va = _va(sys.argv[2])
        for a, k, t in refs_to(va):
            print(f"  {a:08x}  [{k}]  {t}")
    elif cmd == "dis":
        va = _va(sys.argv[2]); c = int(sys.argv[3]) if len(sys.argv) > 3 else 40
        print_dis(va, c)
    elif cmd == "func":
        va = _va(sys.argv[2]); c = int(sys.argv[3]) if len(sys.argv) > 3 else 400
        print_func(va, c)
    elif cmd == "str":
        for va, enc, prev in find_str(sys.argv[2]):
            print(f"  {va:08x}  [{enc}]  {prev}")
    elif cmd == "callers":
        va = _va(sys.argv[2])
        for a, t in callers(va):
            print(f"  {a:08x}  {t}")
    else:
        print("unknown cmd", cmd)
