#!/usr/bin/env python3
"""
dump_process.py —— 把一个正在运行的 32 位进程的内存转储出来，供 Ghidra 分析。

为什么不用 Scylla：Scylla 是 GUI 工具，没法脚本化。而我们的目的（见
.claude/DECISIONS.md D004）只是**读代码**，不需要一个能重新运行的脱壳版
—— 那么"把已解壳的内存原样倒出来"就完全够用，而且更可靠。

用法：
    python tools/dump_process.py <pid> [输出目录]

产出（默认到 re/）：
    <exe名>_<pid>.img       主模块的内存镜像（原样，按虚拟地址连续）
    <exe名>_<pid>.exe       PE 化版本：把节表的 RawOffset 改成 VA、RawSize 改成 VirtualSize，
                            这样 Ghidra 能按 PE 正确识别节和入口点
    <exe名>_<pid>_extra/    主模块之外的可执行私有内存区（ASProtect 的 stolen code /
                            API 重定向 thunk 常驻在这里）
    <exe名>_<pid>.map.txt   完整的内存区域清单（VirtualQueryEx 结果）
"""

import ctypes
import ctypes.wintypes as wt
import os
import struct
import sys

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ           = 0x0010

MEM_COMMIT  = 0x1000
MEM_IMAGE   = 0x1000000
MEM_MAPPED  = 0x40000
MEM_PRIVATE = 0x20000

PAGE_NOACCESS = 0x01
PAGE_GUARD    = 0x100

PROT_NAME = {
    0x01: "NOACCESS", 0x02: "READONLY", 0x04: "READWRITE", 0x08: "WRITECOPY",
    0x10: "EXECUTE", 0x20: "EXECUTE_READ", 0x40: "EXECUTE_READWRITE",
    0x80: "EXECUTE_WRITECOPY",
}
EXEC_PROT = {0x10, 0x20, 0x40, 0x80}


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    """64 位进程调用 VirtualQueryEx 观察 WOW64 目标时用的是 64 位版结构。"""
    _fields_ = [
        ("BaseAddress",       ctypes.c_ulonglong),
        ("AllocationBase",    ctypes.c_ulonglong),
        ("AllocationProtect", wt.DWORD),
        ("__alignment1",      wt.DWORD),
        ("RegionSize",        ctypes.c_ulonglong),
        ("State",             wt.DWORD),
        ("Protect",           wt.DWORD),
        ("Type",              wt.DWORD),
        ("__alignment2",      wt.DWORD),
    ]


def read_mem(h, addr, size):
    """分块读，遇到不可读的页就跳过（返回已读到的部分 + 是否完整）。"""
    buf = bytearray()
    chunk = 0x10000
    off = 0
    while off < size:
        n = min(chunk, size - off)
        tmp = ctypes.create_string_buffer(n)
        got = ctypes.c_size_t(0)
        ok = k32.ReadProcessMemory(h, ctypes.c_void_p(addr + off), tmp,
                                   ctypes.c_size_t(n), ctypes.byref(got))
        if ok and got.value:
            buf += tmp.raw[:got.value]
            off += got.value
        else:
            buf += b"\x00" * n          # 读不到就填 0，保持偏移对齐
            off += n
    return bytes(buf)


def enum_regions(h):
    mbi = MEMORY_BASIC_INFORMATION()
    addr = 0
    out = []
    while addr < 0x7FFF0000:
        r = k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                               ctypes.sizeof(mbi))
        if not r:
            break
        out.append((mbi.BaseAddress, mbi.RegionSize, mbi.State, mbi.Protect, mbi.Type))
        nxt = mbi.BaseAddress + mbi.RegionSize
        if nxt <= addr:
            break
        addr = nxt
    return out


def find_main_module(pid):
    """用 ToolHelp 找主模块（32 位进程也能枚举到）。返回 (base, size, path)。"""
    TH32CS_SNAPMODULE = 0x08
    TH32CS_SNAPMODULE32 = 0x10

    class MODULEENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD),
            ("th32ProcessID", wt.DWORD), ("GlblcntUsage", wt.DWORD),
            ("ProccntUsage", wt.DWORD), ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
            ("modBaseSize", wt.DWORD), ("hModule", wt.HMODULE),
            ("szModule", ctypes.c_wchar * 256), ("szExePath", ctypes.c_wchar * 260),
        ]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snap == -1:
        raise OSError("CreateToolhelp32Snapshot 失败: %d" % ctypes.get_last_error())
    me = MODULEENTRY32W()
    me.dwSize = ctypes.sizeof(me)
    k32.Module32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
    if not k32.Module32FirstW(snap, ctypes.byref(me)):
        raise OSError("Module32FirstW 失败")
    base = ctypes.cast(me.modBaseAddr, ctypes.c_void_p).value
    return base, me.modBaseSize, me.szExePath


def pe_ify(img, base):
    """把内存镜像改造成 Ghidra 友好的 PE：
       PointerToRawData = VirtualAddress, SizeOfRawData = VirtualSize。"""
    out = bytearray(img)
    if out[:2] != b"MZ":
        return None
    pe = struct.unpack_from("<I", out, 0x3C)[0]
    if out[pe:pe + 4] != b"PE\0\0":
        return None
    nsec = struct.unpack_from("<H", out, pe + 6)[0]
    optsz = struct.unpack_from("<H", out, pe + 20)[0]
    sect = pe + 24 + optsz
    for i in range(nsec):
        o = sect + i * 40
        vsize, vaddr = struct.unpack_from("<II", out, o + 8)
        # 内存里节是按 VA 展开的，原始偏移直接等于 VA
        struct.pack_into("<II", out, o + 16, vsize, vaddr)
    # FileAlignment 设成和 SectionAlignment 一致，避免解析器纠结
    salign = struct.unpack_from("<I", out, pe + 24 + 32)[0]
    struct.pack_into("<I", out, pe + 24 + 36, salign)
    return bytes(out)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    pid = int(sys.argv[1])
    outdir = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "re")
    os.makedirs(outdir, exist_ok=True)

    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        print("OpenProcess 失败: %d（需要管理员权限？）" % ctypes.get_last_error())
        return 1

    base, size, path = find_main_module(pid)
    name = os.path.splitext(os.path.basename(path))[0]
    print("主模块 %s  base=0x%X  size=0x%X" % (path, base, size))

    # --- 主模块镜像 ---
    img = read_mem(h, base, size)
    img_path = os.path.join(outdir, "%s_%d.img" % (name, pid))
    with open(img_path, "wb") as f:
        f.write(img)
    print("写出 %s  (%d 字节)" % (img_path, len(img)))

    pe = pe_ify(img, base)
    if pe:
        exe_path = os.path.join(outdir, "%s_%d.exe" % (name, pid))
        with open(exe_path, "wb") as f:
            f.write(pe)
        print("写出 %s  (PE 化，Ghidra 直接开)" % exe_path)
    else:
        print("！内存里不是有效 PE 头，只能用 .img 按裸二进制载入 Ghidra（基址 0x%X）" % base)

    # --- 内存区域清单 + 主模块之外的可执行私有内存 ---
    regions = enum_regions(h)
    map_path = os.path.join(outdir, "%s_%d.map.txt" % (name, pid))
    extradir = os.path.join(outdir, "%s_%d_extra" % (name, pid))
    n_extra = 0
    with open(map_path, "w", encoding="utf-8") as f:
        f.write("base             size       state    protect              type\n")
        for b, sz, state, prot, typ in regions:
            if state != MEM_COMMIT:
                continue
            tname = {MEM_IMAGE: "IMAGE", MEM_MAPPED: "MAPPED", MEM_PRIVATE: "PRIVATE"}.get(typ, str(typ))
            f.write("%016X %010X COMMIT   %-20s %s\n"
                    % (b, sz, PROT_NAME.get(prot & 0xFF, hex(prot)), tname))
            # ASProtect 的 stolen code / thunk 通常在主模块之外的可执行私有页里
            if (typ == MEM_PRIVATE and (prot & 0xFF) in EXEC_PROT
                    and not (b <= base < b + sz) and sz <= 0x400000):
                os.makedirs(extradir, exist_ok=True)
                data = read_mem(h, b, sz)
                with open(os.path.join(extradir, "%08X_%X.bin" % (b, sz)), "wb") as g:
                    g.write(data)
                n_extra += 1
    print("写出 %s（%d 个已提交区域）" % (map_path, sum(1 for r in regions if r[2] == MEM_COMMIT)))
    if n_extra:
        print("写出 %s（%d 个主模块外的可执行私有区）" % (extradir, n_extra))

    k32.CloseHandle(h)
    return 0


if __name__ == "__main__":
    sys.exit(main())
