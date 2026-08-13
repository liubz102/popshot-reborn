#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""里程碑 J.3 的**战斗逻辑**测试 —— 房间广播、服务端仲裁、结算、对战胜负。

分工：

* `test_room.py` 测大厅和**开局链**（怎么把一局开起来）；
* 这里测「开起来之后」：死亡 / 重生 / 掉落 / 拾取 / 分数 / 换图的广播，
  一件东西只能被一个人捡到的仲裁，以及每座位一份的结算。

V0.1 的单人行为由 `test_gameserver.py` 钉着（那些用例一条都不许变红）——
这里只测**多了一个人之后**多出来的事。

依据：`.claude/FINDINGS.md` §161（每个包为什么广播出去是安全的、
客户端拿什么 id 找目标）、§112 / §116（结算界面那几格的来源）。
"""
import os
import struct
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gameserver                                              # noqa: E402
from gameserver import (                                       # noqa: E402
    DEATH_REPORT_FORMAT, GAME_RESULT_CLEARED, GAME_RESULT_DEFEATED,
    GAME_RESULT_TAIL_COUNT, ITEM_HANDLE_BASE,
    CONTROLLER_SLOT_COUNT,
    OP_BROADCAST_DEATH, OP_CHANGE_CONTROLLER_SLOT,
    OP_COUNT_GAME_READY, OP_CREATED_ITEM, OP_CREATE_ITEM,
    OP_END_GAME, OP_END_QUEST, OP_GET_ITEM, OP_LEAVE_SESSION, OP_LOADING_DONE,
    OP_MAP_CHANGE_READY, OP_MAP_LOADING_DONE, OP_MARK_QUEST_SUCCESS,
    OP_MOVE_INTO_SESSION, OP_PEER_DATA_UP, OP_PICKED_ITEM,
    OP_REP_CHANGE_TO_NEXT_MAP,
    OP_REP_GAME_RESULT, OP_REP_QUEST_SCORE, OP_REPORT_HP_ZERO,
    OP_REQ_CHANGE_TO_NEXT_MAP, OP_REQ_RESPAWN, OP_RESPAWN_CHARACTER,
    OP_UPDATE_QUEST_SCORE, RoomQuest, SESSION_STATUS_PLAYING,
    SESSION_STATUS_WAITING, StartGameHandshake,
    build_change_controller_slot, build_game, take_frame, w_i32, w_wstr,
)
from lobby import Lobby, MOVE_INTO_ALREADY_PLAYING                  # noqa: E402
import relayserver                                                  # noqa: E402


# ----------------------------------------------------------------------------
# 夹具
# ----------------------------------------------------------------------------
def frames(blob):
    out = []
    rest = blob
    while rest:
        kind, opcode, payload, consumed = take_frame(rest)
        if kind is None:
            raise AssertionError(f"拆帧失败，剩余 {len(rest)} 字节: {rest[:32]!r}")
        out.append((kind, opcode, payload))
        rest = rest[consumed:]
    return out


def opcodes(conn):
    """某条连接收到的全部 opcode，按顺序（跨批次拉平）。"""
    return [op for blob in conn.sent for _, op, _ in frames(blob)]


def bodies(conn, opcode):
    """某条连接收到的某个 opcode 的全部载荷，按顺序。"""
    return [payload for blob in conn.sent for _, op, payload in frames(blob)
            if op == opcode]


class Args:
    hold_lobby = False
    room_burst_delay = 0
    login_result = 0
    no_death_reply = False


ACCOUNTS = {
    "alice": {"display_name": "Alice", "level": 3, "experience": 200,
              "money": 10, "character": 0},
    "bob": {"display_name": "Bob", "level": 5, "experience": 400,
            "money": 20, "character": 1},
    "carol": {"display_name": "Carol", "level": 7, "experience": 600,
              "money": 30, "character": 2},
}


class FakeAccounts:
    """只实现结算真正会调的那两个方法。

    奖励要**真的加进去**：结算下发的是「已经入账的总经验」（D024），
    只有真加了才能测出「每个人各按自己的分数入账、互不串账」。
    """

    def __init__(self, conns):
        self.saved = {name: dict(data) for name, data in ACCOUNTS.items()}
        self.conns = conns
        self.cleared = []

    def add_quest_reward(self, username, experience=0, money=0):
        account = self.saved[username]
        account["experience"] = int(account["experience"]) + int(experience)
        account["money"] = int(account["money"]) + int(money)
        return dict(account)

    def set_quest_cleared(self, username, quest_id, difficulty):
        self.cleared.append((username, quest_id, difficulty))
        return dict(self.saved[username])


def make_conn(username, accounts=None):
    """一条只把发出去的字节攒进 `sent` 的假连接（同 `test_room.make_conn`）。"""
    conn = gameserver.Conn.__new__(gameserver.Conn)
    conn.addr = ("::ffff:127.0.0.1", 40000)
    conn.args = Args()
    conn.sent = []
    conn.logged = []
    conn.log = conn.logged.append
    conn.online = lambda _msg: None
    conn.vlog = lambda _msg: None
    conn.send = conn.sent.append
    conn.send_lock = threading.RLock()
    conn.send_queue = None
    conn.batch_delay_ms = 0
    conn.last_packet_at = 0.0
    conn.noisy_seen = set()
    conn.account_name = username
    conn.account = dict(ACCOUNTS[username])
    conn.accounts = accounts
    conn.room = None
    conn.my_seat = 0
    conn.settled = False
    conn.quest_score = 0
    conn.quest_success = False
    conn.solo_quest = RoomQuest()
    conn.items_created = 0
    conn.items_picked = 0
    conn.deaths_broadcast = 0
    conn.respawn_sent = 0
    conn.last_position = None
    conn.start_game = StartGameHandshake()
    conn.peer_relay_on = False
    conn.peer_data_dumped = False
    conn.peer_data_in = 0
    conn.peer_data_out = 0
    conn.peer_forward_ms = gameserver.relayserver.RttStats()
    conn.peer_gap_ms = gameserver.relayserver.RttStats()
    conn.peer_last_at = None
    conn.peer_report_at = 0.0
    return conn


def create_session_payload(title="来玩", session_type=2, arguments=None):
    """客户端方向的 `0x0201` 载荷：三个字符串 + int32 + 描述符。

    ★ 参数个数由房间类型决定（`DESCRIPTOR_SENT_ARGUMENT_COUNTS`）——
    闯关（2）是 `(关卡 id, 难度)` 两个，对战（1）是三个。给错个数的话
    描述符解析失败，房间根本登记不进大厅。
    """
    if arguments is None:
        arguments = (3, 1) if session_type == 2 else (0, 0, 0)
    return (w_wstr(title) + w_wstr("") + w_wstr("")
            + w_i32(0)
            + w_i32(session_type)
            + b"".join(w_i32(v) for v in arguments))


def move_into_payload(room_id, password="", flag=0):
    return w_i32(room_id) + w_wstr(password) + w_i32(flag)


def hp_zero_payload(handle=0x000186A1, seat=0, arg=0xFF, deaths=0,
                    x=100.0, y=200.0):
    """客户端方向的 `0x0408`（18 字节，紧凑，死亡次数在线偏移 6）。"""
    return struct.pack(DEATH_REPORT_FORMAT, handle, seat & 0xFF, arg, deaths,
                       x, y)


def respawn_payload(character_id=0, x=100, y=200, spawn_index=1):
    """客户端方向的 `0x0413`（16 字节，和服务端方向的 `0x0419` 同一份结构）。"""
    return (w_i32(character_id) + w_i32(x) + w_i32(y) + w_i32(spawn_index))


def create_item_payload(item_id=10101, x=100.0, y=200.0):
    """客户端方向的 `0x0406 gcpCreateItem`（32 字节）。"""
    return struct.pack(gameserver.CREATE_ITEM_FORMAT,
                       item_id, x, y, 0.0, 0.0, 3, -1, -1)


def get_item_payload(seat_id=0, handle=ITEM_HANDLE_BASE):
    """客户端方向的 `0x0407 gcpGetItem`（两个 int32）。"""
    return w_i32(seat_id) + w_i32(handle)


class BattleRoom(unittest.TestCase):
    """alice（房主，座位 0）+ bob（座位 1），已经一起进了关卡。

    每个用例一张干净的大厅表 —— `LOBBY` 是模块级单例（同 `test_room`）。
    """

    session_type = 2        # 2 = 闯关；对战的用例自己覆盖

    def setUp(self):
        self._saved_lobby = gameserver.LOBBY
        gameserver.LOBBY = Lobby()
        self.lobby = gameserver.LOBBY
        self._saved_relay = gameserver.PEER_RELAY
        gameserver.PEER_RELAY = relayserver.RelayServer(
            members_of=gameserver._relay_room_members,
            fallback=gameserver._relay_fallback)
        self._saved_relay_enabled = gameserver.TCP_RELAY_ENABLED
        gameserver.TCP_RELAY_ENABLED = False

        self.accounts = FakeAccounts(None)
        self.alice = make_conn("alice", self.accounts)
        self.bob = make_conn("bob", self.accounts)
        gameserver.Conn.on_game_packet(
            self.alice, 0x0201,
            create_session_payload(session_type=self.session_type))
        self.room = self.lobby.room_of(self.alice)
        gameserver.Conn.on_game_packet(self.bob, OP_MOVE_INTO_SESSION,
                                       move_into_payload(self.room.room_id))
        self.start_battle()
        self.clear()

    def tearDown(self):
        gameserver.LOBBY = self._saved_lobby
        gameserver.PEER_RELAY = self._saved_relay
        gameserver.TCP_RELAY_ENABLED = self._saved_relay_enabled

    def start_battle(self):
        """走完真正的开局链，让两个人都进 stage 7。"""
        gameserver.Conn.on_game_packet(self.alice, OP_COUNT_GAME_READY, b"")
        gameserver.Conn.on_game_packet(self.alice, OP_COUNT_GAME_READY, b"")
        gameserver.Conn.on_game_packet(self.alice, OP_LOADING_DONE, b"")
        gameserver.Conn.on_game_packet(self.bob, OP_LOADING_DONE, b"")

    def clear(self):
        self.alice.sent.clear()
        self.bob.sent.clear()

    @property
    def quest(self):
        return self.room.quest


# ----------------------------------------------------------------------------
# 死亡 / 重生
# ----------------------------------------------------------------------------
class DeathBroadcastTests(BattleRoom):
    """`0x0408 -> 0x0406`。§161：读侧按 `World::Find(句柄)` 找角色，
    而玩家角色的句柄 = 座位×100000+100001（`0x405f02`），跨机器一致。"""

    def test_a_death_reaches_everyone_in_the_room(self):
        gameserver.Conn.on_game_packet(self.alice, OP_REPORT_HP_ZERO,
                                       hp_zero_payload(seat=0, deaths=0))
        self.assertEqual([OP_BROADCAST_DEATH], opcodes(self.alice))
        self.assertEqual([OP_BROADCAST_DEATH], opcodes(self.bob))

    def test_the_broadcast_is_byte_identical_for_everyone(self):
        gameserver.Conn.on_game_packet(self.alice, OP_REPORT_HP_ZERO,
                                       hp_zero_payload(seat=0, deaths=1))
        self.assertEqual(bodies(self.alice, OP_BROADCAST_DEATH),
                         bodies(self.bob, OP_BROADCAST_DEATH))

    def test_the_same_death_reported_twice_is_only_broadcast_once(self):
        # 两台机器各自模拟同一只怪，同一次死亡会被报两遍。广播两遍等于
        # 战绩表（0x48c942）多记一次死亡。
        payload = hp_zero_payload(handle=0x0010C8FB, seat=0xFF, deaths=0)
        gameserver.Conn.on_game_packet(self.alice, OP_REPORT_HP_ZERO, payload)
        self.clear()
        gameserver.Conn.on_game_packet(self.bob, OP_REPORT_HP_ZERO, payload)
        self.assertEqual([], opcodes(self.alice))
        self.assertEqual([], opcodes(self.bob))

    def test_dying_again_is_a_different_event(self):
        # ★ 去重的键里必须带死亡次数：同一个角色会死很多次，只按句柄去重
        #   第二次死就被吃掉了（那一格是 [char+0x600]，只由我们广播的
        #   0x0406 写，所以重复上报的两发一定同值、真的第二次死一定更大）。
        for reported in (0, 1, 2):
            self.clear()
            gameserver.Conn.on_game_packet(
                self.alice, OP_REPORT_HP_ZERO, hp_zero_payload(deaths=reported))
            self.assertEqual([OP_BROADCAST_DEATH], opcodes(self.bob),
                             f"第 {reported + 1} 次死亡没广播")

    def test_a_map_change_forgets_the_dedup_table(self):
        # 换图会把六个座位的角色和场景物件全部卸掉重建（0x47900a），
        # 旧句柄作废 —— 不清的话新图里同号的东西会被当成「已经报过了」。
        payload = hp_zero_payload(handle=0x0010C8FB, seat=0xFF, deaths=0)
        gameserver.Conn.on_game_packet(self.alice, OP_REPORT_HP_ZERO, payload)
        self.quest.begin_map_change("Quest03_2")
        self.clear()
        gameserver.Conn.on_game_packet(self.alice, OP_REPORT_HP_ZERO, payload)
        self.assertEqual([OP_BROADCAST_DEATH], opcodes(self.bob))

    def test_the_death_count_is_still_the_reported_value_plus_one(self):
        # V0.1 §109 的契约不许变：HUD 心形 = 最大生命 - 这一格。
        gameserver.Conn.on_game_packet(self.alice, OP_REPORT_HP_ZERO,
                                       hp_zero_payload(seat=1, deaths=1))
        body = bodies(self.bob, OP_BROADCAST_DEATH)[0]
        self.assertEqual(2, struct.unpack_from("<i", body, 6)[0])

    def test_a_respawn_reaches_everyone(self):
        # 不广播的话别人屏幕上你就一直躺着（读侧 0x4931c2 按座位取角色）。
        gameserver.Conn.on_game_packet(self.bob, OP_REQ_RESPAWN,
                                       respawn_payload(character_id=1))
        self.assertEqual([OP_RESPAWN_CHARACTER], opcodes(self.alice))
        self.assertEqual([OP_RESPAWN_CHARACTER], opcodes(self.bob))
        self.assertEqual(bodies(self.alice, OP_RESPAWN_CHARACTER),
                         bodies(self.bob, OP_RESPAWN_CHARACTER))


# ----------------------------------------------------------------------------
# 掉落物 / 拾取
# ----------------------------------------------------------------------------
class ItemTests(BattleRoom):
    """`0x0406 -> 0x0404` 和 `0x0407 -> 0x0405`。"""

    def test_a_drop_reaches_everyone(self):
        gameserver.Conn.on_game_packet(self.alice, OP_CREATE_ITEM,
                                       create_item_payload())
        self.assertEqual([OP_CREATED_ITEM], opcodes(self.alice))
        self.assertEqual([OP_CREATED_ITEM], opcodes(self.bob))
        self.assertEqual(bodies(self.alice, OP_CREATED_ITEM),
                         bodies(self.bob, OP_CREATED_ITEM))

    def test_handles_are_allocated_per_room_not_per_connection(self):
        # ★ 句柄进客户端 World 的 map 当 key（0x473e7c）。每条连接各自从
        #   ITEM_HANDLE_BASE 数的话，alice 的第 1 件和 bob 的第 1 件同号，
        #   后到的会覆盖先到的。
        gameserver.Conn.on_game_packet(self.alice, OP_CREATE_ITEM,
                                       create_item_payload())
        gameserver.Conn.on_game_packet(self.bob, OP_CREATE_ITEM,
                                       create_item_payload())
        handles = [struct.unpack_from("<I", body, 0)[0]
                   for body in bodies(self.alice, OP_CREATED_ITEM)]
        self.assertEqual([ITEM_HANDLE_BASE, ITEM_HANDLE_BASE + 1], handles)

    def test_a_pickup_reaches_everyone(self):
        gameserver.Conn.on_game_packet(self.bob, OP_GET_ITEM,
                                       get_item_payload(seat_id=1))
        self.assertEqual([OP_PICKED_ITEM], opcodes(self.alice))
        self.assertEqual([OP_PICKED_ITEM], opcodes(self.bob))

    def test_one_item_can_only_be_picked_up_once(self):
        # ★ 这一条是服务端仲裁的全部意义：两个人几乎同时踩到同一件东西，
        #   两台机器都会判「我碰到了」并各发一发 0x0407。
        payload = get_item_payload(seat_id=0, handle=ITEM_HANDLE_BASE + 7)
        gameserver.Conn.on_game_packet(self.alice, OP_GET_ITEM, payload)
        self.clear()
        gameserver.Conn.on_game_packet(
            self.bob, OP_GET_ITEM,
            get_item_payload(seat_id=1, handle=ITEM_HANDLE_BASE + 7))
        self.assertEqual([], opcodes(self.alice))
        self.assertEqual([], opcodes(self.bob), "晚到的那一发绝不能回包")

    def test_the_winner_is_whoever_asked_first(self):
        gameserver.Conn.on_game_packet(
            self.bob, OP_GET_ITEM,
            get_item_payload(seat_id=1, handle=ITEM_HANDLE_BASE + 7))
        body = bodies(self.alice, OP_PICKED_ITEM)[0]
        self.assertEqual(1, struct.unpack_from("<i", body, 0)[0])
        self.assertEqual({ITEM_HANDLE_BASE + 7: 1}, self.quest.items_taken)

    def test_different_items_are_all_granted(self):
        for i in range(3):
            gameserver.Conn.on_game_packet(
                self.alice, OP_GET_ITEM,
                get_item_payload(handle=ITEM_HANDLE_BASE + i))
        self.assertEqual(3, opcodes(self.bob).count(OP_PICKED_ITEM))


# ----------------------------------------------------------------------------
# 分数
# ----------------------------------------------------------------------------
class ScoreTests(BattleRoom):
    """`0x0410 -> 0x0415`。处理器 `0x4a3efe` 写
    `[GameContextQuest + 座位*4 + 0x3b8]`，按座位索引，所以广播是对的。"""

    def test_a_score_update_reaches_everyone_with_the_right_seat(self):
        self.bob.my_seat = 1
        gameserver.Conn.on_game_packet(self.bob, OP_UPDATE_QUEST_SCORE,
                                       w_i32(64))
        for conn in (self.alice, self.bob):
            body = bodies(conn, OP_REP_QUEST_SCORE)[0]
            self.assertEqual((1, 64), struct.unpack_from("<ii", body, 0))


# ----------------------------------------------------------------------------
# 换图
# ----------------------------------------------------------------------------
class MapChangeTests(BattleRoom):
    """`0x0411 -> 0x0417` 广播、`0x0412 -> 0x0418` **等所有人**。"""

    def request(self, conn, name="Quest03_2"):
        gameserver.Conn.on_game_packet(conn, OP_REQ_CHANGE_TO_NEXT_MAP,
                                       w_wstr(name))

    def done(self, conn):
        gameserver.Conn.on_game_packet(conn, OP_MAP_LOADING_DONE, b"")

    def test_the_whole_room_changes_map_together(self):
        self.request(self.alice)
        self.assertEqual([OP_REP_CHANGE_TO_NEXT_MAP], opcodes(self.alice))
        self.assertEqual([OP_REP_CHANGE_TO_NEXT_MAP], opcodes(self.bob))

    def test_a_second_request_for_the_same_map_is_not_rebroadcast(self):
        # 两个人同时走到地图边缘会各发一发。再广播一次的话，先收到的人
        # 会被要求再卸一次场景。
        self.request(self.alice)
        self.clear()
        self.request(self.bob)
        self.assertEqual([], opcodes(self.alice))
        self.assertEqual([], opcodes(self.bob))

    def test_nobody_is_released_until_everyone_has_loaded(self):
        self.request(self.alice)
        self.clear()
        self.done(self.alice)
        self.assertEqual([], opcodes(self.alice), "先加载完的不能提前放行")
        self.assertEqual([], opcodes(self.bob))
        self.done(self.bob)
        self.assertEqual([OP_MAP_CHANGE_READY], opcodes(self.alice))
        self.assertEqual([OP_MAP_CHANGE_READY], opcodes(self.bob))

    def test_leaving_mid_load_releases_the_rest(self):
        # ★ 走的人可能正是没加载完的那一个 —— 不重新算一次的话，
        #   剩下的人永远卡在换图的加载画面里。
        self.request(self.alice)
        self.done(self.alice)
        self.clear()
        gameserver.Conn.on_game_packet(self.bob, OP_LEAVE_SESSION, b"")
        self.assertIn(OP_MAP_CHANGE_READY, opcodes(self.alice))

    def test_a_map_change_clears_the_pickup_table(self):
        gameserver.Conn.on_game_packet(self.alice, OP_GET_ITEM,
                                       get_item_payload())
        self.request(self.alice)
        self.assertEqual({}, self.quest.items_taken)


# ----------------------------------------------------------------------------
# 结算
# ----------------------------------------------------------------------------
def result_seat(body):
    return struct.unpack_from("<i", body, 0)[0]


def result_tail(body):
    """`0x0309` 尾部数组（座位 + 12 个业务值之后是 count + count 个 int32）。"""
    offset = 4 + 12 * 4
    count = struct.unpack_from("<i", body, offset)[0]
    return list(struct.unpack_from(f"<{count}i", body, offset + 4))


def end_game_seat(body):
    return struct.unpack_from("<i", body, 0)[0]


def end_game_score(body):
    """`0x0411` 里结算界面「分数」那一格（座位 + success 之后的 12 个业务值）。"""
    values = struct.unpack_from(
        f"<{gameserver.END_GAME_VALUE_COUNT}i", body, 8)
    return sum(values[i] for i in gameserver.END_GAME_SCORE_PARTS)


class QuestSettlementTests(BattleRoom):
    """闯关（合作）的结算：`0x0309` 和 `0x0411` 都是每座位一份（自己那份在最前）。"""

    def score(self, conn, seat, value):
        conn.my_seat = seat
        gameserver.Conn.on_game_packet(conn, OP_UPDATE_QUEST_SCORE,
                                       w_i32(value))

    def clear_quest(self):
        gameserver.Conn.on_game_packet(self.alice, OP_MARK_QUEST_SUCCESS,
                                       w_i32(1))

    def end(self, conn=None):
        gameserver.Conn.on_game_packet(conn or self.alice, OP_END_QUEST, b"")

    def test_everyone_gets_one_result_per_occupied_seat(self):
        self.score(self.alice, 0, 40)
        self.score(self.bob, 1, 25)
        self.clear()
        self.end()
        for conn in (self.alice, self.bob):
            seats = [result_seat(b) for b in bodies(conn, OP_REP_GAME_RESULT)]
            self.assertEqual([0, 1], seats)

    def test_everyone_gets_one_end_game_per_occupied_seat(self):
        # §178 / D101：那 13 个 dword（结算界面「分数」那一行）只有 0x0411 会写，
        # 而且是按包里的座位号索引写的。不每座位发一份，队友那一行就是 0，
        # 于是两个人看到的结算界面对不上。
        self.score(self.alice, 0, 40)
        self.score(self.bob, 1, 25)
        self.clear()
        self.end()
        for conn in (self.alice, self.bob):
            seats = [end_game_seat(b) for b in bodies(conn, OP_END_GAME)]
            self.assertEqual({0, 1}, set(seats))
            self.assertEqual(2, len(seats))

    def test_the_end_game_for_my_own_seat_comes_first(self):
        # 弹结算界面的是**第一发** 0x0411（0x4913fc 有重入保护），而右上角
        # 数据栏那四个全局只有自己那一份会写 —— 自己排第一，界面弹出来的
        # 那一刻数据栏就是新值，时序和 V0.1 单人版一致。
        self.score(self.alice, 0, 40)
        self.score(self.bob, 1, 25)
        self.clear()
        self.end()
        self.assertEqual(0, end_game_seat(bodies(self.alice, OP_END_GAME)[0]))
        self.assertEqual(1, end_game_seat(bodies(self.bob, OP_END_GAME)[0]))

    def test_each_end_game_carries_that_seats_own_numbers(self):
        # 队友那一行要显示的是**他自己**的分数，不是收包这个人的分数。
        self.score(self.alice, 0, 40)
        self.score(self.bob, 1, 25)
        self.clear()
        self.end()
        for conn in (self.alice, self.bob):
            scores = {end_game_seat(b): end_game_score(b)
                      for b in bodies(conn, OP_END_GAME)}
            self.assertEqual({0: 40, 1: 25}, scores)

    def test_the_result_packets_still_precede_the_end_game(self):
        # §99：0x0309 要在 GameContext 还活着时发，0x0411 才结束关卡。
        self.end()
        for conn in (self.alice, self.bob):
            ops = [op for op in opcodes(conn)
                   if op in (OP_REP_GAME_RESULT, OP_END_GAME)]
            first_end = ops.index(OP_END_GAME)
            self.assertNotIn(OP_REP_GAME_RESULT, ops[first_end:])

    def test_the_room_only_settles_once(self):
        # 房里每个人的客户端都会发一发 0x040f。不挡的话一局入账好几次。
        self.score(self.alice, 0, 40)
        self.clear()
        self.end(self.alice)
        self.clear()
        self.end(self.bob)
        self.assertEqual([], opcodes(self.alice))
        self.assertEqual([], opcodes(self.bob))
        self.assertEqual(240, self.accounts.saved["alice"]["experience"])

    def test_each_player_is_paid_their_own_score(self):
        self.score(self.alice, 0, 40)
        self.score(self.bob, 1, 25)
        self.end()
        self.assertEqual(200 + 40, self.accounts.saved["alice"]["experience"])
        self.assertEqual(400 + 25, self.accounts.saved["bob"]["experience"])
        self.assertEqual(10 + 40, self.accounts.saved["alice"]["money"])
        self.assertEqual(20 + 25, self.accounts.saved["bob"]["money"])

    def test_clearing_the_quest_marks_everyone_as_cleared(self):
        # 合作：关底是大家一起打的，脚本只在某一台机器上喊到也算全房间通关。
        self.score(self.alice, 0, 40)
        self.score(self.bob, 1, 25)
        self.clear_quest()
        self.clear()
        self.end()
        expected = [GAME_RESULT_CLEARED, GAME_RESULT_CLEARED] + [0] * 4
        for conn in (self.alice, self.bob):
            for body in bodies(conn, OP_REP_GAME_RESULT):
                self.assertEqual(expected, result_tail(body))

    def test_a_failed_quest_never_writes_minus_one(self):
        # 0 和 -1 是两个档：`0x55223f` 的 setge 用 >= 0 选胜利 BGM。
        # V0.1 单机没通关时发的就是 0，改成 -1 会让失败开始放失败曲。
        self.end()
        body = bodies(self.alice, OP_REP_GAME_RESULT)[0]
        self.assertEqual([0] * GAME_RESULT_TAIL_COUNT, result_tail(body))

    def test_everyone_who_cleared_unlocks_the_next_difficulty(self):
        # ★ 客人手上没有 `self.room`（那是房主解析 0x0201 得到的），
        #   `current_quest()` 要能从大厅那一份读出关卡 id / 难度。
        self.clear_quest()
        self.end()
        self.assertEqual({("alice", 3, 1), ("bob", 3, 1)},
                         set(self.accounts.cleared))


class PvpSettlementTests(BattleRoom):
    """对战（房间类型 != 2）：按本局分数判胜负。"""

    session_type = 1

    def score(self, conn, seat, value):
        conn.my_seat = seat
        gameserver.Conn.on_game_packet(conn, OP_UPDATE_QUEST_SCORE,
                                       w_i32(value))

    def test_the_higher_score_wins_and_the_other_loses(self):
        self.score(self.alice, 0, 40)
        self.score(self.bob, 1, 25)
        self.clear()
        gameserver.Conn.on_game_packet(self.alice, OP_END_QUEST, b"")
        expected = [GAME_RESULT_CLEARED, GAME_RESULT_DEFEATED] + [0] * 4
        for conn in (self.alice, self.bob):
            for body in bodies(conn, OP_REP_GAME_RESULT):
                self.assertEqual(expected, result_tail(body))

    def test_a_draw_makes_everyone_a_winner(self):
        self.score(self.alice, 0, 30)
        self.score(self.bob, 1, 30)
        self.clear()
        gameserver.Conn.on_game_packet(self.alice, OP_END_QUEST, b"")
        expected = [GAME_RESULT_CLEARED, GAME_RESULT_CLEARED] + [0] * 4
        body = bodies(self.alice, OP_REP_GAME_RESULT)[0]
        self.assertEqual(expected, result_tail(body))

    def test_a_scoreless_round_judges_nobody(self):
        # 没打就散了。判谁输都是瞎判，两边都放失败曲更难看。
        gameserver.Conn.on_game_packet(self.alice, OP_END_QUEST, b"")
        body = bodies(self.alice, OP_REP_GAME_RESULT)[0]
        self.assertEqual([0] * GAME_RESULT_TAIL_COUNT, result_tail(body))

    def test_a_pvp_round_never_records_a_quest_clear(self):
        self.score(self.alice, 0, 40)
        gameserver.Conn.on_game_packet(self.alice, OP_END_QUEST, b"")
        self.assertEqual([], self.accounts.cleared)


class PvpFinishTests(BattleRoom):
    """★ 对战必须由**服务端**判胜负并结算（§167）。

    用户 2026-08-12 实机报的：「对战模式分出胜负后无法退出返回房间，
    胜利的人还可以动，死的人无法复活，倒计时结束也不退出」。
    根因：客户端自带的结束链 `0x4a3cf7` 第一行就是 `cmp [this+0x3b0], 2`，
    而那个状态只有剧本关才会进 —— 对战地图里它永远是 1，
    所以整局**一发 `0x040f` 都不会发**（实机日志逐包对过）。
    """

    session_type = 1

    def kill(self, killer_seat, victim_seat, deaths=0):
        """让 `killer_seat` 打死 `victim_seat` 一次。

        `0x0408` 里的「凶手」字段就是开火者的座位号（`[char+0x158]`，
        由 `0x4fedee` 写），服务端的对战计分靠它。
        """
        gameserver.Conn.on_game_packet(
            self.alice, OP_REPORT_HP_ZERO,
            hp_zero_payload(handle=victim_seat * 100000 + 100001,
                            seat=victim_seat, arg=killer_seat, deaths=deaths))

    def test_kills_are_credited_to_the_shooter(self):
        self.kill(0, 1, deaths=0)
        self.kill(0, 1, deaths=1)
        self.assertEqual(2, self.quest.kills[0])
        self.assertEqual(0, self.quest.kills[1])

    def test_a_suicide_scores_nothing(self):
        self.kill(1, 1)
        self.assertEqual([0] * 6, self.quest.kills)

    def test_a_monster_kill_scores_nothing(self):
        # 怪物 / 环境的凶手字段是 0xff（线上就是这个字节）。
        self.kill(0xFF, 1)
        self.assertEqual([0] * 6, self.quest.kills)

    def test_reaching_the_score_limit_ends_the_round(self):
        # 2 人个人战的上限是 4（`0x55be71` 的表）。
        for i in range(4):
            self.kill(0, 1, deaths=i)
        self.assertTrue(self.quest.settled)
        self.assertIn("达到上限", self.quest.pvp_reason)
        for conn in (self.alice, self.bob):
            self.assertIn(OP_REP_GAME_RESULT, opcodes(conn))
            self.assertIn(OP_END_GAME, opcodes(conn))

    def test_the_round_does_not_end_one_kill_early(self):
        for i in range(3):
            self.kill(0, 1, deaths=i)
        self.assertFalse(self.quest.settled)
        self.assertIsNone(self.quest.pvp_reason)

    def test_the_winner_is_the_one_with_the_kills(self):
        for i in range(4):
            self.kill(0, 1, deaths=i)
        expected = [GAME_RESULT_CLEARED, GAME_RESULT_DEFEATED] + [0] * 4
        body = bodies(self.alice, OP_REP_GAME_RESULT)[0]
        self.assertEqual(expected, result_tail(body))

    def test_the_time_limit_ends_the_round(self):
        self.quest.started_at -= gameserver.PVP_TIME_LIMIT_MS / 1000.0 + 1
        gameserver.Conn.on_game_packet(self.alice, OP_PEER_DATA_UP,
                                       b"\xff\x00\xff\x00" + b"\x00" * 8)
        self.assertTrue(self.quest.settled)
        self.assertIn("时间到", self.quest.pvp_reason)

    def test_the_last_one_standing_ends_the_round(self):
        gameserver.Conn.on_game_packet(self.bob, OP_LEAVE_SESSION, b"")
        self.assertTrue(self.quest.settled)
        self.assertEqual("只剩一边了", self.quest.pvp_reason)

    def test_a_quest_round_is_never_ended_by_the_server(self):
        # 闯关那一路客户端会自己发 0x040f，服务端绝不能抢在前面结算。
        room = gameserver.Conn.lobby_room(self.alice)
        room.session_type = 2
        self.quest.started_at -= gameserver.PVP_TIME_LIMIT_MS / 1000.0 + 1
        self.assertFalse(gameserver.Conn.check_pvp_finished(self.alice))
        self.assertFalse(self.quest.settled)

    def test_the_round_is_only_settled_once(self):
        for i in range(6):
            self.kill(0, 1, deaths=i)
        # 每座位一份（§178），所以两个人的房间正好两发；结算了两次就是四发。
        self.assertEqual([0, 1],
                         sorted(end_game_seat(b)
                                for b in bodies(self.alice, OP_END_GAME)))

    def test_score_limits_match_the_client(self):
        # `0x55be71`：个人战看人数，组队战看「人数 // 2」；表外一律 5。
        self.assertEqual(4, gameserver.pvp_score_limit(2, False))
        self.assertEqual(6, gameserver.pvp_score_limit(3, False))
        self.assertEqual(8, gameserver.pvp_score_limit(4, False))
        self.assertEqual(9, gameserver.pvp_score_limit(5, False))
        self.assertEqual(10, gameserver.pvp_score_limit(6, False))
        self.assertEqual(4, gameserver.pvp_score_limit(2, True))
        self.assertEqual(6, gameserver.pvp_score_limit(4, True))
        self.assertEqual(8, gameserver.pvp_score_limit(6, True))
        self.assertEqual(5, gameserver.pvp_score_limit(1, False))


# ----------------------------------------------------------------------------
# 房间生命周期
# ----------------------------------------------------------------------------
class RoomLifecycleTests(BattleRoom):
    """开局挡人、结算完回房间复位、第二局能再开起来。"""

    def test_the_room_is_marked_playing_once_everyone_is_in(self):
        self.assertEqual(SESSION_STATUS_PLAYING, self.room.status)

    def test_nobody_can_join_a_room_that_is_playing(self):
        # 关卡是开局那一刻按座位表加载的，中途多一个人两边就对不上了。
        carol = make_conn("carol", self.accounts)
        result, _, _ = self.lobby.join(carol, self.room.room_id)
        self.assertEqual(MOVE_INTO_ALREADY_PLAYING, result)

    def test_returning_from_the_result_screen_reopens_the_room(self):
        gameserver.Conn.on_game_packet(self.alice, OP_END_QUEST, b"")
        gameserver.Conn.leave_game_result(self.alice)
        self.assertEqual(SESSION_STATUS_WAITING, self.room.status)
        self.assertIsNone(self.room.quest)
        self.assertEqual(StartGameHandshake.WAIT_START, self.room.battle.state)

    def test_a_second_round_can_be_started(self):
        # ★ 不复位 `room.battle` 的话它停在 IN_GAME，房主再按 F5 发来的
        #   0x0402 会被 StartGameHandshake 当成「已经在游戏里了」直接丢掉。
        gameserver.Conn.on_game_packet(self.alice, OP_END_QUEST, b"")
        gameserver.Conn.leave_game_result(self.alice)
        gameserver.Conn.leave_game_result(self.bob)
        self.clear()
        gameserver.Conn.on_game_packet(self.alice, OP_COUNT_GAME_READY, b"")
        self.assertEqual([gameserver.OP_TRIGGER_COUNT_GAME], opcodes(self.bob))

    def test_the_second_round_starts_with_a_fresh_quest_state(self):
        gameserver.Conn.on_game_packet(self.alice, OP_CREATE_ITEM,
                                       create_item_payload())
        gameserver.Conn.on_game_packet(self.alice, OP_END_QUEST, b"")
        gameserver.Conn.leave_game_result(self.alice)
        self.start_battle()
        self.clear()
        gameserver.Conn.on_game_packet(self.alice, OP_CREATE_ITEM,
                                       create_item_payload())
        handle = struct.unpack_from(
            "<I", bodies(self.alice, OP_CREATED_ITEM)[0], 0)[0]
        self.assertEqual(ITEM_HANDLE_BASE, handle)


# ----------------------------------------------------------------------------
# 控制权交接（有人中途退出，§180 / D103）
# ----------------------------------------------------------------------------
def controller_handover(body):
    """`0x0414` 的载荷 -> `(走的人的座位, 接管者的座位)`。"""
    return struct.unpack("<ii", body)


class ControllerHandoverTests(BattleRoom):
    """房主中途退出之后，怪 / 刷怪点的模拟权必须交给还在的人。

    不交接的话每台客户端都算出「类别 20 不归我」（表里指着一个空座位），
    于是没人刷怪、关卡的闸门再也不开 —— 用户报的
    「走到屏幕最右边被屏幕挡住」（§180）。
    """

    def test_the_table_starts_out_round_robin_like_the_client(self):
        # 客户端 GameContext::StartGame：[ctx+0x294+i*4] = 在座座位[i % n]
        self.assertEqual([0, 1, 0, 1, 0, 1], self.quest.controllers)

    def test_the_host_leaving_hands_the_monsters_to_whoever_is_left(self):
        gameserver.Conn.on_game_packet(self.alice, OP_LEAVE_SESSION, b"")
        bodies_ = bodies(self.bob, OP_CHANGE_CONTROLLER_SLOT)
        self.assertEqual(1, len(bodies_))
        self.assertEqual((0, 1), controller_handover(bodies_[0]))
        self.assertEqual([1] * CONTROLLER_SLOT_COUNT, self.quest.controllers)

    def test_a_dropped_connection_hands_over_too(self):
        # 「强制退出」走的是断线那条路（连接直接断，没有 0x0203）。
        gameserver.Conn.leave_room(self.alice, "Alice 断线了。")
        self.assertEqual([(0, 1)],
                         [controller_handover(b) for b in
                          bodies(self.bob, OP_CHANGE_CONTROLLER_SLOT)])

    def test_a_guest_leaving_also_hands_over_its_share(self):
        # 两人房里客人也扛着三格（21/23/25），走了同样要交接。
        gameserver.Conn.on_game_packet(self.bob, OP_LEAVE_SESSION, b"")
        self.assertEqual([(1, 0)],
                         [controller_handover(b) for b in
                          bodies(self.alice, OP_CHANGE_CONTROLLER_SLOT)])
        self.assertEqual([0] * CONTROLLER_SLOT_COUNT, self.quest.controllers)

    def test_the_kicked_player_hands_over_its_share(self):
        gameserver.Conn.on_game_packet(self.alice, gameserver.OP_KICK_OUT,
                                       w_i32(1) + w_i32(0))
        self.assertEqual([(1, 0)],
                         [controller_handover(b) for b in
                          bodies(self.alice, OP_CHANGE_CONTROLLER_SLOT)])

    def test_the_one_who_left_is_not_sent_anything(self):
        gameserver.Conn.on_game_packet(self.alice, OP_LEAVE_SESSION, b"")
        self.assertNotIn(OP_CHANGE_CONTROLLER_SLOT, opcodes(self.alice))

    def test_the_handover_does_not_fire_after_the_round_is_over(self):
        # 结算看完回到房间 -> 房间标回「待机中」、quest 丢掉。等待房里没有怪，
        # 这时再有人走就不该发这个包（D103）。
        for conn in (self.alice, self.bob):
            gameserver.Conn.leave_game_result(conn)
        self.clear()
        gameserver.Conn.on_game_packet(self.alice, OP_LEAVE_SESSION, b"")
        self.assertNotIn(OP_CHANGE_CONTROLLER_SLOT, opcodes(self.bob))


class ControllerHandoverInRoomTests(BattleRoom):
    """还没开局（房间「待机中」）时离开 —— 一个 `0x0414` 都不该发。"""

    def start_battle(self):
        pass

    def test_leaving_a_waiting_room_sends_no_handover(self):
        self.assertEqual(SESSION_STATUS_WAITING, self.room.status)
        gameserver.Conn.on_game_packet(self.alice, OP_LEAVE_SESSION, b"")
        self.assertNotIn(OP_CHANGE_CONTROLLER_SLOT, opcodes(self.bob))


class ControllerHandoverWhileLoadingTests(BattleRoom):
    """★ 关卡**正在加载**（`0x0400` 发了、还没收齐 `0x0403`）时有人走。

    客户端那张控制者表是它自己建的，我们不知道它到底建在「stage 6 加载完」
    还是「进 stage 7」那一刻 —— 万一建得比那个人走掉更早，表里就留着一个
    已经空了的座位，那一局的怪从第一秒起就没人模拟。所以这段时间走掉的人
    要记下来，等真进了关卡立刻补一发（§180 / D103）。
    """

    def start_battle(self):
        self.carol = make_conn("carol", self.accounts)
        gameserver.Conn.on_game_packet(self.carol, OP_MOVE_INTO_SESSION,
                                       move_into_payload(self.room.room_id))
        # 只走到「大家开始加载关卡」这一步（房主两发 0x0402 -> 0x0401 + 0x0400）。
        gameserver.Conn.on_game_packet(self.alice, OP_COUNT_GAME_READY, b"")
        gameserver.Conn.on_game_packet(self.alice, OP_COUNT_GAME_READY, b"")

    def clear(self):
        super().clear()
        self.carol.sent.clear()

    def test_the_departure_is_remembered_and_replayed_after_loading(self):
        self.assertEqual(StartGameHandshake.PREPARING, self.room.battle.state)
        gameserver.Conn.on_game_packet(self.carol, OP_LEAVE_SESSION, b"")
        # 加载途中不发（客户端可能还没建表），只记下来
        self.assertNotIn(OP_CHANGE_CONTROLLER_SLOT, opcodes(self.bob))
        self.assertEqual([2], self.room.battle.left_while_loading)
        self.clear()
        # 剩下两个人加载完 -> 一起进 stage 7 -> 这时才补发
        for conn in (self.alice, self.bob):
            gameserver.Conn.on_game_packet(conn, OP_LOADING_DONE, b"")
        self.assertEqual([(2, 0)],
                         [controller_handover(b) for b in
                          bodies(self.bob, OP_CHANGE_CONTROLLER_SLOT)])
        self.assertEqual([], self.room.battle.left_while_loading)

    def test_the_replay_comes_after_the_stage_7_release(self):
        # ★ 顺序是硬约束：客户端要先收到 0x0402 进 stage 7 把 GameContext
        #   建起来，才有表可改。
        gameserver.Conn.on_game_packet(self.carol, OP_LEAVE_SESSION, b"")
        self.clear()
        for conn in (self.alice, self.bob):
            gameserver.Conn.on_game_packet(conn, OP_LOADING_DONE, b"")
        seen = opcodes(self.bob)
        self.assertLess(seen.index(OP_COUNT_GAME_READY),
                        seen.index(OP_CHANGE_CONTROLLER_SLOT))

    def test_nothing_is_replayed_when_nobody_left(self):
        for conn in (self.alice, self.bob, self.carol):
            gameserver.Conn.on_game_packet(conn, OP_LOADING_DONE, b"")
        self.assertNotIn(OP_CHANGE_CONTROLLER_SLOT, opcodes(self.bob))

    def test_a_second_round_forgets_the_old_departure(self):
        gameserver.Conn.on_game_packet(self.carol, OP_LEAVE_SESSION, b"")
        for conn in (self.alice, self.bob):
            gameserver.Conn.on_game_packet(conn, OP_LOADING_DONE, b"")
        for conn in (self.alice, self.bob):
            gameserver.Conn.leave_game_result(conn)
        self.clear()
        gameserver.Conn.on_game_packet(self.alice, OP_COUNT_GAME_READY, b"")
        gameserver.Conn.on_game_packet(self.alice, OP_COUNT_GAME_READY, b"")
        for conn in (self.alice, self.bob):
            gameserver.Conn.on_game_packet(conn, OP_LOADING_DONE, b"")
        self.assertNotIn(OP_CHANGE_CONTROLLER_SLOT, opcodes(self.bob))


class ControllerHandoverThreeWayTests(BattleRoom):
    """三个人一起打的一局，验「交给最闲的那个」和连着走两个人。"""

    def start_battle(self):
        self.carol = make_conn("carol", self.accounts)
        gameserver.Conn.on_game_packet(self.carol, OP_MOVE_INTO_SESSION,
                                       move_into_payload(self.room.room_id))
        gameserver.Conn.on_game_packet(self.alice, OP_COUNT_GAME_READY, b"")
        gameserver.Conn.on_game_packet(self.alice, OP_COUNT_GAME_READY, b"")
        for conn in (self.alice, self.bob, self.carol):
            gameserver.Conn.on_game_packet(conn, OP_LOADING_DONE, b"")

    def clear(self):
        super().clear()
        self.carol.sent.clear()

    def test_three_players_split_the_table(self):
        self.assertEqual([0, 1, 2, 0, 1, 2], self.quest.controllers)

    def test_everyone_left_behind_gets_the_same_handover(self):
        gameserver.Conn.on_game_packet(self.alice, OP_LEAVE_SESSION, b"")
        for conn in (self.bob, self.carol):
            self.assertEqual([(0, 1)],
                             [controller_handover(b) for b in
                              bodies(conn, OP_CHANGE_CONTROLLER_SLOT)],
                             f"{conn.account_name} 那边没收到（或者收错了）")
        self.assertEqual([1, 1, 2, 1, 1, 2], self.quest.controllers)

    def test_a_second_departure_converges_on_the_last_one_standing(self):
        gameserver.Conn.on_game_packet(self.alice, OP_LEAVE_SESSION, b"")
        self.clear()
        gameserver.Conn.on_game_packet(self.bob, OP_LEAVE_SESSION, b"")
        self.assertEqual([(1, 2)],
                         [controller_handover(b) for b in
                          bodies(self.carol, OP_CHANGE_CONTROLLER_SLOT)])
        self.assertEqual([2] * CONTROLLER_SLOT_COUNT, self.quest.controllers)

    def test_the_heir_is_the_least_loaded_survivor(self):
        # 手动摆一张不均的表：座位 1 扛 4 格、座位 2 扛 1 格。
        # 座位 0 走的时候应当交给 2（最闲），不是「新房主」1。
        self.quest.controllers = [0, 1, 1, 1, 1, 2]
        gameserver.Conn.on_game_packet(self.alice, OP_LEAVE_SESSION, b"")
        self.assertEqual([(0, 2)],
                         [controller_handover(b) for b in
                          bodies(self.bob, OP_CHANGE_CONTROLLER_SLOT)])
        self.assertEqual([2, 1, 1, 1, 1, 2], self.quest.controllers)


# ----------------------------------------------------------------------------
# RoomQuest 本身（不碰协议，纯模型）
# ----------------------------------------------------------------------------
class RoomQuestTests(unittest.TestCase):

    def test_ranking_in_quest_mode_is_all_or_nothing(self):
        quest = RoomQuest()
        self.assertEqual([0] * 6, quest.ranking({0: 10, 2: 5}, True))
        quest.mark_success(True)
        self.assertEqual([1, 0, 1, 0, 0, 0], quest.ranking({0: 10, 2: 5}, True))

    def test_mark_success_never_goes_back_to_false(self):
        quest = RoomQuest()
        quest.mark_success(True)
        self.assertTrue(quest.mark_success(False))

    def test_ranking_ignores_seats_out_of_range(self):
        quest = RoomQuest()
        self.assertEqual([0] * 6, quest.ranking({-1: 10, 9: 20}, False))

    def test_claim_item_is_first_come_first_served(self):
        quest = RoomQuest()
        self.assertTrue(quest.claim_item(0x40000000, 0))
        self.assertFalse(quest.claim_item(0x40000000, 1))

    def test_item_handles_never_repeat(self):
        quest = RoomQuest()
        handles = [quest.allocate_item() for _ in range(5)]
        self.assertEqual(len(handles), len(set(handles)))

    # -- 控制者表（§180）---------------------------------------------------
    def test_the_wire_format_is_two_int32(self):
        # 反序列化 0x54cfbf = 两发 0x5d59ff，就这 8 个字节。
        self.assertEqual(b"\x02\x00\x00\x00\x05\x00\x00\x00",
                         build_change_controller_slot(2, 5))

    def test_assign_controllers_matches_the_client_formula(self):
        quest = RoomQuest(seats=[1, 4])
        self.assertEqual([1, 4, 1, 4, 1, 4], quest.controllers)
        self.assertEqual([3] * CONTROLLER_SLOT_COUNT,
                         RoomQuest(seats=[3]).controllers)

    def test_an_empty_room_leaves_the_table_at_zero(self):
        # 客户端在 vec 为空时也是填 0（`mov [edi], ebx`）。
        self.assertEqual([0] * CONTROLLER_SLOT_COUNT,
                         RoomQuest(seats=[]).controllers)

    def test_assign_controllers_ignores_seats_out_of_range(self):
        self.assertEqual([2] * CONTROLLER_SLOT_COUNT,
                         RoomQuest(seats=[-1, 2, 99]).controllers)

    def test_handover_replaces_every_slot_the_leaver_held(self):
        quest = RoomQuest(seats=[0, 1])
        self.assertEqual(1, quest.handover_controller(0, [1]))
        self.assertEqual([1] * CONTROLLER_SLOT_COUNT, quest.controllers)

    def test_handover_does_nothing_when_the_leaver_held_nothing(self):
        quest = RoomQuest(seats=[0, 1])
        before = list(quest.controllers)
        self.assertIsNone(quest.handover_controller(5, [0, 1]))
        self.assertEqual(before, quest.controllers)

    def test_handover_does_nothing_without_a_survivor(self):
        quest = RoomQuest(seats=[0])
        self.assertIsNone(quest.handover_controller(0, []))
        self.assertIsNone(quest.handover_controller(0, [0]))

    def test_handover_picks_the_least_loaded_survivor(self):
        quest = RoomQuest(seats=[0, 1, 2])       # [0, 1, 2, 0, 1, 2]
        quest.controllers = [0, 1, 1, 1, 2, 0]   # 1 扛 3 格、2 扛 1 格
        self.assertEqual(2, quest.handover_controller(0, [1, 2]))
        self.assertEqual([2, 1, 1, 1, 2, 2], quest.controllers)

    def test_handover_breaks_ties_on_the_lowest_seat(self):
        quest = RoomQuest(seats=[0, 1, 2])
        self.assertEqual(1, quest.handover_controller(0, [2, 1]))

    def test_forced_handover_fires_even_when_the_mirror_is_clean(self):
        # 「关卡加载途中走的人」：镜像里必然没有他（镜像是他走之后才建的），
        # 但客户端那张表可能有 —— 那时必须照发。
        quest = RoomQuest(seats=[0, 1])
        self.assertIsNone(quest.handover_controller(2, [0, 1]))
        self.assertEqual(0, quest.handover_controller(2, [0, 1], force=True))
        self.assertEqual([0, 1, 0, 1, 0, 1], quest.controllers)


if __name__ == "__main__":
    unittest.main()
