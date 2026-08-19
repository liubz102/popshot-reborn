#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""epoch_from_dump.py —— 拿逐连接 dump 验证「局号完全由服务端驱动」（§218 / D137）。

客户端的局号 `[GameSession+0x3c]`（= `UdpPacket` 头 `+4`，也就是每座位收包
队列的**纪元号**）不是它自己的私有计数器，每一次变化都是服务端某一发包造成的：

    0x0100 gspRepLogin(result=0)        -> = -1   （新建 GameSession，0x4050f8）
    0x0203 gspRepLeaveSession(result=0) -> = -1   （0x552943 -> 0x4054fa）
    0x0303 gspSession                   -> = 包尾那个 u16（0x556ed1，★ 直接设定）
    0x0400 gspPrepareGame               -> += 1   （0x5517a3）
    0x0403（结算看完回房间）             -> += 1   （0x551900）

这个脚本把 `--verbose` 服务端留下的 `logs/game_*_27799.txt` 走一遍，
按上面这张表**预测**每条连接当前的局号，再和客户端自己盖在每一发 `0x040e`
头里的号逐发对比。

    python tools/epoch_from_dump.py logs/game_*_27799.txt

**对不上的应该只有「换代那一刹那还在途的包」**——客户端在处理我们那一发
换代包之前发出来的，头里还是旧号。它们正是 `RelayServer.deliver()` 要按
「跨代」丢掉的那些（bug调查/9「第二局打不死人」的毒药就是其中一发）。
对不上的数量远大于个位数 = 模型漏了一条换代路径，照着时刻去 dump 里翻。

2026-08-19 现场（`bug调查/9/最后复现/logs-server` 三条连接）实测：
9467 发对上、6 发对不上，6 发全是在途包，其中 dk 那 1 发就是毒心跳。
"""
import re
import struct
import sys

#: `hexdump()` 那种「    0000  ff 00 ...  |....|」的行。
HEX = re.compile(r"^\s{4}[0-9a-f]{4}\s\s((?:[0-9a-f]{2} |\s{3})+)\s*\|")
#: 服务端 -> 客户端：`→ 发出 N 字节明文`（只有 `--verbose` 才有）。
OUT = re.compile(r"^\[(\d\d:\d\d:\d\d\.\d\d\d)\] #\d+ → 发出 (\d+) 字节明文")
#: 客户端 -> 服务端：`★ 游戏包 opcode=0x....`
IN = re.compile(r"^\[(\d\d:\d\d:\d\d\.\d\d\d)\] #\d+ ★ 游戏包 opcode=0x([0-9a-f]{4})")

ADVANCE = {0x0400, 0x0403}
RESET = {0x0100, 0x0203}
ASSIGN = 0x0303
PEER_DATA_UP = 0x040e


def frames(blob):
    """把一串明文拆成 `(opcode, payload)`。0xFE 控制帧跳过。"""
    i = 0
    while i < len(blob):
        if blob[i] == 0xFE and i + 4 <= len(blob):
            n = struct.unpack_from("<H", blob, i + 2)[0] + 4
        elif blob[i] == 0xFF and i + 10 <= len(blob):
            n = struct.unpack_from("<H", blob, i + 2)[0] + 10
            if i + n > len(blob):
                return
            yield (struct.unpack_from("<H", blob, i + 8)[0], blob[i + 10:i + n])
        else:
            return
        i += n


def walk(path):
    """产出 `(方向, 时刻, opcode, 字节)`；方向是 ``"out"`` / ``"in"``。"""
    pending = None
    buf = bytearray()
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            hit = HEX.match(line)
            if hit and pending is not None:
                buf += bytes.fromhex(hit.group(1).replace(" ", ""))
                continue
            if pending is not None:
                yield pending + (bytes(buf),)
                pending, buf = None, bytearray()
            hit = OUT.match(line)
            if hit:
                pending = ("out", hit.group(1), None)
                continue
            hit = IN.match(line)
            if hit:
                pending = ("in", hit.group(1), int(hit.group(2), 16))
    if pending is not None:
        yield pending + (bytes(buf),)


def check(path, show=8):
    value, ok, bad = -1, 0, 0
    mismatches = []
    for kind, when, opcode, blob in walk(path):
        if kind == "out":
            for op, payload in frames(blob):
                if op == ASSIGN and len(payload) >= 2:
                    value = struct.unpack_from("<h", payload,
                                               len(payload) - 2)[0]
                    print("  %s  0x0303 -> 局号 = %d（包尾 u16）" % (when, value))
                elif op in ADVANCE:
                    value += 1
                    print("  %s  0x%04x -> 局号 %d" % (when, op, value))
                elif (op in RESET and len(payload) >= 4
                      and struct.unpack_from("<i", payload, 0)[0] == 0):
                    value = -1
                    print("  %s  0x%04x(result=0) -> 局号 -1" % (when, op))
        elif opcode == PEER_DATA_UP and len(blob) >= 12 and blob[0] == 0xFF:
            said = struct.unpack_from("<H", blob, 4)[0]
            if said == (value & 0xFFFF):
                ok += 1
            else:
                bad += 1
                mismatches.append((when, said, value))
    print("  => 对上 %d 发，对不上 %d 发" % (ok, bad))
    for when, said, value in mismatches[:show]:
        print("     !! %s 客户端说 %d，模型说 %d（换代在途？）"
              % (when, said, value))
    if len(mismatches) > show:
        print("     …… 还有 %d 发" % (len(mismatches) - show))
    return bad


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    total = 0
    for path in argv:
        print("==== %s ====" % path)
        total += check(path)
    print("总计对不上 %d 发" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
