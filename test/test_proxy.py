#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端本机中继的 SOCKS5 / HTTP CONNECT 代理回归测试。"""
from __future__ import annotations

import base64
import socket
import struct
import threading
import time
import unittest
from unittest import mock

import config as server_config
import relay


def recv_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise EOFError("测试代理收到意外 EOF")
        data.extend(chunk)
    return bytes(data)


def echo_tunnel(sock):
    while True:
        data = sock.recv(4096)
        if not data:
            return
        sock.sendall(data)


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def parse_socks5_udp_header(datagram):
    """假代理自己解 SOCKS5 UDP 头（RFC 1928 §7）-> `(atyp, host, port, 载荷)`。

    ★ 故意**不复用** `relay.socks5_udp_unwrap`：假代理要像真代理那样独立地看包，
    否则封包 / 拆包互相印证等于没测。
    """
    if len(datagram) < 4 or datagram[0:2] != b"\x00\x00":
        raise AssertionError(f"SOCKS5 UDP 头 RSV 不是 0: {datagram[:4]!r}")
    if datagram[2] != 0:
        raise AssertionError(f"SOCKS5 UDP 头 FRAG 不是 0: {datagram[2]}")
    atyp = datagram[3]
    if atyp == 1:
        host = socket.inet_ntop(socket.AF_INET, datagram[4:8])
        pos = 8
    elif atyp == 4:
        host = socket.inet_ntop(socket.AF_INET6, datagram[4:20])
        pos = 20
    elif atyp == 3:
        length = datagram[4]
        host = datagram[5:5 + length].decode("ascii")
        pos = 5 + length
    else:
        raise AssertionError(f"未知 SOCKS5 ATYP {atyp}")
    port = struct.unpack("!H", datagram[pos:pos + 2])[0]
    return atyp, host, port, datagram[pos + 2:]


class FakeProxyMixin:
    """假 SOCKS5 / HTTP CONNECT 代理。`test_udpsync` 也混它进来测经代理的 UDP 旁路。"""

    def start_fake_proxy(self, handler, accept_many=False):
        listener = socket.create_server(("127.0.0.1", 0))
        state = {"connections": 0}

        def serve(sock):
            try:
                with sock:
                    handler(sock, state)
            except OSError as error:
                # 清理阶段关闭 listener 时，没有连接进来的测试不应把它算成故障。
                if listener.fileno() >= 0:
                    state["error"] = error
            except BaseException as error:
                state["error"] = error

        def run():
            try:
                while True:
                    sock, _ = listener.accept()
                    state["connections"] += 1
                    if not accept_many:
                        serve(sock)
                        return
                    # 「关联死了再重建」那种用例要连第二次：每条连接一条线程。
                    threading.Thread(target=serve, args=(sock,), daemon=True).start()
            except OSError:
                return              # listener 被清理阶段关掉了
            finally:
                listener.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(listener.close)
        return listener.getsockname()[1], state, thread

    def start_socks5(self, username="", password="", reject=False, reject_status=5,
                     udp=False, bnd_zero=False, resolve=None, accept_many=False):
        """假 SOCKS5 代理。`udp=True` 时接受 UDP ASSOCIATE 并真的中转 UDP（`_serve_udp_association`）；
        `bnd_zero` 让应答的 BND.ADDR 是 `0.0.0.0`（不少代理这么回）；`resolve` 是假代理侧的
        「域名 -> 主机」表（目标写域名时代理得自己解）。"""
        def handler(sock, state):
            version, count = recv_exact(sock, 2)
            methods = recv_exact(sock, count)
            state["greeting"] = (version, methods)
            method = 2 if username else 0
            if version != 5 or method not in methods:
                sock.sendall(b"\x05\xff")
                return
            sock.sendall(bytes((5, method)))

            if method == 2:
                auth_version, user_len = recv_exact(sock, 2)
                user = recv_exact(sock, user_len).decode("utf-8")
                password_len = recv_exact(sock, 1)[0]
                supplied_password = recv_exact(sock, password_len).decode("utf-8")
                state["auth"] = (auth_version, user, supplied_password)
                ok = auth_version == 1 and user == username and supplied_password == password
                sock.sendall(bytes((1, 0 if ok else 1)))
                if not ok:
                    return

            version, command, reserved, atyp = recv_exact(sock, 4)
            if atyp == 1:
                host = socket.inet_ntop(socket.AF_INET, recv_exact(sock, 4))
            elif atyp == 4:
                host = socket.inet_ntop(socket.AF_INET6, recv_exact(sock, 16))
            elif atyp == 3:
                length = recv_exact(sock, 1)[0]
                host = recv_exact(sock, length).decode("ascii")
            else:
                raise AssertionError(f"未知 SOCKS5 ATYP {atyp}")
            port = struct.unpack("!H", recv_exact(sock, 2))[0]
            state["request"] = (version, command, reserved, atyp, host, port)
            state.setdefault("requests", []).append(state["request"])
            if reject:
                sock.sendall(bytes((5, reject_status, 0, 1)) + b"\x00\x00\x00\x00\x00\x00")
                return
            if command == relay.SOCKS5_CMD_UDP_ASSOCIATE:
                if not udp:
                    # 不支持 UDP ASSOCIATE 的代理：状态 7（Command not supported）
                    sock.sendall(bytes((5, 7, 0, 1)) + b"\x00\x00\x00\x00\x00\x00")
                    return
                self._serve_udp_association(sock, state, bnd_zero, resolve or {})
                return
            sock.sendall(bytes((5, 0, 0, 1)) + b"\x00\x00\x00\x00\x00\x00")
            echo_tunnel(sock)

        port, state, thread = self.start_fake_proxy(handler, accept_many=accept_many)
        proxy = relay.ProxySettings("socks5", "127.0.0.1", port,
                                    username, password)
        return proxy, state, thread

    @staticmethod
    def _serve_udp_association(sock, state, bnd_zero, resolve):
        """一条 UDP 关联：bind 一个中转口、回 BND，然后握着控制连接直到对方（或测试）关掉。

        中转规则和真代理一样（RFC 1928 §7）：第一发数据报的来源就是客户端；客户端来的拆头、
        按头里的地址转给目标；别处来的套上「来源地址」的头回给客户端。
        控制连接一断就关中转口（关联作废）。测试用 `state["kill_control"]()` 模拟代理撤关联。
        """
        relay_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        relay_sock.bind(("127.0.0.1", 0))
        relay_port = relay_sock.getsockname()[1]
        state["control"] = sock
        state["relay_addr"] = ("127.0.0.1", relay_port)
        state["associations"] = state.get("associations", 0) + 1
        state.setdefault("udp_targets", [])

        def kill_control():
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

        state["kill_control"] = kill_control

        def pump():
            client = None
            while True:
                try:
                    data, src = relay_sock.recvfrom(65536)
                except OSError:
                    return
                if client is None or src == client:
                    client = src
                    try:
                        atyp, host, port, payload = parse_socks5_udp_header(data)
                    except AssertionError as error:
                        state["error"] = error
                        continue
                    state["udp_targets"].append((atyp, host, port))
                    try:
                        relay_sock.sendto(payload, (resolve.get(host, host), port))
                    except OSError as error:
                        state["error"] = error
                else:
                    header = (b"\x00\x00\x00\x01" + socket.inet_aton(src[0])
                              + struct.pack("!H", src[1]))
                    relay_sock.sendto(header + data, client)

        threading.Thread(target=pump, daemon=True).start()
        bnd = b"\x00\x00\x00\x00" if bnd_zero else socket.inet_aton("127.0.0.1")
        sock.sendall(bytes((5, 0, 0, 1)) + bnd + struct.pack("!H", relay_port))
        try:
            while sock.recv(4096):          # 控制连接：握着直到对方（或测试）关掉
                pass
        except OSError:
            pass
        finally:
            relay_sock.close()              # 关联随控制连接一起撤掉

    def start_http(self, username="", password="", banner=b""):
        def handler(sock, state):
            request = bytearray()
            while b"\r\n\r\n" not in request:
                request.extend(sock.recv(4096))
                if len(request) > 65536:
                    raise AssertionError("HTTP CONNECT 请求过大")
            lines = bytes(request).split(b"\r\n")
            state["request_line"] = lines[0].decode("ascii")
            state["headers"] = {
                key.strip().lower(): value.strip()
                for key, value in (line.decode("ascii").split(":", 1)
                                   for line in lines[1:] if b":" in line)
            }
            sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n" + banner)
            echo_tunnel(sock)

        port, state, thread = self.start_fake_proxy(handler)
        proxy = relay.ProxySettings("http", "127.0.0.1", port,
                                    username, password)
        return proxy, state, thread

    def assert_tunnel_echoes(self, target_host, target_port, proxy, blob=b"proxy-test"):
        with relay.connect_remote(target_host, target_port, proxy) as sock:
            sock.sendall(blob)
            self.assertEqual(blob, recv_exact(sock, len(blob)))


class ProxyConfigTests(unittest.TestCase):
    def test_old_or_empty_config_keeps_direct_connections(self):
        old_values, warnings = server_config.parse_text(
            "server_address = popshot.example.com\n")
        self.assertEqual([], warnings)
        self.assertIsNone(relay.proxy_from_config(old_values))

        empty_values, warnings = server_config.parse_text(
            "proxy_type = http\nproxy_address =   \nproxy_port = 8080\n")
        self.assertEqual([], warnings)
        self.assertIsNone(relay.proxy_from_config(empty_values))

    def test_proxy_fields_parse_and_ipv6_brackets_are_removed(self):
        values, warnings = server_config.parse_text(
            "proxy_type = http\n"
            "proxy_address = [2001:db8::2]\n"
            "proxy_port = 8080\n"
            "proxy_username = alice\n"
            "proxy_password = secret#part\n")
        self.assertEqual([], warnings)
        proxy = relay.proxy_from_config(values)
        self.assertEqual("http", proxy.kind)
        self.assertEqual("2001:db8::2", proxy.host)
        self.assertEqual(8080, proxy.port)
        self.assertEqual("alice", proxy.username)
        self.assertEqual("secret#part", proxy.password)

    def test_invalid_type_is_rejected_only_when_proxy_is_enabled(self):
        values = dict(server_config.DEFAULTS, proxy_type="broken")
        self.assertIsNone(relay.proxy_from_config(values))
        values["proxy_address"] = "127.0.0.1"
        with self.assertRaisesRegex(ValueError, "socks5.*http"):
            relay.proxy_from_config(values)

    def test_password_without_username_is_rejected(self):
        values = dict(server_config.DEFAULTS,
                      proxy_address="127.0.0.1", proxy_password="secret")
        with self.assertRaisesRegex(ValueError, "proxy_username"):
            relay.proxy_from_config(values)


class Socks5ProxyTests(FakeProxyMixin, unittest.TestCase):
    def test_domain_target_uses_socks5_without_local_dns(self):
        proxy, state, thread = self.start_socks5()
        self.assert_tunnel_echoes("game.popshot.example", 27799, proxy)
        self.assertEqual((5, b"\x00"), state["greeting"])
        self.assertEqual((5, 1, 0, 3, "game.popshot.example", 27799),
                         state["request"])
        self.assertNotIn("error", state)

    def test_username_and_password_authentication(self):
        proxy, state, thread = self.start_socks5("alice", "secret")
        self.assert_tunnel_echoes("127.0.0.1", 47611, proxy)
        self.assertEqual((5, b"\x02"), state["greeting"])
        self.assertEqual((1, "alice", "secret"), state["auth"])
        self.assertEqual((5, 1, 0, 1, "127.0.0.1", 47611), state["request"])
        self.assertNotIn("secret", proxy.route)
        self.assertNotIn("alice", proxy.route)
        self.assertNotIn("error", state)

    def test_proxy_rejection_does_not_fall_back_to_a_direct_connection(self):
        target = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(target.close)
        target.settimeout(0.2)
        proxy, state, thread = self.start_socks5(reject=True)
        with self.assertRaisesRegex(relay.ProxyError, "目标拒绝连接"):
            relay.connect_remote("127.0.0.1", target.getsockname()[1], proxy)
        with self.assertRaises(socket.timeout):
            target.accept()


class HttpProxyTests(FakeProxyMixin, unittest.TestCase):
    def test_connect_tunnel_and_basic_authentication(self):
        proxy, state, thread = self.start_http("alice", "secret")
        self.assert_tunnel_echoes("game.popshot.example", 27799, proxy)
        self.assertEqual("CONNECT game.popshot.example:27799 HTTP/1.1",
                         state["request_line"])
        expected = base64.b64encode(b"alice:secret").decode("ascii")
        self.assertEqual(f"Basic {expected}",
                         state["headers"]["proxy-authorization"])
        self.assertNotIn("error", state)

    def test_ipv6_target_is_bracketed_in_connect_authority(self):
        proxy, state, thread = self.start_http()
        self.assert_tunnel_echoes("2001:db8::10", 47611, proxy)
        self.assertEqual("CONNECT [2001:db8::10]:47611 HTTP/1.1",
                         state["request_line"])
        self.assertNotIn("error", state)

    def test_target_bytes_coalesced_with_connect_reply_are_not_lost(self):
        proxy, state, thread = self.start_http(banner=b"server-hello")
        with relay.connect_remote("game.popshot.example", 27799, proxy) as sock:
            self.assertEqual(b"server-hello", recv_exact(sock, 12))
        self.assertNotIn("error", state)


class ProxyLogTests(FakeProxyMixin, unittest.TestCase):
    def test_client_relay_log_records_the_route_actually_used(self):
        proxy, state, proxy_thread = self.start_socks5("alice", "secret")
        client, local = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(local.close)
        client.settimeout(5)
        messages = []

        with mock.patch.object(relay, "log", side_effect=messages.append):
            worker = threading.Thread(
                target=relay.handle,
                args=(local, ("127.0.0.1", 12345),
                      "game.popshot.example", 27799, "游戏", proxy),
                daemon=True)
            worker.start()
            client.sendall(b"logged-route")
            self.assertEqual(b"logged-route", recv_exact(client, 12))
            client.shutdown(socket.SHUT_WR)
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive(), "中继连接没有正常收尾")

        joined = "\n".join(messages)
        self.assertIn("✓ 游戏服", joined)
        self.assertIn(proxy.route, joined)
        self.assertNotIn("alice", joined)
        self.assertNotIn("secret", joined)
        self.assertNotIn("error", state)


# ----------------------------------------------------------------------------
# SOCKS5 UDP ASSOCIATE（X_Mod D71）：位置数据的 UDP 旁路经代理
# ----------------------------------------------------------------------------
class Socks5UdpHeaderTests(unittest.TestCase):
    """每发数据报的 SOCKS5 UDP 头（RFC 1928 §7）：套上 / 拆掉。"""

    def test_wrap_and_unwrap_round_trip_for_ipv4_ipv6_and_domain(self):
        for host, atyp in (("192.0.2.1", 1), ("2001:db8::10", 4),
                           ("game.popshot.example", 3)):
            header = relay.socks5_udp_header(host, 27799)
            self.assertEqual(b"\x00\x00\x00", header[:3], host)     # RSV=0 FRAG=0
            self.assertEqual(atyp, header[3], host)
            payload, source = relay.socks5_udp_unwrap(
                relay.socks5_udp_wrap(header, b"PSU\x01hello"))
            self.assertEqual(b"PSU\x01hello", payload)
            self.assertEqual((host, 27799), source)

    def test_the_domain_goes_into_the_header_verbatim_no_local_dns(self):
        """★ 域名交给代理解（ATYP 3），和 TCP CONNECT 一个口径 —— 本机一次都不解析。"""
        header = relay.socks5_udp_header("game.popshot.example", 27799)
        self.assertEqual(b"\x03" + bytes((len("game.popshot.example"),))
                         + b"game.popshot.example" + b"\x6c\x97", header[3:])

    def test_fragments_and_garbage_are_not_datagrams(self):
        header = relay.socks5_udp_header("192.0.2.1", 1)
        fragment = header[:2] + b"\x01" + header[3:]
        self.assertIsNone(relay.socks5_udp_unwrap(fragment + b"x"))
        self.assertIsNone(relay.socks5_udp_unwrap(b"\x00\x01" + header[2:] + b"x"))
        self.assertIsNone(relay.socks5_udp_unwrap(b"\x00\x00\x00\x09abc"))   # ATYP 不认识
        self.assertIsNone(relay.socks5_udp_unwrap(header[:-1]))              # 端口少一字节
        self.assertIsNone(relay.socks5_udp_unwrap(b"\x00\x00\x00\x03\x20abc"))  # 域名长度越界
        self.assertIsNone(relay.socks5_udp_unwrap(b"PSU\x01" + b"\x00" * 8))  # 我们自己的线格式
        self.assertIsNone(relay.socks5_udp_unwrap(b""))

    def test_an_empty_payload_is_still_a_datagram(self):
        header = relay.socks5_udp_header("192.0.2.1", 5)
        self.assertEqual((b"", ("192.0.2.1", 5)), relay.socks5_udp_unwrap(header))


class Socks5UdpAssociateTests(FakeProxyMixin, unittest.TestCase):
    def associate(self, proxy):
        sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
        self.addCleanup(sock.close)
        return sock, relay._socks5_udp_associate(sock, proxy)

    def test_the_relay_address_comes_from_the_reply(self):
        proxy, state, _ = self.start_socks5(udp=True)
        _sock, relay_addr = self.associate(proxy)
        self.assertEqual(state["relay_addr"], relay_addr)
        # DST.ADDR / DST.PORT 按 RFC 1928 填全零
        self.assertEqual((5, relay.SOCKS5_CMD_UDP_ASSOCIATE, 0, 1, "0.0.0.0", 0),
                         state["request"])
        self.assertNotIn("error", state)

    def test_an_unspecified_bind_address_means_the_proxy_itself(self):
        """`BND.ADDR = 0.0.0.0` 的意思是「发到你连我的这个地址」—— 用控制连接的对端，
        不用 `proxy.host`（那可能是域名，又得本机解析一次）。"""
        proxy, state, _ = self.start_socks5(udp=True, bnd_zero=True)
        _sock, relay_addr = self.associate(proxy)
        self.assertEqual(("127.0.0.1", state["relay_addr"][1]), relay_addr)

    def test_a_proxy_without_udp_support_is_reported_not_worked_around(self):
        proxy, state, _ = self.start_socks5(udp=False)
        sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
        self.addCleanup(sock.close)
        with self.assertRaisesRegex(relay.ProxyError, "UDP ASSOCIATE"):
            relay._socks5_udp_associate(sock, proxy)

    def test_authentication_is_shared_with_connect(self):
        proxy, state, _ = self.start_socks5("alice", "secret", udp=True)
        self.associate(proxy)
        self.assertEqual((1, "alice", "secret"), state["auth"])
        self.assertNotIn("error", state)

    def test_connect_still_works_after_the_split(self):
        """拆出 `_socks5_handshake` / `_socks5_request` 之后 CONNECT 一个字节都不变。"""
        proxy, state, _ = self.start_socks5()
        self.assert_tunnel_echoes("127.0.0.1", 27799, proxy)
        self.assertEqual((5, relay.SOCKS5_CMD_CONNECT, 0, 1, "127.0.0.1", 27799),
                         state["request"])


class Socks5UdpUpstreamTests(FakeProxyMixin, unittest.TestCase):
    """`_Socks5UdpUpstream`：长得像一个 UDP socket，中间经代理。"""

    def target_socket(self):
        target = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        target.bind(("127.0.0.1", 0))
        target.settimeout(5)
        self.addCleanup(target.close)
        return target

    def test_datagrams_are_wrapped_going_out_and_unwrapped_coming_back(self):
        target = self.target_socket()
        target_port = target.getsockname()[1]
        proxy, state, _ = self.start_socks5(
            udp=True, resolve={"game.popshot.example": "127.0.0.1"})
        upstream = relay._Socks5UdpUpstream(proxy, "game.popshot.example", target_port)
        self.addCleanup(upstream.close)
        self.assertEqual(state["relay_addr"], upstream.relay_addr)

        upstream.sendto(b"PSU\x01ping", upstream.relay_addr)
        data, proxy_side = target.recvfrom(4096)
        self.assertEqual(b"PSU\x01ping", data)               # 目标收到的是拆好头的载荷
        self.assertEqual([(3, "game.popshot.example", target_port)], state["udp_targets"])

        target.sendto(b"PSU\x01pong", proxy_side)
        payload, source = upstream.recvfrom(4096)
        self.assertEqual(b"PSU\x01pong", payload)             # 回来的头被拆掉了
        self.assertEqual(("127.0.0.1", target_port), source)
        self.assertNotIn("error", state)

    def test_garbage_on_the_udp_socket_is_not_a_reply(self):
        """坏头 / 分片不算一发回应：继续等，等到超时照常抛 `socket.timeout`。"""
        target = self.target_socket()
        proxy, state, _ = self.start_socks5(udp=True)
        upstream = relay._Socks5UdpUpstream(proxy, "127.0.0.1", target.getsockname()[1])
        self.addCleanup(upstream.close)
        upstream.sendto(b"PSU\x01ping", upstream.relay_addr)   # 顺便让 socket 绑上口
        target.recvfrom(4096)
        our_port = upstream.sock.getsockname()[1]
        junk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(junk.close)
        junk.sendto(b"PSU\x01not-a-socks-datagram", ("127.0.0.1", our_port))
        junk.sendto(b"\x00\x00\x01\x01\x7f\x00\x00\x01\x00\x01frag", ("127.0.0.1", our_port))
        with self.assertRaises(socket.timeout):
            upstream.recvfrom(4096)

    def test_the_watcher_reports_the_proxy_dropping_the_control_connection(self):
        """RFC 1928 §7：控制连接一断关联就没了。这是一个**事件**（EOF），守望线程报一次。"""
        proxy, state, _ = self.start_socks5(udp=True)
        dead = []
        upstream = relay._Socks5UdpUpstream(proxy, "127.0.0.1", 27799,
                                            on_dead=dead.append)
        self.addCleanup(upstream.close)
        state["kill_control"]()
        self.assertTrue(wait_for(lambda: dead == [upstream]))

    def test_closing_it_ourselves_is_not_a_death(self):
        proxy, state, _ = self.start_socks5(udp=True)
        dead = []
        upstream = relay._Socks5UdpUpstream(proxy, "127.0.0.1", 27799,
                                            on_dead=dead.append)
        upstream.close()
        self.assertLess(upstream.fileno(), 0)
        # 「不该回调」没有事件可等，只能看一小段时间里真没来（测试专用）。
        time.sleep(0.2)
        self.assertEqual([], dead)

    def test_a_refused_association_leaves_nothing_behind(self):
        proxy, state, _ = self.start_socks5(udp=False)
        with self.assertRaisesRegex(relay.ProxyError, "UDP ASSOCIATE"):
            relay._Socks5UdpUpstream(proxy, "127.0.0.1", 27799)


if __name__ == "__main__":
    unittest.main()
