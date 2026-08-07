#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
re_pe.py —— 通用 PE 静态分析工具箱（capstone + 自解析节表）

和 re_bs.py 的区别：re_bs.py 只服务于那份「file offset == RVA」的平坦内存转储，
这个工具处理**磁盘上的正常 PE**（节的 RAW 与 VA 不一致），例如 nmconew.dll / NMService.exe。

用法：
    python tools/re_pe.py <pe路径> info
    python tools/re_pe.py <pe路径> dis   <va> [count]
    python tools/re_pe.py <pe路径> xref  <va>
    python tools/re_pe.py <pe路径> str   <substr>
    python tools/re_pe.py <pe路径> off2va <fileoff>
    python tools/re_pe.py <pe路径> va2off <va>
"""
import sys, struct
import capstone


class PE:
    def __init__(self, path):
        self.path = path
        self.b = open(path, "rb").read()
        b = self.b
        pe = struct.unpack_from("<I", b, 0x3C)[0]
        self.machine = struct.unpack_from("<H", b, pe + 4)[0]
        nsec = struct.unpack_from("<H", b, pe + 6)[0]
        optsz = struct.unpack_from("<H", b, pe + 20)[0]
        magic = struct.unpack_from("<H", b, pe + 24)[0]
        self.base = struct.unpack_from("<I" if magic == 0x10B else "<Q",
                                       b, pe + 24 + (28 if magic == 0x10B else 24))[0]
        self.ep = struct.unpack_from("<I", b, pe + 24 + 16)[0]
        self.secs = []
        for i in range(nsec):
            o = pe + 24 + optsz + i * 40
            nm = b[o:o + 8].rstrip(b"\0").decode("latin1")
            vs, va, rs, ro = struct.unpack_from("<IIII", b, o + 8)
            self.secs.append((nm, va, vs, ro, rs))

    def off2va(self, off):
        for nm, va, vs, ro, rs in self.secs:
            if ro <= off < ro + rs:
                return self.base + va + (off - ro)
        return None

    def va2off(self, va):
        r = va - self.base
        for nm, sva, vs, ro, rs in self.secs:
            if sva <= r < sva + max(vs, rs):
                d = r - sva
                return ro + d if d < rs else None
        return None

    def read(self, va, n):
        o = self.va2off(va)
        if o is None:
            return b""
        return self.b[o:o + n]

    def find_bytes(self, needle):
        """返回所有命中处的 VA"""
        res, i = [], self.b.find(needle)
        while i != -1:
            v = self.off2va(i)
            if v is not None:
                res.append(v)
            i = self.b.find(needle, i + 1)
        return res

    # ---- 反汇编 ----
    _md = None

    def md(self):
        if PE._md is None:
            PE._md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
            PE._md.detail = True
        return PE._md

    def dis(self, va, count=40):
        code = self.read(va, count * 15)
        out = []
        for insn in self.md().disasm(code, va):
            out.append((insn.address, insn.mnemonic, insn.op_str, insn.size, insn.bytes))
            if len(out) >= count:
                break
        return out

    def print_dis(self, va, count=40):
        for a, m, o, sz, bs in self.dis(va, count):
            print(f"  {a:08x}: {' '.join(f'{x:02x}' for x in bs):<24} {m} {o}")

    def refs_to(self, va):
        """imm32 引用 + call/jmp rel32 目标"""
        hits = []
        for loc in self.find_bytes(struct.pack("<I", va)):
            found = None
            for back in range(0, 9):
                for a, m, o, sz, bs in self.dis(loc - back, 1):
                    if a <= loc < a + sz and struct.pack("<I", va) in bs:
                        found = (a, m, o)
                    break
                if found:
                    break
            hits.append((found[0], "imm", f"{found[1]} {found[2]}") if found
                        else (loc, "data", f"(dword @ {loc:08x})"))
        for opc, kind in [(0xE8, "call"), (0xE9, "jmp")]:
            i = 0
            while True:
                j = self.b.find(bytes([opc]), i)
                if j == -1 or j + 5 > len(self.b):
                    break
                i = j + 1
                jv = self.off2va(j)
                if jv is None:
                    continue
                rel = struct.unpack_from("<i", self.b, j + 1)[0]
                if jv + 5 + rel == va:
                    hits.append((jv, kind + "_rel", f"{kind} {va:08x}"))
        return hits

    def find_str(self, s):
        out = []
        for enc, data in (("ascii", s.encode("latin1")), ("utf16", s.encode("utf-16le"))):
            for va in self.find_bytes(data):
                o = self.va2off(va)
                st = o
                while st > 0 and 0x20 <= self.b[st - 1] < 0x7f:
                    st -= 1
                en = self.b.find(b"\0", o)
                out.append((self.off2va(st), enc, self.b[st:en][:80]))
        return out


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return
    pe = PE(sys.argv[1])
    cmd = sys.argv[2]
    hx = lambda s: int(s, 16)
    if cmd == "info":
        print(f"base={pe.base:08x} ep={pe.base+pe.ep:08x} machine={pe.machine:04x}")
        for nm, va, vs, ro, rs in pe.secs:
            print(f"  {nm:<8} va={pe.base+va:08x} vs={vs:08x} raw={ro:08x} rsz={rs:08x}")
    elif cmd == "dis":
        pe.print_dis(hx(sys.argv[3]), int(sys.argv[4]) if len(sys.argv) > 4 else 40)
    elif cmd == "xref":
        for a, k, t in pe.refs_to(hx(sys.argv[3])):
            print(f"  {a:08x}  [{k}]  {t}")
    elif cmd == "str":
        for va, enc, prev in pe.find_str(sys.argv[3]):
            print(f"  {va:08x}  [{enc}]  {prev}")
    elif cmd == "off2va":
        print(hex(pe.off2va(hx(sys.argv[3]))))
    elif cmd == "va2off":
        print(hex(pe.va2off(hx(sys.argv[3]))))
    else:
        print("unknown", cmd)


if __name__ == "__main__":
    main()
