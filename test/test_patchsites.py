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
        # 定义了没人装 = 白写。安装侧至少出现两次（定义 + 调用）。
        self.assertGreaterEqual(self.src.count("try_patch_crash25_guards"), 3,
                                "try_patch_crash25_guards 定义了但没有人调用")
        self.assertIn("%s_VA" % self.PREFIX, self.src.split(
            "static int try_patch_crash25_guards(void)")[1],
            "%s 这一处没有被 try_patch_crash25_guards 装上" % self.PREFIX)


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


if __name__ == "__main__":
    unittest.main()
