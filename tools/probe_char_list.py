#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_char_list.py —— 读活着的客户端里「人物选择」的数据源

房间右下角的「人物选择」有几个头像，完全由**我的座位的物品清单**决定
（FINDINGS §119）。这个探针把那条判定链上的每一格都读出来，
一眼看出「是没收到 0x030b」还是「收到了但判定没过」。

判定链（`CharacterChanger` 建按钮时，`0x4f586c`）：

    0x40713a(LobbyStage, 0)  数按钮个数
        for id in 100..110: 0x4070c2(id) 为真就 +1
        return 计数 + 3                       ★ 0/1/2 三个基础角色白送
    0x4070c2(id) = `[LobbyStage+0x1cc]`(我的座位) 已占用
                   且 `[LobbyStage + 座位*4 + 0x250]`(物品清单) 非空
                   且 0x55853c(清单, id)
    0x55853c(清单, id): id < 3 -> true；否则在 `清单+0x18` 的
                        `vector<int32>` 里找落在
                        `[(id+1)*1e6, (id+2)*1e6)` 区间的物品

用法：
    python tools/probe_char_list.py <pid>
"""
import ctypes as C
import struct
import sys
from ctypes import wintypes as W

k32 = C.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.restype = W.HANDLE
k32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
k32.ReadProcessMemory.argtypes = [W.HANDLE, W.LPCVOID, W.LPVOID,
                                  C.c_size_t, C.POINTER(C.c_size_t)]

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400

#: 大厅/房间共用的那个单例（`0x552943` 的 `ChangeStage(4)` 就用它）。
LOBBY_STAGE_PTR = 0x0072E29C
#: 座位表：`LobbyStage+0x40` 起，每项 0x3c 字节，`+0x00` 是占用标记
SEAT_TABLE = 0x40
SEAT_STRIDE = 0x3C
SEAT_COUNT = 6
MY_SEAT = 0x1CC
EQUIP_LIST_TABLE = 0x250
#: 清单对象里 `vector<int32>` 的 begin / end
LIST_VECTOR_BEGIN = 0x18
LIST_VECTOR_END = 0x1C
#: 清单开头那 3 个槽位掩码
LIST_SLOT_MASKS = 0x0C

BASE_CHARACTER_IDS = (0, 1, 2)
PREMIUM_CHARACTER_IDS = tuple(range(100, 111))
STRIDE = 1000000

#: `Data/ChrProps.ini` 的 ChrName（韩文原名，客户端 UI 里也是这些）
CHARACTER_NAMES = {
    0: "타이", 1: "카실", 2: "프로코", 3: "아이린",
    100: "엘리어스", 101: "진", 102: "발키리", 103: "화이트 엘리어스",
    104: "발키리 로터스", 105: "발키리 재규어", 106: "시리아", 107: "라스",
    108: "라스 티타늄", 109: "파이크", 110: "시리아 마스",
    98: "쉐도우 타이", 99: "랜덤",
}


class Mem:
    def __init__(self, pid):
        self.h = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION,
                                 False, pid)
        if not self.h:
            raise SystemExit(f"OpenProcess({pid}) 失败: {C.get_last_error()}")

    def read(self, addr, size):
        buf = (C.c_char * size)()
        got = C.c_size_t(0)
        if not k32.ReadProcessMemory(self.h, C.c_void_p(addr), buf, size,
                                     C.byref(got)) or got.value != size:
            return None
        return bytes(buf)

    def u32(self, addr):
        raw = self.read(addr, 4)
        return None if raw is None else struct.unpack("<I", raw)[0]

    def u8(self, addr):
        raw = self.read(addr, 1)
        return None if raw is None else raw[0]


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    mem = Mem(int(sys.argv[1], 0))

    lobby = mem.u32(LOBBY_STAGE_PTR)
    print(f"[{LOBBY_STAGE_PTR:#010x}] LobbyStage = {lobby:#010x}"
          if lobby else "LobbyStage 还没建（没进大厅？）")
    if not lobby:
        return

    my_seat = mem.u32(lobby + MY_SEAT)
    if my_seat is not None and my_seat >= 0x80000000:
        my_seat -= 1 << 32
    print(f"  我的座位 [+0x1cc] = {my_seat}")

    for seat in range(SEAT_COUNT):
        occupied = mem.u8(lobby + SEAT_TABLE + seat * SEAT_STRIDE)
        listp = mem.u32(lobby + EQUIP_LIST_TABLE + seat * 4)
        mark = " ★我" if seat == my_seat else ""
        print(f"  座位 {seat}: 占用={occupied} 物品清单={listp:#010x}{mark}"
              if listp is not None else f"  座位 {seat}: 读不到")

    if my_seat is None or not 0 <= my_seat < SEAT_COUNT:
        print("!! 我的座位越界，客户端会直接判定「一个商城角色都没有」")
        return

    if not mem.u8(lobby + SEAT_TABLE + my_seat * SEAT_STRIDE):
        print("!! 我的座位没被标成已占用 —— 0x0300 没发或发晚了，"
              "商城角色一个都过不了判定")
        return

    listp = mem.u32(lobby + EQUIP_LIST_TABLE + my_seat * 4)
    if not listp:
        print("!! 我的座位没有物品清单对象")
        return

    masks = struct.unpack("<3i", mem.read(listp + LIST_SLOT_MASKS, 12))
    begin = mem.u32(listp + LIST_VECTOR_BEGIN)
    end = mem.u32(listp + LIST_VECTOR_END)
    count = (end - begin) // 4 if begin and end and end >= begin else 0
    print(f"\n物品清单 @ {listp:#010x}  槽位掩码={masks}  "
          f"begin={begin:#010x} end={end:#010x} 共 {count} 件")
    items = []
    if count:
        raw = mem.read(begin, count * 4)
        if raw:
            items = list(struct.unpack(f"<{count}i", raw))
    for item in items:
        owner = item // STRIDE - 1
        name = CHARACTER_NAMES.get(owner, "?")
        print(f"    {item:<12} -> 角色 {owner} {name}")

    print("\n「人物选择」会出现的角色：")
    shown = list(BASE_CHARACTER_IDS)
    for character_id in PREMIUM_CHARACTER_IDS:
        low = (character_id + 1) * STRIDE
        hit = [i for i in items if low <= i < low + STRIDE]
        if hit:
            shown.append(character_id)
    for character_id in shown:
        print(f"    id={character_id:<4} {CHARACTER_NAMES.get(character_id, '?')}"
              f"{'  （基础角色，白送）' if character_id in BASE_CHARACTER_IDS else ''}")
    print(f"  合计 {len(shown)} 个（客户端 0x40713a 数出来的按钮个数）")


if __name__ == "__main__":
    main()
