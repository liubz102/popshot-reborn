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
    OP_END_GAME, OP_END_QUEST, OP_GET_ITEM, OP_GRANT_ITEM, OP_ITEM_EFFECT,
    OP_LEAVE_SESSION, OP_LOADING_DONE,
    OP_MAP_CHANGE_READY, OP_MAP_LOADING_DONE, OP_MARK_QUEST_SUCCESS,
    OP_MOVE_INTO_SESSION, OP_PEER_DATA_UP, OP_PICKED_ITEM, OP_USE_ITEM,
    OP_REP_CHANGE_TO_NEXT_MAP,
    OP_REP_GAME_RESULT, OP_REP_QUEST_SCORE, OP_REPORT_HP_ZERO,
    OP_REQ_CHANGE_TO_NEXT_MAP, OP_REQ_RESPAWN, OP_RESPAWN_CHARACTER,
    OP_UPDATE_QUEST_SCORE, RoomQuest, SESSION_STATUS_PLAYING,
    SESSION_STATUS_WAITING, StartGameHandshake,
    build_change_controller_slot, build_game, take_frame, w_i32, w_wstr,
)
from lobby import (Lobby, MOVE_INTO_ALREADY_PLAYING,               # noqa: E402
                   TEAM_A, TEAM_B, TEAM_LAYOUT_TEAMS)
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
    conn.online_debug = lambda _msg: None
    conn.vlog = lambda _msg: None
    # ★ 走**真的**换代钩子：换代模型的唯一迁移点就在 `Conn.send()` 里
    #   （§218 / D137）。假连接直接把 `send` 换成 `sent.append` 的话，
    #   开局链发出去的 0x0400 / 0x0403 就不会推进模型，测的就不是真接线了。
    def _send(plain):
        gameserver.Conn.note_epoch_from_frame(conn, plain)
        conn.sent.append(plain)

    conn.send = _send
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
    conn.peer_out_gap_ms = gameserver.relayserver.RttStats()
    conn.peer_out_last_at = None
    # 位置数据 UDP 旁路的排序闸门（`udpsync` 铁律 2/3）。假连接也要有 ——
    # `on_peer_data` 的第一件事就是过它。
    conn.peer_order = gameserver.udpsync.HeartbeatOrder()
    conn.peer_lock = threading.RLock()
    conn.peer_order_epoch = None
    conn.login_ticket = ""
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
    #: 房间描述符的参数。``None`` = 用 `create_session_payload` 的默认值。
    #: 对战房是 `(组队, 游戏模式, 道具模式)`，道具模式的用例靠它开关（§190）。
    arguments = None
    #: 还要拉几个人进来（用户名列表，按顺序坐 2 号起）。默认只有 alice + bob；
    #: 「2 打 1 的队伍总分」这类局面需要第三个人才造得出来（§226）。
    extra_players = ()

    def setUp(self):
        self._saved_lobby = gameserver.LOBBY
        gameserver.LOBBY = Lobby()
        self.lobby = gameserver.LOBBY
        self._saved_relay = gameserver.PEER_RELAY
        gameserver.PEER_RELAY = relayserver.RelayServer(
            members_of=gameserver._relay_room_members,
            fallback=gameserver._relay_fallback,
            on_traffic=gameserver._relay_battle_tick)
        self._saved_relay_enabled = gameserver.TCP_RELAY_ENABLED
        gameserver.TCP_RELAY_ENABLED = False

        self.accounts = FakeAccounts(None)
        self.alice = make_conn("alice", self.accounts)
        self.bob = make_conn("bob", self.accounts)
        gameserver.Conn.on_game_packet(
            self.alice, 0x0201,
            create_session_payload(session_type=self.session_type,
                                   arguments=self.arguments))
        self.room = self.lobby.room_of(self.alice)
        gameserver.Conn.on_game_packet(self.bob, OP_MOVE_INTO_SESSION,
                                       move_into_payload(self.room.room_id))
        self.members = [self.alice, self.bob]
        for name in self.extra_players:
            member = make_conn(name, self.accounts)
            gameserver.Conn.on_game_packet(member, OP_MOVE_INTO_SESSION,
                                           move_into_payload(self.room.room_id))
            setattr(self, name, member)
            self.members.append(member)
        self.start_battle()
        self.clear()

    def tearDown(self):
        gameserver.LOBBY = self._saved_lobby
        gameserver.PEER_RELAY = self._saved_relay
        gameserver.TCP_RELAY_ENABLED = self._saved_relay_enabled

    def start_battle(self):
        """走完真正的开局链，让**房里每个人**都进 stage 7。"""
        gameserver.Conn.on_game_packet(self.alice, OP_COUNT_GAME_READY, b"")
        gameserver.Conn.on_game_packet(self.alice, OP_COUNT_GAME_READY, b"")
        for member in self.members:
            gameserver.Conn.on_game_packet(member, OP_LOADING_DONE, b"")

    def clear(self):
        for member in self.members:
            member.sent.clear()

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

    def test_someone_elses_report_of_my_death_is_ignored(self):
        # ★★ bug调查/8「人还活着却进了观战模式」：每台机器各自模拟全场伤害，
        #    射手那台算「炸死了他」、受害者那台算「躲过去了」的分歧是必然的。
        #    照单广播就是让客户端对活人执行 Die()。谁死没死只有本人说了算。
        gameserver.Conn.on_game_packet(self.alice, OP_REPORT_HP_ZERO,
                                       hp_zero_payload(seat=1, arg=0, deaths=0))
        self.assertEqual([], opcodes(self.alice))
        self.assertEqual([], opcodes(self.bob))
        self.assertEqual([0] * gameserver.ROOM_SEAT_COUNT, self.quest.deaths)

    def test_my_own_report_of_my_death_still_broadcasts(self):
        gameserver.Conn.on_game_packet(self.bob, OP_REPORT_HP_ZERO,
                                       hp_zero_payload(seat=1, arg=0, deaths=0))
        self.assertEqual([OP_BROADCAST_DEATH], opcodes(self.alice))
        self.assertEqual([OP_BROADCAST_DEATH], opcodes(self.bob))

    def test_monster_reports_are_not_gated_by_seat(self):
        # 怪由控制者那台模拟，谁都可能替它报 —— 不能套「只认本人」那道门，
        # 去重仍然归 RoomQuest.record_death 管。
        gameserver.Conn.on_game_packet(
            self.bob, OP_REPORT_HP_ZERO,
            hp_zero_payload(handle=0x0010C8FB, seat=0xFF, deaths=0))
        self.assertEqual([OP_BROADCAST_DEATH], opcodes(self.alice))

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
        # ★ bug调查/8 起下发值取自服务端权威计数，但正常路径上两者恒等 ——
        #   `[char+0x600]` 只由我们的广播写，所以「先死一次，再报 1」正是
        #   客户端会做的事，下发的就该是 2。上报方必须是本人（座位 1 = bob）。
        gameserver.Conn.on_game_packet(self.bob, OP_REPORT_HP_ZERO,
                                       hp_zero_payload(seat=1, deaths=0))
        gameserver.Conn.on_game_packet(self.bob, OP_REPORT_HP_ZERO,
                                       hp_zero_payload(seat=1, deaths=1))
        body = bodies(self.bob, OP_BROADCAST_DEATH)[1]
        self.assertEqual(2, struct.unpack_from("<i", body, 6)[0])

    def test_a_bogus_report_cannot_push_the_death_count_up(self):
        # ★ bug调查/8：客户端报的次数比服务端已经广播的多，只可能是句柄撞号 /
        #   跨对象残留。跟着跳会让 HUD 心形一次扣好几颗，所以以我们的为准。
        gameserver.Conn.on_game_packet(self.bob, OP_REPORT_HP_ZERO,
                                       hp_zero_payload(seat=1, deaths=7))
        body = bodies(self.bob, OP_BROADCAST_DEATH)[0]
        self.assertEqual(1, struct.unpack_from("<i", body, 6)[0])
        self.assertEqual(1, self.quest.deaths[1])

    def test_a_respawn_reaches_everyone(self):
        # 不广播的话别人屏幕上你就一直躺着（读侧 0x4931c2 按座位取角色）。
        gameserver.Conn.on_game_packet(self.bob, OP_REQ_RESPAWN,
                                       respawn_payload(character_id=1))
        self.assertEqual([OP_RESPAWN_CHARACTER], opcodes(self.alice))
        self.assertEqual([OP_RESPAWN_CHARACTER], opcodes(self.bob))
        self.assertEqual(bodies(self.alice, OP_RESPAWN_CHARACTER),
                         bodies(self.bob, OP_RESPAWN_CHARACTER))


class RespawnWatchdogTests(BattleRoom):
    """★ bug调查/8「人死了不复活」：客户端那条「死后 5 秒自己发 `0x0413`」的链
    有时候断掉（线上一天 15 次，全在 3 人以上的局），受害者从此躺在地上到本局
    结束。客户端只是在等一发 `0x0419`，而 `0x0419` 由服务端说了算 —— 所以
    不管它卡在哪一道守卫上，服务端到点补一发就能把人拉起来。"""

    session_type = 1
    arguments = (0, 3, 0)      # 个人战 / 夺分（没有命数上限）

    def die(self, conn, seat, deaths=0, x=1500.0, y=820.0):
        gameserver.Conn.on_game_packet(
            conn, OP_REPORT_HP_ZERO,
            hp_zero_payload(handle=seat * 100000 + 100001, seat=seat,
                            arg=0xFF, deaths=deaths, x=x, y=y))

    def later(self, conn=None, extra=1.0):
        """把时钟拨到看门狗到点之后，跑一次心跳。"""
        conn = conn or self.alice
        return gameserver.Conn.check_respawn_watchdog(
            conn, now=gameserver.time.monotonic()
            + gameserver.RESPAWN_WATCHDOG_S + extra)

    def test_a_client_that_never_asks_gets_respawned_anyway(self):
        self.die(self.bob, 1)
        self.clear()
        self.assertEqual(1, self.later())
        for conn in (self.alice, self.bob):
            self.assertEqual([OP_RESPAWN_CHARACTER], opcodes(conn),
                             "看门狗补的 0x0419 必须全房间都收到")
        body = bodies(self.bob, OP_RESPAWN_CHARACTER)[0]
        self.assertEqual(1, struct.unpack_from("<i", body, 0)[0], "座位号")

    def test_a_normal_respawn_disarms_the_watchdog(self):
        self.die(self.bob, 1)
        gameserver.Conn.on_game_packet(self.bob, OP_REQ_RESPAWN,
                                       respawn_payload(character_id=1))
        self.clear()
        self.assertEqual(0, self.later())
        self.assertEqual([], opcodes(self.bob))

    def test_the_watchdog_waits_for_the_client_first(self):
        # 客户端写死 5 秒，看门狗必须明显晚于它，不然会抢在正常重生前面。
        self.die(self.bob, 1)
        self.clear()
        self.assertEqual(0, gameserver.Conn.check_respawn_watchdog(
            self.alice, now=gameserver.time.monotonic() + 5.5))
        self.assertEqual([], opcodes(self.bob))

    def test_it_reuses_the_spawn_point_the_client_picked_last_time(self):
        # 坐标只有客户端知道（`0x4fe70e` 选的 `[char+0x2b0]`）。它自己报过
        # 一次，服务端就记住了 —— 补包时照着发，不会把人扔到地图边缘。
        gameserver.Conn.on_game_packet(
            self.bob, OP_REQ_RESPAWN,
            respawn_payload(character_id=1, x=777, y=888, spawn_index=3))
        self.die(self.bob, 1, deaths=1)
        self.clear()
        self.later()
        body = bodies(self.bob, OP_RESPAWN_CHARACTER)[0]
        self.assertEqual((1, 777, 888, 3), struct.unpack_from("<4i", body, 0))

    def test_it_borrows_someone_elses_spawn_point(self):
        # 重生点表是整张图共用的，借队友用过的那个一样落在地图内。
        gameserver.Conn.on_game_packet(
            self.alice, OP_REQ_RESPAWN,
            respawn_payload(character_id=0, x=123, y=456, spawn_index=2))
        self.die(self.bob, 1)
        self.clear()
        self.later()
        body = bodies(self.bob, OP_RESPAWN_CHARACTER)[0]
        self.assertEqual((1, 123, 456, 2), struct.unpack_from("<4i", body, 0))

    def test_it_falls_back_to_where_he_died(self):
        # 一个重生点都没见过（本局第一次死）：原地站起来不是原版行为，
        # 但比一直躺着强，而且那个坐标一定在地图内 —— 他刚站在那儿。
        self.die(self.bob, 1, x=1500.5, y=820.25)
        self.clear()
        self.later()
        body = bodies(self.bob, OP_RESPAWN_CHARACTER)[0]
        self.assertEqual((1, 1500, 820, 0), struct.unpack_from("<4i", body, 0))

    def test_a_map_change_forgets_the_pending_watchdog(self):
        # 换图会把角色对象全部卸掉重建，旧闩和旧重生点都作废。
        self.die(self.bob, 1)
        self.quest.begin_map_change("Quest03_2")
        self.clear()
        self.assertEqual(0, self.later())

    def test_a_settled_round_does_not_respawn_anyone(self):
        self.die(self.bob, 1)
        self.quest.settled = True
        self.clear()
        self.assertEqual(0, self.later())

    def test_someone_who_left_is_not_respawned(self):
        self.die(self.bob, 1)
        gameserver.Conn.on_game_packet(self.bob, OP_LEAVE_SESSION, b"")
        self.clear()
        self.assertEqual(0, self.later())

    def test_the_watchdog_can_be_switched_off(self):
        # `--respawn-watchdog 0`：留取证窗口用（bug调查/8）——兜底一开，卡住的人
        # 8 秒就被捞起来了，来不及在他那台跑 probe-death.bat。
        self.bob.args.respawn_watchdog = 0
        try:
            self.die(self.bob, 1)
            self.clear()
            self.assertEqual(0, self.later())
            self.assertEqual([], opcodes(self.bob))
        finally:
            self.bob.args.respawn_watchdog = None

    def test_the_battle_heartbeat_drives_the_watchdog(self):
        # ★ 看门狗挂在 `_relay_battle_tick` 上（同步数据战斗中恒定 ~8 Hz，
        #   两条通道都会走到它）。这条钉的就是那根线，别哪天被拆了没人发现。
        self.bob.args.respawn_watchdog = 0.01
        try:
            self.die(self.bob, 1)
            self.clear()
            gameserver.time.sleep(0.05)   # 让那 10 毫秒的闩到点
            gameserver.Conn.on_game_packet(self.alice, OP_PEER_DATA_UP,
                                           b"\xff\x00\xff\x00" + b"\x00" * 8)
        finally:
            self.bob.args.respawn_watchdog = None
        self.assertIn(OP_RESPAWN_CHARACTER, opcodes(self.bob))


class SurvivalRespawnWatchdogTests(RespawnWatchdogTests):
    """生存模式：三条命用完就该躺着（§204），看门狗不许把人捞回来。"""

    arguments = (0, 0, 0)      # 个人战 / 生存

    def test_the_last_life_is_still_the_last_life(self):
        # 3 人以上的个人战里，一个人命用完了这局还在打（用户报的正是那种局）。
        # 那种时候躺着是**原版规则**，看门狗不许把他捞回来。
        # 这里两个人的局第三条命一没就判负结算了，所以把结算标志按回去，
        # 单独把「剩余生命」这一道门露出来。
        for deaths in range(gameserver.PVP_SURVIVAL_LIVES):
            self.die(self.bob, 1, deaths=deaths)
        self.assertEqual(0, self.quest.remaining_lives(1))
        self.quest.settled = False
        self.quest.pvp_reason = None
        self.clear()
        self.assertEqual(0, self.later())
        self.assertEqual([], opcodes(self.bob))

    def test_a_life_that_is_left_still_gets_the_watchdog(self):
        self.die(self.bob, 1, deaths=0)
        self.assertEqual(2, self.quest.remaining_lives(1))
        self.clear()
        self.assertEqual(1, self.later())


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


class CoinPickupTests(BattleRoom):
    """★★ 地上捡到的金币要进结算（§230 / D152）。

    客户端捡金币时**一分钱都不加**（`CoinItem1/5` 的 `vf_11c` 只播
    `Item-EatCoin` 的音效），所以这一份必须由服务端记账、结算时发下去。
    """

    def drop(self, conn, item_id):
        """客户端报一次掉落，返回服务端分配的句柄。"""
        gameserver.Conn.on_game_packet(conn, OP_CREATE_ITEM,
                                       create_item_payload(item_id=item_id))
        body = bodies(conn, OP_CREATED_ITEM)[-1]
        return struct.unpack_from("<I", body, 0)[0]

    def pick(self, conn, seat, handle):
        gameserver.Conn.on_game_packet(conn, OP_GET_ITEM,
                                       get_item_payload(seat_id=seat,
                                                        handle=handle))

    def test_a_coin_is_credited_to_whoever_picked_it_up(self):
        handle = self.drop(self.alice, 10101)
        self.pick(self.bob, 1, handle)
        self.assertEqual(0, self.quest.coins_of(0))
        self.assertEqual(gameserver.coin_value(10101), self.quest.coins_of(1))

    def test_the_five_coin_is_worth_more_than_the_one_coin(self):
        # 面额只有类名这一个证据（CoinItem1 / CoinItem5），比例 1:5。
        self.pick(self.alice, 0, self.drop(self.alice, 10101))
        self.pick(self.alice, 0, self.drop(self.alice, 10102))
        self.assertEqual(gameserver.coin_value(10101)
                         + gameserver.coin_value(10102),
                         self.quest.coins_of(0))
        self.assertGreater(gameserver.coin_value(10102),
                           gameserver.coin_value(10101))

    def test_a_boss_coin_shower_adds_up(self):
        for _ in range(20):
            self.pick(self.alice, 0, self.drop(self.alice, 10102))
        self.assertEqual(20 * gameserver.coin_value(10102),
                         self.quest.coins_of(0))

    def test_the_loser_of_the_arbitration_is_not_credited(self):
        # ★ 同一件东西两个人几乎同时踩到 —— 只有仲裁赢的那个记账。
        handle = self.drop(self.alice, 10102)
        self.pick(self.bob, 1, handle)
        self.pick(self.alice, 0, handle)
        self.assertEqual(0, self.quest.coins_of(0))
        self.assertEqual(gameserver.coin_value(10102), self.quest.coins_of(1))

    def test_items_that_are_not_coins_credit_nothing(self):
        for item_id in (10100, 10300, 10200):      # 红心 / 护盾 / 武器
            self.pick(self.alice, 0, self.drop(self.alice, item_id))
        self.assertEqual(0, self.quest.coins_of(0))

    def test_an_unknown_handle_credits_nothing(self):
        # 协议试探造的假句柄：`item_id_of` 是 None，别当成金币。
        self.pick(self.alice, 0, ITEM_HANDLE_BASE + 999)
        self.assertEqual(0, self.quest.coins_of(0))


# ----------------------------------------------------------------------------
# 道具模式：服务端往地图上刷道具（§191 / D109）
# ----------------------------------------------------------------------------
class ItemSpawnBase(BattleRoom):
    """用户实机报的「道具模式下地图里找不到道具」。

    根因（§191）：地图文件里一件道具都没放，客户端也没有任何一处会自己请求
    生成 —— **只有服务端能把道具放到地图上**，而我们从来没发过。
    """

    session_type = 1
    arguments = (0, 0, 1)       # 个人战 + 道具模式

    def tick(self, conn=None):
        """走一发同步数据，让 `_relay_battle_tick` 跑一次。"""
        gameserver.Conn.on_peer_data(conn or self.alice, b"\x00" * 43)

    def due_now(self):
        """把「下一次刷新」的时刻拨到现在。"""
        self.quest.next_item_spawn_at = 0.0

    def spawned(self, conn=None):
        """某条连接收到的刷新包，解成 `(句柄, 物件 id, X, Y)` 列表。"""
        out = []
        for body in bodies(conn or self.alice, OP_CREATED_ITEM):
            handle = struct.unpack_from("<I", body, 0)[0]
            item_id, x, y = struct.unpack_from("<iff", body, 4)
            out.append((handle, item_id, x, y))
        return out

    def spawn_many(self, count):
        for _ in range(count):
            self.due_now()
            self.tick()

    def spawn_and_take(self, count):
        """刷 `count` 件，每刷一件就当场捡走 —— 否则会撞上「同时最多几件」的上限。"""
        for _ in range(count):
            self.due_now()
            self.tick()
            for handle in list(self.quest.items_on_map):
                self.quest.claim_item(handle, 0)


class ItemSpawnTests(ItemSpawnBase):

    def test_nothing_is_spawned_before_the_first_delay(self):
        # 开局那一刻就往地上扔东西太怪；先给玩家几秒钟落地。
        self.tick()
        self.assertEqual([], opcodes(self.alice))

    def test_an_item_appears_once_the_timer_is_due(self):
        self.due_now()
        self.tick()
        self.assertEqual([OP_CREATED_ITEM], opcodes(self.alice))
        # bob 那边还会先收到一发转发过来的同步数据（0x040f），只看刷新包。
        self.assertEqual(1, opcodes(self.bob).count(OP_CREATED_ITEM),
                         "★ 不广播的话道具只在一个人屏幕上存在")
        self.assertEqual(bodies(self.alice, OP_CREATED_ITEM),
                         bodies(self.bob, OP_CREATED_ITEM))

    def test_the_next_one_waits_for_the_interval(self):
        self.due_now()
        self.tick()
        self.clear()
        self.tick()
        self.assertEqual([], opcodes(self.alice))

    def test_the_spawned_id_is_always_one_the_client_can_build(self):
        # ★ 工厂 0x513278 认不出的 id 会走 default 分支 —— 10305 就是这样一个
        #   「Item.ini 里有、工厂里没有」的坑（§191）。
        self.spawn_and_take(40)
        ids = {item_id for _, item_id, _, _ in self.spawned()}
        self.assertTrue(ids)
        self.assertTrue(ids <= set(gameserver.PVP_ITEM_IDS
                                   + gameserver.PVP_WEAPON_ITEM_IDS))
        self.assertNotIn(10305, ids)
        for item_id in ids:
            self.assertIn(item_id, gameserver.ITEM_NAMES)

    def test_the_handles_are_unique_and_room_level(self):
        self.spawn_many(5)
        handles = [handle for handle, _, _, _ in self.spawned()]
        self.assertEqual(len(handles), len(set(handles)))
        self.assertEqual(handles, sorted(handles))
        self.assertGreaterEqual(min(handles), ITEM_HANDLE_BASE)

    def test_a_client_drop_and_a_server_spawn_never_share_a_handle(self):
        self.due_now()
        self.tick()
        gameserver.Conn.on_game_packet(self.alice, OP_CREATE_ITEM,
                                       create_item_payload())
        handles = [handle for handle, _, _, _ in self.spawned()]
        self.assertEqual(len(handles), len(set(handles)))

    def test_the_coordinates_are_positive(self):
        # 客户端会 `fmod` 进地图（§192），负数取模出来还是负数 = 图外。
        self.spawn_many(20)
        for _, _, x, y in self.spawned():
            self.assertGreaterEqual(x, 0.0)
            self.assertGreaterEqual(y, 0.0)

    def test_the_map_holds_at_most_the_cap(self):
        self.spawn_many(gameserver.ITEM_SPAWN_MAX_ALIVE + 5)
        self.assertEqual(gameserver.ITEM_SPAWN_MAX_ALIVE, len(self.spawned()))

    def test_picking_one_up_frees_a_slot(self):
        self.spawn_many(gameserver.ITEM_SPAWN_MAX_ALIVE)
        taken = self.spawned()[0][0]
        gameserver.Conn.on_game_packet(
            self.bob, OP_GET_ITEM, get_item_payload(seat_id=1, handle=taken))
        self.clear()
        self.due_now()
        self.tick()
        self.assertEqual([OP_CREATED_ITEM], opcodes(self.alice))

    def test_the_pickup_arbitration_covers_server_spawned_items(self):
        self.due_now()
        self.tick()
        handle = self.spawned()[0][0]
        gameserver.Conn.on_game_packet(
            self.alice, OP_GET_ITEM, get_item_payload(seat_id=0, handle=handle))
        self.clear()
        gameserver.Conn.on_game_packet(
            self.bob, OP_GET_ITEM, get_item_payload(seat_id=1, handle=handle))
        self.assertEqual([], opcodes(self.bob), "晚到的那一发绝不能回包")

    def test_nothing_is_spawned_after_the_round_is_over(self):
        self.quest.pvp_reason = "时间到"
        self.due_now()
        self.tick()
        self.assertEqual([], opcodes(self.alice))

    def test_a_second_round_starts_spawning_again(self):
        # `room.quest` 回房间时整个丢掉，刷新时钟跟着重来。
        self.due_now()
        self.tick()
        first = self.quest
        self.room.quest = None
        self.assertIsNot(first, self.alice.quest_state())
        self.clear()
        self.tick()
        self.assertEqual([], opcodes(self.alice), "新的一局也要先等第一次延迟")

    def test_the_tick_runs_on_the_relay_path_too(self):
        # ★ 中继一建起来，整局连一发 0x040e 都不会再有（§160）——
        #   所以节拍必须挂在 `deliver()` 上，不能挂在 `on_peer_data` 上。
        self.due_now()
        gameserver.PEER_RELAY.deliver(self.alice, b"\x00" * 43)
        self.assertEqual(1, opcodes(self.bob).count(OP_CREATED_ITEM))

    def test_a_broken_tick_never_breaks_the_sync_forwarding(self):
        # 中继连接一断客户端会自己退房（§158），转发必须比附加逻辑更硬。
        def boom(_conn):
            raise RuntimeError("坏了")

        relay = relayserver.RelayServer(
            members_of=gameserver._relay_room_members,
            fallback=gameserver._relay_fallback, on_traffic=boom)
        self.assertEqual(1, relay.deliver(self.alice, b"\x00" * 43))
        self.assertEqual([gameserver.OP_PEER_DATA_DOWN], opcodes(self.bob))


class ItemPoolTests(unittest.TestCase):
    """道具池本身 —— 不走协议，所以可以钉死（随机数注进去）。"""

    class Recorder:
        """记下 `due_item_spawn` 到底从哪个池子里挑的。"""

        def __init__(self):
            self.pools = []

        def choice(self, pool):
            self.pools.append(tuple(pool))
            return pool[0]

        def uniform(self, low, _high):
            return low

    def pool_for(self, team_mode):
        quest = RoomQuest()
        quest.next_item_spawn_at = 0.0
        recorder = self.Recorder()
        quest.due_item_spawn(now=1.0, team_mode=team_mode,
                             random_source=recorder)
        return recorder.pools[0]

    def test_a_free_for_all_pool_has_no_team_items(self):
        self.assertEqual(gameserver.PVP_ITEM_IDS
                         + gameserver.PVP_WEAPON_ITEM_IDS,
                         self.pool_for(False))

    def test_a_team_round_adds_the_team_items(self):
        self.assertEqual(
            gameserver.PVP_ITEM_IDS + gameserver.PVP_WEAPON_ITEM_IDS
            + gameserver.PVP_TEAM_ITEM_IDS,
            self.pool_for(True))

    def test_the_three_special_weapons_are_in_the_pool(self):
        # ★ 用户报的第二条：「道具模式只掉道具，从没见过武器」（§223）。
        #   三把武器和箱子道具走同一发 0x0404，缺的只是没进池子。
        pool = set(self.pool_for(False))
        for item_id in (10200, 10201, 10202):
            self.assertIn(item_id, pool)

    def test_every_id_in_the_pool_can_actually_be_built(self):
        # ★ 10305（FastShot）在 `Item.ini` 里有，但工厂 0x513278 的跳表
        #   `0x513b56` 那一格指的是 default —— 发下去客户端建不出对象（§191）。
        for pool in (gameserver.PVP_ITEM_IDS, gameserver.PVP_TEAM_ITEM_IDS,
                     gameserver.PVP_WEAPON_ITEM_IDS):
            self.assertNotIn(10305, pool)
            for item_id in pool:
                self.assertIn(item_id, gameserver.ITEM_NAMES)
        for item_id in gameserver.PVP_ITEM_IDS + gameserver.PVP_TEAM_ITEM_IDS:
            self.assertTrue(10300 <= item_id <= 10500,
                            f"{item_id} 不是 PvP 道具的 id 段")
        for item_id in gameserver.PVP_WEAPON_ITEM_IDS:
            self.assertTrue(10200 <= item_id <= 10202,
                            f"{item_id} 不是特殊武器的 id 段")

    def test_the_pools_do_not_overlap(self):
        self.assertFalse(set(gameserver.PVP_ITEM_IDS)
                         & set(gameserver.PVP_TEAM_ITEM_IDS))
        self.assertFalse(set(gameserver.PVP_WEAPON_ITEM_IDS)
                         & set(gameserver.PVP_ITEM_IDS
                               + gameserver.PVP_TEAM_ITEM_IDS))

    def test_every_boxed_item_in_the_pool_has_an_item_ini_record(self):
        # ★★ `UseItemEffect` 第一件事就是查 `Item.ini` 的记录表，查不到
        #    **直接 return**（§201）。所以「工厂建得出箱子」还不够 ——
        #    没记录的道具捡起来能进槽，按 Ctrl 却彻底没反应。
        #    ⚠ 这条只管**进槽的箱子道具**：三把特殊武器捡起来当场换枪，
        #    压根不走 `UseItemEffect`，`Item.ini` 里没有它们也是对的（§223）。
        for pool in (gameserver.PVP_ITEM_IDS, gameserver.PVP_TEAM_ITEM_IDS):
            for item_id in pool:
                self.assertIn(item_id, gameserver.ITEM_INI_ITEM_IDS,
                              f"{item_id} 在 Item.ini 里没有记录，用了不会有效果")

    def test_the_weapons_never_go_into_an_item_slot(self):
        # 武器 `[item+0x2a9] == 0` -> 拾取当场生效；再补一发 0x040b
        # 等于凭空多一件道具（§194 / §223）。
        for item_id in gameserver.PVP_WEAPON_ITEM_IDS:
            self.assertNotIn(item_id, gameserver.GRANTABLE_ITEM_IDS)

    def test_the_sp_up_item_stays_out_of_the_pool(self):
        # 10302 是「工厂建得出、但 Item.ini 没这一节」的那一个（§201）。
        # 它和 10305 正好是两种相反的坏法，两条都要钉住。
        self.assertNotIn(10302, gameserver.PVP_ITEM_IDS)
        self.assertNotIn(10302, gameserver.PVP_TEAM_ITEM_IDS)
        self.assertNotIn(10302, gameserver.ITEM_INI_ITEM_IDS)


class ItemSpawnTeamModeTests(ItemSpawnBase):
    """组队战才刷「全队」道具（端到端那一半）。"""

    arguments = (1, 0, 1)       # 组队战 + 道具模式

    def test_the_room_is_in_item_mode_and_in_team_layout(self):
        self.assertTrue(self.room.item_mode())
        self.spawn_and_take(30)
        ids = {item_id for _, item_id, _, _ in self.spawned()}
        self.assertTrue(
            ids <= set(gameserver.PVP_ITEM_IDS
                       + gameserver.PVP_WEAPON_ITEM_IDS
                       + gameserver.PVP_TEAM_ITEM_IDS))


class ItemSpawnFreeForAllTests(ItemSpawnBase):

    def test_team_items_stay_out_of_a_free_for_all(self):
        self.spawn_and_take(60)
        ids = {item_id for _, item_id, _, _ in self.spawned()}
        self.assertFalse(ids & set(gameserver.PVP_TEAM_ITEM_IDS))


class NoItemModeTests(ItemSpawnBase):
    """노템전（普通模式）—— 一件都不许刷。"""

    arguments = (0, 0, 0)

    def test_nothing_is_spawned(self):
        self.spawn_many(5)
        self.assertEqual([], opcodes(self.alice))


class ItemModeOffByGameModeTests(ItemSpawnBase):
    """游戏模式 2 下客户端**强制**无道具（`0x465be2`），服务端跟着它判。"""

    arguments = (0, 2, 1)

    def test_nothing_is_spawned(self):
        self.spawn_many(5)
        self.assertEqual([], opcodes(self.alice))


class QuestRoomItemSpawnTests(ItemSpawnBase):
    """闯关房没有道具模式（`0x409dd9` 对 type != 1 恒返回 -1）。"""

    session_type = 2
    arguments = (3, 1)

    def test_nothing_is_spawned(self):
        self.spawn_many(5)
        self.assertEqual([], opcodes(self.alice))


# ----------------------------------------------------------------------------
# 道具槽：捡到之后进不进得了道具栏、按 Ctrl 用不用得出去（§194 / D110）
#
# 用户实机报的：「走过去有捡起的动画和音效，道具也会消失，但是道具栏不会
# 显示新捡的道具，也无法使用」。
#
# 根因：`0x0405` 拾取放行对 PvP 道具**只把箱子抹掉 + 放特效**。道具进槽只有
# `0x040b`、离开槽只有 `0x040c`、效果生效只有 `0x040a` —— 三个包在客户端里
# 各自只有一个调用点，全都得服务端发。
# ----------------------------------------------------------------------------
class ItemSlotBase(ItemSpawnBase):
    """道具模式的房间，且带上「刷一件 -> 捡走」的两个夹具。

    ★ 这一整组测的是**道具槽**，所以夹具把特殊武器的权重压成 0（§223）——
    武器捡起来是当场换枪、根本不进槽，随机抽到一把就会让「捡了进槽」
    这一类断言偶发地红。武器自己那条路由 `ItemPoolTests` 和
    `test_quest_drops_are_not_grantable` 钉住。
    """

    def setUp(self):
        super().setUp()
        saved = gameserver.PVP_WEAPON_SPAWN_WEIGHT
        gameserver.PVP_WEAPON_SPAWN_WEIGHT = 0
        self.addCleanup(setattr, gameserver,
                        "PVP_WEAPON_SPAWN_WEIGHT", saved)

    def spawn_one(self):
        """刷一件道具，返回 `(句柄, 物件 id)`。"""
        self.due_now()
        self.tick()
        handle, item_id, _x, _y = self.spawned()[-1]
        return handle, item_id

    def pick(self, conn, seat_id, handle):
        gameserver.Conn.on_game_packet(conn, OP_GET_ITEM,
                                       get_item_payload(seat_id, handle))

    def spawn_and_pick(self, conn=None, seat_id=0):
        """刷一件并让某个座位捡走，返回物件 id。"""
        conn = conn or self.alice
        handle, item_id = self.spawn_one()
        self.clear()
        self.pick(conn, seat_id, handle)
        return item_id

    def use(self, conn, slot_index=0):
        gameserver.Conn.on_game_packet(conn, OP_USE_ITEM,
                                       w_i32(slot_index))

    def effects(self, conn):
        """某条连接收到的 `0x040a`，解成 `(座位, arg3, 物件 id, arg2)`。"""
        return [struct.unpack(gameserver.ITEM_EFFECT_FORMAT, body)
                for body in bodies(conn, OP_ITEM_EFFECT)]


class ItemGrantTests(ItemSlotBase):

    def test_picking_up_a_pvp_item_also_grants_it(self):
        # ★ 这就是用户报的那条：没有这一发，箱子没了但道具栏是空的。
        self.spawn_and_pick()
        self.assertIn(OP_GRANT_ITEM, opcodes(self.alice))

    def test_the_grant_carries_the_id_that_was_on_the_ground(self):
        item_id = self.spawn_and_pick()
        self.assertEqual([w_i32(item_id)], bodies(self.alice, OP_GRANT_ITEM))

    def test_the_grant_goes_only_to_the_one_who_picked_it_up(self):
        # ★★ `0x040b` 的处理器（`0x55206b`）用的是 `0x409f39`
        #    = **收包这台机器上的本地玩家**，包里根本没有座位号。
        #    广播出去就是「一个箱子人手一件」。
        self.spawn_and_pick()
        self.assertNotIn(OP_GRANT_ITEM, opcodes(self.bob))

    def test_the_grant_follows_the_seat_in_the_request_not_the_sender(self):
        # 收件人由 `0x0407` 里的座位号决定（那一格是 `[Character+0x2ac]`）。
        handle, _item_id = self.spawn_one()
        self.clear()
        self.pick(self.alice, 1, handle)          # 座位 1 = bob
        self.assertIn(OP_GRANT_ITEM, opcodes(self.bob))
        self.assertNotIn(OP_GRANT_ITEM, opcodes(self.alice))

    def test_the_pickup_broadcast_still_reaches_everyone(self):
        # 补发 `0x040b` 不能把原来那一发 `0x0405` 挤掉 —— 别人屏幕上的
        # 箱子还得靠它消失。
        self.spawn_and_pick()
        self.assertIn(OP_PICKED_ITEM, opcodes(self.alice))
        self.assertIn(OP_PICKED_ITEM, opcodes(self.bob))

    def test_the_grant_comes_after_the_pickup(self):
        # 先放行再给道具：反过来的话客户端会在物件还在世界里时就多一件。
        self.spawn_and_pick()
        seq = [op for op in opcodes(self.alice)
               if op in (OP_PICKED_ITEM, OP_GRANT_ITEM)]
        self.assertEqual([OP_PICKED_ITEM, OP_GRANT_ITEM], seq)

    def test_a_coin_is_never_granted(self):
        # ★ 金币 / 红心的 `[item+0x2a9]` 是 0，拾取当场就生效了
        #   （`Item::vf_d4` 那条 `vf_11c` 分支）。再发一发 `0x040b`
        #   等于凭空往道具栏里塞一件根本不存在的东西。
        gameserver.Conn.on_game_packet(self.alice, OP_CREATE_ITEM,
                                       create_item_payload(item_id=10101))
        handle = struct.unpack_from("<I", bodies(self.alice,
                                                 OP_CREATED_ITEM)[0], 0)[0]
        self.clear()
        self.pick(self.alice, 0, handle)
        self.assertEqual([OP_PICKED_ITEM], opcodes(self.alice))
        self.assertEqual([], self.quest.item_slots[0])

    def test_an_unknown_handle_is_not_granted(self):
        # 协议试探 / 控制通道手搓出来的句柄我们没记过类型，宁可不发。
        self.clear()
        self.pick(self.alice, 0, ITEM_HANDLE_BASE + 999)
        self.assertEqual([OP_PICKED_ITEM], opcodes(self.alice))

    def test_the_mirror_follows_what_was_granted(self):
        first = self.spawn_and_pick()
        second = self.spawn_and_pick()
        self.assertEqual([first, second], self.quest.item_slots[0])
        self.assertEqual([], self.quest.item_slots[1])

    def test_a_full_slot_stops_granting(self):
        # ★ 客户端 `AddItem` 扫不到空格就**整个函数什么都不做**。我们这边
        #   要是照记不误，之后按 Ctrl 就会用出一件客户端没有的道具。
        for _ in range(gameserver.ITEM_SLOT_COUNT):
            self.spawn_and_pick()
        self.assertEqual(gameserver.ITEM_SLOT_COUNT,
                         len(self.quest.item_slots[0]))
        self.clear()
        self.spawn_and_pick()
        self.assertEqual([OP_PICKED_ITEM], opcodes(self.alice),
                         "满了就只放行拾取，不再发 0x040b")
        self.assertEqual(gameserver.ITEM_SLOT_COUNT,
                         len(self.quest.item_slots[0]))


class ItemUseTests(ItemSlotBase):

    def test_using_an_item_takes_it_out_of_the_slot(self):
        self.spawn_and_pick()
        self.clear()
        self.use(self.alice)
        self.assertIn(OP_USE_ITEM, opcodes(self.alice))
        self.assertEqual([], self.quest.item_slots[0])

    def test_the_removal_goes_only_to_the_one_who_used_it(self):
        # `0x040c` 的处理器同样按「收包机器上的本地玩家」认人。
        self.spawn_and_pick()
        self.clear()
        self.use(self.alice)
        self.assertNotIn(OP_USE_ITEM, opcodes(self.bob))

    def test_the_removal_echoes_the_slot_index(self):
        self.spawn_and_pick()
        self.spawn_and_pick()
        self.clear()
        self.use(self.alice, slot_index=1)
        self.assertEqual([w_i32(1)], bodies(self.alice, OP_USE_ITEM))

    def test_the_effect_reaches_everyone(self):
        # ★ 不广播的话别人屏幕上你既不加速也不亮护盾，而伤害是各机器各算的。
        self.spawn_and_pick()
        self.clear()
        self.use(self.alice)
        self.assertIn(OP_ITEM_EFFECT, opcodes(self.alice))
        self.assertIn(OP_ITEM_EFFECT, opcodes(self.bob))
        self.assertEqual(bodies(self.alice, OP_ITEM_EFFECT),
                         bodies(self.bob, OP_ITEM_EFFECT))

    def test_the_effect_carries_the_seat_and_the_item_id(self):
        item_id = self.spawn_and_pick()
        self.clear()
        self.use(self.alice)
        self.assertEqual(
            [(0, gameserver.ITEM_EFFECT_ARG3, item_id,
              gameserver.ITEM_EFFECT_ARG2)],
            self.effects(self.bob))

    def test_the_effect_names_the_user_not_the_receiver(self):
        handle, item_id = self.spawn_one()
        self.clear()
        self.pick(self.bob, 1, handle)
        self.use(self.bob)
        self.assertEqual([(1, gameserver.ITEM_EFFECT_ARG3, item_id,
                           gameserver.ITEM_EFFECT_ARG2)],
                         self.effects(self.alice))

    def test_items_are_used_first_in_first_out(self):
        first = self.spawn_and_pick()
        second = self.spawn_and_pick()
        self.clear()
        self.use(self.alice)
        self.use(self.alice)
        self.assertEqual([first, second],
                         [eff[2] for eff in self.effects(self.bob)])

    def test_using_an_empty_slot_replies_nothing(self):
        # 没捡过就按 Ctrl（或者连按两下）—— 一个包都不回，同拾取被拒的处置。
        self.clear()
        self.use(self.alice)
        self.assertEqual([], opcodes(self.alice))
        self.assertEqual([], opcodes(self.bob))

    def test_using_twice_only_works_once(self):
        self.spawn_and_pick()
        self.use(self.alice)
        self.clear()
        self.use(self.alice)
        self.assertEqual([], opcodes(self.alice))

    def test_a_short_payload_is_ignored(self):
        self.spawn_and_pick()
        self.clear()
        gameserver.Conn.on_game_packet(self.alice, OP_USE_ITEM, b"\x00")
        self.assertEqual([], opcodes(self.alice))
        self.assertEqual(1, len(self.quest.item_slots[0]),
                         "解析失败绝不能把道具吃掉")

    def test_one_players_items_are_not_the_others(self):
        handle, _ = self.spawn_one()
        self.pick(self.alice, 0, handle)
        self.clear()
        self.use(self.bob)                       # bob 手上什么都没有
        self.assertEqual([], opcodes(self.bob))
        self.assertEqual(1, len(self.quest.item_slots[0]))


# ----------------------------------------------------------------------------
# 道具效果**结束**：`0x040d`（§200）
#
# 用户实机报的：「三重射击和毒药道具效果时间过了之后，自己能看到模型恢复了，
# 但是别人看不到模型恢复」。
#
# 根因：`0x040a` 只管开始。弹数型道具（`Status.ini` 里只有 `Magazine`
# 没有 `Time`）的 duration 是 -1，唯一的终止条件是「本机玩家把那几发打完」——
# 只有他自己那台机器知道，于是客户端发 `0x040d(座位, 属性号)` 上来。
# 服务端不转发的话，别人屏幕上那把枪永远变不回去。
# ----------------------------------------------------------------------------
def remove_attr_payload(seat_id, attr_id):
    """客户端方向的 `0x040d rawRemoveCharAttr`（两个 int32）。"""
    return w_i32(seat_id) + w_i32(attr_id)


class AttrRemovalTests(BattleRoom):

    OP = gameserver.OP_REMOVE_CHAR_ATTR
    ATTR_TRIPLE_SHOT = 6

    def end_attr(self, conn, seat_id=None, attr_id=None):
        seat_id = conn.my_seat if seat_id is None else seat_id
        attr_id = self.ATTR_TRIPLE_SHOT if attr_id is None else attr_id
        gameserver.Conn.on_game_packet(
            conn, self.OP, remove_attr_payload(seat_id, attr_id))

    def test_the_end_of_an_effect_reaches_the_others(self):
        # ★ 这就是用户报的那条：没有这一发，队友屏幕上三连射的枪永远不变回去。
        self.end_attr(self.alice)
        self.assertIn(self.OP, opcodes(self.bob))

    def test_the_reporter_does_not_get_it_back(self):
        # 客户端 `0x551dfb` 第一句就是 `if (座位 == 我的座位) return`，
        # 回给他等于白费字节。
        self.end_attr(self.alice)
        self.assertNotIn(self.OP, opcodes(self.alice))

    def test_the_payload_is_seat_then_attr(self):
        self.end_attr(self.alice, attr_id=self.ATTR_TRIPLE_SHOT)
        self.assertEqual([remove_attr_payload(0, self.ATTR_TRIPLE_SHOT)],
                         bodies(self.bob, self.OP))

    def test_the_seat_comes_from_the_connection_not_the_packet(self):
        # 谁的效果结束了只能由服务端说了算，否则一个人就能替别人撤护盾。
        self.bob.my_seat = 1
        self.end_attr(self.bob, seat_id=0)
        self.assertEqual([remove_attr_payload(1, self.ATTR_TRIPLE_SHOT)],
                         bodies(self.alice, self.OP))

    def test_a_short_payload_is_dropped(self):
        gameserver.Conn.on_game_packet(self.alice, self.OP, b"\x06\x00\x00")
        self.assertEqual([], opcodes(self.bob))

    def test_an_out_of_range_attr_is_dropped(self):
        # `Status.ini` 只有 0~20，`AddAttrVisual` 的跳表也只有 20 项。
        self.end_attr(self.alice, attr_id=gameserver.CHAR_ATTR_MAX + 1)
        self.end_attr(self.alice, attr_id=-1)
        self.assertEqual([], opcodes(self.bob))

    def test_the_base_state_attr_is_forwarded_too(self):
        # 死后每 5 秒一发 `(座位, 0)`（§167 实测），照转不误 ——
        # 那是原版协议的一部分，客户端自己会判要不要动作。
        self.end_attr(self.alice, attr_id=0)
        self.assertEqual([remove_attr_payload(0, 0)],
                         bodies(self.bob, self.OP))

    def test_every_attr_id_in_the_table_is_named(self):
        for attr_id in range(gameserver.CHAR_ATTR_MAX + 1):
            self.assertIn(attr_id, gameserver.CHAR_ATTR_NAMES)


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


def end_game_success(body):
    """`0x0411` 的 `bool32 成功`（座位号之后那一个 int32）。"""
    return bool(struct.unpack_from("<i", body, 4)[0])


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
        exp, _money = gameserver.quest_reward(3, 1, 40, False)
        self.assertEqual(200 + exp, self.accounts.saved["alice"]["experience"])

    def test_each_player_is_paid_their_own_score(self):
        # ★★ 经验和金币不再等于分数（§227）：按「关卡 id × 难度」给基础奖励，
        #    分数只做小幅加成，两者**各有各的系数**。房间是 (关卡 3, 难度 1)，
        #    没通关（本例不调 clear_quest），所以基础部分打 QUEST_FAILED_RATIO 折。
        self.score(self.alice, 0, 40)
        self.score(self.bob, 1, 25)
        self.end()
        alice_exp, alice_money = gameserver.quest_reward(3, 1, 40, False)
        bob_exp, bob_money = gameserver.quest_reward(3, 1, 25, False)
        self.assertEqual(200 + alice_exp, self.accounts.saved["alice"]["experience"])
        self.assertEqual(400 + bob_exp, self.accounts.saved["bob"]["experience"])
        self.assertEqual(10 + alice_money, self.accounts.saved["alice"]["money"])
        self.assertEqual(20 + bob_money, self.accounts.saved["bob"]["money"])
        # 打得多的人拿得多，但经验和金币是两个不同的数。
        self.assertGreater(alice_exp, bob_exp)
        self.assertNotEqual(alice_exp, alice_money)

    def test_picked_coins_land_in_the_settlement_money(self):
        """★★ 地上捡到的金币要加进「金币 +N」（§230 / D152）。

        客户端捡金币时一分钱都不加，所以不加这一份的话，怪和 boss 掉的金币
        对玩家来说等于不存在。
        """
        self.score(self.alice, 0, 40)
        quest = self.alice.quest_state()
        quest.add_coins(0, 250)
        quest.add_coins(1, 10)
        self.end()
        base_exp, base_money = gameserver.quest_reward(3, 1, 40, False)
        self.assertEqual(10 + base_money + 250,
                         self.accounts.saved["alice"]["money"])
        self.assertEqual(20 + base_money + 10,
                         self.accounts.saved["bob"]["money"])
        # 经验不吃金币 —— 那是两条独立的线。
        self.assertEqual(200 + base_exp, self.accounts.saved["alice"]["experience"])

    def test_the_result_screen_shows_the_coins_too(self):
        # `0x0309` 的值 10 就是界面上那一行「金币 +N」，必须含捡到的那一份。
        self.alice.quest_state().add_coins(0, 250)
        self.end()
        body = [b for b in bodies(self.alice, OP_REP_GAME_RESULT)
                if result_seat(b) == 0][0]
        values = struct.unpack_from(
            f"<{gameserver.GAME_RESULT_VALUE_COUNT}i", body, 4)
        _exp, base_money = gameserver.quest_reward(3, 1, 0, False)
        self.assertEqual(base_money + 250,
                         values[gameserver.GAME_RESULT_MONEY])

    def test_money_no_longer_scales_with_the_score(self):
        # ★ D152：金币 = 固定值 + 捡到的金币，**不吃分数加成**。
        low, high = gameserver.quest_reward(3, 1, 0, True),             gameserver.quest_reward(3, 1, 5000, True)
        self.assertEqual(low[1], high[1], "金币不该跟着分数走")
        self.assertLess(low[0], high[0], "经验仍然该跟着分数走")

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
    """夺分模式（arguments[1] == 3）：按本局分数判胜负。"""

    session_type = 1
    arguments = (0, 3, 0)

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

    def test_a_draw_judges_nobody(self):
        # ★ 照抄原版 `0x55c0bb`：**全员并列就一个都不判**（尾数组全 0 =
        #   标签「未完成」+ 胜利曲）。以前我们判成「大家都赢」，
        #   那是自己发明的口径（§226）。
        self.score(self.alice, 0, 30)
        self.score(self.bob, 1, 30)
        self.clear()
        gameserver.Conn.on_game_packet(self.alice, OP_END_QUEST, b"")
        body = bodies(self.alice, OP_REP_GAME_RESULT)[0]
        self.assertEqual([0] * GAME_RESULT_TAIL_COUNT, result_tail(body))

    def test_fewer_deaths_breaks_a_tie_on_score(self):
        # 原版的个人合成分是 `(分数 + 1) * 1000 - 死亡数`：分数一样时
        # 死得少的赢，但死亡数永远翻不了分数的盘。
        self.score(self.alice, 0, 30)
        self.score(self.bob, 1, 30)
        quest = self.alice.quest_state()
        quest.deaths[1] = 2
        self.clear()
        gameserver.Conn.on_game_packet(self.alice, OP_END_QUEST, b"")
        body = bodies(self.alice, OP_REP_GAME_RESULT)[0]
        self.assertEqual([GAME_RESULT_CLEARED, GAME_RESULT_DEFEATED] + [0] * 4,
                         result_tail(body))

    def test_a_scoreless_round_judges_nobody(self):
        # 没打就散了。判谁输都是瞎判，两边都放失败曲更难看。
        gameserver.Conn.on_game_packet(self.alice, OP_END_QUEST, b"")
        body = bodies(self.alice, OP_REP_GAME_RESULT)[0]
        self.assertEqual([0] * GAME_RESULT_TAIL_COUNT, result_tail(body))

    def test_pvp_money_is_flat_but_still_picks_up_coins(self):
        # ★ D152：对战的金币也不吃杀敌数（杀敌数就是对战的分数），
        #   只剩「参战底薪 + 胜方加成」两个固定值，再加上地上捡到的。
        #   经验仍然按杀敌数走。
        self.assertEqual(gameserver.pvp_reward(0, True)[1],
                         gameserver.pvp_reward(40, True)[1])
        self.assertLess(gameserver.pvp_reward(0, True)[0],
                        gameserver.pvp_reward(40, True)[0])
        self.score(self.alice, 0, 40)
        self.alice.quest_state().add_coins(0, 17)
        gameserver.Conn.on_game_packet(self.alice, OP_END_QUEST, b"")
        win_exp, win_money = gameserver.pvp_reward(40, True)
        self.assertEqual(10 + win_money + 17, self.accounts.saved["alice"]["money"])
        self.assertEqual(200 + win_exp, self.accounts.saved["alice"]["experience"])

    def test_a_pvp_round_never_records_a_quest_clear(self):
        self.score(self.alice, 0, 40)
        gameserver.Conn.on_game_packet(self.alice, OP_END_QUEST, b"")
        self.assertEqual([], self.accounts.cleared)

    def test_a_pvp_round_pays_by_kills_and_by_the_verdict(self):
        # ★★ 对战的经验和金币也拆开了（§227）：底薪 + 每杀 + 胜方加成，
        #    而不是「经验 = 金币 = 杀敌数」。
        self.score(self.alice, 0, 40)
        self.score(self.bob, 1, 25)
        gameserver.Conn.on_game_packet(self.alice, OP_END_QUEST, b"")
        win_exp, win_money = gameserver.pvp_reward(40, True)
        lose_exp, lose_money = gameserver.pvp_reward(25, False)
        self.assertEqual(200 + win_exp, self.accounts.saved["alice"]["experience"])
        self.assertEqual(400 + lose_exp, self.accounts.saved["bob"]["experience"])
        self.assertEqual(10 + win_money, self.accounts.saved["alice"]["money"])
        self.assertEqual(20 + lose_money, self.accounts.saved["bob"]["money"])
        self.assertNotEqual(win_exp, win_money)
        # 输了也有底薪，不会一分不给。
        self.assertGreater(lose_exp, 0)
        self.assertGreater(lose_money, 0)


class TeamDeathmatchSettlementTests(BattleRoom):
    """★★ 组队 + 夺分：**赢的那一队整队都「胜利」**（§226 / D147）。

    用户 2026-08-20 实机报：组队夺分打完，结算界面上只有得分最高的**那一个人**
    写着「胜利」，同队队友全是「败北」。根因是 `RoomQuest.ranking()` 连
    `teams` 参数都没有 —— 生存那一路早就按队伍判了，夺分这一路漏了。

    照抄的是客户端 `DeathMatchVictoryCondition` 虚表槽 14（`0x55bfda`）的
    组队分支：比两队的合成分，高的整队 +1、低的整队 -1、平了谁都不判。
    """

    session_type = 1
    arguments = (1, 3, 0)       # 组队战 + 夺分
    #: ★ 三个人才造得出「个人最高分在人少的那一队」的局面 —— 两个人的话
    #: 队伍总分恒等于个人分，根本区分不出这两套口径。
    extra_players = ("carol",)

    def score(self, conn, seat, value):
        conn.my_seat = seat
        gameserver.Conn.on_game_packet(conn, OP_UPDATE_QUEST_SCORE,
                                       w_i32(value))

    def seat_teams(self, teams):
        """直接摆队伍号（默认按座位奇偶分，这里要能造出 2v1 之类的局面）。"""
        for seat, team in teams.items():
            self.room.seats[seat].team = team

    def tail(self):
        gameserver.Conn.on_game_packet(self.alice, OP_END_QUEST, b"")
        return result_tail(bodies(self.alice, OP_REP_GAME_RESULT)[0])

    def test_the_room_really_is_in_team_layout(self):
        # 这一组用例的前提。分队口径不对的话下面全是空转。
        self.assertEqual(TEAM_LAYOUT_TEAMS, self.room.team_layout())

    def test_the_whole_winning_team_is_marked_as_a_winner(self):
        # A 队 = 座位 0 + 2，B 队 = 座位 1。个人最高分（9）在 B 队，
        # 但 A 队总分 5 + 6 = 11 更高 —— 判的是**队伍总分**。
        self.seat_teams({0: TEAM_A, 1: TEAM_B, 2: TEAM_A})
        self.score(self.alice, 0, 5)
        self.score(self.bob, 1, 9)
        self.score(self.carol, 2, 6)
        self.assertEqual([GAME_RESULT_CLEARED, GAME_RESULT_DEFEATED,
                          GAME_RESULT_CLEARED, 0, 0, 0], self.tail())

    def test_the_top_individual_does_not_carry_a_losing_team(self):
        # 反过来：alice 个人分最高（9），可她那一队只有她一个人，
        # B 队 5 + 6 = 11 更高，于是**她输**。这一条正是和「最高分者所在队
        # 获胜」那套口径分道扬镳的地方。
        self.seat_teams({0: TEAM_A, 1: TEAM_B, 2: TEAM_B})
        self.score(self.alice, 0, 9)
        self.score(self.bob, 1, 5)
        self.score(self.carol, 2, 6)
        self.assertEqual([GAME_RESULT_DEFEATED, GAME_RESULT_CLEARED,
                          GAME_RESULT_CLEARED, 0, 0, 0], self.tail())

    def test_a_team_draw_judges_nobody(self):
        self.seat_teams({0: TEAM_A, 1: TEAM_B, 2: TEAM_B})
        self.score(self.alice, 0, 7)
        self.score(self.bob, 1, 4)
        self.score(self.carol, 2, 3)
        self.assertEqual([0] * GAME_RESULT_TAIL_COUNT, self.tail())

    def test_fewer_team_deaths_breaks_a_team_draw(self):
        self.seat_teams({0: TEAM_A, 1: TEAM_B, 2: TEAM_B})
        self.score(self.alice, 0, 7)
        self.score(self.bob, 1, 4)
        self.score(self.carol, 2, 3)
        self.alice.quest_state().deaths[1] = 3
        self.assertEqual([GAME_RESULT_CLEARED, GAME_RESULT_DEFEATED,
                          GAME_RESULT_DEFEATED, 0, 0, 0], self.tail())

    def test_everyone_on_the_only_team_wins(self):
        # `0x55c594`：在座的人全同队 -> 没有对手，全员胜。
        self.seat_teams({0: TEAM_A, 1: TEAM_A, 2: TEAM_A})
        self.score(self.alice, 0, 7)
        self.score(self.bob, 1, 2)
        self.score(self.carol, 2, 0)
        self.assertEqual([GAME_RESULT_CLEARED] * 3 + [0] * 3, self.tail())

    def test_the_end_game_success_flag_follows_the_team_verdict(self):
        # `0x0411` 的 success 跟着尾数组走，两个包不能自相矛盾 ——
        # 否则队友那份写「胜利」标签、却放失败曲。
        self.seat_teams({0: TEAM_A, 1: TEAM_B, 2: TEAM_A})
        self.score(self.alice, 0, 9)
        self.score(self.bob, 1, 2)
        self.score(self.carol, 2, 0)
        gameserver.Conn.on_game_packet(self.alice, OP_END_QUEST, b"")
        flags = {end_game_seat(b): end_game_success(b)
                 for b in bodies(self.alice, OP_END_GAME)}
        self.assertEqual({0: True, 1: False, 2: True}, flags)


class PvpFinishTests(BattleRoom):
    """★ 对战必须由**服务端**判胜负并结算（§167）。

    用户 2026-08-12 实机报的：「对战模式分出胜负后无法退出返回房间，
    胜利的人还可以动，死的人无法复活，倒计时结束也不退出」。
    根因：客户端自带的结束链 `0x4a3cf7` 第一行就是 `cmp [this+0x3b0], 2`，
    而那个状态只有剧本关才会进 —— 对战地图里它永远是 1，
    所以整局**一发 `0x040f` 都不会发**（实机日志逐包对过）。
    """

    session_type = 1
    arguments = (0, 3, 0)

    def kill(self, killer_seat, victim_seat, deaths=0):
        """让 `killer_seat` 打死 `victim_seat` 一次。

        `0x0408` 里的「凶手」字段就是开火者的座位号（`[char+0x158]`，
        由 `0x4fedee` 写），服务端的对战计分靠它。

        ★ 发这一包的必须是**受害者本人**（bug调查/8）：玩家的死亡只认本人
        上报，凶手那一格也随之改由受害者本机提供 —— 计分反而更准了。
        """
        gameserver.Conn.on_game_packet(
            (self.alice, self.bob)[victim_seat], OP_REPORT_HP_ZERO,
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

    def test_a_suicide_costs_the_killer_a_point(self):
        """★ 自杀要**扣**一分，不是「不记分」（§224）。

        客户端 `Character::Die` 在凶手座位 == 受害者座位时走
        `Character::OnSuicide`（`0x506eba`）：`[char+0x604]--`（HUD 上那个
        杀敌数）＋ `AddScore(座位, -1)`（夺分胜负线读的那一格）。
        """
        self.kill(0, 1, deaths=0)
        self.kill(0, 1, deaths=1)
        self.assertEqual(2, self.quest.kills[0])
        self.kill(0, 0)                     # 自己把自己炸死
        self.assertEqual(1, self.quest.kills[0])

    def test_a_suicide_never_pushes_the_score_below_zero(self):
        # `0x506eba` 开头 `test ecx,ecx / jle` —— 已经是 0 就整个函数不做事。
        self.kill(0, 0)
        self.assertEqual([0] * 6, self.quest.kills)

    def test_a_free_for_all_kill_is_never_treated_as_a_team_kill(self):
        """★ 个人战里人人的队伍号都是 0，**不许**因此被当成杀队友。

        客户端 `0x500165` 先问 `0x409df1(描述符+0x18) == 1`，不是组队战
        就直接走正常加分那一路，一格队伍号都不读。
        """
        room = gameserver.Conn.lobby_room(self.alice)
        self.assertEqual(room.seats[0].team, room.seats[1].team)
        self.kill(0, 1)
        self.assertEqual(1, self.quest.kills[0])

    def test_a_kill_by_someone_who_already_left_scores_nothing(self):
        """凶手已经退房 —— 客户端 `0x404ff6` 查不到角色，两边都不该动分。"""
        room = gameserver.Conn.lobby_room(self.alice)
        self.quest.kills[0] = 2
        room.seats[0] = None
        self.assertEqual(0, self.quest.record_kill(
            0, 1, teams={1: 0}, team_mode=False))
        self.assertEqual(2, self.quest.kills[0])

    def test_a_suicide_delays_the_end_by_one_kill(self):
        """★ bug调查/10 的回归：HUD 写着 5，服务端却已经数到 6 就结算了。

        2 人个人战的上限是 4。打死对手 3 次（HUD 3）之后自杀一次
        （HUD 2），得再打死两次才到 4 —— 服务端不扣那一分的话，
        第 4 次死亡就会在玩家看到「3」的时候提前结算。
        """
        for i in range(3):
            self.kill(0, 1, deaths=i)
        self.kill(0, 0)
        self.assertEqual(2, self.quest.kills[0])
        self.kill(0, 1, deaths=3)
        self.assertEqual(3, self.quest.kills[0])
        self.assertFalse(self.quest.settled, "扣掉那一分就不该在这里结算")
        self.kill(0, 1, deaths=4)
        self.assertEqual(4, self.quest.kills[0])
        self.assertTrue(self.quest.settled)

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

    def test_free_for_all_never_ends_on_teams(self):
        """★ 个人战判「只剩一边」**只看在座人数**，不看队伍号。

        客户端 `0x55c594` 开头就是 `0x409df1(描述符) == 1` 不成立直接跳到
        非组队分支，一格队伍号都不读。个人战现在人人发 0（越界修复，
        见 `lobby.TEAM_LAYOUT_*`），要是还按 `sides` 判就会一开局判结束。
        """
        quest = gameserver.RoomQuest()
        seats = [0, 1, 2]
        teams = {0: 0, 1: 0, 2: 0}          # 个人战：人人「没分队」
        self.assertIsNone(quest.pvp_finished(seats, teams, 99,
                                             team_mode=False))
        # 掉到一个人才算完
        self.assertEqual("只剩一边了",
                         quest.pvp_finished([1], teams, 99, team_mode=False))

    def test_team_mode_still_ends_when_one_side_is_left(self):
        quest = gameserver.RoomQuest()
        self.assertEqual(
            "只剩一边了",
            quest.pvp_finished([0, 2], {0: 1, 2: 1}, 99, team_mode=True))
        self.assertIsNone(
            quest.pvp_finished([0, 1], {0: 1, 1: 2}, 99, team_mode=True))

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


class PvpScoreLimitFrozenTests(BattleRoom):
    """★ 夺分的胜利线按**开局那一刻**的人数定死，中途掉线不重算（§220 / D139）。

    用户 2026-08-19 实机报的：三人个人战本来「杀 6 个赢」，打到一半掉线
    一个，服务端就改按 4 个结算了 —— 可客户端右上角那个「MAX 6」纹丝不动。
    根因是客户端的 `DeathMatchVictoryCondition` 只在建关卡时造一次，
    分数线写进 `[victory+0x198]` 之后全镜像里再没有第二处写它，
    **没有任何包能让那个数字变**。所以要对齐只能是服务端跟着冻结。
    """

    session_type = 1
    arguments = (0, 3, 0)       # 个人战 + 夺分模式 + 普通模式

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

    def kill(self, killer_seat, victim_seat, deaths=0):
        """`killer_seat` 打死 `victim_seat` 一次（由受害者本人上报，bug调查/8）。"""
        victim = (self.alice, self.bob, self.carol)[victim_seat]
        gameserver.Conn.on_game_packet(
            victim, OP_REPORT_HP_ZERO,
            hp_zero_payload(handle=victim_seat * 100000 + 100001,
                            seat=victim_seat, arg=killer_seat, deaths=deaths))

    def test_the_kickoff_seats_are_remembered(self):
        self.assertEqual([0, 1, 2], self.quest.start_seats)
        self.assertEqual(6, self.quest.score_limit([0, 1, 2], False))

    def test_a_disconnect_does_not_lower_the_score_limit(self):
        gameserver.Conn.on_game_packet(self.carol, OP_LEAVE_SESSION, b"")
        self.assertEqual([0, 1], gameserver.Conn.battle_seats(self.alice))
        # 三人份的 6 分，不是剩下两人份的 4 分。
        self.assertEqual(6, self.quest.score_limit([0, 1], False))
        for i in range(5):
            self.kill(0, 1, deaths=i)
        self.assertFalse(self.quest.settled,
                         "掉线一个就按 4 分结算 = 用户报的那个 bug")
        self.kill(0, 1, deaths=5)
        self.assertTrue(self.quest.settled)
        self.assertIn("达到上限 6", self.quest.pvp_reason)

    def test_the_last_one_standing_still_ends_the_round(self):
        # 冻的只是分数线；「只剩一边了」要的就是**现在**还剩几个人。
        gameserver.Conn.on_game_packet(self.carol, OP_LEAVE_SESSION, b"")
        self.assertFalse(self.quest.settled)
        gameserver.Conn.on_game_packet(self.bob, OP_LEAVE_SESSION, b"")
        self.assertTrue(self.quest.settled)
        self.assertEqual("只剩一边了", self.quest.pvp_reason)

    def test_the_next_round_uses_the_new_player_count(self):
        # 冻结只管这一局：走的人没回来，下一局客户端自己也只按 2 人建
        # 胜负条件（那时它才重新造 GameContextQuest），两边一起变成 4 分。
        gameserver.Conn.on_game_packet(self.carol, OP_LEAVE_SESSION, b"")
        for i in range(6):
            self.kill(0, 1, deaths=i)
        self.assertTrue(self.quest.settled)
        for conn in (self.alice, self.bob):
            gameserver.Conn.leave_game_result(conn)
        self.clear()
        gameserver.Conn.on_game_packet(self.alice, OP_COUNT_GAME_READY, b"")
        gameserver.Conn.on_game_packet(self.alice, OP_COUNT_GAME_READY, b"")
        for conn in (self.alice, self.bob):
            gameserver.Conn.on_game_packet(conn, OP_LOADING_DONE, b"")
        self.assertEqual([0, 1], self.quest.start_seats)
        self.assertEqual(4, self.quest.score_limit([0, 1], False))
        for i in range(4):
            self.kill(0, 1, deaths=i)
        self.assertIn("达到上限 4", self.quest.pvp_reason)

    def test_a_questless_room_falls_back_to_the_live_count(self):
        # 协议试探 / 控制通道手搓包建出来的那份没有开局快照，
        # 按现在的人数算 —— 老行为一个字节不变。
        self.assertEqual([], gameserver.RoomQuest().start_seats)
        self.assertEqual(4, gameserver.RoomQuest().score_limit([0, 1], False))
        self.assertEqual(6, gameserver.RoomQuest().score_limit([0, 1, 2], False))


class PvpTeamKillTests(BattleRoom):
    """★ 组队战里杀队友和自杀一样**扣一分**（§224）。

    客户端 `Character::Die`（`0x4ffbb7`）在 `0x500165` 先问
    `0x409df1(描述符+0x18) == 1`（是不是组队战），再比双方的队伍号
    （`0x40462c` 读 `[desc + 座位*0x3c + 0x48]`）—— 同队就走
    `Character::OnSuicide`，和自己把自己炸死走的是同一个 -1。
    """

    session_type = 1
    arguments = (1, 3, 0)       # 组队战 + 夺分模式 + 普通模式

    def kill(self, killer_seat, victim_seat, deaths=0):
        gameserver.Conn.on_game_packet(
            (self.alice, self.bob)[victim_seat], OP_REPORT_HP_ZERO,
            hp_zero_payload(handle=victim_seat * 100000 + 100001,
                            seat=victim_seat, arg=killer_seat, deaths=deaths))

    def test_the_room_really_is_in_team_mode(self):
        room = gameserver.Conn.lobby_room(self.alice)
        self.assertEqual(TEAM_LAYOUT_TEAMS, room.team_layout())
        self.assertNotEqual(room.seats[0].team, room.seats[1].team)

    def test_killing_an_opponent_still_scores(self):
        self.kill(0, 1)
        self.assertEqual(1, self.quest.kills[0])

    def test_killing_a_team_mate_costs_a_point(self):
        room = gameserver.Conn.lobby_room(self.alice)
        self.quest.kills[0] = 2
        room.seats[1].team = room.seats[0].team
        self.kill(0, 1)
        self.assertEqual(1, self.quest.kills[0])

    def test_a_team_kill_never_pushes_the_score_below_zero(self):
        room = gameserver.Conn.lobby_room(self.alice)
        room.seats[1].team = room.seats[0].team
        self.kill(0, 1)
        self.assertEqual([0] * 6, self.quest.kills)


class SurvivalFinishTests(BattleRoom):
    """生存模式（arguments[1] == 0）：每人固定三条命。"""

    session_type = 1
    arguments = (1, 0, 0)       # 组队战 + 生存模式 + 普通模式

    def die(self, victim_seat, deaths):
        """环境击杀，不给任何座位加杀敌分，避免误靠夺分规则结算。"""
        gameserver.Conn.on_game_packet(
            self.alice, OP_REPORT_HP_ZERO,
            hp_zero_payload(handle=victim_seat * 100000 + 100001,
                            seat=victim_seat, arg=0xFF, deaths=deaths))

    def test_two_deaths_do_not_end_the_round(self):
        self.die(0, 0)
        self.die(0, 1)
        self.assertFalse(self.quest.settled)
        self.assertIsNone(self.quest.pvp_reason)

    def test_the_third_death_ends_the_round_and_the_other_team_wins(self):
        for deaths in range(3):
            self.die(0, deaths)

        self.assertTrue(self.quest.settled)
        self.assertIn("生命都用完", self.quest.pvp_reason)
        self.assertEqual([0] * 6, self.quest.kills,
                         "这条回归必须证明不是误靠夺分规则结束")
        expected = [GAME_RESULT_DEFEATED, GAME_RESULT_CLEARED] + [0] * 4
        for conn in (self.alice, self.bob):
            self.assertIn(OP_END_GAME, opcodes(conn))
            body = bodies(conn, OP_REP_GAME_RESULT)[0]
            self.assertEqual(expected, result_tail(body))

    def test_kill_score_limit_is_ignored_in_survival_mode(self):
        self.quest.kills[0] = gameserver.pvp_score_limit(2, True)
        self.assertFalse(gameserver.Conn.check_pvp_finished(self.alice))
        self.assertFalse(self.quest.settled)


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

    def test_team_survival_waits_until_every_member_is_out_of_lives(self):
        quest = RoomQuest()
        seats = [0, 1, 2, 3]
        teams = {0: 1, 1: 2, 2: 1, 3: 2}
        quest.deaths[0] = gameserver.PVP_SURVIVAL_LIVES
        quest.deaths[2] = gameserver.PVP_SURVIVAL_LIVES - 1
        self.assertIsNone(quest.survival_finished(
            seats, teams, team_mode=True, now=quest.started_at))

        quest.deaths[2] += 1
        reason = quest.survival_finished(
            seats, teams, team_mode=True, now=quest.started_at)
        self.assertIn("队伍 1", reason)
        self.assertIn("生命都用完", reason)
        self.assertEqual([-1, 1, -1, 1, 0, 0],
                         quest.survival_ranking(
                             seats, teams, team_mode=True))

    def test_free_survival_ends_when_only_one_player_has_lives(self):
        quest = RoomQuest()
        quest.deaths[0] = gameserver.PVP_SURVIVAL_LIVES
        self.assertIsNotNone(quest.survival_finished(
            [0, 1], {0: 1, 1: 2}, team_mode=False,
            now=quest.started_at))

    def test_survival_uses_the_death_count_that_was_broadcast(self):
        # ★ bug调查/8：下发值 = 服务端权威计数，所以要真死三次才没命。
        #   （以前直接信客户端报的「之前死过几次」，一发就能把命清零。）
        quest = RoomQuest()
        for expected in (1, 2, 3):
            deaths, first = quest.record_death(100001, 0, expected - 1)
            self.assertTrue(first)
            self.assertEqual(expected, deaths)
        self.assertEqual(0, quest.remaining_lives(0))

    def test_a_monster_reported_twice_with_different_counts_is_eaten(self):
        # ★ bug调查/8 实测：同一只怪 76 毫秒内被两台机器报了 count=0 和 count=1
        #   （句柄按控制者座位分段复用，`[obj+0x600]` 跨对象残留就差 1），
        #   `(句柄, 次数)` 那把键当场失效 —— 时间窗才拦得住。
        quest = RoomQuest()
        self.assertEqual((1, True), quest.record_death(0x201, 0xFF, 0, now=100.0))
        self.assertEqual((1, False), quest.record_death(0x201, 0xFF, 1, now=100.076))
        # 窗过了才算真的又死一次。
        self.assertEqual((2, True), quest.record_death(0x201, 0xFF, 1, now=110.0))

    def test_the_dedup_window_does_not_apply_to_players(self):
        # 玩家只有本人会上报，一次死亡天然只有一发；给玩家也加窗只会平白
        # 吃掉真死亡（服务端补重生之后 3 秒内又死是完全可能的）。
        quest = RoomQuest()
        self.assertEqual((1, True), quest.record_death(100001, 0, 0, now=100.0))
        self.assertEqual((2, True), quest.record_death(100001, 0, 1, now=100.5))

    def test_claim_item_is_first_come_first_served(self):
        quest = RoomQuest()
        self.assertTrue(quest.claim_item(0x40000000, 0))
        self.assertFalse(quest.claim_item(0x40000000, 1))

    def test_item_handles_never_repeat(self):
        quest = RoomQuest()
        handles = [quest.allocate_item() for _ in range(5)]
        self.assertEqual(len(handles), len(set(handles)))

    # -- 道具槽（§194）------------------------------------------------------
    def test_the_slot_wire_format_is_one_int32(self):
        # ⚠ §193 初记的「u16」是错的：两个处理器读字段用的是 `0x5d5984`
        #   = `Read(&buf, 4)`。u16 那个原语是 `0x5d5942`。
        self.assertEqual(b"\x63\x28\x00\x00",
                         gameserver.build_grant_item(10339))
        self.assertEqual(b"\x02\x00\x00\x00", gameserver.build_use_item(2))
        self.assertEqual(2, gameserver.parse_use_item(b"\x02\x00\x00\x00"))

    def test_parse_use_item_rejects_a_short_payload(self):
        with self.assertRaises(ValueError):
            gameserver.parse_use_item(b"\x00\x00")

    def test_the_effect_wire_format_puts_the_item_id_third(self):
        # 处理器 `0x551d95` 的 push 顺序是 F1 / F3 / F2 ->
        # `UseItemEffect(F2, F3, F1)`，所以**道具 id 落在第 3 个字段**。
        # 抄的是客户端自己那条 `PvpItem::vf_11c`：`(id, 0, -1)`。
        self.assertEqual(
            struct.pack("<iiii", 4, -1, 10300, 0),
            gameserver.build_item_effect(4, 10300))

    def test_remember_item_is_what_makes_a_pickup_grantable(self):
        quest = RoomQuest()
        self.assertIsNone(quest.item_id_of(0x40000000))
        quest.remember_item(0x40000000, 10301)
        self.assertEqual(10301, quest.item_id_of(0x40000000))

    def test_grant_item_stops_at_four(self):
        quest = RoomQuest()
        for i in range(gameserver.ITEM_SLOT_COUNT):
            self.assertTrue(quest.grant_item(0, 10300 + i))
        self.assertFalse(quest.grant_item(0, 10307),
                         "客户端 AddItem 满了就什么都不做，镜像必须跟着停")
        self.assertEqual(gameserver.ITEM_SLOT_COUNT,
                         len(quest.item_slots[0]))

    def test_grant_item_ignores_seats_out_of_range(self):
        quest = RoomQuest()
        self.assertFalse(quest.grant_item(-1, 10300))
        self.assertFalse(quest.grant_item(99, 10300))

    def test_use_item_pops_and_shifts(self):
        quest = RoomQuest()
        for item_id in (10300, 10301, 10302):
            quest.grant_item(1, item_id)
        self.assertEqual(10301, quest.use_item(1, 1))
        self.assertEqual([10300, 10302], quest.item_slots[1])
        # 挪完之后「下一件」永远在第 0 格 —— 客户端也正是恒发 0。
        self.assertEqual(10300, quest.use_item(1, 0))
        self.assertEqual([10302], quest.item_slots[1])

    def test_use_item_on_an_empty_slot_is_none(self):
        quest = RoomQuest()
        self.assertIsNone(quest.use_item(0, 0))
        quest.grant_item(0, 10300)
        self.assertIsNone(quest.use_item(0, 1))
        self.assertIsNone(quest.use_item(9, 0))
        self.assertIsNone(quest.use_item(0, -1))

    def test_every_spawnable_item_is_grantable(self):
        # 服务端刷什么就得能进槽 —— 刷了一件进不了槽的东西，
        # 玩家看到的又是「捡了没用」。
        # ⚠ 三把特殊武器是例外（拾取当场换枪，见下一条）。
        for item_id in gameserver.PVP_ITEM_IDS + gameserver.PVP_TEAM_ITEM_IDS:
            self.assertIn(item_id, gameserver.GRANTABLE_ITEM_IDS)

    def test_quest_drops_are_not_grantable(self):
        # 金币 / 红心 / 武器拾取当场生效（`[item+0x2a9] == 0`），不进槽。
        for item_id in (10000, 10001, 10100, 10101, 10102, 10103,
                        10200, 10201, 10202, 10603):
            self.assertNotIn(item_id, gameserver.GRANTABLE_ITEM_IDS)

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
