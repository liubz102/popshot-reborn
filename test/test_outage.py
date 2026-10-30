#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""断网一阵子不被系统默认的重传超时踢掉 —— X_Mod §151 / D109 / D110（bug调查/29，2026-10-23）。

云服玩家闯关里网断了十几秒，他电脑上的 Windows 重传 20 秒上下就把中继连云服的那条 TCP
断掉，原版客户端跟着弹「发生服务器障碍」、被踢出关卡。修法是 `netlisten.limit_silence`：

* 玩家那头（本机中继出站那条）**永不**放弃；
* 服务端三个 accept（认证 / 游戏 / 中继）一律 `PEER_SILENCE_LIMIT_S`，
  Windows 用 `TCP_MAXRT`（秒）、Linux 用 `TCP_USER_TIMEOUT`（毫秒），换系统不换玩法。

这里钉：两种系统各设的是哪个选项、什么单位；别的系统一个字不设；真 socket 上读得回来；
每条路径都真的调到了。
"""
import socket
import sys
import threading
import time
import unittest
from unittest import mock

import authserver
import gameserver
import netlisten
import relay
import relayserver
from netlisten import PEER_SILENCE_LIMIT_S, limit_silence
from relayserver import RelayServer
from test_latency import Args, free_port
from test_proxy import FakeProxyMixin

ON_WINDOWS = sys.platform == "win32"


def maxrt(sock):
    return sock.getsockopt(socket.IPPROTO_TCP, netlisten.WIN_TCP_MAXRT)


def connected_pair(test):
    listener = socket.create_server(("127.0.0.1", 0))
    test.addCleanup(listener.close)
    client = socket.create_connection(listener.getsockname(), timeout=5)
    test.addCleanup(client.close)
    accepted, _ = listener.accept()
    test.addCleanup(accepted.close)
    return client


class Recorder:
    """只记 setsockopt 收到什么的假 socket —— 在 Windows 上测 Linux 那条分支用。"""

    def __init__(self):
        self.calls = []

    def setsockopt(self, *args):
        self.calls.append(args)


# ----------------------------------------------------------------------------
# 1. 每种系统设的是哪个选项、什么单位
# ----------------------------------------------------------------------------
class LimitSilencePlatformTests(unittest.TestCase):
    def options(self, platform, seconds):
        sock = Recorder()
        with mock.patch.object(netlisten.sys, "platform", platform):
            self.assertIs(sock, limit_silence(sock, seconds))
        return sock.calls

    def test_windows_is_tcp_maxrt_in_seconds(self):
        # 5 = `ws2ipdef.h` 的 TCP_MAXRT；-1 = 永不放弃
        self.assertEqual([(socket.IPPROTO_TCP, 5, 60)], self.options("win32", 60))
        self.assertEqual([(socket.IPPROTO_TCP, 5, -1)], self.options("win32", None))

    def test_linux_is_tcp_user_timeout_in_milliseconds(self):
        # 18 = `linux/tcp.h` 的 TCP_USER_TIMEOUT；它没有「永不」，取 int 能表示的最大毫秒数
        self.assertEqual([(socket.IPPROTO_TCP, 18, 60000)], self.options("linux", 60))
        self.assertEqual([(socket.IPPROTO_TCP, 18, 0x7FFFFFFF)], self.options("linux", None))

    def test_other_systems_are_left_alone(self):
        # 别的系统上 IPPROTO_TCP 的 5 / 18 号可能是别的选项（Linux 的 5 就是 TCP_KEEPINTVL）
        self.assertEqual([], self.options("darwin", 60))
        self.assertEqual([], self.options("darwin", None))

    def test_a_closed_socket_is_not_an_error(self):
        sock = socket.socket()
        sock.close()
        limit_silence(sock, None)                    # 不抛就算过
        limit_silence(sock, PEER_SILENCE_LIMIT_S)

    def test_a_non_socket_is_not_an_error(self):
        class Nothing:
            pass
        limit_silence(Nothing(), PEER_SILENCE_LIMIT_S)   # 不抛就算过


# ----------------------------------------------------------------------------
# 2. 真 socket 上读得回来；中继出站那条是「永不」
# ----------------------------------------------------------------------------
@unittest.skipUnless(ON_WINDOWS, "TCP_MAXRT 是 Windows 的选项")
class WindowsSocketTests(FakeProxyMixin, unittest.TestCase):
    def test_a_plain_socket_uses_the_system_default(self):
        """0 = 系统默认：重传 5 次（20 秒上下）就放弃 —— 就是 10-23 出事那条路。"""
        self.assertEqual(0, maxrt(connected_pair(self)))

    def test_both_values_read_back(self):
        sock = connected_pair(self)
        limit_silence(sock, PEER_SILENCE_LIMIT_S)
        self.assertEqual(PEER_SILENCE_LIMIT_S, maxrt(sock))
        limit_silence(sock, None)
        self.assertEqual(-1, maxrt(sock))

    def test_client_relay_direct_connect_never_gives_up(self):
        target = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(target.close)
        sock = relay.connect_remote("127.0.0.1", target.getsockname()[1])
        self.addCleanup(sock.close)
        self.assertEqual(-1, maxrt(sock))

    def test_client_relay_through_socks5_never_gives_up(self):
        proxy, state, thread = self.start_socks5()
        with relay.connect_remote("127.0.0.1", 27799, proxy) as sock:
            self.assertEqual(-1, maxrt(sock))
            sock.sendall(b"still-a-tunnel")          # 设了它隧道照常通
            self.assertEqual(b"still-a-tunnel", sock.recv(64))
        self.assertNotIn("error", state)


# ----------------------------------------------------------------------------
# 3. 服务端三个 accept 都调到了，而且是同一个数
# ----------------------------------------------------------------------------
class ServerAcceptTests(unittest.TestCase):
    """每条路径真的建一次连接，看服务端那一侧的 socket 收到了什么。"""

    def spy(self, module):
        seen = []

        def probe(sock, seconds):
            seen.append((sock, seconds))
            return limit_silence(sock, seconds)

        original = module.limit_silence
        module.limit_silence = probe
        self.addCleanup(setattr, module, "limit_silence", original)
        return seen

    def assert_limited(self, seen):
        for _ in range(300):
            if seen:
                break
            time.sleep(0.01)
        self.assertTrue(seen, "这条路径没有调 limit_silence")
        sock, seconds = seen[0]
        self.assertEqual(PEER_SILENCE_LIMIT_S, seconds)
        if ON_WINDOWS:
            self.assertEqual(PEER_SILENCE_LIMIT_S, maxrt(sock))

    def connect(self, port):
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(client.close)

    def test_game_server_accept(self):
        seen = self.spy(gameserver)
        port = free_port()
        ready = threading.Event()
        threading.Thread(
            target=gameserver.serve, args=(port, Args()),
            kwargs={"host": "127.0.0.1", "ready": ready,
                    "accounts": gameserver.AccountStore()},
            daemon=True).start()
        self.assertTrue(ready.wait(timeout=5))
        self.connect(port)
        self.assert_limited(seen)

    def test_relay_server_accept(self):
        seen = self.spy(relayserver)
        server = RelayServer(members_of=lambda conn: [], port=0)
        ready = threading.Event()
        threading.Thread(target=server.serve,
                         kwargs={"host": "127.0.0.1", "ready": ready},
                         daemon=True).start()
        self.assertTrue(ready.wait(timeout=5))
        self.addCleanup(server.stop)
        self.connect(server.listener.getsockname()[1])
        self.assert_limited(seen)

    def test_auth_server_accept(self):
        seen = self.spy(authserver)
        port = free_port()
        args = authserver.build_arg_parser().parse_args(["--port", str(port)])
        ready = threading.Event()
        threading.Thread(
            target=authserver.serve, args=(port, args, "auth"),
            kwargs={"host": "127.0.0.1", "ready": ready},
            daemon=True).start()
        self.assertTrue(ready.wait(timeout=5))
        self.connect(port)
        self.assert_limited(seen)


if __name__ == "__main__":
    unittest.main()
