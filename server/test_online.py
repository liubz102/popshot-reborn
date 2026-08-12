#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V0.2 联机地基的测试：server.config 解析 / 票据 / 认证 / 游戏服凭票据认人。

单机时代那套「认证服写 active_account、游戏服读它」的做法在多账号下直接失效，
所以这一组测试盯的都是**身份不会串**这件事。
"""
import json
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import authserver
import config as server_config
import eventlog
import gameserver
import relay
from account_store import AUTH_OK, AccountStore, tutorial_state
from simple import SimpleCipher
from tickets import TicketStore, short
from web import server as web_server


class ConfigTests(unittest.TestCase):
    def test_defaults_when_the_file_is_missing(self):
        values, warnings = server_config.load(
            os.path.join(tempfile.gettempdir(), "no-such-server.config"))
        self.assertEqual(server_config.DEFAULTS, values)
        self.assertTrue(warnings)

    def test_the_shipped_template_parses_to_the_defaults(self):
        # 模板里写的示例值必须和代码里的默认值一致，否则玩家改一半会得到
        # 两套不同的行为。
        values, warnings = server_config.parse_text(
            server_config.DEFAULT_CONFIG_TEXT)
        self.assertEqual(server_config.DEFAULTS, values)
        self.assertEqual([], warnings)

    def test_ipv4_ipv6_and_domain_all_parse(self):
        for text, expected in (
                ("server_address = 192.168.1.100", "192.168.1.100"),
                ("server_address = 2001:db8::1", "2001:db8::1"),
                ("server_address = [2001:db8::1]", "2001:db8::1"),
                ("server_address = popshot.example.com", "popshot.example.com"),
        ):
            values, warnings = server_config.parse_text(text)
            self.assertEqual(expected, values["server_address"], text)
            self.assertEqual([], warnings, text)

    def test_comments_blank_lines_and_bom_are_tolerated(self):
        values, warnings = server_config.parse_text(
            "﻿# 注释\n\n; 另一种注释\n  LOCAL_REGISTER_PORT = 28000  \n")
        self.assertEqual(28000, values["local_register_port"])
        self.assertEqual([], warnings)

    def test_a_broken_line_only_warns(self):
        # 配置写错就用默认值继续跑；服务端起不来、玩家什么都看不到才是最坏的结果。
        values, warnings = server_config.parse_text(
            "local_register_port = 这不是数字\nnonsense\nunknown_key = 1\n")
        self.assertEqual(server_config.DEFAULT_REGISTER_PORT,
                         values["local_register_port"])
        self.assertEqual(3, len(warnings))

    def test_out_of_range_ports_fall_back(self):
        values, _ = server_config.parse_text("server_register_port = 99999")
        self.assertEqual(server_config.DEFAULT_REGISTER_PORT,
                         values["server_register_port"])

    def test_register_url_brackets_ipv6_only(self):
        self.assertEqual("http://127.0.0.1:27810/",
                         server_config.register_url("127.0.0.1", 27810))
        self.assertEqual("http://[::1]:27810/",
                         server_config.register_url("::1", 27810))
        self.assertEqual("http://[2001:db8::1]:80/",
                         server_config.register_url("[2001:db8::1]", 80))
        self.assertEqual("http://popshot.example.com:27810/",
                         server_config.register_url("popshot.example.com", 27810))

    def test_ensure_exists_does_not_clobber_a_hand_edited_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "server.config")
            with open(path, "w", encoding="utf-8") as f:
                f.write("server_address = 10.0.0.7\n")
            server_config.ensure_exists(path)
            values, _ = server_config.load(path)
            self.assertEqual("10.0.0.7", values["server_address"])

    def test_ensure_exists_writes_the_template_with_lf(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = server_config.ensure_exists(os.path.join(tmp, "server.config"))
            with open(path, "rb") as f:
                raw = f.read()
            # 这份文件要随 Linux 服务端包一起发，绝不能带 CR。
            self.assertNotIn(b"\r", raw)


class TicketStoreTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.tickets = TicketStore(ttl_seconds=60, clock=lambda: self.now)

    def test_a_ticket_resolves_back_to_its_owner(self):
        ticket = self.tickets.issue("alice")
        self.assertEqual("alice", self.tickets.resolve(ticket))

    def test_two_accounts_never_share_a_ticket(self):
        a = self.tickets.issue("alice")
        b = self.tickets.issue("bob")
        self.assertNotEqual(a, b)
        self.assertEqual("alice", self.tickets.resolve(a))
        self.assertEqual("bob", self.tickets.resolve(b))

    def test_resolving_does_not_consume(self):
        # 客户端和游戏服断线重连时会拿同一张票再来一次。
        ticket = self.tickets.issue("alice")
        self.assertEqual("alice", self.tickets.resolve(ticket))
        self.assertEqual("alice", self.tickets.resolve(ticket))

    def test_unknown_and_empty_tickets_resolve_to_nothing(self):
        self.assertIsNone(self.tickets.resolve(""))
        self.assertIsNone(self.tickets.resolve(None))
        self.assertIsNone(self.tickets.resolve("deadbeef"))

    def test_a_new_login_invalidates_the_previous_ticket(self):
        old = self.tickets.issue("alice")
        new = self.tickets.issue("alice")
        self.assertIsNone(self.tickets.resolve(old))
        self.assertEqual("alice", self.tickets.resolve(new))

    def test_tickets_expire(self):
        ticket = self.tickets.issue("alice")
        self.now += 61
        self.assertIsNone(self.tickets.resolve(ticket))
        self.assertEqual(0, len(self.tickets))

    def test_short_never_leaks_the_whole_ticket(self):
        ticket = self.tickets.issue("alice")
        self.assertNotIn(short(ticket), (ticket,))
        self.assertLessEqual(len(short(ticket)), 9)

    # -- 断线重连（§171 / D096 / D097）------------------------------------
    #
    # ★ 票据**只在内存里**（D097）：这一组盯的是「同一个服务端进程活着，
    #   但网断了一阵子」这一种情况 —— 服务端重启后作废是设计，不测。
    def test_resolving_slides_the_expiry(self):
        # 玩一局超过 TTL 再掉线，重连时那张票据必须还认得。
        ticket = self.tickets.issue("alice")
        for _ in range(10):
            self.now += 59
            self.assertEqual("alice", self.tickets.resolve(ticket))
        self.now += 61
        self.assertIsNone(self.tickets.resolve(ticket))

    def test_a_bound_ticket_lives_much_longer_than_an_unused_one(self):
        used, unused = self.tickets.issue("alice"), self.tickets.issue("bob")
        self.assertTrue(self.tickets.bind(used))
        self.assertTrue(self.tickets.is_bound(used))
        self.assertFalse(self.tickets.is_bound(unused))
        self.now += 3600            # 网络断了一小时才恢复
        self.assertEqual("alice", self.tickets.resolve(used))
        self.assertIsNone(self.tickets.resolve(unused))

    def test_binding_an_unknown_ticket_is_a_no_op(self):
        self.assertFalse(self.tickets.bind("deadbeef"))
        self.assertFalse(self.tickets.bind(""))


class AuthServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.accounts = AccountStore(os.path.join(self.tmp.name, "accounts.json"))
        self.tickets = TicketStore()
        self.service = authserver.AuthService(self.accounts, self.tickets)
        self.accounts.register("alice", "pw")

    def test_a_good_login_issues_a_ticket(self):
        result, ticket, _msg, status = self.service.login("alice", "pw")
        self.assertEqual(authserver.LOGIN_RESULT_OK, result)
        self.assertEqual(AUTH_OK, status)
        self.assertEqual("alice", self.tickets.resolve(ticket))

    def test_an_unknown_user_and_a_bad_password_get_different_codes(self):
        # 需求：未注册和密码错要在画面上分开提示。
        no_user = self.service.login("nobody", "pw")
        bad_pw = self.service.login("alice", "nope")
        self.assertEqual(authserver.LOGIN_RESULT_NO_SUCH_USER, no_user[0])
        self.assertEqual(authserver.LOGIN_RESULT_BAD_PASSWORD, bad_pw[0])
        self.assertNotEqual(no_user[2], bad_pw[2])
        self.assertEqual(("", ""), (no_user[1], bad_pw[1]))     # 不发票据

    def test_the_failure_codes_are_the_ones_the_client_understands(self):
        """★ 20025 / 20026 是**客户端认识的** NMCO 错误码（FINDINGS §128）。

        `nmconew.dll` 的 `0x10077000` 把包里的结果码映射成 NM 错误码，客户端拿它
        去 `Data/Chinese.ini` 查中文，弹「不存在的帐号」/「密码错误」。
        换成别的数（比如当初的 1 / 2）会全部落进 default 分支 -> 20000
        「认证服务器失败」，玩家只会看到一句没用的笼统话。别改。
        """
        self.assertEqual(20025, authserver.LOGIN_RESULT_NO_SUCH_USER)
        self.assertEqual(20026, authserver.LOGIN_RESULT_BAD_PASSWORD)
        self.assertEqual(0, authserver.LOGIN_RESULT_OK)

    def test_a_failed_login_issues_nothing(self):
        self.service.login("alice", "nope")
        self.assertEqual(0, len(self.tickets))

    def test_the_ticket_goes_into_the_second_string_field(self):
        """★ 实测：客户端转发给 `gcpReqLogin` 的是 `CULoginReplyPacket` 的
        **第二个**字符串（FINDINGS §123）。放错字段的话游戏服收到的会是
        那句中文说明，票据一律查不到、谁都登不进去 —— 这条测试就是为了
        别再把默认值改回 s1。"""
        import protocol as P

        class Args:
            reply = "login"
            sweep = None
            gap = 0.0
            result = 0
            ticket_field = "s2"
            allow_any = False

        payload = P.build_login("alice", "pw")
        frame = P.Frame(P.OPCODE_LOGIN, payload, key=0x11223344)
        replies = authserver.make_reply(frame, Args(), self.service)
        self.assertEqual(1, len(replies))
        _result, first, second, _a, _b, _c = P.parse_login_reply(
            P.unpack(replies[0]).payload)
        self.assertEqual("alice", self.tickets.resolve(second))
        self.assertIsNone(self.tickets.resolve(first))


class GameLoginTests(unittest.TestCase):
    """游戏服的 `0x0100 gcpReqLogin` 必须凭票据认人，认不出就断开。"""

    class FakeSocket:
        def __init__(self):
            self.writes = []
            self.closed = False

        def sendall(self, data):
            self.writes.append(bytes(data))

        def shutdown(self, _how):
            self.closed = True

        def close(self):
            self.closed = True

    class Args:
        hold_lobby = False
        accounts = None
        login_result = 0
        room_burst_delay = 0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.accounts = AccountStore(os.path.join(self.tmp.name, "accounts.json"))
        self.tickets = TicketStore()
        self.accounts.register("alice", "pw1")
        self.accounts.register("bob", "pw2")
        self.accounts.add_quest_reward("alice", experience=250, money=70)
        self._saved = list(gameserver._conns)
        self.addCleanup(lambda: gameserver._conns.__setitem__(
            slice(None), self._saved))
        gameserver._conns[:] = []

    def make_conn(self, addr=("::ffff:192.168.11.79", 50000)):
        conn = gameserver.Conn.__new__(gameserver.Conn)
        conn.args = self.Args()
        conn.addr = addr
        conn.connected_at = time.monotonic()
        conn.sock = self.FakeSocket()
        conn.cout = SimpleCipher.server_to_client()
        conn.send_lock = threading.RLock()
        conn.send_queue = None
        conn.batch_delay_ms = 0
        conn.seq = 1
        conn.logged = []
        conn.log = conn.logged.append
        # 连接事件日志：测试里只收进列表，不落盘也不打屏幕。
        conn.online_events = []
        conn.online = conn.online_events.append
        conn.vlog = lambda _msg: None
        conn.last_packet_at = 0.0
        conn.noisy_seen = set()
        conn.my_seat = 0
        conn.room = None
        conn.channel_code = 0
        conn.channel_index = 0
        conn.account_name = None
        conn.account = None
        conn.accounts = self.accounts
        conn.tickets = self.tickets
        conn.start_game = gameserver.StartGameHandshake()
        gameserver.register_conn(conn)
        return conn

    def sent_opcodes(self, conn):
        out = []
        for blob in conn.sock.writes:
            # 明文是逐包 encrypt 的，测试里只关心 opcode，直接解一遍流。
            out.append(blob)
        return out

    def login(self, conn, ticket):
        gameserver.Conn.on_game_packet(conn, 0x0100, gameserver.w_wstr(ticket))

    def test_a_valid_ticket_binds_that_account(self):
        conn = self.make_conn()
        self.login(conn, self.tickets.issue("alice"))
        self.assertEqual("alice", conn.account_name)
        self.assertEqual(250, conn.account["experience"])
        self.assertFalse(conn.sock.closed)

    def test_two_players_get_their_own_accounts(self):
        a, b = self.make_conn(), self.make_conn()
        self.login(a, self.tickets.issue("alice"))
        self.login(b, self.tickets.issue("bob"))
        self.assertEqual(("alice", "bob"), (a.account_name, b.account_name))
        self.assertEqual(250, a.account["experience"])
        self.assertEqual(0, b.account["experience"])

    def test_an_unknown_ticket_is_refused_and_never_falls_back(self):
        # V0.1 在这里会退回「随便给个本地账号」，联机之后那等于免密登录。
        conn = self.make_conn()
        self.login(conn, "not-a-real-ticket")
        self.assertIsNone(conn.account_name)
        payload = self.decode_first_login_reply(conn)
        # 认不出来的票据一律回 2（「现有连接已断开。请重新尝试连接。」，D097）——
        # 服务端重启后客户端拿旧票据来重连，走的就是这一路。
        self.assertEqual(gameserver.LOGIN_RESULT_SUPERSEDED,
                         struct.unpack_from("<i", payload, 0)[0])

    def test_an_empty_ticket_is_refused(self):
        conn = self.make_conn()
        self.login(conn, "")
        self.assertIsNone(conn.account_name)

    def test_a_ticket_for_a_deleted_account_is_refused(self):
        ticket = self.tickets.issue("ghost")
        conn = self.make_conn()
        self.login(conn, ticket)
        self.assertIsNone(conn.account_name)

    def test_logging_in_again_kicks_the_older_connection(self):
        # 两条连接同时往同一份存档里写，谁最后写谁赢 —— 必须只留一条。
        old, new = self.make_conn(), self.make_conn()
        self.login(old, self.tickets.issue("alice"))
        self.login(new, self.tickets.issue("alice"))
        self.assertTrue(old.sock.closed)
        self.assertFalse(new.sock.closed)

    def test_the_ticket_never_shows_up_whole_in_the_log(self):
        conn = self.make_conn()
        ticket = self.tickets.issue("alice")
        self.login(conn, ticket)
        self.assertFalse([line for line in conn.logged if ticket in line])

    # -- 断线自动重连（§132 顶号 / §171 网络故障 / D097 服务端重启）----------
    #
    # 客户端断线后不会安静地退回登录框，它**立刻自动重连**并重放手里那张票据。
    # 回 result=3 的话玩家看到的是「在无法连接的地方尝试了连接。」（像被封 IP），
    # 回 result=2 才是「现有连接已断开。请重新尝试连接。」——
    # 所以**所有认不出票据的情况都回 2**（D097）。

    def test_replaying_a_superseded_ticket_says_you_were_kicked(self):
        old_ticket = self.tickets.issue("alice")
        old = self.make_conn()
        self.login(old, old_ticket)
        self.login(self.make_conn(), self.tickets.issue("alice"))  # 别处顶号
        again = self.make_conn()          # 被踢的客户端自动重连，重放旧票据
        self.login(again, old_ticket)
        self.assertIsNone(again.account_name)
        payload = self.decode_first_login_reply(again)
        self.assertEqual(gameserver.LOGIN_RESULT_SUPERSEDED,
                         struct.unpack_from("<i", payload, 0)[0])

    def test_a_ticket_the_server_never_issued_also_says_reconnect(self):
        # 服务端重启过之后，客户端手里那张票据在新进程看来就是「没见过」——
        # 这是最常见的一路，必须和顶号一样回 2（D097）。
        conn = self.make_conn()
        self.login(conn, "deadbeef" * 4)
        payload = self.decode_first_login_reply(conn)
        self.assertEqual(gameserver.LOGIN_RESULT_SUPERSEDED,
                         struct.unpack_from("<i", payload, 0)[0])

    def test_a_login_without_any_ticket_still_says_bad_ticket(self):
        # 空票据不是重连，是协议级错误（手搓包 / 试探），保留 result=3。
        conn = self.make_conn()
        self.login(conn, "")
        payload = self.decode_first_login_reply(conn)
        self.assertEqual(gameserver.LOGIN_RESULT_BAD_TICKET,
                         struct.unpack_from("<i", payload, 0)[0])

    def test_the_kicked_message_code_is_two(self):
        # 客户端 0x54f416 那条分支的文案是写死的，改这个数就等于换文案。
        self.assertEqual(2, gameserver.LOGIN_RESULT_SUPERSEDED)
        self.assertEqual(3, gameserver.LOGIN_RESULT_BAD_TICKET)

    def test_a_superseded_ticket_is_not_resolvable_anymore(self):
        old_ticket = self.tickets.issue("alice")
        self.tickets.issue("alice")
        self.assertIsNone(self.tickets.resolve(old_ticket))
        self.assertEqual(("alice", "superseded"),
                         self.tickets.revoked_reason(old_ticket))

    def test_revoked_tickets_expire_too(self):
        # 作废记录跟着 TTL 走，不然长期开服的进程会无限攒。
        clock = [1000.0]
        store = TicketStore(ttl_seconds=60, clock=lambda: clock[0])
        old = store.issue("alice")
        store.issue("alice")
        self.assertIsNotNone(store.revoked_reason(old))
        clock[0] += 61
        self.assertIsNone(store.revoked_reason(old))

    # -- 连接事件日志（谁连上、谁断开、从哪个 IP）---------------------------

    def test_login_and_kick_are_written_to_the_connection_log(self):
        old = self.make_conn(("::ffff:192.168.11.79", 50001))
        self.login(old, self.tickets.issue("alice"))
        self.assertTrue(any("✓ 登录" in e and "'alice'" in e
                            and "192.168.11.79:50001" in e
                            for e in old.online_events))
        new = self.make_conn(("::ffff:192.168.11.211", 50002))
        self.login(new, self.tickets.issue("alice"))
        self.assertTrue(any("被顶号" in e and "192.168.11.211:50002" in e
                            for e in old.online_events))

    def test_the_connection_log_never_carries_a_ticket(self):
        conn = self.make_conn()
        ticket = self.tickets.issue("alice")
        self.login(conn, ticket)
        self.assertFalse([e for e in conn.online_events if ticket in e])

    def test_v4_mapped_addresses_are_shown_as_plain_ipv4(self):
        # `::` 双栈监听收 IPv4 连接时 getpeername 给的是 ::ffff:1.2.3.4，
        # 玩家报的 IP 里没有那个前缀，日志里也不该有。
        self.assertEqual("192.168.11.79:1234",
                         eventlog.peer(("::ffff:192.168.11.79", 1234)))
        self.assertEqual("[fe80::1]:1234", eventlog.peer(("fe80::1", 1234)))
        self.assertEqual("10.0.0.5:1", eventlog.peer(("10.0.0.5", 1)))
        self.assertEqual("?", eventlog.peer(None))

    def test_the_connection_log_writes_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "online.log")
            eventlog.configure(path=path)
            try:
                eventlog.online("测试 ✓ 登录 账号='alice' ip=10.0.0.1:1")
                with open(path, encoding="utf-8") as fh:
                    body = fh.read()
            finally:
                eventlog.configure(to_file=False)
        self.assertIn("[online]", body)
        self.assertIn("账号='alice'", body)

    def decode_first_login_reply(self, conn):
        stream = SimpleCipher.server_to_client()
        plain = bytearray()
        for blob in conn.sock.writes:
            plain += stream.decrypt(blob)
        buf = bytearray(plain)
        while buf:
            kind, opcode, payload, buf = gameserver.take_frame(buf)
            if kind == "game" and opcode == gameserver.OP_REP_LOGIN:
                return payload
        self.fail("没有发出 gspRepLogin")


class ControlChannelUserPickingTests(unittest.TestCase):
    """多人在线时控制通道**不许猜**要操作谁。"""

    class Fake:
        def __init__(self, name):
            self.account_name = name
            self.seq = 0

    def setUp(self):
        self._saved = list(gameserver._conns)
        self.addCleanup(lambda: gameserver._conns.__setitem__(
            slice(None), self._saved))
        gameserver._conns[:] = []

    def test_a_single_connection_needs_no_user(self):
        only = self.Fake("alice")
        gameserver._conns[:] = [only]
        self.assertIs(only, gameserver.pick_conn())

    def test_several_connections_refuse_to_guess(self):
        gameserver._conns[:] = [self.Fake("alice"), self.Fake("bob")]
        self.assertIsNone(gameserver.pick_conn())
        reply = gameserver.handle_control_command("status")
        self.assertTrue(reply.startswith("err"), reply)
        self.assertIn("--user", reply)

    def test_user_selects_the_right_connection(self):
        alice, bob = self.Fake("alice"), self.Fake("bob")
        gameserver._conns[:] = [alice, bob]
        self.assertIs(bob, gameserver.pick_conn("bob"))

    def test_who_lists_everyone(self):
        gameserver._conns[:] = [self.Fake("alice"), self.Fake("bob")]
        reply = gameserver.handle_control_command("who")
        self.assertIn("alice", reply)
        self.assertIn("bob", reply)

    def test_an_unknown_user_is_an_error_not_a_wrong_target(self):
        gameserver._conns[:] = [self.Fake("alice")]
        reply = gameserver.handle_control_command("status --user bob")
        self.assertTrue(reply.startswith("err"), reply)


class RegisterWebTests(unittest.TestCase):
    """注册页的四条路径：注册 / 重名 / 导出 / 导入。走真的 HTTP。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.accounts = AccountStore(os.path.join(cls.tmp.name, "accounts.json"))
        cls.httpd = web_server.make_server(0, cls.accounts, "127.0.0.1")
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    #: 账号名的去重计数器。用户名最长 16 个字符，光靠截断用例名是不够的 ——
    #: `test_register_*` 这一串截到前 12 个字符会**全部撞在一起**（踩过）。
    counter = 0

    def setUp(self):
        # 每个用例用自己的账号名，免得互相干扰（类级别只建一次服务器）。
        # 名字里那截用例名只是给日志留线索，唯一性由计数器保证。
        type(self).counter += 1
        self.who = f"u{self.counter}_{self.id().rsplit('.', 1)[-1][5:14]}"

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def post(self, path, payload):
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.url(path), data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_the_page_shows_the_server_address_from_the_host_header(self):
        with urllib.request.urlopen(self.url("/"), timeout=10) as response:
            html = response.read().decode("utf-8")
        self.assertIn(f"127.0.0.1:{self.port}", html)
        self.assertIn("明文传输并明文保存", html)
        self.assertIn("存档转移助手", html)
        # 上传页那句提示是需求逐字要求的，别被后来的改动顺手改掉。
        self.assertIn("更新服务器上已有的存档时需要填入正确的用户名密码。"
                      "若本服务器上没有保存过你的账号，则不需要填入。", html)

    def test_the_page_escapes_a_hostile_host_header(self):
        request = urllib.request.Request(
            self.url("/"), headers={"Host": '<script>alert(1)</script>'})
        with urllib.request.urlopen(request, timeout=10) as response:
            html = response.read().decode("utf-8")
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_register_then_duplicate(self):
        first = self.post("/api/register",
                          {"username": self.who, "password": "pw",
                           "password2": "pw"})
        self.assertTrue(first["ok"], first)
        again = self.post("/api/register",
                          {"username": self.who, "password": "pw",
                           "password2": "pw"})
        self.assertFalse(again["ok"])
        self.assertIn("该用户名已存在，请在登录界面直接登录", again["message"])

    def test_the_page_ships_the_skip_tutorial_box_checked(self):
        with urllib.request.urlopen(self.url("/"), timeout=10) as response:
            html = response.read().decode("utf-8")
        self.assertIn("跳过新手教程", html)
        # 需求：进注册页时这个框默认**勾着**。
        self.assertIn('<input id="skipTut" type="checkbox" checked>', html)
        self.assertIn("skip_tutorial:", html)

    def test_register_with_skip_tutorial_marks_the_save_completed(self):
        reply = self.post("/api/register",
                          {"username": self.who, "password": "pw",
                           "password2": "pw", "skip_tutorial": True})
        self.assertTrue(reply["ok"], reply)
        account = self.accounts.get_account(self.who)[1]
        self.assertTrue(account["tutorial_completed"])
        self.assertEqual(3, tutorial_state(account))

    def test_register_without_skip_tutorial_keeps_the_original_flow(self):
        reply = self.post("/api/register",
                          {"username": self.who, "password": "pw",
                           "password2": "pw", "skip_tutorial": False})
        self.assertTrue(reply["ok"], reply)
        account = self.accounts.get_account(self.who)[1]
        self.assertFalse(account["tutorial_completed"])
        self.assertEqual(0, tutorial_state(account))

    def test_register_defaults_to_the_original_flow_when_the_field_is_absent(self):
        # 直接调接口的人不说，就维持原版行为 —— 页面每次都会显式发这个字段。
        reply = self.post("/api/register",
                          {"username": self.who, "password": "pw",
                           "password2": "pw"})
        self.assertTrue(reply["ok"], reply)
        self.assertFalse(
            self.accounts.get_account(self.who)[1]["tutorial_completed"])

    def test_register_does_not_take_the_string_false_for_yes(self):
        # bool("false") 是 True —— 手搓请求的人栽在这上面会非常难查。
        reply = self.post("/api/register",
                          {"username": self.who, "password": "pw",
                           "password2": "pw", "skip_tutorial": "false"})
        self.assertTrue(reply["ok"], reply)
        self.assertFalse(
            self.accounts.get_account(self.who)[1]["tutorial_completed"])

    def test_register_rejects_a_mismatched_confirmation(self):
        reply = self.post("/api/register",
                          {"username": self.who, "password": "pw",
                           "password2": "other"})
        self.assertFalse(reply["ok"])
        self.assertFalse(self.accounts.has_account(self.who))

    def test_register_reports_a_bad_username_in_chinese(self):
        reply = self.post("/api/register",
                          {"username": "a b", "password": "pw",
                           "password2": "pw"})
        self.assertFalse(reply["ok"])
        self.assertIn("用户名", reply["message"])

    def test_export_needs_the_right_password(self):
        self.post("/api/register", {"username": self.who, "password": "pw",
                                    "password2": "pw"})
        bad = self.post("/api/export", {"username": self.who, "password": "no"})
        self.assertFalse(bad["ok"])
        self.assertNotIn("save", bad)
        good = self.post("/api/export", {"username": self.who, "password": "pw"})
        self.assertTrue(good["ok"], good)
        self.assertEqual(self.who, good["save"]["username"])

    def test_export_of_an_unknown_user_says_so(self):
        reply = self.post("/api/export", {"username": "nosuchguy",
                                          "password": "pw"})
        self.assertFalse(reply["ok"])
        self.assertIn("尚未注册", reply["message"])

    def test_import_creates_then_needs_the_password_to_replace(self):
        save = {"popshot_save": 1, "username": self.who,
                "account": {"password": "pw", "money": 42}}
        created = self.post("/api/import", {"save": save})
        self.assertTrue(created["ok"], created)
        self.assertEqual(42, self.accounts.get_account(self.who)[1]["money"])

        save["account"]["money"] = 99
        refused = self.post("/api/import",
                            {"save": save, "username": self.who,
                             "password": "wrong"})
        self.assertFalse(refused["ok"])
        self.assertEqual(42, self.accounts.get_account(self.who)[1]["money"])

        replaced = self.post("/api/import",
                             {"save": save, "username": self.who,
                              "password": "pw"})
        self.assertTrue(replaced["ok"], replaced)
        self.assertEqual(99, self.accounts.get_account(self.who)[1]["money"])

    def test_import_of_a_non_save_file_is_reported(self):
        reply = self.post("/api/import", {"save": {"hello": "world"}})
        self.assertFalse(reply["ok"])
        self.assertIn("存档", reply["message"])

    def test_an_unknown_api_is_a_clean_404(self):
        # ★ 「clean」是字面意思：404 也必须先把请求体读干净再回。
        #   不读的话，keep-alive 的下一次解析会撞上剩下的 body，连接被掐掉，
        #   客户端拿到的是 ConnectionAbortedError 而不是 404（还时有时无）。
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/nope", {"padding": "x" * 4096})
        self.assertEqual(404, ctx.exception.code)
        # 同一条 keep-alive 连接上紧接着的请求必须照常工作。
        reply = self.post("/api/register",
                          {"username": self.who, "password": "pw",
                           "password2": "pw"})
        self.assertTrue(reply["ok"], reply)


class RelayTests(unittest.TestCase):
    """本机中继：纯字节转发，不碰协议（D065）。"""

    def echo_server(self):
        """一个把收到的字节原样回吐的服务器，当「远端」用。"""
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)

        def run():
            try:
                sock, _ = listener.accept()
            except OSError:
                return
            with sock:
                while True:
                    data = sock.recv(4096)
                    if not data:
                        return
                    sock.sendall(data)

        threading.Thread(target=run, daemon=True).start()
        return listener.getsockname()[1]

    def start_relay(self, remote_port):
        local = socket.create_server(("127.0.0.1", 0))
        local_port = local.getsockname()[1]
        local.close()
        ready = threading.Event()
        threading.Thread(
            target=relay.serve_one,
            args=(local_port, "127.0.0.1", remote_port, "测试", ready),
            daemon=True).start()
        self.assertTrue(ready.wait(timeout=10), "中继没起来")
        return local_port

    def test_bytes_pass_through_untouched(self):
        # 两层协议都是有状态的流密码，中继一旦改字节就全乱了。
        local_port = self.start_relay(self.echo_server())
        blob = bytes(range(256)) * 8
        with socket.create_connection(("127.0.0.1", local_port), timeout=10) as c:
            c.sendall(blob)
            got = b""
            while len(got) < len(blob):
                chunk = c.recv(65536)
                if not chunk:
                    break
                got += chunk
        self.assertEqual(blob, got)

    def test_an_unreachable_target_closes_the_client_connection(self):
        # 连不上就干脆断开，让客户端弹它自己的「认证服务器失败」框，而不是干等。
        dead = socket.create_server(("127.0.0.1", 0))
        dead_port = dead.getsockname()[1]
        dead.close()
        local_port = self.start_relay(dead_port)
        with socket.create_connection(("127.0.0.1", local_port), timeout=10) as c:
            self.assertEqual(b"", c.recv(16))

    def test_the_port_map_never_collides_with_the_local_server(self):
        # 单机后端和中继后端必须听不同的口，模式判定才是零状态的（D066）。
        local_ports = {entry[0] for entry in relay.PORT_MAP}
        server_ports = {server_config.AUTH_PORT, server_config.GAME_PORT,
                        server_config.CONTROL_PORT}
        self.assertEqual(set(), local_ports & server_ports)


class DualStackListenTests(unittest.TestCase):
    """监听地址固定 `::`，IPv4 和 IPv6 的客户端都要连得进来（D063）。"""

    def test_a_v6_listener_also_accepts_v4_clients(self):
        s = gameserver.listen(0, "::")
        self.addCleanup(s.close)
        port = s.getsockname()[1]
        if s.family != socket.AF_INET6:
            self.skipTest("这台机器没有可用的 IPv6，已退回 IPv4 监听")
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            served, _ = s.accept()
            served.close()
            self.assertTrue(client)
        with socket.socket(socket.AF_INET6) as client6:
            client6.settimeout(5)
            client6.connect(("::1", port))
            served, _ = s.accept()
            served.close()


if __name__ == "__main__":
    unittest.main()
