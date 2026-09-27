# -*- coding: utf-8 -*-
"""格斗模式（무투전）招式表的运行时读取器（X_Mod · X16 · B1，D84）。

产物 `server/bot_mutu.json` 由 `tools/mutudata.py` 从 `Data/NewMutu.ini` + 动作文件离线提取
（服务端包里没有明文资源树、也不带 numpy）。这里只用标准库，CPython 3.8 也能跑。

为什么要它：格斗招式打没打中**只有出招者本机判**（X_Mod §121）—— bot 没有本机，服务端得替它判，
就得知道每招每个判定体「第 k 个逻辑帧在脚底前后多远」。坐标约定（✅ B2 实机核过，X_Mod §127）见产物的 `units`：
`track` 里的 `(dx, dy)` 是**朝右时**相对脚底的 px（已乘模型缩放 0.85 / 卡希尔 0.75），朝左 dx 取反。

★ `skills(角色)` 的下标就是 `0x0016` 里的招式号 —— 客户端 ini 哈希表的遍历顺序，**不是**文件顺序（X_Mod §127）。
"""
import json
import os

#: 认得的产物格式版本（和 `tools/mutudata.py` 的 `FORMAT` 一起改）。
#: 2：招式按客户端招式号排、轨迹乘了模型缩放（X_Mod §127）—— 1 那份的下标和坐标都是错的，不认。
FORMAT = 2

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_mutu.json")

#: `SkillType`：1 站着（踩地没蹲）/ 2 蹲着 / 3 空中；0 = 不可用（§121，`0x495cba`）。
STAND, CROUCH, AIR = 1, 2, 3


class HitObject(object):
    """一个判定体（Damager 或 Damagee）。`points[k] = (f, dx, dy)` —— 第 k 个逻辑帧在不在、在哪。"""

    __slots__ = ("bone", "size", "start", "end", "points")

    def __init__(self, raw):
        self.bone = raw.get("bone", "")
        self.size = float(raw.get("size", 0.0))
        self.start = int(raw.get("start", 0))
        self.end = int(raw.get("end", 0))
        self.points = {int(k): (int(f), float(dx), float(dy)) for k, f, dx, dy in raw.get("track", ())}

    def at(self, k, facing):
        """第 k 个逻辑帧（招式开始那一帧是 0，顿帧不算）的偏移；这一帧不在就 `None`。"""
        point = self.points.get(k)
        if point is None:
            return None
        _f, dx, dy = point
        return (dx if facing >= 0 else -dx, dy)


class Skill(object):
    """一招。字段见 `tools/mutudata.py` 的模块说明。"""

    __slots__ = ("name", "index", "motion", "rate", "duration", "samples", "timer_ms",
                 "frames_total", "damage", "kind", "skill_type", "prev", "input", "handles",
                 "move", "damagers", "damagees")

    def __init__(self, raw):
        self.name = raw.get("name", "")
        self.index = int(raw.get("index", -1))
        self.motion = raw.get("motion", "")
        self.rate = float(raw.get("rate", 1.0))
        self.duration = float(raw.get("duration", 0.0))
        self.samples = int(raw.get("samples", 0))
        timer = raw.get("timer_ms")
        self.timer_ms = None if timer is None else int(timer)
        self.frames_total = int(raw.get("frames_total", 0))
        self.damage = float(raw.get("damage", 0.0))
        self.kind = int(raw.get("kind", 2))
        self.skill_type = int(raw.get("skill_type", 0))
        self.prev = int(raw.get("prev", -1))
        self.input = raw.get("input", "")
        self.handles = int(raw.get("handles", 1))
        #: 第 k 个逻辑帧自己往前挪几 px（朝右；只收走动的帧）。
        self.move = {int(k): int(step) for k, step in raw.get("move_track", ())}
        self.damagers = [HitObject(d) for d in raw.get("damagers", ())]
        self.damagees = [HitObject(d) for d in raw.get("damagees", ())]


class _Store(object):
    """整张表一百多 KB，一次读进来留着。"""

    def __init__(self, path=DATA_PATH):
        self.path = path
        self._table = None
        self._cache = {}

    def table(self):
        if self._table is None:
            self._table = self._read()
        return self._table

    def _read(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                table = json.load(fp)
        except (IOError, OSError, ValueError):
            # 没有招式表不该让服务端起不来 —— 只是 bot 不会出格斗招式。
            return {"characters": {}}
        if table.get("format") != FORMAT:
            return {"characters": {}}
        return table

    def skills(self, character_id):
        """这个角色的 10 招（下标 = 客户端招式号，X_Mod §127）；表里没有就是空列表。"""
        key = str(int(character_id))
        if key not in self._cache:
            raw = self.table().get("characters", {}).get(key, ())
            self._cache[key] = [Skill(s) for s in raw]
        return self._cache[key]

    def config(self):
        """`NewMutuConfig.ini` 带过来的那几格（重力 / 跳高 / 击退系数，§120 / §122）。"""
        return dict(self.table().get("config") or {})

    def guard(self):
        """`GameProps.ini` 的格挡参数：`sp_cost`（每帧扣的体力）/ `damage_rate`（§122）。"""
        return dict(self.table().get("guard") or {})


STORE = _Store()


def skills(character_id):
    return STORE.skills(character_id)


def config():
    return STORE.config()


def guard():
    return STORE.guard()
