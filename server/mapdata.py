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
    3  原版数据里没有，只在运行时出现

    ★ **出界返回 2**，和客户端一致。别改成「出界算空」——
      那会让 bot 觉得图外能走。

## ★ 「挡人」和「挡子弹」是**两个**判据，别混用

    is_solid(x, y)       挡住行走 / 接住下落  -> cell != 0（含单向平台）
    blocks_bullet(x, y)  挡住子弹             -> cell >= 2（★ 单向平台不挡）

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
import os
import struct
import zlib

#: 认得的产物格式版本。对不上就当没有数据 —— 宁可 bot 不会走，
#: 也不要按错的布局解出一张乱七八糟的地图。
FORMAT = 2

#: 找不到精确名时按这个顺序退。
DIFFICULTY_ORDER = ("#Normal", "#Easy", "#Hard", "#Extreme")

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "bot_mapdata")

#: 出界的格值。照抄客户端 `0x472fe0`。
OUT_OF_BOUNDS = 2


class MapTerrain(object):
    """一张图的地形。**只读**，加载后不再变。"""

    __slots__ = ("name", "version", "width", "height", "_cells",
                 "_offsets", "_ys", "points", "jump_pads")

    def __init__(self, record):
        self.name = record["name"]
        self.version = record["version"]
        self.width = record["width"]
        self.height = record["height"]
        self._cells = _unblob(record["cells"])
        counts = struct.unpack(
            "<%dH" % self.width, _unblob(record["ground_counts"]))
        ys_raw = _unblob(record["ground_ys"])
        self._ys = struct.unpack("<%dH" % (len(ys_raw) // 2), ys_raw)
        # 前缀和：第 x 列的站立面是 _ys[_offsets[x]:_offsets[x+1]]。
        offsets = [0] * (self.width + 1)
        acc = 0
        for i, n in enumerate(counts):
            acc += n
            offsets[i + 1] = acc
        self._offsets = offsets
        self.points = dict((int(k), [tuple(p) for p in v])
                           for k, v in record.get("points", {}).items())
        #: ★ **弹跳台**（V0.3 §99）：`[(台x, 台y, 落点dx, 落点dy), …]`。
        #:   `dx/dy` 是**落点相对台子的偏移**，不是速度 —— 客户端
        #:   `JumpingObj::Tick`（`0x510d05`）拿它现解一条抛物线。
        self.jump_pads = tuple(
            (float(a), float(b), float(c), float(d))
            for a, b, c, d in record.get("jump", ()))

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
            return {"maps": {}, "bases": {}}
        if idx.get("format") != FORMAT:
            return {"maps": {}, "bases": {}}
        return idx

    def available(self):
        return sorted(self.index().get("maps", {}))

    def resolve(self, map_name):
        """把客户端给的地图串解析成产物里的确切名字；找不到返回 None。"""
        if not map_name:
            return None
        name = map_name.split(":", 1)[0].strip()
        if not name:
            return None
        maps = self.index().get("maps", {})
        if name in maps:
            return name
        for suffix in DIFFICULTY_ORDER:
            if name + suffix in maps:
                return name + suffix
        for cand in sorted(self.index().get("bases", {}).get(name, ())):
            if cand in maps:
                return cand
        return None

    def load(self, map_name):
        """按客户端给的地图串取地形；没有这张图的数据返回 None。"""
        name = self.resolve(map_name)
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


def load(map_name):
    """取一张图的地形；没有数据返回 `None`（**调用方必须能接受 None**）。"""
    return STORE.load(map_name)


def resolve(map_name):
    return STORE.resolve(map_name)


def available():
    return STORE.available()
