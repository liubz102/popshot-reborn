#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`server/lobby.py` 的测试 —— 纯模型，不碰 socket。

这里只测「房间表本身对不对」。组包 / 广播时序在 `test_gameserver.py`。
"""
import os
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from lobby import (                                            # noqa: E402
    Lobby, Room, Seat, ROOM_SEAT_COUNT,
    SESSION_STATUS_WAITING, SESSION_STATUS_PLAYING,
    MOVE_INTO_OK, MOVE_INTO_ALREADY_PLAYING, MOVE_INTO_FULL,
    MOVE_INTO_NO_SUCH_ROOM, MOVE_INTO_BAD_PASSWORD, item_mode_of,
)


class FakeConn:
    """只用来当身份标识 —— 大厅模型对连接一无所求。"""

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return f"<conn {self.name}>"


def seat_for(conn, nickname=None, level=1, character_id=0):
    return Seat(conn, username=conn.name, nickname=nickname or conn.name,
                level=level, character_id=character_id)


class CreateRoomTests(unittest.TestCase):
    def setUp(self):
        self.lobby = Lobby()
        self.alice = FakeConn("alice")

    def test_first_room_gets_id_zero_and_host_seat_zero(self):
        # 客户端显示的是 room_id + 1（'%d번'，§138），所以从 0 开始分配，
        # 玩家看到的第一个房间就是「1번」。
        room = self.lobby.create_room(self.alice, title="来玩", session_type=2,
                                      arguments=(3, 1))
        self.assertEqual(0, room.room_id)
        self.assertEqual(0, room.host_seat)
        self.assertIs(self.alice, room.seats[0].conn)
        self.assertEqual(1, room.player_count())
        self.assertEqual(SESSION_STATUS_WAITING, room.status)

    def test_room_ids_keep_increasing_even_after_a_room_closes(self):
        first = self.lobby.create_room(self.alice)
        self.lobby.leave(self.alice)
        second = self.lobby.create_room(self.alice)
        # 号不复用：复用的话「刚退掉的房间号」会立刻指向另一个房间，
        # 慢一拍的加入请求就会进错屋。
        self.assertEqual(1, second.room_id)
        self.assertNotEqual(first.room_id, second.room_id)

    def test_creating_a_second_room_leaves_the_first(self):
        first = self.lobby.create_room(self.alice)
        second = self.lobby.create_room(self.alice)
        self.assertIs(second, self.lobby.room_of(self.alice))
        self.assertIsNone(self.lobby.get(first.room_id))

    def test_seat_snapshots_are_six_long_and_mark_empties(self):
        room = self.lobby.create_room(self.alice, seat=seat_for(self.alice))
        snaps = room.seat_snapshots()
        self.assertEqual(ROOM_SEAT_COUNT, len(snaps))
        self.assertTrue(snaps[0]["occupied"])
        self.assertEqual("alice", snaps[0]["nickname"])
        self.assertEqual([{"occupied": False}] * 5, snaps[1:])

    def test_game_type_follows_session_type(self):
        self.assertEqual(2, self.lobby.create_room(self.alice,
                                                   session_type=2).game_type)
        self.assertEqual(1, self.lobby.create_room(self.alice,
                                                   session_type=1).game_type)
        self.assertEqual(5, self.lobby.create_room(self.alice,
                                                   session_type=5).game_type)


class JoinTests(unittest.TestCase):
    def setUp(self):
        self.lobby = Lobby()
        self.alice = FakeConn("alice")
        self.bob = FakeConn("bob")
        self.room = self.lobby.create_room(self.alice, title="来玩",
                                           session_type=2, arguments=(3, 1),
                                           seat=seat_for(self.alice))

    def test_join_takes_the_lowest_free_seat(self):
        result, room, index = self.lobby.join(self.bob, self.room.room_id,
                                              seat=seat_for(self.bob))
        self.assertEqual(MOVE_INTO_OK, result)
        self.assertIs(self.room, room)
        self.assertEqual(1, index)
        self.assertEqual(2, room.player_count())
        self.assertEqual(0, room.host_seat)

    def test_join_unknown_room_says_no_such_room(self):
        result, room, index = self.lobby.join(self.bob, 999)
        self.assertEqual((MOVE_INTO_NO_SUCH_ROOM, None, None),
                         (result, room, index))

    def test_join_playing_room_says_already_playing(self):
        self.lobby.update_room(self.room, status=SESSION_STATUS_PLAYING)
        result, _, _ = self.lobby.join(self.bob, self.room.room_id)
        self.assertEqual(MOVE_INTO_ALREADY_PLAYING, result)

    def test_join_full_room_says_full(self):
        for i in range(ROOM_SEAT_COUNT - 1):
            other = FakeConn(f"p{i}")
            self.assertEqual(MOVE_INTO_OK,
                             self.lobby.join(other, self.room.room_id,
                                             seat=seat_for(other))[0])
        self.assertTrue(self.room.is_full())
        result, _, _ = self.lobby.join(self.bob, self.room.room_id)
        self.assertEqual(MOVE_INTO_FULL, result)

    def test_wrong_password_says_bad_password(self):
        self.lobby.update_room(self.room, password="1234")
        self.assertEqual(MOVE_INTO_BAD_PASSWORD,
                         self.lobby.join(self.bob, self.room.room_id, "9999")[0])
        self.assertEqual(MOVE_INTO_OK,
                         self.lobby.join(self.bob, self.room.room_id, "1234",
                                         seat=seat_for(self.bob))[0])

    def test_password_beats_full_when_both_are_wrong(self):
        # 两个原因同时成立时报哪一个是有意选的（见 lobby.join 的注释）：
        # 玩家刚输完密码，先告诉他密码不对最有用。
        self.lobby.update_room(self.room, password="1234")
        for i in range(ROOM_SEAT_COUNT - 1):
            other = FakeConn(f"p{i}")
            self.lobby.join(other, self.room.room_id, "1234",
                            seat=seat_for(other))
        self.assertEqual(MOVE_INTO_BAD_PASSWORD,
                         self.lobby.join(self.bob, self.room.room_id, "x")[0])

    def test_joining_a_room_you_are_already_in_is_a_no_op_success(self):
        self.lobby.join(self.bob, self.room.room_id, seat=seat_for(self.bob))
        result, room, index = self.lobby.join(self.bob, self.room.room_id)
        self.assertEqual((MOVE_INTO_OK, 1), (result, index))
        self.assertEqual(2, room.player_count())

    def test_joining_another_room_leaves_the_previous_one(self):
        second = self.lobby.create_room(self.bob, seat=seat_for(self.bob))
        self.lobby.join(self.bob, self.room.room_id, seat=seat_for(self.bob))
        self.assertIs(self.room, self.lobby.room_of(self.bob))
        self.assertIsNone(self.lobby.get(second.room_id))


class QuickJoinTests(unittest.TestCase):
    def setUp(self):
        self.lobby = Lobby()
        self.alice = FakeConn("alice")
        self.bob = FakeConn("bob")

    def test_quick_join_picks_a_waiting_room_of_the_right_type(self):
        pvp = self.lobby.create_room(self.alice, session_type=1,
                                     seat=seat_for(self.alice))
        carol = FakeConn("carol")
        quest = self.lobby.create_room(carol, session_type=2,
                                       seat=seat_for(carol))
        result, room, index = self.lobby.quick_join(self.bob, game_type=2,
                                                    seat=seat_for(self.bob))
        self.assertEqual(MOVE_INTO_OK, result)
        self.assertIs(quest, room)
        self.assertEqual(1, index)
        self.assertNotEqual(pvp.room_id, room.room_id)

    def test_quick_join_skips_locked_full_and_playing_rooms(self):
        locked = self.lobby.create_room(self.alice, session_type=2,
                                        password="1", seat=seat_for(self.alice))
        result, room, _ = self.lobby.quick_join(self.bob, game_type=2)
        self.assertEqual((MOVE_INTO_NO_SUCH_ROOM, None), (result, room))
        self.assertEqual(1, locked.player_count())

    def test_quick_join_with_nothing_available_says_no_such_room(self):
        self.assertEqual(MOVE_INTO_NO_SUCH_ROOM,
                         self.lobby.quick_join(self.bob, game_type=2)[0])


class LeaveTests(unittest.TestCase):
    def setUp(self):
        self.lobby = Lobby()
        self.alice = FakeConn("alice")
        self.bob = FakeConn("bob")
        self.room = self.lobby.create_room(self.alice, seat=seat_for(self.alice))
        self.lobby.join(self.bob, self.room.room_id, seat=seat_for(self.bob))

    def test_leave_reports_who_is_left(self):
        result = self.lobby.leave(self.bob)
        self.assertEqual(1, result.seat_index)
        self.assertEqual([self.alice], result.remaining)
        self.assertIsNone(result.new_host_seat)
        self.assertFalse(result.closed)
        self.assertIsNone(self.lobby.room_of(self.bob))

    def test_host_leaving_transfers_to_the_lowest_remaining_seat(self):
        result = self.lobby.leave(self.alice)
        self.assertEqual(0, result.seat_index)
        self.assertEqual(1, result.new_host_seat)
        self.assertEqual(1, self.room.host_seat)
        self.assertIs(self.bob, self.room.host_conn)
        self.assertFalse(result.closed)

    def test_last_player_leaving_closes_the_room(self):
        self.lobby.leave(self.bob)
        result = self.lobby.leave(self.alice)
        self.assertTrue(result.closed)
        self.assertEqual([], result.remaining)
        self.assertIsNone(self.lobby.get(self.room.room_id))
        self.assertEqual([], self.lobby.rooms())

    def test_leaving_when_not_in_a_room_returns_none(self):
        self.assertIsNone(self.lobby.leave(FakeConn("nobody")))

    def test_a_room_with_connectionless_seats_is_not_closed(self):
        # 调试通道的 `fakeroom` 造的座位没有连接。「房间空了没有」必须按
        # **座位**判，按连接数判会把还坐着人的房间当空房解散掉。
        self.room.seats[3] = Seat(None, nickname="测试玩家")
        result = self.lobby.leave(self.bob)
        self.assertFalse(result.closed)
        result = self.lobby.leave(self.alice)
        self.assertFalse(result.closed)
        self.assertIsNotNone(self.lobby.get(self.room.room_id))
        self.assertEqual(1, self.room.player_count())

    def test_kick_removes_the_named_seat(self):
        result = self.lobby.kick(self.room, 1)
        self.assertEqual(1, result.seat_index)
        self.assertIsNone(self.lobby.room_of(self.bob))
        self.assertEqual(1, self.room.player_count())

    def test_kick_on_an_empty_seat_returns_none(self):
        self.assertIsNone(self.lobby.kick(self.room, 4))
        self.assertIsNone(self.lobby.kick(self.room, 99))


class ListTests(unittest.TestCase):
    def setUp(self):
        self.lobby = Lobby()

    def make(self, name, session_type):
        conn = FakeConn(name)
        return self.lobby.create_room(conn, title=name,
                                      session_type=session_type,
                                      seat=seat_for(conn))

    def test_rooms_are_sorted_by_id_and_filtered_by_game_type(self):
        pvp = self.make("pvp", 1)
        quest = self.make("quest", 2)
        quest2 = self.make("quest2", 2)
        self.assertEqual([pvp, quest, quest2], self.lobby.rooms())
        self.assertEqual([quest, quest2], self.lobby.rooms(game_type=2))
        self.assertEqual([pvp], self.lobby.rooms(game_type=1))
        self.assertEqual([], self.lobby.rooms(game_type=5))

    def test_empty_rooms_never_show_up(self):
        room = self.make("quest", 2)
        self.lobby.leave(room.seats[0].conn)
        self.assertEqual([], self.lobby.rooms())


class ConcurrencyTests(unittest.TestCase):
    """六个人同时抢一个房间，只能有六个座位，且没有两个人拿到同一个号。"""

    def test_parallel_joins_never_hand_out_the_same_seat(self):
        lobby = Lobby()
        host = FakeConn("host")
        room = lobby.create_room(host, seat=seat_for(host))
        results = []
        results_lock = threading.Lock()
        start = threading.Event()

        def worker(n):
            conn = FakeConn(f"p{n}")
            start.wait()
            outcome = lobby.join(conn, room.room_id, seat=seat_for(conn))
            with results_lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join()

        seats = sorted(i for r, _, i in results if r == MOVE_INTO_OK)
        self.assertEqual([1, 2, 3, 4, 5], seats)
        self.assertTrue(all(r == MOVE_INTO_FULL
                            for r, _, _ in results if r != MOVE_INTO_OK))
        self.assertEqual(ROOM_SEAT_COUNT, room.player_count())


class ItemModeTests(unittest.TestCase):
    """道具模式（아이템전）的判据，和客户端 `0x409dd9` 同一个口径（§190）。"""

    def test_the_third_argument_of_a_normal_room_is_the_switch(self):
        self.assertTrue(item_mode_of(1, (0, 0, 1)))
        self.assertFalse(item_mode_of(1, (0, 0, 0)))

    def test_the_team_flag_does_not_matter(self):
        self.assertTrue(item_mode_of(1, (1, 3, 1)))

    def test_only_a_normal_room_can_have_items(self):
        # 0x409dd9：type 5（天梯）恒返回 0，别的 type 恒返回 -1。
        for session_type in (0, 2, 3, 4, 5, 6):
            self.assertFalse(item_mode_of(session_type, (1, 1, 1)),
                             f"type {session_type} 不该有道具模式")

    def test_game_mode_two_forces_no_items(self):
        # 客户端 0x465be2 在这个模式下强制把道具标志清 0。
        self.assertFalse(item_mode_of(1, (0, 2, 1)))

    def test_a_short_descriptor_is_not_item_mode(self):
        self.assertFalse(item_mode_of(1, ()))
        self.assertFalse(item_mode_of(1, (0,)))
        self.assertFalse(item_mode_of(1, (0, 0)))

    def test_only_exactly_one_counts(self):
        # 客户端拿 `== 1` 判（0x48c794 / 0x499120），别的值一律不是道具模式。
        self.assertFalse(item_mode_of(1, (0, 0, -1)))
        self.assertFalse(item_mode_of(1, (0, 0, 2)))

    def test_the_room_follows_its_descriptor(self):
        room = Room(0, FakeConn("alice"), session_type=1, arguments=(0, 0, 1))
        self.assertTrue(room.item_mode())
        room.arguments = (0, 0, 0)
        self.assertFalse(room.item_mode())


if __name__ == "__main__":
    unittest.main()
