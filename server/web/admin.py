#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""管理页 `/admin` —— 改三份运营配置，管管理员账号（V0.3商店 M8）。

和注册页**共用同一个 27810 端口、同一个 `Handler`**：`web/server.py` 的
`Handler` 继承本文件的 `AdminRoutes`，路由表里 `/admin` 开头的都转进来。

    GET  /admin                       页面本身（`admin.html`）
    GET  /admin/admin.css             样式（**不要登录** —— 登录页自己也要它）
    GET  /admin/admin.js              脚本（同上）
    GET  /admin/itemicons.png         物品图标图集（要登录）
    GET  /admin/api/session           我是谁（页面启动时问一次）
    POST /admin/api/login             {name, password}
    POST /admin/api/logout
    GET  /admin/api/catalog           物品表 + 字段描述 + 图集元信息（登录后拿一次）
    GET  /admin/api/config/{shop|recipe|drops}     -> {ok, text, warnings}
    POST /admin/api/config/{shop|recipe|drops}     {text}
    GET  /admin/api/admins            -> {ok, names}
    POST /admin/api/admins/add        {name, password}
    POST /admin/api/admins/password   {name, password}
    POST /admin/api/admins/remove     {name}
    GET  /admin/api/item?id=1120041   某件东西**现在在商店里**是什么价（选择器侧栏用）
    GET  /admin/api/players?q=名字&page=0  按用户名 / 昵称找玩家（一页 10 行）
    GET  /admin/api/player?name=alice  一个玩家的可编辑资料
    POST /admin/api/player            {name, level, money, materials, inventory}

## 玩家资料页：**商店在卖的东西一律只读**（用户 2026-09-05 拍板）

D22 之后商店按**真实等级**卖东西。管理页要是能把 4 级才卖的枪直接塞进
1 级号的仓库，那条等级门槛就白设了。所以：

- **能改**：等级、金币、材料、以及 `shop.json` 里 `listed=false` 的物品
  （合成产物、货架上根本买不到的那批）；
- **不能改**：`listed=true` 的物品。要给就改金币和等级，让玩家自己进商店买。

判据是 `listed` 而不是「在不在 `shop.json` 里」—— 材料和合成产物本来就
在那份表里躺着（`default_shop()` 收它们只为有个中文名），它们正是用户
说的「商店里没有的物品」。

## ★★ 口令是明文存的，而这个页面公网可达

用户拍板走明文（D3，和玩家账号一个口径，铁律 9），默认管理员还是
`admin` / `Admin123`。**三条补偿是硬要求，改代码时别顺手拿掉**：

1. 登录**按 IP 限速**（`LoginRateLimiter`，注意它和注册限速的极性相反）；
2. **启动日志里警告「默认管理员还在用出厂口令」**（`default_admin_password_in_use()`，
   `app.py` 启动时打一行）。★ 这一条以前是画在页面顶上的一条红字，
   用户 2026-09-05 要求页面上不要显示，于是挪进了日志（D24）；
3. 日志里**只打名字和结果，绝不打口令**。

## 配置编辑器是**结构化表单**（D16 取代 D14）

原来是三个 `<textarea>` 直接改 JSON 原文；用户嫌「改一个价格要在几百行里找」。
D14 当时就写明了退路：**表单化只是换一个前台生成同样的 JSON，服务端不用动。**
所以现在：

- **保存通路一个字没变** —— 前台组装出同样的 JSON，仍旧 `POST` 到
  `/admin/api/config/{name}`，仍旧过 `shopcfg.validate_*`，不过就不落盘。
  **真正的护栏还在那一层**，前台长什么样都绕不过去。
- 多出来的只有两个**只读**接口：`/admin/api/catalog`（物品表 + 字段描述表）
  和 `/admin/itemicons.png`（图标图集）。
- 字段描述表 `shopcfg.SCHEMA` **贴着 validator 放**，两边对不上就有用例报红
  —— 这是「以后新增的字段自动出现在画面上」的保证。

## `_` 开头的键不再回写（D16）

以前保存的是「解析后的对象」，`_说明` 那种注释键会原样留在文件里。
现在唯一的编辑入口是这个页面，那几句话搬进了 `SCHEMA[...]["help"]` 直接画在面板上，
**保存时只写 `format` + 那一个列表** —— 老文件里的 `_说明` 会在第一次保存时消失，
这是用户要的。
"""
from __future__ import annotations

import http.cookies
import json
import math
import os
import secrets
import threading
import time
import urllib.parse

import account_store
import eventlog
import shopcfg
import shopdata

HERE = os.path.dirname(os.path.abspath(__file__))
ADMIN_PATH = os.path.join(HERE, "admin.html")

#: `/admin/<名字>` 能直接取到的静态文件。
#:
#: ★ `admin.css` / `admin.js` **不要登录**：`/admin` 本身（登录表单）就是
#:   未登录状态渲染的，样式再要登录，登录页就成了一堆裸标签。它们里面没有秘密。
#: ★ `itemicons.png` **要登录**：0.62 MB 的原版美术，没理由发给路过的人。
#:   `<img>` / CSS `url()` 是同源子请求，`Path=/admin` 的会话 cookie 会跟着走
#:   （`SameSite=Strict` 只挡跨站，同站子请求照带）。
STATIC_FILES = {
    "admin.css": ("text/css; charset=utf-8", False),
    "admin.js": ("application/javascript; charset=utf-8", False),
    "itemicons.png": ("image/png", True),
}

#: 图集索引（`tools/shopicons.py` 的产物之一）。
ICON_INDEX_PATH = os.path.join(HERE, "itemicons.json")

#: 认得的图集格式版本。对不上就当没有图标 —— 管理页画问号占位，
#: 而不是按错的行列切出一堆张冠李戴的图。
ICON_FORMAT = 1

#: 会话**闲置**有效期（用户 2026-09-05 拍板：一小时）。
#:
#: ★ 这是**滑动**的：每一次带着有效令牌的请求都把到期时刻推到「现在 + 1 小时」
#:   （`AdminSessions.resolve`）⇒ 一直在操作就永远不掉线，撂下一小时不动才登出。
#: ★ **只在内存里**，服务端一重启全部失效 —— 和票据同一个口径（V0.2 D097）。
#:   管理页是低频运维工具，重登一次的代价远小于把令牌落盘。
SESSION_TTL_SECONDS = 3600

#: 会话 cookie 的名字。`HttpOnly` + `SameSite=Strict`，JS 读不到也带不出去。
SESSION_COOKIE = "popshot_admin"

#: cookie 的 `Max-Age`。**故意和 `SESSION_TTL_SECONDS` 脱钩**（D29）：
#: 浏览器不知道服务端在滑动到期时刻，写 1 小时的话「登录后连续操作两小时」
#: 到第 60 分钟就会把 cookie 丢掉，明明还在用却被踢出去。
#: ⇒ cookie 只负责「**别在关掉页面时消失**」，真正说了算的是服务端那份到期时刻；
#:   cookie 活得比会话久没有风险 —— 令牌一过期，服务端认不出来就是没登录。
SESSION_COOKIE_MAX_AGE = 7 * 24 * 3600

#: 登录失败后，同一个 IP 要等的秒数。
#:
#: ★ 铁律 10 说「禁止固定时间的阈值」，**这里正是它明说的那个例外**：
#:   对面是个不会通知我们的攻击者，物理上没有任何事件可等。
#:   5 秒 ≈ 每分钟最多 12 次尝试 —— 对打错一次密码的人几乎无感，
#:   对爆破 `Admin123` 这种弱口令则是致命的。
LOGIN_COOLDOWN_SECONDS = 5

#: URL 里的名字 → `server/data/` 里的文件名。
CONFIG_FILES = {
    "shop": shopcfg.SHOP_FILENAME,
    "recipe": shopcfg.RECIPE_FILENAME,
    "drops": shopcfg.DROPS_FILENAME,
}

#: 每份配置的校验器。★ **存盘前必过这一关**，不过就不落盘（D10 的同一个道理：
#: 宁可让用户看到「第 3 条配方的材料有 5 种」，也不要让服务端读到半份坏文件）。
CONFIG_VALIDATORS = {
    "shop": shopcfg.validate_shop,
    "recipe": shopcfg.validate_recipes,
    "drops": shopcfg.validate_drops,
}

#: 配置正文上限。三份加起来现在约 45 KB，给 4 MB 足够宽裕。
#: `web/server.py` 的 `MAX_BODY_BYTES` 是 1 MB —— 那是**请求体**的上限，
#: 比这里更严，所以实际卡住的是那一个。留着这条只为让错误话说得更清楚。
MAX_CONFIG_BYTES = 4 << 20


class AdminSessions:
    """`{token: (管理员名, 到期时刻)}`。只在内存里，服务端一重启就全没了。"""

    def __init__(self, ttl=SESSION_TTL_SECONDS, clock=time.monotonic):
        self.ttl = max(1, int(ttl))
        self._clock = clock
        self._sessions = {}
        self._lock = threading.Lock()

    def issue(self, name):
        """发一个新令牌。★ `secrets` 不是 `random` —— 这是认证凭据。"""
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            self._prune(now)
            self._sessions[token] = (str(name), now + self.ttl)
        return token

    def resolve(self, token):
        """令牌对应哪个管理员；没有 / 过期都返回 `None`。

        ★ **认出来就顺手续期**（滑动过期，用户 2026-09-05 拍板）：到期时刻
        推到「现在 + ttl」。判据是「这一发请求本身」—— 有请求就是有人在操作，
        不需要前台额外报「我还在」（那种心跳会让页面开着就永不登出，
        正好和用户要的「撂下一小时就登出」相反）。
        """
        if not token:
            return None
        now = self._clock()
        with self._lock:
            self._prune(now)
            entry = self._sessions.get(token)
            if entry is None:
                return None
            self._sessions[token] = (entry[0], now + self.ttl)
            return entry[0]

    def drop(self, token):
        """退出登录。已经不在了也当成功 —— 幂等，前台不用分情况。"""
        with self._lock:
            self._sessions.pop(token, None)

    def drop_admin(self, name):
        """把某个管理员的**全部**会话作废。

        ★ 改密码和删账号之后必须调它：不然那个人手里的旧令牌还能继续用，
        「我把他删了」和「他还在操作」会同时成立。
        """
        with self._lock:
            for token in [t for t, (who, _) in self._sessions.items()
                          if who == name]:
                del self._sessions[token]

    def _prune(self, now):
        """清掉过期的。**调用方持锁。**"""
        for token in [t for t, (_, deadline) in self._sessions.items()
                      if deadline <= now]:
            del self._sessions[token]


class LoginRateLimiter:
    """按 IP 限制管理员登录频率。

    ★★ **和 `RegisterRateLimiter` 的极性正好相反**，别照着改：
    那个「只有**成功**才记一笔」（批量注册脚本要的是成功，锁成功那侧就够）；
    这个「只有**失败**才记一笔」—— 爆破口令靠的是海量失败，锁失败那一侧
    才拦得住，而输对了的人不该被自己上一次的手滑挡住。

    `clock` 只为测试留（默认 `time.monotonic`，不受系统改时间影响）。
    """

    def __init__(self, cooldown=LOGIN_COOLDOWN_SECONDS, clock=time.monotonic):
        self.cooldown = max(0, int(cooldown))
        self._clock = clock
        self._until = {}
        self._lock = threading.Lock()

    def retry_after(self, host):
        """这个 IP 还要等几秒。`0` = 现在就可以试。"""
        if self.cooldown <= 0:
            return 0
        now = self._clock()
        with self._lock:
            self._prune(now)
            deadline = self._until.get(host)
        if deadline is None:
            return 0
        # 向上取整，理由同 `RegisterRateLimiter.retry_after`。
        return max(0, int(math.ceil(deadline - now)))

    def mark_failure(self, host):
        """记下「这个 IP 刚登录失败」，返回它要等的秒数。"""
        if self.cooldown <= 0:
            return 0
        now = self._clock()
        with self._lock:
            self._prune(now)
            self._until[host] = now + self.cooldown
        return self.cooldown

    def clear(self, host):
        """登录成功 ⇒ 把这个 IP 的处罚撤掉。

        不撤的话，「输错一次、马上输对」的人在**下一次**登录时还要再等一轮，
        而他明明已经证明了自己知道口令。
        """
        with self._lock:
            self._until.pop(host, None)

    def _prune(self, now):
        for key in [k for k, deadline in self._until.items() if deadline <= now]:
            del self._until[key]


def render_admin():
    """读 `admin.html`，把名字 / 口令规则那两句话填进去。"""
    with open(ADMIN_PATH, "r", encoding="utf-8") as fp:
        html = fp.read()
    return (html
            .replace("__USERNAME_RULE__", _escape(account_store.USERNAME_RULE_TEXT))
            .replace("__PASSWORD_RULE__", _escape(account_store.PASSWORD_RULE_TEXT)))


def _escape(text):
    return (str(text)
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


# --------------------------------------------------------------------------
# 物品表 + 图集 —— 页面登录后拿一次，之后全在前台算
# --------------------------------------------------------------------------

_icon_index_cache = None
_catalog_cache = None
_catalog_lock = threading.Lock()


def icon_index():
    """图集索引 `{"size","cols","width","height","cells":{icon名: 格号}}`。

    产物没生成 / 版本对不上 / 读坏了 ⇒ 返回 `None`，页面画问号占位。
    **不让它把管理页带崩** —— 图标是锦上添花，配置才是正事。

    只读一次：`itemicons.json` 随代码走，运行期间不会变。
    """
    global _icon_index_cache
    if _icon_index_cache is not None:
        return _icon_index_cache or None
    try:
        with open(ICON_INDEX_PATH, "r", encoding="utf-8") as fp:
            index = json.load(fp)
    except (IOError, OSError, ValueError):
        _icon_index_cache = {}
        return None
    if not isinstance(index, dict) or index.get("format") != ICON_FORMAT:
        _icon_index_cache = {}
        return None
    _icon_index_cache = index
    return index


def catalog():
    """全部**能进背包**的物品，给管理页的选择器和图标用。

    ★ 只收 `ownable` 的：`shopcfg._check_item_id()` 也只放行这一批，
    选得到却存不进去的东西不该出现在选择器里（§11）。

    算一次留着：`shop_items.json` 随代码走，运行期间不会变。约 800 件、
    序列化后 145 KB 上下，页面登录后取一次。

    ★ `desc` 就是**游戏里提示框那段说明**（`shopcfg.item_desc_zh`，和
    `0x0501` 的 `ItemInfo+0x18` / `0x0500` 的 `ShopStock+0x18` 同源）——
    管理页的悬停浮窗直接拿它画，两边看到的是同一份数字（D26）。
    638 件有说明，加起来才 21 KB，没必要为它多开一条按需查询的路。
    """
    global _catalog_cache
    if _catalog_cache is not None:
        return _catalog_cache
    with _catalog_lock:
        if _catalog_cache is not None:      # 等锁的时候别人已经算完了
            return _catalog_cache
        index = icon_index() or {}
        cells = index.get("cells") or {}
        items = []
        for kind in shopdata.kinds():
            for item_id in shopdata.ids_of_kind(kind):
                item = shopdata.get(item_id)
                if item is None or not item.ownable:
                    continue
                entry = {
                    "id": item.id,
                    "kind": item.kind,
                    "name": shopcfg.item_name_zh(item),
                    "cell": cells.get(item.icon),
                }
                # 有才带 —— 800 件里大部分字段是空的，全量带上白涨一倍体积。
                desc = shopcfg.item_desc_zh(item)
                if desc:
                    entry["desc"] = desc
                if item.name_kr:
                    entry["name_kr"] = item.name_kr
                if item.character is not None:
                    entry["character"] = item.character
                if item.series:
                    entry["series"] = item.series
                if item.tier:
                    entry["tier"] = item.tier
                if item.part_flag:
                    entry["part_flag"] = item.part_flag
                if item.bonus:
                    entry["bonus"] = item.bonus
                if item.weapon:
                    entry["weapon"] = item.weapon
                items.append(entry)
        _catalog_cache = items
    return _catalog_cache


#: 玩家列表一页几行。
#:
#: 这不是「等一等就好了」的阈值（铁律 10），是**一页显示多少行**的界面取舍
#: —— 用户 2026-09-05 指定 10 行，超过就翻页。分页在**服务端**做
#: （`search_accounts` 回「这一页 + 命中总数」），所以账号再多也不会
#: 一次性把整份名单发给浏览器。
PLAYER_PAGE_SIZE = 10


def default_admin_password_in_use(accounts):
    """默认管理员还在用出厂口令吗？

    ★ 只回一个布尔，**不回口令是什么**（铁律 9）。
    `app.py` 启动时拿它打一行警告 —— 这个页面公网可达，而口令是明文存的
    （D3），出厂口令没改就等于没有认证。以前这句话画在页面顶上，
    用户 2026-09-05 要求拿掉，于是挪来了这里（D24）。
    """
    return (accounts.admin_verify(
        account_store.DEFAULT_ADMIN_NAME,
        account_store.DEFAULT_ADMIN_PASSWORD) == account_store.AUTH_OK)


def _online_usernames():
    """现在有哪些账号连着游戏服。拿不到（比如单跑注册页）就当没人在线。

    ★ **惰性 import** `gameserver`：`web/` 这一层本来不依赖游戏服，
    单元测试和 `--no-web` 之外的组合都不该因为它而多背一个大模块。
    """
    try:
        import gameserver
    except ImportError:
        return set()
    return {conn.account_name for conn in gameserver.all_conns()
            if conn.account_name}


def _push_account(username):
    """改完存档立刻推给在线的那条连接，返回是否真推了。

    推的这几发和控制通道的 `sync-account` 是同一套（顺序也一样，§29）：
    `0x0600` 带着**金币 / 经验 / 等级**（等级那一格就是客户端全局
    `[0x72e338]`，所以改完等级不用重登），`0x0501`→`0x0601` 刷仓库，
    `0x0604` 刷穿着，`0x030b` 是装备加成的唯一来源（§1）。

    不在线就什么都不做 —— 下次登录时本来就是从存档读的。
    """
    try:
        import gameserver
    except ImportError:
        return False
    pushed = False
    for conn in gameserver.all_conns():
        if conn.account_name != username:
            continue
        try:
            conn.reload_account()
            conn.send_rep_money(reason="（管理页改了资料）")
            conn.send_slot_equipped_list(reason="（管理页改了资料）")
            conn.send_rep_inventory(reason="（管理页改了资料）")
            conn.send_rep_equipped_list(reason="（管理页改了资料）")
        except (OSError, AttributeError):
            # socket 刚断 / 还没登录完 —— 不能让它把保存这件事带崩，
            # 存档已经落盘了，玩家重登一样能看到。
            continue
        pushed = True
    return pushed


def stackable(item_id):
    """这件东西的**数量有没有意义**。装备类没有 —— 只有「有」和「没有」。

    判据是 `part_flag != 0`（= `shopdata.equippable`），和 `shop.py` 里
    「装备不能重复购买」用的是同一条（`check_purchase` 的 `BUY_ALREADY_OWNED`）。

    ★ 客户端那边也是这么认的：`ItemInfo+0x10` 的形态标志里，`0x01` 才是
    「计数持有」（提示框写「소지개수 : %d개」），装备发的是 `0x08` 可装备位，
    **数量那一格根本没人读**（FINDINGS §28）。⇒ 给一件铠甲存 ×3 是句空话，
    管理页干脆不给填，免得管理员以为自己发了三件（用户 2026-09-05）。
    """
    return not shopdata.equippable(item_id)


def _optional_int(value, label):
    """`None` / 空串 = 「这一项不改」；其余必须是整数。"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}要填一个整数，收到 {value!r}") from None


def _counts_of(raw):
    """`{itemId: 数量}` 的补丁体。不是字典就当没传。"""
    if not isinstance(raw, dict):
        return {}
    counts = {}
    for key, value in raw.items():
        try:
            item_id, count = int(key), int(value)
        except (TypeError, ValueError):
            raise ValueError(f"物品数量表里有一条读不出来：{key!r}={value!r}") from None
        if count < 0:
            raise ValueError(f"物品 {item_id} 的数量不能是负数")
        counts[item_id] = count
    return counts


def _player_view(username, account):
    """一个玩家的可编辑资料。名字 / 图标让前台自己按 `catalog()` 查。"""
    table, _warnings = shopcfg.shop()
    experience = account_store.player_experience(account)
    start, nxt = account_store.experience_bounds(experience)
    equipped = set(account_store.equipped_items(account))
    inventory = account_store.inventory_items(account)
    materials = account_store.material_counts(account)

    def locked(item_id):
        """商店在卖 ⇒ 只读。★ 判据是 `listed`，不是「在不在 shop.json 里」
        —— 材料和合成产物本来就在那份表里躺着（`listed=false`），
        它们正是用户说的「商店里没有的物品」。"""
        return bool((table.get(item_id) or {}).get("listed"))

    return {
        "username": username,
        "nickname": account_store.display_name(account),
        "level": account_store.player_level(account),
        "level_max": account_store.LEVEL_MAX,
        "experience": experience,
        "level_start_exp": start,
        "next_level_exp": nxt,
        "money": account_store.player_money(account),
        "online": username in _online_usernames(),
        "materials": [{"id": item_id, "count": materials[item_id],
                       "locked": locked(item_id), "stackable": True}
                      for item_id in sorted(materials)],
        "inventory": [{"id": item_id,
                       "count": inventory[item_id]["count"],
                       "locked": locked(item_id),
                       "stackable": stackable(item_id),
                       "equipped": item_id in equipped}
                      for item_id in sorted(inventory)],
    }


class AdminRoutes:
    """混进 `web.server.Handler` 的 `/admin` 那一组接口。

    需要宿主提供的东西：`self.accounts`（`AccountStore`）、`self._reply` /
    `self._send` / `self._send_json`、`self.client_ip()` / `self.client_label()`、
    `self.headers`、`self.log_message`。类属性 `admin_sessions` /
    `admin_limiter` 由 `make_server` 塞进来。
    """

    #: 由 `make_server` 塞进来的两个共享对象。
    admin_sessions: AdminSessions = None
    admin_limiter: LoginRateLimiter = None

    # ------------------------------------------------------------ 会话工具
    def _admin_token(self):
        """从 Cookie 头里取会话令牌。没有就 `None`。"""
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = http.cookies.SimpleCookie()
            jar.load(raw)
        except http.cookies.CookieError:
            # 别人塞了一个畸形 Cookie 不该让管理页 500。
            return None
        morsel = jar.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def _admin_name(self):
        """当前登录的是谁；没登录返回 `None`。"""
        return self.admin_sessions.resolve(self._admin_token())

    def _require_admin(self):
        """没登录就回 401 并返回 `None`；登录了就返回名字。

        ★ **每个接口第一句都调它**（`login` 除外）。漏一个就等于把那个接口
        开在公网上 —— `test_web_admin` 有一条用例逐个路径检查这件事。
        """
        name = self._admin_name()
        if name is None:
            self._reply(False, "请先登录管理页", status=401)
            return None
        return name

    def _set_session_cookie(self, token):
        # HttpOnly：JS 读不到，XSS 也偷不走。SameSite=Strict：别的站点发过来的
        # 请求不带它，顺手把 CSRF 也挡了（管理页没有跨站使用的场景）。
        # ★ `Max-Age` 用 `SESSION_COOKIE_MAX_AGE`，**不是**会话 ttl —— 理由
        #   写在那个常量上面：服务端的到期时刻是滑动的，浏览器不知道。
        self.send_header(
            "Set-Cookie",
            "%s=%s; Path=/admin; HttpOnly; SameSite=Strict; Max-Age=%d"
            % (SESSION_COOKIE, token, SESSION_COOKIE_MAX_AGE))

    def _clear_session_cookie(self):
        self.send_header(
            "Set-Cookie",
            "%s=; Path=/admin; HttpOnly; SameSite=Strict; Max-Age=0"
            % SESSION_COOKIE)

    # -------------------------------------------------------------- 路由
    def admin_get(self, path, query):
        """`/admin` 开头的 GET。认识就处理并返回 True。"""
        if path in ("/admin", "/admin/"):
            self._send(200, render_admin(), "text/html; charset=utf-8")
            return True
        asset = path[len("/admin/"):] if path.startswith("/admin/") else ""
        if asset in STATIC_FILES:
            self._admin_asset(asset)
            return True
        if path == "/admin/api/catalog":
            self._admin_catalog()
            return True
        if path == "/admin/api/session":
            name = self._admin_name()
            self._send_json({"ok": True, "name": name,
                             "logged_in": name is not None})
            return True
        if path.startswith("/admin/api/config/"):
            self._admin_config_get(path.rsplit("/", 1)[-1])
            return True
        if path == "/admin/api/admins":
            if self._require_admin() is None:
                return True
            self._send_json({"ok": True, "names": self.accounts.admin_names()})
            return True
        if path == "/admin/api/item":
            self._admin_item_lookup(query)
            return True
        if path == "/admin/api/players":
            self._admin_player_search(query)
            return True
        if path == "/admin/api/player":
            self._admin_player_get(query)
            return True
        if path.startswith("/admin"):
            self._reply(False, "没有这个接口", status=404)
            return True
        return False

    def admin_post(self, path, data):
        """`/admin` 开头的 POST。认识就处理并返回 True。"""
        if path == "/admin/api/login":
            self._admin_login(data)
            return True
        if path == "/admin/api/logout":
            self._admin_logout()
            return True
        if path.startswith("/admin/api/config/"):
            self._admin_config_post(path.rsplit("/", 1)[-1], data)
            return True
        if path.startswith("/admin/api/admins/"):
            self._admin_manage(path.rsplit("/", 1)[-1], data)
            return True
        if path == "/admin/api/player":
            self._admin_player_save(data)
            return True
        if path.startswith("/admin"):
            self._reply(False, "没有这个接口", status=404)
            return True
        return False

    # ---------------------------------------------------------- 静态文件
    def _admin_asset(self, name):
        """把 `server/web/<name>` 原样吐出去，带 `ETag` 条件请求。

        ★ 用 `ETag` + `no-cache` 而**不是** `max-age`：图集有 0.62 MB，
        每次刷新都重下太浪费；但 `max-age` 又会让「刚跑完
        `update-shopicons.bat`，浏览器里还是旧图」这种事出现一整天。
        `no-cache` 的意思是「每次都问一下」—— 没变就是一个 304 空响应，
        字节数约等于零，而且**永远不会看到旧的**。

        ★ 路径是从 `STATIC_FILES` 白名单里取的常量，不是用户输入拼的
        —— 这里不存在 `..\\..\\accounts.json` 那种走法。
        """
        content_type, needs_login = STATIC_FILES[name]
        if needs_login and self._require_admin() is None:
            return
        path = os.path.join(HERE, name)
        try:
            st = os.stat(path)
            with open(path, "rb") as fp:
                body = fp.read()
        except OSError:
            # 打包漏了一个文件时，说清楚是**哪一个** —— 云上 500 而本地好好的
            # 就是这么来的（`tools/build-common.ps1` 里那张验收清单防的也是它）。
            self._reply(False, f"服务端少了 web/{name}（打包漏了？）", status=404)
            return
        etag = '"%d-%d"' % (st.st_mtime_ns, st.st_size)
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "private, no-cache")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "private, no-cache")
        self.end_headers()
        self.wfile.write(body)

    # -------------------------------------------------------------- 接口
    def _admin_catalog(self):
        """物品表 + 字段描述 + 图集元信息。页面登录后拿一次，之后全在前台算。"""
        if self._require_admin() is None:
            return
        index = icon_index()
        self._send_json({
            "ok": True,
            "items": catalog(),
            # ★ 字段描述表直接来自 `shopcfg` —— 页面照着它生成输入框，
            #   所以「给 validator 加一个字段」= 「画面上自动多一个框」。
            "schema": shopcfg.SCHEMA,
            "icons": None if index is None else {
                "url": "/admin/itemicons.png",
                "size": index.get("size"),
                "cols": index.get("cols"),
                "width": index.get("width"),
                "height": index.get("height"),
            },
            "kinds": shopcfg.KIND_ZH,
            "characters": {str(k): v for k, v in shopcfg.CHARACTER_ZH.items()},
            "series": shopcfg.SERIES_ZH,
            "max_materials": shopcfg.MAX_MATERIALS,
        })

    def _admin_login(self, data):
        # ★ 限速放在**最前面**：被限住的时候连「有没有这个管理员」都不该
        #   问得出来，否则限速就成了一个免费的枚举接口（同 `_api_register`）。
        host = self.client_ip()
        wait = self.admin_limiter.retry_after(host)
        if wait:
            self._reply(False, f"登录太频繁，请 {wait} 秒后再试", status=429)
            return
        name = str(data.get("name") or "").strip()
        result = self.accounts.admin_verify(name, data.get("password"))
        if result != account_store.AUTH_OK:
            wait = self.admin_limiter.mark_failure(host)
            # ★ 只打名字和结果，**绝不打口令**（铁律 9）。
            eventlog.online(f"[admin] 登录失败 {name!r}（{result}）"
                            f" 来自 {self.client_label()}")
            # 「没这个人」和「密码错」对**攻击者**是两条不同的信息，但玩家账号
            # 那边本来就分开说（`AUTH_MESSAGES`），管理页人少、限速也在，
            # 保持同一套文案比自作聪明地含糊其辞更好查。
            self._reply(False, account_store.AUTH_MESSAGES.get(result, "登录失败")
                        + (f"（{wait} 秒后才能再试）" if wait else ""))
            return
        self.admin_limiter.clear(host)
        token = self.admin_sessions.issue(name)
        eventlog.online(f"[admin] 登录成功 {name!r} 来自 {self.client_label()}")
        body = json.dumps({"ok": True, "message": "登录成功", "name": name},
                          ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._set_session_cookie(token)
        self.end_headers()
        self.wfile.write(body)

    def _admin_logout(self):
        token = self._admin_token()
        name = self.admin_sessions.resolve(token)
        self.admin_sessions.drop(token)
        if name:
            eventlog.online(f"[admin] 退出登录 {name!r}")
        body = json.dumps({"ok": True, "message": "已退出登录"},
                          ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._clear_session_cookie()
        self.end_headers()
        self.wfile.write(body)

    def _admin_config_get(self, which):
        if self._require_admin() is None:
            return
        filename = CONFIG_FILES.get(which)
        if filename is None:
            self._reply(False, f"没有名为 {which!r} 的配置", status=404)
            return
        path = shopcfg.path_of(filename)
        try:
            with open(path, "r", encoding="utf-8") as fp:
                text = fp.read()
        except OSError as error:
            self._reply(False, f"读不到 {filename}（{error}）")
            return
        # ★ 顺手把「服务端现在实际用的是哪一份」也报出来：文件坏了的时候
        #   `shopcfg` 会**保留上一份好的**（D10），不说的话用户会以为
        #   自己刚存的那份已经生效了。
        _parsed, warnings = shopcfg._load(filename)
        self._send_json({"ok": True, "text": text, "warnings": warnings,
                         "path": path})

    def _admin_config_post(self, which, data):
        name = self._require_admin()
        if name is None:
            return
        filename = CONFIG_FILES.get(which)
        if filename is None:
            self._reply(False, f"没有名为 {which!r} 的配置", status=404)
            return
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            self._reply(False, "内容是空的，没有保存")
            return
        if len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
            self._reply(False, "内容太大了，没有保存")
            return
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as error:
            # 行号列号照原样报出去 —— 编辑几百行 JSON 时这是最有用的一句话。
            self._reply(False, f"不是合法的 JSON：第 {error.lineno} 行"
                               f"第 {error.colno} 列 {error.msg}")
            return
        try:
            CONFIG_VALIDATORS[which](parsed)
        except shopcfg.ConfigError as error:
            # ★ **校验不过就不落盘。** 存一份坏文件下去，服务端会退回上一份
            #   好的继续跑（D10）—— 用户会以为改生效了，实际没有。
            self._reply(False, f"校验没过，没有保存：{error}")
            return
        # ★ 只写 `format` + 那一个列表（D16）。以前存的是「解析后的整个对象」，
        #   `_说明` 那种注释键会原样留下 —— 那几句话是写给**手改 json 的人**看的，
        #   而现在唯一的编辑入口就是这个页面，说明已经画在面板上了
        #   （`shopcfg.SCHEMA[...]["help"]`）。⇒ 老文件里的 `_说明`
        #   会在第一次保存时消失，这是用户拍板要的。
        list_key = shopcfg.SCHEMA[which]["list_key"]
        shopcfg.write_json(shopcfg.path_of(filename), {
            "format": shopcfg.FORMAT,
            list_key: parsed.get(list_key, []),
        })
        # 热重载本来靠 mtime，但 mtime 的粒度可能粗到看不出这一次改动
        # —— 存完直接把缓存丢掉，下一次读一定是新的。
        shopcfg.invalidate()
        eventlog.online(f"[admin] {name!r} 保存了 {filename}")
        self._send_json({"ok": True,
                         "message": f"已保存 {filename}，即刻生效（不用重启）"})

    def _admin_manage(self, action, data):
        name = self._require_admin()
        if name is None:
            return
        target = str(data.get("name") or "").strip()
        if action == "add":
            names = self.accounts.admin_add(target, data.get("password"))
            eventlog.online(f"[admin] {name!r} 添加了管理员 {target!r}")
            self._send_json({"ok": True, "message": f"已添加管理员 {target}",
                             "names": names})
            return
        if action == "password":
            self.accounts.admin_set_password(target, data.get("password"))
            # ★ 改完口令要把那个人**已有的会话全部作废** —— 否则「我把密码
            #   改了」和「拿着旧密码登进来的人还在操作」会同时成立。
            self.admin_sessions.drop_admin(target)
            eventlog.online(f"[admin] {name!r} 改了 {target!r} 的口令")
            logged_out = (target == name)
            self._send_json({
                "ok": True, "logged_out": logged_out,
                "message": ("已改口令" + ("，请用新口令重新登录" if logged_out
                                          else f"（{target} 需要重新登录）"))})
            return
        if action == "remove":
            # 「至少保留一个管理员」拦在 `account_store` 层（不只前端），
            # 这里直接让它抛 `AccountError`，宿主的 `do_POST` 会转成友好提示。
            names = self.accounts.admin_remove(target)
            self.admin_sessions.drop_admin(target)
            eventlog.online(f"[admin] {name!r} 删除了管理员 {target!r}")
            self._send_json({"ok": True, "message": f"已删除管理员 {target}",
                             "names": names, "logged_out": target == name})
            return
        self._reply(False, "没有这个操作", status=404)

    def _admin_item_lookup(self, query):
        """按 itemId 查一件东西。省得对着 7 位数字猜这是啥。"""
        if self._require_admin() is None:
            return
        raw = (urllib.parse.parse_qs(query or "").get("id") or [""])[0]
        try:
            item_id = int(raw)
        except ValueError:
            self._reply(False, "物品 id 要是一个整数")
            return
        item = shopdata.get(item_id)
        if item is None:
            self._reply(False, f"物品表里没有 {item_id}"
                               "（中文版客户端不认识它，发下去是个空格子）")
            return
        table, _warnings = shopcfg.shop()
        entry = table.get(item_id) or {}
        self._send_json({
            "ok": True,
            "id": item.id,
            "kind": item.kind,
            "name": entry.get("name") or item.name_kr or "",
            "name_kr": item.name_kr or "",
            "desc": shopcfg.item_desc_zh(item),
            "part_flag": item.part_flag,
            "character": item.character,
            "ownable": item.ownable,
            "listed": bool(entry.get("listed")),
            "price": entry.get("price"),
            "bonus": item.bonus,
            "weapon": item.weapon,
        })

    # ------------------------------------------------------------ 玩家资料
    def _admin_player_search(self, query):
        """`/admin/api/players?q=…&page=N` —— 按用户名或昵称找人，一页 10 行。"""
        if self._require_admin() is None:
            return
        fields = urllib.parse.parse_qs(query or "")
        raw = (fields.get("q") or [""])[0]
        try:
            page = max(0, int((fields.get("page") or ["0"])[0]))
        except ValueError:
            page = 0
        found, total = self.accounts.search_accounts(
            raw, limit=PLAYER_PAGE_SIZE, offset=page * PLAYER_PAGE_SIZE)
        pages = max(1, -(-total // PLAYER_PAGE_SIZE))     # 向上取整
        if not found and page >= pages:
            # 翻过了头（删号 / 换了查询串之后还停在第 5 页）：退回最后一页，
            # 而不是回一张空表让人以为「没有这个人」。
            page = pages - 1
            found, total = self.accounts.search_accounts(
                raw, limit=PLAYER_PAGE_SIZE, offset=page * PLAYER_PAGE_SIZE)
        online = _online_usernames()
        self._send_json({
            "ok": True,
            "page": page,
            "pages": pages,
            "size": PLAYER_PAGE_SIZE,
            "total": total,
            "players": [{
                "username": username,
                "nickname": account_store.display_name(account),
                "level": account_store.player_level(account),
                "money": account_store.player_money(account),
                "online": username in online,
            } for username, account in found],
        })

    def _admin_player_get(self, query):
        """`/admin/api/player?name=…` —— 一个玩家的可编辑资料。"""
        if self._require_admin() is None:
            return
        username = (urllib.parse.parse_qs(query or "").get("name") or [""])[0]
        _name, account = self.accounts.get_account(username)
        if account is None:
            self._reply(False, f"没有叫 {username!r} 的账号", status=404)
            return
        self._send_json({"ok": True, "player": _player_view(username, account)})

    def _admin_player_save(self, data):
        """`POST /admin/api/player` —— 改等级 / 金币 / 材料 / 非商店物品。

        ★ **商店上架的东西（`shop.json` 里 `listed`）一律拒绝**（用户
        2026-09-05 拍板）：那批物品带着等级门槛，直接塞进仓库就绕过了
        「够等级才买得到」这条规则。要给就改金币和等级，让玩家自己进商店买。
        """
        admin = self._require_admin()
        if admin is None:
            return
        username = str(data.get("name") or "").strip()
        _name, account = self.accounts.get_account(username)
        if account is None:
            self._reply(False, f"没有叫 {username!r} 的账号", status=404)
            return
        table, _warnings = shopcfg.shop()
        try:
            materials = _counts_of(data.get("materials"))
            inventory = _counts_of(data.get("inventory"))
            level = _optional_int(data.get("level"), "等级")
            money = _optional_int(data.get("money"), "金币")
        except ValueError as error:
            self._reply(False, str(error))
            return
        locked = sorted(item_id for item_id in
                        list(materials) + list(inventory)
                        if (table.get(item_id) or {}).get("listed"))
        if locked:
            self._reply(False,
                        "这些是商店在卖的东西，不能直接改（改金币和等级，"
                        "让玩家自己进商店买）：" + "、".join(
                            f"{i} {(table.get(i) or {}).get('name') or ''}".strip()
                            for i in locked))
            return
        # ★ 装备类只有「有 / 没有」，数量没有意义（见 `stackable()`）：
        #   **有无没变就整条从补丁里拿掉**，有无变了才写 1 / 0。
        #
        #   为什么不是简单地夹成 0/1：老存档里可能躺着 ×2（早先用控制通道的
        #   `give` 发过两次）。夹的话，管理员只是点开看一眼再按保存，就会
        #   多出一行「物品 1010004 ×2 -> ×1」—— 一个客户端根本不读的数字，
        #   却让人以为自己改了什么。
        owned_before = account_store.inventory_items(account)
        for item_id in list(inventory):
            if stackable(item_id):
                continue
            want = 1 if inventory[item_id] else 0
            if want == (1 if item_id in owned_before else 0):
                del inventory[item_id]
            else:
                inventory[item_id] = want
        account, changes = self.accounts.admin_update_account(
            username, level=level, money=money,
            materials=materials, inventory=inventory)
        pushed = _push_account(username)
        if changes:
            eventlog.online(f"[admin] {admin!r} 改了玩家 {username!r}: "
                            + "；".join(changes))
        if not changes:
            message = "没有任何改动"
        else:
            message = "已保存：" + "；".join(changes)
            message += ("；玩家在线，已即时推给客户端"
                        if pushed else "；玩家不在线，下次登录生效")
        self._send_json({"ok": True, "message": message,
                         "changes": changes, "pushed": pushed,
                         "player": _player_view(username, account)})
