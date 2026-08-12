#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py —— 服务端唯一入口。

**单机假服务器和云端服务端跑的是同一个文件**（CLAUDE.md 铁律 8）：

    认证服   [::]:47611     NMCO 协议，校验密码 + 签发票据
    游戏服   [::]:27799     gcp/gsp 协议，凭票据认人
    注册页   [::]:<配置>    用户注册 + 存档转移助手（默认 27810）
    控制通道 127.0.0.1:27800  只给本机调试用（tools/gs_ctl.py），默认在服务端包里关掉

四个监听器在**一个进程**里，因为认证服签发的票据必须让游戏服查得到（决策 D064）。
监听地址固定 ``::``（IPv6 双栈，IPv4 也连得进来，D063），不做成配置项。

用法：

    python server/app.py                  # 全都起来（单机 / 开服都用这条）
    python server/app.py --verbose        # 逐包 dump，逆协议时用
    python server/app.py --no-control     # 关掉调试控制通道（云端建议）
    python server/app.py --no-web         # 不起注册页
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import authserver
import config as server_config
import eventlog
import gameserver
from account_store import AccountStore
from netlisten import describe as describe_listen
from tickets import TicketStore

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def log(msg):
    print(f"[{gameserver.ts()}] [app] {msg}", flush=True)


class _AuthArgs:
    """`authserver.make_reply` 要的那几个开关。单进程模式下取固定值。"""

    def __init__(self, accounts=None, verbose=False, ticket_field="s2",
                 allow_any=False):
        self.accounts = accounts
        self.reply = "login"
        self.sweep = None
        self.gap = 0.0
        self.result = 0
        self.ticket_field = ticket_field
        self.allow_any = allow_any
        self.verbose = verbose


def build_arg_parser():
    ap = argparse.ArgumentParser(
        description="炮炮火枪手服务端（认证 + 游戏 + 注册页，单机和云端共用）")
    ap.add_argument("--host", default="::",
                    help="监听地址。默认 :: = IPv4/IPv6 都能连（一般不用改）")
    ap.add_argument("--auth-port", type=int, default=server_config.AUTH_PORT,
                    help=f"认证服端口（客户端写死 {server_config.AUTH_PORT}，别改）")
    ap.add_argument("--game-port", type=int, default=server_config.GAME_PORT,
                    help=f"游戏服端口（客户端写死 {server_config.GAME_PORT}，别改）")
    ap.add_argument("--web-port", type=int, default=None,
                    help="注册页端口。默认读 server.config 的 local_register_port")
    ap.add_argument("--no-web", action="store_true", help="不启动注册页")
    ap.add_argument("--no-control", action="store_true",
                    help="不启动调试控制通道（部署到公网服务器时建议加）")
    ap.add_argument("--no-tcp-relay", action="store_true",
                    help="不启动原版 TCP 中继，也不回 0x0210。"
                         "客户端会自动退回 0x040e 那条（同样是原版的）"
                         "回退路径 —— 中继出问题时的应急开关（D078 / FINDINGS §158）")
    ap.add_argument("--relay-port", type=int,
                    default=server_config.PEER_RELAY_PORT,
                    help=f"原版 TCP 中继的端口（默认 {server_config.PEER_RELAY_PORT}）。"
                         "改了的话客户端包里 bshook 的映射表也要跟着改")
    ap.add_argument("--control-port", type=int,
                    default=server_config.CONTROL_PORT,
                    help="调试控制通道端口，只绑 127.0.0.1")
    ap.add_argument("--accounts", default=None,
                    help="账号 JSON 路径（默认 server/data/accounts.json）")
    ap.add_argument("--config", default=None,
                    help="server.config 路径（默认包根目录下的那个）")
    ap.add_argument("--ticket-field", choices=("s1", "s2"), default="s2",
                    help="票据放在 CULoginReplyPacket 的哪个字符串字段。"
                         "默认 s2 —— 实测客户端转发给 gcpReqLogin 的就是它"
                         "（FINDINGS §123）")
    ap.add_argument("--allow-any-password", action="store_true",
                    help="⚠ 只给协议试探：跳过密码校验")
    ap.add_argument("--verbose", action="store_true",
                    help="逐包 hexdump + 抓包落盘。逆协议时开，日常别开")
    ap.add_argument("--no-online-log", action="store_true",
                    help="连接事件只打到屏幕，不写 logs/online.log")
    # 游戏服那边的排查开关，原样透传。
    ap.add_argument("--hold-lobby", action="store_true",
                    help="游戏包一律不应答（纯抓包）")
    ap.add_argument("--no-death-reply", action="store_true",
                    help="收到 0x0408 也不回死亡广播（对比排查用）")
    ap.add_argument("--room-burst-delay", type=int, default=0, metavar="毫秒",
                    help="建房/回房间的那串包不合并（复现 §120 用）")
    return ap


def _game_args(args):
    """把 app 的命令行翻成 `gameserver.Conn` 认得的那套 args。"""
    ns = argparse.Namespace()
    ns.port = args.game_port
    ns.host = args.host
    ns.hold = False
    ns.version_result = 0
    ns.hold_lobby = args.hold_lobby
    ns.login_result = 0
    ns.accounts = args.accounts
    ns.no_death_reply = args.no_death_reply
    ns.room_burst_delay = args.room_burst_delay
    ns.control_port = 0 if args.no_control else args.control_port
    ns.verbose = args.verbose
    return ns


def _start(name, target, args=(), kwargs=None):
    """起一个后台线程，并等它真的开始监听（或抛异常）。"""
    ready = threading.Event()
    failure = []
    kwargs = dict(kwargs or {})
    kwargs["ready"] = ready

    def run():
        try:
            target(*args, **kwargs)
        except Exception as error:      # 端口被占用是最常见的一种
            failure.append(error)
            ready.set()

    threading.Thread(target=run, daemon=True, name=name).start()
    ready.wait(timeout=10)
    if failure:
        raise RuntimeError(f"{name} 启动失败: {failure[0]}")
    return True


def main(argv=None):
    args = build_arg_parser().parse_args(argv)

    gameserver.VERBOSE = args.verbose
    authserver.VERBOSE = args.verbose

    config_path = args.config or server_config.config_path()
    server_config.ensure_exists(config_path)
    cfg, warnings = server_config.load(config_path)
    for warning in warnings:
        log(f"server.config: {warning}")
    web_port = args.web_port or cfg["local_register_port"]

    accounts = AccountStore(args.accounts)
    # 票据**只在内存里**（D097）：断线重连靠它，服务端重启则一律作废 ——
    # 重启后玩家（和客户端）都要重新登录。故意不落盘，见 tickets.py 开头。
    tickets = TicketStore()
    service = authserver.AuthService(accounts, tickets)

    log(f"账号存档: {accounts.path}（当前 {len(accounts.usernames())} 个账号）")
    log(f"配置文件: {config_path}")
    # 上下线流水另存一份：`server.out` 每次启动都会被覆盖，连接记录不该跟着没。
    if args.no_online_log:
        eventlog.configure(to_file=False)
        log("连接日志: 只打到屏幕（--no-online-log）")
    else:
        log(f"连接日志: {eventlog.path()}（谁连上、谁断开、从哪个 IP；精简模式也照记）")

    auth_args = _AuthArgs(accounts=args.accounts, verbose=args.verbose,
                          ticket_field=args.ticket_field,
                          allow_any=args.allow_any_password)
    if args.allow_any_password:
        log("⚠ --allow-any-password：任何密码都放行，只该在协议试探时用")

    _start("auth", authserver.serve,
           kwargs={"port": args.auth_port, "args": auth_args,
                   "service": service, "host": args.host})
    log(f"认证服   {describe_listen(args.host, args.auth_port)}")

    _start("game", gameserver.serve,
           kwargs={"port": args.game_port, "args": _game_args(args),
                   "accounts": accounts, "tickets": tickets,
                   "host": args.host})
    log(f"游戏服   {describe_listen(args.host, args.game_port)}")

    # 原版 TCP 中继（D078）。它和游戏服是同一个进程里的两个监听器 ——
    # 中继要按「谁和谁同房间」投递，那份状态就在 `gameserver.LOBBY` 里。
    gameserver.TCP_RELAY_ENABLED = not args.no_tcp_relay
    if args.no_tcp_relay:
        log("中继服   已关闭（--no-tcp-relay）；玩家间同步走 0x040e 回退路径")
    else:
        gameserver.PEER_RELAY.port = args.relay_port
        _start("relay", gameserver.PEER_RELAY.serve,
               kwargs={"host": args.host})
        log(f"中继服   {describe_listen(args.host, args.relay_port)}"
            f" —— 原版 rcp 协议，战斗内同步走它")

    if not args.no_web:
        # 延迟 import：注册页是纯标准库的，但没必要在 --no-web 时也加载。
        from web import server as web_server
        _start("web", web_server.serve,
               kwargs={"port": web_port, "accounts": accounts,
                       "host": args.host})
        log(f"注册页   {describe_listen(args.host, web_port)}"
            f" —— 本机打开 http://127.0.0.1:{web_port}/")
    else:
        log("注册页   已关闭（--no-web）")

    if not args.no_control:
        threading.Thread(target=gameserver.serve_control,
                         args=(args.control_port,), daemon=True).start()
        log(f"控制通道 127.0.0.1:{args.control_port}（只给本机调试）")
    else:
        log("控制通道 已关闭（--no-control）")

    log("全部就绪。Ctrl+C 退出。")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log("bye")


if __name__ == "__main__":
    main()
