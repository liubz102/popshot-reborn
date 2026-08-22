#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py —— 服务端唯一入口。

**单机假服务器和云端服务端跑的是同一个文件**（CLAUDE.md 铁律 8）：

    认证服   [::]:47611     NMCO 协议，校验密码 + 签发票据
    游戏服   [::]:27799     gcp/gsp 协议，凭票据认人
    注册页   [::]:<配置>    注册 + 资料修改 + 存档转移助手（默认 27810）
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
import errno
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import account_store
import authserver
import config as server_config
import eventlog
import gameserver
import logcleanup
import udpsync
import versioning
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
    ap.add_argument("--register-cooldown", type=int, default=None, metavar="秒",
                    help="一次注册成功后同一个 IP 要等多久才能再注册（0 = 不限）。"
                         "默认读 server.config 的 register_cooldown_seconds")
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
    ap.add_argument("--no-udp-sync", action="store_true",
                    help=f"不启动位置数据的 UDP 旁路（UDP "
                         f"{server_config.UDP_SYNC_PORT}）。加了它就是 2026-08-19 "
                         "之前的行为：全部同步数据走 TCP。老客户端本来就不发 "
                         "UDP，所以这个开关只影响更新过的客户端")
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
    ap.add_argument("--log-retention-days", type=int, default=None, metavar="天",
                    help="logs/ 里超过这么多天没动过的日志文件会被删掉（0 = 不清理）。"
                         "默认读 server.config 的 log_retention_days")
    ap.add_argument("--no-log-cleanup", action="store_true",
                    help="这次启动完全不清理日志（等价于 --log-retention-days 0）")
    # 游戏服那边的排查开关，原样透传。
    ap.add_argument("--hold-lobby", action="store_true",
                    help="游戏包一律不应答（纯抓包）")
    ap.add_argument("--no-death-reply", action="store_true",
                    help="收到 0x0408 也不回死亡广播（对比排查用）")
    ap.add_argument("--room-burst-delay", type=int, default=0, metavar="毫秒",
                    help="建房/回房间的那串包不合并（复现 §120 用）")
    ap.add_argument("--respawn-watchdog", type=float, default=None, metavar="秒",
                    help="死了多少秒还没等到客户端的 0x0413 就由服务端补发 0x0419"
                         "（bug调查/8「死了不复活」的兜底）。0 = 关掉兜底，"
                         "留出在玩家机器上跑 probe-death.bat 的取证窗口")
    return ap


def _game_args(args):
    """把 app 的命令行翻成 `gameserver.Conn` 认得的那套 args。"""
    ns = argparse.Namespace()
    ns.port = args.game_port
    ns.host = args.host
    ns.hold = False
    ns.version_result = 0
    # 版本门禁跟着 server-ClientFilter.config 热重载（每条连接查一眼 mtime，
    # 改配置不用重启服务器）。CLI 钉死值留给单跑 gameserver.py 和测试用。
    ns.client_min_version = versioning.FOLLOW_FILE
    ns.hold_lobby = args.hold_lobby
    ns.login_result = 0
    ns.accounts = args.accounts
    ns.no_death_reply = args.no_death_reply
    ns.room_burst_delay = args.room_burst_delay
    ns.respawn_watchdog = args.respawn_watchdog
    ns.control_port = 0 if args.no_control else args.control_port
    ns.verbose = args.verbose
    return ns


class PortBusy(RuntimeError):
    """要绑的端口被别人占着。**不是**普通启动失败，要单独说清楚。"""


def _start(name, target, args=(), kwargs=None, port=None, proto="TCP"):
    """起一个后台线程，并等它真的开始监听（或抛异常）。

    ★ 端口被占用时抛 `PortBusy`，`main()` 会把它变成一句人话再退出 ——
    玩家看到的应该是「端口 27799 被占用」，不是一屏 traceback。
    """
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
        error = failure[0]
        if isinstance(error, OSError) and error.errno == errno.EADDRINUSE:
            where = f"{proto} {port}" if port else name
            raise PortBusy(f"端口 {where}（{name}）被占用，无法启动")
        raise RuntimeError(f"{name} 启动失败: {error}")
    return True


def _report_level_realign(accounts):
    """把 `AccountStore.realign_levels()` 的结果说出来（屏幕 + 运营流水）。

    ★ **按状态翻转说话**：没有任何账号需要改就一行都不打。跑过一次之后
    每次启动都会是这样，日志里不会积累无意义的心跳。
    """
    try:
        changed = accounts.realign_levels()
    except Exception as error:              # noqa: BLE001 —— 存档坏了也不该拦住开服
        log(f"⚠ 等级对齐失败（{error!r}）；存档没被改动，游戏照常起")
        return
    if not changed:
        return
    capped = [row for row in changed if row["capped"]]
    head = (f"等级曲线换代: {len(changed)} 个账号的等级已按新曲线重算"
            f"（上限 {account_store.LEVEL_MAX} 级"
            f"，满级需要 {account_store.experience_for_level(account_store.LEVEL_MAX)} 点经验）")
    log(head)
    eventlog.online(head)
    for row in changed:
        line = (f"  {row['username']}: {row['old']} -> {row['new']} 级"
                f"（总经验 {row['experience']}）"
                + ("  ★ 经验已到顶，被等级上限钳住" if row["capped"] else ""))
        log(line)
        eventlog.online("等级重算 " + line.strip())
    if capped:
        names = "、".join(row["username"] for row in capped)
        tail = (f"★ 其中 {len(capped)} 个账号的经验超过了满级线，"
                f"等级被钳到 {account_store.LEVEL_MAX}: {names}")
        log(tail)
        eventlog.online(tail)


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
    # 版本门禁的当前状态（每条连接还会热重载，这里只是启动时报一声）。
    min_version, filter_warnings = versioning.load_client_filter()
    if min_version is None:
        log(f"版本门禁: 不限制（{versioning.client_filter_path()} 里填 0"
            f" 或文件缺失/认不出时都是它，任何版本含旧版客户端都能连）")
    else:
        log(f"版本门禁: 仅允许 {versioning.format_version(min_version)} 及以上"
            f"（低于它的连接会收到「版本过旧」提示；改 {versioning.CLIENT_FILTER_FILENAME}"
            f" 不用重启，下一条连接就按新值判）")
    for warning in filter_warnings:
        log(f"⚠ 版本门禁: {warning}")
    # 上下线流水另存一份：`server.out` 每次启动都会被覆盖，连接记录不该跟着没。
    # `--verbose` 时另外放行 `eventlog.debug()`（转发耗时 / 中继 RTT 这类遥测，D112）。
    if args.no_online_log:
        eventlog.configure(to_file=False, verbose=args.verbose)
        log("连接日志: 只打到屏幕（--no-online-log）")
    else:
        eventlog.configure(verbose=args.verbose)
        log(f"连接日志: {eventlog.path()}（谁连上、谁断开、从哪个 IP；精简模式也照记）")
    if args.verbose:
        log("调试日志: 已放行 [online-debug]（转发耗时 / 中继 RTT / 客户端异常上报）")

    # 等级曲线换代后的存档对齐（§229 / D150）。**必须排在 `eventlog.configure`
    # 之后** —— `server.out` 每次启动都会被覆盖，这份清单要留在不被覆盖的
    # 那一份流水里，否则重启一次就再也查不到「谁从几级变成了几级」。
    # 只在服务端真的启动时跑，那时没人在线；幂等，清单为空就一个字都不打。
    _report_level_realign(accounts)

    # 日志清理（D113）。**只在服务端真的启动时跑这一次** —— 启动脚本发现
    # 服务端已在运行而跳过启动时，这里根本不会被执行到，正合需求。
    # 之后每天凌晨 4 点再清一次（云主机常年开机，等不到下一次启动）。
    retention = args.log_retention_days
    if retention is None:
        retention = cfg["log_retention_days"]
    if args.no_log_cleanup:
        retention = 0
    if retention > 0:
        log(f"日志清理 保留最近 {retention} 天（启动时清一次，之后每天 "
            f"{logcleanup.DAILY_HOUR} 点清一次；后台线程，不挡游戏）")
    # 关掉时那句「已关闭」由 `logcleanup.start` 自己说，免得打两遍。
    logcleanup.start(days=retention, log=log)

    auth_args = _AuthArgs(accounts=args.accounts, verbose=args.verbose,
                          ticket_field=args.ticket_field,
                          allow_any=args.allow_any_password)
    if args.allow_any_password:
        log("⚠ --allow-any-password：任何密码都放行，只该在协议试探时用")

    _start("auth", authserver.serve,
           kwargs={"port": args.auth_port, "args": auth_args,
                   "service": service, "host": args.host},
           port=args.auth_port)
    log(f"认证服   {describe_listen(args.host, args.auth_port)}")

    _start("game", gameserver.serve,
           kwargs={"port": args.game_port, "args": _game_args(args),
                   "accounts": accounts, "tickets": tickets,
                   "host": args.host},
           port=args.game_port)
    log(f"游戏服   {describe_listen(args.host, args.game_port)}")

    # 原版 TCP 中继（D078）。它和游戏服是同一个进程里的两个监听器 ——
    # 中继要按「谁和谁同房间」投递，那份状态就在 `gameserver.LOBBY` 里。
    gameserver.TCP_RELAY_ENABLED = not args.no_tcp_relay
    if args.no_tcp_relay:
        log("中继服   已关闭（--no-tcp-relay）；玩家间同步走 0x040e 回退路径")
    else:
        gameserver.PEER_RELAY.port = args.relay_port
        _start("relay", gameserver.PEER_RELAY.serve,
               kwargs={"host": args.host}, port=args.relay_port)
        log(f"中继服   {describe_listen(args.host, args.relay_port)}"
            f" —— 原版 rcp 协议，战斗内同步走它")

    # ★ 位置数据的 UDP 旁路（bug调查/9）。和游戏服 TCP **同一个端口号**，
    #   TCP/UDP 两套端口空间不冲突。开火/命中/伤害这些不能丢的东西照旧走 TCP。
    #   `udp_sync = 0` 或 `--no-udp-sync` 关掉；关掉、没放行 UDP、玩家用的是
    #   没更新的客户端 —— 三种情况的表现完全一样：全部走 TCP，没人察觉。
    udp_on = bool(cfg["udp_sync"]) and not args.no_udp_sync
    udpsync.SERVER.enabled = udp_on
    # ★ 把票据表接给 UDP 侧，让它分得清「登录包还在路上」和「根本不认识这张票」
    #   （`udpsync.ACK_NOT_LOGGED_IN`）。前者是**每次登录都会经过**的时序窗口，
    #   后者才值得在玩家的日志里说一句。`gameserver` 只注入了「票据 -> 连接」，
    #   票据表是这里建的，所以这一半在这里补。
    #   ★ 用 `is_live` 而不是 `resolve` —— 后者每次调用都给票据续期，
    #     而这只是旁观者在问「这张票是不是真的」，不该延长任何东西的寿命。
    udpsync.SERVER.bind_lookup(ticket_known=tickets.is_live)
    udpsync.SERVER.redundancy = cfg["udp_sync_redundancy"]
    # ★ 跟着 `--game-port` 走，不用自己那份常量 —— 「和游戏服同号」是这套东西
    #   对玩家承诺的唯一一句话（防火墙只要记一个号），改了游戏服端口而 UDP
    #   还钉在 27799 的话，这句话就不成立了。
    udpsync.SERVER.port = args.game_port
    if not udp_on:
        why = "--no-udp-sync" if args.no_udp_sync else "server.config 的 udp_sync = 0"
        log(f"同步UDP  已关闭（{why}）；位置数据和其它同步数据一样走 TCP")
    else:
        _start("udpsync", udpsync.SERVER.serve,
               kwargs={"host": args.host}, port=args.game_port, proto="UDP")
        log(f"同步UDP  {describe_listen(args.host, args.game_port)}/udp"
            f" —— 只有位置数据走它（冗余 {cfg['udp_sync_redundancy']} 份），"
            f"开火/伤害/死亡照旧走 TCP")
        log(f"         ⚠ 防火墙要单独放行 UDP {args.game_port}"
            f"（和游戏服 TCP 同号，但规则是两条）")

    if not args.no_web:
        # 延迟 import：注册页是纯标准库的，但没必要在 --no-web 时也加载。
        from web import server as web_server
        cooldown = (args.register_cooldown if args.register_cooldown is not None
                    else cfg["register_cooldown_seconds"])
        _start("web", web_server.serve,
               kwargs={"port": web_port, "accounts": accounts,
                       "host": args.host, "cooldown": cooldown},
               port=web_port)
        log(f"注册页   {describe_listen(args.host, web_port)}"
            f" —— 本机打开 http://127.0.0.1:{web_port}/")
        log("注册冷却 " + (f"{cooldown} 秒（同一 IP 注册成功后要等这么久；"
                          f"注册页上的按钮也锁这么久）" if cooldown
                          else "已关闭（register_cooldown_seconds = 0）"))
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
    # 端口被占用是**最常见**的启动失败，而它的 traceback 对玩家毫无意义。
    # 把它压成一句话 + 退出码 1，启动脚本据此把处理办法打出来。
    try:
        main()
    except PortBusy as error:
        log(f"!! {error}")
        log("   处理办法：先关掉占用它的程序（或上一次没关干净的服务端），再重试。")
        sys.exit(1)
