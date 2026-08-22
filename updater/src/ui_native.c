/* --------------------------------------------------------------------------
   ui_native.c —— 渲染链第 3 档：原生 Win32 回退界面（系统没有 WebOC 或
   前两档全失败时用）。外观不追求复刻，但双进度条/状态文字/取消确认按钮
   语义一致 —— 更新绝不断在 UI 上。CONFIRM/MESSAGE 模态直接用 MessageBox。
   -------------------------------------------------------------------------- */
#define WIN32_LEAN_AND_MEAN
#define _CRT_SECURE_NO_WARNINGS
#include <windows.h>
#include <commctrl.h>
#include <wchar.h>
#include <string.h>
#include "ui_internal.h"
#include "ui_window.h"

#pragma comment(lib, "comctl32.lib")

static const int MARGIN = 16;

void ui_native_build(struct UiWindow *win)
{
    HINSTANCE inst = (HINSTANCE)GetWindowLongPtrW(win->hwnd, GWLP_HINSTANCE);
    /* PATCH 窗：双进度条 + 状态行 + 底部按钮。CONFIRM/MESSAGE 模态走
       MessageBox（见 ui_window.c），不进这里。 */
    win->nat_current_lab = CreateWindowW(L"STATIC", L"目前",
        WS_CHILD | WS_VISIBLE, MARGIN, MARGIN + 4, 40, 20,
        win->hwnd, NULL, inst, NULL);
    win->nat_current = CreateWindowW(PROGRESS_CLASSW, NULL,
        WS_CHILD | WS_VISIBLE, MARGIN + 48, MARGIN, win->width - MARGIN * 2 - 48, 22,
        win->hwnd, NULL, inst, NULL);
    win->nat_total_lab = CreateWindowW(L"STATIC", L"全部",
        WS_CHILD | WS_VISIBLE, MARGIN, MARGIN + 34, 40, 20,
        win->hwnd, NULL, inst, NULL);
    win->nat_total = CreateWindowW(PROGRESS_CLASSW, NULL,
        WS_CHILD | WS_VISIBLE, MARGIN + 48, MARGIN + 30, win->width - MARGIN * 2 - 48, 22,
        win->hwnd, NULL, inst, NULL);
    win->nat_status = CreateWindowW(L"STATIC", L"正在检查更新…",
        WS_CHILD | WS_VISIBLE, MARGIN, MARGIN + 72, win->width - MARGIN * 2, 56,
        win->hwnd, NULL, inst, NULL);
    win->nat_remaining = CreateWindowW(L"STATIC", L"",
        WS_CHILD | WS_VISIBLE, MARGIN, MARGIN + 136, win->width - MARGIN * 2, 20,
        win->hwnd, NULL, inst, NULL);
    win->nat_cancel = CreateWindowW(L"BUTTON", L"取消",
        WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON,
        win->width - MARGIN - 55 - 70, win->height - 34, 55, 24,
        win->hwnd, (HMENU)2, inst, NULL);
    win->nat_confirm = CreateWindowW(L"BUTTON", L"确认",
        WS_CHILD | BS_PUSHBUTTON,          /* 初始隐藏，收尾时显示 */
        win->width - MARGIN - 55 - 70, win->height - 34, 55, 24,
        win->hwnd, (HMENU)3, inst, NULL);
}

void ui_native_on_size(struct UiWindow *win)
{
    (void)win;        /* 尺寸固定，无需重排 */
}

int ui_native_command(struct UiWindow *win, WPARAM wp)
{
    int id = (int)LOWORD(wp);
    if (id == 2) {                    /* 取消按钮 */
        if (win->kind == UI_WIN_PATCH) {
            /* 复用 external 的语义：交给同一个按钮处理函数 */
            if (win->mode == UI_MODE_NATIVE) {
                SendMessageW(win->hwnd, WM_APP + 0x51, 0, (LPARAM)L"btnCancel");
            }
            return 1;
        }
        win->result_button = UI_BTN_CANCEL;
        return 1;
    }
    if (id == 3) {
        if (win->kind == UI_WIN_PATCH) {
            SendMessageW(win->hwnd, WM_APP + 0x51, 0, (LPARAM)L"btnConfirm");
            return 1;
        }
        win->result_button = UI_BTN_CONFIRM;
        return 1;
    }
    return 0;
}

void ui_native_status(struct UiWindow *win, const wchar_t *text)
{
    if (win->nat_status) SetWindowTextW(win->nat_status, text);
}

void ui_native_progress_total(struct UiWindow *win, int percent)
{
    if (win->nat_total)
        SendMessageW(win->nat_total, PBM_SETPOS, (WPARAM)percent, 0);
}

void ui_native_progress_current(struct UiWindow *win, int percent)
{
    if (win->nat_current)
        SendMessageW(win->nat_current, PBM_SETPOS, (WPARAM)percent, 0);
}

void ui_native_remaining(struct UiWindow *win, const wchar_t *text)
{
    if (win->nat_remaining) SetWindowTextW(win->nat_remaining, text ? text : L"");
}

void ui_native_swap_button(struct UiWindow *win)
{
    if (win->nat_cancel) ShowWindow(win->nat_cancel, SW_HIDE);
    if (win->nat_confirm) ShowWindow(win->nat_confirm, SW_SHOW);
}

void ui_native_announce(struct UiWindow *win, const wchar_t *ver_text)
{
    wchar_t line[128];
    if (ver_text && *ver_text)
        _snwprintf(line, 128, L"发现新版本 %s，正在自动更新…", ver_text);
    else
        wcscpy(line, L"正在检查更新，请稍候…");
    line[127] = 0;
    ui_native_status(win, line);
}
