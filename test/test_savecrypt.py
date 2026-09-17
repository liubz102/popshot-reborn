#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`savecrypt` 的不变式。

★ 为什么单独一个文件：密码学有自己的一套判据（篡改一位必须被抓、同一份数据
两次封出来必须不同、明文不许漏），混进账号语义的用例里两边都说不清。
"""
import base64
import json
import unittest
import zlib

import savecrypt
from savecrypt import SaveCryptError


def seal_bytes(plain, nonce=b"\x11" * savecrypt.NONCE_BYTES, key=None):
    """用模块自己的原语封一段**任意字节**。

    `seal()` 只收 dict、而且一定会先 zlib 压缩，所以「明文是个 JSON 数组」
    「明文是一颗 zlib 炸弹」这类用例正常路径造不出来。这里复用的是
    `savecrypt` 自己的 `_subkeys` / `_keystream` / `_key_id`，
    **不是**把加密方案在测试里重抄一遍。
    """
    import hmac
    import hashlib
    import struct
    master = savecrypt._KEYS[0] if key is None else key
    enc_key, mac_key = savecrypt._subkeys(master, nonce)
    header = (savecrypt.MAGIC + struct.pack("B", savecrypt.FORMAT_VERSION)
              + savecrypt._key_id(master) + nonce)
    ciphertext = savecrypt._xor(
        plain, savecrypt._keystream(enc_key, nonce, len(plain)))
    tag = hmac.new(mac_key, header + ciphertext, hashlib.sha256).digest()
    return base64.b64encode(header + ciphertext + tag).decode("ascii")


def flip(blob, index, value=None):
    """把 base64 解出来的第 `index` 个字节翻一位（或设成 `value`），再封回去。"""
    raw = bytearray(base64.b64decode(blob))
    if value is None:
        raw[index] ^= 1
    else:
        raw[index] = value
    return base64.b64encode(bytes(raw)).decode("ascii")


#: 各段在 base64 解码之后的偏移，和 `savecrypt` 的布局表一一对应。
MAGIC_AT = 0
VERSION_AT = len(savecrypt.MAGIC)
KEY_ID_AT = VERSION_AT + 1
NONCE_AT = KEY_ID_AT + savecrypt.KEY_ID_BYTES
CIPHERTEXT_AT = savecrypt.HEADER_BYTES


class RoundTripTests(unittest.TestCase):
    def test_it_comes_back_exactly(self):
        for fields in ({},
                       {"money": 0},
                       {"nick": "小炮", "note": "带 emoji 的礼物留言 🎁"},
                       {"big": 2 ** 53, "neg": -1, "f": 1.5,
                        "nested": {"a": [1, {"b": None}, True]}},
                       {str(1120041 + i): {"count": i, "expires": None}
                        for i in range(400)}):
            self.assertEqual(fields, savecrypt.unseal(savecrypt.seal(fields)),
                             msg=repr(fields)[:60])

    def test_two_seals_of_the_same_data_differ(self):
        # nonce 每次重取 ⇒ 密文完全不同。这一条不是洁癖：nonce 固定的话
        # keystream 会重用，两份存档 XOR 一下就露出结构，一个不会读 Python
        # 的人也能靠剪贴字节做坏事。
        fields = {"money": 12345}
        first, second = savecrypt.seal(fields), savecrypt.seal(fields)
        self.assertNotEqual(first, second)
        self.assertEqual(savecrypt.unseal(first), savecrypt.unseal(second))

    def test_the_plaintext_never_shows_through(self):
        # 需求「玩家看不懂」的直接判据。
        blob = savecrypt.seal({"money": 987654, "message": "生日快乐",
                               "inventory": {"1120041": {"count": 1}}})
        for secret in ("987654", "生日快乐", "1120041", "money", "inventory"):
            self.assertNotIn(secret, blob, msg=secret)

    def test_the_blob_is_plain_ascii_and_survives_json(self):
        # 它要放进一份 JSON 文件里被浏览器 parse / stringify 一个来回。
        blob = savecrypt.seal({"nick": "小炮"})
        self.assertTrue(blob.isascii())
        self.assertEqual(blob, json.loads(json.dumps({"d": blob}))["d"])

    def test_sealing_anything_but_a_dict_is_refused(self):
        for bad in ([1, 2], "x", None, 7):
            with self.assertRaises(SaveCryptError, msg=repr(bad)):
                savecrypt.seal(bad)


class TamperTests(unittest.TestCase):
    def setUp(self):
        self.blob = savecrypt.seal({"money": 42, "level": 7})

    def test_every_section_is_covered(self):
        cases = [
            ("magic", flip(self.blob, MAGIC_AT), "bad_save_data"),
            ("version", flip(self.blob, VERSION_AT, 99), "future_save"),
            ("key_id", flip(self.blob, KEY_ID_AT), "save_other_key"),
            ("nonce", flip(self.blob, NONCE_AT), "save_tampered"),
            ("ciphertext", flip(self.blob, CIPHERTEXT_AT), "save_tampered"),
            ("tag", flip(self.blob, -1), "save_tampered"),
        ]
        for label, spoiled, want in cases:
            with self.assertRaises(SaveCryptError, msg=label) as ctx:
                savecrypt.unseal(spoiled)
            self.assertEqual(want, ctx.exception.code, msg=label)

    def test_garbage_in_never_leaks_a_raw_exception(self):
        # 这些全都得收成 SaveCryptError，不能漏出 binascii.Error / TypeError
        # —— 漏出去就是注册页一个 500。
        for bad in (None, 7, b"bytes", "", "   ", "不是 base64!!!",
                    "a" * 3, base64.b64encode(b"short").decode("ascii"),
                    self.blob[:20], self.blob[:-4],
                    base64.b64encode(json.dumps({"money": 1}).encode()
                                     ).decode("ascii")):
            with self.assertRaises(SaveCryptError, msg=repr(bad)[:40]):
                savecrypt.unseal(bad)

    def test_a_blob_sealed_with_another_key_is_named_as_such(self):
        other = bytes(b ^ 0xFF for b in savecrypt._KEYS[0])
        foreign = seal_bytes(zlib.compress(b'{"money":999999}'), key=other)
        with self.assertRaises(SaveCryptError) as ctx:
            savecrypt.unseal(foreign)
        self.assertEqual("save_other_key", ctx.exception.code)
        self.assertIn("另一套服务端", ctx.exception.message)

    def test_a_retired_key_still_opens_its_own_saves(self):
        # ★ `_KEYS` 是一张表，不是一个值 —— 将来换密钥时老存档还得开得了。
        # 这条钉的是那个形状（现在表里只有一把）。
        retired = bytes(b ^ 0x5A for b in savecrypt._KEYS[0])
        blob = seal_bytes(zlib.compress(b'{"money":7}'), key=retired)
        saved = savecrypt._KEYS
        try:
            savecrypt._KEYS = saved + (retired,)
            self.assertEqual({"money": 7}, savecrypt.unseal(blob))
        finally:
            savecrypt._KEYS = saved

    def test_the_payload_is_only_interpreted_after_the_mac(self):
        """★★ 解压封顶 —— MAC 在这里保护不了服务端。

        密钥随包发到每个玩家机器上，所以攻击者能给一颗 zlib 炸弹算出**合法的
        MAC**。封顶的判据是压缩流自己的 `eof`（事件，不是阈值，铁律 10）。
        """
        bomb = zlib.compress(b"\0" * (savecrypt.MAX_PLAIN_BYTES * 16), 9)
        self.assertLess(len(bomb), 1 << 20, "炸弹本身要塞得进 1 MB 的请求体")
        with self.assertRaises(SaveCryptError) as ctx:
            savecrypt.unseal(seal_bytes(bomb))
        self.assertEqual("bad_save_data", ctx.exception.code)

    def test_a_truncated_compressed_stream_is_refused(self):
        # `eof` 判据顺带抓住这一档：解得出东西，但流没走完。
        body = zlib.compress(b'{"money":1}' + b" " * 4096)
        with self.assertRaises(SaveCryptError) as ctx:
            savecrypt.unseal(seal_bytes(body[:-3]))
        self.assertEqual("bad_save_data", ctx.exception.code)

    def test_a_payload_that_is_not_a_json_object_is_refused(self):
        for body in (b"[1,2,3]", b'"just a string"', b"not json at all",
                     b"\xff\xfe\x00"):
            with self.assertRaises(SaveCryptError, msg=repr(body)) as ctx:
                savecrypt.unseal(seal_bytes(zlib.compress(body)))
            self.assertEqual("bad_save_data", ctx.exception.code, msg=repr(body))

    def test_the_message_never_echoes_the_ciphertext(self):
        # 异常 message 会进日志（`web/server.py` 的 500 兜底 repr 它）。
        for spoiled in (flip(self.blob, CIPHERTEXT_AT), "不是 base64!!!"):
            with self.assertRaises(SaveCryptError) as ctx:
                savecrypt.unseal(spoiled)
            self.assertNotIn(spoiled[:16], ctx.exception.message)


class FixedVectorTests(unittest.TestCase):
    """★★ 把算法钉死。

    密钥和构造**一旦随包发出去就再也改不动了** —— 改了，全世界已经导出的存档
    全部作废。所以这里存一条固定向量：谁动了密钥派生 / 计数器 / MAC 覆盖范围 /
    压缩参数 / JSON 序列化方式，先炸的是这一条，而不是几个月后线上玩家的存档。

    真要换构造，只能**加一个新的 `FORMAT_VERSION` 并把旧的留着能解**，
    然后在这里加一条新向量，别改旧的。
    """

    FIELDS = {"money": 12345, "level": 7, "nick": "小炮"}
    NONCE = bytes(range(savecrypt.NONCE_BYTES))
    EXPECTED = "UFNTVgKEt5o1AAECAwQFBgcICQoLDA0OD4/vBvq8wuMtWJ4j/T5RTh6/vVeVseT1shjy9ZYIMQLtcFXoChDW96RrR7ACb8U9YAKZoIxAZCTbrCXVtHXbz7nK7+ttwVmn4PSNR/BkQ4TfmA=="

    def test_the_vector_still_matches(self):
        self.assertEqual(
            self.EXPECTED,
            savecrypt._seal_with_nonce(self.FIELDS, self.NONCE))

    def test_the_vector_still_opens(self):
        self.assertEqual(self.FIELDS, savecrypt.unseal(self.EXPECTED))


if __name__ == "__main__":
    unittest.main()
