#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_room_seats.py —— 轮询房间的 6 个座位 + 每座位的「角色对象」指针

FINDINGS §82 的核心疑点：座位数据补齐后点 F5 能进开局握手，但 `0x0412`
（倒计时开始）之后客户端在渲染路径空指针崩溃：

    00409e20  mov edi, [esi+0x1cc]              ; 我的座位号
    00409e34  mov eax, [esi + edi*4 + 0x1d0]    ; ★ 返回 NULL
    0050a368  mov eax, dword ptr [ebx]          ; ebx = 上面那个 NULL -> C0000005

按 §79，`0x0300` 的处理器 `0x40637a` 里 `0x405d8c(1)` 本该逐座位把对象建出来
（`0x405e1c` -> `new(0x7c4)` -> 落到 `+0x1d0+i*4`）。这个探针就是用来**一次分清**
两种可能的：

    A) 压根没建成      -> 全程 obj[me] == 0
    B) 建了又被销毁    -> obj[me] 先非 0，倒计时/换场景时变 0

布局（`[0x72e29c]` = LobbyStage，§78/§79）：

    +0x1cc  我的座位号          +0x34  房主座位号
    +0x40 + i*0x3c   座位 i（0x3c 字节），其中
            +0x00 占用(byte)  +0x01 关闭(byte)  +0x04 昵称(TString)
            +0x08 ?(byte)     +0x0c 角色 id     +0x10 等级(u16)
            +0x14 对端 IP      +0x18 端口(u16)
    +0x1d0 + i*4     座位 i 的「角色对象」指针   ← 本探针的重点
    +0x1e8 + i       座位 i 的「是我」标记（0x405a5d 读它）
    +0x284 + i*4     另一组每座位指针（0x0300 处理器会清它）
    +0x1c4  0x0300 处理器置 1

用法：
    python tools/probe_room_seats.py <pid> [秒数] [采样间隔秒]

只在快照变化时打印一行，所以可以放心开长一点（默认 60 秒）盖住
「建房 -> 进房 -> 按 F5 -> 倒计时 -> 崩溃」整个窗口。
"""
import ctypes as C
import sys
import time
from ctypes import wintypes as W

k32 = C.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.restype = W.HANDLE
k32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
k32.ReadProcessMemory.argtypes = [W.HANDLE, W.LPCVOID, W.LPVOID,
                                  C.c_size_t, C.POINTER(C.c_size_t)]

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400

LOBBY_STAGE = 0x72E29C
SEAT_BASE = 0x40
SEAT_STRIDE = 0x3C
SEAT_COUNT = 6
OBJ_BASE = 0x1D0


def read(h, addr, n):
    buf = (C.c_ubyte * n)()
    got = C.c_size_t()
    if not addr or not k32.ReadProcessMemory(h, W.LPCVOID(addr), buf, n,
                                             C.byref(got)):
        return None
    return bytes(buf)


def u32(h, addr):
    b = read(h, addr, 4)
    return None if b is None else int.from_bytes(b, "little")


def u16(h, addr):
    b = read(h, addr, 2)
    return None if b is None else int.from_bytes(b, "little")


def u8(h, addr):
    b = read(h, addr, 1)
    return None if b is None else b[0]


def tstring(h, addr, maxchars=32):
    """客户端的 str::TString<wchar_t>：对象第 1 个 dword 是字符指针。"""
    ptr = u32(h, addr)
    if not ptr:
        return ""
    b = read(h, ptr, maxchars * 2)
    if b is None:
        return "<读不到>"
    out = []
    for i in range(0, len(b) - 1, 2):
        c = b[i] | (b[i + 1] << 8)
        if c == 0:
            break
        out.append(chr(c))
    return "".join(out)


def snapshot(h):
    lobby = u32(h, LOBBY_STAGE)
    if not lobby:
        return None
    me = u32(h, lobby + 0x1CC)
    host = u32(h, lobby + 0x34)
    flag_1c4 = u8(h, lobby + 0x1C4)
    seats = []
    for i in range(SEAT_COUNT):
        s = lobby + SEAT_BASE + i * SEAT_STRIDE
        seats.append((
            u8(h, s + 0x00),                    # 占用
            u8(h, s + 0x01),                    # 关闭
            tstring(h, s + 0x04),               # 昵称
            u32(h, s + 0x0C),                   # 角色 id
            u16(h, s + 0x10),                   # 等级
            u32(h, s + 0x14),                   # 对端 IP
            u32(h, lobby + OBJ_BASE + i * 4),   # ★ 角色对象指针
            u8(h, lobby + 0x1E8 + i),           # 「是我」标记
        ))
    return (lobby, me, host, flag_1c4, tuple(seats))


def render(snap):
    lobby, me, host, flag_1c4, seats = snap
    lines = [f"LobbyStage={lobby:#x} me={me} host={host} [+0x1c4]={flag_1c4}"]
    for i, (occ, closed, nick, cid, lvl, ip, obj, mine) in enumerate(seats):
        if not occ and not obj:
            continue
        ips = ".".join(str(b) for b in ip.to_bytes(4, "little")) if ip else "-"
        mark = " ★我" if i == me else ""
        warn = "  ← 角色对象为 NULL!" if occ and not obj else ""
        lines.append(
            f"  座位{i}{mark} 占用={occ} 关闭={closed} 昵称={nick!r} "
            f"角色id={cid} 等级={lvl} IP={ips} 对象={obj or 0:#x} "
            f"我标记={mine}{warn}")
    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    pid = int(sys.argv[1])
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
    step = float(sys.argv[3]) if len(sys.argv) > 3 else 0.2

    h = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        print(f"OpenProcess({pid}) 失败: {C.get_last_error()}")
        return

    last = None
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            snap = snapshot(h)
        except Exception as error:            # 进程正在退出时读到一半很正常
            print(f"[{time.strftime('%H:%M:%S')}] 读取异常: {error!r}")
            break
        if snap != last:
            ts = time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
            if snap is None:
                print(f"[{ts}] LobbyStage 还没建（[0x72e29c] = 0）")
            else:
                print(f"[{ts}] " + render(snap))
            last = snap
        time.sleep(step)
    print("采样结束")


if __name__ == "__main__":
    main()
