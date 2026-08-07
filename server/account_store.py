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
}

#: 客户端认为「教程已完成」的最小值。大厅 `0x43b357` 是 `cmp eax,3 / jge`（§54）。
TUTORIAL_DONE_STATE = 3


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


def player_money(account):
    """账号金币（客户端全局 `0x72e330`）。

    ★ **登录包里没有金币字段** —— `gspRepLogin` 的处理器 `0x54f2cc` 一路写到
    `0x72e378`，唯独没碰 `0x72e330`。金币只能由 `0x0600 gspRepMoney` 下发，
    这就是为什么以前一重登金币就归零（FINDINGS §95）。
    """
    return _non_negative(account, "money")

