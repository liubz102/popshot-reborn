# -*- coding: utf-8 -*-
"""格斗模式（무투전，对战模式号 2）里 bot 的那一半（X_Mod · X16，D84）。

`bot.py` 只留钩子，格斗特有的规则和状态都在这里：

* **挨格斗招式**（C2，X_Mod §122）：真人的格斗招式打中 bot，`rpSplashDamaged +12` 是 2 或 3（普通的伤害恒 0）。
  类型 2 = 锁输入 10 帧 + 按 x^(1/10) 曲线滑 2·push.x、不改速度；类型 3 = 打飞（伤害 > 10 才加 push、
  **不**做 push.y ×2）。两类都转向攻击者。每台客户端都对 bot 做同样的事 —— 服务端不跟着做，下一发心跳就把它拽回去。
* bot 出招 / 格挡 / AI 是 C3 的事（D84）。

帧口径：一格 = 收方一个逻辑帧（32 ms，D106）。客户端这几样都是**逻辑帧计时器**（`0x5d5e37` 起、按帧号比），
所以这里按「走过了几格」数，不按挂钟。
"""
import math
import struct

import roomclock

#: `rpSplashDamaged +12` 的类型（X_Mod §122）：0 = 直接命中 / 溅射 / 冲刺 / 火墙（`0x4928ea push 0`），
#: 2 / 3 = 格斗招式（`0x4f9c82`：`BounceHit ? 3 : 2`）。
HIT_PLAIN = 0
HIT_SLIDE = 2
HIT_FLY = 3
MUTU_HIT_KINDS = (HIT_SLIDE, HIT_FLY)

#: 挨类型 2 锁输入几帧：`[+0x53c]` 起 10（`0x4ff941 push 0xa`），`0x5150d4` 在跑就整段不读键。
INPUT_LOCK_FRAMES = 10
#: 滑退走几帧 = `[+0x17c]` 的长度：击退 `0x50f7ca` 的尾巴 `0x50f961` 对**所有**类型都起 10。
SLIDE_FRAMES = 10
#: 滑退曲线的 γ（`[0x693730]` = 10.0，经 `0x5ce3a0` 取 1/γ 当指数）。
SLIDE_GAMMA = 10.0
#: 滑退总长 = push.x × 2（`0x4ff951 fld [push.x]` → `0x4ff959 fadd st0,st0` → `[+0x54c]`）。
SLIDE_PUSH_SCALE = 2.0


def _f32(value):
    return struct.unpack("<f", struct.pack("<f", float(value)))[0]


def slide_reach(total, n):
    """滑退曲线 `F(n) = trunc(D · powf(n / 10, 1/10))`（`0x4fe24e`，n = 0..10）。

    x 和 1/γ 先存成 f32、`pow` 的结果按 float 返回（同 `botmove.dash_distance`：`0x5ce3a0` → `0x49da65`），
    再和 f32 的 D 在 x87 里乘、`_ftol2` 截断。
    """
    if n <= 0:
        return 0
    x = _f32(n / _f32(SLIDE_FRAMES))
    return int(_f32(math.pow(x, _f32(1.0 / SLIDE_GAMMA))) * _f32(total))


def slide_steps(total):
    """一路不被打扰时第 1..10 帧各滑多少 px：D = 30 是 23 / 2 / 1 / 1 / 0 / 1 / 0 / 1 / 0 / 1。"""
    return [slide_reach(total, n + 1) - slide_reach(total, n) for n in range(SLIDE_FRAMES)]


def facing_after_hit(push_x):
    """挨格斗招式之后转向攻击者（`0x4ff914`）：被往右推（push.x > 0）⇒ 朝左 −1，否则朝右 +1。"""
    return -1 if push_x > 0.0 else 1


class HitReact(object):
    """bot 挨了一下类型 2 之后的那一段：`[+0x53c]` 锁输入 10 帧 + 按 `[+0x17c]` 的帧号滑退（`0x4fe24e`）。

    客户端的三格状态原样搬过来：
    * `lock_left` = `[+0x53c]` 还剩几帧；在跑才滑、才锁输入（`0x5150d4`）；
    * `elapsed` = `[+0x17c]` 已过几帧 —— **任何**击退都会把它重起（`0x50f961`，`restart_timer`）；
    * `last` = `[+0x550]`：上一次真滑到的 n。这一帧滑 `F(elapsed + 1) − F(last)` ⇒ 哪一帧因为按着方向键
      没滑，下一帧会一口气补上；滑到一半又挨了别的击退（`elapsed` 归零）则会往回滑一点 —— 都是原版的样子。

    🤔 挨打那一帧和角色更新谁先谁后没核：按「挨打之后的第一格 n′ = 1」算（D = 30 头一格 23 px；若是 2 则 25）。
    """

    __slots__ = ("total", "last", "elapsed", "lock_left")

    def __init__(self, push_x):
        self.total = _f32(float(push_x) * SLIDE_PUSH_SCALE)
        self.last = 0
        self.elapsed = 0
        self.lock_left = INPUT_LOCK_FRAMES

    @property
    def locked(self):
        return self.lock_left > 0

    def restart_timer(self):
        """又挨了一下别的击退：`[+0x17c]` 重起（`0x50f961`），`[+0x550]` 和锁输入不动。"""
        self.elapsed = 0

    def next_frame(self, direction=0):
        """走一格，返回这一格滑多少 px。锁输入那几帧里、走路方向 `[+0x4b4]` 是 0 才滑（`0x4fe27a`）。"""
        if self.lock_left <= 0:
            return 0
        self.lock_left -= 1
        n = self.elapsed + 1
        self.elapsed = n
        if direction or not self.total:
            return 0
        step = slide_reach(self.total, n) - slide_reach(self.total, self.last)
        self.last = n
        return step

    def __repr__(self):
        return "<HitReact 滑 %g 到第%d帧 锁%d>" % (self.total, self.last, self.lock_left)


# ---------------------------------------------------------------------------
# 服务端看懂真人的格斗招式（他发的 `0x0016`，X_Mod §121）
# ---------------------------------------------------------------------------
#: 一个逻辑帧多长（秒）—— 他那台的网格，和房间循环同一个 32 ms（D106）。
FRAME_S = roomclock.TICK_S

#: 他那台发出 `0x0016` 之后第几帧走招式的第 0 帧。🤔 照冲刺的结构推（`bot.HUMAN_DASH_START_FRAMES`，X_Mod §114）：
#: 本机那一发走回环，下一帧末才建招式对象（`[+0x5e0]` 等回环），再下一帧 Update 才走第 0 帧。B2 实机核。
HUMAN_SKILL_START_FRAMES = 2

#: 打中一次，攻击者动画顿 4 个逻辑帧（`0x4fa017`，收到伤害包且查得到源句柄的机器都顿）；他那台自己也是
#: 收到回环那一发才顿 ⇒ 从那发 `rpSplashDamaged` 在他那台发出之后第 2 帧起（同上，🤔 B2 核）。
HITSTOP_FRAMES = 4
HITSTOP_START_FRAMES = 2

#: 格斗招式的近身优先级（`0x4f88e6`：活着的 Damager 数 `[skill+0x44] > 0` 时是 3，否则 0，X_Mod §121）。
MUTU_PRIORITY = 3


class HumanSkill(object):
    """服务端看到的真人一招格斗招式（`0x0016` 类型 2）。

    帧号一律按**他那台的逻辑帧网格**数：`grid` = 那发 `0x0016` 排在网格上的时刻（到达时刻对齐，
    `Conn.sync_event_grid`），`raw` = 从它起第几帧。招式第 k 个逻辑帧（`mutudata` 表里的 k）= 从第
    `HUMAN_SKILL_START_FRAMES` 帧起**没顿住**的帧数；顿住的那几帧 k 不走、不挪、判定体原地不动。

    * `move_at(raw)`：这一帧招式自己挪几 px（Move 曲线，×朝向 `[+0x2d0]`，经 `0x50d9a7`）；
    * `damager_live(raw)`：这一帧有没有活着的 Damager（= 优先级 3）；
    * `end_raw`：招式播完那一帧（之后他那台发收招、恢复走路）。收招包 / 被打断 / 换招另外 `cut`。
    """

    __slots__ = ("skill", "facing", "origin", "grid", "gen", "stops", "cut", "placed")

    def __init__(self, skill, facing, origin, grid, gen=None):
        self.skill = skill
        self.facing = 1 if facing >= 0 else -1
        self.origin = (float(origin[0]), float(origin[1]))
        self.grid = float(grid)
        self.gen = gen
        self.stops = set()          # 顿住的帧（raw）
        self.cut = None             # 收招 / 被打断 / 换招那一帧（raw），之后不算
        self.placed = False         # 外推时第 0 帧硬置过没有

    def raw_at(self, t):
        """时刻 t 落在他网格上的第几帧（相对 `grid`，四舍五入到最近的一帧）。"""
        return int(math.floor((float(t) - self.grid) / FRAME_S + 0.5))

    def frame(self, raw):
        """第 raw 帧是招式的第几个逻辑帧 k；还没开始 / 已经播完 / 被收掉返回 `None`。

        顿住的那一帧 k 和上一帧一样（动画时钟停着）⇒ 数到 raw 为止（含）一共走了几帧、减 1。
        """
        start = HUMAN_SKILL_START_FRAMES
        if raw < start or (self.cut is not None and raw >= self.cut):
            return None
        k = raw - start - sum(1 for s in self.stops if start <= s <= raw)
        if k < 0 or k >= self.skill.frames_total:
            return None
        return k

    def holds(self, raw):
        """第 raw 帧他那台**不按键走路**吗：发出之后那一帧起键缓冲就非空（`[[0x72e2e0]+4]+4 ≠ −1`，
        `0x506fed`，X_Mod §120），回环到了建招式对象接着关，一直到招式播完 / 被收掉。
        （输入排在对象 tick 之后，发出那一帧本身已经走过了。）"""
        return 1 <= raw < self.end_raw

    def advancing(self, raw):
        """第 raw 帧招式往前走没走（顿住的帧不走）。"""
        return raw not in self.stops

    def move_at(self, raw):
        k = self.frame(raw)
        if k is None or not self.advancing(raw):
            return 0
        return self.skill.move.get(k, 0) * self.facing

    def damager_live(self, raw):
        k = self.frame(raw)
        return k is not None and any(k in d.points for d in self.skill.damagers)

    def add_hitstop(self, hit_grid):
        """他这一招打中了人（那发 `rpSplashDamaged` 排在网格上的时刻）：从之后第 2 帧起顿 4 帧。"""
        first = self.raw_at(hit_grid) + HITSTOP_START_FRAMES
        self.stops.update(range(first, first + HITSTOP_FRAMES))

    @property
    def end_raw(self):
        """招式播完的那一帧（开始之后第一帧 `frame()` 为 `None` 的 raw）：开始帧 + 总帧数 + 其间顿住的帧。"""
        raw = HUMAN_SKILL_START_FRAMES
        walked = 0
        while True:
            if raw not in self.stops:
                if walked == self.skill.frames_total:
                    break
                walked += 1
            raw += 1
        if self.cut is not None:
            raw = min(raw, self.cut)
        return raw

    def end_time(self):
        return self.grid + self.end_raw * FRAME_S

    def __repr__(self):
        return "<HumanSkill %s 朝%+d 起 %.3f 顿%d 收%s>" % (
            self.skill.name, self.facing, self.grid, len(self.stops), self.cut)
