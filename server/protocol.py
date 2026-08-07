#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
protocol.py —— 《炮炮火枪手》客户端 ↔ 认证服（47611）的线上协议

**本文件的字段与算法全部从静态代码推导，不是从抓包拟合的**（会话 04）。
来源：`game_org/Popshot/nmconew.dll`（Nexon Messenger，未加壳），
客户端 `BigShot.exe` 静态链接了同一套 Nexon NMCO 传输库。

    0x1008c180  解帧（读 12 字节头）
    0x1008cbb0  组帧（写 16 字节头）
    0x1008c180 的上层 0x1008bdb0：校验首字节必须是 0x18
    0x1008f270  加密    C[i] = P[i] ^ P[i-1] ^ key ^ Fw[i&15]
    0x1008f2f0  解密    P[i] = C[i] ^ P[i-1] ^ key ^ Fw[i&15]
    0x100ef548  64 字节常量表 F（16 个小端 dword）

会话 03 曾用差分反解出同一张 F 表，与这里从 DLL 直接读出的 **64/64 逐字节相同**，
互为独立验证。

帧格式
------
    [0..1]  u16 BE   = len(NMCO 消息)   即总长-4
    [2..3]  u16 BE   = opcode           （0x000f = 登录请求）
    [4..]   NMCO 消息：
        [0]      0x18            固定 tag，客户端会校验
        [1..3]   u24 BE = 载荷长 + 12
        [4]      flags           bit1(0x02)=加密  bit2(0x04)=分片续传
        [5..7]   u24 BE = 载荷长
        [8..11]  u32 BE 会话密钥
        [12..15] u32 BE 消息 ID（抓到的一直是 0x7d4bb435）
        [16..]   载荷

加密只处理载荷前 `len//4*4` 个字节（整 dword），**尾部余数原样明文**
—— 这就是抓包里末尾那两个 00 的由来，不是什么“明文尾部字段”。
"""
import os
import struct

# --- 64 字节常量表，直接从 nmconew.dll 的 .data 读出（VA 0x100ef548 / 文件偏移 0xef548）---
F = bytes.fromhex(
    "7815fc40" "1f6c3b11" "19ca8983" "d86c19e2" "89149074" "6615ab4a"
    "a0128c7b" "cdff1800" "4b70abcc" "0f8c5a7b" "91b813aa" "079841de"
    "aebcff12" "34ba5f5f" "99acf510" "01ddc1b1"
)
assert len(F) == 64
FW = [struct.unpack_from("<I", F, i * 4)[0] for i in range(16)]

TAG = 0x18            # NMCO 消息首字节，客户端 0x1008bdb0 处硬校验
FLAG_ENCRYPTED = 0x02
FLAG_CONTINUED = 0x04
DEFAULT_MSG_ID = 0x7d4bb435   # 抓包里恒定，含义未知；应答先原样回显

OPCODE_LOGIN = 0x000f


# ---------------------------------------------------------------- 加解密
def decrypt(buf, key):
    """P[i] = C[i] ^ P[i-1] ^ key ^ Fw[i&15]（dword 小端，尾部余数不动）"""
    b = bytearray(buf)
    prev = 0
    for i in range(len(b) >> 2):
        p = struct.unpack_from("<I", b, i * 4)[0] ^ (key ^ FW[i & 15] ^ prev)
        struct.pack_into("<I", b, i * 4, p)
        prev = p
    return bytes(b)


def encrypt(buf, key):
    """C[i] = P[i] ^ P[i-1] ^ key ^ Fw[i&15]（明文反馈）"""
    b = bytearray(buf)
    prev = 0
    for i in range(len(b) >> 2):
        p = struct.unpack_from("<I", b, i * 4)[0]
        struct.pack_into("<I", b, i * 4, p ^ (key ^ FW[i & 15] ^ prev))
        prev = p
    return bytes(b)


# ---------------------------------------------------------------- 组帧 / 解帧
class Frame:
    __slots__ = ("opcode", "payload", "key", "msg_id", "flags")

    def __init__(self, opcode, payload, key=None, msg_id=DEFAULT_MSG_ID,
                 flags=FLAG_ENCRYPTED):
        self.opcode = opcode
        self.payload = payload          # 明文载荷
        self.key = key
        self.msg_id = msg_id
        self.flags = flags

    def __repr__(self):
        return (f"<Frame op=0x{self.opcode:04x} key=0x{(self.key or 0):08x} "
                f"id=0x{self.msg_id:08x} flags=0x{self.flags:02x} "
                f"载荷{len(self.payload)}字节>")


def frame_len(buf):
    """从流的开头看这一帧总共多少字节；不够判断就回 None"""
    if len(buf) < 4:
        return None
    return struct.unpack_from(">H", buf, 0)[0] + 4


def unpack(pkt):
    """整帧 → Frame（载荷已解密）"""
    if len(pkt) < 20:
        raise ValueError("帧太短: %d" % len(pkt))
    outer = struct.unpack_from(">H", pkt, 0)[0]
    if outer + 4 != len(pkt):
        raise ValueError("外层长度 %d 与实际 %d 不符" % (outer + 4, len(pkt)))
    opcode = struct.unpack_from(">H", pkt, 2)[0]
    m = pkt[4:]
    if m[0] != TAG:
        raise ValueError("NMCO tag 应为 0x18，实际 0x%02x" % m[0])
    L = int.from_bytes(m[1:4], "big")
    flags = m[4]
    plen = int.from_bytes(m[5:8], "big")
    key = int.from_bytes(m[8:12], "big")
    msg_id = int.from_bytes(m[12:16], "big")
    if L != plen + 12:
        raise ValueError("内外长度不一致: L=%d plen=%d" % (L, plen))
    pay = m[16:16 + plen]
    if flags & FLAG_ENCRYPTED:
        pay = decrypt(pay, key)
    return Frame(opcode, pay, key, msg_id, flags)


def pack(frame):
    """Frame → 整帧字节"""
    key = frame.key
    if key is None:
        key = struct.unpack("<I", os.urandom(4))[0]
    pay = frame.payload
    if frame.flags & FLAG_ENCRYPTED:
        pay = encrypt(pay, key)
    plen = len(pay)
    m = (bytes([TAG]) + (plen + 12).to_bytes(3, "big")
         + bytes([frame.flags]) + plen.to_bytes(3, "big")
         + key.to_bytes(4, "big") + frame.msg_id.to_bytes(4, "big") + pay)
    return struct.pack(">HH", len(m), frame.opcode) + m


# ---------------------------------------------------------------- 登录包体
# 载荷（opcode 0x0f）：
#   u16 LE 用户名字符数 / UTF-16LE 用户名
#   u16 LE 密码字符数   / UTF-16LE 密码
#   14 字节尾部（4 个抓包样本完全一致，尚未逐字段解释；
#                最后 2 字节因为 len%4 而没被加密）
LOGIN_TAIL = bytes.fromhex("000003220101560000000000" "0000")


def parse_login(plain):
    n = struct.unpack_from("<H", plain, 0)[0]
    user = plain[2:2 + n * 2].decode("utf-16-le")
    q = 2 + n * 2
    m = struct.unpack_from("<H", plain, q)[0]
    pw = plain[q + 2:q + 2 + m * 2].decode("utf-16-le")
    return user, pw, plain[q + 2 + m * 2:]


def build_login(user, pw, tail=LOGIN_TAIL):
    return (struct.pack("<H", len(user)) + user.encode("utf-16-le")
            + struct.pack("<H", len(pw)) + pw.encode("utf-16-le") + tail)


# ---------------------------------------------------------------- 应答：opcode 0x0c
# CULoginReplyPacket::Deserialize（nmconew.dll va=0x1002e910）按顺序读：
#   int    → this+0x34      结果码
#   string → this+0x38      （缓冲区上限 0x1fff）
#   string → this+0x2037    （缓冲区上限 0x7d1）
#   int    → this+0x2808
#   int    → this+0x280c
#   int    → this+0x2810
# 线上编码：int = 4 字节小端（读取器 0x1008ba60 直接取 dword）；
#           string = u16 小端字符数 + UTF-16LE（与登录请求里抓到的用户名/密码一致）
OPCODE_LOGIN_REPLY = 0x000c


def wstr(s):
    return struct.pack("<H", len(s)) + s.encode("utf-16-le")


def build_login_reply(result=0, s1="", s2="", a=0, b=0, c=0):
    return (struct.pack("<i", result) + wstr(s1) + wstr(s2)
            + struct.pack("<iii", a, b, c))


def parse_login_reply(plain):
    result = struct.unpack_from("<i", plain, 0)[0]
    off = 4
    out = []
    for _ in range(2):
        n = struct.unpack_from("<H", plain, off)[0]
        out.append(plain[off + 2:off + 2 + n * 2].decode("utf-16-le"))
        off += 2 + n * 2
    a, b, c = struct.unpack_from("<iii", plain, off)
    return result, out[0], out[1], a, b, c


# ---------------------------------------------------------------- 应答：opcode 0x2e
# CURequestOldFashionInfoReplyPacket::Deserialize（va=0x10030c10）按顺序读：
#   int    → +0x34        string → +0x38     string → +0x2037   string → +0x4036
#   int    → +0x4068      u16    → +0x406c   u16    → +0x406e   int    → +0x4070
#   i64    → +0x4078      i64    → +0x4080
# 读取原语：0x1002bfe0/0x1002d680=int32、0x10030d20=u16、0x10030d40=8 字节
OPCODE_OLDFASHION = 0x002d
OPCODE_OLDFASHION_REPLY = 0x002e


def build_oldfashion_reply(result=0, s1="", s2="", s3="",
                           a=0, b=0, c=0, d=0, e=0, f=0):
    return (struct.pack("<i", result) + wstr(s1) + wstr(s2) + wstr(s3)
            + struct.pack("<i", a) + struct.pack("<HH", b, c)
            + struct.pack("<i", d) + struct.pack("<qq", e, f))


# ---------------------------------------------------------------- 应答：opcode 0x0e
# CULogoutReplyPacket::Deserialize（va=0x1002f160）只读两个字段：int → +0x34，string → +0x38
OPCODE_LOGOUT = 0x000d
OPCODE_LOGOUT_REPLY = 0x000e


def build_logout_reply(result=0, s1=""):
    return struct.pack("<i", result) + wstr(s1)


def hexdump(b, base=0):
    out = []
    for i in range(0, len(b), 16):
        c = b[i:i + 16]
        h = " ".join(f"{x:02x}" for x in c)
        a = "".join(chr(x) if 0x20 <= x < 0x7f else "." for x in c)
        out.append(f"  {base+i:04x}  {h:<47}  |{a}|")
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        for fn in sys.argv[1:]:
            raw = open(fn, "rb").read()
            off = 0
            while off < len(raw):
                n = frame_len(raw[off:])
                if n is None or off + n > len(raw):
                    print(f"{fn}: off {off} 起剩余 {len(raw)-off} 字节不足一帧")
                    break
                f = unpack(raw[off:off + n])
                print(f"{fn} @off {off}: {f}")
                print(hexdump(f.payload))
                if f.opcode == OPCODE_LOGIN:
                    u, p, t = parse_login(f.payload)
                    print(f"   ★ 用户名={u!r} 密码={p!r} 尾部={t.hex(' ')}")
                off += n
    else:
        want = ("0042000f1800003e0200003 25e9054987d4bb435".replace(" ", "")
                + "e841181eea38ac4f909e1fdd473899bc10406d2af84156143f460d25"
                  "56ab9d5ed0242992e4d8ba0708edd6d69ecd87800000")
        got = pack(Frame(OPCODE_LOGIN, build_login("testuser", "testpass"),
                         key=0x5e905498)).hex()
        print("组帧与真实抓包逐字节一致:", got == want)
        f = unpack(bytes.fromhex(want))
        print(f, parse_login(f.payload))
