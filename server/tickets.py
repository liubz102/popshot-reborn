#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""登录票据 —— 把「认证服认出来的人」交给游戏服。

V0.1 是单机假后台，认证服把用户名写进 `accounts.json` 的 ``active_account``，
游戏服再读出来。多账号之后这条路直接失效：两个人同时登录就会互相顶掉。

原版协议里本来就有现成的通道（FINDINGS §123）：

```text
认证服 0x000f CULogin2Packet(用户名, 密码)
    -> 0x000c CULoginReplyPacket(result, s1, s2, a, b, c)
客户端 -> 游戏服 0x0100 gcpReqLogin
    [0] wstring   ← ★ 就是认证服下发的那个字符串，客户端原样转发
    [1] 本机内网 IP ...
```

所以认证服签发一个随机票据塞进应答的字符串字段，游戏服拿收到的 wstring 查回账号。

票据只活在**进程内存**里（认证服和游戏服合并成一个进程正是为了这个，D064），
服务端一重启全部作废 —— 这没问题，玩家本来就要重新登录。
"""
from __future__ import annotations

import secrets
import threading
import time


#: 票据有效期（秒）。客户端拿到票据后要走完 `0x2d` / `0x0d` 两次认证服往返，
#: 再连游戏服、发版本号、等应答，正常只要几秒。给 10 分钟是留给
#: 「登录框停在那儿没点开始」以外的意外（比如换图分辨率卡一下）。
DEFAULT_TTL_SECONDS = 600

#: 票据被作废的原因。目前只有一种，但游戏服要靠它挑给客户端看的错误码，
#: 所以做成常量而不是布尔（§132）。
REVOKED_SUPERSEDED = "superseded"


class TicketStore:
    """``票据 -> 用户名`` 的内存表，带过期清理。线程安全。"""

    def __init__(self, ttl_seconds=DEFAULT_TTL_SECONDS, clock=time.monotonic):
        self.ttl = float(ttl_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        #: 票据 -> (用户名, 签发时刻)
        self._tickets = {}
        #: 被顶掉的旧票据 -> (用户名, 作废原因, 作废时刻)。
        #: ★ 存在的唯一理由：被顶号的客户端会**自动重连**并重放这张旧票据
        #:   （§132）。查不到就只能回「票据无效」，客户端弹的是
        #:   「在无法连接的地方尝试了连接。」—— 驴唇不对马嘴。记一笔就能改回
        #:   「现有连接已断开。请重新尝试连接。」。同样按 TTL 过期。
        self._revoked = {}

    def _purge_unlocked(self):
        deadline = self._clock() - self.ttl
        for ticket in [t for t, (_, at) in self._tickets.items() if at < deadline]:
            del self._tickets[ticket]
        for ticket in [t for t, (_, _, at) in self._revoked.items() if at < deadline]:
            del self._revoked[ticket]

    def issue(self, username):
        """给一个账号签发新票据。

        同一个账号再登录一次会**作废它之前的票据**：登录框里改个密码重来一次是
        常见操作，留着旧票据只会让「上一次那半条流程」还能走通。
        作废的票据进 `_revoked`，好让重放它的老客户端拿到对得上的提示。
        """
        username = str(username)
        ticket = secrets.token_hex(16)
        now = self._clock()
        with self._lock:
            self._purge_unlocked()
            for old, (name, _) in list(self._tickets.items()):
                if name == username:
                    del self._tickets[old]
                    self._revoked[old] = (name, REVOKED_SUPERSEDED, now)
            self._tickets[ticket] = (username, now)
        return ticket

    def resolve(self, ticket):
        """票据 -> 用户名；无效或过期返回 ``None``。

        ★ **不消费票据**：客户端和游戏服之间断线重连时会拿同一个票据再来一次，
        一次性票据会让它永远进不来。过期由 TTL 兜底。
        """
        ticket = str(ticket or "").strip()
        if not ticket:
            return None
        with self._lock:
            self._purge_unlocked()
            entry = self._tickets.get(ticket)
            return entry[0] if entry else None

    def revoked_reason(self, ticket):
        """这张票据是被**顶掉**的吗？是就返回 ``(用户名, 原因)``，否则 ``None``。

        `resolve()` 返回 `None` 之后才问这一句。分得清「被顶号」和「压根没见过
        这张票」，游戏服才能给客户端挑对错误码（§132 / D071）。
        """
        ticket = str(ticket or "").strip()
        if not ticket:
            return None
        with self._lock:
            self._purge_unlocked()
            entry = self._revoked.get(ticket)
            return (entry[0], entry[1]) if entry else None

    def revoke(self, ticket):
        ticket = str(ticket or "").strip()
        with self._lock:
            entry = self._tickets.pop(ticket, None)
            if entry:
                self._revoked[ticket] = (entry[0], REVOKED_SUPERSEDED,
                                         self._clock())

    def revoke_user(self, username):
        username = str(username)
        now = self._clock()
        with self._lock:
            for ticket, (name, _) in list(self._tickets.items()):
                if name == username:
                    del self._tickets[ticket]
                    self._revoked[ticket] = (name, REVOKED_SUPERSEDED, now)

    def __len__(self):
        with self._lock:
            self._purge_unlocked()
            return len(self._tickets)


def short(ticket):
    """票据的日志形式：只露前 8 位。

    完整票据等同于一次登录凭证，日志会被贴进 issue、发给别人排查（D067）。
    """
    ticket = str(ticket or "")
    return (ticket[:8] + "…") if len(ticket) > 8 else ticket
