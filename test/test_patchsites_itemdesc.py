#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仓库提示框说明文补丁（X_Mod · X10）的站点核对 —— 拿脱壳镜像把前提逐条钉死。

补丁只有一处：`0x4554e7` 的 9 字节（`mov eax,[ebx+0xd0]` + `add eax,0x18`）换成
`call 我们的 thunk` + 4 个 NOP，由 thunk 决定「画 `ItemInfo+0x18` 原文，还是画
服务端 `0x0F02` 推下来的那一套」。它成立靠五个前提，**每一个错了都不会有编译期报错**：

1. 那 9 字节确实是那两条指令，而且没有任何跳转落进补丁区内部；
2. `[ebx+0xd0]` 真的是 `ItemInfo*`、`ItemInfo+4` 真的是 ItemDB 的 key；
3. 接手的 `0x5d5789` 对第 2 个参数**只读一次、只读首 dword** ——
   替身才可以只是「一个指向 UTF-16 缓冲的指针变量」；
4. `eax` 的活期只有一条指令，标志位没人读 ⇒ 换成 call 出去再回来是等价的；
5. ★ `UiCabinetToolTip` **一个类通吃**大厅仓库 / 商店页仓库格 / **待机房间的快速换装背包**
   —— 这条塌了的话「房间里按模式显示」整个需求落空，而且一行报错都不会有。
"""
import os
import struct
import unittest

import test_patchsites as base

#: 同签名的另一处 = `UiCompositionToolTip`（合成提示框，只在大厅）。**不打那处。**
OTHER_SITE = 0x0045FF1B
#: `UiCabinetToolTip` 的虚表 / 构造函数（V0.3商店 §31 记过虚表，构造是本轮定的）。
CABINET_VFT = 0x006681EC
CABINET_CTOR = 0x00454572
#: 构造函数的三个调用者 = 这个提示框服务的三个界面。
CTOR_CALLERS = (0x0044D2A2,   # 商店页里的仓库格
                0x00453BE4,   # 大厅仓库页
                0x0046EE58)   # ★ 待机房间的快速换装背包
#: 「换装完成」那个按钮的宽字符串 —— `0x46ee58` 的宿主函数就是靠它认出来的。
ROOM_SWAP_BUTTON_VA = 0x0066B1F4
ROOM_SWAP_BUTTON = "교체완료"
#: ItemDB 那张哈希表和它的查表函数（`0x454b15..0x454b23` 那一段）。
ITEMDB_VA = 0x0072E1DC
ITEMDB_FIND = 0x00415A94
#: 按 `|` 切说明文的那个函数，和它读第 2 个参数的那一条。
SPLIT_FN = 0x005D5789
SPLIT_READ_ARG2 = 0x005D57C3


def _rel32_targets(img, base_va=0x400000):
    """全镜像所有相对跳转 / 调用的落点 → {目标: [指令地址…]}。"""
    out = {}
    n = len(img)
    for off in range(n - 6):
        byte = img[off]
        if byte in (0xE8, 0xE9):
            target = base_va + off + 5 + struct.unpack_from("<i", img, off + 1)[0]
        elif byte == 0x0F and 0x80 <= img[off + 1] <= 0x8F:
            target = base_va + off + 6 + struct.unpack_from("<i", img, off + 2)[0]
        elif byte == 0xEB or 0x70 <= byte <= 0x7F:
            target = base_va + off + 2 + struct.unpack_from("<b", img, off + 1)[0]
        else:
            continue
        out.setdefault(target, []).append(base_va + off)
    return out


class CabinetDescPatchTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        for path in (base.BSHOOK, base.IMG):
            if not os.path.isfile(path):
                raise unittest.SkipTest("不在源码仓库里（缺 %s），跳过" % path)
        cls.img = base.load_image()
        cls.src = base.c_source()
        cls.site = base.c_define(cls.src, "WDESC_SITE_VA")
        cls.site_len = base.c_define(cls.src, "WDESC_SITE_LEN")
        cls.sig = base.c_byte_array(cls.src, "WDESC_SITE_SIG")
        cls._rel = None

    @classmethod
    def rel32(cls):
        if cls._rel is None:
            cls._rel = _rel32_targets(cls.img)
        return cls._rel

    # -- 前提 1：站点字节 + 没有跳转落进补丁区 -------------------------------

    def test_the_site_is_what_the_image_has(self):
        self.assertEqual(0x004554E7, self.site)
        self.assertEqual(9, self.site_len)
        self.assertEqual(self.site_len, len(self.sig))
        self.assertEqual(self.sig, base.read_va(self.img, self.site, self.site_len))
        # 两条整指令：mov eax,[ebx+0xd0] (6) + add eax,0x18 (3)
        self.assertEqual(bytes((0x8B, 0x83, 0xD0, 0x00, 0x00, 0x00)), bytes(self.sig[:6]))
        self.assertEqual(bytes((0x83, 0xC0, 0x18)), bytes(self.sig[6:]))
        # 放得下 E8 rel32 + NOP
        self.assertGreaterEqual(self.site_len, 5)

    def test_nothing_jumps_into_the_middle_of_the_patch(self):
        """★ 补丁把 9 字节整块换掉，中间落一个跳转就会跳进 `call` 的位移里。"""
        inside = [va for va in self.rel32()
                  if self.site < va < self.site + self.site_len]
        self.assertEqual([], [hex(v) for v in inside])
        # 站点本身可以有入边（`0x4553f9` 那条 `jmp 0x4554df` 汇进来，ebx 两条路上都是 this）
        self.assertNotIn(self.site, self.rel32(), "站点首字节上有跳转落点，得重挑补丁点")

    def test_the_same_bytes_live_in_the_composition_tooltip_too(self):
        """⚠⚠ 同样的 9 字节在镜像里有 **2** 处，另一处是合成提示框。

        所以补丁必须按**固定 VA**装，**不许按签名全图搜** —— 搜就会一次打两处，
        而人在合成页时根本不在房间，跟着房间模式走语义是错的。
        """
        sig = bytes(self.sig)
        hits = []
        start = 0
        while True:
            at = self.img.find(sig, start)
            if at < 0:
                break
            hits.append(0x400000 + at)
            start = at + 1
        self.assertEqual([self.site, OTHER_SITE], hits)

    def test_the_patch_installer_does_not_search_for_the_signature(self):
        """判据落在代码上：`try_patch_cabinet_desc` 里只许出现写死的 VA。"""
        body = self.src[self.src.index("static int try_patch_cabinet_desc"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("WDESC_SITE_VA", body)
        for forbidden in ("memmem", "strstr", "for (va", "while (va"):
            self.assertNotIn(forbidden, body,
                             "补丁被改成全图搜签名了 —— 会连合成提示框一起打")

    # -- 前提 2：+0xd0 是 ItemInfo，+4 是 ItemDB 的 key -----------------------

    def test_the_field_at_0xd0_is_the_itemdb_lookup_result(self):
        """`0x454b15`: `push [eax+4]` / `mov ecx,[0x72e1dc]` / `call 0x415a94`
        / `mov [edi+0xd0],eax` —— 「`+0xd0` 是 `ItemInfo*`」的出处。"""
        code = base.read_va(self.img, 0x00454B15, 20)
        self.assertEqual(bytes((0xFF, 0x70, 0x04)), bytes(code[0:3]))      # push [eax+4]
        self.assertEqual(bytes((0x8B, 0x0D)), bytes(code[3:5]))            # mov ecx, [imm32]
        self.assertEqual(ITEMDB_VA, struct.unpack_from("<I", bytes(code), 5)[0])
        self.assertEqual(0xE8, code[9])                                     # call rel32
        self.assertEqual(ITEMDB_FIND,
                         0x00454B1E + 5 + struct.unpack_from("<i", bytes(code), 10)[0])
        self.assertEqual(bytes((0x89, 0x87, 0xD0, 0x00, 0x00, 0x00)),       # mov [edi+0xd0], eax
                         bytes(code[14:20]))

    # -- 前提 3 / 4：切分函数的契约，和 eax 的活期 ---------------------------

    def test_the_split_call_follows_the_patch(self):
        """补丁区之后是 `push L"|" / push eax / lea eax,[ebp-0x38] / push eax / call 0x5d5789`。

        `push eax`（`0x4554f5`）是 eax **唯一**的消费点，下一条 `lea` 就把它覆盖了
        ⇒ thunk 只要保证返回时 eax 是替身地址。
        """
        resume = self.site + self.site_len      # detour `ret` 之后落回的地方
        self.assertEqual(0x004554F0, resume)
        code = base.read_va(self.img, resume, 15)
        self.assertEqual(0x68, code[0])                                     # push imm32（L"|"）
        self.assertEqual(0x50, code[5])                                     # push eax  ← 唯一消费点
        self.assertEqual(bytes((0x8D, 0x45, 0xC8)), bytes(code[6:9]))       # lea eax,[ebp-0x38]
        self.assertEqual(0x50, code[9])                                     # push eax（dest）
        self.assertEqual(0xE8, code[10])
        self.assertEqual(SPLIT_FN, resume + 15 + struct.unpack_from("<i", bytes(code), 11)[0])
        # call 之后第一条碰 eax 的是「重新装载」：mov eax,[ebp-0x34]
        self.assertEqual(bytes((0x8B, 0x45, 0xCC)), bytes(base.read_va(self.img, 0x00455509, 3)))
        # 补丁区之后没有任何指令读标志位（push imm32 / push r32 / lea / call 都不读）
        # ⇒ detour 少设一次标志是等价的。上面那 5 条就是全部证据。

    def test_the_split_reads_arg2_exactly_once_and_only_its_first_dword(self):
        """★ 这条是「替身可以只是一个指针变量」的全部依据。

        `0x5d57c3`: `mov eax,[ebp+0xc]` / `mov eax,[eax]` —— 取出 `wchar_t*`；
        紧接着 `push 0x3e7`（999）+ 拷进 `sub esp,0x7e4` 开的那块栈缓冲；
        之后 `0x5d57ff lea ecx,[ebp+0xc]` 把那个参数槽当局部变量**复用**掉了
        ⇒ 第二个 dword 永远不会被读。
        """
        self.assertEqual(bytes((0x8B, 0x45, 0x0C, 0x8B, 0x00)),
                         bytes(base.read_va(self.img, SPLIT_READ_ARG2, 5)))
        self.assertEqual(bytes((0x68, 0xE7, 0x03, 0x00, 0x00)),      # push 999
                         bytes(base.read_va(self.img, 0x005D57C8, 5)))
        self.assertEqual(bytes((0x81, 0xEC, 0xE4, 0x07, 0x00, 0x00)),  # sub esp,0x7e4
                         bytes(base.read_va(self.img, 0x005D5793, 6)))
        self.assertEqual(bytes((0x8D, 0x4D, 0x0C)),                  # lea ecx,[ebp+0xc]（复用）
                         bytes(base.read_va(self.img, 0x005D57FF, 3)))
        # 我们那条文案的上限要落在它抄得下的范围里
        self.assertLess(base.c_define(self.src, "WDESC_TEXT_MAX"), 999)

    # -- 前提 5：这个提示框到底服务哪几个界面 --------------------------------

    def test_the_constructor_writes_the_cabinet_vftable(self):
        """`0x454588`: `mov [esi], 0x6681ec` —— 认出这是 `UiCabinetToolTip` 的构造。"""
        code = base.read_va(self.img, 0x00454588, 6)
        self.assertEqual(0xC7, code[0])
        self.assertEqual(CABINET_VFT, struct.unpack_from("<I", bytes(code), 2)[0])

    def test_one_tooltip_class_serves_all_three_inventory_panels(self):
        """★★ 本需求的地基：构造函数 `0x454572` 全镜像**恰好 3 个调用点**
        = 商店页仓库格 / 大厅仓库页 / **待机房间的快速换装背包**
        ⇒ 一处补丁就能让三处都跟着服务端换文案。

        这条一红就说明前提塌了（换了客户端版本、或那块 UI 被动过），
        **别去改这条断言，先回头查房间面板是不是换了别的类**。
        """
        callers = sorted(self.rel32().get(CABINET_CTOR, []))
        self.assertEqual(sorted(CTOR_CALLERS), callers,
                         "UiCabinetToolTip 的构造调用点变了：%s" % [hex(c) for c in callers])

    def test_the_third_caller_really_is_the_room_quick_swap_panel(self):
        """`0x46ee58` 的宿主函数同时在建「교체완료（换装完成）」那个按钮 ——
        那是待机房间快速换装面板独有的。"""
        raw = base.read_va(self.img, ROOM_SWAP_BUTTON_VA,
                           len(ROOM_SWAP_BUTTON) * 2 + 2)
        self.assertEqual(ROOM_SWAP_BUTTON + "\0", bytes(raw).decode("utf-16le"))
        # 那个 push 就在构造调用点前面不远（同一个函数体里）
        window = bytes(base.read_va(self.img, 0x0046ECED, 0x46EE58 - 0x46ECED))
        self.assertIn(b"\x68" + struct.pack("<I", ROOM_SWAP_BUTTON_VA), window)

    # -- 常量对账：C 和 Python 两边必须说同一件事 ----------------------------

    def test_the_wire_constants_match_the_server(self):
        import sys
        server = os.path.join(base.ROOT, "server")
        if server not in sys.path:
            sys.path.insert(0, server)
        import weaponcfg
        self.assertEqual(base.c_define(self.src, "WDESC_FORMAT"), weaponcfg.DESC_WIRE_FORMAT)
        self.assertEqual(base.c_define(self.src, "WDESC_HEADER_BYTES"), weaponcfg.DESC_HEADER_SIZE)
        self.assertEqual(0x0F02, base.c_define(self.src, "WDESC_OPCODE"))
        self.assertNotEqual(base.c_define(self.src, "WTAB_OPCODE"),
                            base.c_define(self.src, "WDESC_OPCODE"))
        self.assertEqual(OTHER_SITE, base.c_define(self.src, "WDESC_SITE_OTHER"))
        # 逃生门没被改名（实机排查时要靠它做对照组）
        self.assertIn("BSHOOK_KEEP_CABINET_DESC", self.src)


if __name__ == "__main__":
    unittest.main()
