#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ingame.py —— 实机看图：驱动真客户端进庆典图，按点位传送 + 截图，拼成接触表（X_Mod · X12）。

    前提：start.bat 已把服务端 + 客户端起起来（要开调试控制通道 27800）。
    python tools\\map_festival_fill\\ingame.py login          # 登测试账号（testuser1）
    python tools\\map_festival_fill\\ingame.py room           # 建个人战对战房 + 叫一个 bot
    python tools\\map_festival_fill\\ingame.py start          # bot 准备 + F5 + 等进图，然后让 bot 站住、最低难度
    python tools\\map_festival_fill\\ingame.py tour Festival02  # 换到这张图，按点位传送截图，拼表到 logs\\map_festival_fill\\ingame\\
    python tools\\map_festival_fill\\ingame.py chat /h        # 房间里注入一行聊天
    python tools\\map_festival_fill\\ingame.py bchat /hold 1  # 战斗中注入一行聊天

## 和 test/custom-weapon-e2e/drive.py 的关系

登录 / 点大厅 / 建房 / 读 stage 和座位 / 截图全部复用它；这里只补三件它没有的：

1. **聊天不走键盘事件**：`keybd_event` 打进去的字会被中文输入法拼成「出/」之类（2026-09-21 实测），
   改成先把游戏窗口所在线程的输入法关掉（`WM_IME_CONTROL` / `IMC_SETOPENSTATUS`，跨进程可用），
   再 `PostMessage(WM_CHAR)` 逐字符注入。
2. **战斗中的聊天**：先回车打开输入行再注入（房间里是点聊天框）。
3. **让 bot 别把一局打没**：夺分模式 4 个人头就结算（`gameserver` 的胜利线），进图后 `/hold 1` + `/d 1`。

★ 换图 `nextmap` 走的是客户端 0x0411 那条路，客户端**不离开 stage 7**（卸场景 + 加载都在 7 里），
  判据用服务端 `status` 的 `maps_entered` 条数 +1。
★ 传送用调试通道 `respawn 0 x y`（0x0419）；落在无碰撞处会掉下去，点位要给在站得住的地方。
"""
from __future__ import annotations

import ctypes as C
import os
import sys
import time
from ctypes import wintypes as W

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "test", "custom-weapon-e2e"))
import drive  # noqa: E402

OUT = os.path.join(ROOT, "logs", "map_festival_fill", "ingame")

u32 = C.WinDLL("user32", use_last_error=True)
imm32 = C.WinDLL("imm32", use_last_error=True)
imm32.ImmGetDefaultIMEWnd.argtypes = [W.HWND]
imm32.ImmGetDefaultIMEWnd.restype = W.HWND
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
WM_IME_CONTROL, IMC_SETOPENSTATUS, IMC_SETCONVERSIONMODE = 0x0283, 0x0006, 0x0002
VK_RETURN = 0x0D

#: 每张图的点位（世界坐标，要站得住）。
TOUR = {
    "Festival00": [("spawn", None), ("leftwall", (280, 560)), ("rightwall", (1660, 560)), ("tower-base", (952, 880)), ("tower-top", (915, 250))],
    "Festival01": [("spawn", None), ("leftwalls", (300, 560)), ("rightwalls", (1650, 560)), ("dragon", (247, 1120)),
                   ("center", (1084, 900)), ("arch", (1000, 330))],
    "Festival02": [("spawn", None), ("pier-left", (560, 1120)), ("pier-right", (1100, 1120)), ("rope", (800, 440)),
                   ("right-building", (1600, 800)), ("leftwall", (160, 880)), ("crates", (900, 1100)), ("waterline", (430, 1150))],
}


# ---------------------------------------------------------------------------
#  注入
# ---------------------------------------------------------------------------

def ime_off(h):
    """把游戏窗口所在线程的输入法关掉并切成英数模式。"""
    ime = imm32.ImmGetDefaultIMEWnd(W.HWND(h))
    if ime:
        u32.SendMessageW(W.HWND(ime), WM_IME_CONTROL, IMC_SETCONVERSIONMODE, 0)
        u32.SendMessageW(W.HWND(ime), WM_IME_CONTROL, IMC_SETOPENSTATUS, 0)
    return bool(ime)


def post_char(h, ch):
    u32.PostMessageW(W.HWND(h), WM_CHAR, ord(ch), 1)


def post_enter(h):
    u32.PostMessageW(W.HWND(h), WM_KEYDOWN, VK_RETURN, 0x001C0001)
    u32.PostMessageW(W.HWND(h), WM_CHAR, 0x0D, 0x001C0001)
    u32.PostMessageW(W.HWND(h), WM_KEYUP, VK_RETURN, 0xC01C0001)


def _inject(h, text):
    ime_off(h)
    for ch in text:
        post_char(h, ch)
        time.sleep(0.05)
    time.sleep(0.2)
    post_enter(h)


def chat(text, confirm=None, fuse=12.0):
    """房间里：点聊天框，注入一行并回车；confirm = 「生效了」的判据。"""
    pid = drive.find_pid()
    h = drive.game_hwnd(pid)
    drive.ensure_foreground(pid)
    ime_off(h)
    drive.click_at(*drive.COORD["chat"], pid=pid)
    time.sleep(0.4)
    _inject(h, text)
    if confirm is None:
        time.sleep(0.8)
        return
    try:
        drive.wait_for(confirm, "聊天 %r 生效" % text, fuse=fuse / 2, poll=0.3)
        return
    except drive.DriveError:
        pass
    post_enter(h)
    drive.wait_for(confirm, "聊天 %r 生效（补回车）" % text, fuse=fuse, poll=0.3,
                   on_fail=lambda: "座位=%s 截图=%s" % (drive.seats(pid), drive.shot("chat-failed", pid)))


def battle_chat(text, settle=1.0):
    """战斗中：回车打开输入行，注入，回车发送。"""
    pid = drive.find_pid()
    h = drive.game_hwnd(pid)
    drive.ensure_foreground(pid)
    ime_off(h)
    post_enter(h)
    time.sleep(0.4)
    _inject(h, text)
    time.sleep(settle)


# ---------------------------------------------------------------------------
#  流程
# ---------------------------------------------------------------------------

def room():
    """建个人战对战房（照 drive.create_room 的点击序列）+ 叫一个 bot。"""
    pid = drive.find_pid()
    drive.goto_lobby(pid)
    drive.click_at(*drive.COORD["tab_pvp"], pid=pid)
    time.sleep(0.6)
    drive.click_at(*drive.COORD["create_room"], pid=pid)
    time.sleep(1.0)
    drive.drag_select(drive.COORD["pvp_mode"], drive.COORD["pvp_mode_free"], pid=pid)
    drive.click_at(*drive.COORD["ok_pvp"], pid=pid)
    drive.wait_for(lambda: drive.room_type() == drive.SESSION_TYPE_NORMAL, "建好个人战房", fuse=60.0)
    chat("/a", confirm=lambda: drive.seats(pid)[1][0])
    print("bot 进房，座位 =", drive.seats(pid))


def start():
    """bot 准备 + F5 + 等进图；进图后让 bot 站住、最低难度。"""
    pid = drive.find_pid()
    if not drive.guests_ready(pid):
        chat("/r", confirm=lambda: drive.guests_ready(pid))
    time.sleep(drive.ROSTER_SETTLE_S)
    drive.ensure_foreground(pid)
    drive.press(drive.VK_F5, pid=pid)
    drive.wait_for(lambda: drive.stage(pid) == drive.STAGE_LEVEL, "开局进图", fuse=180.0,
                   on_fail=lambda: "stage=%s 截图=%s" % (drive.stage(pid), drive.shot("start-failed", pid)))
    battle_chat("/hold 1")
    battle_chat("/d 1")
    print("已进图，bot 已站住")


def tour(name):
    """换到 name 这张图，按 TOUR 点位传送 + 截图，拼接触表。"""
    from PIL import Image, ImageDraw
    os.makedirs(OUT, exist_ok=True)
    pid = drive.find_pid()
    before = len(drive.status().get("maps_entered", []))
    drive.control("nextmap " + name)
    drive.wait_for(lambda: len(drive.status().get("maps_entered", [])) > before, "服务端记下换图 " + name, fuse=60.0)
    time.sleep(6.0)   # 客户端卸场景 + 加载新图，stage 不离开 7、没有事件可等；截早了只是黑屏，重截即可
    shots = []
    for tag, pos in TOUR[name]:
        if pos is not None:
            drive.control("respawn 0 %d %d" % pos)
            time.sleep(1.5)   # 镜头跟过去，纯观感
        drive.ensure_foreground(pid)
        shots.append((tag, drive.shot("%s-%s" % (name, tag), pid)))
    ims = [(tag, Image.open(p).convert("RGB")) for tag, p in shots if p]
    w, h = max(im.width for _, im in ims) // 2, max(im.height for _, im in ims) // 2
    cols = 2
    rows = (len(ims) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (w + 8), rows * (h + 24)), (30, 30, 36))
    d = ImageDraw.Draw(sheet)
    for i, (tag, im) in enumerate(ims):
        x, y = (i % cols) * (w + 8), (i // cols) * (h + 24)
        sheet.paste(im.resize((w, h), Image.LANCZOS), (x, y + 20))
        d.text((x + 4, y + 4), "%s / %s" % (name, tag), fill=(240, 240, 240))
    out = os.path.join(OUT, name + "_sheet.png")
    sheet.save(out)
    print("接触表 ->", out)


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "login":
        print("已登录，pid =", drive.login())
    elif cmd == "room":
        room()
    elif cmd == "start":
        start()
    elif cmd == "tour":
        tour(rest[0])
    elif cmd == "chat":
        chat(rest[0])
    elif cmd == "bchat":
        battle_chat(rest[0])
    elif cmd == "stage":
        pid = drive.find_pid()
        print("stage =", drive.stage(pid), "seats =", drive.seats(pid))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
