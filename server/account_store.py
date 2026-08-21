#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JSON 账号存档（单机假服务器和云端服务端共用）。

**V0.2 的改动**：V0.1 里认证服和游戏服是两个进程，靠往 JSON 里写 ``active_account``
传递「当前是谁在玩」，密码根本不校验 —— 那只在「全世界只有一个玩家」时成立。
现在两个服务合并进 ``server/app.py`` 一个进程（D064），账号身份靠**票据**传递
（认证服签发 -> `CULoginReplyPacket` -> 客户端 `gcpReqLogin`），
``active_account`` 已废弃，本模块只负责**存**和**校验**。

几十人规模、按需求用 JSON 不用 DB；密码按需求**明文保存**（D067）——
但日志里只打用户名和票据，不打密码。
"""
from __future__ import annotations

import bisect
import copy
import json
import os
import re
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

#: ★ 账号等级的下限 = **2**，这就是「解除对战等级限制」的实现。
#:
#: 客户端大厅点「对战」标签时判的是全局 `[0x72e338] == 1`（**恰好等于 1**，不是 `< 2`），
#: 命中就弹「不符合等级要求，无法连接」（V0.1 §83）。房间里点开始时的
#: 「等级太低，无法选择任务」读的是房主座位里的 u16 等级（V0.1 §77），同一个数。
#: 此外，中国区房主在房间里选生存模式时还有一道隐藏的 `< 4` 判断；命中后
#: 客户端会自行把模式改回夺分并回发 0x0302（V0.2 §203）。所以客户端可见等级
#: 必须永远 >= 4，才能同时打开这三道门。
#:
#: 用户已授权这个方案（「可以采用在用户注册时服务端自动将用户等级提升至满足要求的等级」）。
#: 经验值和存档等级仍然如实累计，只在向旧客户端编码时应用这个兼容下限。
MINIMUM_PLAYER_LEVEL = 4

#: 用户名规则：2~16 个 ASCII 字母 / 数字 / 下划线 / 连字符。
#:
#: 为什么不放开中文：用户名同时是 **JSON 的对象键**、注册页 URL 的参数、
#: 房间座位里的显示名，还要经客户端的 UTF-16 登录包往返。2007 年的客户端
#: 在这条链上从没被非 ASCII 名字验证过，先收紧，将来要放开只改这一个正则。
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{2,16}$")
USERNAME_RULE_TEXT = "用户名只能用 2~16 个英文字母、数字、下划线或连字符"

#: 密码规则：1~32 个可打印字符，不许有控制字符（会把登录包和 JSON 弄坏）。
PASSWORD_MAX_LENGTH = 32
PASSWORD_RULE_TEXT = "密码长度需为 1~32 个字符，且不能包含控制字符"

#: 显示昵称规则：1~16 个字符，中文 / 韩文 / 英文都行，但**只限基本多文种平面**。
#:
#: ★ 「不许有 emoji」不是洁癖，是协议硬约束：`w_wstr()` 写的长度字段是
#: **Python 的字符数**，正文却是 UTF-16LE。BMP 内的字符（含全部汉字）
#: 一个字符恰好两字节，两者对得上；而 emoji 这类补充平面字符在 UTF-16 里
#: 占**两个**码元 —— 长度字段会少算，客户端从这个包往后**整条流都解错位**。
#: 与其在每个组包点去数码元，不如在入口把它们挡掉。
NICKNAME_MAX_LENGTH = 16
NICKNAME_RULE_TEXT = ("显示昵称最多 16 个字符，可以用中文，"
                      "但不能包含表情符号或控制字符")

#: `verify()` 的三态。
AUTH_OK = "ok"
AUTH_NO_SUCH_USER = "no_such_user"
AUTH_BAD_PASSWORD = "bad_password"

#: 给玩家看的中文说明。认证服和注册页都用这一份，两处文案不会走样。
AUTH_MESSAGES = {
    AUTH_OK: "登录成功",
    AUTH_NO_SUCH_USER: "该用户尚未注册，请先在注册页面注册",
    AUTH_BAD_PASSWORD: "密码错误，请重新输入",
}

#: 导出的存档文件里的格式标记。导入时用它认一眼，避免用户传错文件。
SAVE_FORMAT_KEY = "popshot_save"
SAVE_FORMAT_VERSION = 1

#: 存档文件的 schema 版本。1 = V0.1（带 `active_account`）；2 = V0.2（票据制）。
SCHEMA_VERSION = 2


class AccountError(Exception):
    """账号操作失败。``code`` 供调用方分支，``args[0]`` 是给玩家看的中文。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def check_username(username):
    """校验并规范化用户名。不合规就抛 `AccountError`。"""
    username = str(username or "").strip()
    if not USERNAME_PATTERN.match(username):
        raise AccountError("invalid_username", USERNAME_RULE_TEXT)
    return username


def check_nickname(nickname, username=""):
    """校验并规范化**显示昵称**，返回真正要存的那个字符串。

    留空 = 用用户名当昵称（需求原文：「留空时默认昵称为用户名」）。
    首尾空白一律剃掉 —— 游戏里看不出来，却会让「看着一样的两个昵称」
    绕过查重。
    """
    nickname = str(nickname or "").strip()
    if not nickname:
        return str(username or "").strip()
    if len(nickname) > NICKNAME_MAX_LENGTH:
        raise AccountError("invalid_nickname", NICKNAME_RULE_TEXT)
    for ch in nickname:
        # 控制字符会把包和 JSON 弄坏；补充平面字符会让 w_wstr 的长度字段
        # 少算（见 NICKNAME_RULE_TEXT 上面那段），两类都必须挡在门外。
        if ord(ch) < 0x20 or ord(ch) == 0x7f or ord(ch) > 0xffff:
            raise AccountError("invalid_nickname", NICKNAME_RULE_TEXT)
        if 0xd800 <= ord(ch) <= 0xdfff:          # 落单的代理项，同样解不回来
            raise AccountError("invalid_nickname", NICKNAME_RULE_TEXT)
    return nickname


def nickname_key(nickname):
    """昵称查重用的键。

    大小写不敏感（`Alice` 和 `alice` 在屏幕上是两个人、在记忆里是一个人），
    首尾空白已经在 `check_nickname` 里剃过，这里再剃一次防止直接调用。
    """
    return str(nickname or "").strip().casefold()


def check_password(password):
    """校验密码。不合规就抛 `AccountError`。**不做任何变换**（首尾空格也是密码）。"""
    password = str(password if password is not None else "")
    if not 1 <= len(password) <= PASSWORD_MAX_LENGTH:
        raise AccountError("invalid_password", PASSWORD_RULE_TEXT)
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in password):
        raise AccountError("invalid_password", PASSWORD_RULE_TEXT)
    return password

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
        return {"schema_version": SCHEMA_VERSION, "accounts": {}}

    def _read_unlocked(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return self._empty()
        if not isinstance(data, dict):
            raise ValueError(f"账号文件根节点必须是对象: {self.path}")
        # V0.1 的存档带 `active_account`（单活动账号的遗物）。读得进来，
        # 但下次写盘就把它丢掉 —— 身份现在由票据传递（D064）。
        data.pop("active_account", None)
        data["schema_version"] = SCHEMA_VERSION
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

    def realign_levels(self):
        """把存档里所有账号的 `level` 按**当前**曲线重算一遍，返回被改动的清单。

        为什么需要它：`level` 是派生字段（D024），`_merged_account()` 每次读都会
        按当前曲线重算 —— 所以**逻辑上**换曲线之后一切自动正确。但**磁盘上**那个
        数要等该账号下次被写才更新，在此之前 JSON 和实际行为各说各话，
        人去看存档会被误导。V0.2 会话 43 把线性曲线换成二次曲线时，云服上
        已经有一批号涨到了好几百级（§229 / D150），所以要在启动时扫一遍、
        写回去、并**把改了谁说出来**。

        返回 ``[{"username", "old", "new", "experience", "capped"}, ...]``，
        按用户名排序。`capped` = 「按经验推出来本该更高，是被 `LEVEL_MAX` 钳住的」。

        ★ **幂等**：没有任何账号需要改时不写盘、返回空表。所以每次启动都可以跑，
        不需要 schema 版本号，以后有人手工丢一份旧 JSON 进来也会被自动纠正。
        """
        with self._lock:
            data = self._read_unlocked()
            changed = []
            for username in sorted(data["accounts"]):
                raw = data["accounts"][username]
                if not isinstance(raw, dict):
                    continue
                # ★ 旧等级必须从**原始字典**里取。走 `_merged_account()` 的话
                #   等级在读的那一刻就已经被重算掉了，永远看不出差异。
                try:
                    old = int(raw.get("level", 0))
                except (TypeError, ValueError):
                    old = 0
                experience = player_experience(raw)
                new_level = level_for_experience(experience)
                if old == new_level:
                    continue
                raw["level"] = new_level
                changed.append({
                    "username": username,
                    "old": old,
                    "new": new_level,
                    "experience": experience,
                    # 「本该更高、被上限钳住」：只有经验真的够到 LEVEL_MAX 才算。
                    "capped": experience >= experience_for_level(LEVEL_MAX),
                })
            if changed:
                self._write_unlocked(data)
            return changed

    @staticmethod
    def _merged_account(username, raw):
        account = copy.deepcopy(NEW_ACCOUNT_DEFAULTS)
        if isinstance(raw, dict):
            account.update(raw)
        account["display_name"] = account.get("display_name") or username
        # 等级由经验推出（D024）。每次读都校一次，手工编辑过存档也不会让
        # JSON 里的等级和经验各说各话；客户端兼容下限不在这里生效。
        account["level"] = level_for_experience(player_experience(account))
        return account

    # ------------------------------------------------------------------ 注册
    def nickname_owner(self, nickname, data=None):
        """谁在用这个显示昵称？返回它的用户名，没人用就返回 `None`。

        `data` 是已经读出来的存档字典（调用方持锁时传进来，避免重复读盘）。
        """
        key = nickname_key(nickname)
        if not key:
            return None
        if data is None:
            with self._lock:
                data = self._read_unlocked()
        for name, raw in data["accounts"].items():
            # ★ 必须走 `_merged_account`：老存档里 `display_name` 可能是空的，
            #   那时**用户名自己就是昵称**（`_merged_account` 会补上）。
            #   直接读原始字段的话，「叫 bob 的老账号」挡不住新人把昵称起成 bob。
            current = self._merged_account(name, raw)
            if nickname_key(current.get("display_name")) == key:
                return name
        return None

    def register(self, username, password, display_name="", skip_tutorial=False):
        """注册新账号。重名或格式不合规抛 `AccountError`，成功返回账号字典。

        查重和写入在**同一把锁**里完成 —— 注册页可能被两个人同时提交，
        「先查再写」如果拆成两步就会让后一个人静默覆盖前一个人。
        **用户名和显示昵称各查各的**，两条路的提示分开说（需求原文：
        「用户名重复和昵称重复需要分别单独 check」）。

        `skip_tutorial=True` 就把新存档的 `tutorial_completed` 直接置上，
        于是首次登录时 `tutorial_state()` 回 3，客户端不再把人拉进强制教学关
        （§54）。`tutorial_progress` **保持 0** —— 它是「客户端自己上报过什么」
        的保真记录，我们没跑过教程就不该往里编一个值（D094）。

        ★ 这里的默认是 **False**（= 原版行为）。注册页上那个勾选框默认**勾着**，
        但它每次都会把 `skip_tutorial` 显式发上来；默认值只影响直接调 API 的人，
        对他们来说「不说就不改行为」才是对的。
        """
        username = check_username(username)
        password = check_password(password)
        # 格式先于查重：昵称写得不合法时不该让人以为是「被别人占了」。
        nickname = check_nickname(display_name, username)
        with self._lock:
            data = self._read_unlocked()
            if username in data["accounts"]:
                raise AccountError(
                    "duplicate", "该用户名已存在，请在登录界面直接登录")
            owner = self.nickname_owner(nickname, data)
            if owner is not None:
                # ★ 不告诉他是**谁**占的（那等于一个免费的账号枚举接口），
                #   但要说清是昵称重了、不是用户名重了。
                raise AccountError(
                    "duplicate_nickname",
                    f"显示昵称「{nickname}」已经被别人用了，请换一个"
                    + ("。（昵称留空时默认用用户名，这里正是用户名撞上了别人的昵称）"
                       if nickname == username and not str(display_name or "").strip()
                       else "。"))
            account = self._merged_account(username, None)
            account["password"] = password
            account["display_name"] = nickname
            account["tutorial_completed"] = bool(skip_tutorial)
            data["accounts"][username] = account
            self._write_unlocked(data)
            return copy.deepcopy(account)

    # ------------------------------------------------------------ 账号资料修改
    def _require_password_unlocked(self, data, username, password):
        """在调用方已经持有 ``_lock`` 时校验账号密码，失败就抛 ``AccountError``。

        修改密码 / 昵称必须把「验证旧密码」和「写回新值」放在同一个临界区里；
        若先调一次 ``verify()``、再另起一次写盘，两个并发请求可能都拿旧密码通过，
        后写入的那一个还会静默覆盖先写入的结果。
        """
        username = str(username or "").strip()
        password = str(password if password is not None else "")
        raw = data["accounts"].get(username)
        if raw is None:
            raise AccountError("no_such_user", AUTH_MESSAGES[AUTH_NO_SUCH_USER])
        account = self._merged_account(username, raw)
        if str(account.get("password", "")) != password:
            raise AccountError("bad_password", AUTH_MESSAGES[AUTH_BAD_PASSWORD])
        return username, account

    def change_password(self, username, old_password, new_password):
        """验证旧密码后修改密码，返回更新后的账号副本。

        新密码沿用注册时的同一套规则；不强制它必须和旧密码不同，避免凭空增加
        用户没有要求的限制。校验和写盘在同一把锁里完成。
        """
        with self._lock:
            data = self._read_unlocked()
            username, account = self._require_password_unlocked(
                data, username, old_password)
            account["password"] = check_password(new_password)
            data["accounts"][username] = account
            self._write_unlocked(data)
            return copy.deepcopy(account)

    def change_nickname(self, username, old_password, display_name):
        """验证密码后修改显示昵称，返回更新后的账号副本。

        昵称规则和注册时完全一致：剃首尾空白、留空退回用户名、只限 BMP，
        并按大小写不敏感的口径查重。改回自己当前的昵称属于合法的幂等操作。
        """
        with self._lock:
            data = self._read_unlocked()
            username, account = self._require_password_unlocked(
                data, username, old_password)
            nickname = check_nickname(display_name, username)
            owner = self.nickname_owner(nickname, data)
            if owner is not None and owner != username:
                raise AccountError(
                    "duplicate_nickname",
                    f"显示昵称「{nickname}」已经被别人用了，请换一个。")
            account["display_name"] = nickname
            data["accounts"][username] = account
            self._write_unlocked(data)
            return copy.deepcopy(account)

    # ------------------------------------------------------------------ 校验
    def verify(self, username, password):
        """校验用户名密码，返回 ``(状态, 账号或 None)``。

        状态是 `AUTH_OK` / `AUTH_NO_SUCH_USER` / `AUTH_BAD_PASSWORD` 三态之一 ——
        需求要求「不存在」和「密码错」在画面上分开提示，所以这里不能合并成布尔。

        ★ 这里**不做**「未知账号自动建号」。V0.1 的假后台那么干是因为只有一个玩家；
        联机之后自动建号等于谁都能顶别人的名字进来。
        """
        username = str(username or "").strip()
        password = str(password if password is not None else "")
        with self._lock:
            data = self._read_unlocked()
            raw = data["accounts"].get(username)
            if raw is None:
                return AUTH_NO_SUCH_USER, None
            account = self._merged_account(username, raw)
            if str(account.get("password", "")) != password:
                return AUTH_BAD_PASSWORD, None
            return AUTH_OK, account

    def get_account(self, username):
        """按名字取账号，返回 ``(名字, 账号)``；不存在时返回 ``(None, None)``。"""
        username = str(username or "").strip()
        with self._lock:
            data = self._read_unlocked()
            if not username or username not in data["accounts"]:
                return None, None
            return username, self._merged_account(username, data["accounts"][username])

    def has_account(self, username):
        name, _ = self.get_account(username)
        return name is not None

    def usernames(self):
        with self._lock:
            return sorted(self._read_unlocked()["accounts"])

    # -------------------------------------------------------------- 存档转移
    def export_account(self, username):
        """导出一个账号，返回可直接下载的 JSON 字典。

        **包含明文密码** —— 导入到另一台服务器时那边可能还没有这个账号，
        没有密码就没法建。注册页上已经写明这一点。
        """
        name, account = self.get_account(username)
        if name is None:
            raise AccountError("no_such_user", AUTH_MESSAGES[AUTH_NO_SUCH_USER])
        return {
            SAVE_FORMAT_KEY: SAVE_FORMAT_VERSION,
            "username": name,
            "account": account,
        }

    @staticmethod
    def parse_save(payload):
        """把上传的 JSON 拆成 ``(用户名, 账号字段字典)``。格式不对抛 `AccountError`。

        既吃 `export_account` 的完整形状，也吃「直接一个账号对象 + 里面带 username」
        这种被人手改过的形状 —— 玩家会用记事本改存档，别为了格式洁癖把人挡在外面。
        """
        if not isinstance(payload, dict):
            raise AccountError("bad_save", "存档文件的内容必须是一个 JSON 对象")
        raw = payload.get("account")
        username = payload.get("username")
        if not isinstance(raw, dict):
            # 退化形状：整个文件就是账号本身。
            raw = payload
            username = username or payload.get("display_name")
        if not isinstance(raw, dict):
            raise AccountError("bad_save", "存档文件里找不到账号数据")
        try:
            username = check_username(username)
        except AccountError:
            raise AccountError(
                "bad_save",
                f"存档文件里没有可用的用户名（{USERNAME_RULE_TEXT}）") from None
        fields = {key: value for key, value in raw.items()
                  if key in NEW_ACCOUNT_DEFAULTS}
        return username, fields

    def import_account(self, payload, auth_username="", auth_password=""):
        """导入存档。返回 ``(用户名, 是新建还是覆盖)``。

        判定顺序（需求原文）：

        1. 服务器上**没有**这个用户 -> 直接新增，不要求填用户名密码；
           密码取存档里的，存档没带就要求表单里填。
        2. 服务器上**有** -> 必须用表单里的用户名密码校验通过才覆盖。
        3. **覆盖时，存档里没有的字段一律重置为默认值** —— 这是「导入」不是「合并」，
           留着旧值会让两台服务器的存档永远对不齐。
        4. 密码不对 -> 拒绝并提示。

        整个判定和写入在同一把锁里，避免「查的时候没有、写的时候有了」。
        """
        username, fields = self.parse_save(payload)
        auth_username = str(auth_username or "").strip()
        auth_password = str(auth_password if auth_password is not None else "")
        with self._lock:
            data = self._read_unlocked()
            existing = data["accounts"].get(username)
            if existing is not None:
                # ★ 「一个字都没填」和「填了但不对」要分开说：前者是提示他去填，
                #   后者是明确告诉他填错了。合成一句「请填入用户名和密码」的话，
                #   打错密码的人会以为自己没填，对着已经填好的框发呆。
                if not auth_username and not auth_password:
                    raise AccountError(
                        "auth_required",
                        f"服务器上已经有账号「{username}」了，"
                        "请在上面填入该账号的用户名和密码后再上传")
                current = self._merged_account(username, existing)
                if (auth_username != username
                        or str(current.get("password", "")) != auth_password):
                    raise AccountError(
                        "bad_password",
                        f"用户名或密码错误：这份存档属于账号「{username}」，"
                        "请填入该账号的用户名和密码")
            # ★ 从默认值起手，只把存档里有的字段盖上去 = 缺的字段自动回默认值。
            account = self._merged_account(username, None)
            account.update(fields)
            account["display_name"] = (str(account.get("display_name") or "").strip()
                                       or username)
            password = str(account.get("password", ""))
            if not password:
                # 存档里没带密码：新建时用表单里填的；覆盖时沿用服务器上的旧密码。
                password = auth_password if existing is None else str(
                    self._merged_account(username, existing).get("password", ""))
            try:
                account["password"] = check_password(password)
            except AccountError:
                raise AccountError(
                    "password_required",
                    "存档文件里没有密码，请在上面的密码框里填一个（"
                    + PASSWORD_RULE_TEXT + "）") from None
            # ★ 手改的等级要真的生效，见 `experience_for_import` 的说明。
            #   `fields` = 存档里**真正写了**的字段，用来分清「他想要 1 级」
            #   和「他压根没写 level」。
            account["experience"] = experience_for_import(account, fields)
            account["level"] = level_for_experience(account["experience"])
            data["accounts"][username] = account
            self._write_unlocked(data)
            return username, ("created" if existing is None else "replaced")

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


#: 升一级需要的经验 = `EXPERIENCE_STEP × 当前等级`（二次曲线）。
#: 于是「到达 L 级的累计经验」= `EXPERIENCE_STEP · L · (L-1) / 2` = `50·L·(L-1)`。
#:
#: ★ **这条曲线是我们自己定的，不是从客户端逆出来的。**
#: 客户端只按 `gspEndGame` 下发的三个绝对值（总经验 / 下一级所需 / 本级起点）
#: 做减法算进度条（FINDINGS §94），它不知道也不关心曲线长什么样，
#: 所以这里怎么定都不会让客户端出错。
#:
#: V0.2 会话 43 之前是**线性**的（每级恒 100），配上「一局给几百上千经验」
#: 一局能跳十几级 —— 用户实机报的问题（§229 / D150）。
EXPERIENCE_STEP = 100

#: 兼容别名。旧代码/旧测试把它当「等差」用过，留着只为不炸 import；
#: **新代码一律用 `EXPERIENCE_STEP`**，因为曲线已经不是等差了。
EXPERIENCE_PER_LEVEL = EXPERIENCE_STEP

#: ★ 等级上限 = 徽章图 `Images/General/LevelMark.smf` 的帧数。
#: 那张图头里写着 `frames = 0x3c = 60`（60 张 21×21 精灵），玩家列表的
#: `UserSnap+0x0c` 取第 `等级-1` 帧，客户端在 `0x441fc0` 自己也钳到 1..60
#: —— 超过 60 会取到别的图的像素。原版实际内容只铺到 21 级
#: （`map.ini` 的 `MinLevel` 最高 21），60 是**物理上限**（§229）。
LEVEL_MAX = 60


def experience_for_level(level):
    """到达 `level` 级所需的**累计**经验。1 级是 0。

    钳在 `[1, LEVEL_MAX + 1]`：多出来的那一级是给 `experience_bounds()`
    在满级时当分母用的（见那边的说明）。
    """
    try:
        level = int(level)
    except (TypeError, ValueError):
        level = 1
    level = max(1, min(LEVEL_MAX + 1, level))
    return EXPERIENCE_STEP * (level - 1) * level // 2


#: 预计算的累计经验表，下标 = 等级（0 号位占位）。`bisect` 反查等级用它，
#: 比每次现算稳，也让曲线只有一处定义。
_LEVEL_THRESHOLDS = [0] + [experience_for_level(lv)
                           for lv in range(1, LEVEL_MAX + 2)]


def level_for_experience(experience):
    """总经验 -> 等级（1 起步，**钳到 `LEVEL_MAX`**）。

    ★ 这里**不加** `MINIMUM_PLAYER_LEVEL` 的下限：存档里记的是真实等级，
    `experience_bounds()` 还要拿它算经验条的两端。抬等级只发生在
    「往客户端发」的那一刻，见 `player_level()`。
    """
    try:
        experience = max(0, int(experience))
    except (TypeError, ValueError):
        experience = 0
    # bisect_right - 1：找最后一个「累计经验 <= 当前经验」的等级。
    # 表里 1 级门槛是 0，所以经验 0 也一定落在 1 级。
    level = bisect.bisect_right(_LEVEL_THRESHOLDS, experience, 1,
                                LEVEL_MAX + 1) - 1
    return max(1, min(LEVEL_MAX, level))


def experience_for_import(account, provided=None):
    """导入存档时该存多少总经验 —— ★ **等级说了算**。

    背景：等级在服务端是由经验推出来的（D024，`_merged_account` 每次读都重算一遍），
    存档里的 `level` 只是个派生字段。所以玩家把导出的 JSON 里的 `level` 从 1 改成 5
    再传上来，如果不做点什么，什么都不会发生 —— 经验还是 0，一读回来等级又变回 1。

    规则（只在导入这一刻生效，D151）：

    * `level` **没出现在存档文件里**（`provided` 里没有它）-> **以经验为准**。
      ★ 这一条不能省：`import_account` 是「从默认值起手再把存档里有的字段盖上去」，
      默认等级是 1 —— 少了这一条，一份只写了 `experience` 的手写存档会被当成
      「他想要 1 级」，经验当场清零；
    * `level` 读不出数（是字符串 / 是负数 / 是 0）-> 同上，**以经验为准**；
    * `level` 和经验推出来的等级**一致** -> ★ **原样保留经验**；
    * 两者**矛盾** -> 经验重算成 `experience_for_level(level)`，等级真的变成他写的那个数。

    ★ 中间那条不是优化，是**必须**的：导出的存档里两个字段本来就自洽，
    无条件按等级重算的话，一次「导出 → 原样导回」就会把玩家**本级内已经攒的
    那部分经验**抹掉（5 级、总经验 1200 会被打回本级起点 1000，白丢 200）。
    只有真的矛盾时才认 `level`。

    `level` 先钳到 `[1, LEVEL_MAX]`：手写 `level: 999` 只会得到 60 级，
    不会算出一个天文数字的经验。

    `provided` = **存档文件里真正出现过的字段名**（`parse_save` 的第二个返回值）。
    不传就退化成「假定 level 写了」，保持老调用方能用。
    """
    experience = player_experience(account)
    if provided is not None and "level" not in provided:
        return experience
    try:
        wanted = int(account.get("level", 0))
    except (TypeError, ValueError, AttributeError):
        return experience
    if wanted <= 0:
        # 0 / 负数 = 「这个字段坏了或者没填」，不是「想降到 0 级」。
        return experience
    wanted = min(LEVEL_MAX, wanted)
    if wanted == level_for_experience(experience):
        return experience
    return experience_for_level(wanted)


def experience_bounds(experience):
    """总经验 -> ``(本级起始总经验, 下一级所需总经验)``。

    对应 `gspEndGame` 的 `pkt+0x18` 和 `pkt+0x14`。客户端算的是
    ``(总经验 - 本级起点) / (下一级所需 - 本级起点)``，所以这两个必须是
    **绝对累计值**，不是「本级内的差值」。

    ★ **满级时给的是 `(total(60), total(61))`**，不是两个相等的数 ——
    客户端那个除法的分母是「下一级 - 本级起点」，两个数一样就是除以 0，
    那是没验过的行为。多留一级当分母，进度条只会停在某个位置不动。
    """
    level = level_for_experience(experience)
    return experience_for_level(level), experience_for_level(level + 1)


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
    """**下发给客户端**的账号等级，供 gspRepLogin 的第 1 个业务 int32 使用（§63）。

    客户端把它存进全局 `0x72e338`，用来过滤「建立房间」里四个下拉框的条目
    （`0x436833` 起）以及天梯标签页的等级门槛（`0x43b676` 要求 >=6）。
    闯关关卡记录的要求等级是 1，所以等级 0 会让任务下拉框整个空掉。

    ★ **这里把等级抬到 `MINIMUM_PLAYER_LEVEL`（=4）以上 = 解除对战等级限制。**
    大厅点「对战」判的是 `[0x72e338] == 1`（恰好等于 1，V0.1 §83），
    房间里点开始判的是房主座位里的同一个数（V0.1 §77）；中国区房主选生存
    模式还会判 `< 4`，不满足就把模式强制改成夺分（V0.2 §203）。存档里仍是
    真实等级，只有下发这一刻抬高 —— 这样经验曲线和经验条两端
    （`experience_bounds`）都不受影响。

    等级 0（没有账号 / 存档字段坏了）仍然照实返回 0：那是「取不到账号」的信号，
    不该被下限悄悄改成 4。
    """
    if not account:
        return 0
    try:
        level = int(account.get("level", 0))
    except (TypeError, ValueError):
        return 0
    if level <= 0:
        return 0
    return max(MINIMUM_PLAYER_LEVEL, level)


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
