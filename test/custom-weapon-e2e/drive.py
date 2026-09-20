#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""drive.py —— 无人值守地驱动真客户端：登录 → 建房 → 开局 → 收局。

    python drive.py login                 # 只把测试账号登进去
    python drive.py shot 名字              # 存一张截图到 logs/e2e-shots/
    python drive.py status                # 打印服务端权威状态
    python drive.py room quest            # 建一个闯关房（pvp = 个人战对战房）
    python drive.py start                 # 按 F5 开局，等到真进图
    python drive.py finish                # 结束这一局，回到房间

## 为什么非得驱动真客户端

要验的是「服务端下发的数值**有没有写进客户端内存**」。假客户端没有那张表，
日志只回读 2 格（§5.3），所以只能让真客户端真进一次图，再从外部读它的内存。

## 两类操作，两套手段

| 界面 | 是什么 | 怎么操作 |
|---|---|---|
| 登录框 | 真 Win32 `#32770` | `WM_SETTEXT` + `BM_CLICK`，**不用抢鼠标** |
| 大厅 / 房间 / 局内 | **D3D 自绘**，没有子窗口 | 只能真挪鼠标（`SetCursorPos` + `mouse_event`）或发按键 |

⇒ 跑测试期间这台机器**不能干别的**，游戏窗口必须在前台。

## ★ 判据一律事件驱动（铁律 10）

「点完一步等 2 秒」这种写法是拿一台机器上的观测值当真理。这里一律轮询**事实**：

- 登录框出现 → `find_dialogs(pid)` 非空
- 进了大厅 → 服务端 `who` 里有这个账号，且客户端武器表已经解析出记录
- 建房成功 / 房间类型 → 服务端 `status` 的 `room_type`
- **真进图了 → `stage()` == 7**（`[[0x72e2a4]+0x54]`）
  ⚠ 别用 `status` 的 `maps_entered`：那是**换图**（`0x0411`）才追加的，
    开局进第一张图它一直是 `[]`；也别用 `GameContext`（`[0x72e2dc]`）——
    退图之后**不清零**，是野指针。
- bot 准备好了 → `[座位+0x2e]`；bot 进房了 → `[座位+0x00]`

每个 `wait_for` 都带一条**保险丝**（`fuse`）。保险丝不是判据 —— 它只负责在
无人值守时别永久卡住，超时就带着截图和现场状态失败，绝不「当作成功继续」。

★ **唯一一处真的在睡**是 `ROSTER_SETTLE_S`（客户端写死的 3 秒锁），理由见那条常量。

## `SendMessage` 的坑

登录失败时客户端会弹模态框，`SendMessageW` 会一直挂到游戏退出（V0.3商店 D 记过）。
这里一律用 `SendMessageTimeoutW`，所以无人值守不会卡死。
"""
import argparse
import ast
import ctypes as C
import os
import re
import socket
import sys
import time
from ctypes import wintypes as W

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
for _p in (os.path.join(ROOT, "server"), os.path.join(ROOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")

import click as clicktool          # noqa: E402  tools/click.py
import gui_probe                   # noqa: E402  tools/gui_probe.py
import screenshot as shottool      # noqa: E402  tools/screenshot.py

import wtab                        # noqa: E402  同目录

SHOT_DIR = os.path.join(ROOT, "logs", "e2e-shots")
CONTROL_HOST, CONTROL_PORT = "127.0.0.1", 27800

ACCOUNT = "testuser1"
PASSWORD = "123"

#: 房间类型（`SessionDescriptor` 的第一个 int32，`gameserver.SESSION_TYPE_*`）。
#: 2 = 闯关（PVE）；1 = 对战。服务端判 PVE/PVP 只看「是不是 2」。
SESSION_TYPE_QUEST = 2
SESSION_TYPE_NORMAL = 1

u32 = C.WinDLL("user32", use_last_error=True)
u32.SendMessageTimeoutW.argtypes = [W.HWND, C.c_uint, W.WPARAM, C.c_void_p,
                                    C.c_uint, C.c_uint, C.POINTER(C.c_size_t)]
u32.SendMessageTimeoutW.restype = W.LPARAM

WM_SETTEXT = 0x000C
BM_CLICK = 0x00F5
SMTO_ABORTIFHUNG = 0x0002
#: 给 `SendMessageTimeout` 的毫秒数。**这不是判据**，是防止模态框把无人值守挂死的
#: 保险丝 —— 真正判「登录成功了没有」看的是服务端 `who`。
SEND_TIMEOUT_MS = 3000


class DriveError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# 服务端控制通道（127.0.0.1:27800）
# --------------------------------------------------------------------------

def control(line, timeout=8.0):
    """往控制通道发一行命令，返回它回的整段文本。"""
    try:
        with socket.create_connection((CONTROL_HOST, CONTROL_PORT), timeout=timeout) as s:
            s.sendall((line + "\n").encode("utf-8"))
            chunks = []
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
    except OSError as err:
        raise DriveError("控制通道 %s:%d 连不上（%s）—— 服务端在跑吗？"
                         % (CONTROL_HOST, CONTROL_PORT, err)) from None
    return b"".join(chunks).decode("utf-8", "replace").strip()


#: `status` 的回执是一行 `键=值` —— 但有些值**自带空格**
#: （`quest=(3, 1)`、`quest_difficulty={1: 4, 2: 4}`、`last_position=(1.0, 2.0)`），
#: 所以不能按空格切，只能「切到下一个 `键=` 之前」。
_STATUS_RE = re.compile(r"(\w+)=(.*?)(?=\s+\w+=|$)")


def status(account=ACCOUNT):
    """服务端权威状态，解析成 dict。账号没登录 / 没有连接时返回 {}。"""
    text = control("status --user %s" % account)
    if not text or text.startswith("err"):
        return {}
    if text.startswith("ok "):
        text = text[3:]
    return {k: v.strip() for k, v in _STATUS_RE.findall(text.strip())}


def room_type(account=ACCOUNT):
    """当前房间的 `session_type`：2 = 闯关（PVE），其余 = 对战（PVP）；不在房间 = None。"""
    raw = status(account).get("room_type")
    if raw in (None, "None"):
        return None
    return int(raw)


def maps_entered(account=ACCOUNT):
    """客户端已经进过的地图列表 —— 发过 `0x0403 加载完成` 才会变长。"""
    raw = status(account).get("maps_entered", "[]")
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return []


def online(account=ACCOUNT):
    """这个账号在线吗（`who` 说了算）。"""
    return ("'%s'" % account) in control("who") or account in control("who")


# --------------------------------------------------------------------------
# 窗口 / 截图 / 输入
# --------------------------------------------------------------------------

def find_pid():
    pid = wtab.find_pid()
    if pid is None:
        raise DriveError("没找到 BigShot.exe —— 先跑 start.bat")
    return pid


def game_hwnd(pid):
    """游戏主窗口。登录前这里拿到的是登录框（530×527），登录后才是 1024×768 那个。"""
    return clicktool.game_window(pid)


def client_size(hwnd):
    r = W.RECT()
    u32.GetClientRect(W.HWND(hwnd), C.byref(r))
    return r.right, r.bottom


def in_lobby_window(pid):
    """主窗口已经是游戏画面（而不是登录框）了吗 —— 按客户区宽度判。"""
    hwnd = game_hwnd(pid)
    if not hwnd:
        return False
    w, h = client_size(hwnd)
    return w >= 800 and h >= 600


def client_origin(hwnd):
    """客户区左上角在**窗口矩形**里的像素偏移 (dx, dy)。

    ★ 截图像素 ≠ 点击坐标：截的是整个窗口矩形（含标题栏和边框），
      `click.py` 用的是客户区坐标。V0.1 把这个差记成写死的 (3, 29)，
      这里改成**算出来** —— 换个 Windows 主题 / DPI 就不是那两个数了。
    """
    wr = W.RECT()
    u32.GetWindowRect(W.HWND(hwnd), C.byref(wr))
    pt = W.POINT(0, 0)
    u32.ClientToScreen(W.HWND(hwnd), C.byref(pt))
    return pt.x - wr.left, pt.y - wr.top


def shot(name, pid=None):
    """抓一张游戏窗口截图存进 `logs/e2e-shots/`，返回文件路径。

    ★ 用**屏幕 BitBlt**（`grab`）而不是 `grab_window` —— 后者走 `GetWindowDC`，
      对 D3D9 独占绘制的游戏主窗口抓出来是全黑的（只有 GDI 对话框能用）。
      代价是窗口必须真的在前台、没被挡住。
    """
    os.makedirs(SHOT_DIR, exist_ok=True)
    pid = pid or find_pid()
    hwnd = game_hwnd(pid)
    if not hwnd:
        return None
    path = os.path.join(SHOT_DIR, "%s-%s.png" % (time.strftime("%H%M%S"), name))
    shottool.bring_to_front(hwnd)
    r = W.RECT()
    u32.GetWindowRect(W.HWND(hwnd), C.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    shottool.write_png(path, w, h, shottool.grab(r.left, r.top, w, h))
    return path


def focus(pid=None):
    hwnd = game_hwnd(pid or find_pid())
    if hwnd:
        clicktool.focus(hwnd)
    return hwnd


def ensure_foreground(pid=None, fuse=20.0):
    """确认游戏窗口**真的**是前台窗口，不是就抢回来。

    ★ `keybd_event` 发的是全局按键，落到**当前前台窗口**上。只要有别的窗口
      （真人在敲的编辑器、弹出来的通知）抢走了焦点，按键就悄悄打到别处去了
      —— 聊天框里一个字都不会出现，而这件事从游戏那一侧完全看不出来。
      2026-09-20 就是这么丢掉一串 `/r` 的。所以发按键前必须先核一次。
    """
    hwnd = game_hwnd(pid or find_pid())
    if not hwnd:
        raise DriveError("没有游戏窗口")
    deadline = time.monotonic() + fuse
    while time.monotonic() < deadline:
        if u32.GetForegroundWindow() == hwnd:
            return hwnd
        clicktool.focus(hwnd)
        time.sleep(0.3)
    raise DriveError("抢不到游戏窗口的前台焦点（%.0f 秒）—— 有别的窗口在抢？" % fuse)


def click_at(x, y, pid=None):
    """点客户区坐标 (x, y)。★ 截图像素 ≠ 客户区坐标，换算见 `client_origin()`。"""
    hwnd = ensure_foreground(pid)
    clicktool.click(hwnd, x, y)


MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


def drag_select(start, target, pid=None, steps=6):
    """「按住箭头 → 拖到条目 → 松开」地选一个下拉项。

    ★ 这个客户端的下拉框**只在按住左键时展开**，一松手就收起来
      —— 所以「点一下箭头再点条目」永远选不中（V0.1 会话 20 实测）。
      中间分几步挪是为了让它收到 `WM_MOUSEMOVE`、把高亮跟过来；
      一步瞬移过去有些控件不认。
    """
    hwnd = game_hwnd(pid or find_pid())
    if not hwnd:
        raise DriveError("没有游戏窗口可操作")
    clicktool.focus(hwnd)
    sx, sy = clicktool.to_screen(hwnd, *start)
    tx, ty = clicktool.to_screen(hwnd, *target)
    u32.SetCursorPos(sx, sy)
    time.sleep(0.25)
    u32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.35)
    for i in range(1, steps + 1):
        u32.SetCursorPos(sx + (tx - sx) * i // steps, sy + (ty - sy) * i // steps)
        time.sleep(0.06)
    time.sleep(0.2)
    u32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.4)


def press(vk, pid=None):
    """给游戏窗口发一次按键（VK 用十六进制整数）。先确认它在前台。"""
    ensure_foreground(pid)
    tap(vk)


# --------------------------------------------------------------------------
# 等待：一律轮询事实，保险丝只防挂死
# --------------------------------------------------------------------------

def wait_for(pred, what, fuse=90.0, poll=0.4, on_fail=None):
    """轮询 `pred()` 直到为真。超过保险丝就带现场信息抛 `DriveError`。"""
    deadline = time.monotonic() + fuse
    last = None
    while time.monotonic() < deadline:
        try:
            last = pred()
        except DriveError:
            raise
        except Exception as err:          # 轮询期间读不到是常态，别把它当失败
            last = None
            _ = err
        if last:
            return last
        time.sleep(poll)
    extra = ""
    if on_fail:
        try:
            extra = "\n现场：%s" % on_fail()
        except Exception:
            pass
    raise DriveError("等「%s」超过保险丝 %.0f 秒还没等到。%s" % (what, fuse, extra))


# --------------------------------------------------------------------------
# 登录
# --------------------------------------------------------------------------

def _send_timeout(hwnd, msg, wparam, lparam):
    got = C.c_size_t(0)
    return u32.SendMessageTimeoutW(W.HWND(hwnd), msg, wparam, lparam,
                                   SMTO_ABORTIFHUNG, SEND_TIMEOUT_MS, C.byref(got))


def login(account=ACCOUNT, password=PASSWORD, pid=None):
    """把测试账号登进去。已经在线就直接返回。"""
    pid = pid or find_pid()
    if online(account):
        return pid
    dlg = wait_for(lambda: (gui_probe.find_dialogs(pid) or [None])[0],
                   "客户端登录框出现", fuse=180.0)
    if not u32.IsWindowVisible(dlg):
        u32.ShowWindow(dlg, 9)
    u32.SetForegroundWindow(dlg)
    children = gui_probe.children(dlg)
    edits = [h for h, c, i, t in children if c.lower() == "edit"]
    buttons = {i: h for h, c, i, t in children if c.lower() == "button"}
    if 1011 in buttons:                       # 分区单选：本机服务器
        _send_timeout(buttons[1011], BM_CLICK, 0, None)
    if len(edits) < 2:
        raise DriveError("登录框里找不到用户名/密码两个 Edit（找到 %d 个）" % len(edits))
    bu = C.create_unicode_buffer(account)
    bp = C.create_unicode_buffer(password)
    _send_timeout(edits[0], WM_SETTEXT, 0, C.addressof(bu))
    _send_timeout(edits[1], WM_SETTEXT, 0, C.addressof(bp))
    if 1006 not in buttons:
        raise DriveError("登录框里没有「开始」按钮（id=1006）")
    _send_timeout(buttons[1006], BM_CLICK, 0, None)
    wait_for(lambda: online(account), "账号 %s 出现在服务端 who 里" % account, fuse=120.0)
    # 进大厅之后客户端才会跑那次 `WeaponTable::Load`（§36）—— 表里有记录才算真到位。
    wait_for(lambda: wtab.snapshot(pid)["info"]["records"] > 0,
             "客户端把 weapon.ini 解析进武器表", fuse=180.0)
    return pid


# --------------------------------------------------------------------------
# 界面坐标与 stage 号 —— 本会话在 1024×768 的真客户端上逐个量出来的
# --------------------------------------------------------------------------

#: 全是**客户区**坐标。`click.py` 每次现算屏幕坐标，所以窗口被挪动也不受影响。
#: ⚠ 换分辨率就得重量 —— `client_size()` 不是 1024×768 时直接报错，别硬点。
COORD = {
    "notice_ok":    (760, 705),   # 登录后「活动」公告窗的「确认」
    "tab_pvp":      (71, 135),    # 大厅「对战」标签
    "tab_quest":    (165, 135),   # 大厅「任务」标签
    "create_room":  (117, 650),   # 「建立房间」
    "ok_quest":     (570, 555),   # 闯关建房对话框的「确认」
    "ok_pvp":       (590, 555),   # 对战建房对话框的「确认」（比闯关那个靠右）
    "pvp_mode":     (538, 476),   # 对战建房「游戏模式」下拉箭头
    "pvp_mode_free": (516, 537),  # 展开后的第二项「个人战」
    "back":         (144, 728),   # 「后退」（★ 左边 60 那个是「主菜单」，别点错）
    "chat":         (347, 414),   # 待机房间的聊天输入框
    "dialog_ok":    (628, 430),   # 居中错误框的「确认」
}
CLIENT_SIZE = (1024, 768)

VK_ENTER, VK_ESC, VK_F5 = 0x0D, 0x1B, 0x74

#: ★★ 铁律 10 的**那个例外**：房里**人员一有变动**（自己刚进房 / 刚建房、有人或 bot
#: 进出、准备状态翻转、一局打完回到待机房），客户端会锁住「游戏开始」**3 秒**，这 3 秒是**写死在客户端里的**
#: （用户 2026-09-20 指出），不是机器快慢的问题。锁着的时候 F5 被静默吞掉：
#: 按钮照样是亮的、bot 的「准备中」照样在、服务端也照样是 `wait_start`
#: —— **三侧都看不出来，没有任何事件可等**，所以这里只能睡。
#: ⚠ 千万别改成「没反应就再按一次」：多按不会让它提前解锁，只会把真人的键盘刷爆。
ROSTER_SETTLE_S = 3.5

#: `[App+0x54]`（App = `[0x72e2a4]`）就是当前 stage（抄 `tools/probe_room_ctx.py`）。
#: 本会话实测：4 = 大厅、5 = 待机房间、7 = 关卡里。6 是加载中。
APP_VA = 0x72E2A4
APP_STAGE = 0x54
STAGE_LOBBY, STAGE_ROOM, STAGE_LEVEL = 4, 5, 7

#: ASCII → 虚拟键码。只够敲 bot 命令（`/a`、`/r`、`/t 2`），不做中文。
_VK_OF = {"/": 0xBF, " ": 0x20}
for _c in "0123456789":
    _VK_OF[_c] = 0x30 + int(_c)
for _c in "abcdefghijklmnopqrstuvwxyz":
    _VK_OF[_c] = 0x41 + (ord(_c) - ord("a"))


#: 待机房间六个座位在 `LobbyStage` 里的布局（抄 `tools/probe_room_seats.py`），
#: 外加本会话 diff 出来的 **`+0x2e` = 「这一格准备好了」**（发 `/r` 前后只有它翻转）。
#: 判 bot 准备好没有靠它，**不靠看画面上那个绿色 Ready**。
LOBBY_VA = 0x72E29C
SEAT_BASE, SEAT_STRIDE, SEAT_COUNT = 0x40, 0x3C, 6
SEAT_OCCUPIED, SEAT_READY = 0x00, 0x2E


def stage(pid=None):
    """客户端现在在哪个界面。读内存，不看画面。"""
    with wtab.Mem(pid or find_pid()) as m:
        app = m.u32(APP_VA) or 0
        return m.u32(app + APP_STAGE) if app else None


def seats(pid=None):
    """六个座位的 (占用, 准备)。座位 0 是房主 —— 房主永远不「准备」，那格恒为 0。"""
    out = []
    with wtab.Mem(pid or find_pid()) as m:
        lobby = m.u32(LOBBY_VA) or 0
        if not lobby:
            return [(0, 0)] * SEAT_COUNT
        for i in range(SEAT_COUNT):
            raw = m.read(lobby + SEAT_BASE + i * SEAT_STRIDE, SEAT_STRIDE)
            out.append((raw[SEAT_OCCUPIED], raw[SEAT_READY]) if raw else (0, 0))
    return out


#: 座位的队伍号：组队战里房主 = 1、第二个座位 = 2；**个人战两边都是 0**。
#: ★ 实测得来：组队战房里 seat0/seat1 逐字节对比，只有这一格是 (1, 2)。
#:   （`tools/probe_room_seats.py` 里记成 `+0x08 ?(byte)` 的就是它。）
SEAT_TEAM = 0x08


def seat_team(index, pid=None):
    """某个座位的队伍号。读不到返回 None。"""
    with wtab.Mem(pid or find_pid()) as m:
        lobby = m.u32(LOBBY_VA) or 0
        if not lobby:
            return None
        raw = m.read(lobby + SEAT_BASE + index * SEAT_STRIDE, SEAT_STRIDE)
        return raw[SEAT_TEAM] if raw else None


def guests_ready(pid=None):
    """除房主以外**有人的座位是不是全都准备好了**（对战房开局的前提）。"""
    rows = seats(pid)[1:]
    taken = [r for r in rows if r[0]]
    return bool(taken) and all(r[1] for r in taken)


def tap(vk):
    u32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.04)
    u32.keybd_event(vk, 0, 2, 0)
    time.sleep(0.06)


def type_chat(text, confirm=None, pid=None):
    """在待机房间的聊天框里敲一行并回车（只支持 ASCII，够发 bot 命令了）。

    ★ 每个字符之前都重新确认游戏窗口在前台 —— 否则真人一切窗口，后半截就打到
      别的地方去了，而聊天框里看不出少了什么（2026-09-20 丢过一串 `/r`）。
    ★ `confirm` 是「这条命令生效了没有」的判据（比如 bot 进房了 / 准备好了）。
      **必须传** —— 打进去 ≠ 提交成功：实测点开聊天框后的第一发回车会被吞掉，
      字还留在输入框里，而画面和服务端都一声不吭。所以这里按 `confirm` 判，
      没生效就**补一发回车**（空输入框上再按一次无害），再不行就带截图失败。
    """
    click_at(*COORD["chat"], pid=pid)
    time.sleep(0.4)
    for ch in text:
        vk = _VK_OF.get(ch.lower())
        if vk is None:
            raise DriveError("聊天框打不出这个字符：%r" % ch)
        ensure_foreground(pid)
        tap(vk)
    ensure_foreground(pid)
    tap(VK_ENTER)
    if confirm is None:
        time.sleep(0.6)
        return
    try:
        wait_for(confirm, "聊天命令 %r 生效" % text, fuse=6.0, poll=0.3)
        return
    except DriveError:
        pass
    ensure_foreground(pid)
    tap(VK_ENTER)
    wait_for(confirm, "聊天命令 %r 生效（补了一发回车）" % text, fuse=12.0, poll=0.3,
             on_fail=lambda: "座位=%s 截图=%s" % (seats(pid), shot("chat-failed", pid)))


def check_resolution(pid=None):
    hwnd = game_hwnd(pid or find_pid())
    size = client_size(hwnd) if hwnd else None
    if size != CLIENT_SIZE:
        raise DriveError("坐标表是按 %dx%d 量的，当前客户区是 %s —— 先把分辨率调回去"
                         % (CLIENT_SIZE[0], CLIENT_SIZE[1], size))


def dismiss_notice(pid=None):
    """关掉登录后那个「活动」公告窗。

    ★ **只在大厅里点**：同一个坐标落在待机房间时是底部工具条那一片，
      乱点会开出一个不该开的面板，后面的步骤全乱。已经在房间里 = 公告窗
      早就关过了，什么都不用做。
    """
    if stage(pid) != STAGE_LOBBY:
        return
    click_at(*COORD["notice_ok"], pid=pid)


def goto_lobby(pid=None):
    """确保人在大厅（在房间里就退出来）。"""
    if room_type() is not None:
        click_at(*COORD["back"], pid=pid)
        wait_for(lambda: room_type() is None, "退回大厅", fuse=60.0)
    wait_for(lambda: stage(pid) == STAGE_LOBBY, "stage 回到大厅", fuse=60.0)


#: 支持的房型。`pvp` = 个人战（最省事）；`pvp_team` = **组队战**，两队人数必须
#: 相等（客户端 `0x468495` 数人头），所以 bot 要用 `/t 2` 换到另一队做 1v1。
ROOM_MODES = ("quest", "pvp", "pvp_team")


def create_room(mode, pid=None):
    """建一个房间：`quest`（闯关 / PVE）/ `pvp`（个人战）/ `pvp_team`（组队战）。

    ★ 对战房**单人开不了局**：客户端要求「半数以上玩家处于准备状态」，
      所以建完顺手用聊天命令 `/a` 叫一个 bot 进来（准备交给 `ensure_can_start()`，
      因为准备状态每局都会被清掉）。bot 的加载完成由服务端代报（`gameserver.py:10261`）。
    """
    if mode not in ROOM_MODES:
        raise DriveError("房间类型只能是 %s，给的是 %r" % ("/".join(ROOM_MODES), mode))
    goto_lobby(pid)
    click_at(*COORD["tab_quest" if mode == "quest" else "tab_pvp"], pid=pid)
    time.sleep(0.6)
    click_at(*COORD["create_room"], pid=pid)
    time.sleep(1.0)
    if mode == "pvp":
        # 建房对话框默认就是「组队战」—— 两队人数不等开不了局，所以个人战要换掉。
        # ★ 这个客户端的下拉框只在**按住**时展开，所以必须拖着选。
        drag_select(COORD["pvp_mode"], COORD["pvp_mode_free"], pid=pid)
    click_at(*COORD["ok_quest" if mode == "quest" else "ok_pvp"], pid=pid)
    want = SESSION_TYPE_QUEST if mode == "quest" else SESSION_TYPE_NORMAL
    wait_for(lambda: room_type() == want, "建好 %s 房（room_type=%d）" % (mode, want),
             fuse=60.0, on_fail=lambda: control("rooms"))
    if mode != "quest":
        type_chat("/a", confirm=lambda: seats(pid)[1][0], pid=pid)
    if mode == "pvp_team":
        # ★ 组队战**默认就把前两个座位分在两队**（实测房主 = 1 队蓝、bot = 2 队红），
        #   所以不用发 `/t`——只要核一下确实是 1v1 就行。
        #   （顺便：`/t 2` 这条命令**发不出去** —— 聊天框走输入法，空格会被 IME 吃掉
        #    变成「/t/他2」，而 `parse_command` 又必须靠空格分参数。）
        t0, t1 = seat_team(0, pid), seat_team(1, pid)
        if not (t0 and t1 and t0 != t1):
            raise DriveError("组队战没分成两队（座位队伍号 = %s / %s）—— 这样开不了局" % (t0, t1))
    return want


def ensure_can_start(mode, pid=None):
    """开局前把「开得了局」的前提凑齐。

    对战房要求「半数以上玩家处于准备状态」⇒ 除房主外的座位必须都准备好。
    ★ **准备状态每局开始时会被服务端清掉**（`gameserver.py:10312 room.clear_ready()`），
      所以**每一局开打前都要重新 `/r`**，不是建房时按一次就完事。
      `/r` 是**开关**，所以先读 `[座位+0x2e]` 确认到底准备了没有，别盲发。
    """
    if mode.startswith("pvp") and not guests_ready(pid):
        type_chat("/r", confirm=lambda: guests_ready(pid), pid=pid)
    # ★ 铁律 10 的那个例外，理由见 ROSTER_SETTLE_S。**两种房都要等** ——
    #   「刚进房 / 刚建房」本身就算一次人员变动，闯关房虽然没有 bot 也一样锁。
    #   放在这里（紧挨着 F5 之前）就能一次盖住「建房→开局」和「收局→再开局」两条路。
    time.sleep(ROSTER_SETTLE_S)


def start_round(mode="quest", pid=None, fuse=180.0):
    """按 F5 开局，等到**真的进了关卡**（stage 7）才返回。

    ★ 必须等到 stage 7 再读内存：开局那一发 `0x0F01` 在 `0x0400` 之前就写过一次，
      但进图时 `WeaponTable::Load` 和闯关的额外 ini 合并（§44）会重新解析整张表、
      hook 再施加一次。早读就漏掉了这两条路径 —— 而那正是最容易出错的地方。

    ★★ **只按一次 F5。** 按不动就当场失败并留下截图 —— 不要反复补按：多按几下
      既不会让客户端提前解锁，还会把真人的键盘刷爆（2026-09-20 就干过一次，
      用户差点没法打字）。开得了局的前提由 `ensure_can_start()` 事先凑齐。
    """
    ensure_can_start(mode, pid)
    press(VK_F5, pid=pid)
    wait_for(lambda: stage(pid) == STAGE_LEVEL, "开局并进到关卡里", fuse=fuse,
             on_fail=lambda: "stage=%s status=%s 截图=%s"
                             % (stage(pid), status().get("start_game"),
                                shot("start-failed", pid)))


def end_round(mode, pid=None, fuse=180.0):
    """结束这一局并回到待机房间。

    ★ **不要用 `gs_ctl back-to-room`**：它只把客户端切回 stage 5，客户端因此
      再也不会发 `0x0405`「结算界面看完了」，服务端那边房间**永远停在「游戏中」**，
      下一次 F5 会被静默拒掉。正确做法是让客户端自己走完结算
      —— `clear`/`endgame` 之后它在结算页停几秒就会自己发 `0x0405`。
    """
    control(("clear" if mode == "quest" else "endgame") + " --user " + ACCOUNT)
    ensure_foreground(pid)   # stage 切换在主循环里做，窗口不在前台就几乎不跑
    wait_for(lambda: stage(pid) == STAGE_ROOM and "待机中" in control("rooms"),
             "结算完回到待机房间", fuse=fuse,
             on_fail=lambda: "stage=%s rooms=%s 截图=%s"
                             % (stage(pid), control("rooms"), shot("end-failed", pid)))
    # ★ 回到待机房间也算一次「人员变动」，客户端照样锁 3 秒（见 ROSTER_SETTLE_S）。
    time.sleep(ROSTER_SETTLE_S)


def main(argv=None):
    ap = argparse.ArgumentParser(description="驱动真客户端")
    ap.add_argument("cmd", choices=["login", "shot", "status", "who", "size",
                                    "stage", "room", "start", "finish", "lobby"])
    ap.add_argument("rest", nargs="*")
    args = ap.parse_args(argv)
    pid = find_pid()
    if args.cmd == "login":
        login(pid=pid)
        print("已登录，pid =", pid)
    elif args.cmd == "shot":
        print("截图:", shot(args.rest[0] if args.rest else "shot", pid))
    elif args.cmd == "status":
        for k, v in status().items():
            print("%-20s %s" % (k, v))
    elif args.cmd == "who":
        print(control("who"))
    elif args.cmd == "size":
        hwnd = game_hwnd(pid)
        print("hwnd=%s client=%s" % (hex(hwnd) if hwnd else None,
                                     client_size(hwnd) if hwnd else None))
    elif args.cmd == "stage":
        print("stage =", stage(pid))
    elif args.cmd == "lobby":
        goto_lobby(pid)
        print("已在大厅")
    elif args.cmd == "room":
        print("建好，room_type =", create_room(args.rest[0] if args.rest else "quest", pid))
    elif args.cmd == "start":
        start_round(args.rest[0] if args.rest else "quest", pid)
        print("已进关卡")
    elif args.cmd == "finish":
        end_round(args.rest[0] if args.rest else "quest", pid)
        print("已回房间")
    return 0


if __name__ == "__main__":
    sys.exit(main())
