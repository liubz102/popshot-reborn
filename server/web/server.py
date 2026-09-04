#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注册网页 —— 用户注册 + 账号资料修改 + 存档转移助手。

纯标准库（`http.server`），零第三方依赖，页面在 `index.html` 里。
和认证服 / 游戏服跑在同一个进程、共用同一个 `AccountStore`（D064）。

路由：

    GET  /                页面本身（`index.html`，把服务器地址替换进去）
    POST /api/register    {username, password, password2, display_name,
                           skip_tutorial}                 -> {ok, message}
    POST /api/change-password
                          {username, old_password, new_password,
                           new_password2}                 -> {ok, message}
    POST /api/current-nickname
                          {username}                      -> {ok, message,
                                                             found, display_name?}
    POST /api/change-nickname
                          {username, old_password, display_name}
                                                            -> {ok, message}
    POST /api/export      {username, password}            -> {ok, message, save}
    POST /api/import      {username, password, save}      -> {ok, message}

★ `/admin` 开头的那一组（管理页，V0.3商店 M8）在 `web/admin.py` 里，
和这里**共用同一个端口、同一个 `Handler`**。路由清单见那个文件的开头。

★ 上传走 JSON 而不是 multipart：文件在浏览器里用 `FileReader` 读成文本、
`JSON.parse` 之后再发上来。这样服务端不用手写 multipart 解析，
也顺带把「这不是一个合法 JSON」挡在了客户端。

★ 页面上的服务器地址取自请求的 `Host` 头 —— 服务端不知道自己的公网地址 /
域名，玩家浏览器地址栏里的那个才是他真正连上的那台。
"""
from __future__ import annotations

import http.server
import ipaddress
import json
import math
import os
import socket
import socketserver
import sys
import threading
import time

if __name__ == "__main__":
    # 直接 `python server/web/server.py` 跑（只调注册页时很方便）时，`server/`
    # 不在 sys.path 上。★ 这一段必须在下面那些 import 之前 —— 放在文件末尾的
    # `__main__` 块里是没用的，模块级 import 早就先执行过了。
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from account_store import (AUTH_MESSAGES, AUTH_OK, NICKNAME_RULE_TEXT,
                           USERNAME_RULE_TEXT, AccountError, AccountStore)
import asynclog
import config as server_config
import eventlog
from netlisten import create_listener
#: 管理页 `/admin`（V0.3商店 M8）。和注册页共用本文件的 `Handler` 和端口。
#: ★ 直接跑 `python server/web/server.py` 时上面那段已经把 `server/` 补进
#:   `sys.path` 了，所以这里按顶层模块名 import（不是 `from web import`）。
if __package__:
    from . import admin
else:
    import admin

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(HERE, "index.html")

#: 请求体上限。注册表单几百字节，存档几 KB；给 1 MB 足够，
#: 又不至于让人一发请求就把服务端的内存吃掉。
MAX_BODY_BYTES = 1 << 20


def _as_ip(text):
    """一段文本 -> `ipaddress` 对象；不像 IP 就回 `None`。

    顺手处理三种真实会遇到的写法：`[2001:db8::1]`（带方括号）、
    `1.2.3.4:5678`（有些代理会带端口）、`::ffff:1.2.3.4`（v4-mapped，
    必须还原成 IPv4，否则和名单里的 `127.0.0.1` 对不上）。
    """
    text = str(text or "").strip()
    if text.startswith("[") and "]" in text:
        text = text[1:text.index("]")]
    elif text.count(":") == 1:              # 只有一个冒号 ⇒ IPv4:port
        text = text.split(":", 1)[0]
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return None
    return ip.ipv4_mapped or ip if ip.version == 6 else ip


def _is_infrastructure(ip):
    """这个地址是不是「只可能是我们自己那一跳」的地址。

    环回 / 私网（10、172.16-31、192.168）/ 链路本地 / 未指定 —— 这些地址
    **不可能是从公网直接连过来的客户端**，所以对端是它们时，前面多半真的
    有一层反向代理（frp / nginx / docker 网桥都落在这几段里）。
    """
    return ip is not None and (ip.is_loopback or ip.is_private
                               or ip.is_link_local or ip.is_unspecified)


def resolve_client_ip(peer_addr, headers):
    """真实客户端 IP -> ``(IP 字符串, 是不是从转发头里取的)``。

    优先级（**不需要任何配置**）：

    1. ``X-Forwarded-For``
    2. ``X-Real-IP``
    3. TCP 对端（`self.client_address`，也就是 Node 那边的
       `connection.remoteAddress` / `socket.remoteAddress`）

    —— 但 1 和 2 **只在 TCP 对端是环回/私网地址时才看**，这一条是整个函数的
    要害，不能省：

    * `X-Forwarded-For` 是**客户端自己就能写**的普通 HTTP 头。无条件采信它，
      一行 `curl -H "X-Forwarded-For: 随便一个IP"` 就能每次换一个限流桶，
      按 IP 的限制直接变成摆设 —— 而这恰恰是它要防的那种脚本。
    * 但如果对端是 `127.0.0.1` / `192.168.x` 这类地址，那**根本不可能**是
      从公网直接连过来的人，只可能是我们自己前面那层 frp / nginx / docker。
      这时候的转发头是那一跳写的，可以信。
    * 于是：公网直连 ⇒ 只认 TCP 对端（伪造无效）；藏在代理后面 ⇒ 认转发头
      （拿到真实玩家 IP）。两种部署各自都是对的，**一个配置项都不用填**。

    ⚠ 代价说清楚：服务器**直接**开在局域网里给同一个网段的人玩时，那些人的
    对端也是私网地址 ⇒ 他们伪造的 `X-Forwarded-For` 会被采信。那是「同一个
    局域网里的熟人绕过 60 秒冷却」，和这条限制要防的「公网上的批量注册脚本」
    不是一回事，接受。

    ★ `X-Forwarded-For` 里**从右往左**找第一个公网地址。方向不能反：
    每一跳代理都是把「它看到的对端」**追加**到链尾（nginx 的
    `$proxy_add_x_forwarded_for` 就是这么干的），所以链的右边是我们自己人
    写的、左边才是客户端能塞进去的。从左往右取（最常见的写法）等于直接
    采信客户端伪造的那一截 —— 客户端只要先塞一个假 IP 进来就赢了。
    """
    direct = eventlog.host(peer_addr)
    if not _is_infrastructure(_as_ip(direct)):
        return direct, False            # 公网直连：转发头一律不看
    chain = []
    for value in headers.get_all("X-Forwarded-For") or ():
        chain.extend(str(value).split(","))
    parsed = [ip for ip in (_as_ip(part) for part in chain) if ip is not None]
    for ip in reversed(parsed):
        if not _is_infrastructure(ip):
            return str(ip), True
    real = _as_ip(headers.get("X-Real-IP"))
    if real is not None and not _is_infrastructure(real):
        return str(real), True
    # 链上全是内网地址（代理漏配了转发头 / 代理和客户端都在内网）。
    # 有解析出来的就用最左边那个，否则退回 TCP 对端。
    if parsed:
        return str(parsed[0]), True
    return direct, False


class RegisterRateLimiter:
    """按客户端 IP 限制注册频率 —— 防止有人拿脚本批量注册。

    ★ **只在内存里**（用户明确要求不落盘）：一个 `{IP: 解禁时刻}` 的字典，
    服务端一重启就全清。表只会在 `_prune` 里变小，长期开服不会一直涨。

    ★ **只有注册成功才记一笔。** 重名、两次密码不一致、用户名不合法这些
    失败**不锁人** —— 打错一个字就要罚等一分钟，会把正常玩家挡在门外，
    而批量注册的脚本要的是**成功**，锁成功那一侧就够了。

    冷却秒数和前台按钮倒计时是**同一个值**（`server.config` 的
    `register_cooldown_seconds`），`0` = 关掉这项限制。

    `clock` 只为测试留：默认 `time.monotonic`（不受系统改时间影响）。
    """

    def __init__(self, cooldown=server_config.DEFAULT_REGISTER_COOLDOWN_SECONDS,
                 clock=time.monotonic):
        self.cooldown = max(0, int(cooldown))
        self._clock = clock
        self._until = {}
        self._lock = threading.Lock()

    def retry_after(self, host):
        """这个 IP 还要等几秒才能再注册。`0` = 现在就可以。"""
        if self.cooldown <= 0:
            return 0
        now = self._clock()
        with self._lock:
            self._prune(now)
            deadline = self._until.get(host)
        if deadline is None:
            return 0
        # 向上取整：还剩 0.2 秒时说「还需 1 秒」，别说「还需 0 秒」却仍然拒绝。
        return max(0, int(math.ceil(deadline - now)))

    def mark(self, host):
        """记下「这个 IP 刚成功注册过」，返回它要等的秒数。"""
        if self.cooldown <= 0:
            return 0
        now = self._clock()
        with self._lock:
            self._prune(now)
            self._until[host] = now + self.cooldown
        return self.cooldown

    def _prune(self, now):
        """清掉已经到期的条目。**调用方持锁。**"""
        for key in [k for k, deadline in self._until.items() if deadline <= now]:
            del self._until[key]


def render_index(server_address, cooldown=0):
    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        html = f.read()
    return (html
            .replace("__SERVER_ADDRESS__", _escape(server_address))
            .replace("__USERNAME_RULE__", _escape(USERNAME_RULE_TEXT))
            .replace("__NICKNAME_RULE__", _escape(NICKNAME_RULE_TEXT))
            # 页面上的倒计时长度。整数，直接进 JS 字面量，不用转义也转不出花来。
            .replace("__REGISTER_COOLDOWN__", str(max(0, int(cooldown)))))


def _escape(text):
    """`Host` 头是外部输入，插进 HTML 前必须转义。"""
    return (str(text)
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


class Handler(admin.AdminRoutes, http.server.BaseHTTPRequestHandler):
    """注册页 + 管理页共用的一个 Handler。

    ★ 管理页那一半在 `web/admin.py` 里（`AdminRoutes`）—— 它需要本类的
    `_reply` / `_send` / `client_ip()` 这些工具，混进来最省事；反过来，
    把管理页的十来个接口塞进本文件会让它长到看不动。
    """

    server_version = "PopShotWeb/0.2"
    protocol_version = "HTTP/1.1"

    #: 由 `serve()` 塞进来的共享账号存储。
    accounts: AccountStore = None

    #: 注册频率限制器（按 IP，只在内存里）。同样由 `make_server` 塞进来。
    limiter: RegisterRateLimiter = None

    # ------------------------------------------------------------ 客户端身份
    def client_ip(self):
        """这次请求真正的客户端 IP（挂在 frp / nginx 后面也对）。

        ★ 限流和日志**必须共用这一个** —— 两边口径不一样的话，
        日志里写着一个 IP、实际按另一个 IP 限流，出问题根本查不动。
        """
        return resolve_client_ip(self.client_address, self.headers)[0]

    def client_label(self):
        """日志用的一段：直连写 `ip:port`，经代理写 `真实IP（经 代理IP）`。"""
        ip, forwarded = resolve_client_ip(self.client_address, self.headers)
        if not forwarded:
            return eventlog.peer(self.client_address)
        return f"{ip}（经 {eventlog.host(self.client_address)}）"

    # -------------------------------------------------------------- 工具
    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # 注册页只有一个人在看，缓存只会让「注册成功了吗」变得可疑。
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload, status=200):
        self._send(status, json.dumps(payload, ensure_ascii=False))

    def _reply(self, ok, message, status=200, **extra):
        self._send_json(dict(ok=ok, message=message, **extra), status)

    def _read_body(self):
        """把请求体整个读出来。

        ★ **不管这个路径认不认识，都必须先把 body 读干净。**
        我们是 HTTP/1.1 + keep-alive：直接对着一个没读过 body 的请求回 404，
        剩在缓冲里的那段 body 会被当成下一个请求的请求行去解析，
        连接随即被服务端掐掉 —— 客户端看到的是
        `ConnectionAbortedError [WinError 10053]`，而且时有时无。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > MAX_BODY_BYTES:
            raise ValueError("请求体过大")
        return self.rfile.read(length)

    @staticmethod
    def _as_bool(value, default=False):
        """把 JSON 里传来的开关值收成布尔。

        页面发的是真正的 `true`/`false`，但手搓请求的人很容易发字符串
        `"false"` —— 而 `bool("false")` 是 `True`，那种「勾都没勾却跳过了教程」
        的误会最难查。字段缺失时回 `default`。
        """
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() not in ("", "0", "false", "no", "off")
        return bool(value)

    @staticmethod
    def _parse_json(raw):
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"请求体不是合法的 JSON：{error}") from None
        return data if isinstance(data, dict) else {}

    def log_message(self, fmt, *args):
        # 默认实现往 stderr 写 Apache 风格的行，和服务端其它日志格式不一致。
        # ★ 绝不打 query string / 请求体 —— 密码就在里面（D067）。
        asynclog.emit(f"[web] {self.address_string()} {fmt % args}")

    # -------------------------------------------------------------- 路由
    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path in ("/", "/index.html"):
            host = self.headers.get("Host") or "localhost"
            self._send(200, render_index(host, self.limiter.cooldown),
                       "text/html; charset=utf-8")
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        # 管理页（V0.3商店 M8）和注册页**共用这一个端口**。它自己认得
        # `/admin` 开头的全部路径（认不出的也回 404），所以放在兜底之前。
        try:
            if self.admin_get(path, query):
                return
        except Exception as error:                      # 兜底，别让线程死掉
            self.log_message("管理页 %s 出错: %r", path, error)
            self._reply(False, "服务器内部错误，请看服务端日志", status=500)
            return
        self._send(404, "404 找不到这个页面", "text/plain; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            raw = self._read_body()      # 先读干净，再谈路由（见 _read_body）
        except ValueError as error:
            self.close_connection = True
            self._reply(False, str(error), status=400)
            return
        route = {
            "/api/register": self._api_register,
            "/api/change-password": self._api_change_password,
            "/api/current-nickname": self._api_current_nickname,
            "/api/change-nickname": self._api_change_nickname,
            "/api/export": self._api_export,
            "/api/import": self._api_import,
        }.get(path)
        is_admin = path.startswith("/admin")
        if route is None and not is_admin:
            self._reply(False, "没有这个接口", status=404)
            return
        try:
            data = self._parse_json(raw)
        except ValueError as error:
            self._reply(False, str(error), status=400)
            return
        try:
            if is_admin:
                self.admin_post(path, data)
            else:
                route(data)
        except AccountError as error:
            # 业务失败：状态码仍是 200，前台只看 ok 字段，浏览器控制台也干净。
            # ★ 管理页也走这一条 —— `admin_add` / `admin_remove` 那几个的
            #   拒绝理由（「至少要保留一个管理员」等）本来就是给人看的中文。
            self._reply(False, error.message)
        except Exception as error:                      # 兜底，别让线程死掉
            self.log_message("接口 %s 出错: %r", path, error)
            self._reply(False, "服务器内部错误，请看服务端日志", status=500)

    # -------------------------------------------------------------- 接口
    def _api_register(self, data):
        # ★ 频率限制放在**最前面**：被限住的 IP 连「这个用户名存不存在」
        #   都不该问得出来，否则限流就成了一个免费的账号枚举接口。
        client_ip = self.client_ip()
        wait = self.limiter.retry_after(client_ip)
        if wait > 0:
            eventlog.online(f"注册页 ✗ 注册被拦下（同一 IP 刚注册过）"
                            f" ip={client_ip} 还需 {wait} 秒")
            self._reply(False,
                        f"这个 IP 刚注册过账号，请等 {wait} 秒后再试。",
                        retry_after=wait)
            return
        username = data.get("username", "")
        password = data.get("password", "")
        password2 = data.get("password2", password)
        # 显示昵称：留空 = 用用户名。用户名重复和昵称重复由 `register()`
        # 分别检查、分别报错（需求要求两条路的提示能分得开）。
        display_name = data.get("display_name", "")
        # 缺字段 = 维持原版行为（走教程）。页面上那个框默认勾着，但它每次都显式发。
        skip_tutorial = self._as_bool(data.get("skip_tutorial"), False)
        if password != password2:
            self._reply(False, "两次输入的密码不一致，请重新输入。")
            return
        account = self.accounts.register(username, password,
                                         display_name=display_name,
                                         skip_tutorial=skip_tutorial)
        nickname = account["display_name"]
        # 只有真的建成了才开始计时（上面那些失败路径都不会走到这里）。
        cooldown = self.limiter.mark(client_ip)
        self.log_message("注册成功: %s (昵称=%s, 跳过新手教程=%s)",
                         username, nickname, skip_tutorial)
        eventlog.online(f"注册页 ✓ 新账号 账号={username!r} 昵称={nickname!r} "
                        f"ip={self.client_label()} "
                        f"跳过新手教程={'是' if skip_tutorial else '否'}"
                        + (f" 该 IP 冷却 {cooldown} 秒" if cooldown else ""))
        tail = ("首次登录会直接进大厅，不再强制新手教程。" if skip_tutorial
                else "首次登录会先带你走一遍新手教程。")
        self._reply(True,
                    f"注册成功！现在可以在游戏登录界面用「{username}」登录了，"
                    f"游戏里显示的昵称是「{nickname}」。{tail}",
                    retry_after=cooldown)

    def _api_change_password(self, data):
        username = str(data.get("username", "")).strip()
        old_password = data.get("old_password", "")
        new_password = data.get("new_password", "")
        new_password2 = data.get("new_password2", "")
        if new_password != new_password2:
            self._reply(False, "两次输入的新密码不一致，请重新输入。")
            return
        # ★ 用户明确要求：账号资料修改**不走注册频率限制**。这里不查询也不标记
        #   self.limiter；只做旧密码校验和账号存档的原子更新。
        self.accounts.change_password(username, old_password, new_password)
        self.log_message("修改密码成功: %s", username)
        eventlog.online(f"注册页 ✓ 修改密码 账号={username!r} "
                        f"ip={self.client_label()}")
        self._reply(True, "密码修改成功！下次登录请使用新密码。")

    def _api_current_nickname(self, data):
        username = str(data.get("username", "")).strip()
        _name, account = self.accounts.get_account(username)
        # 这是修改昵称框失焦时的只读查询：不校验密码，也不碰注册限流器。
        # `found` 单独表达是否存在，`ok` 则表示这次查询本身正常完成。
        if account is None:
            self._reply(True, "未查询到当前用户", found=False)
            return
        nickname = str(account.get("display_name") or username)
        self._reply(True, f"当前昵称: {nickname}",
                    found=True, display_name=nickname)

    def _api_change_nickname(self, data):
        username = str(data.get("username", "")).strip()
        old_password = data.get("old_password", "")
        display_name = data.get("display_name", "")
        # 同上：不碰 RegisterRateLimiter。昵称合法性和重名检查由 AccountStore
        # 在旧密码验证通过之后、同一把锁里完成。
        account = self.accounts.change_nickname(
            username, old_password, display_name)
        nickname = account["display_name"]
        self.log_message("修改昵称成功: %s (昵称=%s)", username, nickname)
        eventlog.online(f"注册页 ✓ 修改昵称 账号={username!r} 昵称={nickname!r} "
                        f"ip={self.client_label()}")
        self._reply(True,
                    f"昵称修改成功！游戏里将显示为「{nickname}」，重新登录后生效。")

    def _api_export(self, data):
        username = str(data.get("username", "")).strip()
        password = data.get("password", "")
        status, _account = self.accounts.verify(username, password)
        if status != AUTH_OK:
            self._reply(False, AUTH_MESSAGES[status])
            return
        save = self.accounts.export_account(username)
        self.log_message("导出存档: %s", username)
        self._reply(True, f"已导出「{username}」的存档，浏览器正在下载。", save=save)

    def _api_import(self, data):
        save = data.get("save")
        username, action = self.accounts.import_account(
            save, data.get("username", ""), data.get("password", ""))
        self.log_message("导入存档: %s (%s)", username, action)
        eventlog.online(f"注册页 ✓ 上传存档 账号={username!r} "
                        f"ip={self.client_label()} 结果={action}")
        # ★ 把服务器上**真正存下来的**数值回给他看一眼。
        #   「提示上传成功、结果服务器上的等级没变」这种事只能靠这个当场发现。
        _name, account = self.accounts.get_account(username)
        summary = (f"（当前等级 {account.get('level')}、"
                   f"经验 {account.get('experience')}、"
                   f"金币 {account.get('money')}）") if account else ""
        if action == "created":
            self._reply(True, f"上传成功！服务器上新建了账号「{username}」，"
                              f"现在可以在游戏登录界面用它登录了。{summary}")
        else:
            self._reply(True,
                        f"上传成功！账号「{username}」的存档已被覆盖更新。{summary}")


class _PreboundHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """socket 由 `netlisten.create_listener` 建好后交进来。

    `HTTPServer` 自己 bind 的话就绕不开「地址家族和地址必须对得上」那件事
    （`AF_INET6` + `127.0.0.1` 直接 `gaierror`），而双栈的判断逻辑已经在
    `netlisten` 里写过一遍了，不想再写第二遍。
    """

    daemon_threads = True

    def __init__(self, sock, handler_class):
        self.socket = sock
        socketserver.BaseServer.__init__(self, sock.getsockname()[:2],
                                         handler_class)
        self.server_name = socket.getfqdn(self.server_address[0])
        self.server_port = self.server_address[1]

    def server_bind(self):        # 已经绑好了
        pass

    def server_activate(self):    # 已经在 LISTEN 了
        pass


def make_server(port, accounts, host="::",
                cooldown=server_config.DEFAULT_REGISTER_COOLDOWN_SECONDS):
    """建好 HTTP 服务器但不开始服务，方便测试拿到真实端口。

    `cooldown` = 注册冷却秒数（`server.config` 的 `register_cooldown_seconds`）。
    默认值就是「开着」—— 漏传参数时应当**多限一点**而不是不限。
    """
    handler = type("BoundHandler", (Handler,),
                   {"accounts": accounts,
                    "limiter": RegisterRateLimiter(cooldown),
                    # 管理页的两个共享对象。★ 都**只在内存里** —— 会话随进程
                    # 走（重启即失效），限速表也一样（同 `RegisterRateLimiter`）。
                    "admin_sessions": admin.AdminSessions(),
                    "admin_limiter": admin.LoginRateLimiter()})
    return _PreboundHTTPServer(create_listener(host, port), handler)


def serve(port, accounts, host="::", ready=None,
          cooldown=server_config.DEFAULT_REGISTER_COOLDOWN_SECONDS):
    """阻塞地提供注册页服务。`app.py` 会把它丢进一个线程。"""
    httpd = make_server(port, accounts, host, cooldown)
    if ready is not None:
        ready.set()
    httpd.serve_forever()


def main():
    import argparse
    # 和 `app.py` 一样把 stdout/stderr 掰成 UTF-8。默认编码在中文 Windows 上是
    # GBK，日志里那个 `✓` 会当场抛 UnicodeEncodeError 并**打断正在处理的请求**
    # （实际踩过：注册成功了，但回包没发出去，浏览器重试后看到「用户名已存在」）。
    # 走 `app.py` 时它已经掰过了，这里只管独立跑的情形。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    ap = argparse.ArgumentParser(description="只跑注册页（调试用）")
    ap.add_argument("--port", type=int, default=27810)
    ap.add_argument("--host", default="::")
    ap.add_argument("--accounts", default=None)
    ap.add_argument("--register-cooldown", type=int, default=None,
                    help="注册冷却秒数。默认读 server.config 的 "
                         "register_cooldown_seconds；0 = 不限制")
    args = ap.parse_args()
    cooldown = args.register_cooldown
    if cooldown is None:
        cooldown = server_config.load()[0]["register_cooldown_seconds"]
    note = f"注册冷却 {cooldown} 秒" if cooldown else "注册冷却已关闭"
    asynclog.emit(f"注册页 http://127.0.0.1:{args.port}/（{note}）")
    serve(args.port, AccountStore(args.accounts), args.host, cooldown=cooldown)


if __name__ == "__main__":
    main()          # sys.path 已经在文件开头补过了
