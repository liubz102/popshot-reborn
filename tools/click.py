#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
click.py —— 点游戏画面里的按钮

大厅/游戏内的 UI 是 D3D 自绘的，**不是 Win32 控件**，
所以 `gui_probe.py` 那套 EnumChildWindows + BM_CLICK 完全用不上。
只能真的把鼠标挪过去点（SetCursorPos + mouse_event），坐标是窗口客户区坐标。

用法：
    python tools/click.py <pid> <x> <y> [<x2> <y2> ...]     # 依次点击
    python tools/click.py <pid> --move <x> <y>              # 只移动不点（看 hover 效果）
    python tools/click.py <pid> --key <VK十六进制>           # 发一次按键
    python tools/click.py <pid> --hold <VK十六进制> <秒>      # 按住一段时间再松开
                                                            # （走路/开火这类要按住的操作）

注意：会真的抢走鼠标焦点。截图前先用 tools/screenshot.py 把窗口拉到前台。
"""
import ctypes as C
import sys
import time
from ctypes import wintypes as W

u32 = C.WinDLL("user32", use_last_error=True)

EnumWindowsProc = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
u32.EnumWindows.argtypes = [EnumWindowsProc, W.LPARAM]
u32.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
u32.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
u32.ClientToScreen.argtypes = [W.HWND, C.POINTER(W.POINT)]

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
KEYEVENTF_KEYUP = 0x0002


def game_window(pid):
    """找该进程里最大的可见窗口，跳过 d3d9 自建的 D3DProxyWindow"""
    found = []

    def cb(h, _):
        p = W.DWORD()
        u32.GetWindowThreadProcessId(h, C.byref(p))
        if p.value == pid and u32.IsWindowVisible(h):
            cn = C.create_unicode_buffer(256)
            u32.GetClassNameW(h, cn, 256)
            if cn.value == "D3DProxyWindow":
                return True
            if u32.IsIconic(h):
                u32.ShowWindow(h, 9)          # SW_RESTORE
                time.sleep(0.6)
            r = W.RECT()
            u32.GetWindowRect(h, C.byref(r))
            if r.right - r.left > 100 and r.bottom - r.top > 100:
                found.append((h, (r.right - r.left) * (r.bottom - r.top)))
        return True

    u32.EnumWindows(EnumWindowsProc(cb), 0)
    if not found:
        return None
    return max(found, key=lambda t: t[1])[0]


def focus(hwnd):
    HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
    flags = 0x0002 | 0x0001 | 0x0040      # NOMOVE | NOSIZE | SHOWWINDOW
    try:
        u32.SwitchToThisWindow(W.HWND(hwnd), True)
    except Exception:
        pass
    u32.SetWindowPos(W.HWND(hwnd), W.HWND(HWND_TOPMOST), 0, 0, 0, 0, flags)
    u32.SetForegroundWindow(W.HWND(hwnd))
    time.sleep(0.5)
    u32.SetWindowPos(W.HWND(hwnd), W.HWND(HWND_NOTOPMOST), 0, 0, 0, 0, flags)
    time.sleep(0.2)


def to_screen(hwnd, x, y):
    pt = W.POINT(x, y)
    u32.ClientToScreen(W.HWND(hwnd), C.byref(pt))
    return pt.x, pt.y


def click(hwnd, x, y):
    sx, sy = to_screen(hwnd, x, y)
    u32.SetCursorPos(sx, sy)
    time.sleep(0.25)
    u32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.08)
    u32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    print(f"点击 客户区({x},{y}) -> 屏幕({sx},{sy})")
    time.sleep(0.6)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return
    pid = int(sys.argv[1])
    hwnd = game_window(pid)
    if not hwnd:
        print(f"pid {pid} 没找到可见的游戏窗口")
        return
    print(f"游戏窗口 hwnd={hwnd:#x}")
    focus(hwnd)
    args = sys.argv[2:]
    if args[0] == "--move":
        sx, sy = to_screen(hwnd, int(args[1]), int(args[2]))
        u32.SetCursorPos(sx, sy)
        print(f"移动到 屏幕({sx},{sy})")
        return
    if args[0] == "--key":
        # 一律按十六进制解析（文档就是这么写的）。用 int(x, 0) 的话不带 0x 前缀的
        # "74" 会被当成十进制 74 = VK 0x4a，静默按错键 —— 会话 11 踩过一次。
        vk = int(args[1], 16)
        u32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.05)
        u32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        print(f"按键 VK={vk:#x}")
        return
    if args[0] == "--hold":
        # 走路/持续开火要的是「按住」。单次 keybd_event 按下即弹起，角色只挪一帧。
        vk = int(args[1], 16)
        secs = float(args[2]) if len(args) > 2 else 1.0
        end = time.time() + secs
        u32.keybd_event(vk, 0, 0, 0)
        # 键盘自动重复：Windows 不会替我们发，游戏又是按「键是否按下」轮询的，
        # 所以只要保持按下状态、别提前弹起就行。这里只是等。
        while time.time() < end:
            time.sleep(0.02)
        u32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        print(f"按住 VK={vk:#x} {secs} 秒")
        return
    for i in range(0, len(args) - 1, 2):
        click(hwnd, int(args[i]), int(args[i + 1]))


if __name__ == "__main__":
    main()
