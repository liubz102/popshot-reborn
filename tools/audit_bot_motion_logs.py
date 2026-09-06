#!/usr/bin/env python3
"""Read existing server/hook logs; emit aggregate bot motion evidence as JSON.

No accounts, authentication data, sockets or running processes are accessed.
FIRE position comparison uses the bot's _muzzle offset: +/-43 x, -57 y.
Only original shots (body source 10+seat) are compared; fragments are excluded.
Client positions are actual historical FIRE> snapshots, NOT replay predictions.
"""
import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import re
import struct

TIME = r'(\d\d:\d\d:\d\d\.\d\d\d)'
SEND = re.compile(r'^\[' + TIME + r'\] #(\d+) → 发出 (\d+) 字节明文')
UP = re.compile(r'^\[' + TIME + r'\] #(\d+) ★ 游戏包 opcode=0x040e.*?载荷 (\d+) 字节')
HEX = re.compile(r'^\s+([0-9a-f]{4})  (.*?)\s*\|')
FIRE = re.compile(r'^\[' + TIME + r'\] FIRE>\s+who (\d+)\(座位 (-?\d+),.*?发方 (\d+) 槽 \d+ 武器 (\d+) 发射点 \(([-.\d]+), ([-.\d]+)\) 角度 ([-.\d]+) 力度 ([-.\d]+) 颗数\(\+22\) (\d+)')
POS = re.compile(r'★射手角色位置\(\+34,38\) \(([-.\d]+), ([-.\d]+)\)')


def seconds(s):
    h, m, sec = s.split(':')
    return int(h) * 3600 + int(m) * 60 + float(sec)


def quantiles(values):
    vals = sorted(values)
    if not vals:
        return {'n': 0}
    return {'n': len(vals), **{label: round(vals[round((len(vals) - 1) * q)], 3)
                             for label, q in [('min', 0), ('p50', .5), ('p90', .9),
                                              ('p99', .99), ('max', 1)]}}


def server_packets(path, include_upstream=False):
    packets, rejected = [], 0
    pending = None

    def finish():
        nonlocal rejected
        if pending is None:
            return
        raw = bytes(pending['raw'])
        if len(raw) != pending['size']:
            rejected += 1
            return
        if pending['upstream']:
            p = raw
        else:
            if len(raw) < 22 or raw[0] != 0xff or raw[8:10] != b'\x0f\x04':
                return
            p = raw[10:]
        if len(p) < 12:
            return
        seat, epoch, seq, op = p[1], struct.unpack_from('<H', p, 4)[0], *struct.unpack_from('<HH', p, 8)
        if not (0 if include_upstream else 1) <= seat <= 5:
            return
        row = {k: pending[k] for k in ('time', 't', 'line', 'conn', 'upstream')}
        row.update(seat=seat, epoch=epoch, seq=seq, op=op, body=p[12:])
        if op == 0x4001 and len(p) >= 43 and struct.unpack_from('<I', p, 14)[0] == 1:
            x, y, vx, vy = struct.unpack_from('<hhhh', p, 19)
            flags = struct.unpack_from('<I', p, 31)[0]
            keys = struct.unpack_from('<H', p, 35)[0]
            row.update(x=x, y=y, vx=vx, vy=vy, ground=bool(flags & 4), keys=keys)
        packets.append(row)

    with Path(path).open(encoding='utf-8', errors='replace') as f:
        for line_no, line in enumerate(f, 1):
            start = SEND.match(line)
            upstream = UP.match(line) if include_upstream else None
            start = start or upstream
            hexline = HEX.match(line) if pending else None
            if hexline:
                chunk = bytes.fromhex(hexline[2])
                if int(hexline[1], 16) != len(pending['raw']):
                    pending['size'] = -1
                pending['raw'].extend(chunk)
            else:
                finish()
                pending = None
                if start:
                    pending = {'time': start[1], 't': seconds(start[1]), 'line': line_no,
                               'conn': int(start[2]), 'size': int(start[3]), 'raw': bytearray(),
                               'upstream': bool(upstream)}
        finish()
    return packets, rejected


def client_fires(path):
    rows, pending = [], None
    with Path(path).open(encoding='utf-8', errors='replace') as f:
        for line_no, line in enumerate(f, 1):
            match = FIRE.match(line)
            if match:
                pending = {'time': match[1], 't': seconds(match[1]), 'who': int(match[2]),
                           'seat': int(match[3]), 'sender': int(match[4]),
                           'weapon': int(match[5]), 'mx': float(match[6]),
                           'my': float(match[7]), 'angle': float(match[8]),
                           'power': float(match[9]), 'count': int(match[10]), 'line': line_no}
            elif pending:
                pos = POS.search(line)
                if pos:
                    pending.update(cx=float(pos[1]), cy=float(pos[2]))
                    rows.append(pending)
                    pending = None
                elif 'FIRE>' in line or '角色位置 = 读不到' in line:
                    pending = None
    return rows


def audit(server, client):
    all_packets, rejected = server_packets(server, include_upstream=True)
    packets = [p for p in all_packets if not p['upstream'] and p['seat'] > 0]
    bindings = []
    for p in all_packets:
        if p['upstream'] and p['op'] == 0x17 and len(p['body']) == 8:
            victim, owner = struct.unpack('<II', p['body'])
            bindings.append({'time': p['time'], 't': p['t'], 'line': p['line'],
                             'victim_handle': victim, 'owner_handle': owner})
    streams = defaultdict(list)
    shots = defaultdict(list)
    for p in packets:
        streams[p['conn'], p['epoch'], p['seat']].append(p)
        if p['op'] == 2 and len(p['body']) == 26 and p['body'][0] == 10 + p['seat']:
            weapon, mx, my, angle, power, count = struct.unpack_from('<iffffi', p['body'], 2)
            # Hook logs retain two decimal places; use a rounded coordinate key.
            shots[p['seat'], weapon, round(mx, 2), round(my, 2), round(angle, 4), round(power, 3), count].append(p)
    jumps, air_counts = [], Counter()
    for stream in streams.values():
        beats = [p for p in stream if p['op'] == 0x4001 and 'keys' in p]
        times = [p['t'] for p in beats]
        for p in beats:
            air_counts['total'] += 1
            if not p['ground']:
                air_counts['air'] += 1
                air_counts['air_zero_keys'] += not bool(p['keys'] & 5)
        for i, p in enumerate(stream):
            if p['op'] != 6 or len(p['body']) != 2:
                continue
            j = bisect_right(times, p['t'] - 0.00001)
            if j >= len(beats):
                continue
            nxt = beats[j]
            prev = beats[j - 1] if j else None
            jumps.append({'time': p['time'], 'line': p['line'], 'seat': p['seat'],
                          'stage': p['body'][1], 'heartbeat_line': nxt['line'],
                          'next_heartbeat_ms': round((nxt['t'] - p['t']) * 1000, 3),
                          'next_vx': nxt['vx'], 'next_keys': nxt['keys'],
                          'previous_air_zero_keys': bool(prev and not prev['ground'] and not (prev['keys'] & 5))})

    matches = []
    for c in client_fires(client):
        candidates = shots.get((c['seat'], c['weapon'], c['mx'], c['my'], c['angle'], c['power'], c['count']), [])
        candidates = [p for p in candidates if 0 <= c['t'] - p['t'] < 2]
        if not candidates:
            continue
        p = min(candidates, key=lambda p: c['t'] - p['t'])
        # y is exact up to float/log rounding: _muzzle always subtracts 57.
        # x has two possible muzzle directions; take the smaller error, giving
        # a conservative lower bound without guessing aim/facing.
        sy = c['my'] + 57.0
        dx = min(abs(c['cx'] - (c['mx'] - 43)), abs(c['cx'] - (c['mx'] + 43)))
        dy = c['cy'] - sy
        preceding = [b for b in bindings if b['victim_handle'] == (c['seat'] + 1) * 100000 + 1 and 0 <= c['t'] - b['t'] < 1]
        matches.append({'time': c['time'], 'seat': c['seat'], 'client_line': c['line'],
                        'server_line': p['line'], 'latency_ms': round((c['t'] - p['t']) * 1000, 3),
                        'y_error': round(dy, 2), 'x_error_lower_bound': round(dx, 2),
                        'position_error_lower_bound': round(math.hypot(dx, dy), 2),
                        'server_y': round(sy, 2), 'client_y': c['cy'],
                        'recent_binding_event': preceding[-1] if preceding else None})

    return {'server_log': str(server), 'client_log': str(client),
            'rejected_incomplete_send_dumps': rejected,
            'bot_packet_counts': dict(Counter(hex(p['op']) for p in packets)),
            'heartbeat_counts': dict(air_counts),
            'jumps_by_stage': dict(Counter(j['stage'] for j in jumps)),
            'upstream_binding_events': len(bindings),
            'binding_examples': bindings[:5],
            'jump_to_next_heartbeat_ms': quantiles([j['next_heartbeat_ms'] for j in jumps]),
            'jumps_previous_air_zero_keys': sum(j['previous_air_zero_keys'] for j in jumps),
            'jump_examples': jumps[:8],
            'matched_fire_latency_ms': quantiles([p['latency_ms'] for p in matches]),
            'actual_client_y_error_px': quantiles([abs(p['y_error']) for p in matches]),
            'actual_client_position_error_lower_bound_px': quantiles([p['position_error_lower_bound'] for p in matches]),
            'largest_actual_client_errors': sorted(matches, key=lambda p: p['position_error_lower_bound'], reverse=True)[:12],
            'scope': 'FIRE-time position difference is observed; attribution to individual causes requires per-frame character traces. Time includes quantization to a client frame.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('server_log')
    parser.add_argument('client_log')
    parser.add_argument('--output')
    args = parser.parse_args()
    result = json.dumps(audit(args.server_log, args.client_log), ensure_ascii=False, indent=2)
    if args.output:
        with Path(args.output).open('w', encoding='utf-8', newline='\n') as out:
            out.write(result + '\n')
    print(result)
