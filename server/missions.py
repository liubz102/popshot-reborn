#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""missions.py —— 晋级任务（X_Mod · X23）：管理页配任务 → 服务端推表 → 结算推进 → 礼物箱发奖。

原版大厅右下角那个「任务」钮（`DlgPromotion`）一直点得开、却从来完成不了：
原版服务端那半边（`0x02e0` / `0x02e1` / `0x02e2`）我们一个都没接（FINDINGS §154~）。
这一版把它接上，而且**任务表由服务端说了算**（D111）：

    管理页「晋级任务」──保存──▶ server/data/missions.json（运营配置，第九份）
    登录 / 回大厅 / 凌晨 4 点 / 管理页保存 ──▶ 0x0F03（分片）这个人的任务表
                                            ──▶ bshook 喂给游戏自己的加载器重建
                                      └──────▶ 0x02e0（六条链的 A / B / C）
    结算 ──▶ settle()：所有「能做」的任务 × 本局数据 ⇒ 各自进度 / 完成 ⇒ 礼物箱

## 三段式（同 `cards.py`）

    gameserver      一局里发生了什么 / 什么时候推包    ← 只管时机
    missions.py     这一局给哪几条任务 +几、表长什么样  ← **纯函数**，不碰网络、不读写盘
    account_store   进度 / 完成 / 礼物一次落盘          ← 一把锁一次写盘

本模块规则表由调用方从 `shopcfg.missions()` 读好传进来，时钟也由调用方给
（`now`）—— 跨 4 点 / 跨周一 / 跨月的那几条单测靠的就是它。

## ★★ 客户端那一侧的硬事实（全部 🔍 逐指令核过，FINDINGS §154~§158）

* 加载器 `0x55ab61`：角色 0..2（`cmp ebx,2; jle`）× 两条链，小节名 `"%s%d"` ——
  前缀 `타이 / 카실 / 프로코` + `_PVP_레벨` / `_퀘스트_레벨`，号 0..59
  （三个上限先读 `[General]`，紧接着被 `push 0x3c` 盖成 60）。
* 每条记录（`0x55a348`）：缺 `PromotionID` = 跳过这一节；缺 `PromotionLevel` /
  `CharacterLimit` / `PromotionGameContextType` / `PromotionType`，或
  `PrecedingPromotionNumber > 2` = **整张表拒收**。
* 通用 ini 解析（`0x5d48c3`）：`FF FE` 开头才走 UTF-16；每行先去头尾
  `" \\r\\n"`、再在**第一个 `#`** 处截断（不只是行首！）；`[` 开头是小节
  （去 `" []\\t"`）；`键=值` 按第一个 `=` 切、两边去 `" \\r\\n\\t"`。
* 线（context type）：`1` = 对战 = 单例里第 0 块，`2` = 闯关 = 第 1 块（`0x55b279`）。
* `0x02e0` S→C：72 B = 角色 0..2 × (对战, 闯关) × {A 最后完成, B 当前, C 进度}（`0x55911c`）。
* `0x02e1` S→C：24 B = i32 角色 · i32 线(1/2) · i32 完成 id · i32 当前 id · i32 进度 ·
  i32 是否完成（`0x559141`，最后一格 `0x5d5956` 读 4 字节取 `!= 0`）。完成那一发
  **按客户端表**里那条记录的 `Reward0~2` / `TextReward1~3` 弹窗 ⇒ 弹窗必须在
  新表（去掉已完成的那条）推下去**之前**发。
* B 是 0 / -2 时客户端自己从 A 往后找下一条（`0x55b318`，要等级够）。
* 记录按 id 查**第一条**（`0x55b135`）⇒ 三个页签不能共用 id ⇒ **客户端 id =
  任务 id × 10 + 页签 + 1**（`client_id()`）。

## 铁律：只用标准库，CPython 3.8（Win7 运行时）也要能跑。
"""
from __future__ import annotations

import copy
import datetime
import struct
import time
import zlib

import cards
import shopcfg
import shopdata

#: `missions.json` 的格式号。
FORMAT = 1

# ---------------------------------------------------------------------------
# 周期（用户 2026-10-30：每日 / 每周 / 每月 / 仅一次，凌晨 4 点重置）
# ---------------------------------------------------------------------------
PERIOD_ONCE = "once"
PERIOD_DAILY = "daily"
PERIOD_WEEKLY = "weekly"
PERIOD_MONTHLY = "monthly"
PERIODS = (PERIOD_ONCE, PERIOD_DAILY, PERIOD_WEEKLY, PERIOD_MONTHLY)
PERIOD_ZH = {PERIOD_ONCE: "仅一次", PERIOD_DAILY: "每日",
             PERIOD_WEEKLY: "每周", PERIOD_MONTHLY: "每月"}
#: 没起名字的任务，名字打头那一格（「[每日] 对战胜利 3 次」）。
PERIOD_NAME_ZH = {PERIOD_ONCE: "任务", PERIOD_DAILY: "每日",
                  PERIOD_WEEKLY: "每周", PERIOD_MONTHLY: "每月"}
#: `TextStatus` 前面那几个字（「今日 %d/3胜」）。仅一次的不写。
PERIOD_STATUS_ZH = {PERIOD_ONCE: "", PERIOD_DAILY: "今日 ",
                    PERIOD_WEEKLY: "本周 ", PERIOD_MONTHLY: "本月 "}
#: 重置时刻：每天 4:00、每周一 4:00、每月 1 日 4:00（服务器本地时间，用户定死）。
RESET_HOUR = 4

# ---------------------------------------------------------------------------
# 线 / 种类
# ---------------------------------------------------------------------------
LINE_PVP = "pvp"
LINE_QUEST = "quest"
LINES = (LINE_PVP, LINE_QUEST)
LINE_ZH = {LINE_PVP: "对战", LINE_QUEST: "闯关"}
#: 客户端表里的 `PromotionGameContextType`，也是 `0x02e1` 里那一格「线」。
LINE_CONTEXT = {LINE_PVP: 1, LINE_QUEST: 2}

KIND_WIN = "win"        # 对战胜利 N 局
KIND_CLEAR = "clear"    # 闯关通关 N 次
KIND_STATS = "stats"    # 按对局数据
KIND_MAP = "map"        # 专用关卡（只有原版那 21 条，不能新建）
KINDS = (KIND_WIN, KIND_CLEAR, KIND_STATS, KIND_MAP)
KIND_ZH = {KIND_WIN: "对战胜利", KIND_CLEAR: "闯关通关",
           KIND_STATS: "按对局数据", KIND_MAP: "专用关卡"}
#: 客户端表里的 `PromotionType`：1 专用关卡（本机单人打 `MapFileName`）/
#: 2 对战胜利 / 4 闯关通关。开始钮按它分路（2 / 4 发快速加入）。
TYPE_MAP = 1
TYPE_PVP = 2
TYPE_QUEST = 4

#: 「按对局数据」的两种计数方式。
COUNT_MATCH = "match"   # 一局达成条件（称号卡片那套一局内条件）就算 1 次
COUNT_SUM = "sum"       # 累计某项数值（击杀 / 得分 / 伤害…）
COUNTS = (COUNT_MATCH, COUNT_SUM)
COUNT_ZH = {COUNT_MATCH: "每局达成算 1 次", COUNT_SUM: "累计数值"}

#: 角色：0 泰尔 / 1 卡希尔 / 2 布洛克，`-1` = 不限（三个页签都出现、共用进度、爱琳也算）。
CHAR_ANY = -1
#: 客户端只有三个页签（`0x55ab61` 的 `cmp ebx, 2; jle`）。爱琳（3）没有页签，
#: 打开面板默认泰尔页（`0x41f33a` 清 0），只推进「不限角色」的任务。
TABS = (0, 1, 2)
#: 小节名的角色前缀 = `0x55ad4e` 那张角色名表（韩文原名，镜像里的串）。
TAB_PREFIX = {0: "타이", 1: "카실", 2: "프로코"}
#: 两条链的小节名格式（`0x693088` / `0x693074`）。
CHAIN_FORMAT = {LINE_PVP: "%s_PVP_레벨", LINE_QUEST: "%s_퀘스트_레벨"}
#: 每条链客户端读几节：`0x55ab61` 先读 `[General]` 的三个 MAXLevel，
#: 紧接着 `push 0x3c` 全盖成 60 ⇒ 小节号 0..59。
CHAIN_SLOTS = 60
#: 我们从 1 排起（0 号留着：`0x55b318` 找「下一条」时拿 `PromotionLevel` 比大小，
#: 查不到起点时那份默认记录的等级不该和真记录撞）。
MAX_RANK = CHAIN_SLOTS - 1

#: 对战的两格筛选（客户端表里 `SubType` / `SubType2`）。`None` = 不限。
PVP_MODES = (0, 3, 2)              # 生存 / 夺分 / 格斗（shopcfg.PVP_MODE_ZH）
ITEM_MODES = (1, 0)                # 道具战 / 无道具
ITEM_MODE_ZH = {1: "道具战", 0: "无道具"}

#: 奖励最多 3 项：说明框（`TextReward1~3`）和完成弹窗（`0x550640` 那个循环 edi 2..4）
#: 都只有 3 行。
MAX_REWARDS = 3
#: 管理页新建的任务从这里往上编号（原版导入的是 `PromotionID` 的整数值，101 ~ 2131）。
FIRST_NEW_ID = 10000
#: 客户端 id = 任务 id × 10 + 3 也得是正的 i32。
MAX_ID = (2 ** 31 - 1 - len(TABS)) // 10
MAX_TARGET = 1000000
MAX_LEVEL = 999
MAX_ORDER = 1000000
#: 文本长度上限（字）。说明框就那么大，超出的部分客户端照样画、只是挤出框外。
TEXT_LIMITS = {"name": 40, "info": 80, "clear_text": 80}

#: 礼物箱里那几份奖励的留言。★ 发送人**照旧是 `GM`**（`account_store.GIFT_SENDER_GM`）：
#: 客户端领完礼物拿发送人和 `"GM"` 比，不相等就拉玩家去给发送人写致谢纸条
#: （V0.3商店 §75 / D76）—— 计划里写的发送人「晋级任务」会每领一次弹一次（D117）。
GIFT_MESSAGE = "完成晋级任务「%s」的奖励"

# ---------------------------------------------------------------------------
# 线格式
# ---------------------------------------------------------------------------
OP_STATUS = 0x02E0       # S→C 六条链的 A/B/C；C→S {i32 客户端 id} = 点了「开始」
OP_COMPLETE = 0x02E1     # S→C 进度 / 完成弹窗；C→S {i32 客户端 id} = 专用关卡打通了
OP_REJECT = 0x02E2       # S→C 拒绝（文案是「时间太短」，我们不用它）
OP_HOOK_TABLE = 0x0F03   # S→bshook 任务表（我们自造的 opcode）

#: `0x0F03` 片头：`u16 format · u32 serial · u32 total · u32 crc32 · u16 index ·
#: u16 count · u32 offset`，后面跟这一片的数据。hook 侧 `promo_wire.h` 同步。
TABLE_FORMAT = 1
TABLE_HEADER = struct.Struct("<HIIIHHI")
#: 每片最多带多少字节的表（帧载荷长是 u16，32 KiB + 22 B 片头稳稳放得下）。
TABLE_CHUNK = 32 * 1024


# ===========================================================================
# 周期键
# ===========================================================================

def period_key(period, now=None):
    """这条任务**当前这一期**的键。按「服务器本地时间 − 4 小时」取：

        仅一次 = ""          每日 = "2026-10-30"
        每周   = "2026-W44"（ISO 周，周一起）   每月 = "2026-10"

    ★ 不靠定时器改存档：存的键 == 现在的键 ⇒ 本期完成过；进度存的键 ≠ 现在的键
      ⇒ 按 0 算（到点清零）。跨 4 点结算的那一局自然按新的一期算。
    """
    if period == PERIOD_ONCE:
        return ""
    when = (time.time() if now is None else now) - RESET_HOUR * 3600
    tm = time.localtime(when)
    if period == PERIOD_DAILY:
        return "%04d-%02d-%02d" % (tm.tm_year, tm.tm_mon, tm.tm_mday)
    if period == PERIOD_WEEKLY:
        year, week, _day = datetime.date(tm.tm_year, tm.tm_mon,
                                         tm.tm_mday).isocalendar()
        return "%04d-W%02d" % (year, week)
    if period == PERIOD_MONTHLY:
        return "%04d-%02d" % (tm.tm_year, tm.tm_mon)
    raise ValueError("认不出的周期 %r" % (period,))


def stamp(now=None):
    """存档里「什么时候完成的」那一格（本地时间，和礼物的 `sent` 同一个写法）。"""
    return time.strftime("%Y-%m-%d %H:%M:%S",
                         time.localtime(time.time() if now is None else now))


# ===========================================================================
# 存档里那一格：missions = {done: {id: {p, at, times}}, progress: {id: {p, n}}}
# ===========================================================================

def empty_state():
    return {"done": {}, "progress": {}}


def normalize_state(raw):
    """存档里的 `missions` → 干净的 `{done, progress}`。认不出的条目丢掉。

    ★ 读盘补齐用（铁律 11）：老存档没有这一格 = 一条都没做过。
    ★ 键一律是任务 id 的**字符串**（JSON 对象的键只能是字符串）。
    """
    out = empty_state()
    if not isinstance(raw, dict):
        return out
    done = raw.get("done")
    if isinstance(done, dict):
        for key, entry in done.items():
            try:
                mid = int(key)
            except (TypeError, ValueError):
                continue
            if mid <= 0 or not isinstance(entry, dict):
                continue
            try:
                times = max(1, int(entry.get("times", 1)))
            except (TypeError, ValueError):
                times = 1
            out["done"][str(mid)] = {"p": str(entry.get("p") or ""),
                                     "at": str(entry.get("at") or ""),
                                     "times": times}
    progress = raw.get("progress")
    if isinstance(progress, dict):
        for key, entry in progress.items():
            try:
                mid = int(key)
                count = int((entry or {}).get("n", 0))
            except (TypeError, ValueError, AttributeError):
                continue
            if mid <= 0 or count <= 0:
                continue
            out["progress"][str(mid)] = {"p": str(entry.get("p") or ""),
                                         "n": count}
    return out


def is_done(mission, state, now=None):
    """这条任务本期完成过没有。"""
    entry = (state or {}).get("done", {}).get(str(mission["id"]))
    return bool(entry) and entry.get("p") == period_key(mission["period"], now)


def progress_of(mission, state, now=None):
    """这条任务本期的进度（上一期攒的不算）。"""
    entry = (state or {}).get("progress", {}).get(str(mission["id"]))
    if not entry or entry.get("p") != period_key(mission["period"], now):
        return 0
    return max(0, int(entry.get("n", 0)))


def times_done(mission, state):
    """一共完成过几次（跨周期累计，给管理页 / 日志看）。"""
    entry = (state or {}).get("done", {}).get(str(mission["id"]))
    return int(entry.get("times", 0)) if entry else 0


def prune_state(state, missions, now=None):
    """写盘前顺手扔掉**过期的进度**（上一期攒到一半的）。完成记录不扔（累计次数在里面）。

    ★ 规则表里已经没有的任务（只停用不删除，所以只会是手改文件删掉的）
      的进度也扔；它的完成记录留着 —— id 永不复用（D111），留着无害。
    """
    by_id = dict((str(m["id"]), m) for m in missions or ())
    out = {"done": dict(state.get("done", {})), "progress": {}}
    for key, entry in state.get("progress", {}).items():
        mission = by_id.get(key)
        if mission is None:
            continue
        if entry.get("p") == period_key(mission["period"], now):
            out["progress"][key] = dict(entry)
    return out


# ===========================================================================
# 能做 / 一局怎么计
# ===========================================================================

def char_matches(mission, character):
    """任务角色对不对得上：任务不限角色，或者就是这个角色。爱琳（3）只对得上不限的。"""
    want = mission.get("character", CHAR_ANY)
    return want == CHAR_ANY or want == character


def doable(mission, *, level, character, state, now=None):
    """**能做** = 启用 ∧ 等级够 ∧ 角色对得上 ∧ 本期没完成。"""
    return (bool(mission.get("enabled"))
            and int(level) >= int(mission.get("level", 1))
            and char_matches(mission, character)
            and not is_done(mission, state, now))


def filters_hold(mission, *, pvp_mode=None, item_mode=None, stage=None,
                 difficulty=None):
    """对局筛选（模式 / 道具 / 关卡 / 难度）对不对得上。线由调用方先比过。

    ★★ 难度默认**恰好**（D113）：所有任务同时推进以后，按「至少」打一次困难会把
      原版简单 / 普通 / 困难三条一起完成。管理员勾了「更高难度也算」才按「至少」。
    """
    if mission["line"] == LINE_PVP:
        want = mission.get("pvp_mode")
        if want is not None and want != pvp_mode:
            return False
        want = mission.get("item_mode")
        if want is not None and want != (1 if item_mode else 0):
            return False
        return True
    want = mission.get("stage")
    if want is not None and want != stage:
        return False
    want = mission.get("difficulty")
    if want is not None:
        if difficulty is None:
            return False
        if mission.get("harder"):
            return int(difficulty) >= int(want)
        return int(difficulty) == int(want)
    return True


def match_increment(mission, *, line, won, match=None, pvp_mode=None,
                    item_mode=None, stage=None, difficulty=None):
    """这一局给这条任务 **+几**（0 = 不算）。只看对局，不看能不能做。

    * 对战胜利 / 闯关通关：这一局赢了 / 通关了 ⇒ +1；
    * 按对局数据 · 每局达成：一局内条件（称号卡片那一套 and / or）成立 ⇒ +1；
    * 按对局数据 · 累计数值：+ 本局那一项的数（可以按武器）；
    * 专用关卡：0 —— 客户端打通后自己报 `0x02e1`，不走结算。
    """
    if mission.get("line") != line or mission.get("kind") == KIND_MAP:
        return 0
    if not filters_hold(mission, pvp_mode=pvp_mode, item_mode=item_mode,
                        stage=stage, difficulty=difficulty):
        return 0
    kind = mission.get("kind")
    if kind in (KIND_WIN, KIND_CLEAR):
        return 1 if won else 0
    if kind != KIND_STATS:
        return 0
    match = match or {}
    if mission.get("count") == COUNT_SUM:
        key = cards.stat_key(mission.get("metric"), mission.get("weapon"))
        return max(0, int(match.get(key, 0)))
    holds, _used, _warnings = cards.eval_conditions(
        mission.get("conditions"), rule_mode=line, match=match, total={},
        base={})
    return 1 if holds else 0


def settle(missions, state, *, level, character, line, won, match=None,
           pvp_mode=None, item_mode=None, stage=None, difficulty=None,
           now=None):
    """一局打完：**所有能做且条件对上的任务同时推进**。返回 `(新存档格, [事件])`。

    事件 = `{"mission", "before", "after", "done"}`，按规则表顺序。完成的那条
    `after` 封顶在目标数，进度那一格清掉、完成记录写本期键 + 累计次数 +1。
    ★ 不改传进来的那一份。`reward_block`（X19）那一局调用方根本不该调它。
    """
    now = time.time() if now is None else now
    new = normalize_state(copy.deepcopy(state))
    events = []
    for mission in missions or ():
        if not doable(mission, level=level, character=character, state=new,
                      now=now):
            continue
        gain = match_increment(mission, line=line, won=won, match=match,
                               pvp_mode=pvp_mode, item_mode=item_mode,
                               stage=stage, difficulty=difficulty)
        if gain <= 0:
            continue
        events.append(_advance(mission, new, gain, now))
    return prune_state(new, missions, now), events


def complete_map(mission, state, *, now=None):
    """专用关卡打通（客户端 `0x02e1`）：直接记完成。返回 `(新存档格, 事件)`。

    能不能做、这条连接有没有点过它的「开始」由调用方先判。
    """
    now = time.time() if now is None else now
    new = normalize_state(copy.deepcopy(state))
    event = _advance(mission, new, max(1, int(mission.get("target", 1))), now)
    return new, event


def _advance(mission, state, gain, now):
    """就地给 `state` 里这条任务 +gain，返回事件。"""
    key = str(mission["id"])
    period = period_key(mission["period"], now)
    target = max(1, int(mission.get("target", 1)))
    before = progress_of(mission, state, now)
    after = before + int(gain)
    if after >= target:
        prev = state["done"].get(key)
        state["done"][key] = {"p": period, "at": stamp(now),
                              "times": (int(prev["times"]) if prev else 0) + 1}
        state["progress"].pop(key, None)
        return {"mission": mission, "before": before, "after": target,
                "done": True}
    state["progress"][key] = {"p": period, "n": after}
    return {"mission": mission, "before": before, "after": after,
            "done": False}


def gift_specs(mission):
    """这条任务的奖励 → `account_store.add_gifts` 认的那几份（每项一份礼物）。"""
    out = []
    for reward in mission.get("rewards") or ():
        if reward.get("item"):
            out.append({"item": int(reward["item"]),
                        "count": int(reward.get("count", 1))})
        elif reward.get("exp"):
            out.append({"exp": int(reward["exp"])})
        elif reward.get("money"):
            out.append({"money": int(reward["money"])})
    return out


def gift_message(mission):
    return GIFT_MESSAGE % display_name(mission)


# ===========================================================================
# 每个玩家自己那张表
# ===========================================================================

def client_id(mission_id, tab):
    """客户端表里的 `PromotionID` = 任务 id × 10 + 页签 + 1（永远不是 0 / -2）。"""
    return int(mission_id) * 10 + int(tab) + 1


def split_client_id(value):
    """`client_id()` 的逆。认不出（0 / 负数 / 页签位不对）返回 `None`。"""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    tab = value % 10 - 1
    if tab not in TABS:
        return None
    return value // 10, tab


def player_chains(missions, state, *, now=None):
    """这个玩家的六条链 `{(线, 页签): [任务…]}`。

    * 启用的、本期**没完成**的才进表（完成了就从面板上消失）；
    * **等级不够的照样带**：客户端自己按 `UserLevelLimit` 藏（`0x55b318`）；
    * 不限角色的三个页签各进一份（客户端 id 不同，进度共用）；
    * 每条链按 (order, id) 排，前 59 条进表 —— 超出的这次不推，后台照样计。
    """
    chains = {}
    for line in LINES:
        for tab in TABS:
            picked = [m for m in missions or ()
                      if m.get("enabled") and m["line"] == line
                      and char_matches(m, tab) and not is_done(m, state, now)]
            picked.sort(key=lambda m: (int(m.get("order", 0)), int(m["id"])))
            chains[(line, tab)] = picked[:MAX_RANK]
    return chains


def chain_heads(chains, state, *, level, now=None, pins=None):
    """六条链各自的 `(A, B, C)`，**按 `0x02e0` 的线序**：页签 0..2 × (对战, 闯关)。

    B = 玩家点「开始」钉住的那条（还在表里、等级够时），否则链里第一条等级够的；
    都没有就 0（客户端自己再找一遍也找不到，面板显示「没有任务」）。
    A 一律 0（「已完成的」已经从表里删掉了，用不上）；C = B 的本期进度。
    """
    out = []
    for tab in TABS:
        for line in LINES:
            chain = chains.get((line, tab)) or []
            pick = None
            pinned = (pins or {}).get((line, tab))
            if pinned is not None:
                pick = next((m for m in chain if m["id"] == pinned
                             and int(level) >= int(m.get("level", 1))), None)
            if pick is None:
                pick = next((m for m in chain
                             if int(level) >= int(m.get("level", 1))), None)
            if pick is None:
                out.append((0, 0, 0))
            else:
                out.append((0, client_id(pick["id"], tab),
                            progress_of(pick, state, now)))
    return out


def build_status(heads):
    """`0x02e0` 的载荷：72 字节 = 6 × {i32 A, i32 B, i32 C}。"""
    if len(heads) != len(TABS) * len(LINES):
        raise ValueError("要 %d 组 (A, B, C)，给了 %d 组"
                         % (len(TABS) * len(LINES), len(heads)))
    return b"".join(struct.pack("<iii", int(a), int(b), int(c))
                    for a, b, c in heads)


def parse_status(payload):
    """`build_status` 的逆（测试 / 日志用）。"""
    return [struct.unpack_from("<iii", payload, 12 * i)
            for i in range(len(payload) // 12)]


def build_complete(tab, line, done_id, current_id, progress, completed):
    """`0x02e1` 的载荷：24 字节。`line` 写 context type（1 对战 / 2 闯关）。"""
    return struct.pack("<iiiiii", int(tab), LINE_CONTEXT[line], int(done_id),
                       int(current_id), int(progress), 1 if completed else 0)


def parse_request_id(payload):
    """C→S `0x02e0` / `0x02e1` 的载荷 = 一个 i32 客户端 id。认不出返回 `None`。"""
    if len(payload or b"") < 4:
        return None
    return struct.unpack_from("<i", payload, 0)[0]


# ===========================================================================
# 文本：说明、条件那句话、清洗
# ===========================================================================

#: ★★ 条件那句话里**每一个固定的字**，一处定义（同 `shopcfg.CARD_PHRASES` 的道理）：
#: 管理页要在改一格的当场把同一句话画出来（`admin.js` 的 `missionConditionText`），
#: 词经 `/admin/api/catalog` 的 `mission_text` 发过去，前端一个中文字都不写死。
#: 两边句子骨架是否一致由 `test_missiontext` 逐字比。
MISSION_PHRASES = {
    "win": "%s胜利 %d 次",
    "clear": "%s通关 %d 次",
    "map": "%s打通",
    "sum": "%s累计%s %d",
    "match": "%s每局达成「%s」 %d 次",
    "map_scope": "专用关卡「%s」",
    "stage": "「%s」",
    "difficulty": "（%s%s）",
    "harder": "及以上",
    "filters": "（%s）",
    "filter_sep": " · ",
    "name": "[%s] %s",
    "unknown": "?",
}


def _stage_label(stage):
    name = shopcfg.QUEST_ZH.get(stage)
    return "%s · %s" % (stage, name) if name else str(stage)


def scope_text(mission):
    """「在什么局里」那半句：`对战（生存模式 · 道具战）` / `闯关「3 · 神秘岛」（困难及以上）`。"""
    words = MISSION_PHRASES
    line = mission.get("line")
    if mission.get("kind") == KIND_MAP:
        return words["map_scope"] % (mission.get("map") or words["unknown"])
    if line == LINE_PVP:
        parts = []
        if mission.get("pvp_mode") is not None:
            parts.append(shopcfg.PVP_MODE_ZH.get(mission["pvp_mode"],
                                                 str(mission["pvp_mode"])))
        if mission.get("item_mode") is not None:
            parts.append(ITEM_MODE_ZH.get(mission["item_mode"],
                                          str(mission["item_mode"])))
        return LINE_ZH[LINE_PVP] + (words["filters"] % words["filter_sep"].join(parts)
                                    if parts else "")
    text = LINE_ZH[LINE_QUEST]
    if mission.get("stage") is not None:
        text += words["stage"] % _stage_label(mission["stage"])
    if mission.get("difficulty") is not None:
        text += words["difficulty"] % (
            shopcfg.DIFFICULTY_ZH.get(mission["difficulty"], mission["difficulty"]),
            words["harder"] if mission.get("harder") else "")
    return text


def condition_text(mission):
    """**条件那句话**（管理页那一行中间那句、默认的「完成条件」都念它）。

        对战（生存模式）胜利 3 次
        闯关「2 · 岩浆巨龙」（普通）通关 1 次
        对战每局达成「一局内的击杀数大于等于5并且本局结果为胜利 / 通关」 10 次
        闯关累计击杀数 500
        专用关卡「QuestLevel01」打通

    ★ `admin.js` 的 `missionConditionText()` 是它的孪生，`test_missiontext` 逐字比。
    """
    words = MISSION_PHRASES
    kind = mission.get("kind")
    target = int(mission.get("target", 1))
    head = scope_text(mission)
    if kind == KIND_WIN:
        return words["win"] % (head, target)
    if kind == KIND_CLEAR:
        return words["clear"] % (head, target)
    if kind == KIND_MAP:
        return words["map"] % head
    if mission.get("count") == COUNT_SUM:
        label = shopcfg.card_metric_label(mission.get("metric"),
                                          mission.get("weapon"))
        return words["sum"] % (head, label, target)
    body = shopcfg.card_conditions_text(mission.get("conditions"))
    return words["match"] % (head, body or shopcfg.CARD_PHRASES["bad"], target)


def display_name(mission):
    """面板上那一行的名字：管理员填了就用，没填按条件现编一个 ——
    「[每日] 对战（生存模式）胜利 3 次」（周期打头，仅一次的写「任务」）。"""
    name = (mission.get("name") or "").strip()
    if name:
        return name
    return MISSION_PHRASES["name"] % (
        PERIOD_NAME_ZH.get(mission.get("period"), MISSION_PHRASES["unknown"]),
        condition_text(mission))


def info_text(mission):
    """`TextMissionInfo`：管理员填了就用，没填 = 「每日任务 - 等级 5」这种。"""
    info = (mission.get("info") or "").strip()
    if info:
        return info
    period = mission.get("period", PERIOD_ONCE)
    head = "" if period == PERIOD_ONCE else PERIOD_ZH[period]
    return "%s任务 - 等级%d" % (head, int(mission.get("level", 1)))


def clear_text(mission):
    """`TextClearCondition`：管理员填了就用，没填 = 条件那句话。"""
    text = (mission.get("clear_text") or "").strip()
    return text or condition_text(mission)


def status_text(mission):
    """`TextStatus`（printf 格式串，客户端拿 C 去填 `%d`）：「今日 %d/3胜」「%d/1次」。

    ★ 不让管理员填：多一个 `%` 客户端就按格式串去读不存在的参数（会崩）。
    """
    target = int(mission.get("target", 1))
    kind = mission.get("kind")
    unit = "胜" if kind == KIND_WIN else (
        "" if kind == KIND_STATS and mission.get("count") == COUNT_SUM else "次")
    return "%s%%d/%d%s" % (PERIOD_STATUS_ZH.get(mission.get("period"), ""),
                           target, unit)


def reward_text(reward):
    """一项奖励 → `TextRewardN` 那一行：「[金币] +100」「[道具] 小鸡T恤 ×2」。"""
    if reward.get("money"):
        return "[金币] +%d" % int(reward["money"])
    if reward.get("exp"):
        return "[经验值] +%d" % int(reward["exp"])
    item = int(reward.get("item") or 0)
    name = shopcfg.item_name(item) or ("#%d" % item)
    count = int(reward.get("count", 1))
    # 原版的写法：材料是「[材料] 合金螺旋桨」，别的是「[道具] 小鸡T恤」。
    head = "[材料]" if shopdata.is_material(item) else "[道具]"
    return "%s %s%s" % (head, name, " ×%d" % count if count > 1 else "")


def reward_wire(reward):
    """一项奖励 → `RewardN` 那一格：金币 `0,数` / 经验 `2,数` / 物品 `1,id,0`。"""
    if reward.get("money"):
        return "0,%d" % int(reward["money"])
    if reward.get("exp"):
        return "2,%d" % int(reward["exp"])
    return "1,%d,0" % int(reward.get("item") or 0)


def rewards_text(mission):
    """奖励摘要（聊天 / 管理页那一行）：「[金币] +100、[经验值] +20」。"""
    return "、".join(reward_text(r) for r in mission.get("rewards") or ())


def sanitize(text):
    """写进客户端 ini 之前洗一遍（D116）：

    * 控制字符 / 换行去掉（一行一个键）；
    * `#` → `＃`：客户端在**每行第一个 `#`** 处截断（`0x5d48c3`），不只是行首；
    * `%` → `％`：只有 `TextStatus` 是 printf 格式串，但别的文本会不会经过
      `Format` 没逐条核，换成全角两种情况下都显示得对。`TextStatus` 不走这里；
    * 头尾空白去掉（客户端本来也去）。
    """
    out = []
    for ch in str(text or ""):
        code = ord(ch)
        if code < 0x20 or code == 0x7F:
            out.append(" ")
        elif ch == "#":
            out.append("＃")
        elif ch == "%":
            out.append("％")
        else:
            out.append(ch)
    return " ".join("".join(out).split())


def admin_text():
    """管理页念句子用的**全部词**（`/admin/api/catalog` 的 `mission_text`）。

    ★ 句子骨架两边各一份（`admin.js` 的 `missionConditionText` 是孪生），词只在
      这儿 —— 管理页一个中文字都不写死。`test_missiontext` 拿同一份去比。
    """
    return {
        "phrases": MISSION_PHRASES,
        "lines": LINE_ZH,
        "kinds": KIND_ZH,
        "periods": PERIOD_ZH,
        "period_names": PERIOD_NAME_ZH,
        "counts": COUNT_ZH,
        "pvp_modes": dict((str(k), v) for k, v in shopcfg.PVP_MODE_ZH.items()),
        "item_modes": dict((str(k), v) for k, v in ITEM_MODE_ZH.items()),
        "characters": dict([(str(CHAR_ANY), "不限角色")]
                           + [(str(k), v) for k, v in
                              shopcfg.CHARACTER_ZH.items()]),
    }


def admin_limits():
    """管理页那道「当场看得见哪儿不对」的判据参数（`mission_limits`）。
    服务端 `validate_missions` 说了算，这些只是让页面别把上下限写死。"""
    return {
        "max_rewards": MAX_REWARDS,
        "first_new_id": FIRST_NEW_ID,
        "max_target": MAX_TARGET,
        "max_level": MAX_LEVEL,
        "max_order": MAX_ORDER,
        "max_rank": MAX_RANK,
        "text_limits": TEXT_LIMITS,
        "tabs": list(TABS),
        "char_any": CHAR_ANY,
        "sum_metrics": sum_metrics(),
        "max_reward_amount": shopcfg.MAX_REWARD,
    }


# ===========================================================================
# 客户端 ini（bshook 喂给游戏自己的加载器）
# ===========================================================================

def section_name(line, tab, rank):
    return CHAIN_FORMAT[line] % TAB_PREFIX[tab] + str(int(rank))


def promotion_type(mission):
    kind = mission.get("kind")
    if kind == KIND_MAP:
        return TYPE_MAP
    if kind == KIND_WIN:
        return TYPE_PVP
    if kind == KIND_CLEAR:
        return TYPE_QUEST
    return TYPE_PVP if mission.get("line") == LINE_PVP else TYPE_QUEST


def record_fields(mission, tab, rank):
    """一条任务在某个页签里的那一节 `[(键, 值)]`。键名全是镜像里的串
    （`test_patchsites_promo` 钉着）。五个缺了会显示「Invalid String」的文本键总写。"""
    line = mission["line"]
    fields = [
        ("PromotionID", client_id(mission["id"], tab)),
        ("PromotionLevel", int(rank)),
        ("UserLevelLimit", int(mission.get("level", 1))),
        ("CharacterLimit", int(tab)),
        ("PromotionGameContextType", LINE_CONTEXT[line]),
        ("PromotionType", promotion_type(mission)),
        ("PromotionValue", int(mission.get("target", 1))),
    ]
    if line == LINE_PVP:
        if mission.get("pvp_mode") is not None:
            fields.append(("PromotionGameContextSubType", int(mission["pvp_mode"])))
        if mission.get("item_mode") is not None:
            fields.append(("PromotionGameContextSubType2",
                           int(mission["item_mode"])))
    elif mission.get("kind") == KIND_MAP:
        fields.append(("MapFileName", sanitize(mission.get("map"))))
        fields.append(("ScriptPath", sanitize(mission.get("script"))))
        if mission.get("difficulty") is not None:
            fields.append(("PromotionDifficultyLevel", int(mission["difficulty"])))
    else:
        # ★ 不限写 1 / 1：这两格只影响「开始」钮（type 4 一律发快速加入，
        #   服务端不看参数），判定全在服务端。
        fields.append(("PromotionQuestStageID", int(mission.get("stage") or 1)))
        fields.append(("PromotionDifficultyLevel",
                       int(mission.get("difficulty") or 1)))
    fields.extend([
        ("Name", sanitize(display_name(mission))),
        ("TextMissionInfo", sanitize(info_text(mission))),
        ("TextClearCondition", sanitize(clear_text(mission))),
    ])
    rewards = list(mission.get("rewards") or ())[:MAX_REWARDS]
    for slot in range(MAX_REWARDS):
        if slot < len(rewards):
            fields.append(("TextReward%d" % (slot + 1),
                           sanitize(reward_text(rewards[slot]))))
        elif slot == 0:
            fields.append(("TextReward1", ""))
    fields.append(("TextStatus", status_text(mission)))
    for slot, reward in enumerate(rewards):
        fields.append(("Reward%d" % slot, reward_wire(reward)))
    return fields


def build_client_ini(chains):
    """六条链 → 客户端那张 `Promotion-chn.ini`（UTF-16LE + BOM + CRLF）。"""
    lines = ["# X_Mod 晋级任务表（服务端生成，X23）"]
    for tab in TABS:
        for line in LINES:
            for rank, mission in enumerate(chains.get((line, tab)) or (),
                                           start=1):
                if rank > MAX_RANK:
                    break
                lines.append("[%s]" % section_name(line, tab, rank))
                for key, value in record_fields(mission, tab, rank):
                    lines.append("%s=%s" % (key, value))
                lines.append("")
    text = "\r\n".join(lines) + "\r\n"
    return b"\xff\xfe" + text.encode("utf-16-le")


def build_table_frames(data, serial):
    """整张表切成 `0x0F03` 的几片：`[载荷…]`。★ 调用方要**整批一次**入发送队列。"""
    data = bytes(data)
    crc = zlib.crc32(data) & 0xFFFFFFFF
    chunks = [data[at:at + TABLE_CHUNK]
              for at in range(0, len(data), TABLE_CHUNK)] or [b""]
    frames = []
    offset = 0
    for index, chunk in enumerate(chunks):
        frames.append(TABLE_HEADER.pack(TABLE_FORMAT, int(serial) & 0xFFFFFFFF,
                                        len(data), crc, index, len(chunks),
                                        offset) + chunk)
        offset += len(chunk)
    return frames


def parse_table_frames(frames):
    """`build_table_frames` 的逆，**按 bshook 的规矩**拼（测试用）：
    `index==0` 重置；后续片 serial / total / crc / count 一致、index / offset 连续；
    收齐验长度和 crc。不对就抛 `ValueError`（hook 那边是丢弃这份 + 记一行）。"""
    got = None
    for frame in frames:
        if len(frame) < TABLE_HEADER.size:
            raise ValueError("片头不完整")
        fmt, serial, total, crc, index, count, offset = \
            TABLE_HEADER.unpack_from(frame, 0)
        body = frame[TABLE_HEADER.size:]
        if fmt != TABLE_FORMAT:
            raise ValueError("格式号 %d 不认识" % fmt)
        if index == 0:
            got = {"serial": serial, "total": total, "crc": crc,
                   "count": count, "next": 0, "data": bytearray()}
        if got is None:
            raise ValueError("没收到第 0 片")
        if (serial, total, crc, count) != (got["serial"], got["total"],
                                           got["crc"], got["count"]):
            raise ValueError("片头和第 0 片对不上")
        if index != got["next"] or offset != len(got["data"]):
            raise ValueError("片号 / 偏移不连续")
        if len(got["data"]) + len(body) > total:
            raise ValueError("数据比总长还多")
        got["data"] += body
        got["next"] += 1
    if got is None or got["next"] != got["count"]:
        raise ValueError("没收齐")
    if len(got["data"]) != got["total"]:
        raise ValueError("长度不对")
    if zlib.crc32(bytes(got["data"])) & 0xFFFFFFFF != got["crc"]:
        raise ValueError("crc 不对")
    return got["serial"], bytes(got["data"])


# ===========================================================================
# 客户端加载器的 Python 镜像（测试用：原版 ini 读得出、生成→解析往返一致）
# ===========================================================================

#: 记录字段 → (键名, 缺省值)。缺省值照 `0x55a348` 每一发 `0x55b74a` 前的 ebx。
_INT_KEYS = (
    ("PromotionID", -2), ("PromotionLevel", -2), ("UserLevelLimit", -1),
    ("CharacterLimit", -2), ("PrecedingPromotionNumber", -2),
    ("PromotionGameContextType", -2), ("PromotionGameContextSubType", -1),
    ("PromotionGameContextSubType2", -1), ("PromotionType", -2),
    ("PromotionValue", -2), ("PromotionItemProb", -2), ("PromotionItemID", -2),
    ("PromotionQuestStageID", -2), ("PromotionDifficultyLevel", -2),
)
_TEXT_DEFAULT = "Invalid String"
_TEXT_KEYS = (
    ("Name", _TEXT_DEFAULT), ("TextMissionInfo", _TEXT_DEFAULT),
    ("TextClearCondition", _TEXT_DEFAULT), ("TextReward1", _TEXT_DEFAULT),
    ("TextReward2", ""), ("TextReward3", ""), ("TextStatus", _TEXT_DEFAULT),
    ("MapFileName", ""), ("ScriptPath", ""),
)
#: `0x55a348` 那四格缺一格整张表拒收（PromotionID 缺了只是跳过这一节）。
_REQUIRED = ("PromotionLevel", "PromotionGameContextType", "PromotionType",
             "CharacterLimit")


class ClientRejects(ValueError):
    """客户端加载器会**整张表拒收**的那种错。"""


def _wtoi(text):
    """`_wtoi` 的口径：前导空白、可选正负号、一串数字；别的都停。"""
    text = (text or "").lstrip()
    sign = 1
    if text[:1] in ("+", "-"):
        sign = -1 if text[0] == "-" else 1
        text = text[1:]
    digits = ""
    for ch in text:
        if "0" <= ch <= "9":
            digits += ch
        else:
            break
    return sign * int(digits) if digits else 0


def parse_ini_sections(data):
    """通用 ini 解析（`0x5d48c3`）的镜像：`{小节: {键: 值}}`。"""
    raw = bytes(data)
    if raw[:2] == b"\xff\xfe":
        text = raw[2:].decode("utf-16-le", "replace")
    else:
        text = raw.decode("cp949", "replace")
    sections = {}
    current = None
    for line in text.split("\n"):
        line = line.strip(" \r\n")
        cut = line.find("#")
        if cut >= 0:
            line = line[:cut]
        if not line:
            continue
        if line[0] == "[":
            current = sections.setdefault(line.strip(" []\t"), {})
            continue
        at = line.find("=")
        if at < 0 or current is None:
            continue
        key = line[:at].strip(" \r\n\t")
        # ★ 重复的键：客户端的表插入遇重复不覆盖（原版那几节重复的
        #   `TextMissionInfo` 读到的是第一条）。
        current.setdefault(key, line[at + 1:].strip(" \r\n\t"))
    return sections


def parse_client_ini(data):
    """客户端加载器 `0x55a2d9` 的镜像：按它的顺序读出全部记录，或抛 `ClientRejects`。

    返回 `[{"tab", "line", "section", 键…}]`，顺序 = 客户端 vector 里的顺序
    （页签 0..2 × 对战链、闯关链 × 小节号 0..59）。
    """
    sections = parse_ini_sections(data)
    records = []
    for tab in TABS:
        for line in LINES:
            for index in range(CHAIN_SLOTS):
                name = section_name(line, tab, index)
                fields = sections.get(name)
                if fields is None:
                    continue
                rec = {"tab": tab, "line": line, "section": name}
                for key, default in _INT_KEYS:
                    rec[key] = (_wtoi(fields[key]) if key in fields
                                else default)
                for key, default in _TEXT_KEYS:
                    rec[key] = fields.get(key, default)
                rewards = []
                for slot in range(4):
                    text = fields.get("Reward%d" % slot)
                    if text is None:
                        rewards.append((-2, -2, -2))
                        continue
                    parts = [_wtoi(p) for p in text.split(",")][:3]
                    rewards.append(tuple(parts + [-2] * (3 - len(parts))))
                rec["Rewards"] = rewards
                if rec["PromotionID"] == -2:
                    continue
                if rec["PrecedingPromotionNumber"] > 2:
                    raise ClientRejects("%s: PrecedingPromotionNumber > 2" % name)
                for key in _REQUIRED:
                    if rec[key] == -2:
                        raise ClientRejects("%s 缺 %s" % (name, key))
                records.append(rec)
    return records


# ===========================================================================
# 校验（shopcfg.validate_missions 的本体）
# ===========================================================================

def _int(value, where, low=None, high=None):
    return shopcfg._as_int(value, where, low=low, high=high)


def _text(value, where, limit):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise shopcfg.ConfigError("%s 必须是文字" % where)
    text = " ".join(value.split())
    if len(text) > limit:
        raise shopcfg.ConfigError("%s 太长了（最多 %d 个字，现在 %d）"
                                  % (where, limit, len(text)))
    return text


def validate_rewards(raw, where):
    """奖励：1 ~ 3 项，每项一样东西（金币 / 经验 / 物品 + 数量）。物品校验同批量发奖：
    客户端认得、能进背包；装备类数量固定 1。"""
    if not isinstance(raw, list) or not raw:
        raise shopcfg.ConfigError("%s.rewards 至少要有一项奖励" % where)
    if len(raw) > MAX_REWARDS:
        raise shopcfg.ConfigError(
            "%s.rewards 最多 %d 项（任务说明框和完成弹窗只有 %d 行），给了 %d 项"
            % (where, MAX_REWARDS, MAX_REWARDS, len(raw)))
    out = []
    for at, item in enumerate(raw):
        spot = "%s.rewards[%d]" % (where, at)
        if not isinstance(item, dict):
            raise shopcfg.ConfigError("%s 不是对象" % spot)
        kinds = [k for k in ("money", "exp", "item") if item.get(k)]
        if len(kinds) != 1:
            raise shopcfg.ConfigError("%s 必须正好是金币 / 经验 / 物品里的一样"
                                      % spot)
        kind = kinds[0]
        if kind == "item":
            item_id = _int(item["item"], spot + ".item", low=1)
            if not shopdata.ownable(item_id):
                raise shopcfg.ConfigError(
                    "%s：物品 %d 不在客户端认得的物品表里（或者进不了背包），"
                    "发不出去" % (spot, item_id))
            count = _int(item.get("count", 1), spot + ".count", low=1,
                         high=shopcfg.MAX_REWARD)
            if not shopdata.stackable(item_id):
                count = 1
            out.append({"item": item_id, "count": count})
        else:
            out.append({kind: _int(item[kind], "%s.%s" % (spot, kind), low=1,
                                   high=shopcfg.MAX_REWARD)})
    return out


def _validate_conditions(entry, where):
    """「每局达成算 1 次」那串条件：称号卡片那套校验，**只许一局内**。"""
    conditions = shopcfg._validate_card_conditions(entry, where)
    for at, cond in enumerate(conditions):
        if cond.get("scope") != shopcfg.CARD_SCOPE_MATCH:
            raise shopcfg.ConfigError(
                "%s.conditions[%d]：任务的条件只看「一局内」—— 累计由任务"
                "自己的进度负责" % (where, at))
    return conditions


def sum_metrics():
    """「累计数值」能选哪些指标：一局内取得到数的、不是比率也不是枚举的。"""
    return [key for key in shopcfg.card_metric_keys()
            if not shopcfg.card_metric_is_ratio(key)
            and not shopcfg.card_metric_values(key)]


def validate_missions(raw):
    """`missions.json` → `[任务…]`；有一条不对就抛 `shopcfg.ConfigError`。"""
    if not isinstance(raw, dict):
        raise shopcfg.ConfigError("missions.json 的最外层必须是一个对象")
    entries = raw.get("missions")
    if not isinstance(entries, list):
        raise shopcfg.ConfigError("missions.json 缺少 missions 列表")
    out = []
    seen = {}
    for index, entry in enumerate(entries):
        where = "missions[%d]" % index
        if not isinstance(entry, dict):
            raise shopcfg.ConfigError("%s 不是对象" % where)
        mid = _int(entry.get("id"), where + ".id", low=1, high=MAX_ID)
        if mid in seen:
            raise shopcfg.ConfigError("%s：任务 id %d 和 %s 重了" % (where, mid,
                                                                  seen[mid]))
        seen[mid] = where
        period = entry.get("period", PERIOD_ONCE)
        if period not in PERIODS:
            raise shopcfg.ConfigError("%s.period 只能是 %s：%r"
                                      % (where, " / ".join(PERIODS), period))
        character = _int(entry.get("character", CHAR_ANY), where + ".character",
                         low=CHAR_ANY, high=max(TABS))
        line = entry.get("line")
        if line not in LINES:
            raise shopcfg.ConfigError("%s.line 只能是 %s：%r"
                                      % (where, " / ".join(LINES), line))
        kind = entry.get("kind")
        if kind not in KINDS:
            raise shopcfg.ConfigError("%s.kind 只能是 %s：%r"
                                      % (where, " / ".join(KINDS), kind))
        mission = {
            "id": mid,
            "enabled": bool(entry.get("enabled", False)),
            "period": period,
            "character": character,
            "line": line,
            "order": _int(entry.get("order", 0), where + ".order", low=0,
                          high=MAX_ORDER),
            "level": _int(entry.get("level", 1), where + ".level", low=1,
                          high=MAX_LEVEL),
            "kind": kind,
            "target": _int(entry.get("target", 1), where + ".target", low=1,
                           high=MAX_TARGET),
        }
        for key, limit in TEXT_LIMITS.items():
            mission[key] = _text(entry.get(key), "%s.%s" % (where, key), limit)
        if kind == KIND_WIN and line != LINE_PVP:
            raise shopcfg.ConfigError("%s：「对战胜利」只能在对战线" % where)
        if kind in (KIND_CLEAR, KIND_MAP) and line != LINE_QUEST:
            raise shopcfg.ConfigError("%s：「%s」只能在闯关线"
                                      % (where, KIND_ZH[kind]))
        if line == LINE_PVP and kind != KIND_MAP:
            for key, allowed in (("pvp_mode", PVP_MODES),
                                 ("item_mode", ITEM_MODES)):
                value = entry.get(key)
                if value is not None:
                    value = _int(value, "%s.%s" % (where, key))
                    if value not in allowed:
                        raise shopcfg.ConfigError(
                            "%s.%s 只能是 %s 或者不写（= 不限）：%r"
                            % (where, key, " / ".join(map(str, allowed)), value))
                    mission[key] = value
        if line == LINE_QUEST and kind != KIND_MAP:
            value = entry.get("stage")
            if value is not None:
                mission["stage"] = _int(value, where + ".stage", low=1,
                                        high=max(shopcfg.QUEST_ZH))
            value = entry.get("difficulty")
            if value is not None:
                mission["difficulty"] = _int(value, where + ".difficulty",
                                             low=1,
                                             high=max(shopcfg.DIFFICULTY_ZH))
                if entry.get("harder"):
                    mission["harder"] = True
        if kind == KIND_MAP:
            # ★ 专用关卡不能新建（用户定）：只留原版那 21 条，地图 / 脚本只读。
            if mid >= FIRST_NEW_ID:
                raise shopcfg.ConfigError(
                    "%s：「专用关卡」不能新建，只有原版导入的那几条" % where)
            for key in ("map", "script"):
                value = entry.get(key)
                if not isinstance(value, str) or not value.strip():
                    raise shopcfg.ConfigError("%s.%s 不能为空" % (where, key))
                mission[key] = value.strip()
            if entry.get("difficulty") is not None:
                mission["difficulty"] = _int(entry["difficulty"],
                                             where + ".difficulty", low=1,
                                             high=max(shopcfg.DIFFICULTY_ZH))
            mission["target"] = 1
        if kind == KIND_STATS:
            count = entry.get("count", COUNT_MATCH)
            if count not in COUNTS:
                raise shopcfg.ConfigError("%s.count 只能是 %s：%r"
                                          % (where, " / ".join(COUNTS), count))
            mission["count"] = count
            if count == COUNT_MATCH:
                mission["conditions"] = _validate_conditions(entry, where)
            else:
                metric = entry.get("metric")
                if metric not in sum_metrics():
                    raise shopcfg.ConfigError(
                        "%s.metric 累计不了 %r（能选的有 %s）"
                        % (where, metric, "、".join(sum_metrics())))
                mission["metric"] = metric
                weapon = entry.get("weapon")
                if weapon is not None:
                    weapon = _int(weapon, where + ".weapon", low=1)
                    if weapon not in shopcfg.WEAPON_CARDS:
                        raise shopcfg.ConfigError("%s.weapon 不是武器族号：%r"
                                                  % (where, weapon))
                    if not shopcfg.card_metric_needs_weapon(metric):
                        raise shopcfg.ConfigError(
                            "%s：指标「%s」和武器无关，不该写 weapon"
                            % (where, shopcfg.CARD_METRIC_ZH[metric]))
                    mission["weapon"] = weapon
        mission["rewards"] = validate_rewards(entry.get("rewards"), where)
        out.append(mission)
    return out


def next_id(missions):
    """新建任务的 id = max(10000, 现有最大) + 1。**没有删除只有停用** ⇒ 永不复用。"""
    top = max([int(m.get("id", 0)) for m in missions or ()] + [FIRST_NEW_ID])
    return top + 1
