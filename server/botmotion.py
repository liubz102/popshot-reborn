"""Server-owned bot motion, appended to an otherwise ordinary heartbeat.

BSM1 clients run damage/animation events without applying their motion twice.
The existing 43-byte UdpPacket prefix and reliable event numbering are unchanged.
"""
import itertools
import struct

MAGIC = b"BSM\x01"
TRAILER = struct.Struct("<4sIII")  # identity/life, revision, simulation tick
PEER_SIZE = 43
_identities = itertools.count(1)


def new_identity():
    return next(_identities) & 0xffffffff


def trailer(identity, revision, tick):
    return TRAILER.pack(MAGIC, identity & 0xffffffff,
                        revision & 0xffffffff, tick & 0xffffffff)


def metadata(packet):
    if (len(packet) != PEER_SIZE + TRAILER.size
            or packet[10:12] != b"\x01\x40"
            or packet[PEER_SIZE:PEER_SIZE + 4] != MAGIC):
        return None
    _, identity, revision, tick = TRAILER.unpack_from(packet, PEER_SIZE)
    return identity, revision, tick


# Character::vft+0x7c (0x4fc229): [0x693758] = 35.0.
CONSTRAINT_DISTANCE = 35.0


def constrained_x(x, owner_x, facing):
    boundary = int(owner_x + facing * CONSTRAINT_DISTANCE)
    here = int(x)  # original uses _ftol2 at both points
    if facing == 1 and here < boundary or facing == -1 and here > boundary:
        return x + boundary - here
    return x
