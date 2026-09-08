"""BSM1 contract: packet compatibility, real production wiring, constraint life."""
import struct
import unittest

import bot
import botmotion
import botmove
import botsync
import relayserver
import udpsync
from test_botsync import (FakeBot, BotFrameRoom, TerrainMixin, synth_terrain,
                          bot_frames)


class MotionMetadataTests(unittest.TestCase):
    def test_legacy_prefix_event_sequence_and_checksum(self):
        stream = botsync.BotSyncStream(FakeBot())
        state = botsync.character_state(100, 150, vx=8, vy=-22,
                                        on_ground=False, keys=botsync.KEY_RIGHT)
        legacy = stream.heartbeat(state)
        tagged = stream.heartbeat(state, motion=(7, 12, 30))
        self.assertEqual(43, len(legacy))
        self.assertEqual(59, len(tagged))
        self.assertEqual(legacy[12:], tagged[12:43])
        self.assertEqual(legacy[8:12], tagged[8:12])
        self.assertEqual(0, stream.events)
        self.assertEqual((7, 12, 30), botmotion.metadata(tagged))
        self.assertEqual(botsync.udp_checksum(tagged[12:]),
                         struct.unpack_from('<H', tagged, 6)[0])
        self.assertEqual(udpsync.heartbeat_motion(legacy), udpsync.heartbeat_motion(tagged))

    def test_human_cannot_forward_an_authority_marker(self):
        recipient = object()
        received = []
        relay = relayserver.RelayServer(members_of=lambda _: [recipient],
                                        fallback=lambda *args: received.append(args))
        packet = botsync.BotSyncStream(FakeBot()).heartbeat(
            botsync.character_state(0, 0), motion=(1, 1, 0))
        self.assertEqual(0, relay.deliver(object(), packet))
        self.assertEqual([], received)

    def test_projection_matches_native_integer_boundary(self):
        self.assertEqual(335.75, botmotion.constrained_x(310.75, 300.5, 1))
        self.assertEqual(335.75, botmotion.constrained_x(335.75, 300.5, 1))
        self.assertEqual(265.75, botmotion.constrained_x(320.75, 300.5, -1))


class MotionConstraintTests(TerrainMixin, BotFrameRoom):
    def setUp(self):
        super().setUp()
        self.terrain = synth_terrain('bsm_constraint')
        self.install_terrain(self.terrain)
        self.human_heartbeat(self.alice, 300, 150, ticks=0)
        self.bot_conn.body = botmove.Body(310.75, 150)
        self.bot_conn.battle_pos = (310.75, 150)

    def incoming(self, op, body, seq=1):
        packet = botsync.build_peer_packet(self.alice.my_seat, op, body,
                                           relayserver.epoch_state(self.alice).value,
                                           sequence=seq)
        bot.note_peer_hit(self.room, self.alice, packet)

    def bind(self):
        self.incoming(botsync.OP_DASH,
                      botsync.dash_body(self.alice.my_seat, 1, 0, 300, 150))
        self.incoming(0x17, struct.pack('<ii', botsync.character_handle(self.bot_seat),
                                      botsync.character_handle(self.alice.my_seat)), 2)

    def test_dependency_projects_body_and_expires_at_original_action_end(self):
        self.bind()
        self.assertIsNotNone(self.bot_conn.motion_constraint)
        action = self.alice.motion_action
        bot._apply_motion_constraint(self.room, self.bot_conn, self.terrain, action[1] - .001)
        self.assertEqual(335.75, self.bot_conn.body.x)
        self.assertEqual(150, self.bot_conn.body.y)
        bot._apply_motion_constraint(self.room, self.bot_conn, self.terrain, action[1])
        self.assertIsNone(self.bot_conn.motion_constraint)

    def test_duplicate_does_not_restart_action(self):
        self.bind()
        end = self.alice.motion_action[1]
        with bot._tick_clock(end + 100):
            self.incoming(botsync.OP_DASH,
                          botsync.dash_body(self.alice.my_seat, 1, 0, 300, 150))
        self.assertEqual(end, self.alice.motion_action[1])

    def test_old_action_does_not_restart_new_action(self):
        self.bind()
        packet = botsync.dash_body(self.alice.my_seat, 1, 0, 300, 150)
        self.incoming(botsync.OP_DASH, packet, 10)
        end = self.alice.motion_action[1]
        with bot._tick_clock(end + 100):
            self.incoming(botsync.OP_DASH, packet, 1)
        self.assertEqual(end, self.alice.motion_action[1])

    def test_current_facing_changes_the_constraint_boundary(self):
        self.bind()
        state = botsync.character_state(300, 150, facing=botsync.FACING_LEFT)
        self.incoming(botsync.OP_HEARTBEAT, botsync.heartbeat_body(3, self.alice.my_seat, state))
        bot._apply_motion_constraint(self.room, self.bot_conn, self.terrain,
                                     self.alice.motion_action[1] - .001)
        self.assertEqual(265.75, self.bot_conn.body.x)

    def test_owner_epoch_change_releases_dependency(self):
        self.bind()
        relayserver.epoch_state(self.alice).advance(relayserver.next_generation())
        bot._apply_motion_constraint(self.room, self.bot_conn, self.terrain, bot._now())
        self.assertIsNone(self.bot_conn.motion_constraint)

    def test_damage_releases_dependency(self):
        self.bind()
        bot._knock_back_seat(self.room, self.bot_seat, 20, (8, -8), source='test')
        self.assertIsNone(self.bot_conn.motion_constraint)

    def test_spawn_and_round_use_a_new_motion_identity(self):
        initial = self.bot_conn.motion_identity
        self.bot_conn.pending_spawn = (400, 150)
        self.advance_to_own_beat()
        packets = [p for p in bot_frames(self.alice, self.bot_seat) if udpsync.is_heartbeat(p)]
        self.assertTrue(packets)
        meta = botmotion.metadata(packets[-1])
        self.assertIsNotNone(meta)
        self.assertGreater(meta[0], initial)
        self.assertGreater(meta[1], 0)
        self.bot_conn.reset_battle_frame()
        self.assertGreater(self.bot_conn.motion_identity, meta[0])


if __name__ == '__main__':
    unittest.main()
