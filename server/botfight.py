# -*- coding: utf-8 -*-
"""格斗模式（무투전，对战模式号 2）里 bot 的那一半（X_Mod · X16，D84）。

`bot.py` 只留钩子，格斗特有的规则和状态都在这里：

* **挨格斗招式**（C2，X_Mod §122）：真人的格斗招式打中 bot，`rpSplashDamaged +12` 是 2 或 3（普通的伤害恒 0）。
  类型 2 = 锁输入 10 帧 + 按 x^(1/10) 曲线滑 2·push.x、不改速度；类型 3 = 打飞（伤害 > 10 才加 push、
  **不**做 push.y ×2）。两类都转向攻击者。每台客户端都对 bot 做同样的事 —— 服务端不跟着做，下一发心跳就把它拽回去。
* **看懂真人的招**（C2 / B2，X_Mod §126 / §127）：`HumanSkill`。
* **bot 自己出招 / 格挡**（C3，D84 / D88）：`BotSkill`（服务端当它的本机：动画时钟、顿帧、接招窗口、判定体、句柄）、
  `first_hit`（挑招用：这一招从这儿出够不够得着）、`GuardState`（格挡开关 + 3 帧过渡 + 打破 + 反应时间）、
  `Opening`（出手反应时间，D91）；收招之后几格才能按 / 走（`RETRACT_*`，X_Mod §129）。

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

#: 他那台发出 `0x0016` 之后第几帧走招式的第 0 帧（第一次 Update）：✅ **第 1 帧**（X_Mod §127，B2 探针和服务端
#: 收包时刻同一台机器的钟对：331 次里 302 次 +32 ms、27 次 +16 ms）。C2 当初照冲刺（`bot.HUMAN_DASH_START_FRAMES`
#: = 2）推成第 2 帧，晚了一帧 —— 回环到了当帧就建招式对象、当帧就 Update，没有冲刺那一帧 `StartDash`。
HUMAN_SKILL_START_FRAMES = 1

#: 打中一次，攻击者动画顿 4 个逻辑帧（`0x4fa017`，收到伤害包且查得到源句柄的机器都顿）：✅ 打中那一帧发
#: `rpSplashDamaged`，**之后第 1 帧起**停 4 帧（X_Mod §127：5/5 次，探针在那一帧后空 167 ms、f 只按正常步长动一次）。
HITSTOP_FRAMES = 4
HITSTOP_START_FRAMES = 1

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

    __slots__ = ("skill", "facing", "origin", "grid", "gen", "stops", "cut", "placed", "chain_start")

    def __init__(self, skill, facing, origin, grid, gen=None, chain_start=None):
        self.skill = skill
        self.facing = 1 if facing >= 0 else -1
        self.origin = (float(origin[0]), float(origin[1]))
        self.grid = float(grid)
        self.gen = gen
        self.stops = set()          # 顿住的帧（raw）
        self.cut = None             # 收招 / 被打断 / 换招那一帧（raw），之后不算
        self.placed = False         # 外推时第 0 帧硬置过没有
        #: 这一串连段第一招开始的时刻：接续招（PrevSkill 是上一招）沿用上一招的，起手招就是自己的 `grid`。
        #: bot 的格挡反应时间从这一刻算（D88）—— 他连着打，bot 反应过来一次就一直挡着，不会每招重新「看见」。
        self.chain_start = self.grid if chain_start is None else float(chain_start)

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
        """他这一招打中了人（那发 `rpSplashDamaged` 排在网格上的时刻）：从之后第 1 帧起顿 4 帧（X_Mod §127）。"""
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


# ---------------------------------------------------------------------------
# bot 自己出招（C3，X_Mod §121 / D84 / D88）：服务端当它的「本机」
# ---------------------------------------------------------------------------
#: 一个逻辑帧多少毫秒（计时器 / 接招窗口按它换算）。
FRAME_MS = FRAME_S * 1000.0

#: 接招窗口（`0x4f822e`：计时器剩余 `<= 0x12c`）—— 当前这招最后 300 ms 起可以排下一招，动作一播完立刻出（D88）。
COMBO_WINDOW_MS = 300

#: 推挤体（`0x4f88a1`）：半径 20，每帧摆在身体中心 + (15 x 朝向, 5)。
PUSH_RADIUS = 20.0
PUSH_DX = 15.0
PUSH_DY = 5.0

#: 打中的击退（`0x4f9bae`）：(朝向 x 15 x `DamageBounceVectorMultiplierX`, -伤害 x `...MultiplierY`)。
HIT_PUSH_X = 15.0


def hit_push(damage, facing, config=None):
    """打中那一下发给受害者的 push（`0x4f9bae`，X_Mod §122）：(朝向 x 15 x MultX, -伤害 x MultY)，都是 f32。"""
    config = config or {}
    mult_x = float(config.get("DamageBounceVectorMultiplierX", 1.0))
    mult_y = float(config.get("DamageBounceVectorMultiplierY", 1.5))
    side = 1.0 if facing >= 0 else -1.0
    return (_f32(side * HIT_PUSH_X * mult_x), _f32(-float(damage) * mult_y))


#: ★ 两次 J / K「按键」（起手出招、接招窗口里排下一招）之间至少隔多久（秒）。
#: ⚠ **不是原版规则，是用户 2026-09-27 定的玩法限制**（D88 改）：「真人连按键盘也不是无限快的，两次按键之间隔 100ms 以上」。
#:   原版接招窗口（剩余 ≤ 300 ms 起可排）照旧；泰尔刺拳计时器才 266 ms、一出手窗口就开着，不加这条 bot 下一格就能排下一招。
#:   同 D77 的 0.3 秒，是给 bot 自己上的约束，不是拿来掩盖时序竞态的阈值（铁律 10 管的是后者）。
BOT_PRESS_GAP_S = 0.100

#: ★ 收招之后（`0x0016` 类型 −1）真人那台的帧序（X_Mod §129）：发收招那一帧 N 招式对象还在（下一帧开头回环才删），
#: 这一帧按的 J/K 下一帧才发；N+1 帧的输入读方向、N+2 帧才走得动 / 转得过身。bot 照这个数：
#: 收招那一格记 0，之后每格 +1 ——
#:   * `< RETRACT_PRESS_FRAMES`（= 收招那一格）：不起手、不冲、不举挡；
#:   * `< RETRACT_WALK_FRAMES`（N、N+1）：不走、不转身、不跳。
#: 换方向出手 = N+2 转身 + 「转身那一格不出手」⇒ 最早 N+3，和真人一样，不用另记。
RETRACT_PRESS_FRAMES = 1
RETRACT_WALK_FRAMES = 2

#: ★ 出手反应时间（D91，**用户 2026-09-27 会话 49 定的玩法参数**，同 D88 的格挡反应，不是掩盖竞态的阈值）：
#: bot 能起手（J/K 起手招，或格斗房里的冲刺）并且看得见机会的那一格起，掷一次这两个数之间的均匀随机值，机会**一格不断地**
#: 在、到点了才按。连段里排下一招不管这条（那是提前按好的，原版窗口 + `BOT_PRESS_GAP_S` 管节奏）。
ATTACK_REACT_MIN_S = 0.100
ATTACK_REACT_MAX_S = 0.200


class Opening(object):
    """bot「看见机会 → 反应过来 → 才出手」（D91）。

    按**格号**认「一直在」：上一格也看见了就是同一个机会，接着等；中间断了一格（它忙着 / 机会没了）就是新机会、重掷。
    J/K 和冲刺共用这一份 —— 各掷一个等于取两次里先到的，反应被悄悄缩短（同 D89 ④ 格挡那条）。按出去之后 `reset`。
    """

    __slots__ = ("seen_tick", "ready_at")

    def __init__(self):
        self.seen_tick = None
        self.ready_at = None

    def see(self, tick, now, roll):
        """这一格看见了一个能出手的机会：新机会就掷反应时间（`roll()` ∈ [0, 1)）；反应过来了返回 True。"""
        if self.seen_tick is None or tick - self.seen_tick > 1:
            self.ready_at = None
        self.seen_tick = tick
        if self.ready_at is None:
            span = ATTACK_REACT_MAX_S - ATTACK_REACT_MIN_S
            self.ready_at = now + ATTACK_REACT_MIN_S + span * roll()
        return now >= self.ready_at - 1e-9

    def reset(self):
        self.seen_tick = None
        self.ready_at = None

    def __repr__(self):
        return "<Opening 看见于第%s格 反应到 %s>" % (self.seen_tick, self.ready_at)


class BotSkill(object):
    """bot 正在出的一招（X_Mod §121）。服务端就是它的本机：动画时钟、顿帧、接招窗口、判定体摆哪、打中谁都在这儿。

    一格 = 一个逻辑帧。发 `0x0016` 那一格 `k = -1`（还没走）；**下一格起第 0 帧**（§127：本机回环到了当帧建对象、
    当帧 Update —— 外推真人也是这个口径，`HUMAN_SKILL_START_FRAMES`）。

    * `k`：动画走到这招的第几个逻辑帧（`mutudata` 表里的 k）；顿帧那几格不走（`stop_left`）；
    * `elapsed`：计时器走过几格 —— 计时器是逻辑帧计时器（`0x5d5e37`），**顿帧照走**，接招窗口按它算；
    * 播完 = `k` 走到 `frames_total`（动画时刻 ≥ D，`0x4959cc`）：那一格不再 Update，本机按有没有排下一招发 `0x0016` / 收招；
    * 句柄：出招那一发同一把锁里推了 D+E+1（`BotSyncStream.mutu_skill`）—— 判定体 i = `base + i`、受击体 j = `base + D + j`、
      推挤体 = `base + D + E`。
    """

    __slots__ = ("skill", "facing", "base", "air", "k", "elapsed", "stop_left",
                 "stepped", "hit", "carried", "released", "carry_seen", "queued")

    def __init__(self, skill, facing, base, air=False):
        self.skill = skill
        self.facing = 1 if facing >= 0 else -1
        self.base = int(base)
        #: 空中招（`SkillType=Jump`）：着地状态一变就收、发收招（`0x495bf5` ~ `0x495c67`）。
        self.air = bool(air)
        self.k = -1
        self.elapsed = -1
        self.stop_left = 0
        #: 这一格动画往前走了没有（顿着的那几格 False：不挪、判定体停在原地）。
        self.stepped = False
        #: 打中过的人（`CanHit 0x4810fa` / `MarkHit 0x4810c8`：一招对一人只中一次）。
        self.hit = set()
        #: 推挤体推着的人（`{座位号: 第几帧贴上的}`，替它发过 `0x0017` 的）/ 被推着反手推人挣脱了的（同 `DashSwing`）。
        self.carried = {}
        self.released = set()
        #: 推上真人时他最新那发心跳的序号 + 出招者推上以来最宽松的位置（`bot._carry_confirmed`，X_Mod §130）。
        self.carry_seen = {}
        #: 接招窗口里排好的下一招（`mutudata.Skill`）；播完那一格出它，没排就发收招。
        self.queued = None

    @property
    def handle(self):
        """日志 / `_release_pusher` 用的代表句柄（头一个）。"""
        return self.base

    @property
    def direction(self):
        """推挤约束推到哪一侧（`0x50e654` 按出招者朝向）—— 和 `DashSwing.direction` 同名，`_carry_victims` 两种都认。"""
        return self.facing

    def tick(self):
        """走一格。返回这一格动画往前走了没有（顿着 / 已经播完返回 False）。"""
        self.elapsed += 1
        if self.k < 0:
            self.k = 0
        elif self.stop_left > 0:
            self.stop_left -= 1
            self.stepped = False
            return False
        else:
            self.k += 1
        self.stepped = not self.finished
        return self.stepped

    @property
    def started(self):
        return self.k >= 0

    @property
    def finished(self):
        return self.k >= self.skill.frames_total

    def move_step(self):
        """这一格招式自己挪几 px（Move 曲线 × 朝向，`0x4f82f2`）；顿着 / 播完 0。"""
        if not self.stepped:
            return 0
        return self.skill.move.get(self.k, 0) * self.facing

    def _live(self, defs):
        if not self.started or self.finished:
            return []
        out = []
        for i, d in enumerate(defs):
            p = d.at(self.k, self.facing)
            if p is not None:
                out.append((i, p[0], p[1], d.size))
        return out

    def damagers(self):
        """这一格活着的判定体：`[(下标, dx, dy, 半径)]`（dx 已按朝向镜像；顿着就是停住那一帧的位置）。"""
        return self._live(self.skill.damagers)

    def damagees(self):
        """这一格活着的受击体（伸出去的手脚也算身体，`0x4f9a4b`）：`[(下标, dx, dy, 半径)]`。"""
        return self._live(self.skill.damagees)

    @property
    def live_damagers(self):
        """活着的 Damager 数 `[skill+0x44]` > 0 ⇒ 这一招的近身优先级是 3（`0x4f88e6`）。"""
        return bool(self.damagers())

    def window_open(self):
        """接招窗口开了吗（`0x4f822e`：计时器剩余 ≤ 300 ms）。速率 0 那几招计时器溢出（§119），永远不开。"""
        timer = self.skill.timer_ms
        if timer is None or not self.started:
            return False
        return timer - self.elapsed * FRAME_MS <= COMBO_WINDOW_MS

    def add_hitstop(self):
        """这一格打中了人：之后第 1 格起顿 4 格（`0x4fa017`，X_Mod §127）。"""
        self.stop_left = HITSTOP_FRAMES

    def damager_handle(self, i):
        return self.base + int(i)

    def owns(self, handle):
        """这个句柄是不是这一招的（判定体 / 受击体 / 推挤体）。"""
        return self.base <= int(handle) < self.base + self.skill.handles

    def __repr__(self):
        return "<BotSkill %s 朝%+d 第%d帧 顿%d 中%s 排%s>" % (
            self.skill.name, self.facing, self.k, self.stop_left, sorted(self.hit),
            None if self.queued is None else self.queued.name)


#: 推挤约束（`0x50e654`，`botmotion.CONSTRAINT_DISTANCE`）：被推的人留在出招者朝向那一侧至少这么远。
CARRY_DISTANCE = 35.0


def first_hit(skill, facing, x, y, circles, from_k=0, push=None, applied=False):
    """估这一招从 `(x, y)` 起、第 `from_k` 帧往后，判定体**第几帧**碰得到对方；碰不到 `None`。

    `circles` = 对方身上的圈 `[(cx, cy, r)]`（假设他自己不动）。出招的人按 Move 曲线往前挪（不看地形 —— 只是估够不够得着，
    真出招走 `BotSkill` + 真地形）。`applied` = `(x, y)` 已经走过第 `from_k` 帧的那一步了（外推真人：他那一格先于 bot 走）。
    `push` = 出招者推挤体相对脚底的 `(dx, dy, r)`（dx 朝右为正，`push_offset`）：给了就连推挤一起算 —— 推挤体一碰到他，
    之后每帧把他约束到身前 `CARRY_DISTANCE` 外（`0x50e654`，同 `botmotion.constrained_x`）。泰尔 K01 那种先冲 90 px 再踢的，
    不算推挤会以为冲过了头、踢在他身后。
    """
    side = 1 if facing >= 0 else -1
    px = float(x)
    shift = 0.0
    carried = False
    start = max(0, from_k)
    for k in range(start, skill.frames_total):
        if not (applied and k == start):
            px += skill.move.get(k, 0) * side
        if push is not None and circles:
            if not carried:
                qx, qy = px + push[0] * side, float(y) + push[1]
                for cx, cy, r in circles:
                    reach = r + push[2]
                    if (qx - cx - shift) ** 2 + (qy - cy) ** 2 <= reach * reach:
                        carried = True
                        break
            if carried:
                here = circles[0][0] + shift
                boundary = int(px + side * CARRY_DISTANCE)
                if (side > 0 and int(here) < boundary) or (side < 0 and int(here) > boundary):
                    shift += boundary - int(here)
        for d in skill.damagers:
            p = d.at(k, side)
            if p is None:
                continue
            ox, oy = px + p[0], float(y) + p[1]
            for cx, cy, r in circles:
                reach = r + d.size
                if (ox - cx - shift) ** 2 + (oy - cy) ** 2 <= reach * reach:
                    return k
    return None


def push_offset(character, crouched=False):
    """推挤体相对脚底的 `(dx, dy, r)`（dx 朝右为正）：身体那个圈的圆心 + (15, 5)、半径 20（`0x4f88a1`）。
    `character` = `chrprops` 的角色（形状）；拿不到身体圈就按脚底算。"""
    for _cx, cy, _r, region in character.circles(0.0, 0.0, crouched):
        if region == "body":
            return (PUSH_DX, cy + PUSH_DY, PUSH_RADIUS)
    return (PUSH_DX, PUSH_DY, PUSH_RADIUS)


def remaining_move(skill, facing, from_k):
    """这一招从第 `from_k` 帧起（含）还要自己挪多少 px（× 朝向）。"""
    side = 1 if facing >= 0 else -1
    return sum(step for k, step in skill.move.items() if k >= from_k) * side


def candidates(skills, state, current=None):
    """此刻按得出来的招（`0x495ca1` 那一圈）：`PrevSkill` 是当前这招或为空；`SkillType` 对得上此刻的状态
    （站 = 踩地没蹲、蹲 = 踩地蹲着、空中 = 没踩地，`0x495cba`）。按原版的得分排：接续招（有 `PrevSkill`）在前（`0x495d15`：
    键串长 × 2 + 有 PrevSkill 再 +1 —— 键串都是 1 个字）。"""
    now = -1 if current is None else current.index
    out = [s for s in skills
           if s.skill_type == state and s.input in ("P", "K") and s.prev in (-1, now)]
    out.sort(key=lambda s: 0 if s.prev >= 0 else 1)
    return out


#: 格斗键（`0x429c28`）：J = P 轻击、K = K 重击。bot 挑的是**按哪个键**，出哪一招由客户端按得分定（`press`）。
KEYS = ("P", "K")


def press(skills, key, state, current=None):
    """按一下 `key`，客户端会出哪一招（`0x495ca1` ~ `0x495d2f`）：候选里键对得上的，得分最高的那个 —— 得分 = 键串长 × 2
    +（有 PrevSkill 再 +1），**严格大于**才换（`0x495d27 jbe`）⇒ 同分取遍历在前（招式号小）的。没有就 `None`。

    ★ 真人只能选按哪个键：窗口里再按 J，有接续招就一定是接续招（刺拳 → 二连），不会又出一记起手刺拳。
    """
    best, best_score = None, 0
    for s in skills:
        if s.input != key or s.skill_type != state:
            continue
        now = -1 if current is None else current.index
        if s.prev not in (-1, now):
            continue
        score = len(s.input) * 2 + (1 if s.prev >= 0 else 0)
        if score > best_score:
            best, best_score = s, score
    return best


# ---------------------------------------------------------------------------
# bot 的格挡（C3，X_Mod §122 / D88）
# ---------------------------------------------------------------------------
#: 反应时间（D88，**用户定的玩法参数**，同 D77 不是掩盖竞态的阈值）：bot「看见」他出招之后，每次在这两个数之间均匀随机一个，
#: 过了才能举挡。原版的挡是人按的；服务端一收到 `0x0016` 就知道他出了什么、几帧后判定到 —— 照这个挡是超人。
GUARD_REACT_MIN_S = 0.200
GUARD_REACT_MAX_S = 0.400
#: 开关过渡（`0x502dd7`，3 个逻辑帧，同 `gameserver.GUARD_SWITCH_S`）：别的机器上「在挡」= 开关 XOR 过渡在跑。
GUARD_SWITCH_FRAMES = 3


class GuardState(object):
    """bot 的格挡开关（`[+0x2b6]`）、过渡计时器、「打破」（`[+0x2b7]`）和反应时间。

    按格数（`tick()` 每格一次）：开关一翻，过渡计时器没在跑才起（`0x502dd7` 先问 `0x5d5eb0`，照 `Conn.note_guard`）；
    别的机器判它**在挡** = 开关 XOR 过渡在跑（`0x50a0ea`）。
    """

    __slots__ = ("on", "switch_left", "broken", "seen", "react_at")

    def __init__(self):
        self.on = False
        self.switch_left = 0
        #: 体力扣到 0.5 以下本机置「打破」、发关（`0x5070c5`），重新按 L 才清 —— bot 这边 = 那一下威胁过去了再说。
        self.broken = False
        #: 看见的是他哪一下（`HumanSkill` 对象 / 冲刺那一下的标识）、什么时候反应得过来（D88）。
        self.seen = None
        self.react_at = None

    def set(self, on):
        """开关翻到 `on`；翻了返回 True（调用方发 `0x0018`）。"""
        on = bool(on)
        if on == self.on:
            return False
        self.on = on
        if self.switch_left <= 0:
            self.switch_left = GUARD_SWITCH_FRAMES
        return True

    def drop(self):
        """收方收到它的 `0x0016` 会把 `[+0x2b6]` 直接清掉（`0x4935a7` 那一段，不起过渡、不发包）—— 出招就这样放下挡。"""
        self.on = False
        self.switch_left = 0

    def tick(self):
        if self.switch_left > 0:
            self.switch_left -= 1

    @property
    def effective(self):
        """别的机器此刻判它在挡吗（开关 XOR 过渡在跑）。"""
        return self.on != (self.switch_left > 0)

    def __repr__(self):
        return "<GuardState %s 过渡%d%s>" % ("开" if self.on else "关", self.switch_left,
                                           " 打破" if self.broken else "")
