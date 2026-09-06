"""Execute compiled production motion detours in Unicorn, with the original image.

Build hook/test_bot_motion.c as an x86 DLL in logs/bot_motion_test.dll first.
This verifies ABI/register/stack behavior, version gating, and preserved motion
while native event side effects continue. No live game or system injection.
"""
import json
from pathlib import Path
import struct

from replay_bot_motion import NativeCharacter, ROOT, OBJ, STACK, STUB, END, botsync
from unicorn.x86_const import *
from unicorn import UC_HOOK_CODE
import botmotion


def load_dll(uc, path):
    raw = Path(path).read_bytes()
    u16 = lambda at: struct.unpack_from('<H', raw, at)[0]
    u32 = lambda at: struct.unpack_from('<I', raw, at)[0]
    pe = u32(0x3c)
    assert raw[pe:pe+4] == b'PE\0\0' and u16(pe + 4) == 0x14c
    opt = pe + 24
    assert u16(opt) == 0x10b
    base, size, headers = u32(opt + 28), u32(opt + 56), u32(opt + 60)
    uc.mem_map(base, (size + 4095) & ~4095)
    uc.mem_write(base, raw[:headers])
    for i in range(u16(pe + 6)):
        s = opt + u16(pe + 20) + i * 40
        uc.mem_write(base + u32(s + 12), raw[u32(s+20):u32(s+20)+u32(s+16)])
    mem32 = lambda at: struct.unpack('<I', uc.mem_read(at, 4))[0]
    # Only the five Windows memory-management APIs used by the real installer
    # are emulated. Its signature checks, copies, jumps and enable logic run as
    # compiled C. No operating-system process is patched.
    api_page, next_allocation = 0x3000000, [0x30000000]
    uc.mem_map(api_page, 0x10000)

    def api_call(u, address, size, name):
        counts = {'VirtualAlloc': 4, 'VirtualProtect': 4, 'VirtualFree': 3,
                  'FlushInstructionCache': 3, 'GetCurrentProcess': 0}
        if name not in counts:
            raise AssertionError('Unexpected operating-system call: ' + name)
        esp = u.reg_read(UC_X86_REG_ESP)
        values = struct.unpack('<' + 'I' * (counts[name] + 1), u.mem_read(esp, 4 * (counts[name] + 1)))
        ret, args = values[0], values[1:]
        result = 1
        if name == 'VirtualAlloc':
            result = next_allocation[0]
            length = (args[1] + 4095) & ~4095
            u.mem_map(result, length)
            next_allocation[0] += length
        elif name == 'VirtualProtect':
            u.mem_write(args[3], struct.pack('<I', 0x20))
        elif name == 'GetCurrentProcess':
            result = 0xffffffff
        u.reg_write(UC_X86_REG_EAX, result)
        u.reg_write(UC_X86_REG_ESP, esp + 4 * (counts[name] + 1))
        u.reg_write(UC_X86_REG_EIP, ret)

    desc, api_index = base + u32(opt + 104), 0
    while mem32(desc + 12):
        oft, iat = base + mem32(desc), base + mem32(desc + 16)
        offset = 0
        while mem32(oft + offset):
            name_ptr = base + mem32(oft + offset) + 2
            name = bytes(uc.mem_read(name_ptr, 120)).split(b'\0')[0].decode('ascii')
            address = api_page + api_index * 16
            api_index += 1
            uc.mem_write(iat + offset, struct.pack('<I', address))
            uc.hook_add(UC_HOOK_CODE, api_call, user_data=name, begin=address, end=address)
            offset += 4
        desc += 20
    exp = base + u32(opt + 96)
    funcs, names, ords = [base + mem32(exp + off) for off in (28, 32, 36)]
    result = {}
    for i in range(mem32(exp + 24)):
        p = base + mem32(names + 4 * i)
        name = bytes(uc.mem_read(p, 100)).split(b'\0')[0].decode('ascii').lstrip('_')
        ordinal = struct.unpack('<H', uc.mem_read(ords + 2*i, 2))[0]
        result[name] = base + mem32(funcs + 4 * ordinal)
    return result


class Fixture(NativeCharacter):
    def __init__(self):
        super().__init__()
        self.uc.mem_map(0, 0x10000)  # SEH prolog fs:[0], no exceptions are raised.
        self.exports = load_dll(self.uc, ROOT / 'logs/bot_motion_test.dll')
        self.session, self.packet = OBJ + 0x28000, OBJ + 0x27000
        self.put(0x72e29c, '<I', self.session)
        self.put(self.session + 0x3c, '<I', 1)
        self.put(self.session + 0x1cc, '<I', 0)
        self.put(self.session + 0x1d4, '<I', OBJ)
        self.put(OBJ + 0x2ac, '<I', 1)
        # Mock only the original event callee, not the compiled guard. It writes
        # both protected motion and an unprotected visual counter, returns a
        # sentinel, and has the original calling convention.
        for address, modrm, cleanup in ((STUB + 0x800, 0x86, 4), (STUB + 0x900, 0x81, 8)):
            code = bytearray()
            for offset, value in ((0x34, 0), (0x120, 0), (0x124, 0), (0x128, 0),
                                  (0x130, 0), (0x4c4, 0), (0x594, 77)):
                code += bytes([0xc7, modrm]) + struct.pack('<II', offset, value)
            code += b'\xb8' + struct.pack('<I', 0x12345678) + b'\xc2' + struct.pack('<H', cleanup)
            self.uc.mem_write(address, bytes(code))
        self.reset_mode()

    def call(self, address, args=(), registers=None, stop=END):
        self.put(STACK, '<' + 'I' * (1 + len(args)), END, *args)
        self.uc.reg_write(UC_X86_REG_ESP, STACK)
        self.uc.reg_write(UC_X86_REG_EBP, 0x12340)
        for reg, value in (registers or {}).items():
            self.uc.reg_write(reg, value)
        self.uc.emu_start(address, stop, count=100000)
        assert self.uc.reg_read(UC_X86_REG_EIP) == stop, hex(self.uc.reg_read(UC_X86_REG_EIP))
        return self.uc.reg_read(UC_X86_REG_EAX)

    def reset_mode(self):
        self.call(self.exports['bm_test_setup'], (STUB + 0x800, 0x50f800,
                                                 0x50e636, 0x492e23, STUB + 0x900))

    def write_packet(self, identity=1, revision=1, n=0):
        body = botsync.heartbeat_body(n, 1, botsync.character_state(410, 127, vx=10,
                                          vy=-22, on_ground=False, keys=0))
        body += botmotion.trailer(identity, revision, 12)
        data = botsync.build_peer_packet(1, botsync.OP_HEARTBEAT, body, 1)
        self.uc.mem_write(self.packet, data)
        return data

    def receive(self, identity=1, revision=1, n=0):
        data = self.write_packet(identity, revision, n)
        return self.call(self.exports['bm_test_receive'], (self.session, self.packet, len(data)))

    def entry(self, index):
        return self.call(self.exports['bm_test_entry'], (index,))


def run():
    f = Fixture()
    assert f.receive(n=8) == 1
    # Receiving a first UDP N=8 must not abandon reliable events 0..7.
    q = f.session + 0x2e4 + 0x24
    assert f.get(q + 4, '<B') == 1 and f.get(q + 8, '<I') == 0
    for i, args, reg in ((0, (2,), UC_X86_REG_ESI), (4, (1, 0), UC_X86_REG_ECX)):
        f.place(410, 127, 10, -22, False)
        before = f.state()
        address = f.entry(i)
        eax = f.call(address, args, {reg: OBJ, UC_X86_REG_EBX: 0x11112222,
                                    UC_X86_REG_EDI: 0x33334444})
        assert f.state() == before, (i, f.state(), before)
        assert f.get(OBJ + 0x594, '<I') == 77  # visual side effect was executed
        assert eax == 0x12345678
        assert f.uc.reg_read(UC_X86_REG_EBX) == 0x11112222
        assert f.uc.reg_read(UC_X86_REG_EDI) == 0x33334444
        assert f.uc.reg_read(UC_X86_REG_EBP) == 0x12340
        assert f.uc.reg_read(UC_X86_REG_ESP) == STACK + 4 + 4 * len(args)
    f.call(f.entry(1), registers={UC_X86_REG_ESI: OBJ}, stop=0x50f94e)
    f.call(f.entry(2), (0,), {UC_X86_REG_ECX: OBJ, UC_X86_REG_EDX: 100001})
    assert f.get(OBJ + 0x164, '<I') == 0
    assert f.receive(revision=1) == 0 and f.receive(revision=0) == 0
    assert f.receive(revision=2) == 1
    assert f.receive(identity=2, revision=1) == 1
    assert f.receive(identity=1, revision=999) == 0
    f.write_packet(identity=2, revision=2)
    frame = OBJ + 0x26000
    f.put(frame - 0x1c, '<I', 59)
    regs = {UC_X86_REG_EBP: frame, UC_X86_REG_ESI: f.packet, UC_X86_REG_EDI: f.session}
    f.call(f.entry(5), registers=regs, stop=0x4078fe)
    f.call(f.entry(5), registers=regs, stop=0x40793a)
    # New epoch discards old role membership. A human keeps native Jump/Hit/bind.
    f.put(f.session + 0x3c, '<I', 2)
    f.call(f.entry(0), (1,), {UC_X86_REG_ESI: OBJ})
    assert f.state()['vx'] == 0
    f.call(f.entry(1), registers={UC_X86_REG_ESI: OBJ}, stop=0x50f800)
    f.call(f.entry(2), (0,), {UC_X86_REG_ECX: OBJ, UC_X86_REG_EDX: 100001})
    assert f.get(OBJ + 0x164, '<I') == 100001
    # The production installer must recognize and patch every site, including
    # operand-size prefix / LEA / absolute-load instructions that the former
    # generic hook decoder cannot handle.
    install = Fixture()
    sites = [(0x4078f6, 8), (0x501d57, 5), (0x50f800, 5),
             (0x50e636, 6), (0x492e23, 5), (0x5020d9, 5)]
    originals = [bytes(install.uc.mem_read(va, n)) for va, n in sites]
    assert install.call(install.exports['bm_test_install']) == 1
    for i, (va, n) in enumerate(sites):
        trampoline = install.call(install.exports['bm_test_original'], (i,))
        assert install.uc.mem_read(va, 1) == b'\xe9'
        assert bytes(install.uc.mem_read(trampoline, n)) == originals[i]
        delta = install.get(trampoline + n + 1, '<i')
        assert trampoline + n + 5 + delta == va + n
    return {'compiled_detours': 'PASS', 'all_six_production_install_sites': 'PASS',
            'actual_packet_gate_and_stale_exit': 'PASS', 'jump_dash_stack_and_registers': 'PASS',
            'motion_preserved_visual_side_effects_executed': 'PASS',
            'hit_and_binding_routes': 'PASS', 'stale_duplicate_life_epoch': 'PASS',
            'first_udp_keeps_reliable_base_zero': 'PASS', 'unmarked_human_unchanged': 'PASS'}


if __name__ == '__main__':
    print(json.dumps(run(), indent=2))
