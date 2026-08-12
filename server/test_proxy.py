#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端本机中继的 SOCKS5 / HTTP CONNECT 代理回归测试。"""
from __future__ import annotations

import base64
import socket
import struct
import threading
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


class FakeProxyMixin:
    def start_fake_proxy(self, handler):
        listener = socket.create_server(("127.0.0.1", 0))
        state = {}

        def run():
            try:
                sock, _ = listener.accept()
                with sock:
                    handler(sock, state)
            except OSError as error:
                # 清理阶段关闭 listener 时，没有连接进来的测试不应把它算成故障。
                if listener.fileno() >= 0:
                    state["error"] = error
            except BaseException as error:
                state["error"] = error
            finally:
                listener.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(listener.close)
        return listener.getsockname()[1], state, thread

    def start_socks5(self, username="", password="", reject=False):
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
            status = 5 if reject else 0
            sock.sendall(bytes((5, status, 0, 1)) + b"\x00\x00\x00\x00\x00\x00")
            if not reject:
                echo_tunnel(sock)

        port, state, thread = self.start_fake_proxy(handler)
        proxy = relay.ProxySettings("socks5", "127.0.0.1", port,
                                    username, password)
        return proxy, state, thread

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


if __name__ == "__main__":
    unittest.main()
