#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gui_probe.py —— 探测/操作炮炮火枪手登录对话框（用 ctypes 直接调 Win32）

用法：
    python tools/gui_probe.py enum <pid>
        列出该进程所有可见 #32770 对话框的全部子控件（class/id/text/hwnd）

    python tools/gui_probe.py login <pid> <user> <pass>
        往登录框填用户名/密码，然后点「开始」按钮，触发连服务器
"""
import sys, ctypes as C
from ctypes import wintypes as W

u32 = C.WinDLL("user32", use_last_error=True)

EnumWindowsProc = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
u32.EnumWindows.argtypes = [EnumWindowsProc, W.LPARAM]
u32.EnumChildWindows.argtypes = [W.HWND, EnumWindowsProc, W.LPARAM]
u32.GetClassNameW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
u32.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
u32.GetWindowTextW.restype = C.c_int
u32.GetDlgCtrlID.argtypes = [W.HWND]
u32.GetDlgCtrlID.restype = C.c_int
u32.IsWindowVisible.argtypes = [W.HWND]
u32.ShowWindow.argtypes = [W.HWND, C.c_int]
u32.SetForegroundWindow.argtypes = [W.HWND]
u32.SendMessageW.argtypes = [W.HWND, C.c_uint, W.WPARAM, W.LPARAM]
u32.SendMessageW.restype = C.c_long
u32.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]

class POINT(C.Structure):
    _fields_ = [("x", C.c_long), ("y", C.c_long)]

u32.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
u32.GetClientRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
u32.ScreenToClient.argtypes = [W.HWND, C.POINTER(POINT)]

WM_SETTEXT = 0x000C
BM_CLICK   = 0x00F5

def cls(h):
    b = C.create_unicode_buffer(256); u32.GetClassNameW(h, b, 256); return b.value
def txt(h):
    b = C.create_unicode_buffer(1024); u32.GetWindowTextW(h, b, 1024); return b.value
def pid_of(h):
    p = W.DWORD(0); u32.GetWindowThreadProcessId(h, C.byref(p)); return p.value

def find_dialogs(pid, visible_only=False):
    dialogs = []
    def cb(h, l):
        if ((pid == 0 or pid_of(h) == pid) and cls(h) == "#32770"
                and (not visible_only or u32.IsWindowVisible(h))):
            dialogs.append(h)
        return True
    u32.EnumWindows(EnumWindowsProc(cb), 0)
    # 登录框优先于可能同时存在的隐藏辅助对话框。
    return sorted(dialogs, key=lambda h: (not bool(u32.IsWindowVisible(h)), txt(h) != "PopShot"))

def children(h):
    out = []
    def cb(hh, l):
        out.append((hh, cls(hh), u32.GetDlgCtrlID(hh), txt(hh)))
        return True
    u32.EnumChildWindows(h, EnumWindowsProc(cb), 0)
    return out

def rect_in_parent(child, parent):
    """子控件相对父窗口客户区的 (左, 上, 宽, 高)。

    改登录框文案时要知道每个控件占多宽：换成更长的中文会被裁掉，
    到底要不要连带把控件加宽，得看它右边还有没有空地（V0.2 里程碑 H）。
    """
    r = W.RECT()
    if not u32.GetWindowRect(child, C.byref(r)):
        return None
    pt = POINT(r.left, r.top)
    u32.ScreenToClient(parent, C.byref(pt))
    return (pt.x, pt.y, r.right - r.left, r.bottom - r.top)

def cmd_enum(pid):
    dlgs = find_dialogs(pid)
    if not dlgs:
        print("没找到 #32770 对话框（pid=%d）" % pid); return
    for d in dlgs:
        r = W.RECT()
        u32.GetClientRect(d, C.byref(r))
        print("== 对话框 hwnd=0x%X visible=%d 客户区=%dx%d title=%r ==" %
              (d, bool(u32.IsWindowVisible(d)), r.right, r.bottom, txt(d)))
        for hh, c, i, t in children(d):
            box = rect_in_parent(hh, d)
            where = ("x=%-4d y=%-4d w=%-4d h=%-3d" % box) if box else "(取不到位置)"
            print("   hwnd=0x%08X id=%-6d class=%-16s %s text=%r"
                  % (hh, i, c, where, t))

def cmd_login(pid, user, pw):
    dlgs = find_dialogs(pid)
    if not dlgs:
        print("没找到登录框"); return
    d = dlgs[0]
    if not u32.IsWindowVisible(d):
        u32.ShowWindow(d, 9)  # SW_RESTORE
    u32.SetForegroundWindow(d)
    ch = children(d)
    edits = [(hh, i) for hh, c, i, t in ch if c.lower() == "edit"]
    btns  = [(hh, i, t) for hh, c, i, t in ch if c.lower() == "button"]
    print("Edit 控件:", [(hex(h), i) for h, i in edits])
    # 先选分区（炮火连天 id=1011），保险
    for hh, i, t in btns:
        if i == 1011:
            u32.SendMessageW(hh, BM_CLICK, 0, 0)
            print("已选分区 id=1011")
    # 填账号/密码：WM_SETTEXT 会被系统跨进程 marshal，传本进程缓冲地址即可
    if len(edits) >= 2:
        bu = C.create_unicode_buffer(user)
        bp = C.create_unicode_buffer(pw)
        u32.SendMessageW(edits[0][0], WM_SETTEXT, 0, C.addressof(bu))
        u32.SendMessageW(edits[1][0], WM_SETTEXT, 0, C.addressof(bp))
        print("已填 用户名=%r 密码=%r" % (user, pw))
    start = None
    for h, i, t in btns:
        if i == 1006: start = h          # id=1006 = 开始
    if start:
        print("点击「开始」 hwnd=0x%X (id=1006)" % start)
        u32.SendMessageW(start, BM_CLICK, 0, 0)
    else:
        print("没找到「开始」按钮 id=1006")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(0)
    mode, pid = sys.argv[1], int(sys.argv[2])
    if mode == "enum":
        cmd_enum(pid)
    elif mode == "login":
        cmd_login(pid, sys.argv[3] if len(sys.argv) > 3 else "test",
                       sys.argv[4] if len(sys.argv) > 4 else "test")
    else:
        print("unknown mode", mode)
