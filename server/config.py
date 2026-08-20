#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``server.config`` 的解析器。

**单机假服务器和云端服务端共用同一个文件名、同一份解析器**（CLAUDE.md 铁律 8）。
差异只体现在「哪一边读哪几个键」上：

    客户端包  server_address / server_register_port  -> 选「远程服务器」时连谁、注册页在哪
              proxy_*                                -> 远程 TCP 连接是否经代理
              local_register_port                    -> 「本机服务器」的注册页监听哪个端口
    服务端包  local_register_port                    -> 注册页监听哪个端口

认证服（47611）和游戏服（27799）的端口是客户端写死的，**不可配置**；
监听地址固定 ``::``（IPv6 双栈，IPv4 也能连进来），**也不做成配置项**（D063）。

解析规则刻意宽松 —— 这个文件是给普通玩家用记事本改的：

* ``key = value``，``#`` 或 ``;`` 起头的整行是注释
* 键名大小写不敏感、两侧空白忽略
* **缺键用默认值，多余的键只警告不报错**（老版本配置文件不能让新版服务端起不来）
* 行尾的 CR 一律吃掉（Windows 记事本存的是 CRLF，Linux 上照样要能读）
"""
from __future__ import annotations

import os


#: 认证服端口。客户端硬编码（V0.1 §24：原服 222.73.1.42:47611），不可配置。
AUTH_PORT = 47611
#: 游戏服端口。客户端硬编码（V0.1 §40：原服 222.73.209.12:27799），不可配置。
GAME_PORT = 27799
#: 调试控制通道（tools/gs_ctl.py 连它）。**只绑 127.0.0.1**，服务端包里默认关。
CONTROL_PORT = 27800

#: 原版 TCP 中继（rcp 协议）的服务端监听端口。**这个号是我们定的** ——
#: 客户端连哪儿完全由 `0x0210 gspJoinRelay` 里的 `NetAddress` 说了算（§157），
#: 原版那个地址早就随停运的服务器一起没了。挑 27798 只为紧挨着游戏服 27799，
#: 端口表好记（27799 -> 27809 中继，27798 -> 27808 中继，同一套 +10 的规律）。
PEER_RELAY_PORT = 27798

#: ★ 位置同步的 **UDP** 通道（`server/udpsync.py`）。**故意和游戏服 TCP 同号** ——
#: TCP 和 UDP 是两套独立的端口空间，27799/tcp 和 27799/udp 互不冲突，而玩家只要
#: 记住「27799 两种协议都放行」，防火墙说明少一行、少一个记错的机会。
#: 和 47611 / 27799 / 27798 一样**不做成配置项**：两端要对得上，可配等于多一种
#: 「一边改了一边没改」的连不上（D063 的同一条理由）。
UDP_SYNC_PORT = GAME_PORT

#: 选「远程服务器」时本机 UDP 中继监听的端口（`bshook` 把位置数据镜像到这儿）。
#: 同样和游戏服中继 27809 同号，理由同上。
RELAY_UDP_SYNC_PORT = 27809

#: 原版客户端 `UDPBinder` 写死要 bind 的端口（`0x5bba92(0x1e6c)` = 7788，
#: FINDINGS §153）。**我们不用它** —— 见下面 `CLIENT_UDP_PORT`。
GAME_ORIGINAL_UDP_PORT = 7788

#: 客户端接收位置数据的 UDP 端口。`bshook` 钩住 `ws2_32!bind`，把原版那个
#: 写死的 7788 改写成这个号。
#:
#: ★ **为什么非改不可**：下行要把数据投进这个口，而「这个口到底是不是游戏在听」
#: 必须是确定的，不能靠猜。7788 是个谁都可能占的低位号（原版选它的年代没这个
#: 顾虑），一旦被别的程序占着，位置数据就投进黑洞 —— 表现是**所有人在你屏幕上
#: 定住**，比不开这个功能还糟。
#:
#: 换成我们自己的号之后判据变成硬的：**启动脚本先检查它空着**（占用就直接报错
#: 不启动），然后由 `bshook` 让游戏去 bind 它，bind 成功再由 `bshook` 告诉本机
#: 中继「可以往这儿投了」。三步都是确定的，没有一步靠推测。
#:
#: 收包回调 `0x407869` 和 `0x040f` 是同一个入口，所以走 UDP 送进去和走 TCP
#: 送进去，客户端处理起来一个字节的差别都没有。
CLIENT_UDP_PORT = 27807

#: 选「远程服务器」时本机中继的监听端口。客户端的 `connect` 被 bshook 改写到这三个口，
#: 由 `server/relay.py` 转发到 `server_address` 的 47611 / 27799 / 27798
#: （D065 / D066 / D079）。
#: ★ 故意和 47611 / 27799 / 27798 错开：本机服务端和中继两个后端各听各的，模式判定零状态。
RELAY_AUTH_PORT = 47621
RELAY_GAME_PORT = 27809
RELAY_PEER_PORT = 27808

#: 注册网页的默认端口。
DEFAULT_REGISTER_PORT = 27810

#: ★★ **端口号的唯一来源就是上面这些常量。** 别的地方一律派生，不许再抄一份。
#:
#: 这个项目里要用到同一个端口号的地方有四类，语言各不相同：
#:
#: | 谁 | 怎么拿到 |
#: |---|---|
#: | Python（服务端 / 中继） | 直接 `import config` |
#: | C（`hook/bshook.c`） | `hook/ports.h` —— 由 `tools/gen_ports_h.py` 从本文件生成 |
#: | PowerShell（启动脚本） | `python server/config.py --ports` |
#: | sh（Linux 启动脚本） | 同上 |
#:
#: 以前是各写各的（`bshook.c` 11 个、`launch.ps1` 9 个、`serverctl.*` 各 3 个），
#: 靠环境变量在运行时对齐 —— 那既是重复劳动，也是一类**改一半**的故障：
#: 改了这边没改那边，症状往往是「某个功能悄悄不工作」而不是报错。
#: 现在改一处就够了，`test_ports.py` 会盯着 `ports.h` 有没有跟上。
PORT_EXPORTS = (
    "AUTH_PORT",
    "GAME_PORT",
    "CONTROL_PORT",
    "PEER_RELAY_PORT",
    "UDP_SYNC_PORT",
    "RELAY_AUTH_PORT",
    "RELAY_GAME_PORT",
    "RELAY_PEER_PORT",
    "RELAY_UDP_SYNC_PORT",
    "GAME_ORIGINAL_UDP_PORT",
    "CLIENT_UDP_PORT",
    "DEFAULT_REGISTER_PORT",
)


def port_table():
    """``{常量名: 端口号}``。给生成器和启动脚本用。"""
    return {name: globals()[name] for name in PORT_EXPORTS}

#: 一次成功注册之后，**同一个客户端 IP** 要等多久才能再注册（秒）。
#: 前台按钮的倒计时和后台的 IP 限制**共用这一个值**（需求明确要求一致）。
#: `0` = 完全不限制。
#: ★ 2026-08-14 由 60 改成 20（用户拍板，D111）：60 秒挡住脚本的效果和 20 秒
#: 差不多，而「一家人轮流注册」这种正常场景等 60 秒太难受。
DEFAULT_REGISTER_COOLDOWN_SECONDS = 20

#: 冷却时间的上限。24 小时已经远超「防批量注册」需要的量级，
#: 再大多半是把毫秒填进来了 —— 与其让整台服务器一天不能注册，不如当填错处理。
MAX_REGISTER_COOLDOWN_SECONDS = 86400

#: 日志自动清理：`logs/` 里超过这么多天没动过的日志文件会被删掉。
#: `0` = 不清理。触发时机见 `logcleanup.py`（服务端每次**真的启动**时一次 +
#: 每天凌晨 4 点一次）。
DEFAULT_LOG_RETENTION_DAYS = 3

#: 保留天数的上限。10 年 —— 再大就等于「不清理」，那该填 0 而不是填 99999。
MAX_LOG_RETENTION_DAYS = 3650

#: 每个 UDP 数据报最多捎带几份**历史**位置包（0 = 只发当前这一份）。
#: 上限 4：位置包只有 43 字节，捎 4 份也才 ~250 字节/报，再多就纯属浪费上行了。
MAX_UDP_SYNC_REDUNDANCY = 4

#: 配置文件名。放在包根目录（= `start.bat` 同目录 = `server/` 的上一级）。
CONFIG_FILENAME = "server.config"

#: ★ `server_address` 的默认值是一个**局域网示例地址**，不是 `127.0.0.1`。
#: 填 `127.0.0.1` 的话「远程服务器」和「本机服务器」连的是同一台机器，
#: 这个选项就等于没有 —— 玩家看到 `192.168.1.100` 才知道这里该填别人的地址。
#: ⚠ 改这里就要同步改下面 `DEFAULT_CONFIG_TEXT` 里的那一行
#: （`test_the_shipped_template_parses_to_the_defaults` 会钉住这一点）。
DEFAULTS = {
    "server_address": "192.168.1.100",
    "server_register_port": DEFAULT_REGISTER_PORT,
    "local_register_port": DEFAULT_REGISTER_PORT,
    # 地址为空就是关闭。旧版 server.config 没有这些键时也因此保持直连。
    "proxy_type": "socks5",
    "proxy_address": "",
    "proxy_port": 1080,
    "proxy_username": "",
    "proxy_password": "",
    "register_cooldown_seconds": DEFAULT_REGISTER_COOLDOWN_SECONDS,
    "log_retention_days": DEFAULT_LOG_RETENTION_DAYS,
    # ★ 位置数据走 UDP 的总开关（1 = 开）。关掉就是 2026-08-19 之前的行为：
    #   全部同步数据走 TCP。留着它主要是给**对照测试**用 —— 「开/关各打一局，
    #   比 online.log 里的到达间隔」是判断这套东西到底有没有用的唯一办法。
    "udp_sync": 1,
    "udp_sync_redundancy": 2,
}

#: 值要按**端口**解析的键（1~65535）。
_PORT_KEYS = ("server_register_port", "local_register_port", "proxy_port")

#: 值要按**秒数**解析的键（0 ~ MAX_REGISTER_COOLDOWN_SECONDS，0 = 关闭）。
#: 和端口分开是因为两者的合法区间不一样：秒数允许 0，端口不允许。
_SECOND_KEYS = ("register_cooldown_seconds",)

#: 值要按**天数**解析的键（0 ~ MAX_LOG_RETENTION_DAYS，0 = 关闭）。
_DAY_KEYS = ("log_retention_days",)

#: 值要按**开关**解析的键（0/1；也认 on/off、true/false、yes/no）。
_FLAG_KEYS = ("udp_sync",)

#: 值要按**冗余份数**解析的键（0 ~ MAX_UDP_SYNC_REDUNDANCY，0 = 不捎带）。
_REDUNDANCY_KEYS = ("udp_sync_redundancy",)


SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
PACKAGE_ROOT = os.path.dirname(SERVER_DIR)


def config_path(root: str | None = None) -> str:
    """``server.config`` 的完整路径（包根目录下）。"""
    return os.path.join(os.path.abspath(root or PACKAGE_ROOT), CONFIG_FILENAME)


def _clean_port(value, key, warnings):
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        warnings.append(f"{key} 不是数字（{value!r}），改用默认值 {DEFAULTS[key]}")
        return DEFAULTS[key]
    if not (1 <= port <= 65535):
        warnings.append(f"{key} 超出 1~65535（{port}），改用默认值 {DEFAULTS[key]}")
        return DEFAULTS[key]
    return port


def _clean_count(value, key, warnings, maximum):
    """「0 = 关掉这项功能」的计数值：允许 0，但不接受负数和离谱的大数。

    秒数（注册冷却）和天数（日志保留）用的是同一套规则，只有上限不同。
    """
    try:
        count = int(str(value).strip())
    except (TypeError, ValueError):
        warnings.append(f"{key} 不是数字（{value!r}），改用默认值 {DEFAULTS[key]}")
        return DEFAULTS[key]
    if not (0 <= count <= maximum):
        warnings.append(f"{key} 超出 0~{maximum}"
                        f"（{count}），改用默认值 {DEFAULTS[key]}")
        return DEFAULTS[key]
    return count


#: 开关键认得的写法。玩家是用记事本改这个文件的，只认 `0`/`1` 太苛刻了。
_TRUE_WORDS = frozenset(("1", "on", "true", "yes", "y", "开", "是"))
_FALSE_WORDS = frozenset(("0", "off", "false", "no", "n", "关", "否"))


def _clean_flag(value, key, warnings):
    """开关键 -> `0` / `1`。认不出来的写法一律用默认值 + 一条警告。"""
    word = str(value).strip().lower()
    if word in _TRUE_WORDS:
        return 1
    if word in _FALSE_WORDS:
        return 0
    warnings.append(f"{key} 只认 1/0（或 on/off、true/false），"
                    f"填的是 {value!r}，改用默认值 {DEFAULTS[key]}")
    return DEFAULTS[key]


def parse_text(text: str):
    """配置文本 -> ``(配置字典, 警告列表)``。

    警告是给日志用的，**任何一条警告都不会让解析失败** —— 配置写错了就用默认值
    继续跑，总好过服务端起不来、玩家看不到任何提示。
    """
    values = dict(DEFAULTS)
    warnings = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip().lstrip("﻿")      # 记事本可能存成 UTF-8 with BOM
        if not line or line[0] in "#;":
            continue
        key, sep, value = line.partition("=")
        if not sep:
            warnings.append(f"第 {lineno} 行没有 '='，已忽略: {raw.strip()!r}")
            continue
        key = key.strip().lower()
        value = value.strip()
        # 值里再出现 '#' 一律当正文，不当行内注释 —— 密码/地址里可能有它。
        if key not in DEFAULTS:
            warnings.append(f"第 {lineno} 行是不认识的配置项 {key!r}，已忽略")
            continue
        if key in _PORT_KEYS:
            values[key] = _clean_port(value, key, warnings)
        elif key in _SECOND_KEYS:
            values[key] = _clean_count(value, key, warnings,
                                       MAX_REGISTER_COOLDOWN_SECONDS)
        elif key in _DAY_KEYS:
            values[key] = _clean_count(value, key, warnings,
                                       MAX_LOG_RETENTION_DAYS)
        elif key in _FLAG_KEYS:
            values[key] = _clean_flag(value, key, warnings)
        elif key in _REDUNDANCY_KEYS:
            values[key] = _clean_count(value, key, warnings,
                                       MAX_UDP_SYNC_REDUNDANCY)
        else:
            values[key] = value
    values["server_address"] = normalize_host(values["server_address"]) or \
        DEFAULTS["server_address"]
    # proxy_address 和 server_address 的关键区别：前者为空代表明确关闭，
    # 绝不能像后者一样补默认主机名。
    values["proxy_address"] = normalize_host(values["proxy_address"])
    return values, warnings


def load(path: str | None = None, root: str | None = None):
    """读 ``server.config``。文件不存在也不报错，直接返回默认值。

    返回 ``(配置字典, 警告列表)``。
    """
    path = path or config_path(root)
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            text = f.read()
    except FileNotFoundError:
        return dict(DEFAULTS), [f"没有找到 {path}，全部使用默认值"]
    except OSError as error:
        return dict(DEFAULTS), [f"读不了 {path}（{error}），全部使用默认值"]
    return parse_text(text)


def normalize_host(host) -> str:
    """把用户填的地址收拾成「不带方括号的裸主机名」。

    IPv6 用户很可能照着 URL 的样子写成 ``[2001:db8::1]``，
    而 `socket.getaddrinfo` 要的是不带方括号的形式。两种写法都收下。
    """
    host = str(host or "").strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1].strip()
    return host


def is_ipv6_literal(host) -> bool:
    """裸主机名看起来是不是 IPv6 字面量（用来决定 URL 里要不要加方括号）。

    只按「有没有冒号」判断就够了：域名和 IPv4 里都不会出现冒号，
    而任何合法的 IPv6 字面量至少有一个。
    """
    return ":" in normalize_host(host)


def http_host(host) -> str:
    """裸主机名 -> 能直接拼进 URL 的形式（IPv6 补方括号）。"""
    host = normalize_host(host)
    return f"[{host}]" if is_ipv6_literal(host) else host


def register_url(host, port) -> str:
    """注册页的 URL。普通 http 即可（需求明确说不用 https）。"""
    return f"http://{http_host(host)}:{int(port)}/"


#: `server.config` 的初始内容。打包脚本和 `app.py`（文件缺失时）都用它。
#:
#: ★ 写成 LF：这份文件要同时随 Windows 客户端包和 Linux 服务端包发布，
#:   Win10 的记事本从 1809 起就能正常显示 LF（本机是 19045）。
DEFAULT_CONFIG_TEXT = """\
# ============================================================================
#  炮炮火枪手 —— 服务器配置
#
#  只有在登录界面选择「远程服务器」时才会用到 server_address / server_register_port / proxy_*；
#  选「本机服务器」时只用 local_register_port。
# ============================================================================

# ---------------------------------------------------------------------------
# 远程服务器地址。IPv4 / IPv6 / 域名都支持，三选一填一个：
#
#   server_address = 192.168.1.100          <- 局域网里的另一台电脑（IPv4）
#   server_address = 2001:db8::1            <- IPv6（方括号加不加都行）
#   server_address = popshot.example.com    <- 域名
#
# ★ 下面这个 192.168.1.100 只是【示例地址】，多半不是你要连的那台机器。
#   在登录界面选「远程服务器」之前，请把它改成开服那台电脑真正的地址。
# ---------------------------------------------------------------------------
server_address = 192.168.1.100

# ---------------------------------------------------------------------------
# 远程服务器上「用户注册页」的端口号。
# 要和那台服务器自己的 server.config 里的 local_register_port 一致。
# ---------------------------------------------------------------------------
server_register_port = 27810

# ---------------------------------------------------------------------------
# 远程服务器代理（可选）。代理地址留空、删掉 proxy_address 这一行，或沿用没有
# proxy_* 的旧配置文件时，都会【直接连接】，不使用代理。
#
# 代理只影响登录界面选择「远程服务器」后的三条游戏 TCP 连接（认证 / 游戏 /
# 战斗中继），不影响「本机服务器」。注册链接由浏览器打开，是否走代理由浏览器或
# 系统自己的代理设置决定。
#
# proxy_type 支持 socks5 和 http（HTTP CONNECT）；代理地址支持 IPv4 / IPv6 / 域名。
# IPv6 地址请写成 proxy_address = 2001:db8::2 或 [2001:db8::2]。
# 代理需要账号密码时填写最后两项；不需要时保持为空。日志不会打印代理密码。
#
# 示例（SOCKS5）：
#   proxy_type = socks5
#   proxy_address = 127.0.0.1
#   proxy_port = 1080
#
# 示例（HTTP CONNECT）：
#   proxy_type = http
#   proxy_address = proxy.example.com
#   proxy_port = 8080
# ---------------------------------------------------------------------------
proxy_type = socks5
proxy_address =
proxy_port = 1080
proxy_username =
proxy_password =

# ---------------------------------------------------------------------------
# 本机「用户注册页」监听的端口号。
# 选「本机服务器」时点登录界面的注册链接，打开的就是 http://127.0.0.1:这个端口/
# 端口被别的程序占用时改这里。
# ---------------------------------------------------------------------------
local_register_port = 27810

# ---------------------------------------------------------------------------
# 注册冷却时间（秒）—— 防止有人拿脚本批量注册。
#
# 一次注册【成功】之后：
#   * 注册页上的「注册」按钮会锁住并倒计时这么多秒（刷新页面也一样锁着）；
#   * 服务器也会记住这个 IP，这段时间里它再来注册一律拒绝。
# 两边用的是同一个数字，改这里就都改了。
#
# 注册【失败】（用户名重复、昵称重复、两次密码不一致等）不算，按钮会立刻恢复可点。
# 填 0 = 完全不限制。IP 记录只放在内存里，服务端一重启就清空。
# ---------------------------------------------------------------------------
register_cooldown_seconds = 20

# ---------------------------------------------------------------------------
# 日志自动清理 —— logs\\ 目录里超过这么多天没再写过的日志文件会被删掉。
#
# 清理时机有两个：
#   * 服务端【每次真正启动】时一次（本机游玩经常开关，覆盖这种场景）；
#     启动脚本发现服务端已经在跑而跳过启动时，清理也跟着跳过。
#   * 每天【凌晨 4 点】一次（云服务器常年开机，覆盖这种场景）。
# 两次都在后台线程里做，不会影响正在进行的游戏。
#
# 清的是 logs\\ 里的日志文件（*.log / *.out / *.err / game_* / auth_* / conn_*），
# 判据是文件最后修改时间；正在写的日志因此不会被删。
# 填 0 = 不清理。
# ---------------------------------------------------------------------------
log_retention_days = 3

# ---------------------------------------------------------------------------
# 位置数据走 UDP（udp_sync）
#
# 战斗中「谁站在哪」这种数据每秒发 8 次，丢一份下一份 0.13 秒后就补上了，
# 本来不怕丢；怕的是 TCP 为了把丢的那一份重传回来，把后面已经到了的全都压在
# 队列里等 —— 跨境线路上实测会造成【停 0.4 秒、然后 3 份一起到】，每秒一次，
# 在别人屏幕上就是你的角色一跳一跳地瞬移。
#
# 开着（1）时：位置数据额外从 UDP 27799 走一份（上下行都走），谁先到用谁；
#             UDP 不通就还是 TCP，玩家什么都感觉不到。开火、命中、伤害、死亡
#             这些【不能丢】的数据任何时候都只走 TCP，不受这个开关影响。
# 关掉（0）时：完全等同 2026-08-19 之前的行为，全部走 TCP。
#
# ⚠ 服务器要放行 UDP 27799（和游戏服 TCP 同一个号，但防火墙要单独加一条 UDP 规则）。
#   没放行也不用改这里 —— 客户端探测不通会自己退回 TCP。
# ---------------------------------------------------------------------------
udp_sync = 1

# ---------------------------------------------------------------------------
# 每个 UDP 包捎带几份【历史】位置数据（0~4，0 = 不捎带）。
#
# 这不是为了画面更顺（位置是快照，新的自然覆盖旧的），而是为了在丢包时
# 把位置包的编号序列一发不漏地补齐 —— 客户端拿开局后【第一份】位置数据给
# 战斗事件队列定基线，那一份要是丢了，整局的开火判定都会错位。
# 网络特别差可以调到 3~4；局域网里玩可以填 0 省点流量。
# ---------------------------------------------------------------------------
udp_sync_redundancy = 2

# ---------------------------------------------------------------------------
# 说明：
#   * 认证服（47611）和游戏服（27799）的端口是客户端写死的，不需要也不能配置。
#   * 监听地址固定为 ::（IPv4 和 IPv6 都能连进来），不需要配置。
#   * 账号和密码在本项目里是【明文】保存和传输的，请不要使用其他网站用过的密码。
# ---------------------------------------------------------------------------
"""


def ensure_exists(path: str | None = None, root: str | None = None) -> str:
    """`server.config` 不存在就按模板生成一份，返回它的路径。

    不覆盖已有文件 —— 玩家改过的配置比我们的模板重要。
    """
    path = path or config_path(root)
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(DEFAULT_CONFIG_TEXT)
    return path


if __name__ == "__main__":
    # `python server/config.py --ports` -> 每行一个 `名字=端口号`。
    #
    # ★ 存在的理由只有一个：**PowerShell 和 sh 也要用这些端口号**（启动前的
    #   端口占用检查、停服时找进程、启动横幅）。让它们来问这一份，就不用
    #   在每个脚本里再抄一遍 —— 抄一遍就多一处「改了这边没改那边」。
    import sys as _sys

    if "--ports" in _sys.argv[1:]:
        for _name, _port in port_table().items():
            print(f"{_name}={_port}")
    else:
        print("用法: python config.py --ports", file=_sys.stderr)
        raise SystemExit(2)
