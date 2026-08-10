#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注册网页 —— 用户注册 + 存档转移助手。

纯标准库（`http.server`），零第三方依赖，页面在 `index.html` 里。
和认证服 / 游戏服跑在同一个进程、共用同一个 `AccountStore`（D064）。

路由：

    GET  /                页面本身（`index.html`，把服务器地址替换进去）
    POST /api/register    {username, password, password2} -> {ok, message}
    POST /api/export      {username, password}            -> {ok, message, save}
    POST /api/import      {username, password, save}      -> {ok, message}

★ 上传走 JSON 而不是 multipart：文件在浏览器里用 `FileReader` 读成文本、
`JSON.parse` 之后再发上来。这样服务端不用手写 multipart 解析，
也顺带把「这不是一个合法 JSON」挡在了客户端。

★ 页面上的服务器地址取自请求的 `Host` 头 —— 服务端不知道自己的公网地址 /
域名，玩家浏览器地址栏里的那个才是他真正连上的那台。
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import socketserver

from account_store import (AUTH_MESSAGES, AUTH_OK, USERNAME_RULE_TEXT,
                           AccountError, AccountStore)
import eventlog
from netlisten import create_listener

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(HERE, "index.html")

#: 请求体上限。注册表单几百字节，存档几 KB；给 1 MB 足够，
#: 又不至于让人一发请求就把服务端的内存吃掉。
MAX_BODY_BYTES = 1 << 20


def render_index(server_address):
    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        html = f.read()
    return (html
            .replace("__SERVER_ADDRESS__", _escape(server_address))
            .replace("__USERNAME_RULE__", _escape(USERNAME_RULE_TEXT)))


def _escape(text):
    """`Host` 头是外部输入，插进 HTML 前必须转义。"""
    return (str(text)
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "PopShotWeb/0.2"
    protocol_version = "HTTP/1.1"

    #: 由 `serve()` 塞进来的共享账号存储。
    accounts: AccountStore = None

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
        print(f"[web] {self.address_string()} {fmt % args}", flush=True)

    # -------------------------------------------------------------- 路由
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            host = self.headers.get("Host") or "localhost"
            self._send(200, render_index(host),
                       "text/html; charset=utf-8")
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
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
            "/api/export": self._api_export,
            "/api/import": self._api_import,
        }.get(path)
        if route is None:
            self._reply(False, "没有这个接口", status=404)
            return
        try:
            data = self._parse_json(raw)
        except ValueError as error:
            self._reply(False, str(error), status=400)
            return
        try:
            route(data)
        except AccountError as error:
            # 业务失败：状态码仍是 200，前台只看 ok 字段，浏览器控制台也干净。
            self._reply(False, error.message)
        except Exception as error:                      # 兜底，别让线程死掉
            self.log_message("接口 %s 出错: %r", path, error)
            self._reply(False, "服务器内部错误，请看服务端日志", status=500)

    # -------------------------------------------------------------- 接口
    def _api_register(self, data):
        username = data.get("username", "")
        password = data.get("password", "")
        password2 = data.get("password2", password)
        if password != password2:
            self._reply(False, "两次输入的密码不一致，请重新输入。")
            return
        account = self.accounts.register(username, password)
        self.log_message("注册成功: %s", account["display_name"])
        eventlog.online(f"注册页 ✓ 新账号 账号={username!r} ip={eventlog.peer(self.client_address)}")
        self._reply(True, f"注册成功！现在可以在游戏登录界面用「{username}」登录了。")

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
        eventlog.online(f"注册页 ✓ 上传存档 账号={username!r} ip={eventlog.peer(self.client_address)} 结果={action}")
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


def make_server(port, accounts, host="::"):
    """建好 HTTP 服务器但不开始服务，方便测试拿到真实端口。"""
    handler = type("BoundHandler", (Handler,), {"accounts": accounts})
    return _PreboundHTTPServer(create_listener(host, port), handler)


def serve(port, accounts, host="::", ready=None):
    """阻塞地提供注册页服务。`app.py` 会把它丢进一个线程。"""
    httpd = make_server(port, accounts, host)
    if ready is not None:
        ready.set()
    httpd.serve_forever()


def main():
    import argparse
    ap = argparse.ArgumentParser(description="只跑注册页（调试用）")
    ap.add_argument("--port", type=int, default=27810)
    ap.add_argument("--host", default="::")
    ap.add_argument("--accounts", default=None)
    args = ap.parse_args()
    print(f"注册页 http://127.0.0.1:{args.port}/", flush=True)
    serve(args.port, AccountStore(args.accounts), args.host)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(HERE))
    main()
