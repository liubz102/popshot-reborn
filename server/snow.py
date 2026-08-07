#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
snow.py —— 《炮炮火枪手》包加密算法（SNOW 2.0）的 Python 复刻

来源：脱壳镜像 re/BigShot_22524.img 里的 SnowCipher 类（RTTI 名 .?AVSnowCipher@@）
  0x5dc7bc  loadkey(key, keysize, iv0..iv3)      ret 0x18
  0x5dbf85  一次生成 16 个密钥流字（写到 state+0x50）
  0x5dd1e1  取下一个密钥流字（index 在 state+0x90，==16 时先生成）
  0x5dd200  Encrypt: dst[i] = src[i] + ks[i]   （32 位加法，不是异或！）
  0x5dd242  Decrypt: dst[i] = src[i] - ks[i]
  ★ 只处理 len//4 个整字，尾部 len%4 字节原样不加密

6 张常量表（各 256 个 dword）直接从镜像里抠出，未做任何"重新推导"：
  T0=0x6d8aa8(alpha)  T1=0x6d8ea8(alpha^-1)
  S0=0x6d92a8 S1=0x6d96a8 S2=0x6d9aa8 S3=0x6d9ea8  （AES S 盒的 T-table，4 个旋转）
"""
import zlib, base64, struct

_TBL_B64 = (
    "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"
    "/2zy/zH5f5j8X03+byH/I8n/0+R/Ovm/n/wvIP9vkf8LyP9P5H8w+R9N/nuQ/4PJ/4Pkfyv5L0D+95H/F8j/7eS/Kvk/l/yPI//d"
    "yP9d5P9A8t+M/B9F/m8i/7+Q/yvI/23kfxr5b07+jyT/35P/veR/Cfm/m/wfSv6PJf/Lyf9i8t+A/A8l/3XJ/wjy34H8/0P++5H/"
    "d8j/fPJ/Avn/gfwvJf8VyP9z5H88+T+T/Lcn/43JfzHyP4z8ryD/15P/6uT/JfK/kPyXI/+vkP915P9K8t+H/Jch/8PJf3Hy35L8"
    "zyX/z5P/ReT/DPL/JvlvSP6PJ/8fkf9fyf8n5P8h8n8j+f+S/O9H/l8j/8+S/1bk/3PyP5X87yL/75H/VeT/N/J/Nvk/iPz3Iv87"
    "yf855L8r+T+F/F9E/nuT/6fI/2ryP5b8byH/p5L/J8n/ZeS/BPkvRf43kP8B5P9m8r+G/D9G/guS/93kvxL5X0b+ryP/r5L/a8l/"
    "efI/ivw3Iv/VyP8X5H87+a9F/o8m/x+S/0nkvxD5P5H81yH/Tch/WfK/kvwPIf+TyX8b8j+R/B9A/i8m/0XIf0XyfxX5/4z830f+"
    "3yf/95D/GuS/M/k/nPz3J/8vk/+e5L/2/wDx//Sm"
)

_raw = zlib.decompress(base64.b64decode(_TBL_B64))
assert len(_raw) == 6144
_T = [list(struct.unpack_from("<256I", _raw, i * 1024)) for i in range(6)]
T0, T1, S0, S1, S2, S3 = _T

M = 0xFFFFFFFF


def _alpha(x):       # alpha * x
    return ((x << 8) & M) ^ T0[x >> 24]


def _alphadiv(x):    # alpha^-1 * x
    return (x >> 8) ^ T1[x & 0xFF]


def _sbox(w):
    return S0[w & 0xFF] ^ S1[(w >> 8) & 0xFF] ^ S2[(w >> 16) & 0xFF] ^ S3[w >> 24]


def key_from_wstr(s):
    """客户端的密钥派生（脱壳镜像 0x5608b0）：
       key[j] = (宽字符 s[j % len] 的低字节 + j) & 0xff , j = 0..15"""
    if isinstance(s, str):
        s = bytes(ord(c) & 0xFF for c in s)     # 只取宽字符的低字节
    n = len(s)
    return bytes((s[j % n] + j) & 0xFF for j in range(16))


def _w32(key, i):
    """原版把 key 声明成了 char*（有符号），编译出的是 movsx —— 这里如实复刻。
       key 字节 <0x80 时与普通大端取字等价。"""
    v = 0
    for k in range(4):
        c = key[i + k]
        if c >= 0x80:
            c -= 0x100
        v = (c & M) if k == 0 else (((v << 8) & M) | (c & M)) & M
    return v & M


class Snow:
    """SnowCipher。m[0..15] 对应客户端 state+0x00..0x3c 的 16 个 dword
       （即 SNOW 2.0 论文里的 s15..s0）。"""

    def __init__(self, key, keysize=128, iv=(0, 0, 0, 0)):
        assert keysize == 128, "客户端只用 128 位（0x560427: push 0x80）"
        w = [_w32(key, 0), _w32(key, 4), _w32(key, 8), _w32(key, 12)]
        m = w + [~x & M for x in w] + w[:] + [~x & M for x in w]
        m[0] ^= iv[0]
        m[3] ^= iv[1]
        m[5] ^= iv[2]
        m[6] ^= iv[3]
        self.m = m
        self.R1 = 0
        self.R2 = 0
        for t in range(32):                       # 32 轮初始化
            i = 15 - (t & 15)
            F = ((self.R1 + m[(i + 1) & 15]) & M) ^ self.R2
            m[i] = _alpha(m[i]) ^ _alphadiv(m[(i - 11) & 15]) ^ m[(i - 2) & 15] ^ F
            tmp = (self.R2 + m[(i - 5) & 15]) & M
            self.R2 = _sbox(self.R1)
            self.R1 = tmp
        self.buf = []
        self.idx = 16                              # 0x5dd1d2: [state+0x90] = 0x10

    def _gen_block(self):
        m, out = self.m, []
        for k in range(16):
            i = 15 - k
            prev = m[(i - 1) & 15]
            m[i] = _alpha(m[i]) ^ _alphadiv(m[(i - 11) & 15]) ^ m[(i - 2) & 15]
            tmp = (self.R2 + m[(i - 5) & 15]) & M
            self.R2 = _sbox(self.R1)
            self.R1 = tmp
            out.append((((m[i] + self.R1) & M) ^ prev ^ self.R2) & M)
        self.buf = out

    def next_word(self):
        if self.idx == 16:
            self._gen_block()
            self.idx = 0
        v = self.buf[self.idx]
        self.idx += 1
        return v

    def keystream(self, nwords):
        return [self.next_word() for _ in range(nwords)]

    def _xcrypt(self, data, sign):
        n = len(data) // 4
        out = bytearray(data)
        for i in range(n):
            v = struct.unpack_from("<I", data, i * 4)[0]
            k = self.next_word()
            struct.pack_into("<I", out, i * 4, (v + sign * k) & M)
        return bytes(out)                          # 尾部 len%4 字节原样

    def encrypt(self, data):
        return self._xcrypt(data, +1)

    def decrypt(self, data):
        return self._xcrypt(data, -1)


if __name__ == "__main__":
    s = Snow(bytes(16))
    print("key=00*16 keystream[:4] =", [hex(x) for x in s.keystream(4)])
