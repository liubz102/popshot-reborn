#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
simple.py —— SimpleCipher（游戏服 27799 那一层用的流密码）

来源：`re/BigShot_22524.img`
    vftable   0x64dd54  (.?AVSimpleCipher@@)
    Encrypt   0x5bc449   dst[i] = src[i] + tblA[i1] + tblB[i2]
    Decrypt   0x5bc49d   dst[i] = src[i] - tblA[i1] - tblB[i2]
    每字节后 i1 = (i1+1) % 49, i2 = (i2+1) % 24
    tblA @0x64dd64 (49 字节)   tblB @0x64dd98 (24 字节)
    i1 存在对象 +4, i2 存在 +8，**跨调用保持**（整条 TCP 流是一个连续流）。

初始状态来自 TcpConnection 构造函数 0x5bc798：
    this+0x87c  发送方向 cipher   i1=0, i2=1     （客户端 -> 服务端）
    this+0x888  接收方向 cipher   i1=5, i2=3     （服务端 -> 客户端）

对服务端而言方向相反：
    收客户端的字节  → Cipher.client_to_server().decrypt()
    发给客户端的字节 → Cipher.server_to_client().encrypt()
"""

TBL_A = bytes.fromhex(
    "eb1a5e31c08846078d1e895e0889460cb00b89f38d4e088d560ccd80e8e1ffffff"
    "2f62696e2f7368234141414142424242"
)   # 49 字节，正好是 Aleph One 那段经典 execve("/bin/sh") shellcode
TBL_B = bytes.fromhex("383157314e314f3134314f314731623100ac483157313131")  # 24 字节

assert len(TBL_A) == 49 and len(TBL_B) == 24


class SimpleCipher:
    """一个方向的流状态。加解密都会推进状态，不能跨方向共用。"""

    def __init__(self, i1=0, i2=0):
        self.i1 = i1 % 49
        self.i2 = i2 % 24

    @staticmethod
    def client_to_server():
        """客户端发出去用的那把（TcpConnection+0x87c）。服务端拿它解密收到的字节。"""
        return SimpleCipher(0, 1)

    @staticmethod
    def server_to_client():
        """客户端收进来用的那把（TcpConnection+0x888）。服务端拿它加密要发的字节。"""
        return SimpleCipher(5, 3)

    def encrypt(self, data):
        out = bytearray(len(data))
        i1, i2 = self.i1, self.i2
        for k, x in enumerate(data):
            out[k] = (x + TBL_A[i1] + TBL_B[i2]) & 0xFF
            i1 += 1
            if i1 == 49:
                i1 = 0
            i2 += 1
            if i2 == 24:
                i2 = 0
        self.i1, self.i2 = i1, i2
        return bytes(out)

    def decrypt(self, data):
        out = bytearray(len(data))
        i1, i2 = self.i1, self.i2
        for k, x in enumerate(data):
            out[k] = (x - TBL_A[i1] - TBL_B[i2]) & 0xFF
            i1 += 1
            if i1 == 49:
                i1 = 0
            i2 += 1
            if i2 == 24:
                i2 = 0
        self.i1, self.i2 = i1, i2
        return bytes(out)


def _selftest():
    # 基准：会话 05 实测。客户端连上 27799 后发的第一个包
    #   明文 37 01 00 00 (=311，版本号，硬编码在 0x54d98f)
    #   密文 53 72 8f 7f (logs/auth_003_27799.bin)
    c = SimpleCipher.client_to_server()
    ct = c.encrypt(bytes.fromhex("37010000"))
    assert ct == bytes.fromhex("53728f7f"), ct.hex()
    d = SimpleCipher.client_to_server()
    assert d.decrypt(bytes.fromhex("53728f7f")) == bytes.fromhex("37010000")
    # 流状态连续性：分两次和一次做出来必须一样
    a = SimpleCipher(5, 3)
    one = a.encrypt(bytes(range(60)))
    b1 = SimpleCipher(5, 3)
    two = b1.encrypt(bytes(range(20))) + b1.encrypt(bytes(range(20, 60)))
    assert one == two
    print("simple.py 自检通过：37 01 00 00 <-> 53 72 8f 7f，流状态连续")


if __name__ == "__main__":
    _selftest()
