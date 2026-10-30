#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""晋级任务表（X_Mod · X23）的站点核对 —— `hook/bshook.c` 里写死的地址和字节拿脱壳镜像验一遍。

和 `test_patchsites_wtab.py` 同一个道理：bshook 要**调游戏自己的加载器**重建任务表、换掉单例指针，
地址或调用约定错一个，没有编译期报错，只有实机上的崩溃。这里能离线钉死的：

* 单例多大、谁分配、哪几格要清零（`0x410033` 那 33 字节）；
* 加载器 `0x55a2d9` 吃 eax = 单例、栈上一个流、`ret 4`；它把解析器放进 `[单例+0]`、
  记录塞进 `+0x04` 的 vector、上限写 `+0x10..+0x18` —— **全在 `+0x1c` 以下**，
  所以从旧单例抄过来的 `+0x1c..+0x6b`（等级 + A/B/C）不会被它冲掉；
* 解析器只通过流对象虚表的 **+0x14 IsEof / +0x18 Read** 两格读数据，而且是 thiscall、
  被调方清栈 —— `promo_wire.h` 的 `__fastcall` 冒充靠的就是这个（四个函数体的字节整段钉住，
  「只有这两格」是拿反汇编器逐条核过的，FINDINGS §154）；
* 生成器（`server/missions.py`）写的小节前缀 / 角色名 / 键名都是镜像里真有的那几个串；
* 线格式常量 hook / 服务端两边一致。
"""
import hashlib
import os
import re
import struct
import sys
import unittest

import test_patchsites as base

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

import missions                                                # noqa: E402

PROMO_WIRE = os.path.join(base.ROOT, "hook", "promo_wire.h")

#: 解析器和三个读流小函数的函数体（起点, 终点）→ sha256 前 16 位。
#: ★ 「流对象只被调 +0x14 / +0x18」是拿反汇编器对**这几段字节**逐条核出来的结论：
#:   解析器 `0x5d48c3` 里唯一的间接调用是 `0x5d4bba call [eax+0x14]`，三个小函数里各是一条
#:   `call [eax+0x18]`。字节一变（不是这一版客户端），那个结论就得重核 —— 这里当场红。
BODIES = {
    (0x5D48C3, 0x5D4C21): "851d91f56cc18b2a",   # ini 解析器（ret 4 收尾）
    (0x5D5942, 0x5D5956): "0ed396033b098a73",   # 读 1 字节（认 BOM）
    (0x5D5BA9, 0x5D5BEF): "217ed0e94da6c878",   # 读一行 UTF-16（一次 2 字节）
    (0x5D5C4D, 0x5D5C92): "c6b3942a559db09a",   # 读一行 ANSI（一次 1 字节）
    (0x55A2D9, 0x55A348): "21af0f0f1a82f2f4",   # 加载器 + 建解析器那一段
}


def header_define(text, name):
    m = re.search(r"^#define\s+%s\s+\(?([0-9xXa-fA-F]+)[uU]?" % re.escape(name), text, re.M)
    if not m:
        raise AssertionError("promo_wire.h 里找不到 #define %s" % name)
    return int(m.group(1), 0)


class PromotionTableHookTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        for path in (base.BSHOOK, base.IMG, PROMO_WIRE):
            if not os.path.isfile(path):
                raise unittest.SkipTest("不在源码仓库里（缺 %s），跳过" % path)
        cls.img = base.load_image()
        cls.src = base.c_source()
        with open(PROMO_WIRE, "r", encoding="utf-8") as fp:
            cls.wire = fp.read()
        d = lambda name: base.c_define(cls.src, name)       # noqa: E731
        cls.singleton = d("PROMO_SINGLETON_VA")
        cls.size = d("PROMO_SINGLETON_SIZE")
        cls.keep_from = d("PROMO_KEEP_FROM")
        cls.vec_first = d("PROMO_VEC_FIRST")
        cls.vec_last = d("PROMO_VEC_LAST")
        cls.create = d("PROMO_CREATE_VA")
        cls.new = d("PROMO_NEW_VA")
        cls.load = d("PROMO_LOAD_VA")

    # ------------------------------------------------------------ 签名本身

    def test_the_three_signatures_are_what_the_image_has(self):
        for name, va in (("PROMO_CREATE_SIG", self.create), ("PROMO_LOAD_SIG", self.load),
                         ("PROMO_NEW_SIG", self.new)):
            sig = base.c_byte_array(self.src, name)
            self.assertEqual(sig, base.read_va(self.img, va, len(sig)), name)

    def test_the_singleton_is_made_exactly_once_and_only_there(self):
        """建单例的函数全镜像只有两个调用者（启动 `0x40d8e9`、`0x5503cb`），它们都只在
        `[0x72e954]` 还是 0 的时候才真建 —— 我们换上去的新指针不会被它覆盖回去。"""
        sig = base.c_byte_array(self.src, "PROMO_CREATE_SIG")
        self.assertEqual(1, self.img.count(sig))
        self.assertEqual(bytes([0xA1]) + struct.pack("<I", self.singleton), sig[:5])
        self.assertEqual(bytes([0x85, 0xC0, 0x75]), sig[5:8], "建之前没判「已经有了」")
        callers = []
        for off in range(len(self.img) - 5):
            if self.img[off] != 0xE8:
                continue
            va = base.IMAGE_BASE + off
            if va + 5 + struct.unpack_from("<i", self.img, off + 1)[0] == self.create:
                callers.append(va)
        self.assertEqual([0x40D8E9, 0x5503CB], callers,
                         "建单例的调用者变了：%s" % [hex(c) for c in callers])
        # 加载器全镜像也只有启动那一个调用者 —— 别处不会再拿资源包里那份把我们的表冲回去。
        loaders = [base.IMAGE_BASE + off for off in range(len(self.img) - 5)
                   if self.img[off] == 0xE8
                   and base.IMAGE_BASE + off + 5
                   + struct.unpack_from("<i", self.img, off + 1)[0] == self.load]
        self.assertEqual([0x40D982], loaders)

    # ------------------------------------------------------- 单例怎么建

    def test_the_create_site_says_how_big_the_singleton_is_and_what_to_clear(self):
        """`push 0x6c / call operator new / … / and [eax+4],0 / and [eax+8],0 / and [eax+0xc],0`
        —— bshook 照这三样建新单例；`+0x1c` 起的那一段**原版不清零**（启动后由别处写），
        我们从旧单例抄。"""
        code = base.read_va(self.img, self.create, 40)
        self.assertEqual(bytes([0x6A, self.size]), code[9:11], "单例大小不是 PROMO_SINGLETON_SIZE")
        self.assertEqual(self.new, base.call_target(self.img, self.create + 11))
        cleared = [code[at + 2] for at in (21, 25, 29)]
        self.assertEqual([0x83, 0x60] * 3,
                         [code[at + k] for at in (21, 25, 29) for k in (0, 1)])
        self.assertEqual([self.vec_first, self.vec_last, self.vec_last + 4], cleared)
        self.assertLess(max(cleared), self.keep_from)
        # 0x410058：`mov [0x72e954], eax` —— 建好的指针就存在我们换的那一格。
        self.assertEqual(bytes([0xA3]) + struct.pack("<I", self.singleton),
                         base.read_va(self.img, 0x410058, 5))

    def test_operator_new_is_cdecl(self):
        """启动那一发 `call 0x5f399e` 之后紧跟 `pop ecx`（0x410045）—— 调用方清栈，
        `promo_game_new` 里那句 `add esp, 4` 就是照这个写的。"""
        self.assertEqual(b"\x59", base.read_va(self.img, 0x410045, 1))
        self.assertEqual(self.new, base.call_target(self.img, 0x41003E))

    # --------------------------------------------------------- 加载器

    def test_the_loader_takes_eax_and_pops_one_argument(self):
        """`0x55a2d9: push esi / push [esp+8] / mov esi,eax / call 0x55a2fa / … / ret 4`。"""
        sig = base.c_byte_array(self.src, "PROMO_LOAD_SIG")
        self.assertEqual(bytes([0x8B, 0xF0]), sig[5:7], "单例不是从 eax 进来的")
        self.assertEqual(0x55A2FA, base.call_target(self.img, self.load + 7))
        # 成功才去建记录：`test eax,eax / je / mov ecx,esi / call 0x55ab61`
        self.assertEqual(bytes([0x85, 0xC0, 0x74, 0x0D, 0x8B, 0xCE]),
                         base.read_va(self.img, 0x55A2E5, 6))
        self.assertEqual(0x55AB61, base.call_target(self.img, 0x55A2EB))
        self.assertEqual(bytes([0x5E, 0xC2, 0x04, 0x00]), base.read_va(self.img, 0x55A2F6, 4),
                         "加载器不是 ret 4 —— promo_game_load 压的那一个参数会没人弹")

    def test_the_loader_parks_the_parser_at_offset_zero(self):
        """`0x55a2fa`：`new(0x24)` 一个解析器、`mov [esi],eax` 存进 `[单例+0]`、
        把**流**（`[ebp+8]`）交给解析器 `0x5d48c3`。旧单例的这一格（旧 ini）我们故意泄漏。"""
        code = base.read_va(self.img, 0x55A305, 2)
        self.assertEqual(bytes([0x6A, 0x24]), code)
        self.assertEqual(self.new, base.call_target(self.img, 0x55A307))
        self.assertEqual(bytes([0xFF, 0x75, 0x08, 0x8B, 0xC8, 0x89, 0x06]),
                         base.read_va(self.img, 0x55A327, 7))
        self.assertEqual(0x5D48C3, base.call_target(self.img, 0x55A32E))

    def test_the_builder_only_writes_below_the_copied_tail(self):
        """`0x55ab61` 写 `+0x10/+0x14/+0x18`（三条链的等级上限，读完 ini 又**一律钉成 60**），
        记录 push 进 `+0x04` 的 vector（`0x55aab0: lea esi,[edi+4]`）—— 都在 `+0x1c` 以下。"""
        for va, off in ((0x55ABB2, 0x10), (0x55AC03, 0x14), (0x55AC54, 0x18)):
            self.assertEqual(bytes([0x89, 0x47, off]), base.read_va(self.img, va, 3))
            self.assertLess(off, self.keep_from)
        self.assertEqual(bytes([0x6A, 0x3C, 0x58, 0x89, 0x47, 0x18, 0x89, 0x47, 0x14,
                                0x89, 0x47, 0x10]),
                         base.read_va(self.img, 0x55AC6B, 12),
                         "上限不再一律钉成 60 了 —— missions.MAX_RANK 跟着要改")
        self.assertEqual(missions.CHAIN_SLOTS, 0x3C)
        self.assertEqual(bytes([0x8D, 0x77, self.vec_first]), base.read_va(self.img, 0x55AAB0, 3))

    # ------------------------------------------------------ 流对象的两格

    def test_the_parser_and_its_readers_are_the_bytes_we_checked(self):
        for (start, end), digest in BODIES.items():
            got = hashlib.sha256(base.read_va(self.img, start, end - start)).hexdigest()[:16]
            self.assertEqual(digest, got, "%08X..%08X 的字节变了：「只调 +0x14 / +0x18」"
                                          "那个结论要重核" % (start, end))

    def test_is_eof_is_slot_0x14_called_as_thiscall(self):
        """`0x5d4bb3: mov edi,[ebp+8] / mov eax,[edi] / mov ecx,edi / call [eax+0x14] / test al,al`。"""
        self.assertEqual(bytes([0x8B, 0x7D, 0x08, 0x8B, 0x07, 0x8B, 0xCF, 0xFF, 0x50, 0x14,
                                0x84, 0xC0]),
                         base.read_va(self.img, 0x5D4BB3, 12))
        self.assertIn("#define PROMO_VT_ISEOF   (0x14 / 4)", self.wire)
        self.assertIn("#define PROMO_VT_READ    (0x18 / 4)", self.wire)

    def test_read_is_slot_0x18_thiscall_and_the_callee_pops(self):
        """三个读流小函数都是 `push n / push dst / [mov ecx,esi] / call [eax+0x18]`，
        之后**没有** `add esp` —— 读一行的那两个还在循环里一遍遍压参数，被调方不弹的话
        `pop edi / pop esi` 当场取错。⇒ 被调方弹 8 字节（thiscall），`__fastcall` 正好一样。"""
        sites = {
            0x5D5948: (1, bytes([0x6A, 0x01, 0x8D, 0x55, 0xFF, 0x52, 0xFF, 0x50, 0x18,
                                 0x8A, 0x45, 0xFF])),
            0x5D5BC0: (2, bytes([0x6A, 0x02, 0x8D, 0x4D, 0xFC, 0x51, 0x8B, 0xCE, 0xFF, 0x50,
                                 0x18, 0x8B, 0xF8, 0x85, 0xFF])),
            0x5D5C64: (1, bytes([0x6A, 0x01, 0x8D, 0x4D, 0xFC, 0x51, 0x8B, 0xCE, 0xFF, 0x50,
                                 0x18, 0x8B, 0xF8, 0x85, 0xFF])),
        }
        for va, (width, code) in sites.items():
            self.assertEqual(code, base.read_va(self.img, va, len(code)), "%08X" % va)
            self.assertEqual(width, code[1], "%08X 一次读的字节数" % va)
        # 读一行的两个：读到 `\n` 或读不出（eax == 0）就停 —— Read 必须返回读到的字节数。
        self.assertEqual(bytes([0x66, 0x83, 0x7D, 0xFC, 0x0A]), base.read_va(self.img, 0x5D5BDE, 5))
        self.assertEqual(bytes([0x80, 0x7D, 0xFC, 0x0A]), base.read_va(self.img, 0x5D5C82, 4))

    def test_the_parser_reads_a_bom_then_picks_utf16(self):
        """开头两次读 1 字节（`0x5d4979` / `0x5d4983` 都是 `call 0x5d5942`），`FF FE` 才走
        UTF-16 那条读行（`0x5d49fa call 0x5d5ba9`）。服务端生成的表必须带 BOM。"""
        self.assertEqual(0x5D5942, base.call_target(self.img, 0x5D4979))
        self.assertEqual(0x5D5942, base.call_target(self.img, 0x5D4983))
        self.assertEqual(bytes([0x80, 0x7D, 0xF3, 0xFF]), base.read_va(self.img, 0x5D4988, 4))
        self.assertEqual(bytes([0x3C, 0xFE]), base.read_va(self.img, 0x5D4991, 2))
        self.assertEqual(0x5D5BA9, base.call_target(self.img, 0x5D49FA))
        data = missions.build_client_ini({})
        self.assertEqual(b"\xff\xfe", data[:2])

    def test_the_stream_vtable_puts_the_two_slots_where_the_game_looks(self):
        vt = re.search(r"PROMO_STREAM_VT\[PROMO_VT_SLOTS\]\s*=\s*\{(.*?)\};", self.wire, re.S)
        self.assertIsNotNone(vt, "promo_wire.h 里找不到虚表")
        slots = re.findall(r"\(promo_fn\)(\w+)", vt.group(1))
        self.assertEqual(16, len(slots))
        self.assertEqual("promo_stream_eof", slots[0x14 // 4])
        self.assertEqual("promo_stream_read", slots[0x18 // 4])
        self.assertEqual({"promo_stream_trap"},
                         set(s for at, s in enumerate(slots) if at not in (5, 6)))
        self.assertIn("int PROMO_FASTCALL promo_stream_read(promo_stream *self, void *edx_unused, "
                      "void *dst, unsigned n)", self.wire)
        self.assertIn("int PROMO_FASTCALL promo_stream_eof(promo_stream *self)", self.wire)
        self.assertRegex(self.wire, r"typedef struct promo_stream \{\s*const promo_fn \*vt;",
                         "虚表指针必须是流对象的第一格")

    # -------------------------------------------- 生成器写的串 = 镜像里的串

    def test_the_generator_writes_strings_the_loader_looks_for(self):
        def wide(text):
            return b"\x00\x00" + text.encode("utf-16-le") + b"\x00\x00"
        for line, fmt in missions.CHAIN_FORMAT.items():
            self.assertIn(wide(fmt), self.img, "小节格式 %s" % line)
        for tab, prefix in missions.TAB_PREFIX.items():
            self.assertIn(wide(prefix), self.img, "角色前缀 %d" % tab)
        keys = [k for k, _ in missions._INT_KEYS] + [k for k, _ in missions._TEXT_KEYS]
        for key in keys:
            self.assertIn(wide(key), self.img, key)
        self.assertIn(wide("Reward%d"), self.img)
        # 生成器写出去的每一个键都在上面那张表里（或者是 Reward0..2）。
        sample = missions.validate_missions({"missions": [
            {"id": 1, "line": "pvp", "kind": "win", "pvp_mode": 0, "item_mode": 1,
             "rewards": [{"money": 1}, {"exp": 2}, {"money": 3}]},
            {"id": 2, "line": "quest", "kind": "clear", "stage": 3, "difficulty": 2,
             "rewards": [{"money": 1}]},
            {"id": 3, "line": "quest", "kind": "map", "map": "QuestLevel01",
             "script": "x.lua", "difficulty": 1, "rewards": [{"money": 1}]}]})
        for mission in sample:
            for key, _value in missions.record_fields(mission, 0, 1):
                self.assertTrue(key in keys or re.fullmatch(r"Reward[0-2]", key), key)

    # ---------------------------------------------------------- 两边一致

    def test_the_wire_constants_match_the_server(self):
        self.assertEqual(missions.OP_HOOK_TABLE, base.c_define(self.src, "PROMO_OPCODE"))
        self.assertEqual(missions.TABLE_FORMAT, header_define(self.wire, "PROMO_FORMAT"))
        self.assertEqual(missions.TABLE_HEADER.size, header_define(self.wire, "PROMO_HEADER_BYTES"))
        self.assertEqual(missions.TABLE_CHUNK, 32 * 1024)
        self.assertIn("#define PROMO_CHUNK_MAX     (32u * 1024u)", self.wire)

    # ------------------------------------------------ bshook 里那几道闸

    def test_0x0f03_is_routed_beside_the_other_two_tables(self):
        body = base.c_function(self.src, "static int __cdecl wtab_on_frame(")
        route = body.index("if (opcode == PROMO_OPCODE) return promo_on_frame(frame);")
        self.assertLess(route, body.index("if (opcode != WTAB_OPCODE) return 0;"))

    def test_only_the_two_exception_codes_are_caught(self):
        self.assertEqual(0xE06D7363, base.c_define(self.src, "PROMO_CXX_CODE"))
        body = base.c_function(self.src, "static int promo_filter(")
        self.assertIn("PROMO_CXX_CODE", body)
        self.assertIn("PROMO_TRAP_CODE", body)
        self.assertIn("EXCEPTION_CONTINUE_SEARCH", body)

    def test_the_pointer_is_swapped_only_after_a_successful_load(self):
        body = base.c_function(self.src, "static void promo_rebuild(")
        swap = body.index("*(void *volatile *)PROMO_SINGLETON_VA = fresh;")
        self.assertLess(body.index("if (code) {"), swap)
        self.assertLess(body.index("if (!ok) {"), swap)
        self.assertLess(body.index("g_promo_applied_crc"), swap,
                        "「和现在用的一样就跳过」那一闸要在重建之前")

    def test_the_new_singleton_keeps_the_old_tail(self):
        body = base.c_function(self.src, "static int promo_load_guarded(")
        self.assertIn("memset(made, 0, PROMO_KEEP_FROM);", body)
        self.assertIn("PROMO_SINGLETON_SIZE - PROMO_KEEP_FROM", body)
        self.assertEqual(0x6C, self.size)
        self.assertEqual(0x1C, self.keep_from)


if __name__ == "__main__":
    unittest.main()
