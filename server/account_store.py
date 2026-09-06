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

#: 物品表（V0.3商店 M1）。存档层要它只为回答两个**存储完整性**问题：
#: 「这个 id 客户端认不认识」和「这两件抢不抢同一个槽」。
#: 业务规则（上没上架 / 等级够不够 / 角色对不对）不在这儿，在 `shop.py`。
#: 它只依赖 `json` + `os`，不会绕回来 import 本模块。
import shopdata


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
    #: 仓库里的持有物 `{itemId 字符串: {"count": 数量, "expires": 到期或 None}}`
    #: （V0.3商店 M2）。键写成字符串是因为 JSON 的对象键只能是字符串。
    #:
    #: `expires` 现在**恒为 None**（本版只做永久物品，见 PLAN「本版不做」）。
    #: 留着这个键是因为原版真有期限制装备（`ShopItem.ini` 的 `5x` 段），
    #: 将来要做时不用再动一次线上存档的结构（铁律 11）。
    #:
    #: ★ **不含商城角色物品** —— 那一批是从 `owned_characters` 派生的
    #: （`character_item_ids()`），两条路各管各的。
    "inventory": {},
    #: 当前穿在身上的 itemId **列表**（不是「部位 -> id」字典，D6）。
    #: `0x030b gspSlotEquippedList` 的线格式本来就是一个 id 列表，客户端拿它
    #: 直接建 `EquipmentEx`；而套装的 `PartFlag` 是组合值（全身 = 31），
    #: 字典模型下一件套装要占好几个键。
    #:
    #: 服务端保证两条不变式：**两两 `part_flag` 按位与为 0**（不抢槽）
    #: 且**每件都在 `inventory` 里**（不能穿没有的东西）。
    "equipped": [],
    #: 合成材料的存量 `{itemId 字符串: 数量}`。★ 和 `inventory` 分开存（D11）。
    "materials": {},
}

#: 客户端认为「教程已完成」的最小值。大厅 `0x43b357` 是 `cmp eax,3 / jge`（§54）。
TUTORIAL_DONE_STATE = 3

#: ★★ 这里**曾经**有个 `MINIMUM_PLAYER_LEVEL = 4`（V0.2 D120）：低等级账号
#: 下发给客户端的等级被抬到 4，用来一次顶开「对战频道 / 生存模式 / 5-6 人房」
#: 那几道原版等级门。**V0.3商店 D22 把它删了**，理由是它和商店对不上 ——
#: 客户端的显示等级和门槛判定读的是**同一个**全局 `[0x72e338]`，抬高它就等于
#: 让 1 级号既显示成 4 级，又能买 4 级才卖的武器（购买判定走服务端，
#: 但服务端拿的也是这个抬过的值）。
#:
#: 现在下发的是**真实等级**，那几道门改由 `hook/bshook.c` 的
#: `try_patch_player_level_gate()` 直接 patch 掉（7 处，见那边的注释）。

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
#:
#: ★ V0.3 商店给账号加的三个字段**没有**把它推到 3：新字段走
#: 「读盘时按默认值补齐」，天然幂等，不需要版本号驱动的一次性迁移（D5）。
SCHEMA_VERSION = 2

#: 管理页（`/admin`）的管理员账号表，和 `accounts` 在存档里平级。
#: 值的形状是 `{"password": "明文", "role": "system"|"operator"}`
#: —— 口令和玩家账号一个口径（D3 / 铁律 9）。
ADMIN_ACCOUNTS_KEY = "admin_accounts"

#: 管理员的两种权限（用户 2026-09-06 拍板，D34）。
#:
#: * `system` **系统管理员** —— 管理页所有标签页都能进；
#: * `operator` **运营** —— 只能进 物品库 / 商店货架 / 合成配方 / 材料掉落
#:   这四个配置页，看不到「玩家资料」和「管理员账号」。
ADMIN_ROLE_SYSTEM = "system"
ADMIN_ROLE_OPERATOR = "operator"
ADMIN_ROLES = (ADMIN_ROLE_SYSTEM, ADMIN_ROLE_OPERATOR)
ADMIN_ROLE_ZH = {ADMIN_ROLE_SYSTEM: "系统管理员", ADMIN_ROLE_OPERATOR: "运营"}

#: 首次生成的默认管理员。★ **弱密码是明知故犯**：管理页公网可达，
#: 所以配套的补偿是「登录按 IP 限速」+「页面上提示立刻改掉」（D3）。
DEFAULT_ADMIN_NAME = "admin"
DEFAULT_ADMIN_PASSWORD = "Admin123"


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

    def ensure_item_fields(self):
        """把存档里的物品三件套补齐 / 洗干净，顺带保证有一个管理员。

        照 `realign_levels()` 的路子（D5）：**幂等** —— 没有任何东西需要改时
        不写盘，所以每次启动都可以跑，不需要 schema 版本号；以后有人手工丢一份
        旧 JSON 进来也会被自动补上。

        管四件事：

        1. 老账号缺 `inventory` / `equipped` / `materials` 时补空的。
           逻辑上 `_merged_account()` 每次读都会补，但**磁盘上**那三个键要等
           该账号下次被写才出现 —— 中间人去翻存档会以为功能根本没生效。
        2. 洗掉脏条目：数量 <= 0 的、id 解析不出来的、客户端不认识的。
        3. 让 `equipped` 满足两条不变式（不抢槽 + 必须是自己的）。
           `shop_items.json` 换代（某件装备被移出中文版）之后靠它收敛。
        4. 顶层的 `admin_accounts`：**键不存在**时建一个默认管理员。
           键在但是空字典 = 用户主动关掉了管理页，**不碰**（D13）。

        返回 ``{"accounts": [{"username", "notes"}, ...],
                "admin_created": 名字 or None, "admin_broken": bool}``。
        """
        with self._lock:
            data = self._read_unlocked()
            changed = []
            for username in sorted(data["accounts"]):
                raw = data["accounts"][username]
                if not isinstance(raw, dict):
                    continue
                # ★ 必须从**原始字典**算。走 `_merged_account()` 的话三个键
                #   在读的那一刻就已经被补上了，永远看不出磁盘上缺什么。
                inventory, equipped, materials, notes = normalize_item_fields(raw)
                if (raw.get("inventory") == inventory
                        and raw.get("equipped") == equipped
                        and raw.get("materials") == materials):
                    continue
                raw["inventory"] = inventory
                raw["equipped"] = equipped
                raw["materials"] = materials
                changed.append({
                    "username": username,
                    # 形状变了但没丢东西（比如手写的 `"1010015": 2` 简写）
                    # 也要说一声，否则日志里会出现「改了谁但没说改了什么」。
                    "notes": notes or ["规范化了物品字段的写法"],
                })

            admin_created = None
            admin_broken = False
            if ADMIN_ACCOUNTS_KEY not in data:
                data[ADMIN_ACCOUNTS_KEY] = {
                    DEFAULT_ADMIN_NAME: {"password": DEFAULT_ADMIN_PASSWORD,
                                         "role": ADMIN_ROLE_SYSTEM}}
                admin_created = DEFAULT_ADMIN_NAME
            elif not isinstance(data[ADMIN_ACCOUNTS_KEY], dict):
                # 手改坏了。★ **不自动修**：这一格里可能还留着用户自己加的
                # 管理员，盖成默认值等于把它们抹了。报一声就够 —— 失败模式是
                # 「谁都登不进管理页」，玩家一点感觉都没有（铁律 11）。
                admin_broken = True

            if changed or admin_created:
                self._write_unlocked(data)
            return {"accounts": changed, "admin_created": admin_created,
                    "admin_broken": admin_broken}

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
            # 仓库 / 装备 / 材料照样跟着存档走（`parse_save` 按
            # `NEW_ACCOUNT_DEFAULTS` 过滤，加字段就自动带上），但要**当场洗一遍**
            # —— 上传的文件是玩家用记事本改过的，脏条目不该落到磁盘上。
            (account["inventory"], account["equipped"],
             account["materials"], _notes) = normalize_item_fields(account)
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

    # ------------------------------------------------- 仓库 / 装备 / 材料
    def _account_unlocked(self, data, username):
        """持锁状态下取一个可改的账号视图。账号不存在就 `KeyError`。"""
        if username not in data["accounts"]:
            raise KeyError(username)
        return self._merged_account(username, data["accounts"][username])

    def spend_money(self, username, amount):
        """扣金币，返回更新后的账号。余额不够就抛 `AccountError`，**一个字节都不写**。

        ★ 查余额和扣款必须在**同一把锁**里。拆成「先 `get_account()` 看够不够、
        再 `spend_money()` 扣」的话，两笔购买挤在一起会双双看到余额够，
        扣成负数 —— 这是商店最经典的一个洞。
        """
        amount = int(amount)
        if amount < 0:
            raise AccountError("invalid_amount", "扣款金额不能是负数")
        with self._lock:
            data = self._read_unlocked()
            account = self._account_unlocked(data, username)
            balance = player_money(account)
            if balance < amount:
                raise AccountError(
                    "not_enough_money",
                    f"金币不足：需要 {amount}，现有 {balance}")
            if amount == 0:              # 白送的东西，没必要重写一遍生产数据
                return copy.deepcopy(account)
            account["money"] = balance - amount
            data["accounts"][username] = account
            self._write_unlocked(data)
            return copy.deepcopy(account)

    def add_materials(self, username, materials):
        """给合成材料，返回 ``(更新后的账号, 被跳过的 id 列表)``。

        `materials` = `{itemId: 数量}`；数量 <= 0 的条目直接忽略（不算跳过）。

        ★ **为什么这里跳过而不抛**（和 `add_item` 恰好相反）：它的调用点是
        结算发奖，规则来自用户手改的 `drops.json`。一条配错的掉落规则不该让
        整局的结算包发不出去 —— 玩家看到的会是「卡在结算界面」，
        比少拿一个材料难查十倍。跳过的 id 原样返回，由调用方打进日志。
        """
        wanted = {}
        skipped = []
        for key, value in (materials or {}).items():
            try:
                item_id, count = int(key), int(value)
            except (TypeError, ValueError):
                skipped.append(key)
                continue
            if count <= 0:
                continue
            if not shopdata.ownable(item_id):
                # 客户端表里没有 `[Item-]` 节 ⇒ 结算界面那一栏画不出图标
                # （FINDINGS §3 约束 3），仓库里也查不到（§11）。
                skipped.append(item_id)
                continue
            wanted[item_id] = wanted.get(item_id, 0) + count
        with self._lock:
            data = self._read_unlocked()
            account = self._account_unlocked(data, username)
            if not wanted:
                return copy.deepcopy(account), skipped
            records = _material_records(account)
            for item_id, count in wanted.items():
                records[item_id] = records.get(item_id, 0) + count
            account["materials"] = {str(i): records[i] for i in sorted(records)}
            data["accounts"][username] = account
            self._write_unlocked(data)
            return copy.deepcopy(account), skipped

    def consume_materials(self, username, materials):
        """扣合成材料，返回更新后的账号。

        ★ **任何一种不够就整笔失败**（抛 `AccountError`），一个字节都不写。
        合成是「材料换装备」的一次性交易，扣一半留一半等于凭空吃掉玩家的材料。
        和 `spend_money` 一样，查和扣在同一把锁里。
        """
        wanted = {}
        for key, value in (materials or {}).items():
            try:
                item_id, count = int(key), int(value)
            except (TypeError, ValueError):
                raise AccountError(
                    "invalid_material", f"材料条目不合法: {key!r}={value!r}") from None
            if count < 0:
                raise AccountError("invalid_material", "材料数量不能是负数")
            if count:
                wanted[item_id] = wanted.get(item_id, 0) + count
        with self._lock:
            data = self._read_unlocked()
            account = self._account_unlocked(data, username)
            if not wanted:
                return copy.deepcopy(account)
            records = _material_records(account)
            short = [(i, n, records.get(i, 0))
                     for i, n in sorted(wanted.items()) if records.get(i, 0) < n]
            if short:
                detail = "、".join(f"{i} 需要 {n} 现有 {have}"
                                  for i, n, have in short)
                raise AccountError("not_enough_materials", f"材料不足：{detail}")
            for item_id, count in wanted.items():
                left = records[item_id] - count
                if left > 0:
                    records[item_id] = left
                else:
                    del records[item_id]        # 用光了就把这一格删掉，别留 0
            account["materials"] = {str(i): records[i] for i in sorted(records)}
            data["accounts"][username] = account
            self._write_unlocked(data)
            return copy.deepcopy(account)

    def add_item(self, username, item_id, count=1, expires=None):
        """把一件东西放进仓库，返回更新后的账号。

        ★ **客户端不认识的 id 一律拒绝**（`shopdata.ownable`）：只有货架条目、
        进不了背包的那 226 件塞进去，仓库里就是一个空格子（FINDINGS §11）。

        ★ 和 `add_materials` 相反，这里**抛异常**：调用点是「买」和「合成」
        这种玩家主动发起的单笔交易，请求处理器本来就要回一个失败码；
        静默跳过会变成「钱扣了东西没有」。

        已经有的再拿一件就把 `count` 累加。**「装备不能重复持有」是业务规则，
        不在这一层** —— 那条由 `shop.py` 在扣钱之前用 `has_item()` 判
        （原版失败文案 `이미 소지하고 있습니다`，FINDINGS §7）。
        """
        item_id = int(item_id)
        count = int(count)
        if count <= 0:
            raise AccountError("invalid_count", "数量必须是正数")
        if not shopdata.ownable(item_id):
            raise AccountError(
                "unknown_item",
                f"物品 {item_id} 不在客户端认得的物品表里，不能发给玩家")
        with self._lock:
            data = self._read_unlocked()
            account = self._account_unlocked(data, username)
            records = _inventory_records(account)
            entry = records.get(item_id)
            if entry is None:
                records[item_id] = {"count": count, "expires": expires}
            else:
                entry["count"] += count
                # 期限制物品的「续期」本版不做（PLAN「本版不做」），所以这里
                # 不动已有的 `expires` —— 免得买第二件反而把期限改短。
            account["inventory"] = {str(i): dict(records[i])
                                    for i in sorted(records)}
            data["accounts"][username] = account
            self._write_unlocked(data)
            return copy.deepcopy(account)

    def compose_item(self, username, item_id, cost=0, materials=None,
                     expires=None):
        """合成：**一把锁里**扣金币 + 扣材料 + 入库，返回更新后的账号。

        ★ **为什么不能拿现成的三个方法拼**（`spend_money` + `consume_materials`
        + `add_item`）：那是三次「读盘 → 改 → 写盘」。中间崩一次，玩家的金币
        和材料就没了、东西没到手 —— 铁律 11 说的「崩在中间老数据还在不在」
        问的正是这个。合成是一次**原子交易**，要么三样一起成，要么一个字节
        都不写。

        ★ 校验也全在锁里重做一遍。`shop.check_compose()` 那一轮是为了**挑
        错误码**（客户端只认 4 种说法），不是并发保护 —— 同一个号开两个客户端
        各点一次，两边都会看到「材料够」。真正说了算的是这里。

        `materials` = `{itemId: 数量}`。失败一律抛 `AccountError`，
        `code` 和 `shop.COMPOSE_*` 那几个原因字符串对得上，调用方直接拿去查码表。
        """
        item_id = int(item_id)
        cost = int(cost)
        if cost < 0:
            raise AccountError("invalid_amount", "合成花费不能是负数")
        if not shopdata.ownable(item_id):
            raise AccountError(
                "unknown_item",
                f"物品 {item_id} 不在客户端认得的物品表里，不能发给玩家")
        wanted = {}
        for key, value in (materials or {}).items():
            try:
                material_id, count = int(key), int(value)
            except (TypeError, ValueError):
                raise AccountError(
                    "invalid_material",
                    f"材料条目不合法: {key!r}={value!r}") from None
            if count < 0:
                raise AccountError("invalid_material", "材料数量不能是负数")
            if count:
                wanted[material_id] = wanted.get(material_id, 0) + count
        with self._lock:
            data = self._read_unlocked()
            account = self._account_unlocked(data, username)
            # ① 已经有了就不能再合（原版失败文案 `이미 소지하고 있습니다`）。
            if has_item(account, item_id):
                raise AccountError("already_owned",
                                   f"仓库里已经有 {item_id} 了")
            # ② 材料。★ 先报材料再报金币 —— 和 `shop.check_compose()` 同序，
            #    免得两层校验挑出不一样的原因码。
            records = _material_records(account)
            short = [(i, n, records.get(i, 0))
                     for i, n in sorted(wanted.items()) if records.get(i, 0) < n]
            if short:
                detail = "、".join(f"{i} 需要 {n} 现有 {have}"
                                  for i, n, have in short)
                raise AccountError("not_enough_materials", f"材料不足：{detail}")
            # ③ 金币。
            balance = player_money(account)
            if balance < cost:
                raise AccountError(
                    "not_enough_money",
                    f"金币不足：需要 {cost}，现有 {balance}")
            for material_id, count in wanted.items():
                left = records[material_id] - count
                if left > 0:
                    records[material_id] = left
                else:
                    del records[material_id]      # 用光了就把这一格删掉
            inventory = _inventory_records(account)
            inventory[item_id] = {"count": 1, "expires": expires}
            account["materials"] = {str(i): records[i] for i in sorted(records)}
            account["inventory"] = {str(i): dict(inventory[i])
                                    for i in sorted(inventory)}
            account["money"] = balance - cost
            data["accounts"][username] = account
            self._write_unlocked(data)
            return copy.deepcopy(account)

    def set_equipped(self, username, item_ids):
        """整套换装，返回 ``(更新后的账号, 被丢掉的 id 列表)``。

        ★ **顺序有意义**：`shopdata.resolve_equipped()` 是**先到先得**，
        所以调用方要把「玩家刚点的那件」放在最前面 —— 换装于是天然表现为
        「新的顶掉旧的」，不用在每个调用点重写一遍「先找出同槽的再卸下」。

        丢掉的有两类，都在 `dropped` 里：抢了槽的、和不在仓库里的。
        """
        with self._lock:
            data = self._read_unlocked()
            account = self._account_unlocked(data, username)
            wanted = []
            for value in item_ids or ():
                try:
                    wanted.append(int(value))
                except (TypeError, ValueError):
                    continue
            kept, dropped = shopdata.resolve_equipped(wanted)
            owned = _inventory_records(account)
            final = []
            for item_id in kept:
                if item_id in owned:
                    final.append(item_id)
                else:
                    dropped.append(item_id)
            # 和磁盘上那份**原样**比：一样就不写（少一次动生产数据的机会），
            # 不一样就顺手把手改留下的脏条目一起洗掉。
            if account.get("equipped") == final:
                return copy.deepcopy(account), dropped
            account["equipped"] = final
            data["accounts"][username] = account
            self._write_unlocked(data)
            return copy.deepcopy(account), dropped

    def equip_item(self, username, item_id):
        """穿上一件，返回 ``(更新后的账号, 被顶下来的 id 列表)``。

        把它排在最前面交给 `set_equipped()`，抢同一个槽的旧装备就被自动顶掉。
        锁是 `RLock`，所以这里读完再调 `set_equipped()` 全程握着同一把锁。
        """
        item_id = int(item_id)
        with self._lock:
            data = self._read_unlocked()
            account = self._account_unlocked(data, username)
            rest = [i for i in equipped_items(account) if i != item_id]
            return self.set_equipped(username, [item_id] + rest)

    def unequip_item(self, username, item_id):
        """脱下一件，返回 ``(更新后的账号, 被丢掉的 id 列表)``。没穿着就什么都不做。"""
        item_id = int(item_id)
        with self._lock:
            data = self._read_unlocked()
            account = self._account_unlocked(data, username)
            rest = [i for i in equipped_items(account) if i != item_id]
            return self.set_equipped(username, rest)

    # ------------------------------------------------- 管理页的玩家信息修改
    def search_accounts(self, query="", limit=None, offset=0):
        """按**用户名或昵称**找账号，返回 ``([(用户名, 账号字典), …], 命中总数)``。

        大小写不敏感的**子串**匹配，两边任意一边命中就算（需求原文：
        「可以通过昵称或用户名来查找具体用户」）。查询串留空 = 全部。

        ★ 昵称走 `nickname_key()` 归一化，和注册时的查重口径一致 ——
        否则「全角空格」「大小写」这类差异会让管理员搜不到刚注册的人。

        ★ **总数是「命中多少」，不是「返回多少」** —— 管理页靠它算页数。
        所以这里先把命中的数完，再按 `offset`/`limit` 切一页出来；
        提前 `break` 的话页码就没法算了。账号是全量读进内存的，多走一遍
        字符串比较的代价远小于「翻到第 3 页才发现没有第 3 页」。
        """
        needle = str(query or "").strip().lower()
        key_needle = nickname_key(query)
        hits = []
        with self._lock:
            data = self._read_unlocked()
            for username in sorted(data["accounts"]):
                account = self._merged_account(username,
                                               data["accounts"][username])
                if needle:
                    nickname = str(account.get("display_name") or "")
                    if (needle not in username.lower()
                            and needle not in nickname.lower()
                            and not (key_needle
                                     and key_needle in nickname_key(nickname))):
                        continue
                hits.append((username, account))
        offset = max(0, int(offset or 0))
        page = hits[offset:] if limit is None else hits[offset:offset + int(limit)]
        return page, len(hits)

    def admin_update_account(self, username, level=None, money=None,
                             materials=None, inventory=None):
        """管理页改一个玩家的存档，返回 ``(更新后的账号, 改了什么的清单)``。

        **只动传进来的那几项**，其余一个字节不碰（铁律 11：线上数据是生产数据）。
        四项都是 `None` 就直接把账号读回来，一次盘都不写。

        | 参数 | 语义 |
        |---|---|
        | `level` | 目标等级，钳到 `[1, LEVEL_MAX]`。★ **改的其实是经验** —— |
        | | 等级是派生字段（D024），所以按 `experience_for_level()` 落到该级 |
        | | 起点。已经是这一级就**原样不动**，免得把本级内攒的经验抹掉 |
        | | （和 `experience_for_import` 中间那条同一个道理）。 |
        | `money` | 金币**绝对值**，钳到 >= 0 |
        | `materials` | `{itemId: 数量}` 的**增量补丁**：只动列出来的这些 id， |
        | | 数量 <= 0 = 把这一格删掉。不是全量替换 |
        | `inventory` | 同上，作用在仓库上。数量 <= 0 = 删掉，**顺带脱下** |
        | | —— 不能穿着一件仓库里没有的东西（`set_equipped` 的不变式） |

        ★ 「哪些物品**允许**改」不在这一层 —— 那是业务规则，在
        `web/admin.py` 里（商店上架的东西只能靠买，用户 2026-09-05 拍板）。
        这里只管「id 客户端认不认识」这条存储完整性。
        """
        changes = []
        with self._lock:
            data = self._read_unlocked()
            account = self._account_unlocked(data, username)
            if level is not None:
                wanted = max(1, min(LEVEL_MAX, int(level)))
                if wanted != level_for_experience(player_experience(account)):
                    before = account["level"]
                    account["experience"] = experience_for_level(wanted)
                    account["level"] = wanted
                    changes.append(f"等级 {before} -> {wanted}")
            if money is not None:
                wanted = max(0, int(money))
                if wanted != player_money(account):
                    changes.append(f"金币 {player_money(account)} -> {wanted}")
                    account["money"] = wanted
            if materials:
                records = _material_records(account)
                for key, value in materials.items():
                    item_id, count = int(key), max(0, int(value))
                    if count and not shopdata.ownable(item_id):
                        raise AccountError(
                            "unknown_item",
                            f"物品 {item_id} 不在客户端认得的物品表里，不能发给玩家")
                    was = records.get(item_id, 0)
                    if count == was:
                        continue
                    if count:
                        records[item_id] = count
                    else:
                        records.pop(item_id, None)
                    changes.append(f"材料 {item_id} ×{was} -> ×{count}")
                account["materials"] = {str(i): records[i]
                                        for i in sorted(records)}
            if inventory:
                records = _inventory_records(account)
                for key, value in inventory.items():
                    item_id, count = int(key), max(0, int(value))
                    if count and not shopdata.ownable(item_id):
                        raise AccountError(
                            "unknown_item",
                            f"物品 {item_id} 不在客户端认得的物品表里，不能发给玩家")
                    was = records.get(item_id, {}).get("count", 0)
                    if count == was:
                        continue
                    if count:
                        entry = records.get(item_id)
                        if entry is None:
                            records[item_id] = {"count": count, "expires": None}
                        else:
                            entry["count"] = count
                    else:
                        records.pop(item_id, None)
                    changes.append(f"物品 {item_id} ×{was} -> ×{count}")
                account["inventory"] = {str(i): dict(records[i])
                                        for i in sorted(records)}
                # 删干净的那几件如果正穿在身上，必须一起脱下来 ——
                # 「穿着的每一件都在仓库里」是 `set_equipped()` 的不变式，
                # 破了它以后每一次换装都会把这件悄悄丢掉，查起来莫名其妙。
                kept = [i for i in equipped_items(account) if i in records]
                if kept != list(equipped_items(account)):
                    account["equipped"] = kept
                    changes.append("顺带脱下了已经不在仓库里的装备")
            if changes:
                data["accounts"][username] = account
                self._write_unlocked(data)
            return copy.deepcopy(account), changes

    # ----------------------------------------------------------- 管理员
    def _admin_table_unlocked(self, data):
        """持锁状态下取可改的管理员表。**手改坏了就拒绝改，绝不覆盖**
        —— 那一格里可能还留着用户自己加的管理员。"""
        raw = data.get(ADMIN_ACCOUNTS_KEY)
        if raw is None:
            raw = {}
            data[ADMIN_ACCOUNTS_KEY] = raw
        if not isinstance(raw, dict):
            raise AccountError(
                "admin_table_broken",
                f"存档里的 {ADMIN_ACCOUNTS_KEY} 不是一个对象，请先手工改回来")
        return raw

    def admin_names(self):
        """全部管理员的名字（已排序）。表坏了就当一个都没有。"""
        with self._lock:
            return sorted(admin_accounts(self._read_unlocked()))

    def admin_list(self):
        """`[{"name", "role"}, …]`（按名字排序）—— 管理员账号页用它画表。"""
        with self._lock:
            table = admin_accounts(self._read_unlocked())
        return [{"name": name, "role": admin_role_of(table[name])}
                for name in sorted(table)]

    def admin_role(self, name):
        """这个管理员是什么权限；没有这个人返回 `None`。

        ★ **每一发请求都现查**，不缓存进会话：改了权限要**立刻**生效，
        不该等对方重新登录（那期间他手里的令牌还带着旧权限）。
        """
        with self._lock:
            table = admin_accounts(self._read_unlocked())
        entry = table.get(str(name or "").strip())
        return None if entry is None else admin_role_of(entry)

    @staticmethod
    def _check_role(role):
        role = str(role or "").strip().lower()
        if role not in ADMIN_ROLES:
            raise AccountError(
                "bad_role",
                "权限只能是「%s」或「%s」" % (ADMIN_ROLE_ZH[ADMIN_ROLE_SYSTEM],
                                             ADMIN_ROLE_ZH[ADMIN_ROLE_OPERATOR]))
        return role

    @staticmethod
    def _system_admins(table, exclude=None):
        """表里还有哪些**系统管理员**（可以排掉一个正要被删 / 被降的）。"""
        return [name for name, entry in table.items()
                if name != exclude and admin_role_of(entry) == ADMIN_ROLE_SYSTEM]

    def admin_verify(self, name, password):
        """校验管理员口令，返回 `AUTH_OK` / `AUTH_NO_SUCH_USER` / `AUTH_BAD_PASSWORD`。

        ★ 和玩家登录共用同一套三态，`AUTH_MESSAGES` 的中文文案直接能复用。
        ★ 调用方**不要把密码打进日志**（铁律 9），打名字和结果就够。
        """
        name = str(name or "").strip()
        password = str(password if password is not None else "")
        with self._lock:
            table = admin_accounts(self._read_unlocked())
        entry = table.get(name)
        if entry is None:
            return AUTH_NO_SUCH_USER
        if str(entry.get("password", "")) != password:
            return AUTH_BAD_PASSWORD
        return AUTH_OK

    def admin_add(self, name, password, role=None):
        """加一个管理员，返回新的名字表。

        名字和口令走玩家账号那套规则 —— 管理员名同样是 JSON 的键、
        也要经表单往返，没理由放得更松。

        `role` 不给 = **系统管理员**，和「老存档里没有 `role` 这个键」
        同一个口径（`admin_role_of`）—— 全局只有一条规则要记：
        **没有这条信息就是系统管理员**。管理页上那个下拉框会明确传值。
        """
        name = check_username(name)
        password = check_password(password)
        role = self._check_role(ADMIN_ROLE_SYSTEM if role is None else role)
        with self._lock:
            data = self._read_unlocked()
            table = self._admin_table_unlocked(data)
            if name in table:
                raise AccountError("admin_exists", f"管理员「{name}」已经存在")
            table[name] = {"password": password, "role": role}
            self._write_unlocked(data)
            return sorted(table)

    def admin_add_from_player(self, username, role=ADMIN_ROLE_OPERATOR):
        """把一个**玩家账号**原样收进管理员表：用户名和明文密码照搬。

        管理页「玩家资料」那一页的「设为管理员（运营）」走这一发（D40）。

        ★ **密码不出这一层**：调用方（管理页）只传用户名过来，页面从头到尾
        看不见密码，也就不会跑到日志、截图和浏览器历史里去（铁律 9）。
        ★ **不重新校验密码**：那串东西这个玩家一直在用，`check_password`
        再拦一道只会拦出「你的号能玩、但设不了管理员」这种解释不清的结果。
        名字倒是天然合规 —— 注册时就走过 `check_username` 了。
        """
        username = str(username or "").strip()
        role = self._check_role(role)
        with self._lock:
            data = self._read_unlocked()
            if not username or username not in data["accounts"]:
                raise AccountError("no_such_user",
                                   AUTH_MESSAGES[AUTH_NO_SUCH_USER])
            account = self._merged_account(username, data["accounts"][username])
            table = self._admin_table_unlocked(data)
            if username in table:
                raise AccountError("admin_exists",
                                   f"管理员「{username}」已经存在")
            table[username] = {"password": str(account.get("password", "")),
                               "role": role}
            self._write_unlocked(data)
            return sorted(table)

    def admin_set_role(self, name, role):
        """改一个管理员的权限，返回它的新权限。

        ★ **最后一个系统管理员不能降成运营** —— 和 `admin_remove` 是同一条
        不变式（运营看不到「管理员账号」这一页，降完就没人能改回来了）。
        拦在**存档层**，不只拦在前端。
        """
        name = str(name or "").strip()
        role = self._check_role(role)
        with self._lock:
            data = self._read_unlocked()
            table = self._admin_table_unlocked(data)
            if name not in table:
                raise AccountError("no_such_admin", f"没有名为「{name}」的管理员")
            entry = table[name]
            if not isinstance(entry, dict):
                entry = {}
                table[name] = entry
            if (role != ADMIN_ROLE_SYSTEM
                    and not self._system_admins(table, exclude=name)):
                raise AccountError(
                    "last_system_admin",
                    "至少要保留一个系统管理员，这是最后一个，不能改成运营")
            entry["role"] = role
            self._write_unlocked(data)
            return role

    def admin_set_password(self, name, password):
        """改管理员口令。★ 默认管理员那个弱口令就靠它换掉（D3）。"""
        name = str(name or "").strip()
        password = check_password(password)
        with self._lock:
            data = self._read_unlocked()
            table = self._admin_table_unlocked(data)
            if name not in table:
                raise AccountError("no_such_admin", f"没有名为「{name}」的管理员")
            entry = table[name]
            if not isinstance(entry, dict):
                entry = {}
                table[name] = entry
            entry["password"] = password
            self._write_unlocked(data)
            return name

    def admin_remove(self, name):
        """删一个管理员，返回剩下的名字表。

        ★ **最后一个系统管理员不能删**：删光了谁都进不去「管理员账号」页，
        只能上服务器手改 JSON 才救得回来。**运营不算数**（用户 2026-09-06）
        —— 留一屋子运营和「没人能管账号」是一回事。
        这一条拦在**存档层**，不只拦在前端 —— 前端拦得住鼠标，拦不住直接 POST。
        """
        name = str(name or "").strip()
        with self._lock:
            data = self._read_unlocked()
            table = self._admin_table_unlocked(data)
            if name not in table:
                raise AccountError("no_such_admin", f"没有名为「{name}」的管理员")
            if (admin_role_of(table[name]) == ADMIN_ROLE_SYSTEM
                    and not self._system_admins(table, exclude=name)):
                raise AccountError("last_admin",
                                   "至少要保留一个系统管理员，不能把它删掉")
            del table[name]
            self._write_unlocked(data)
            return sorted(table)


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

    存档里记的就是真实等级，`experience_bounds()` 还要拿它算经验条的两端。
    下发给客户端的也是同一个数（D22 起不再套兼容下限），见 `player_level()`。
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

    ★★ **发的就是真实等级，不套任何下限**（V0.3商店 D22，推翻 V0.2 D120）。

    同一个全局 `[0x72e338]` 同时被**三类**代码读：界面上的等级显示、
    那几道原版等级门（对战频道 / 生存模式 / 5-6 人房 / 天梯）、以及
    **物品的「穿上」判定**（商店 `0x445817` / 房间 `0x46aff1` 拿它和
    `ItemInfo+0x1c` 比）。抬高它就等于连商店的等级门槛一起放水，所以
    「解锁对战」只能去 patch 客户端的判据本身，不能改这个数。

    等级 0（没有账号 / 存档字段坏了）照实返回 0：那是「取不到账号」的信号。
    """
    if not account:
        return 0
    try:
        level = int(account.get("level", 0))
    except (TypeError, ValueError):
        return 0
    if level <= 0:
        return 0
    return level


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


# ==========================================================================
# 仓库 / 装备 / 材料（V0.3商店 M2）
#
# 下面这几个读取器全部**逐条容错**，和 `_quest_records()` 一个道理：存档是
# 给人手改的，键写成数字、值写成字符串、混进垃圾条目都可能发生，绝不能让
# 一条脏数据把整个下发打掉。
# ==========================================================================

def _inventory_records(account):
    """存档里的 `inventory` -> `{int itemId: {"count": int, "expires": ...}}`。

    ★ 简写也认：`"1010015": 2` 会被当成 `{"count": 2, "expires": None}`
    —— 人手写 JSON 时最容易这么写，没必要为此把存档判成坏的。
    """
    if not account:
        return {}
    raw = account.get("inventory")
    if not isinstance(raw, dict):
        return {}
    records = {}
    for key, value in raw.items():
        try:
            item_id = int(key)
        except (TypeError, ValueError):
            continue
        if item_id <= 0:
            continue
        expires = None
        if isinstance(value, dict):
            expires = value.get("expires")
            raw_count = value.get("count", 1)
        else:
            raw_count = value
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            count = 1
        if count <= 0:                       # 数量 0 / 负数 = 根本没有这件东西
            continue
        records[item_id] = {"count": count, "expires": expires}
    return records


def inventory_items(account):
    """仓库里的持有物 `{itemId: {"count", "expires"}}`。"""
    return _inventory_records(account)


def owned_item_ids(account):
    """持有物的 itemId 列表（已排序）。

    ★ **不含商城角色物品** —— 那一批由 `character_item_ids()` 从
    `owned_characters` 派生（V0.1 §119），不落在 `inventory` 里。
    要往 `0x030b` 里塞的是这两份的**并集**。
    """
    return sorted(_inventory_records(account))


def has_item(account, item_id):
    """仓库里有没有这件东西。"""
    try:
        return int(item_id) in _inventory_records(account)
    except (TypeError, ValueError):
        return False


def _material_records(account):
    """存档里的 `materials` -> `{int itemId: int 数量}`。"""
    if not account:
        return {}
    raw = account.get("materials")
    if not isinstance(raw, dict):
        return {}
    records = {}
    for key, value in raw.items():
        try:
            item_id, count = int(key), int(value)
        except (TypeError, ValueError):
            continue
        if item_id <= 0 or count <= 0:
            continue
        records[item_id] = count
    return records


def material_counts(account):
    """合成材料的存量 `{itemId: 数量}`。"""
    return _material_records(account)


def material_count(account, item_id):
    """某一种材料有几个（没有就是 0）。"""
    try:
        return _material_records(account).get(int(item_id), 0)
    except (TypeError, ValueError):
        return 0


def equipped_items(account):
    """当前穿在身上的 itemId 列表，**已满足两条不变式**。

    ★ 这是 `0x030b gspSlotEquippedList` 里「装备」那一半的唯一数据源，
    而 `0x030b` 又是战斗加成的唯一来源（FINDINGS §1 / §4）—— 所以宁可
    在这儿把可疑的条目丢掉，也不要发一个客户端查不到的 id 下去。

    两条不变式在**读的时候**就地生效（手改过的存档也照样干净），
    `ensure_item_fields()` 负责把同一套结论落到磁盘上：

    1. 两两 `part_flag` 按位与为 0（`shopdata.resolve_equipped`，先到先得）；
    2. 每件都在 `inventory` 里 —— 穿着一件自己没有的东西是脏数据。
    """
    raw = (account or {}).get("equipped")
    if not isinstance(raw, (list, tuple)):
        return []
    kept, _dropped = shopdata.resolve_equipped(raw)
    owned = _inventory_records(account)
    return [item_id for item_id in kept if item_id in owned]


def normalize_item_fields(raw):
    """把一个**原始**账号字典里的物品三件套洗成可以直接写回 JSON 的形态。

    返回 ``(inventory, equipped, materials, notes)``。前三个的键已经排序、
    已经是字符串；`notes` 是给人看的改动说明（空表 = 这个账号本来就是干净的）。

    ★ 传进来的必须是**原始**字典，不是 `_merged_account()` 的结果 ——
    后者在读的那一刻就把缺的键补上了，看不出「磁盘上到底缺了什么」。
    """
    notes = []
    for field in ("inventory", "equipped", "materials"):
        if field not in raw:
            notes.append("补上 " + field)

    inventory = _inventory_records(raw)
    # 客户端不认识（或只有货架条目、进不了背包）的持有物留着也发不下去，
    # 仓库里会是个空格子（FINDINGS §11）。`shop_items.json` 换代之后
    # 靠这一步收敛。
    unknown = sorted(i for i in inventory if not shopdata.ownable(i))
    for item_id in unknown:
        del inventory[item_id]
    if unknown:
        notes.append("丢掉客户端不认识的持有物 "
                     + "/".join(str(i) for i in unknown))

    materials = _material_records(raw)
    unknown_material = sorted(i for i in materials if not shopdata.ownable(i))
    for item_id in unknown_material:
        del materials[item_id]
    if unknown_material:
        notes.append("丢掉客户端不认识的材料 "
                     + "/".join(str(i) for i in unknown_material))

    new_inventory = {str(i): dict(inventory[i]) for i in sorted(inventory)}
    new_materials = {str(i): materials[i] for i in sorted(materials)}

    # ★ 装备要拿**洗过的** inventory 去判「是不是自己的」，所以这里现拼一个
    #   账号视图给 `equipped_items()`，而不是直接把 `raw` 递过去。
    view = dict(raw)
    view["inventory"] = new_inventory
    new_equipped = equipped_items(view)
    before = raw.get("equipped")
    if isinstance(before, (list, tuple)):
        lost = [i for i in before if i not in new_equipped]
        if lost:
            notes.append("卸下站不住的装备 "
                         + "/".join(str(i) for i in lost))

    return new_inventory, new_equipped, new_materials, notes


def admin_accounts(data):
    """存档顶层的管理员表 `{名字: {"password": ..., "role": ...}}`。

    ★ **坏了就当没有，不抛异常** —— 管理页是运维功能，一条手改坏的
    `admin_accounts` 不该把整个游戏服拖住（铁律 11）。失败模式是
    「谁都登不进管理页」，改坏它的人一眼看得出来，玩家一点感觉都没有。
    """
    raw = (data or {}).get(ADMIN_ACCOUNTS_KEY)
    if not isinstance(raw, dict):
        return {}
    table = {}
    for name, value in raw.items():
        if not isinstance(value, dict):
            continue
        table[str(name)] = value
    return table


def admin_role_of(entry):
    """一条管理员记录的权限（D34）。

    ★ 两条规则，都别改：

    1. **没有 `role` 这个键 = 系统管理员**。权限是 2026-09-06 才加的，
       在那之前建的账号（包括默认的 `admin`）文件里一个 `role` 都没有
       —— 把它们当成运营，第一件事就是**没人进得去「管理员账号」页**，
       连改回来的入口都没有了。
    2. **写了但不认识 = 运营**（最小权限）。`"sysadmin"` 这种手滑要是当成
       系统管理员，一个拼写错误就等于全放行。
    """
    if not isinstance(entry, dict) or "role" not in entry:
        return ADMIN_ROLE_SYSTEM
    role = str(entry.get("role") or "").strip().lower()
    return role if role in ADMIN_ROLES else ADMIN_ROLE_OPERATOR
