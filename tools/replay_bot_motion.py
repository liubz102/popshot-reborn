#!/usr/bin/env python3
r"""Replay original x86 heartbeat/jump code, without starting the game.

Requires Unicorn (developer tool only). Example in PowerShell:
  $env:PYTHONPATH='logs/investigation-deps'
  C:\Python314\python.exe tools/replay_bot_motion.py

The heartbeat decoder runs in full. Jump runs only its native velocity-setting
blocks, with an empty terrain query, a speed getter, and a sqrt shim. Rendering,
sound, animation, and collision are outside this minimal counterexample.
This is an offline diagnostic, not an implementation of the game's full physics.
"""
import hashlib
import argparse
import json
from pathlib import Path
import struct
import sys

from unicorn import Uc, UC_ARCH_X86, UC_MODE_32, UC_HOOK_CODE
from unicorn.x86_const import (
    UC_X86_REG_EAX, UC_X86_REG_EBP, UC_X86_REG_ECX, UC_X86_REG_EDI,
    UC_X86_REG_EDX, UC_X86_REG_EIP, UC_X86_REG_ESI, UC_X86_REG_ESP, UC_X86_REG_FPCW,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
import botsync

IMAGE_SHA256 = '81e66c3eeb193560da2b59cb0924ba0d725e2ad405435f1b6389511af1fa8d1b'
OBJ, STACK, VTABLE, STUB, END = 0x1000000, 0x101f000, 0x1020000, 0x1021000, 0x1022000


class NativeCharacter:
    def __init__(self):
        data = (ROOT / 're/BigShot_22524.img').read_bytes()
        if hashlib.sha256(data).hexdigest() != IMAGE_SHA256:
            raise ValueError('This diagnostic is pinned to BigShot_22524.img')
        self.uc = Uc(UC_ARCH_X86, UC_MODE_32)
        self.uc.mem_map(0x400000, (len(data) + 4095) & ~4095)
        self.uc.mem_write(0x400000, data)
        self.uc.mem_map(OBJ, 0x30000)
        self.put(OBJ, '<I', VTABLE)
        self.put(VTABLE + 0x128, '<I', STUB)
        # Test fixture for the character's virtual speed getter: fld 7.0; ret.
        self.uc.mem_write(STUB, b'\xd9\x05' + struct.pack('<I', STUB + 0x100) + b'\xc3')
        self.put(STUB + 0x100, '<f', 7.0)
        # Only the CRT sqrt dependency is replaced; native jump arithmetic stays.
        self.uc.mem_write(0x5f5fe4, b'\xdd\x44\x24\x04\xd9\xfa\xc3')
        self.put(0x72e2dc, '<I', 0)  # 0x40a04f uses the original default gravity.
        self.uc.reg_write(UC_X86_REG_FPCW, 0x37f)
        self.payload = None
        self.uc.hook_add(UC_HOOK_CODE, self._read_state,
                         begin=0x5d59c1, end=0x5d59c1)
        self.place(400, 150, 10.5, 0.4, False)

    def put(self, address, fmt, *values):
        self.uc.mem_write(address, struct.pack(fmt, *values))

    def get(self, address, fmt):
        return struct.unpack(fmt, self.uc.mem_read(address, struct.calcsize(fmt)))[0]

    def _read_state(self, uc, address, size, _):
        esp = uc.reg_read(UC_X86_REG_ESP)
        ret, dest, count = struct.unpack('<III', uc.mem_read(esp, 12))
        assert count == 24 and len(self.payload) == 24
        uc.mem_write(dest, self.payload)
        uc.reg_write(UC_X86_REG_ESP, esp + 12)
        uc.reg_write(UC_X86_REG_EIP, ret)

    def place(self, x, y, vx, vy, ground):
        for offset, value in ((0x34, x), (0x38, y), (0x120, vx), (0x124, vy)):
            self.put(OBJ + offset, '<f', value)
        self.put(OBJ + 0x128, '<B', int(ground))

    def state(self):
        return {name: round(self.get(OBJ + off, '<f'), 5) for name, off in
                [('x', 0x34), ('y', 0x38), ('vx', 0x120), ('vy', 0x124)]}

    def heartbeat(self, state):
        self.payload = state
        self.put(STACK, '<I', END)
        self.uc.reg_write(UC_X86_REG_ESI, OBJ)
        self.uc.reg_write(UC_X86_REG_ESP, STACK)
        self.uc.emu_start(0x5041e1, END, count=1000)
        assert self.uc.reg_read(UC_X86_REG_EIP) == END

    def jump(self, stage):
        # 0x501d6e selects 180/240 world-unit jump heights. The blocks below
        # include both horizontal-key branches, vx overwrite, sqrt(2*g*h),
        # vy overwrite, and airborne flag writes. No synthetic velocity rule.
        self.uc.reg_write(UC_X86_REG_ESI, OBJ)
        self.uc.reg_write(UC_X86_REG_ESP, STACK)
        self.uc.reg_write(UC_X86_REG_EBP, STACK + 0x100)
        self.uc.reg_write(UC_X86_REG_EDI, 400)
        self.put(STACK + 0xf0, '<f', 180.0 if stage == 1 else 240.0)
        self.put(STACK + 0x108, '<I', stage)
        self.uc.emu_start(0x501e81, 0x501ecc, count=1000)
        assert self.uc.reg_read(UC_X86_REG_EIP) == 0x501ecc
        # Finish the empty terrain query's calling convention (push x, terrain).
        self.uc.reg_write(UC_X86_REG_ESP, self.uc.reg_read(UC_X86_REG_ESP) + 8)
        self.uc.reg_write(UC_X86_REG_EAX, 0)
        self.uc.emu_start(0x501ed1, 0x501eff, count=1000)
        assert self.uc.reg_read(UC_X86_REG_EIP) == 0x501eff
        self.uc.emu_start(0x501f6f, 0x501f83, count=100)

    def hit(self, push):
        """Native damage>10 velocity branch; excludes damage/animation effects."""
        self.uc.reg_write(UC_X86_REG_ESI, OBJ)
        self.uc.reg_write(UC_X86_REG_EBP, STACK + 0x100)
        self.put(STACK + 0x10c, '<I', 20)
        self.put(STACK + 0x114, '<I', STUB + 0x200)
        self.put(STUB + 0x200, '<ff', *push)
        self.uc.emu_start(0x50f864, 0x50f8c3, count=100)
        assert self.uc.reg_read(UC_X86_REG_EIP) == 0x50f8c3

    def bind_to(self, handle):
        """Original 0x50e636 setter, called by the 0x0017 receive handler."""
        self.put(STACK, '<II', END, 0)
        self.uc.reg_write(UC_X86_REG_ESP, STACK)
        self.uc.reg_write(UC_X86_REG_ECX, OBJ)
        self.uc.reg_write(UC_X86_REG_EDX, handle)
        self.uc.emu_start(0x50e636, END, count=100)
        assert self.get(OBJ + 0x164, '<I') == handle


def reproduce():
    rows = []
    for stage in (1, 2):
        vy = -18.8 if stage == 1 else -22.8
        state = botsync.character_state(410.5, 150 + vy, vx=10.5, vy=vy,
                                        on_ground=False, keys=0)
        # Wire order event->heartbeat, but the event is queued. On receipt of
        # the heartbeat 0x407be7 applies it immediately. 0x405810 flushes later.
        c = NativeCharacter()
        c.heartbeat(state)
        anchor = c.state()
        c.jump(stage)
        after = c.state()
        assert anchor['vx'] == 10 and after['vx'] == 0
        assert after['vy'] < anchor['vy']
        rows.append({'stage': stage, 'after_heartbeat': anchor,
                     'after_queued_jump': after,
                     'four_ticks_horizontal_divergence_px': 4 * 10.5})

    # Control: a RIGHT key takes the original nonzero branch. With the fixture
    # speed getter returning 7, original [0x6937ac]=0.25 yields vx=1.75.
    # This proves key dependence, NOT that changing keys alone is a full fix.
    c = NativeCharacter()
    c.heartbeat(botsync.character_state(410, 127, vx=10, vy=-22,
                                        on_ground=False, keys=botsync.KEY_RIGHT))
    c.jump(2)
    assert c.state()['vx'] == 1.75
    # Even without a new anchor, the previous airborne heartbeat keys=0 make
    # the second jump erase vx. This predates the immediate-anchor patch.
    old = NativeCharacter()
    old.heartbeat(botsync.character_state(400, 150, vx=10, vy=0,
                                          on_ground=False, keys=0))
    old.jump(2)
    assert old.state()['vx'] == 0
    # A different frame schedule restores the authoritative velocity. Merely
    # checking wire order misses this scheduling dependence.
    restored = NativeCharacter()
    restored.jump(2)
    restored.heartbeat(botsync.character_state(410, 127, vx=10, vy=-22,
                                               on_ground=False, keys=0))
    assert restored.state()['vx'] == 10

    hit = NativeCharacter()
    hit.heartbeat(botsync.character_state(408, 143, vx=8, vy=-6,
                                          on_ground=False, keys=0))
    hit_anchor = hit.state()
    hit.hit((8.0, -8.0))
    assert hit.state()['vx'] == 16 and hit.state()['vy'] == -14

    bound = NativeCharacter()
    bound.place(1535.77, 902.71, 0, 0, False)
    bound.bind_to(100001)  # human seat 0; seen in the 11:28:19.165 packet.
    bound_before = bound.state()
    correction = botsync.character_state(1653, 763, vx=0, vy=-5,
                                          on_ground=False, keys=0)
    for _ in range(5):
        bound.heartbeat(correction)
    assert bound.state() == bound_before
    bound_ignored = bound.state()
    bound.bind_to(0)
    bound.heartbeat(correction)
    assert bound.state() != bound_before

    return {'image_sha256': IMAGE_SHA256, 'native_jump_replays': rows,
            'control_right_key_after_jump': c.state(),
            'previous_airborne_heartbeat_then_jump': old.state(),
            'control_jump_flushed_before_heartbeat': restored.state(),
            'hit_anchor_then_queued_hit': {'before': hit_anchor, 'after': hit.state()},
            'bound_heartbeat_replay': {'before': bound_before,
                                       'after_five_heartbeats': bound_ignored,
                                       'after_release_and_heartbeat': bound.state()},
            'scope': 'Native state/jump blocks; no live game, network or collision.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output')
    args = parser.parse_args()
    result = json.dumps(reproduce(), ensure_ascii=False, indent=2)
    if args.output:
        with Path(args.output).open('w', encoding='utf-8', newline='\n') as out:
            out.write(result + '\n')
    print(result)
