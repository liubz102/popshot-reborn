#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字节补丁的**站点**核对：`hook/bshook.c` 里写死的地址和字节，拿脱壳镜像验一遍。

为什么值得单开一个模块：补丁打在 exe 的运行时内存上，**打错了没有编译期报错**
—— 最好的情况是补丁不生效（特征串对不上，日志里一行 `!!`），最坏的情况是
跳进指令中间，随机崩在别处。而「特征串在不在、唯一不唯一、跳转落在哪」
全部是**离线可判定**的：`re/BigShot_22524.img` 是拉平的内存镜像，
**文件偏移 == VA − 0x400000**。

收的都是**实机没法按需复现**的补丁 —— 触发条件是「D3D 设备丢失」「掉线时
正好在拆加载画面」这种，只能靠离线把每一条都钉死：

- **索引缓冲空指针判据**（§35 / D25）：本仓库第一个**跳板式**补丁
  （站点塞不下，要跳到 VirtualAlloc 的代码洞里）；
- **bug调查/25 那两处**（§57 / §58）：上面两个病各自的**第二处站点** ——
  静态 D3DX9 的 `ID3DXSprite::Begin` 里又一个不查返回值的 `Lock()`，
  以及 `LoadingStage` 析构时 `LobbyStage` 已经没了。

⚠ 依赖仓库布局（`hook/`、`re/`），发布包里没有它们，自动跳过。
"""
import os
import re
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BSHOOK = os.path.join(ROOT, "hook", "bshook.c")
IMG = os.path.join(ROOT, "re", "BigShot_22524.img")
IMAGE_BASE = 0x400000


def load_image():
    with open(IMG, "rb") as f:
        return f.read()


def read_va(img, va, n):
    off = va - IMAGE_BASE
    assert 0 <= off <= len(img) - n, "VA %08X 落在镜像外" % va
    return img[off:off + n]


def c_source():
    with open(BSHOOK, "r", encoding="utf-8") as f:
        return f.read()


def c_define(src, name):
    """抠 `#define <name> <整数>`（十进制或 0x…，允许 u 后缀）。"""
    m = re.search(r"^#define\s+%s\s+(0[xX][0-9A-Fa-f]+|\d+)[uU]?\s*(?:/\*|$)"
                  % re.escape(name), src, re.M)
    if not m:
        raise AssertionError("bshook.c 里找不到 #define %s" % name)
    return int(m.group(1), 0)


def c_byte_array(src, name):
    """抠 `static const unsigned char <name>[..] = { … };` 里的字节。

    ★ 要先把 C 注释去掉：这几张表的注释里全是 `mov [ebp+0x54], eax` 这种，
      里头的 `0x54` 会被当成表里的一项。
    """
    m = re.search(r"unsigned char\s+%s\s*\[[^\]]*\]\s*=\s*\{(.*?)\};"
                  % re.escape(name), src, re.S)
    if not m:
        raise AssertionError("bshook.c 里找不到数组 %s" % name)
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    return bytes(int(v, 0) for v in re.findall(r"0[xX][0-9A-Fa-f]+|\b\d+\b", body))


class D3dIndexBufferPatchTest(unittest.TestCase):
    """§35 / D25 —— `IDirect3DIndexBuffer9::Lock()` 失败时别往 NULL 里 memcpy。"""

    @classmethod
    def setUpClass(cls):
        for path in (BSHOOK, IMG):
            if not os.path.isfile(path):
                raise unittest.SkipTest("不在源码仓库里（缺 %s），跳过" % path)
        cls.img = load_image()
        cls.src = c_source()
        cls.sig_va = c_define(cls.src, "D3D_IB_SIG_VA")
        cls.patch_off = c_define(cls.src, "D3D_IB_PATCH_OFF")
        cls.patch_len = c_define(cls.src, "D3D_IB_PATCH_LEN")
        cls.resume_va = c_define(cls.src, "D3D_IB_RESUME_VA")
        cls.bail_va = c_define(cls.src, "D3D_IB_BAIL_VA")
        cls.sig = c_byte_array(cls.src, "D3D_IB_SIG")
        cls.cave = c_byte_array(cls.src, "D3D_IB_CAVE")
        cls.bail_off = c_define(cls.src, "D3D_IB_CAVE_BAIL_OFF")
        cls.resume_off = c_define(cls.src, "D3D_IB_CAVE_RESUME_OFF")

    # --- 站点本身 ---------------------------------------------------------
    def test_the_signature_is_what_the_image_really_has(self):
        self.assertEqual(read_va(self.img, self.sig_va, len(self.sig)), self.sig,
                         "D3D_IB_SIG 和镜像对不上 —— 地址或字节抄错了")

    def test_the_signature_is_unique_in_the_whole_image(self):
        # 不唯一 = 这串字节不足以认出「就是这个客户端的这一处」，补丁可能打错地方。
        self.assertEqual(1, self.img.count(self.sig),
                         "D3D_IB_SIG 在镜像里出现 %d 次，不唯一" % self.img.count(self.sig))

    def test_the_stolen_bytes_are_exactly_what_the_patch_overwrites(self):
        # 洞里头 9 个字节必须是站点原样搬过去的三条指令，否则跳板一回去语义就变了。
        site = read_va(self.img, self.sig_va + self.patch_off, self.patch_len)
        self.assertEqual(site, self.cave[:self.patch_len],
                         "代码洞开头没有原样保留被覆盖掉的那 %d 字节" % self.patch_len)

    def test_the_patch_stops_right_before_the_fill_loop(self):
        # 站点多吃一个字节就啃进循环头，回跳会落在指令中间。
        self.assertEqual(self.sig_va + self.patch_off + self.patch_len, self.resume_va,
                         "补丁长度和回跳地址对不上")
        self.assertGreaterEqual(self.patch_len, 5, "放不下 E9 rel32")

    # --- 两个跳转目标确实是指令边界，而且是我们以为的那条指令 ---------------
    def test_the_resume_target_is_the_head_of_the_fill_loop(self):
        # 0x5bf6d9: mov eax,[ebp+0x5c] / xor ecx,ecx
        self.assertEqual(b"\x8b\x45\x5c\x33\xc9", read_va(self.img, self.resume_va, 5))

    def test_the_loop_back_edge_really_lands_on_the_resume_target(self):
        # 循环底部：cmp [ebp+0x60],0xc00 / jl <循环头>。★ 这条同时证明了
        # 「没有别的跳转会落进被覆盖的那 9 个字节」—— 全函数只有这一条往回跳。
        tail = read_va(self.img, self.resume_va, 0x5C)
        idx = tail.index(b"\x81\x7d\x60\x00\x0c\x00\x00")     # cmp [ebp+0x60], 0xc00
        jl_va = self.resume_va + idx + 7
        self.assertEqual(0x7C, self.img[jl_va - IMAGE_BASE], "循环底部不是 jl rel8")
        rel = struct.unpack("<b", self.img[jl_va + 1 - IMAGE_BASE:jl_va + 2 - IMAGE_BASE])[0]
        self.assertEqual(self.resume_va, jl_va + 2 + rel,
                         "循环的回跳目标不是 D3D_IB_RESUME_VA")

    def test_the_bail_target_sits_just_after_the_unlock_call(self):
        # 指针是 NULL 时压根没锁上，所以要跳到 Unlock **之后**：
        #   mov eax,[ebx+0x13c] / mov ecx,[eax] / push eax / call [ecx+0x30]
        unlock = b"\x8b\x83\x3c\x01\x00\x00\x8b\x08\x50\xff\x51\x30"
        self.assertEqual(unlock, read_va(self.img, self.bail_va - len(unlock), len(unlock)),
                         "D3D_IB_BAIL_VA 前面那几条不是 Unlock 序列")
        # 落点本身是 `mov ecx,ebx`（下一句 call 0x5bd247 和索引缓冲无关）
        self.assertEqual(b"\x8b\xcb", read_va(self.img, self.bail_va, 2))

    # --- 代码洞自己的控制流 ------------------------------------------------
    def test_the_cave_is_the_byte_sequence_we_think_it_is(self):
        # ★ 手抄一份对照：改了洞就得在这里同步改一遍，逼着改的人重新想一遍。
        expected = (
            self.cave[:self.patch_len]      # 偷来的三条原指令
            + b"\x83\x7d\x58\x00"           # cmp dword [ebp+0x58], 0   ← Lock 填的指针
            + b"\x75\x05"                   # jne +5 → 第二条 E9
            + b"\xe9\x00\x00\x00\x00"       # jmp BAIL   （rel32 装的时候现算）
            + b"\xe9\x00\x00\x00\x00"       # jmp RESUME （同上）
        )
        self.assertEqual(expected, self.cave)

    def test_the_conditional_skips_exactly_over_the_bail_jump(self):
        jcc = self.patch_len + 4                       # cmp 占 4 字节
        self.assertEqual(0x75, self.cave[jcc], "判据后面不是 jne rel8")
        target = jcc + 2 + self.cave[jcc + 1]
        self.assertEqual(self.bail_off - 1 + 5, target,
                         "jne 没有正好跳过那条 `jmp BAIL`")
        self.assertEqual(0xE9, self.cave[self.bail_off - 1])
        self.assertEqual(0xE9, self.cave[self.resume_off - 1])
        self.assertEqual(len(self.cave), self.resume_off + 4,
                         "代码洞的长度和最后一个 rel32 对不上")

    def test_the_rel32_slots_are_left_blank_in_the_template(self):
        # 模板里必须是 0：真值要按洞的实际地址算，写死任何非零值都是错的。
        for off in (self.bail_off, self.resume_off):
            self.assertEqual(b"\x00\x00\x00\x00", self.cave[off:off + 4])

    # --- 安装侧 ------------------------------------------------------------
    def test_the_installer_refuses_to_patch_twice(self):
        # 反复调 try_patch_* 是常态（patch_thread 轮询）；重入一次就会再
        # VirtualAlloc 一个洞、再把站点覆盖一遍 —— 第二次覆盖的是 E9 自己。
        body = self.src[self.src.index("static int try_patch_d3d_ib_lock(void)"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("g_d3d_ib_patched", body)
        self.assertIn("p[0] == 0xE9", body, "缺少「已经装过了」的判据")

    def test_the_patch_thread_actually_installs_it(self):
        self.assertIn("try_patch_d3d_ib_lock()", self.src)
        self.assertGreaterEqual(self.src.count("try_patch_d3d_ib_lock"), 2,
                                "定义了但没有人调用")


class _JmpGuardSiteMixin(object):
    """`install_jmp_guard` 那一类补丁的通用体检：站点在不在、唯不唯一、
    偷走的字节会不会啃进别人的跳转目标。

    子类给 `PREFIX`（bshook.c 里那组 `#define` 的前缀）即可。
    """

    PREFIX = None
    #: 装这一处的那个函数叫什么（同一族的几处共用一个安装函数）。
    INSTALLER = "try_patch_crash25_guards"

    @classmethod
    def setUpClass(cls):
        for path in (BSHOOK, IMG):
            if not os.path.isfile(path):
                raise unittest.SkipTest("不在源码仓库里（缺 %s），跳过" % path)
        cls.img = load_image()
        cls.src = c_source()
        cls.va = c_define(cls.src, cls.PREFIX + "_VA")
        cls.stolen = c_define(cls.src, cls.PREFIX + "_STOLEN")
        cls.sig = c_byte_array(cls.src, cls.PREFIX + "_SIG")
        cls.sig_len = c_define(cls.src, cls.PREFIX + "_SIG_LEN")

    def test_the_signature_is_what_the_image_really_has(self):
        self.assertEqual(self.sig_len, len(self.sig), "_SIG_LEN 和数组长度对不上")
        self.assertEqual(read_va(self.img, self.va, self.sig_len), self.sig,
                         "%s_SIG 和镜像对不上 —— 地址或字节抄错了" % self.PREFIX)

    def test_the_signature_is_unique_in_the_whole_image(self):
        self.assertEqual(1, self.img.count(self.sig),
                         "%s_SIG 在镜像里出现 %d 次，不唯一"
                         % (self.PREFIX, self.img.count(self.sig)))

    def test_there_is_room_for_the_jump(self):
        self.assertGreaterEqual(self.stolen, 5, "站点放不下 E9 rel32")
        self.assertLessEqual(self.stolen, self.sig_len, "偷的比特征串还长")

    def test_the_detour_is_wired_up(self):
        # 定义了没人装 = 白写。至少两处：定义一次 + `patch_thread` 里调一次。
        self.assertGreaterEqual(self.src.count(self.INSTALLER + "()"), 1,
                                "%s 定义了但没有人调用" % self.INSTALLER)
        self.assertIn("static int %s(void)" % self.INSTALLER, self.src)
        self.assertIn("%s_VA" % self.PREFIX, self.src.split(
            "static int %s(void)" % self.INSTALLER)[1],
            "%s 这一处没有被 %s 装上" % (self.PREFIX, self.INSTALLER))


class SpriteIndexBufferPatchTest(_JmpGuardSiteMixin, unittest.TestCase):
    """§57（bug调查/25）—— 静态 D3DX9 的 `ID3DXSprite::Begin` 里，
    `IDirect3DIndexBuffer9::Lock()` 又一处不查返回值就往指针里写。"""

    PREFIX = "SPRITE_IB"

    @classmethod
    def setUpClass(cls):
        super(SpriteIndexBufferPatchTest, cls).setUpClass()
        cls.resume_to = c_define(cls.src, "SPRITE_IB_RESUME_TO")
        cls.bail_to = c_define(cls.src, "SPRITE_IB_BAIL_TO")
        cls.slot = c_define(cls.src, "SPRITE_IB_SLOT")

    def test_the_site_sits_right_after_the_unchecked_lock(self):
        # 0x61186a: call [ecx+0x2c] = IDirect3DIndexBuffer9::Lock（槽 11）
        self.assertEqual(b"\xff\x51\x2c", read_va(self.img, self.va - 3, 3),
                         "站点前面那三个字节不是 Lock 的 call")

    def test_the_patch_stops_right_before_the_fill_loop(self):
        # 多啃一个字节就啃进 `mov edx,[ebp+8]`，回跳会落在指令中间。
        self.assertEqual(self.va + self.stolen, self.resume_to,
                         "偷的字节数和回跳地址对不上")
        self.assertEqual(b"\x8b\x55\x08", read_va(self.img, self.resume_to, 3),
                         "回跳目标不是 `mov edx,[ebp+8]`（填充循环的头）")

    def test_the_loop_back_edge_really_lands_on_the_resume_target(self):
        # 循环底部 0x6118b3: cmp ecx,0x4000 / jb <循环头>。★ 这条同时证明了
        # 「没有别的跳转会落进被偷走的那 5 个字节」—— 全函数只有这一条往回跳。
        cmp_va = 0x006118B3
        self.assertEqual(b"\x81\xf9\x00\x40\x00\x00",
                         read_va(self.img, cmp_va, 6), "循环底部不是 cmp ecx,0x4000")
        jb_va = cmp_va + 6
        self.assertEqual(0x72, self.img[jb_va - IMAGE_BASE], "循环底部不是 jb rel8")
        rel = struct.unpack("<b", self.img[jb_va + 1 - IMAGE_BASE:
                                           jb_va + 2 - IMAGE_BASE])[0]
        self.assertEqual(self.resume_to, jb_va + 2 + rel,
                         "循环的回跳目标不是 SPRITE_IB_RESUME_TO")

    def test_the_bail_target_is_begins_own_failure_exit(self):
        # 0x611f3b: pop edi / pop esi / pop ebx / leave / ret 8
        self.assertEqual(b"\x5f\x5e\x5b\xc9\xc2\x08\x00",
                         read_va(self.img, self.bail_to, 7),
                         "SPRITE_IB_BAIL_TO 不是 Begin 的收尾序列")
        # ★ 而且必须和「CreateIndexBuffer 失败」那条 jl 去的是同一个地方 ——
        #   我们造出来的收尾状态和原版那条路逐字节相同，靠的就是这个。
        jl_va = 0x00611858
        self.assertEqual(b"\x0f\x8c", read_va(self.img, jl_va, 2),
                         "0x611858 不是 jl rel32")
        rel = struct.unpack("<i", read_va(self.img, jl_va + 2, 4))[0]
        self.assertEqual(self.bail_to, jl_va + 6 + rel,
                         "CreateIndexBuffer 失败那条 jl 去的不是 SPRITE_IB_BAIL_TO")

    def test_the_index_buffer_slot_is_the_one_begin_itself_uses(self):
        # 0x611834: lea edi,[esi+0x14] —— 槽号写错就会去 Release 别的东西。
        self.assertEqual(bytes([0x8D, 0x7E, self.slot]),
                         read_va(self.img, 0x00611834, 3),
                         "SPRITE_IB_SLOT 和 Begin 自己用的槽对不上")

    def test_the_bail_releases_and_clears_the_buffer(self):
        # 只是「不填就走」会让这一局一直拿没初始化的索引画三角形（懒创建
        # 之后再不会重填）—— 所以必须 Release + 清槽，逼下一帧重来。
        body = self.src[self.src.index("void sprite_ib_guard_detour(void)"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("call dword ptr [ecx + 8]", body, "bail 分支没有 Release")
        self.assertIn("mov  dword ptr [esi + SPRITE_IB_SLOT], 0", body,
                      "bail 分支没把索引缓冲槽清零")
        self.assertIn("SPRITE_IB_HRESULT", body, "bail 分支没有置失败返回值")

    def test_the_failure_log_is_deduped_by_state_not_by_count(self):
        # 锁不上会每次重建都再来一遍。铁律 10：按状态翻转去重。
        self.assertIn("InterlockedExchange(&g_sprite_ib_failing, 1)", self.src)
        self.assertIn("InterlockedExchange(&g_sprite_ib_failing, 0)", self.src)


class LoadingStageGuardPatchTest(_JmpGuardSiteMixin, unittest.TestCase):
    """§58（bug调查/25）—— `LoadingStage::~LoadingStage` 遍历六个座位时，
    全局 `LobbyStage` 已经被清 0（和 §81-A 同一个拆除顺序病）。"""

    PREFIX = "LDSTAGE"

    @classmethod
    def setUpClass(cls):
        super(LoadingStageGuardPatchTest, cls).setUpClass()
        cls.resume_to = c_define(cls.src, "LDSTAGE_RESUME_TO")
        cls.skip_to = c_define(cls.src, "LDSTAGE_SKIP_TO")
        cls.lobby = c_define(cls.src, "LOBBY_STAGE_GLOBAL")

    def test_the_stolen_bytes_really_load_the_lobby_stage_global(self):
        # `mov edi,[<全局>]`：绕道里重跑这一条，全局地址写错就全错。
        self.assertEqual(b"\x8b\x3d" + struct.pack("<I", self.lobby),
                         read_va(self.img, self.va, 6),
                         "站点头 6 字节不是 mov edi,[LOBBY_STAGE_GLOBAL]")

    def test_the_patch_stops_right_before_the_seat_loop(self):
        self.assertEqual(self.va + self.stolen, self.resume_to,
                         "偷的字节数和回跳地址对不上")
        self.assertEqual(b"\x8b\xc6\x8b\xcf", read_va(self.img, self.resume_to, 4),
                         "回跳目标不是 `mov eax,esi / mov ecx,edi`（循环的头）")

    def test_the_loop_back_edge_really_lands_on_the_resume_target(self):
        # 0x46fe21: inc esi / cmp esi,6 / jl <循环头>。★ 同时证明没有别的
        # 跳转会落进被偷走的那 8 个字节。
        jl_va = 0x0046FE25
        self.assertEqual(b"\x46\x83\xfe\x06", read_va(self.img, jl_va - 4, 4),
                         "0x46fe21 不是 inc esi / cmp esi,6")
        self.assertEqual(0x7C, self.img[jl_va - IMAGE_BASE], "不是 jl rel8")
        rel = struct.unpack("<b", self.img[jl_va + 1 - IMAGE_BASE:
                                           jl_va + 2 - IMAGE_BASE])[0]
        self.assertEqual(self.resume_to, jl_va + 2 + rel,
                         "循环的回跳目标不是 LDSTAGE_RESUME_TO")

    def test_the_skip_target_pops_exactly_what_the_entry_pushed(self):
        # 0x46fe27: pop edi / pop esi / mov eax,ebx / pop ebx / ret
        # —— edi 和 esi 是站点**之前**（0x46fdff/0x46fe00）压的，栈是平的；
        #    eax 取 ebx，而走到这里 ebx 必然是 0 ⇒ 返回「没找到」。
        self.assertEqual(b"\x5f\x5e\x8b\xc3\x5b\xc3",
                         read_va(self.img, self.skip_to, 6),
                         "LDSTAGE_SKIP_TO 不是 pop edi/esi ; mov eax,ebx ; pop ebx ; ret")
        self.assertEqual(b"\x56\x57", read_va(self.img, self.va - 2, 2),
                         "站点前面不是 push esi / push edi —— 跳过去栈就不平了")

class TextureWriteGuardPatchTest(_JmpGuardSiteMixin, unittest.TestCase):
    """§60-D（bug调查/25 审计）—— `LockRect` 之后无条件往 `pBits` 写一个 dword。"""

    PREFIX = "TEXWR"

    @classmethod
    def setUpClass(cls):
        super(TextureWriteGuardPatchTest, cls).setUpClass()
        cls.resume_to = c_define(cls.src, "TEXWR_RESUME_TO")
        cls.pbits = c_define(cls.src, "TEXWR_PBITS")

    def test_the_site_sits_right_after_the_unchecked_lockrect(self):
        # 0x5c5dd5: call [ecx+0x4c] = IDirect3DTexture9::LockRect（槽 19）
        self.assertEqual(b"\xff\x51\x4c", read_va(self.img, self.va - 3, 3))

    def test_the_stolen_bytes_are_exactly_the_three_we_replace(self):
        # mov eax,[ebp-pbits] / mov ecx,[ebp+8] / mov [eax],ecx
        self.assertEqual(bytes([0x8B, 0x45, (0x100 - self.pbits) & 0xFF,
                                0x8B, 0x4D, 0x08, 0x89, 0x08]),
                         read_va(self.img, self.va, self.stolen),
                         "偷的 8 个字节不是「取 pBits / 取值 / 写进去」那三条")
        self.assertEqual(self.va + self.stolen, self.resume_to)

    def test_unlockrect_still_runs_after_the_resume_point(self):
        # 没锁上也照旧 Unlock —— 对没锁上的纹理 Unlock 只是返回错误码，无害；
        # 真跳过它反而会让「锁上了但我们不写」那条路漏掉 Unlock。
        tail = read_va(self.img, self.resume_to, 0x10)
        self.assertIn(b"\xff\x50\x50", tail, "落点之后没有 UnlockRect（[eax+0x50]）")


class SpriteFlushGuardPatchTest(_JmpGuardSiteMixin, unittest.TestCase):
    """§60-E（bug调查/25 审计）—— `ID3DXSprite::Flush` 的顶点 `Lock()` 不查返回值。"""

    PREFIX = "FLUSH"

    @classmethod
    def setUpClass(cls):
        super(SpriteFlushGuardPatchTest, cls).setUpClass()
        cls.resume_to = c_define(cls.src, "FLUSH_RESUME_TO")
        cls.ptr = c_define(cls.src, "FLUSH_PTR")
        cls.scratch = c_define(cls.src, "FLUSH_SCRATCH_SZ")

    def test_the_site_sits_right_after_the_unchecked_lock(self):
        self.assertEqual(b"\xff\x51\x2c", read_va(self.img, self.va - 3, 3))
        self.assertEqual(self.va + self.stolen, self.resume_to)

    def test_the_vertex_pointer_slot_is_the_one_the_copy_loop_reads(self):
        # 0x612733: mov ecx,[ebp-0x14] —— 绕道改写的就是这一格。
        self.assertEqual(bytes([0x8B, 0x4D, (0x100 - self.ptr) & 0xFF]),
                         read_va(self.img, 0x00612733, 3),
                         "FLUSH_PTR 和拷贝循环读的那一格对不上")
        self.assertEqual(b"\xf3\xa5", read_va(self.img, 0x0061274E, 2),
                         "0x61274e 不是 rep movsd —— 这条路的形状变了")

    def test_the_scratch_is_big_enough_for_the_loops_own_upper_bound(self):
        # 循环入口 0x61272c: cmp eax,0x4000 / jae —— 顶点数被这条钉死。
        self.assertEqual(b"\x3d\x00\x40\x00\x00", read_va(self.img, 0x0061272C, 5),
                         "循环上界不再是 cmp eax,0x4000")
        self.assertEqual(b"\x73", read_va(self.img, 0x00612731, 1), "不是 jae")
        # 偏移最大 = 0x4000 顶点 × 3 × 8 字节 + 一次拷贝 0x18 dword
        self.assertGreater(self.scratch, 0x4000 * 3 * 8 + 0x18 * 4,
                           "废纸篓不够大，循环能写出界")

    def test_the_detour_redirects_instead_of_null_checking(self):
        # ★ 判空在这里没用：[ebp-0x14] 从没初始化过，Lock 失败时是栈垃圾不是 NULL。
        body = self.src[self.src.index("void flush_guard_detour(void)"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("g_flush_scratch", body, "失败分支没有改指向废纸篓")
        self.assertIn("mov  dword ptr [ebp - FLUSH_PTR], eax", body)
        self.assertIn("g_flush_scratch\n        ? install_jmp_guard(FLUSH_VA",
                      self.src, "废纸篓没分配到时必须整个不装（否则会写 NULL）")


class PresenceInputPatchTest(_JmpGuardSiteMixin, unittest.TestCase):
    """§62 / D53（bug调查/25）—— 在场证据的采样点：窗口过程 `0x40ee1d`。

    这一处和别的补丁不一样：它**不改游戏行为**，只是把服务端看不见的事实
    （键盘 / 鼠标 / 前台）抄一份出来。所以要钉的不是「跳转落在哪」，而是
    **「这个位置真的能看见每一条消息」**和**「标志位没被我们弄脏」**。
    """

    PREFIX = "PRESIN"
    INSTALLER = "try_patch_presence_input"

    @classmethod
    def setUpClass(cls):
        super(PresenceInputPatchTest, cls).setUpClass()
        cls.resume_to = c_define(cls.src, "PRESIN_RESUME_TO")

    def test_the_patch_stops_right_after_the_first_compare(self):
        self.assertEqual(self.va + self.stolen, self.resume_to)
        # 落点是 `je 0x40ee33` —— 它**要用**我们偷走的那条 cmp 设的标志位。
        self.assertEqual(b"\x74\x0f", read_va(self.img, self.resume_to, 2),
                         "落点不是那条 je —— 偷的字节数变了")

    def test_the_detour_reports_before_running_the_stolen_compare(self):
        # ★ 顺序不能反：先 cmp 再 call 的话，报事实那一串会把 ZF 冲掉，
        #   落点那条 je 就跳错了（而且是**随机**跳错，不会当场报错）。
        body = self.src[self.src.index("void presence_input_detour(void)"):]
        body = body[:body.index("\n}\n")]
        self.assertLess(body.index("call presence_note_message"),
                        body.index("cmp  eax, esi"),
                        "绕道先跑了 cmp 才报事实 —— 标志位会被冲掉")
        self.assertIn("pushad", body)
        self.assertIn("popad", body)
        # push/ret 回落点：它俩都不动标志位，`jmp` 也行但这是本文件的惯例。
        self.assertIn("push PRESIN_RESUME_TO", body)

    def test_every_window_message_really_flows_through_this_spot(self):
        # ① 它是 `0x40ee15 jne` 的落点；② 也是 `0x40ee17` 的直落点。
        jne_va = 0x0040EE15
        self.assertEqual(0x75, self.img[jne_va - IMAGE_BASE], "0x40ee15 不是 jne")
        rel = struct.unpack("<b", self.img[jne_va + 1 - IMAGE_BASE:
                                           jne_va + 2 - IMAGE_BASE])[0]
        self.assertEqual(self.va, jne_va + 2 + rel, "那条 jne 不再落在采样点上")
        self.assertEqual(b"\xff\x15", read_va(self.img, 0x0040EE17, 2),
                         "0x40ee17 不再是那句 call（直落路径变了）")

    def test_the_activateapp_switch_is_still_downstream_of_us(self):
        # WM_ACTIVATEAPP 要能被我们看见，那张 switch 就必须在采样点**后面**。
        self.assertEqual(b"\x83\xf8\x1c", read_va(self.img, 0x0040EF58, 3),
                         "0x40ef58 不再是 cmp eax,0x1c")
        self.assertLess(self.va, 0x0040EF58)

    def test_the_four_messages_are_the_ones_we_hard_coded(self):
        # 绕道里是自己按消息号分类的，所以这四个常量必须还在原地。
        for va, msg in ((0x0040EDF9, 0x202), (0x0040EE01, 0x208),
                        (0x0040EE06, 0x205)):
            self.assertEqual(struct.pack("<I", msg),
                             read_va(self.img, va + 1, 4),
                             "%08X 那个鼠标消息号变了" % va)
        self.assertEqual(struct.pack("<I", 0x101),
                         read_va(self.img, 0x0040EE2C + 1, 4), "WM_KEYUP 变了")

    def test_the_original_afk_timer_is_reset_right_after_us(self):
        # ★ 这条是「为什么选这个位置」的依据：原版自己的 90 秒挂机计时器
        #   就在这条路的末尾重置（`0x40ee3d call LobbyStage::ResetIdleTimer`）
        #   —— 也就是说原版认的「玩家有动作」就是这四个消息。
        call_va = 0x0040EE3D
        self.assertEqual(0xE8, self.img[call_va - IMAGE_BASE])
        rel = struct.unpack("<i", read_va(self.img, call_va + 1, 4))[0]
        self.assertEqual(c_define(self.src, "AFK_TIMER_VA"), call_va + 5 + rel,
                         "0x40ee3d 调的不再是 ResetIdleTimer —— 选点的依据没了")

    #: 客户端自己的移动键表在哪（`0x515600`~`0x51576D`，每个键两次
    #: `call 0x429bf0 InputSystem::GetKeyState`）。**别手抄名单** —— 下面那条
    #: 用例现从镜像里把 VK 码扒出来，和 `presence_game_key()` 对。
    MOVE_KEY_TABLE = (0x00515600, 0x00515780)
    GET_KEY_STATE_VA = 0x00429BF0
    #: 白名单里**不在**那张表上的那几个：换枪 / 技能，用户 2026-09-21 给的。
    #: 客户端拿 VK 当下标取键位数组（`[InputSystem+键+0x205]`，V0.2 §183），
    #: 没有 `cmp` 可扒 ⇒ 这几个只能写死，但要在这里点名，免得偷偷长出第 N 个。
    EXTRA_KEYS = ("'1'", "'2'", "'3'", "VK_SHIFT", "VK_LSHIFT", "VK_RSHIFT",
                  "VK_CONTROL", "VK_LCONTROL", "VK_RCONTROL")

    def _move_keys_from_the_image(self):
        """从镜像里扒出客户端**真的**在读的那几个移动键的 VK 码。"""
        lo, hi = self.MOVE_KEY_TABLE
        found, i = set(), lo
        while i < hi:
            off = i - IMAGE_BASE
            if self.img[off] == 0xE8:
                rel = struct.unpack("<i", self.img[off + 1:off + 5])[0]
                if i + 5 + rel == self.GET_KEY_STATE_VA and self.img[off - 2] == 0x6A:
                    found.add(self.img[off - 1])      # 紧挨着的 push imm8
            i += 1
        return found

    def test_the_whitelist_is_the_clients_own_key_table(self):
        """★★ §67：认哪些键是**白名单**，而且名单不是手抄的。

        `A/←/Q` `D/→/E` `W/↑/空格` `S/↓` 是同一组轴的**别名**（客户端没有改键
        功能，VK 码写死在 `0x515600` 那段）—— 少认一个，用那个键走位的真人
        就会被判成挂机。所以这里现扒现对，不留手抄的余地。
        """
        vks = self._move_keys_from_the_image()
        self.assertEqual({0x41, 0x25, 0x51,      # A ← Q
                          0x44, 0x27, 0x45,      # D → E
                          0x57, 0x26, 0x20,      # W ↑ 空格
                          0x53, 0x28}, vks,      # S ↓
                         "客户端的移动键表变了 —— 白名单要跟着改")
        body = self.src[self.src.index("static int presence_game_key"):]
        body = body[:body.index("\n}\n")]
        named = {0x41: "'A'", 0x51: "'Q'", 0x44: "'D'", 0x45: "'E'",
                 0x57: "'W'", 0x53: "'S'", 0x25: "VK_LEFT", 0x27: "VK_RIGHT",
                 0x26: "VK_UP", 0x28: "VK_DOWN", 0x20: "VK_SPACE"}
        for vk in sorted(vks):
            self.assertIn(named[vk], body,
                          "白名单漏了 VK 0x%02X（%s）" % (vk, named[vk]))
        for token in self.EXTRA_KEYS:
            self.assertIn(token, body, "白名单漏了 %s" % token)

    def test_the_menu_keys_are_not_evidence(self):
        """★ 用户 2026-09-21 点名的那一条：**F5 不算**。

        它只在开局前按，连点器每局按一下就把 60 秒回溯量整段洗白
        （bug调查/26 的一半根因）。这里钉的是「白名单里没有它」。
        """
        body = self.src[self.src.index("static int presence_game_key"):]
        body = body[:body.index("\n}\n")]
        for vk in ("VK_F5", "VK_RETURN", "VK_ESCAPE", "VK_TAB", "VK_F1"):
            self.assertNotIn(vk, body, "%s 混进白名单了" % vk)
        self.assertIn("default:", body, "没有兜底分支 —— 白名单变黑名单了")


class SeatGetterPatchTest(unittest.TestCase):
    """§60-C（bug调查/25 审计）—— 两个座位取值器改成「先查 this/base」。

    ★ 这两处是**原地等长改 3 个字节**，不是跳板：`jl`(负数) + `jge`(≥6) 两条
      检查用**无符号 `jae`** 一条就够，省出来的两字节正好给 `test`。
    """

    # (常量前缀, 站点偏移 -> (改前, 改后), 判据寄存器的 test 字节)
    SITES = {
        "SEATGET": {1: (0xC0, 0xC9), 2: (0x7C, 0x74), 7: (0x7D, 0x73)},
        "SEATPTR": {1: (0xC9, 0xD2), 2: (0x7C, 0x74), 7: (0x7D, 0x73)},
    }

    @classmethod
    def setUpClass(cls):
        for path in (BSHOOK, IMG):
            if not os.path.isfile(path):
                raise unittest.SkipTest("不在源码仓库里（缺 %s），跳过" % path)
        cls.img = load_image()
        cls.src = c_source()

    def _site(self, pfx):
        va = c_define(self.src, pfx + "_VA")
        sig = c_byte_array(self.src, pfx + "_SIG")
        self.assertEqual(c_define(self.src, pfx + "_SIG_LEN"), len(sig))
        return va, sig

    def test_the_signatures_match_the_image_and_are_unique(self):
        for pfx in self.SITES:
            va, sig = self._site(pfx)
            self.assertEqual(read_va(self.img, va, len(sig)), sig,
                             "%s_SIG 和镜像对不上" % pfx)
            self.assertEqual(1, self.img.count(sig), "%s_SIG 不唯一" % pfx)

    def test_every_poked_byte_is_where_the_source_says_it_is(self):
        for pfx, pokes in self.SITES.items():
            va, sig = self._site(pfx)
            for off, (expect, _want) in pokes.items():
                self.assertEqual(expect, sig[off],
                                 "%s 偏移 %d 的原字节不是 0x%02X" % (pfx, off, expect))
                self.assertIn("poke_imm8(%s_VA + %d, 0x%02X, 0x%02X"
                              % (pfx, off, expect, _want), self.src,
                              "源码里没有这一次 poke")

    def test_the_patch_keeps_the_function_exactly_as_long(self):
        # 等长是这个改法成立的全部理由：错一个字节，后面整段就歪了。
        for pfx, pokes in self.SITES.items():
            va, sig = self._site(pfx)
            after = bytearray(sig)
            for off, (_e, want) in pokes.items():
                after[off] = want
            self.assertEqual(len(sig), len(after))
            # 两条 jcc 的 rel8 一个字节都没动 ⇒ 落点还是原来那个「返回 0」出口
            self.assertEqual(sig[3], after[3], "第一条 jcc 的 rel8 被动了")
            self.assertEqual(sig[8], after[8], "第二条 jcc 的 rel8 被动了")

    def test_both_conditional_jumps_land_on_the_return_zero_tail(self):
        for pfx in self.SITES:
            va, sig = self._site(pfx)
            t1 = va + 4 + struct.unpack("<b", bytes([sig[3]]))[0]
            t2 = va + 9 + struct.unpack("<b", bytes([sig[8]]))[0]
            self.assertEqual(t1, t2, "%s 两条 jcc 落点不一样" % pfx)
            self.assertEqual(b"\x33\xc0", read_va(self.img, t1, 2),
                             "%s 的落点不是 xor eax,eax（返回 0）" % pfx)

    def test_the_unsigned_compare_really_covers_the_negative_case(self):
        # 换掉 `jl` 之后，负下标只剩 `jae` 兜着 —— 所以 `cmp` 的立即数必须还在。
        for pfx in self.SITES:
            va, sig = self._site(pfx)
            self.assertEqual(0x83, sig[4], "%s 偏移 4 不是 cmp r32,imm8" % pfx)
            self.assertEqual(6, sig[6], "%s 的座位上界不是 6" % pfx)

    def test_the_three_pokes_are_written_in_the_safe_order(self):
        # 三个字节不是原子写的。顺序必须是 test -> jae -> jz：
        # 任何一个中间态都不能比原版弱（理由写在 bshook.c 的注释里）。
        for pfx in self.SITES:
            body = self.src[self.src.index("c1 = sig_tail_ok"):]
            body = body[:body.index("d = install_jmp_guard")]
            order = [int(m, 0) for m in
                     re.findall(r"poke_imm8\(%s_VA \+ (\d+)," % pfx, body)]
            self.assertEqual([1, 7, 2], order,
                             "%s 的 poke 顺序不是 test -> jae -> jz" % pfx)

    def test_the_getter_is_reached_by_the_three_second_level_getters(self):
        # 补取值器能顺带保护它们，全靠这三处都是「先调它、al 为假就走空分支」。
        for caller in (0x0040460E, 0x0040462C, 0x00404D9E):
            head = read_va(self.img, caller, 12)
            self.assertEqual(b"\x56\x8b\xcf\x8b\xf0\xe8", head[:6],
                             "%08X 不再是 push esi/mov ecx,edi/mov esi,eax/call" % caller)
            rel = struct.unpack("<i", head[6:10])[0]
            self.assertEqual(c_define(self.src, "SEATGET_VA"), caller + 10 + rel,
                             "%08X 调的不是 SEATGET_VA" % caller)
            self.assertEqual(b"\x84\xc0", head[10:12],
                             "%08X 调完没有 test al,al" % caller)


class MoverLinkPatchTest(_JmpGuardSiteMixin, unittest.TestCase):
    """X_Mod §74 —— `MapObject::LinkPath` 收尾那条 `mov [esi+0x90], eax`（存 t0）。

    `bshook` 在这儿记下每个挂在路径上的对象的 (link, t0, t_off)，经 UDP 旁路报给
    服务端，服务端的移动平台判定按客户端**实际的**相位走。
    """

    PREFIX = "MOVERLK"
    INSTALLER = "try_patch_mover_link"

    @classmethod
    def setUpClass(cls):
        super(MoverLinkPatchTest, cls).setUpClass()
        cls.resume_to = c_define(cls.src, "MOVERLK_RESUME_TO")

    def test_the_resume_target_is_the_epilogue(self):
        # 偷走的正好是那条 6 字节的 mov，落点就是 pop edi / pop esi / ret
        self.assertEqual(self.va + self.stolen, self.resume_to)
        self.assertEqual(b"\x5f\x5e\xc3", read_va(self.img, self.resume_to, 3))

    def test_the_site_sits_right_after_the_timer_call(self):
        # 前一条是 `call 0x40a01e`（Timer()）—— 站点上 eax 就是刚取的 t0
        self.assertEqual(b"\xe8", read_va(self.img, self.va - 5, 1))
        rel = struct.unpack("<i", read_va(self.img, self.va - 4, 4))[0]
        self.assertEqual(0x0040A01E, self.va + rel)

    def test_the_site_is_inside_link_path(self):
        # 函数头 `push esi / mov esi,ecx / mov eax,[esi+0xfc] / test eax,eax / je`：
        # 只有 [this+0xfc]（挂在哪个对象上）非零的对象才走到站点
        head = read_va(self.img, 0x00511D60, 13)
        self.assertEqual(b"\x56\x8b\xf1\x8b\x86\xfc\x00\x00\x00\x85\xc0\x74\x31", head)


class MoverStartPatchTest(_JmpGuardSiteMixin, unittest.TestCase):
    """X_Mod §78 —— 开打时 `GameContext::StartGame` 把所有移动平台的起点 t0 重取一遍。

    `0x476435`（World 的方法）遍历所有对象，`[obj+0x8c]`（follower 挂着的路径）非空就
    `call Timer(); mov [esi+0x90], eax`。战斗里鲤鱼按**这个**起点走；会话 30 只挂了
    LinkPath 那一处，报上去的起点早了整整一段加载等待（2026-09-23 实测 8.46 s / 6.16 s）。
    """

    PREFIX = "MOVERST"
    INSTALLER = "try_patch_mover_link"

    @classmethod
    def setUpClass(cls):
        super(MoverStartPatchTest, cls).setUpClass()
        cls.resume_to = c_define(cls.src, "MOVERST_RESUME_TO")

    def test_the_resume_target_is_the_next_instruction(self):
        # 偷走的正好是那条 6 字节的 mov，落点是 `lea eax, [ebp-8]`（取下一个对象）
        self.assertEqual(self.va + self.stolen, self.resume_to)
        self.assertEqual(b"\x8d\x45\xf8", read_va(self.img, self.resume_to, 3))

    def test_the_site_sits_right_after_the_timer_call(self):
        self.assertEqual(b"\xe8", read_va(self.img, self.va - 5, 1))
        rel = struct.unpack("<i", read_va(self.img, self.va - 4, 4))[0]
        self.assertEqual(0x0040A01E, self.va + rel)

    def test_only_objects_that_follow_a_path_are_rebased(self):
        # `cmp dword [esi+0x8c], 0 / je +0x0b` —— 跳过站点，正好落在我们的 resume 上
        self.assertEqual(b"\x83\xbe\x8c\x00\x00\x00\x00\x74\x0b",
                         read_va(self.img, 0x00476455, 9))
        self.assertEqual(self.resume_to, 0x0047645E + 0x0B)

    def test_start_game_is_the_one_who_calls_it(self):
        # GameContext 虚表 +0xc = 0x491244；它末尾 `mov ecx,[World] / call 0x476435`
        self.assertEqual(0x00491244,
                         struct.unpack("<I", read_va(self.img, 0x00670B4C + 0xC, 4))[0])
        self.assertEqual(b"\x8b\x0d\xd4\xe2\x72\x00", read_va(self.img, 0x00491382, 6))
        self.assertEqual(b"\xe8", read_va(self.img, 0x00491388, 1))
        rel = struct.unpack("<i", read_va(self.img, 0x00491389, 4))[0]
        self.assertEqual(0x00476435, 0x0049138D + rel)

    def test_every_game_mode_reaches_the_base_start_game(self):
        # 23 个 GameContext 子类的 +0xc 要么就是 0x491244，要么是这三个覆盖之一，
        # 而这三个都 call 0x491244 ⇒ 每种模式开打都会重取起点。
        for site in (0x004990A6, 0x0049C3EB, 0x004A3CD9):
            self.assertEqual(b"\xe8", read_va(self.img, site, 1))
            rel = struct.unpack("<i", read_va(self.img, site + 1, 4))[0]
            self.assertEqual(0x00491244, site + 5 + rel,
                             "%08X 不再调 StartGame 的基类" % site)


class MoverOriginWritersTest(unittest.TestCase):
    """X_Mod §78 —— 起点 t0 的写入点**就这两处**，bshook 挂的 ① ② 一个不漏。

    判据：全镜像「`call Timer()`（0x40a01e）紧跟 `mov [r32+0x90], eax`」。
    （`PathFollower::SetPath` 0x549bd6 另写一次 `[follower+8]`，但它只在 LinkPath 里被调、
    紧接着就被 ① 覆盖。hook 发包时现读对象，就算以后冒出第三处也照样报对。）
    """

    def test_the_two_hooked_sites_are_all_there_is(self):
        for path in (BSHOOK, IMG):
            if not os.path.isfile(path):
                self.skipTest("不在源码仓库里（缺 %s）" % path)
        img = load_image()
        src = c_source()
        found = set()
        for m in re.finditer(br"\xe8(....)\x89[\x80-\x87]\x90\x00\x00\x00", img, re.S):
            call_va = IMAGE_BASE + m.start()
            if call_va + 5 + struct.unpack("<i", m.group(1))[0] == 0x0040A01E:
                found.add(call_va + 5)
        self.assertEqual({c_define(src, "MOVERLK_VA"), c_define(src, "MOVERST_VA")}, found)


class ImeNativeUiPatchTest(unittest.TestCase):
    """X14 / §106 / D70 —— 游戏不再接候选通知、不吞 WM_IME_SETCONTEXT、替输入法回答光标位置。

    补丁只改 `0x40edcb` 那条 call 的 rel32；下面几条钉的是它赖以成立的事实：
    这条 call 真是去 `ImeContext` 的消息处理、而且是唯一调用点（改一处就管住全部消息），
    rel32 四字节对齐（运行中原子替换），原函数在 WM_IME_NOTIFY 里拦的正好是我们接走的
    3 / 4 / 5 / 9，以及算光标位置用的控件几何偏移和 800×600 模式的 1.28 缩放。
    """

    HANDLER = 0x00428F45

    @classmethod
    def setUpClass(cls):
        for path in (BSHOOK, IMG):
            if not os.path.isfile(path):
                raise unittest.SkipTest("不在源码仓库里（缺 %s），跳过" % path)
        cls.img = load_image()
        cls.src = c_source()
        cls.va = c_define(cls.src, "IME_MSG_CALL_VA")
        cls.sig = c_byte_array(cls.src, "IME_MSG_SIG")

    def test_signature_matches_image(self):
        self.assertEqual(c_define(self.src, "IME_MSG_SIG_LEN"), len(self.sig))
        self.assertEqual(read_va(self.img, self.va, len(self.sig)), self.sig)

    def test_call_goes_to_ime_handler(self):
        rel = struct.unpack("<i", read_va(self.img, self.va + 1, 4))[0]
        self.assertEqual(self.va + 5 + rel, self.HANDLER)
        self.assertEqual(c_define(self.src, "IME_MSG_HANDLER_JMP"), self.HANDLER)

    def test_it_is_the_only_call_site(self):
        sites = []
        for m in re.finditer(br"\xe8(....)", self.img, re.S):
            call_va = IMAGE_BASE + m.start()
            if call_va + 5 + struct.unpack("<i", m.group(1))[0] == self.HANDLER:
                sites.append(call_va)
        self.assertEqual([self.va], sites)

    def test_rel32_is_dword_aligned(self):
        # 运行中只换这 4 个字节，靠 InterlockedExchange 原子替换 —— 前提是对齐。
        self.assertEqual((self.va + 1) % 4, 0)

    # 原函数整段 0x428f45 ~ 0x4292a1（以 0x42929e 的 `ret 0xc` 结尾），860 字节。
    HANDLER_END = 0x004292A1
    HANDLER_SHA256 = "77b2d3acf13e61a83a74902bcf3f96482cc2a2fb2f42f70411316da0f8b555ab"

    def test_handler_bytes_pinned(self):
        # 「原函数照跑、然后吞掉」那条路要在 thunk 里真 call 原函数、调完接着用 esi（ImeContext）；
        # 这段字节里一条写 esi 的指令都没有（2026-09-26 用 capstone 逐条核过，下一条有 capstone 时
        # 会再核一遍）。测试运行时里没有 capstone，所以这里把整段字节钉死：字节不变，结论就不变。
        import hashlib
        code = read_va(self.img, self.HANDLER, self.HANDLER_END - self.HANDLER)
        self.assertEqual(code[-3:], bytes.fromhex("c20c00"))                    # ret 0xc
        self.assertEqual(hashlib.sha256(code).hexdigest(), self.HANDLER_SHA256)

    def test_handler_never_writes_esi(self):
        # 同上，逐条核：原函数一条写 esi 的指令都没有，esi 进出不变。只在装了 capstone 的开发机上跑。
        deps = os.path.join(ROOT, "tools", "_pydeps")
        if os.path.isdir(deps) and deps not in sys.path:
            sys.path.insert(0, deps)
        try:
            import capstone
            md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        except Exception as e:      # 32 位运行时载不进 x64 的 capstone 原生库，是 OSError 不是 ImportError
            self.skipTest("capstone 用不了（%s）" % e)
        end = 0x42929E
        self.assertEqual(read_va(self.img, end, 3), bytes.fromhex("c20c00"))  # ret 0xc
        code = read_va(self.img, self.HANDLER, end + 3 - self.HANDLER)
        writes = []
        for insn in md.disasm(code, self.HANDLER):
            dst = insn.op_str.split(",")[0].strip()
            if dst == "esi" and insn.mnemonic not in ("push", "cmp", "test"):
                writes.append("%08X %s %s" % (insn.address, insn.mnemonic, insn.op_str))
            if insn.mnemonic in ("pushal", "popal"):
                writes.append("%08X %s" % (insn.address, insn.mnemonic))
        self.assertEqual([], writes)

    def test_handler_takes_exactly_these_notifications(self):
        # 0x428f50: cmp [ebp+8], WM_IME_NOTIFY；jne …；eax = wParam；
        # sub 3 → je（CHANGE）/ dec → je（CLOSE）/ dec → je（OPEN）/ sub 4 → je（SETCANDIDATEPOS）
        self.assertEqual(read_va(self.img, 0x428F50, 7), bytes.fromhex("817d0882020000"))
        self.assertEqual(read_va(self.img, 0x428F60, 3), bytes.fromhex("83e803"))
        self.assertEqual(read_va(self.img, 0x428F69, 1), b"\x48")
        self.assertEqual(read_va(self.img, 0x428F6C, 1), b"\x48")
        self.assertEqual(read_va(self.img, 0x428F6F, 3), bytes.fromhex("83e804"))
        # 结尾：返回值 = (msg == WM_IME_SETCONTEXT) —— 原版就是这样吞掉它的
        self.assertEqual(read_va(self.img, 0x429287, 7), bytes.fromhex("817d0881020000"))
        self.assertEqual(read_va(self.img, 0x42928E, 3), bytes.fromhex("0f94c0"))

    def test_ui_geometry_offsets(self):
        # SumRect：沿 +0x28 父链累加 +0x10 / +0x14，宽高取 +0x18 / +0x1c
        self.assertEqual(read_va(self.img, 0x42516A, 9), bytes.fromhex("037110 037914 8b4928"))
        self.assertEqual(read_va(self.img, 0x425177, 6), bytes.fromhex("8b4a1c8b5218"))
        # 自带框布局：[Desktop+0x10] = 聚焦的输入框，光标矩形在它 +0x110
        self.assertEqual(read_va(self.img, 0x43019C, 8), bytes.fromhex("a1b4e272008b7010"))
        self.assertEqual(read_va(self.img, 0x4301B4, 6), bytes.fromhex("81c610010000"))
        self.assertEqual(c_define(self.src, "IME_UI_DESKTOP_PP"), 0x72E2B4)
        # ImeContext 构造：[esi+4] = 主窗口（回答屏幕坐标时拿它做 ClientToScreen）
        self.assertEqual(read_va(self.img, 0x428D02, 5), bytes.fromhex("53895e04c6"))

    def test_800x600_mode_scale(self):
        # 0x40f3ab: cmp [App+0x98], 2 → fmul qword [0x693860]（客户区 × 1.28 = 界面）
        self.assertEqual(read_va(self.img, 0x40F3AB, 7), bytes.fromhex("83bf9800000002"))
        self.assertEqual(read_va(self.img, 0x40F3B7, 6), bytes.fromhex("dc0d60386900"))
        self.assertEqual(struct.unpack("<d", read_va(self.img, 0x693860, 8))[0], 1.28)
        self.assertEqual(c_define(self.src, "IME_UI_SCALE_VA"), 0x693860)
        self.assertEqual(c_define(self.src, "IME_UI_MODE_800X600"), 2)
        self.assertEqual(c_define(self.src, "IME_UI_APP_PP"), 0x72E2A4)


if __name__ == "__main__":
    unittest.main()
