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

★ **票据同时也是「断线重连」的凭证**（§171 / D096）。服务端一断，客户端会
自己弹「发生服务器障碍,自动尝试连接。」并**反复重连、每次都重放同一张票据**
（`0x54da2e` → `0x5bc41a`），从头到尾**不会**回认证服重新登录。所以：

- 票据的有效期必须是**滑动**的（每次被认出来就续期），否则玩一局超过 10 分钟
  再断线就重连不回来；
- 已经用它登进过游戏服的票据（`bind()`）给一个**长得多**的有效期，因为
  「服务端挂了、人去把它拉起来」这段时间没有任何续期机会；
- 票据表要**落盘**：服务端重启后客户端手上那张票必须还认得，
  不然玩家看到的是「在无法连接的地方尝试了连接。」然后卡死（§171）。

落盘的是**明文票据**，和 `accounts.json` 里的明文密码同一个信任域（D067），
但日志里仍然只打前 8 位（`short()`）。
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time


SERVER_DIR = os.path.dirname(os.path.abspath(__file__))

#: 默认落盘位置，和 `accounts.json` 同一个目录。
DEFAULT_PATH = os.path.join(SERVER_DIR, "data", "tickets.json")

#: **还没用过**的票据有效期（秒）。客户端拿到票据后要走完 `0x2d` / `0x0d`
#: 两次认证服往返，再连游戏服、发版本号、等应答，正常只要几秒。
#: 给 10 分钟是留给「登录框停在那儿没点开始」以外的意外（比如换图分辨率卡一下）。
DEFAULT_TTL_SECONDS = 600

#: **已经用它登进游戏服**的票据有效期（秒，默认 12 小时）。
#: 这条时限的用途只有一个：服务端崩了/被重启，玩家的客户端在那边不停重连。
#: 短了的话「等管理员把服务端拉起来」这段时间就把票据熬没了（§171 / D096）。
BOUND_TTL_SECONDS = 12 * 3600

#: 票据被作废的原因。目前只有一种，但游戏服要靠它挑给客户端看的错误码，
#: 所以做成常量而不是布尔（§132）。
REVOKED_SUPERSEDED = "superseded"


class TicketStore:
    """``票据 -> 用户名`` 的表，带过期清理和可选的落盘。线程安全。

    - `clock` 只用于**计时**（测试注入假时钟）；
    - `wall_clock` 只用于**落盘**（把剩余寿命换算成绝对时刻，服务端不在的
      那段时间照样算进去）。两个分开，测试才能既控制时间又验证落盘。
    """

    def __init__(self, ttl_seconds=DEFAULT_TTL_SECONDS, clock=time.monotonic,
                 path=None, bound_ttl_seconds=BOUND_TTL_SECONDS,
                 wall_clock=time.time, on_warning=None):
        self.ttl = float(ttl_seconds)
        self.bound_ttl = float(bound_ttl_seconds)
        self.path = os.path.abspath(path) if path else None
        self._clock = clock
        self._wall = wall_clock
        self._warn = on_warning or (lambda _msg: None)
        self._lock = threading.Lock()
        #: 票据 -> (用户名, 最近一次续期时刻, 是否已经登进过游戏服)
        self._tickets = {}
        #: 被顶掉的旧票据 -> (用户名, 作废原因, 作废时刻)。
        #: ★ 存在的唯一理由：被顶号的客户端会**自动重连**并重放这张旧票据
        #:   （§132）。查不到就只能回「票据无效」，客户端弹的是
        #:   「在无法连接的地方尝试了连接。」—— 驴唇不对马嘴。记一笔就能改回
        #:   「现有连接已断开。请重新尝试连接。」。同样按 TTL 过期。
        self._revoked = {}
        if self.path:
            self.load()

    # -- 内部 ---------------------------------------------------------------
    def _ttl_of(self, bound):
        return self.bound_ttl if bound else self.ttl

    def _purge_unlocked(self):
        now = self._clock()
        for ticket in [t for t, (_, at, bound) in self._tickets.items()
                       if now - at >= self._ttl_of(bound)]:
            del self._tickets[ticket]
        deadline = now - self.ttl
        for ticket in [t for t, (_, _, at) in self._revoked.items() if at < deadline]:
            del self._revoked[ticket]

    # -- 落盘 ---------------------------------------------------------------
    def _snapshot_unlocked(self):
        now, wall = self._clock(), self._wall()
        tickets = [
            {"ticket": t, "user": user, "bound": bool(bound),
             "expires_at": wall + self._ttl_of(bound) - (now - at)}
            for t, (user, at, bound) in self._tickets.items()
        ]
        revoked = [
            {"ticket": t, "user": user, "reason": reason,
             "expires_at": wall + self.ttl - (now - at)}
            for t, (user, reason, at) in self._revoked.items()
        ]
        return {"schema_version": 1, "tickets": tickets, "revoked": revoked}

    def _save_unlocked(self):
        """写盘。**永远不因为写不进去而中断登录** —— 最多丢掉重连能力。"""
        if not self.path:
            return
        data = self._snapshot_unlocked()
        tmp = f"{self.path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, self.path)
        except OSError as error:
            self._warn(f"票据表写不进 {self.path}：{error}"
                       f"（服务端重启后玩家要重新登录）")
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def load(self):
        """从盘上读回票据表。文件缺失/损坏都当成空表，只发一句警告。"""
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            tickets = data["tickets"]
            revoked = data.get("revoked", [])
        except (OSError, ValueError, KeyError, TypeError) as error:
            self._warn(f"票据表读不出来（{self.path}）：{error}；当空表处理")
            return
        now, wall = self._clock(), self._wall()
        kept = 0
        with self._lock:
            for row in tickets:
                try:
                    left = float(row["expires_at"]) - wall
                    bound = bool(row.get("bound"))
                    if left <= 0:
                        continue
                    self._tickets[str(row["ticket"])] = (
                        str(row["user"]), now - (self._ttl_of(bound) - left),
                        bound)
                    kept += 1
                except (KeyError, TypeError, ValueError):
                    continue
            for row in revoked:
                try:
                    left = float(row["expires_at"]) - wall
                    if left <= 0:
                        continue
                    self._revoked[str(row["ticket"])] = (
                        str(row["user"]), str(row.get("reason",
                                                      REVOKED_SUPERSEDED)),
                        now - (self.ttl - left))
                except (KeyError, TypeError, ValueError):
                    continue
        return kept

    # -- 对外 ---------------------------------------------------------------
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
            for old, (name, _, _) in list(self._tickets.items()):
                if name == username:
                    del self._tickets[old]
                    self._revoked[old] = (name, REVOKED_SUPERSEDED, now)
            self._tickets[ticket] = (username, now, False)
            self._save_unlocked()
        return ticket

    def resolve(self, ticket):
        """票据 -> 用户名；无效或过期返回 ``None``。

        ★ **不消费票据，而且每次都续期**：客户端和游戏服之间断线重连时会拿
        同一张票据再来一次（§132 / §171），一次性票据会让它永远进不来；
        固定从签发时刻算 TTL 的话，玩一局超过 TTL 再断线也进不来。
        """
        ticket = str(ticket or "").strip()
        if not ticket:
            return None
        with self._lock:
            self._purge_unlocked()
            entry = self._tickets.get(ticket)
            if not entry:
                return None
            username, _, bound = entry
            self._tickets[ticket] = (username, self._clock(), bound)
            return username

    def bind(self, ticket):
        """标记「这张票据真的登进游戏服了」，有效期换成 `bound_ttl`。

        分成两档的理由：**没用过**的票据是登录流程里的一次性凭证，短命才安全；
        **用过**的票据是那个玩家的重连凭证，服务端重启期间没人能给它续期，
        短命就等于「服务端一重启，正在玩的人全都卡在错误提示上」（§171 / D096）。
        """
        ticket = str(ticket or "").strip()
        if not ticket:
            return False
        with self._lock:
            entry = self._tickets.get(ticket)
            if not entry:
                return False
            username, _, was_bound = entry
            self._tickets[ticket] = (username, self._clock(), True)
            if not was_bound:
                self._save_unlocked()
            return True

    def is_bound(self, ticket):
        with self._lock:
            entry = self._tickets.get(str(ticket or "").strip())
            return bool(entry and entry[2])

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
                self._save_unlocked()

    def revoke_user(self, username):
        username = str(username)
        now = self._clock()
        with self._lock:
            hit = False
            for ticket, (name, _, _) in list(self._tickets.items()):
                if name == username:
                    del self._tickets[ticket]
                    self._revoked[ticket] = (name, REVOKED_SUPERSEDED, now)
                    hit = True
            if hit:
                self._save_unlocked()

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
