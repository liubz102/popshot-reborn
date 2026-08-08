#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地假后台的 JSON 账号存档。

认证服和游戏服是两个独立进程。认证服收到账号密码后把 ``active_account``
写进同一份 JSON；游戏服处理 gcpReqLogin 时重新读取它。当前项目只支持一个
localhost 客户端，因此单一活动账号足够；将来若支持并发客户端，应改成票据映射。
"""
from __future__ import annotations

import copy
import json
import os
import threading


SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(SERVER_DIR, "data", "accounts.json")

NEW_ACCOUNT_DEFAULTS = {
    "password": "",
    "display_name": "",
    "tutorial_completed": False,
    #: 客户端 `0x030f gcpReqFirstUserResult` 上报的教程进度原始值（实测 4 / 5）。
    #: 只做保真记录，是否跳过教程由 `tutorial_completed` 说了算（见 tutorial_state）。
    "tutorial_progress": 0,
    "level": 1,
    "experience": 0,
    "money": 0,
    #: 房间里「人物选择」选中的角色 id（`SessionSlot+0x0c`）。客户端点头像时
    #: 用 `0x0301` 把整个座位报上来，服务端存下再广播回去（FINDINGS §103）。
    "character": 0,
    #: 每个关卡「已通关过的最高难度」（1=简单 / 2=普通 / 3=困难）。
    #: 键是关卡 id 的**字符串**形式（JSON 的对象键只能是字符串）。
    #: 服务端用 `0x020c gspQuestReachedDifficulty` 全量下发，客户端的开局准入
    #: 校验只放行「已达成难度 + 1」以内的难度（FINDINGS §118）。
    "quest_difficulty": {},
    #: True = 三个难度直接全开，不要求逐级通关（默认）。
    #: 置 False 就恢复原版的逐级解锁：通关简单才能选普通，通关普通才能选困难。
    "quest_unlock_all": True,
    #: True = 房间「人物选择」里把 11 个商城角色全部放出来（默认）。
    #: 置 False 就只放 `owned_characters` 里列着的那几个（原版是花钱买）。
    "character_unlock_all": True,
    #: 手动持有的商城角色 id 列表（`character_unlock_all` 为 False 时才看它）。
    "owned_characters": [],
}

#: 客户端认为「教程已完成」的最小值。大厅 `0x43b357` 是 `cmp eax,3 / jge`（§54）。
TUTORIAL_DONE_STATE = 3

#: 客户端 `0x6dc52c` 的关卡 id 表（建房对话框的「任务」下拉框按这个顺序填）。
#: 「全开」时给这几个 id 都发满难度；目录里没有记录的 id 发了也无害，
#: 那张 map 只是 `关卡 id -> 已达成难度`，没记录的条目不会被读到。
QUEST_ID_TABLE = (3, 2, 1, 4, 5, 6, 7)

#: 难度上限。准入校验 `0x4683ba` 把「已达成难度 + 1」夹到 4 之后再和所选难度
#: 比大小，所以存档里记到 4 就等于全开（实际可选的只有 1/2/3，见 §118）。
QUEST_DIFFICULTY_MAX = 4

#: 三个**永远可选**的基础角色（`0x40713a` 结尾那句 `lea eax,[edi+3]`）。
#: 客户端的持有判定 `0x55853c` 对 `角色 id < 3` 直接 `return true`，
#: 服务端发不发物品都一样。0=타이 泰尔 / 1=카실 卡希尔 / 2=프로코 布洛克。
BASE_CHARACTER_IDS = (0, 1, 2)

#: 商城角色 id。客户端 `0x40713a` / `0x407168` 固定枚举 100 起的这一段，
#: 逐个问「我的背包里有没有它的物品」。`Data/ChrProps.ini` 里这 11 个是
#: 100 엘리어스 / 101 진 / 102 발키리 / 103 화이트 엘리어스 / 104 발키리 로터스 /
#: 105 발키리 재규어 / 106 시리아 / 107 라스 / 108 라스 티타늄 / 109 파이크 /
#: 110 시리아 마스。
#: （id 3 아이린 和 98 쉐도우 타이 被 `CharacterChanger` 的按钮循环
#:   `0x4f58e8`/`0x4f58f1` 显式跳过，id 99 랜덤 要另一个开关，都放不出来。）
PREMIUM_CHARACTER_IDS = tuple(range(100, 111))

#: 商城角色物品 id 的第二段。真实商城条目是 `ShopItem.ini` 的
#: `[Item-101400001]`…`[Item-111400001]`，即 `(角色 id + 1) * 1000000 + 400001`。
#: 客户端的判定 `0x55851f` 只要求落在 `[(id+1)*1e6, (id+2)*1e6)` 区间内，
#: 但照抄真实 id 才能让 `0x505bb9` 在物品表里查到定义、把外观也套上。
CHARACTER_ITEM_SUFFIX = 400001

#: 物品 id 的「一个角色一段」步长（`0x55853c` 的 `imul eax,eax,0xf4240`）。
ITEM_ID_CHARACTER_STRIDE = 1000000


def character_item_id(character_id):
    """商城角色 id -> 解锁它所需的背包物品 id。

    客户端 `0x55853c(角色 id)`：`id < 3` 直接放行，否则拿
    `(id + 1) * 1000000` 当下界去背包的 `vector<int32>` 里 `find`，
    命中区间 `[下界, 下界 + 1000000)` 就算持有（FINDINGS §119）。
    """
    return ((int(character_id) + 1) * ITEM_ID_CHARACTER_STRIDE
            + CHARACTER_ITEM_SUFFIX)


def character_unlock_all(account):
    """账号是否「商城角色全开」。存档里没写就按 True。"""
    if not account:
        return True
    return bool(account.get("character_unlock_all", True))


def owned_characters(account):
    """要放进「人物选择」的商城角色 id（已排序、去重、只留已知的那 11 个）。

    ★ 基础的 0/1/2 不在里面 —— 客户端对它们根本不查背包。
    """
    if character_unlock_all(account):
        return list(PREMIUM_CHARACTER_IDS)
    raw = (account or {}).get("owned_characters")
    if not isinstance(raw, (list, tuple)):
        return []
    known = set(PREMIUM_CHARACTER_IDS)
    picked = set()
    for value in raw:
        try:
            character_id = int(value)
        except (TypeError, ValueError):
            continue
        if character_id in known:
            picked.add(character_id)
    return sorted(picked)


def character_item_ids(account):
    """要用 `0x030b gspSlotEquippedList` 下发的背包物品 id 列表。"""
    return [character_item_id(character_id)
            for character_id in owned_characters(account)]


class AccountStore:
    """每次操作都重新读盘，方便用户直接编辑 JSON 后让下次登录生效。"""

    def __init__(self, path: str | None = None):
        self.path = os.path.abspath(path or DEFAULT_PATH)
        self._lock = threading.RLock()
        self.ensure_exists()

    @staticmethod
    def _empty():
        return {"schema_version": 1, "active_account": None, "accounts": {}}

    def _read_unlocked(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return self._empty()
        if not isinstance(data, dict):
            raise ValueError(f"账号文件根节点必须是对象: {self.path}")
        data.setdefault("schema_version", 1)
        data.setdefault("active_account", None)
        accounts = data.setdefault("accounts", {})
        if not isinstance(accounts, dict):
            raise ValueError(f"accounts 必须是对象: {self.path}")
        return data

    def _write_unlocked(self, data):
        parent = os.path.dirname(self.path)
        os.makedirs(parent, exist_ok=True)
        tmp = f"{self.path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

    def ensure_exists(self):
        with self._lock:
            if os.path.exists(self.path):
                # 立即校验，避免服务跑到登录时才报 JSON 格式错误。
                self._read_unlocked()
                return
            self._write_unlocked(self._empty())

    @staticmethod
    def _merged_account(username, raw):
        account = copy.deepcopy(NEW_ACCOUNT_DEFAULTS)
        if isinstance(raw, dict):
            account.update(raw)
        account["display_name"] = account.get("display_name") or username
        return account

    def login(self, username, password):
        """记录本地登录并自动创建未知账号；当前假后台不校验密码。"""
        username = str(username or "local").strip() or "local"
        password = str(password or "")
        with self._lock:
            data = self._read_unlocked()
            raw = data["accounts"].get(username)
            account = self._merged_account(username, raw)
            if raw is None:
                account["password"] = password
            # 等级由经验推出（D024）。手工编辑过存档的话，这里把它校回自洽，
            # 免得 JSON 里的等级和经验各说各话。
            account["level"] = level_for_experience(player_experience(account))
            data["accounts"][username] = account
            data["active_account"] = username
            self._write_unlocked(data)
            return copy.deepcopy(account)

    def get_account(self, username=None):
        with self._lock:
            data = self._read_unlocked()
            username = username or data.get("active_account")
            if not username or username not in data["accounts"]:
                return None, None
            return username, self._merged_account(username, data["accounts"][username])

    def resolve_game_login(self, ticket=""):
        """优先把非空游戏票据当账号名；现阶段通常回退到活动账号。"""
        ticket = str(ticket or "").strip()
        if ticket:
            name, account = self.get_account(ticket)
            if account is not None:
                return name, account
        return self.get_account()

    def set_tutorial_completed(self, username, completed=True):
        with self._lock:
            data = self._read_unlocked()
            if username not in data["accounts"]:
                raise KeyError(username)
            account = self._merged_account(username, data["accounts"][username])
            account["tutorial_completed"] = bool(completed)
            data["accounts"][username] = account
            self._write_unlocked(data)

    def set_tutorial_progress(self, username, progress):
        """记下客户端 `0x030f` 上报的教程进度，返回更新后的账号。

        客户端跑完射击教学后会做两件事（`0x4f22c1`）：把进度值写进自己的
        `[0x72e2a4]+0x64`，再用 `gcpReqFirstUserResult` 把**同一个值**发上来。
        实测发的是 4 或 5，都 >= `TUTORIAL_DONE_STATE`。

        原始值和布尔都存：布尔是给人看、给人改的开关，原始值只做保真记录，
        下次登录时能把客户端恢复到它自己上报过的那个状态（见 `tutorial_state`）。
        """
        progress = int(progress)
        with self._lock:
            data = self._read_unlocked()
            if username not in data["accounts"]:
                raise KeyError(username)
            account = self._merged_account(username, data["accounts"][username])
            account["tutorial_progress"] = progress
            if progress >= TUTORIAL_DONE_STATE:
                account["tutorial_completed"] = True
            data["accounts"][username] = account
            self._write_unlocked(data)
            return copy.deepcopy(account)

    def set_character(self, username, character_id):
        """记下玩家在房间里选中的角色，返回更新后的账号。

        客户端点「人物选择」的头像时把**整个座位**用 `0x0301` 报上来，自己
        什么都不改，等服务端广播回来才更新预览（FINDINGS §103）。存下来是
        为了让重登、退房再建房之后选中的角色还在。
        """
        character_id = max(0, int(character_id))
        with self._lock:
            data = self._read_unlocked()
            if username not in data["accounts"]:
                raise KeyError(username)
            account = self._merged_account(username, data["accounts"][username])
            account["character"] = character_id
            data["accounts"][username] = account
            self._write_unlocked(data)
            return copy.deepcopy(account)

    def set_quest_cleared(self, username, quest_id, difficulty):
        """记下「第 `quest_id` 关的 `difficulty` 难度通关了」，返回更新后的账号。

        只往上记，不往下改：同一关用简单打了第二遍，不该把已经解锁的困难
        又锁回去。返回值供调用方判断要不要重发 `0x020c`。
        """
        quest_id = int(quest_id)
        difficulty = int(difficulty)
        with self._lock:
            data = self._read_unlocked()
            if username not in data["accounts"]:
                raise KeyError(username)
            account = self._merged_account(username, data["accounts"][username])
            records = _quest_records(account)
            if difficulty <= records.get(quest_id, 0):
                return copy.deepcopy(account)
            records[quest_id] = min(difficulty, QUEST_DIFFICULTY_MAX)
            account["quest_difficulty"] = {
                str(key): value for key, value in sorted(records.items())}
            data["accounts"][username] = account
            self._write_unlocked(data)
            return copy.deepcopy(account)

    def add_quest_reward(self, username, experience=0, money=0):
        """把一局闯关的所得记进存档，返回更新后的账号。

        `gspEndGame` 下发的经验/金币要由服务端持久化，否则玩家一退出游戏
        就回到原点 —— 这一层的真源是 JSON，协议字段只做编码（D024）。
        等级由经验推出来（`level_for_experience`），不单独存，避免两个字段
        互相打架。
        """
        experience = max(0, int(experience))
        money = max(0, int(money))
        with self._lock:
            data = self._read_unlocked()
            if username not in data["accounts"]:
                raise KeyError(username)
            account = self._merged_account(username, data["accounts"][username])
            account["experience"] = max(0, int(account.get("experience", 0))) + experience
            account["money"] = max(0, int(account.get("money", 0))) + money
            account["level"] = level_for_experience(account["experience"])
            data["accounts"][username] = account
            self._write_unlocked(data)
            return copy.deepcopy(account)


#: 每升一级需要的经验。
#:
#: ★ **这条曲线是本地假后台自己定的，不是从客户端逆出来的。**
#: 客户端只按 `gspEndGame` 下发的三个绝对值（总经验 / 下一级所需 / 本级起点）
#: 做减法算进度条（FINDINGS §94），它不知道也不关心曲线长什么样，
#: 所以这里怎么定都不会让客户端出错。想改难度直接改这个数。
EXPERIENCE_PER_LEVEL = 100


def level_for_experience(experience):
    """总经验 -> 等级（1 起步）。"""
    return max(1, int(experience) // EXPERIENCE_PER_LEVEL + 1)


def experience_bounds(experience):
    """总经验 -> ``(本级起始总经验, 下一级所需总经验)``。

    对应 `gspEndGame` 的 `pkt+0x18` 和 `pkt+0x14`。客户端算的是
    ``(总经验 - 本级起点) / (下一级所需 - 本级起点)``，所以这两个必须是
    **绝对累计值**，不是「本级内的差值」。
    """
    level = level_for_experience(experience)
    return (level - 1) * EXPERIENCE_PER_LEVEL, level * EXPERIENCE_PER_LEVEL


def tutorial_state(account):
    """账号 -> `gspRepLogin` 第 7 个业务 int32（客户端 `[0x72e2a4]+0x64`）。

    客户端在状态 >= 3 时跳过强制新手教程（FINDINGS §54）。

    **`tutorial_completed` 是权威**：它是给人编辑的开关，置 False 就一定重来一遍
    教程，哪怕存档里还留着上次上报的进度值。置 True 时优先回放客户端自己上报过的
    原始值（4 / 5），实在没有就退回刚好达标的 3。
    """
    if not account or not bool(account.get("tutorial_completed")):
        return 0
    try:
        progress = int(account.get("tutorial_progress", 0))
    except (TypeError, ValueError):
        progress = 0
    return max(TUTORIAL_DONE_STATE, progress)


def display_name(account):
    """房间座位里显示的昵称（`SessionSlot+0x04`，见 gameserver 的
    `build_session_slot`）。存档里没填就退回空串，让调用方自己兜底。"""
    if not account:
        return ""
    return str(account.get("display_name") or "")


def player_level(account):
    """账号等级，供 gspRepLogin 的第 1 个业务 int32 使用（FINDINGS §63）。

    客户端把它存进全局 `0x72e338`，用来过滤「建立房间」里四个下拉框的条目
    （`0x436833` 起）以及天梯标签页的等级门槛（`0x43b676` 要求 >=6）。
    闯关关卡记录的要求等级是 1，所以等级 0 会让任务下拉框整个空掉。
    """
    if not account:
        return 0
    try:
        return max(0, int(account.get("level", 0)))
    except (TypeError, ValueError):
        return 0


def _non_negative(account, field):
    if not account:
        return 0
    try:
        return max(0, int(account.get(field, 0)))
    except (TypeError, ValueError):
        return 0


def player_experience(account):
    """账号总经验。`gspRepLogin` 第 4 个业务 int32 和 `gspRepMoney+0x0c`
    都发它（都落在客户端全局 `0x72e33c`）。"""
    return _non_negative(account, "experience")


def player_character(account):
    """房间座位里的角色 id（`SessionSlot+0x0c`）。

    客户端 `0x406520`（`0x0301` 的 action 4）拿它去 `0x557128` 查角色名，
    房间中下那个 3D 预览也按它换模型。存档里没有就退回 0（第一个角色）。
    """
    return _non_negative(account, "character")


def _quest_records(account):
    """存档里的 `quest_difficulty` -> `{int 关卡 id: int 已达成难度}`。

    存档是给人手改的，键写成数字、值写成字符串、混进垃圾条目都可能发生，
    所以这里逐条容错：解析不出来的直接丢掉，绝不让一条脏数据把整个
    `0x020c` 的下发打掉。
    """
    if not account:
        return {}
    raw = account.get("quest_difficulty")
    if not isinstance(raw, dict):
        return {}
    records = {}
    for key, value in raw.items():
        try:
            quest_id, difficulty = int(key), int(value)
        except (TypeError, ValueError):
            continue
        if difficulty <= 0:
            continue
        records[quest_id] = min(difficulty, QUEST_DIFFICULTY_MAX)
    return records


def quest_unlock_all(account):
    """账号是否「三个难度全开」。存档里没写就按 True（见 D056）。"""
    if not account:
        return True
    return bool(account.get("quest_unlock_all", True))


def quest_difficulty_records(account):
    """要用 `0x020c gspQuestReachedDifficulty` 下发的 `{关卡 id: 已达成难度}`。

    ★ 这张表是「哪些难度能开局」的**唯一**数据源：客户端的准入校验
    `0x4683ba` 只放行 `min(已达成难度 + 1, 4)` 以内的难度，服务端不发
    就等于每一关都只有「简单」能玩（FINDINGS §118）。

    `quest_unlock_all` 为真时把已知的关卡 id 全部填满，同时保留存档里
    记着的更高值（虽然已经封顶，但不希望这个开关顺手把记录抹掉）。
    """
    records = _quest_records(account)
    if quest_unlock_all(account):
        for quest_id in QUEST_ID_TABLE:
            records[quest_id] = max(records.get(quest_id, 0),
                                    QUEST_DIFFICULTY_MAX)
    return records


def quest_cleared_difficulty(account, quest_id):
    """某一关已通关过的最高难度（没通关过就是 0）。不看「全开」开关。"""
    try:
        return _quest_records(account).get(int(quest_id), 0)
    except (TypeError, ValueError):
        return 0


def player_money(account):
    """账号金币（客户端全局 `0x72e330`）。

    ★ **登录包里没有金币字段** —— `gspRepLogin` 的处理器 `0x54f2cc` 一路写到
    `0x72e378`，唯独没碰 `0x72e330`。金币只能由 `0x0600 gspRepMoney` 下发，
    这就是为什么以前一重登金币就归零（FINDINGS §95）。
    """
    return _non_negative(account, "money")

