#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mapdata.py —— 读 `bot_mapdata/` 里的**地图地形数据**（V0.3 M4）。

★ 它**不在 `data/` 下面**：`data/` 按约定只装**用户数据**
（`accounts.json` / `tickets.json`，都是 `.gitignore` 掉的运行时状态）。
地形数据是**随代码走的产物** —— 进 git、进两个发布包，和 `*.py` 同级对待。

数据是 `tools/mapdata.py` 从原版 `.map` 里离线抽出来的 ——
**逐像素抄的是客户端自己那份碰撞位图**（`.map` 尾部烘着的 `TerrainData`），
不是从贴图 alpha 拼的。所以这里的 `cell(x, y)` 和客户端的
`TerrainData::Get`（`0x472fe0`）返回同一个值。

    0  空
    1  ★ **单向平台**（游戏里那种细白线）：站得上去、按 ↓ 能穿下去、
       从下往上跳能穿过去，而且**完全不挡子弹**（§29）
    2  实心
    3  ★ **可破坏物**（冰块 / 木箱）。挡人也挡子弹，和实心一样 ——
       打碎之前它就是一堵墙（§136）

    ★ **出界返回 2**，和客户端一致。别改成「出界算空」——
      那会让 bot 觉得图外能走。

## ★ 「挡人」和「挡子弹」是**两个**判据，别混用

    is_solid(x, y)       挡住行走 / 接住下落  -> cell != 0（含单向平台、冰块）
    blocks_bullet(x, y)  挡住子弹             -> cell >= 2（★ 单向平台不挡，冰块挡）

判据来自客户端弹体的扫掠碰撞 `0x47f976`：值 2/3 恒挡，值 1 那条分支还要
一个虚函数点头，而 19 个弹体类**全部**用的是默认实现 `xor al,al ; ret`
—— 那条分支永远走不到。混用的后果是 bot 觉得隔着一根白线打不到人，
明明打得到。

## 铁律：这个模块只用标准库

服务端的便携运行时里没有 Pillow、没有 numpy。这里只有 `json` / `zlib` /
`base64` / `struct`，CPython 3.8（Win7 运行时）也能跑。

## 名字怎么对上

客户端发过来的地图名有两种花样：

    `Megatron_b:NewPvp`     房间里的地图串，`:` 后面是玩法模式
    `Quest02_2`             换图请求（`0x0416`）给的是**基名**，
                            真正的文件是 `Quest02_2#Normal.map` 那一组

`load()` 两种都吃：先切掉 `:` 之后的部分，再按 `精确名 -> #Normal ->
#Easy -> #Hard -> #Extreme -> 该基名下任意一个` 的顺序找。
四个难度版本的地形**多数**是同一份，但不是全部（`Boss00#Extreme`
和另外三个就不一样），所以顺序写死在这里，别靠字典序碰运气。
"""
import base64
import json
import math
import os
import struct
import zlib

#: 认得的产物格式版本。对不上就当没有数据 —— 宁可 bot 不会走，
#: 也不要按错的布局解出一张乱七八糟的地图。
#:
#: 3：`cells` 里多了值 3 = **可破坏物**（冰块 / 木箱，§136）。
#:    2 那一版的产物里冰块那一块是**空的**，bot 会直接穿过去。
#: 4：可破坏物改成**单独一层** `breakables`（形状 / 血量 / 恢复延迟），
#:    `cells` 退回不含它们的原样网格 —— 打碎了要放行，过一阵原样长回来
#:    （§138）。合成在 `MapTerrain` 里做。
#: 5：破坏物多一格 `handle` = **世界句柄**（§139），`rpSplashDamaged +4`
#:    填的就是它。
#: 6：索引里多一层 `props` = `Data/map.ini` 的地图属性（现在只有
#:    **`FallDown`** —— 这张图掉出去会不会死，§143）。
FORMAT = 6

#: 找不到精确名、**也没人告诉我们难度**时按这个顺序退。
#: ⚠ 这只是最后的兜底 —— 闯关房请一律把难度传进来（见 `DIFFICULTY_SUFFIX`）。
DIFFICULTY_ORDER = ("#Normal", "#Easy", "#Hard", "#Extreme")

#: ★★★ 闯关房的**难度 -> 地图文件后缀**（§140）。
#:
#: 出处是客户端自己拼文件名那一段（`0x405742`，房间描述符 `type == 2` 时）：
#:
#:     mov edi, [房间+0x24]        ; 描述符的第 2 个参数 = 难度
#:     dec edi ; je -> push 0x65e9ac (#Easy)
#:     dec edi ; je -> push 0x65e99c (#Normal)
#:     dec edi ; je -> push 0x65e990 (#Hard)
#:     dec edi ; jne 跳过 -> push 0x65e97c (#Extreme)
#:
#: 也就是 **1=简单 2=普通 3=困难 4=极限**，和 `Data/Quest/QuestNN/mob-1..4.ini`
#: 那四份怪物表一一对应。
#:
#: ★ 为什么非有不可：四个难度的 `.map` **不是同一张图**。`Quest03_1` 的四份
#:   宽度是 10600 / 11400 / 11350 / 11350，从 x≈4500 起几乎每一列都不一样。
#:   不传难度就恒退到 `#Normal`，于是玩「简单」时服务端手上是另一张图 ——
#:   bot 从真实的树里穿过去、在真实的空地上腾空走路，A\* 算出来的路线
#:   在真人屏幕上根本不成立（用户 2026-08-30 实机）。
DIFFICULTY_SUFFIX = {1: "#Easy", 2: "#Normal", 3: "#Hard", 4: "#Extreme"}

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "bot_mapdata")

#: 出界的格值。照抄客户端 `0x472fe0`。
OUT_OF_BOUNDS = 2

#: 可破坏物在网格里的格值。客户端自己的命中判定（`BreakableObj::HitTest`
#: = `0x4fa7b5`）认的就是 3，而原版 174 张烘焙网格里一个 3 都没有。
BREAKABLE_CELL = 3

#: 一字节 4 格 -> 4 个字节（值仍是 0..3）。合成破坏物时重算站立面用。
_UNPACK = tuple(bytes(((b >> 0) & 3, (b >> 2) & 3, (b >> 4) & 3, (b >> 6) & 3))
                for b in range(256))
#: 再把 0..3 压成「非空 = 1」。和 `tools/mapdata.py` 的 `_SOLID` 同一张表。
_SOLID = bytes((0, 1, 1, 1)) + bytes(252)
#: 压成「**挡子弹** = 1」（值 ≥ 2；单向平台 1 不挡，见文件头 §29）。
#: `bullet_coarse()` 用它。
_BLOCKS_BULLET = bytes((0, 0, 1, 1)) + bytes(252)

#: ★★ **粗网格的块边长**（像素），`bullet_coarse()` 用。
#:
#: 为什么要有粗网格（用户 2026-09-01 的「子弹多就卡」）：
#: `bot._terrain_contact()` 是**逐像素**扫的（`BOT_SHELL_TERRAIN_STEP = 1`，
#: 那个 1 是原版口径，不能改），实测空中飞 300 像素的一颗弹要 **270 µs**，
#: 而每颗在飞的弹每 32 ms 都要算一次 —— 10 颗就吃掉 2.7 ms / 32 ms。
#: 有了粗网格，成片的空气可以**整块跳过**，逐像素只留给真的贴着地形的那几步。
#:
#: 16 是权衡出来的，不是拍的：块越大跳得越远，但「这一块里有地形」的
#: 误判面积按平方涨（贴地飞的弹会整段退回逐像素）。16×16 让最大的图
#: （11350×768）也只要 34 KB，而空中一步能跳过平均 7~8 个像素。
COARSE_SHIFT = 4
COARSE = 1 << COARSE_SHIFT


class Breakable(object):
    """一件**可破坏物**（冰块 / 木箱）—— 形状、血量、碎了多久长回来。

    数据全是原版的（`tools/mapdata.py` 的 `collect_breakables` 从 `.map`
    里抽的）：

    * `hp` —— `BreakableObj::Deserialize`（`0x4fa3e7`）读的第一个 i32；
    * `regen_ms` —— 第二个 i32（缺省 15000，构造函数 `0x4fa379` 写死的），
      `BreakableObj::Tick`（`0x4fa4e9`）拿它判「碎了多久原样长回来」。

    `rows` 是**预先算好的贴图行**：`(起字节, 止字节, 或运算位图)`。
    合成一份地形 = 从不含破坏物的原网格出发，把还活着的这几件 **OR** 上去
    —— 一行一次大整数运算，走 C 层，不逐格循环。
    """

    __slots__ = ("index", "handle", "x", "y", "left", "top", "width",
                 "height", "hp", "regen_ms", "rows", "mask",
                 "col0", "col1", "row0", "row1")

    def __init__(self, index, record, map_width, map_height):
        self.index = index
        #: ★★ **世界句柄**（§139）：`rpSplashDamaged +4` 填的就是它。
        #:   服务端靠它认「真人把哪一块打碎了」，也靠它让 bot 打碎的那一下
        #:   在别人屏幕上生效。0 = 产物是老格式，没这一格。
        self.handle = int(record.get("handle", 0))
        self.x = int(record["x"])
        self.y = int(record["y"])
        self.width = int(record["w"])
        self.height = int(record["h"])
        self.hp = int(record.get("hp", 40))
        self.regen_ms = int(record.get("regen", 15000))
        self.left = self.x - self.width // 2
        self.top = self.y - self.height // 2
        mask = _unblob(record["mask"])
        #: 形状本身留着：命中判定要**逐格**问「这一点在不在它身上」
        #: （客户端 `BreakableObj::HitTest` = `0x4fa7b5`）。2 bit/格。
        self.mask = mask
        rows = []
        col0, col1 = map_width, -1
        row0, row1 = map_height, -1
        for my in range(self.height):
            ty = self.top + my
            if ty < 0 or ty >= map_height:
                continue
            base = my * self.width
            filled = []
            for mx in range(self.width):
                tx = self.left + mx
                if tx < 0 or tx >= map_width:
                    continue
                i = base + mx
                if (mask[i >> 2] >> ((i & 3) * 2)) & 3:
                    filled.append(tx)
            if not filled:
                continue
            lo, hi = filled[0], filled[-1]
            col0 = min(col0, lo)
            col1 = max(col1, hi)
            row0 = min(row0, ty)
            row1 = max(row1, ty)
            first = (ty * map_width + lo) >> 2
            last = ((ty * map_width + hi) >> 2) + 1
            pattern = bytearray(last - first)
            for tx in filled:
                i = ty * map_width + tx
                pattern[(i >> 2) - first] |= BREAKABLE_CELL << ((i & 3) * 2)
            rows.append((first, last, int.from_bytes(bytes(pattern), "big")))
        self.rows = tuple(rows)
        self.col0, self.col1 = col0, col1
        self.row0, self.row1 = row0, row1

    def covers(self, x, y):
        """(x, y) 在这件东西的**外接矩形**里吗。"""
        return (self.left <= x < self.left + self.width
                and self.top <= y < self.top + self.height)

    def hit(self, x, y):
        """★★★ (x, y) 打在这件东西身上了吗 —— **逐格问形状**。

        照抄客户端 `BreakableObj::HitTest`（`0x4fa7b5`）：把世界坐标换算成
        局部坐标（`局部 = 世界 − 左上角`，`0x51a935` 那一句），再看
        **3×3 邻域**里有没有一格是它（`cmp al, 3`）。
        """
        mx = int(x) - self.left
        my = int(y) - self.top
        for dy in (-1, 0, 1):
            ny = my + dy
            if ny < 0 or ny >= self.height:
                continue
            base = ny * self.width
            for dx in (-1, 0, 1):
                nx = mx + dx
                if nx < 0 or nx >= self.width:
                    continue
                i = base + nx
                if (self.mask[i >> 2] >> ((i & 3) * 2)) & 3:
                    return True
        return False

    @property
    def radius(self):
        """★ 它在伤害衰减里的半径 —— `(宽 + 高) / 2`（§139）。

        出处 `BreakableObj` 虚表槽 `+0x7c` = `0x50d8a1`：
        `(h + w) × [[this+0x13c]+0x18] × 0.5`，中间那个系数的构造缺省是
        **1.0**（`0x4c9946: fld1; fstp [esi+0x18]`），而 677 件的缩放全是 1。
        角色那一侧同一个槽返回的是写死的 **35**（§90）。
        """
        return (self.width + self.height) / 2.0

    def distance_to(self, x, y):
        """(x, y) 到这件东西外接矩形的距离；在里面就是 0。"""
        dx = max(self.left - x, 0.0, x - (self.left + self.width))
        dy = max(self.top - y, 0.0, y - (self.top + self.height))
        return math.hypot(dx, dy)

    def __repr__(self):
        return "<Breakable #%d (%d,%d) %dx%d hp=%d regen=%dms>" % (
            self.index, self.x, self.y, self.width, self.height,
            self.hp, self.regen_ms)


class MapTerrain(object):
    """一张图的地形。**只读** —— 破坏物状态变了要换一个对象（`variant()`）。

    ★★ 「只读」这条是 `botnav` 那份可达图缓存的**前提**：缓存按地形对象
      本身做弱引用键，一个对象 = 一张可达图。所以「哪几件破坏物还在」
      不能做成这个对象上的可变状态，只能一个状态一份地形（§138）。
      同一个存活集合永远拿到**同一个对象** ⇒ 那张图也只算一次。
    """

    #: ★ `__weakref__` 是给 `botnav` 的可达图缓存留的：那份缓存按**地形对象
    #:   本身**做弱引用键（图名不行 —— 单测里的合成地形全叫 `Tiny`），
    #:   地形被丢掉时缓存自然跟着没。地形本身仍然是只读的。
    __slots__ = ("name", "version", "width", "height", "_cells",
                 "_offsets", "_ys", "points", "jump_pads", "__weakref__",
                 "breakables", "alive", "_base_cells", "_base_offsets",
                 "_base_ys", "_root", "_variants", "_coarse", "_base_coarse")

    def __init__(self, record):
        self.name = record["name"]
        self.version = record["version"]
        self.width = record["width"]
        self.height = record["height"]
        self._base_cells = _unblob(record["cells"])
        counts = struct.unpack(
            "<%dH" % self.width, _unblob(record["ground_counts"]))
        ys_raw = _unblob(record["ground_ys"])
        self._base_ys = struct.unpack("<%dH" % (len(ys_raw) // 2), ys_raw)
        # 前缀和：第 x 列的站立面是 _ys[_offsets[x]:_offsets[x+1]]。
        offsets = [0] * (self.width + 1)
        acc = 0
        for i, n in enumerate(counts):
            acc += n
            offsets[i + 1] = acc
        self._base_offsets = offsets
        self.points = dict((int(k), [tuple(p) for p in v])
                           for k, v in record.get("points", {}).items())
        #: ★ **弹跳台**（V0.3 §99）：`[(台x, 台y, 落点dx, 落点dy), …]`。
        #:   `dx/dy` 是**落点相对台子的偏移**，不是速度 —— 客户端
        #:   `JumpingObj::Tick`（`0x510d05`）拿它现解一条抛物线。
        self.jump_pads = tuple(
            (float(a), float(b), float(c), float(d))
            for a, b, c, d in record.get("jump", ()))
        self.breakables = tuple(
            Breakable(i, item, self.width, self.height)
            for i, item in enumerate(record.get("breakables", ())))
        self._root = self
        self._variants = {}
        #: 粗网格（弹道加速）。`_base_coarse` 只存在于**根地形**上，
        #: 所有 variant 共用它；`_coarse` 是本 variant 自己那份。
        self._base_coarse = None
        # 缺省状态 = **全都还在**：原版里碎了会自己长回来，完好才是稳态。
        self._compose(frozenset(range(len(self.breakables))))

    # -- 破坏物合成 ---------------------------------------------------------

    def _compose(self, alive):
        """把 `alive` 这几件破坏物贴上去，重算被盖住的那几列站立面。"""
        self.alive = alive
        # ★ `_cells` 要重建，挂在它上面的粗网格跟着作废（懒重建）。
        self._coarse = None
        items = [b for b in self.breakables if b.index in alive]
        if not items:
            self._cells = self._base_cells
            self._offsets = self._base_offsets
            self._ys = self._base_ys
            return
        cells = bytearray(self._base_cells)
        for item in items:
            for first, last, pattern in item.rows:
                chunk = int.from_bytes(cells[first:last], "big") | pattern
                cells[first:last] = chunk.to_bytes(last - first, "big")
        self._cells = bytes(cells)
        # ★★ 站立面只重算**被盖住的那一片矩形**，而且用和 `extract_ground`
        #    同一个位运算手法：「这一行非空 且 上一行是空」= `cur & ~prev`，
        #    一次大整数运算出一整行的上沿。逐格 `cell()` 问的话，
        #    `CamelCulvert_br2`（44 件）要 240 ms —— 那就是一次卡顿。
        x0 = max(0, min(item.col0 for item in items))
        x1 = min(self.width - 1, max(item.col1 for item in items))
        # ★ 下界多看一格：盖住 y=hi 之后，原来 y=hi+1 那个站立面就不再
        #   满足「正上方是空」了。
        y0 = max(1, min(item.row0 for item in items))
        y1 = min(self.height - 1, max(item.row1 for item in items) + 1)
        span = x1 - x0 + 1
        fresh = {}
        prev = self._solid_row(y0 - 1, x0, span)
        for y in range(y0, y1 + 1):
            cur = self._solid_row(y, x0, span)
            edge = cur & ~prev
            prev = cur
            if not edge:
                continue
            row = edge.to_bytes(span, "big")
            start = 0
            while True:
                idx = row.find(1, start)
                if idx < 0:
                    break
                fresh.setdefault(x0 + idx, []).append(y)
                start = idx + 1
        column = {}
        base_ys, base_offsets = self._base_ys, self._base_offsets
        for x in range(x0, x1 + 1):
            keep = [y for y in base_ys[base_offsets[x]:base_offsets[x + 1]]
                    if y < y0 or y > y1]
            column[x] = sorted(keep + fresh.get(x, []))
        ys = []
        offsets = [0] * (self.width + 1)
        acc = 0
        for x in range(self.width):
            seg = column.get(x)
            if seg is None:
                seg = base_ys[base_offsets[x]:base_offsets[x + 1]]
            ys.extend(seg)
            acc += len(seg)
            offsets[x + 1] = acc
        self._ys = tuple(ys)
        self._offsets = offsets

    def _solid_row(self, y, x0, span):
        """第 y 行、从 x0 起 `span` 格的「非空」位图，当一个大整数返回。

        ★ 两张查表都走 C 层：`_UNPACK` 把 2 bit/格摊成 1 字节/格，
          `_SOLID` 再把 0..3 压成 0/1。逐格 Python 循环慢两个数量级。
        """
        if y < 0 or y >= self.height:
            return 0
        i0 = y * self.width + x0
        i1 = i0 + span - 1
        first, last = i0 >> 2, (i1 >> 2) + 1
        flat = b"".join(map(_UNPACK.__getitem__, self._cells[first:last]))
        off = i0 - (first << 2)
        return int.from_bytes(flat[off:off + span].translate(_SOLID), "big")

    def breakable_by_handle(self, handle):
        """按**世界句柄**找破坏物；不是破坏物的句柄返回 `None`（§139）。"""
        if not handle:
            return None
        for item in self.breakables:
            if item.handle == handle:
                return item
        return None

    def variant(self, alive):
        """「只有 `alive` 这几件破坏物还在」的那一份地形。**memo 化**。

        同一个存活集合永远返回**同一个对象** —— `botnav` 的可达图缓存按
        对象做键，这样一个状态只算一张图，来回破坏 / 恢复也不重复算。
        """
        count = len(self.breakables)
        alive = frozenset(int(i) for i in alive if 0 <= int(i) < count)
        root = self._root
        if alive == root.alive:
            return root
        got = root._variants.get(alive)
        if got is None:
            got = object.__new__(MapTerrain)
            for field in ("name", "version", "width", "height", "points",
                          "jump_pads", "breakables", "_base_cells",
                          "_base_offsets", "_base_ys"):
                setattr(got, field, getattr(root, field))
            got._root = root
            got._variants = root._variants
            got._base_coarse = None       # 只认根那份（`_base_coarse_grid`）
            got._compose(alive)
            root._variants[alive] = got
        return got

    # -- 格子 ---------------------------------------------------------------

    def cell(self, x, y):
        """(x, y) 那一格的值 0..3。★ 出界返回 2（实心），和客户端一致。"""
        if x < 0 or y < 0 or x >= self.width or y >= self.height:
            return OUT_OF_BOUNDS
        i = y * self.width + x
        return (self._cells[i >> 2] >> ((i & 3) * 2)) & 3

    def is_solid(self, x, y):
        """挡得住**人**吗（走路 / 下落）。单向平台算挡。★ 图外算挡得住。"""
        return self.cell(x, y) != 0

    def blocks_bullet(self, x, y):
        """挡得住**子弹**吗。★ 单向平台（值 1）**不挡**，见文件头 §29。"""
        return self.cell(x, y) >= 2

    # -- 粗网格（弹道加速） -------------------------------------------------

    def _base_coarse_grid(self):
        """**不含破坏物**的那张粗网格，整张图算一次，所有 variant 共用。

        走 `_base_cells`（原网格）而不是 `_cells`：破坏物是一层一层 OR 上去的，
        每换一个存活集合就整张重算的话，最大的图要 96 ms —— 那本身就是一次
        卡顿。破坏物那几块由 `bullet_coarse()` 另外补，见那边。
        """
        root = self._root
        got = root._base_coarse
        if got is not None:
            return got
        width, height = self.width, self.height
        gw = (width + COARSE - 1) >> COARSE_SHIFT
        gh = (height + COARSE - 1) >> COARSE_SHIFT
        grid = bytearray(gw * gh)
        cells = root._base_cells
        for y in range(height):
            i0 = y * width
            i1 = i0 + width - 1
            first, last = i0 >> 2, (i1 >> 2) + 1
            flat = b"".join(map(_UNPACK.__getitem__, cells[first:last]))
            off = i0 - (first << 2)
            row = flat[off:off + width].translate(_BLOCKS_BULLET)
            pos = row.find(1)
            if pos < 0:
                continue                    # 整行都是空气（绝大多数行）
            base = (y >> COARSE_SHIFT) * gw
            while pos >= 0:
                bx = pos >> COARSE_SHIFT
                grid[base + bx] = 1
                # 直接跳到下一块的开头 —— 同一块里再有几个也没有新信息。
                pos = row.find(1, (bx + 1) << COARSE_SHIFT)
        got = (bytes(grid), gw, gh)
        root._base_coarse = got
        return got

    def bullet_coarse(self):
        """「每 `COARSE`×`COARSE` 一块，这块里**有没有**挡子弹的格子」。

        返回 ``(网格 bytes, 网格宽, 网格高)``，`网格[by * 宽 + bx]` 非 0 = 这一块
        里有东西。**只读、按地形对象 memo 一份**（地形本身就是只读的）。

        用法见 `bot._terrain_contact()`：某一步的采样点落在为 0 的块里 ⇒
        这一块内部整个是空的 ⇒ 该采样点连同**它在这块里还能走的那几步**
        全都不用逐格问，直接跳过去。跳多远由采样点离块边还有多少像素决定
        （每步每个轴最多走 1 像素），所以**结果和逐像素扫完全一致**。

        ★★ 网格只会**多**标不会**少**标：还活着的破坏物按**外接矩形**整块标脏
        （不是按它真正的形状）。标脏的后果只是「这一段退回逐像素扫」，
        而逐像素扫给的就是精确答案 —— 所以过度标记安全，漏标才不安全。
        换来的是换一个存活集合只要几十微秒，不用整张重算。

        ★ 出界不进这张网格：`y < 0` 不算实心（§83），左右和底下算实心，
          三种口径各不相同，交给调用方按老规矩判 —— 那几步很少见，不值得
          为它把网格搞复杂。
        """
        got = self._coarse
        if got is not None:
            return got
        base, gw, gh = self._base_coarse_grid()
        alive = self.alive
        items = [b for b in self.breakables
                 if b.index in alive and b.col1 >= 0]
        if not items:
            got = (base, gw, gh)
        else:
            grid = bytearray(base)
            for item in items:
                bx0 = max(0, item.col0) >> COARSE_SHIFT
                bx1 = min(self.width - 1, item.col1) >> COARSE_SHIFT
                by0 = max(0, item.row0) >> COARSE_SHIFT
                by1 = min(self.height - 1, item.row1) >> COARSE_SHIFT
                for by in range(by0, by1 + 1):
                    row = by * gw
                    grid[row + bx0:row + bx1 + 1] = b"\x01" * (bx1 - bx0 + 1)
            got = (bytes(grid), gw, gh)
        self._coarse = got
        return got

    def is_one_way(self, x, y):
        """是不是单向平台：站得上去，按 ↓ 能穿下去，往上跳能穿过去。"""
        return self.cell(x, y) == 1

    # -- 站立面 -------------------------------------------------------------

    def surfaces(self, x):
        """第 x 列自上而下的**站立面** y（实心区的上沿）。列越界返回空。"""
        if x < 0 or x >= self.width:
            return ()
        return self._ys[self._offsets[x]:self._offsets[x + 1]]

    def ground_below(self, x, y):
        """从 (x, y) 往下掉，落在哪个站立面上；一路掉出图外返回 None。"""
        for sy in self.surfaces(x):
            if sy >= y:
                return sy
        return None

    def ground_above(self, x, y):
        """(x, y) 头顶上方最近的站立面；没有返回 None。"""
        found = None
        for sy in self.surfaces(x):
            if sy < y:
                found = sy
            else:
                break
        return found

    def first_solid(self, x):
        """第 x 列最上面那块**挡得住掉落物**的格子的 y；整列没有返回 None。

        ★★ **和 `surfaces(x)[0]` 不是一回事**（V0.3 §114）：站立面是**人**
        的判据（`is_solid`，单向平台算数），而掉落物 / 弹体的
        `vft+0x100` 是 `xor al,al ; ret` —— 值 1 的**单向平台根本不挡它们**，
        东西会直接穿过去落到下面那层实心地面上。所以「一件道具会停在哪」
        要用 `blocks_bullet`（cell ≥ 2）来找，不能用站立面。

        从站立面起步只是为了少扫几格：实心格一定在某一段非空区里。
        """
        for sy in self.surfaces(x):
            y = sy
            while y < self.height and self.cell(x, y) != 0:
                if self.blocks_bullet(x, y):
                    return y
                y += 1
        return None

    # -- 弹道 ---------------------------------------------------------------

    def line_blocked(self, x0, y0, x1, y1, step=4):
        """(x0,y0) -> (x1,y1) 这条**弹道**中间有没有被地形挡住。

        用的是 `blocks_bullet`，**不是** `is_solid` —— 单向平台不挡子弹
        （§29）。用错了 bot 会觉得隔着一根白线打不到人。

        `step` 是采样步长（像素）。默认 4：角色一步 36 左右，4 像素的
        漏检对「这一发打不打得中」不构成影响，而全像素采样在纯 Python 里
        每发要走上千次循环。★ 端点本身不算 —— 枪口和目标常常贴着地面。
        """
        dx = x1 - x0
        dy = y1 - y0
        dist = max(abs(dx), abs(dy))
        if dist <= step:
            return False
        n = int(dist // step)
        for i in range(1, n):
            t = float(i) / n
            if self.blocks_bullet(int(x0 + dx * t), int(y0 + dy * t)):
                return True
        return False

    def __repr__(self):
        return "<MapTerrain %s v%d %dx%d>" % (
            self.name, self.version, self.width, self.height)


def _unblob(text):
    return zlib.decompress(base64.b64decode(text))


class _Store(object):
    """索引 + 已加载地图的缓存。全 174 张加起来才 2 MB 上下，加载过就留着。"""

    def __init__(self, data_dir=DATA_DIR):
        self.data_dir = data_dir
        self._index = None
        self._cache = {}

    def index(self):
        if self._index is None:
            self._index = self._read_index()
        return self._index

    def _read_index(self):
        path = os.path.join(self.data_dir, "index.json")
        try:
            with open(path, "r", encoding="utf-8") as fp:
                idx = json.load(fp)
        except (IOError, OSError, ValueError):
            # 没有地形数据不该让服务端起不来：bot 照样能跟着真人的轨迹走
            # （D16），只是不会自己找路。
            return {"maps": {}, "bases": {}, "props": {}}
        if idx.get("format") != FORMAT:
            return {"maps": {}, "bases": {}, "props": {}}
        return idx

    def available(self):
        return sorted(self.index().get("maps", {}))

    def resolve(self, map_name, difficulty=None):
        """把客户端给的地图串解析成产物里的确切名字；找不到返回 None。

        `difficulty` 是**闯关房描述符的第 2 个参数**（1..4）。给了就先按
        `DIFFICULTY_SUFFIX` 那一档找 —— 客户端拼文件名用的就是它。
        这一档没有（`Quest03_2` 那种只有一份的图）才退回老顺序。
        """
        if not map_name:
            return None
        name = map_name.split(":", 1)[0].strip()
        if not name:
            return None
        maps = self.index().get("maps", {})
        if name in maps:
            return name
        wanted = DIFFICULTY_SUFFIX.get(difficulty)
        if wanted and name + wanted in maps:
            return name + wanted
        for suffix in DIFFICULTY_ORDER:
            if name + suffix in maps:
                return name + suffix
        for cand in sorted(self.index().get("bases", {}).get(name, ())):
            if cand in maps:
                return cand
        return None

    def load(self, map_name, difficulty=None):
        """按客户端给的地图串取地形；没有这张图的数据返回 None。"""
        name = self.resolve(map_name, difficulty)
        if name is None:
            return None
        if name in self._cache:
            return self._cache[name]
        entry = self.index()["maps"][name]
        path = os.path.join(self.data_dir, entry["file"])
        try:
            with open(path, "r", encoding="utf-8") as fp:
                record = json.load(fp)
        except (IOError, OSError, ValueError):
            return None
        if record.get("format") != FORMAT:
            return None
        terrain = MapTerrain(record)
        self._cache[name] = terrain
        return terrain


STORE = _Store()


def load(map_name, difficulty=None):
    """取一张图的地形；没有数据返回 `None`（**调用方必须能接受 None**）。

    `difficulty` 见 `DIFFICULTY_SUFFIX` —— 闯关房**必须**传，不传会拿到
    别的难度那张图。
    """
    return STORE.load(map_name, difficulty)


def resolve(map_name, difficulty=None):
    return STORE.resolve(map_name, difficulty)


def falls_out_of_the_world(map_name):
    """这张图**掉出下边界会不会死**（`map.ini` 的 `FallDown`，§143）。

    客户端每帧判一次（`Character::CheckFallDown` = `0x50d520`）：这张图的
    记录有 `FallDown` 且**角色底部 y + 5 >= 地图高度**，就调
    `ProcessFallDown` —— 玩家角色那一份（`0x51503a`）**直接发 `0x0408`**
    报死，不走扣血（所以凶手 id 是 0，packet_api §0x0408 那句「掉岩浆 /
    自杀是 0x00」说的就是它）。

    ⚠ **键是完整的地图串**（含 `:NewPvp` 那种玩法后缀）：`map.ini` 里
      `Forest03` 和 `Forest03:NewPvp` 是两条记录，只有后者 `FallDown=1`。
      所以这里**不切** `:` —— 先按原样查，查不到再退回基名。
    """
    props = STORE.index().get("props", {})
    if not props or not map_name:
        return False
    name = map_name.strip()
    entry = props.get(name)
    if entry is None:
        entry = props.get(name.split(":", 1)[0].strip())
    return bool(entry and entry.get("fall_down"))


#: ★★ 判「掉出去了」时给角色底部加的余量 —— 客户端 `0x50d55d` 那个
#: 立即数（`[0x693878]` = 5.0）。照抄，别改成别的数。
FALL_DOWN_MARGIN = 5.0


def qualify(map_name, difficulty):
    """把地图串补成**带难度后缀**的确切名字；补不出来就原样返回。

    ★ 只做名字这一件事，所以调用方可以把结果继续当「地图名」用（日志、
      `ground_item_spawn()`、`mapdata.load()` 都吃），而不必到处多带一个
      难度参数。补不出来时返回去掉 `:模式` 之后的基名，行为和以前一样。
    """
    if not map_name:
        return map_name
    name = map_name.split(":", 1)[0].strip()
    if not name:
        return map_name
    return STORE.resolve(name, difficulty) or name


def available():
    return STORE.available()
