#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""存档密封 —— 把账号里的游戏数据封成一段玩家改不动的密文。

「存档转移助手」导出的文件里，**只有用户名 / 昵称 / 密码三项是明文**，
玩家可以用记事本自己改；其余全部字段（等级 / 经验 / 金币 / 仓库 / 装备 /
材料 / 礼物盒 …）由这个模块封成一段 base64 字符串，改一个字就导不回去。

★★★ 诚实交代（和 D84 / D85 一个调子，别指望它是安全边界）

这把钥匙就明写在这份源码里，而这份源码**随客户端包一起发出去** ——
单机模式（「本机服务器」）跑的就是包里这套 `server/`。所以它做到的只有一档：

* 「把下载的 JSON 用记事本改一改」 —— **从此不可能**（改一个字节 MAC 就不过）；
* 会读 Python 的人、或者起本机服务器改完 `accounts.json` 再导出的人
  —— **拦不住，也不打算拦**。

⇒ 不要在这上面加自校验 / 反调试，那条路 D84 已经论证过不划算。
玩家改数值的正当出路已经有了：找服主在 GM 管理页 `/admin` 里改。

---

## 构造

标准库里**没有 AES**（全项目零第三方依赖，两个内置运行时的 `site-packages`
都是空的）。能当伪随机函数用的只有 `hmac` + `hashlib`，所以走的是唯一一条
不需要自己发明东西的经典路线：

| 环节 | 做法 |
|---|---|
| 派生子密钥 | HKDF（RFC 5869）：`Extract` = `HMAC(salt=nonce, msg=主密钥)`，`Expand` 单块 = `HMAC(prk, info‖0x01)` |
| 加密 | HMAC-SHA256 当 PRF 跑计数器模式：`keystream_i = HMAC(k_enc, nonce‖u32be(i))` |
| 认证 | `tag = HMAC-SHA256(k_mac, 头部‖密文)`，**encrypt-then-MAC** |

二进制布局（base64 之前，固定开销 57 字节）：

```text
偏移  长度  字段
  0    4   magic     b"PSSV"，永不变
  4    1   version   格式版本
  5    4   key_id    主密钥的 sha256 前缀，**不是秘密**，只用来分辨「换了密钥」
  9   16   nonce     每次随机
 25    N   ct        zlib 压缩后的 JSON XOR keystream
25+N  32   tag       HMAC(k_mac, 前 25+N 字节)
```

几个刻意的决定（改之前先读完）：

* **nonce 当 HKDF 的 salt** ⇒ 每份存档一把一次性子密钥，**keystream 永不重用**。
  这一条不能省：主密钥固定，如果 nonce 也固定，两份存档 XOR 一下就露出结构，
  一个**不会读 Python** 的人也能靠「从 A 存档剪一段字节贴到 B 存档」做坏事。
* **先压缩后加密**：`{"count":1,"expires":null}` 在仓库里重复几十遍，压缩收益很大。
  CRIME/BREACH 那类「压缩泄露长度」的攻击需要攻击者能**反复注入并观察**的在线
  预言机，这里是一次性的离线文件，不成立。
* **encrypt-then-MAC，先验 MAC 再解密** ⇒ 被改过的密文在 `zlib` / `json`
  碰到它之前就被挡掉。
* **`json.dumps` 用 `sort_keys=True` + 紧凑分隔符** ⇒ 明文字节确定，
  可以用固定向量把算法钉死（`test_savecrypt.py`，同 `test_versioning` 钉
  `hook_tag()` 的用意）。**算法一旦发出去就改不得** —— 改了全世界已导出的
  存档全部作废。
* **解压必须封顶**，见 `unseal()` 里那段注释。
"""
import base64
import binascii
import hashlib
import hmac
import json
import secrets
import struct
import zlib

#: 密文头部的固定标记。认不出它就根本不是这个模块封的东西。
MAGIC = b"PSSV"

#: 密文自己的格式版本（和外层 JSON 里那个 `popshot_save` 是两回事：
#: 外层那个玩家能改，这一个在 MAC 覆盖范围内，改不动）。
FORMAT_VERSION = 2

NONCE_BYTES = 16
TAG_BYTES = 32
KEY_ID_BYTES = 4
HEADER_BYTES = len(MAGIC) + 1 + KEY_ID_BYTES + NONCE_BYTES

#: 解压出来的明文上限。一个满装备满仓库的账号实测才几十 KB，
#: 4 MB 是天花板不是工作值。
MAX_PLAIN_BYTES = 4 << 20

_HKDF_ENC_INFO = b"popshot-save/enc/1\x01"
_HKDF_MAC_INFO = b"popshot-save/mac/1\x01"
_KEY_ID_INFO = b"popshot-save/keyid/1"


class SaveCryptError(Exception):
    """密封 / 解封失败。``code`` 供调用方分支，``message`` 是给玩家看的中文。

    ★ `message` 里**永远不要回显密文片段** —— `web/server.py` 的 500 兜底会
    把异常 `repr` 进日志。
    """

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
#  主密钥
#
#  ★ 写法上只做一件事：**别让钥匙以完整形态躺在文件里**，免得记事本搜 "key"
#    或者 `strings savecrypt.py` 一眼看见。下面两段常量异或之后再过一次
#    sha256 才是真正那 32 个字节，文件里出现过的任何一段都不是钥匙本身。
#    —— 这只是混淆，不是安全（理由见文件头）。
#
#  ★★ `_KEYS` 是**元组**，第 0 个是当前用来封存档的那把，后面留给退役的密钥
#     （靠 blob 里的 `key_id` 认）。现在只有一把，但这张表的形状必须现在就留下来：
#     密钥一旦随包发出去就再也换不动了，将来真要换时没有这张表，
#     就等于把全世界已经导出的存档一次性作废。
# ---------------------------------------------------------------------------
_KEY_PART_A = bytes.fromhex("5c487fc43f50f0321babe387fddf03fff63786c431e8100bb940484164d4a8f9")
_KEY_PART_B = bytes.fromhex("69e7e2bb01e05c27d5bf9811643956a0192fd026e634520c4ebb684debb0e5a3")


def _fold(part_a, part_b):
    return hashlib.sha256(bytes(x ^ y for x, y in zip(part_a, part_b))).digest()


_KEYS = (_fold(_KEY_PART_A, _KEY_PART_B),)


def _key_id(master):
    """主密钥的公开指纹。**不是秘密** —— 只用来把「被改过」和「换了密钥」分开说。"""
    return hashlib.sha256(_KEY_ID_INFO + master).digest()[:KEY_ID_BYTES]


def _subkeys(master, nonce):
    """HKDF：nonce 当 salt 抽一次，再各展开一块，拿到互相独立的加密 / 认证子密钥。"""
    prk = hmac.new(nonce, master, hashlib.sha256).digest()
    return (hmac.new(prk, _HKDF_ENC_INFO, hashlib.sha256).digest(),
            hmac.new(prk, _HKDF_MAC_INFO, hashlib.sha256).digest())


def _keystream(key, nonce, length):
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + struct.pack(">I", counter),
                        hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def _xor(data, stream):
    return bytes(a ^ b for a, b in zip(data, stream))


# ---------------------------------------------------------------------------
#  对外
# ---------------------------------------------------------------------------
def seal(fields):
    """把一个字典封成 base64 密文字符串。"""
    return _seal_with_nonce(fields, secrets.token_bytes(NONCE_BYTES))


def _seal_with_nonce(fields, nonce):
    """`seal()` 的确定性版本 —— **只给测试用**（固定向量钉算法）。"""
    if not isinstance(fields, dict):
        raise SaveCryptError("bad_save_data", "只能密封一个 JSON 对象")
    plain = json.dumps(fields, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")
    body = zlib.compress(plain, 9)
    master = _KEYS[0]
    enc_key, mac_key = _subkeys(master, nonce)
    header = MAGIC + struct.pack("B", FORMAT_VERSION) + _key_id(master) + nonce
    ciphertext = _xor(body, _keystream(enc_key, nonce, len(body)))
    tag = hmac.new(mac_key, header + ciphertext, hashlib.sha256).digest()
    return base64.b64encode(header + ciphertext + tag).decode("ascii")


def unseal(blob):
    """把 `seal()` 的输出还原成字典。任何一种不对都抛 `SaveCryptError`。

    ★★ 校验顺序**就是安全性本身**：走到第 5 步为止，攻击者控制的字节一个都还
    没有被「解释」过（没解压、没解析 JSON）。别图省事把解密提到验 MAC 前面。
    """
    if not isinstance(blob, str) or not blob.strip():
        raise SaveCryptError("bad_save_data", _BAD_DATA_TEXT)
    try:
        raw = base64.b64decode(blob.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise SaveCryptError("bad_save_data", _BAD_DATA_TEXT) from None
    if len(raw) < HEADER_BYTES + TAG_BYTES:
        raise SaveCryptError("bad_save_data", _BAD_DATA_TEXT)
    if raw[:len(MAGIC)] != MAGIC:
        raise SaveCryptError("bad_save_data", _BAD_DATA_TEXT)

    version = raw[len(MAGIC)]
    if version != FORMAT_VERSION:
        raise SaveCryptError(
            "future_save",
            "这份存档是另一个版本的服务端导出的，这台服务器看不懂。"
            "请让服主把服务端升级到和导出那台一样的版本。")

    key_id = raw[len(MAGIC) + 1:len(MAGIC) + 1 + KEY_ID_BYTES]
    nonce = raw[len(MAGIC) + 1 + KEY_ID_BYTES:HEADER_BYTES]
    header, ciphertext, tag = (raw[:HEADER_BYTES],
                               raw[HEADER_BYTES:-TAG_BYTES],
                               raw[-TAG_BYTES:])
    master = None
    for candidate in _KEYS:
        if hmac.compare_digest(_key_id(candidate), key_id):
            master = candidate
            break
    if master is None:
        # ★ 只是**诊断提示**，不是安全边界：拿着任意密钥的人可以把 key_id 写成
        #   我们的，那样他会得到「被改过」而不是这一句。正牌文件的 key_id 在
        #   MAC 覆盖范围内，改不动 —— 值这 4 个字节。
        raise SaveCryptError(
            "save_other_key",
            "这份存档是另一套服务端导出的，这台服务器解不开。"
            "请在导出它的那台服务器上导入，或者请服主把两边的服务端版本对齐。")

    enc_key, mac_key = _subkeys(master, nonce)
    if not hmac.compare_digest(
            tag, hmac.new(mac_key, header + ciphertext, hashlib.sha256).digest()):
        raise SaveCryptError("save_tampered", _TAMPERED_TEXT)

    # --- MAC 过了，下面才允许「解释」这些字节 ---------------------------------
    body = _xor(ciphertext, _keystream(enc_key, nonce, len(ciphertext)))
    # ★★ 解压必须封顶。「先验 MAC 再解压就安全了」在这儿**不成立** ——
    #    密钥随包发到每个玩家机器上，攻击者能给任意内容算出**合法的 MAC**，
    #    于是 1 MB 的请求体里塞得下一颗能把云服内存吃光的 zlib 炸弹。
    #    判据用 `eof`（压缩流自己的结束标记，是事件不是阈值，铁律 10），
    #    它同时也能抓住「被截断的流」。实测：解到刚好等于上限且流真的结束时
    #    `eof` 仍是 True，所以这里不需要 ±1。
    try:
        decompressor = zlib.decompressobj()
        plain = decompressor.decompress(body, MAX_PLAIN_BYTES)
        if not decompressor.eof:
            raise SaveCryptError("bad_save_data", _BAD_DATA_TEXT)
        fields = json.loads(plain.decode("utf-8"))
    except SaveCryptError:
        raise
    except Exception:
        # zlib.error / UnicodeDecodeError / JSONDecodeError / 深嵌套的
        # RecursionError —— 一律收成一句人话，**绝不能变成 500**。
        raise SaveCryptError("bad_save_data", _BAD_DATA_TEXT) from None
    if not isinstance(fields, dict):
        raise SaveCryptError("bad_save_data", _BAD_DATA_TEXT)
    return fields


_BAD_DATA_TEXT = (
    "存档里的游戏数据那一段（data）坏了或者不完整 —— 常见原因是复制粘贴时被截断、"
    "或者用别的程序另存过。请重新导出一份，把整个文件原样上传。")

_TAMPERED_TEXT = (
    "存档里的游戏数据被改过，导不进来。这份文件里只有 username（用户名）、"
    "nickname（昵称）、password（密码）三项可以自己改，data 那一长串必须原样保留。"
    "要调等级 / 金币 / 材料 / 仓库，请找服主在 GM 管理页里改。")
