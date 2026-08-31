#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""延迟这一段的测试 —— 会话 17 / FINDINGS §182 / 决策 D104。

分三块：

1. **`tune_stream` 本身**：真的关掉了 Nagle，而且踩到什么都不抛；
2. **六条路径都调了它**：三个 accept 循环 + 本机中继的两条 + 出站那条。
   用真 socket 建连接，然后看**服务端那一侧**的 socket 上 `TCP_NODELAY` 是不是 1
   —— 只在监听 socket 上设是没用的（accepted socket 不继承），这条用例就是钉这个；
3. **中继 ping 与 RTT**：战斗中（数据连续、`recv` 从不超时）也要照发，
   算得出 min/avg/p95，而且**ping 丢光了也绝不关连接**（铁律 1 / §158）。
"""
import os
import socket
import struct
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import authserver                                              # noqa: E402
import gameserver                                              # noqa: E402
import netlisten                                               # noqa: E402
import relay                                                   # noqa: E402
import relayserver                                             # noqa: E402
from netlisten import create_listener, tune_stream             # noqa: E402
from relayserver import (RCP_PING, RCP_REGISTER,               # noqa: E402
                         RCP_REP_PING, RelayServer, RttStats)
from test_relayserver import FakeGameConn, FakeRelayClient     # noqa: E402


def nodelay(sock):
    return sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)


def free_port():
    """随便要一个空闲端口，用完就还。"""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Args:
    """`serve()` 只把它往下传，这几个字段够 `Conn.__init__` 跑完了。"""

    accounts = None
    hold = False
    hold_lobby = False
    version_result = 0
    login_result = 0
    no_death_reply = False


class Spy:
    """记下被调用时收到的 socket，然后照常调真家伙。"""

    def __init__(self):
        self.seen = []

    def __call__(self, sock):
        self.seen.append(sock)
        return tune_stream(sock)


# ----------------------------------------------------------------------------
# 1. tune_stream 本身
# ----------------------------------------------------------------------------
class TuneStreamTests(unittest.TestCase):
    def test_it_turns_nagle_off(self):
        listener = create_listener("127.0.0.1", 0)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(client.close)
        server, _ = listener.accept()
        self.addCleanup(server.close)
        # 默认是开着 Nagle 的 —— 这条断言同时说明「不设就是慢的」。
        self.assertEqual(0, nodelay(server))
        self.assertIs(server, tune_stream(server))
        self.assertEqual(1, nodelay(server))

    def test_a_plain_accepted_socket_still_has_nagle_on(self):
        """不设就是慢的 —— 这条断言是「为什么要有 tune_stream」的依据。

        ⚠ **别改成「监听 socket 上设了也不会传下去」** ——那条在 Windows 上是
        反的（实测：`accept()` 出来的 socket 继承了监听 socket 的 `TCP_NODELAY`），
        Linux 上才不继承。服务端包两边都要跑，所以**每处 accept 都必须自己调**
        `tune_stream`，靠继承是不行的；但这件事本身平台相关，不该写成断言
        （FINDINGS §182）。
        """
        listener = create_listener("127.0.0.1", 0)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(client.close)
        server, _ = listener.accept()
        self.addCleanup(server.close)
        self.assertEqual(0, nodelay(server))

    def test_a_closed_socket_is_not_an_error(self):
        sock = socket.socket()
        sock.close()
        tune_stream(sock)                      # 不抛就算过

    def test_a_non_socket_is_not_an_error(self):
        class Nothing:
            pass
        tune_stream(Nothing())                 # 不抛就算过


# ----------------------------------------------------------------------------
# 2. 六条路径
# ----------------------------------------------------------------------------
class CallSiteTests(unittest.TestCase):
    """每条路径都真的建一次连接，看 `tune_stream` 有没有被调到那条 socket 上。"""

    def swap(self, module):
        """把某个模块里的 `tune_stream` 换成探针，用例结束自动换回来。"""
        spy = Spy()
        original = module.tune_stream
        module.tune_stream = spy
        self.addCleanup(setattr, module, "tune_stream", original)
        return spy

    def assert_tuned(self, spy, count=1):
        for _ in range(300):
            if len(spy.seen) >= count:
                break
            time.sleep(0.01)
        self.assertGreaterEqual(len(spy.seen), count,
                                "这条路径没有调 tune_stream")
        for sock in spy.seen[:count]:
            self.assertEqual(1, nodelay(sock))

    # -- 中继服（战斗数据主路）------------------------------------------------
    def test_relay_server_accept(self):
        spy = self.swap(relayserver)
        server = RelayServer(members_of=lambda conn: [], port=0)
        ready = threading.Event()
        threading.Thread(target=server.serve,
                         kwargs={"host": "127.0.0.1", "ready": ready},
                         daemon=True).start()
        self.assertTrue(ready.wait(timeout=5))
        self.addCleanup(server.stop)
        port = server.listener.getsockname()[1]
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(client.close)
        self.assert_tuned(spy)

    # -- 游戏服（0x040f 回退路径）--------------------------------------------
    def test_game_server_accept(self):
        spy = self.swap(gameserver)
        port = free_port()
        ready = threading.Event()
        threading.Thread(
            target=gameserver.serve,
            args=(port, Args()),
            kwargs={"host": "127.0.0.1", "ready": ready,
                    "accounts": gameserver.AccountStore()},
            daemon=True).start()
        self.assertTrue(ready.wait(timeout=5))
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(client.close)
        self.assert_tuned(spy)

    # -- 认证服 ---------------------------------------------------------------
    def test_auth_server_accept(self):
        spy = self.swap(authserver)
        port = free_port()
        args = authserver.build_arg_parser().parse_args(["--port", str(port)])
        ready = threading.Event()
        threading.Thread(
            target=authserver.serve,
            args=(port, args, "auth"),
            kwargs={"host": "127.0.0.1", "ready": ready},
            daemon=True).start()
        self.assertTrue(ready.wait(timeout=5))
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(client.close)
        self.assert_tuned(spy)

    # -- 本机中继：出站那条 ---------------------------------------------------
    def test_relay_outbound_connect(self):
        target = create_listener("127.0.0.1", 0)
        self.addCleanup(target.close)
        port = target.getsockname()[1]
        sock = relay.connect_remote("127.0.0.1", port)
        self.addCleanup(sock.close)
        self.assertEqual(1, nodelay(sock))

    # -- 本机中继：面向 BigShot.exe 的那条 + 出站那条（两条都要）-------------
    def test_relay_handle_tunes_both_directions(self):
        spy = self.swap(relay)
        target = create_listener("127.0.0.1", 0)
        self.addCleanup(target.close)
        target_port = target.getsockname()[1]
        local_port = free_port()
        ready = threading.Event()
        threading.Thread(
            target=relay.serve_one,
            args=(local_port, "127.0.0.1", target_port, "测试"),
            kwargs={"ready": ready}, daemon=True).start()
        self.assertTrue(ready.wait(timeout=5))
        client = socket.create_connection(("127.0.0.1", local_port), timeout=5)
        self.addCleanup(client.close)
        accepted, _ = target.accept()
        self.addCleanup(accepted.close)
        # connect_remote 里一条 + handle() 里 client / remote 两条 = 3 次
        self.assert_tuned(spy, count=3)


# ----------------------------------------------------------------------------
# 3. RTT 统计
# ----------------------------------------------------------------------------
class RttStatsTests(unittest.TestCase):
    def test_empty_has_no_summary(self):
        self.assertIsNone(RttStats().summary())
        self.assertIsNone(RttStats().avg)
        self.assertIsNone(RttStats().p95)

    def test_min_avg_max(self):
        stats = RttStats()
        for value in (2.0, 4.0, 6.0):
            stats.add(value)
        self.assertEqual(3, stats.count)
        self.assertEqual(2.0, stats.lo)
        self.assertEqual(6.0, stats.hi)
        self.assertEqual(4.0, stats.avg)

    def test_p95_of_a_hundred_samples(self):
        stats = RttStats()
        for value in range(1, 101):            # 1..100
            stats.add(float(value))
        # round(0.95 * 99) = 94 -> ordered[94]，也就是升序第 95 个 = 95.0
        self.assertEqual(95.0, stats.p95)

    def test_p95_degrades_to_max_when_there_are_few_samples(self):
        stats = RttStats()
        stats.add(7.0)
        self.assertEqual(7.0, stats.p95)

    def test_the_cap_only_bounds_the_percentile_window(self):
        """`count / min / max / avg` 是全量的，只有 p95 算在最近 cap 个上。"""
        stats = RttStats(cap=4)
        for value in range(10):
            stats.add(float(value))
        self.assertEqual(10, stats.count)
        self.assertEqual(0.0, stats.lo)        # 全量的最小值还在
        self.assertEqual(9.0, stats.hi)
        self.assertEqual(4, len(stats.recent))

    def test_reset_clears_everything(self):
        stats = RttStats()
        stats.add(1.0)
        stats.reset()
        self.assertEqual(0, stats.count)
        self.assertIsNone(stats.summary())


class PingTimingTests(unittest.TestCase):
    """`tick()` / `on_pong()` 的时序，全部用注入的时间跑，不睡觉。"""

    def make_conn(self):
        conn = relayserver.RelayConn.__new__(relayserver.RelayConn)
        conn.closed = False
        conn.game_conn = FakeGameConn("alice")
        conn.addr = ("127.0.0.1", 40000)
        conn.sent = []
        conn.send_frame = lambda op, payload=b"": (conn.sent.append(op) or True)
        conn.pings_out = conn.pongs_in = conn.pings_lost = 0
        conn.opened_at = 0.0
        conn.last_ping_at = 0.0
        conn.ping_sent_at = None
        conn.rtt_window = RttStats()
        conn.rtt_total = RttStats()
        conn.last_rtt_report_at = 0.0
        return conn

    def test_a_ping_goes_out_once_the_interval_has_passed(self):
        conn = self.make_conn()
        conn.tick(now=0.5)                     # 还不到 1 秒
        self.assertEqual([], conn.sent)
        conn.tick(now=1.5)
        self.assertEqual([RCP_PING], conn.sent)
        self.assertEqual(1, conn.pings_out)

    def test_only_one_ping_is_in_flight_at_a_time(self):
        """rcp 的 ping 没有 id：两发同时在飞就分不清 pong 是谁的了。"""
        conn = self.make_conn()
        conn.tick(now=1.5)
        conn.tick(now=2.6)
        conn.tick(now=3.7)
        self.assertEqual([RCP_PING], conn.sent)

    def test_the_rtt_is_measured_from_the_matching_ping(self):
        conn = self.make_conn()
        conn.tick(now=1.5)
        conn.on_pong(now=1.53)
        self.assertEqual(1, conn.rtt_window.count)
        self.assertAlmostEqual(30.0, conn.rtt_window.lo, places=3)
        self.assertAlmostEqual(30.0, conn.rtt_total.lo, places=3)
        self.assertEqual(1, conn.pongs_in)

    def test_a_pong_with_nothing_in_flight_is_not_measured(self):
        """超时之后才回来的那一发算不出可信的 RTT —— 丢掉比记个错的强。"""
        conn = self.make_conn()
        conn.on_pong(now=9.0)
        self.assertEqual(1, conn.pongs_in)
        self.assertEqual(0, conn.rtt_window.count)

    def test_an_unanswered_ping_times_out_and_the_next_one_goes_out(self):
        conn = self.make_conn()
        conn.tick(now=1.5)
        conn.tick(now=1.5 + relayserver.PING_TIMEOUT)      # 判丢
        self.assertEqual(1, conn.pings_lost)
        conn.tick(now=1.5 + relayserver.PING_TIMEOUT + 1.1)
        self.assertEqual([RCP_PING, RCP_PING], conn.sent)

    def test_losing_every_ping_never_closes_the_connection(self):
        """★ 铁律 1（§158）：中继一断，客户端会**自己退出房间**。"""
        conn = self.make_conn()
        now = 1.5
        for _ in range(50):
            conn.tick(now=now)
            now += relayserver.PING_TIMEOUT + 1.1
        self.assertFalse(conn.closed)
        self.assertGreater(conn.pings_lost, 10)

    def test_the_summary_line_is_emitted_once_per_window_then_reset(self):
        conn = self.make_conn()
        lines = []
        original = relayserver.eventlog.debug
        relayserver.eventlog.debug = lines.append
        self.addCleanup(setattr, relayserver.eventlog, "debug", original)
        conn.rtt_window.add(1.0)
        conn.report_rtt(now=1.0)               # 还不到 30 秒
        self.assertEqual([], lines)
        conn.report_rtt(now=relayserver.RTT_REPORT_INTERVAL + 1.0)
        self.assertEqual(1, len(lines))
        self.assertIn("中继 RTT", lines[0])
        self.assertIn("样本=1", lines[0])
        self.assertEqual(0, conn.rtt_window.count)   # 窗口清零了

    def test_an_empty_window_prints_nothing(self):
        conn = self.make_conn()
        lines = []
        original = relayserver.eventlog.debug
        relayserver.eventlog.debug = lines.append
        self.addCleanup(setattr, relayserver.eventlog, "debug", original)
        conn.report_rtt(now=relayserver.RTT_REPORT_INTERVAL + 1.0, force=True)
        self.assertEqual([], lines)


class PingDuringBattleTests(unittest.TestCase):
    """★ 修 §182 的那个漏洞：数据连续时 `recv` 从不超时，ping 就再也不发了。"""

    def setUp(self):
        self.server = RelayServer(members_of=lambda conn: [], port=0)
        ready = threading.Event()
        threading.Thread(target=self.server.serve,
                         kwargs={"host": "127.0.0.1", "ready": ready},
                         daemon=True).start()
        self.assertTrue(ready.wait(timeout=5), "中继监听器没起来")
        self.port = self.server.listener.getsockname()[1]
        self.saved_interval = relayserver.PING_INTERVAL
        relayserver.PING_INTERVAL = 0.0        # 下一个 tick 就发

    def tearDown(self):
        relayserver.PING_INTERVAL = self.saved_interval
        self.server.stop()

    def test_ping_still_goes_out_while_data_keeps_arriving(self):
        alice = FakeGameConn("alice")
        auth = self.server.issue(alice, 0, 0)
        client = FakeRelayClient(self.port)
        self.addCleanup(client.close)
        client.send(RCP_REGISTER, struct.pack("<iii", *auth))
        # 一直喂数据：`recv` 永远不会超时，所以 ping 只可能来自
        # `run()` 里 feed 之后那一次 tick()。
        deadline = time.time() + 5
        got_ping = False
        while time.time() < deadline and not got_ping:
            client.send(relayserver.RCP_DATA_UP, b"\x01\x02\x03")
            frame = client.recv_frame(timeout=0.2)
            if frame is not None and frame[0] == RCP_PING:
                got_ping = True
        self.assertTrue(got_ping, "数据连续时一发 ping 都没有 —— §182 的漏洞回来了")

    def test_a_real_round_trip_produces_a_sample(self):
        alice = FakeGameConn("alice")
        auth = self.server.issue(alice, 0, 0)
        client = FakeRelayClient(self.port)
        self.addCleanup(client.close)
        client.send(RCP_REGISTER, struct.pack("<iii", *auth))
        frame = client.recv_frame(timeout=5)
        self.assertIsNotNone(frame)
        self.assertEqual(RCP_PING, frame[0])
        client.send(RCP_REP_PING)
        relay_conn = None
        for _ in range(300):
            relay_conn = self.server.conn_for(alice)
            if relay_conn is not None and relay_conn.rtt_total.count:
                break
            time.sleep(0.01)
        self.assertIsNotNone(relay_conn)
        self.assertGreaterEqual(relay_conn.rtt_total.count, 1)
        self.assertIsNotNone(relay_conn.rtt_total.summary())


# ----------------------------------------------------------------------------
# 4. 0x0106 gcpReportHack 的正文
# ----------------------------------------------------------------------------
class ReportHackTests(unittest.TestCase):
    def test_a_wstring_payload_is_decoded(self):
        text = "(FastFire) wpnIdx=3,lastFireTime=100,currFireTime=110"
        payload = gameserver.w_wstr(text)
        self.assertEqual(text, gameserver.parse_report_hack(payload))

    def test_trailing_bytes_are_reported_not_dropped(self):
        payload = gameserver.w_wstr("hi") + b"\x01\x02\x03"
        decoded = gameserver.parse_report_hack(payload)
        self.assertTrue(decoded.startswith("hi"))
        self.assertIn("3", decoded)

    def test_a_malformed_payload_never_raises(self):
        for payload in (b"", b"\xff", b"\x10\x00" + b"\x41",
                        bytes(range(16))):
            decoded = gameserver.parse_report_hack(payload)
            self.assertIsInstance(decoded, str)


if __name__ == "__main__":
    unittest.main()
