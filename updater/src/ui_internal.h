/* --------------------------------------------------------------------------
   ui_internal.h —— ui_window.c / ui_native.c 共享的窗口结构。
   -------------------------------------------------------------------------- */
#ifndef UPDATER_UI_INTERNAL_H
#define UPDATER_UI_INTERNAL_H

#include <windows.h>
#include <oleidl.h>
#include <exdisp.h>
#include "ui_window.h"
#include "ui_external.h"

struct UiWindow {
    HWND hwnd;
    int kind;                      /* UiWindowKind */
    int mode;                      /* 实际生效：UiRenderMode */
    int width, height;
    /* IE 控件宿主（mode = IE_FILE / IE_WRITE） */
    void             *site;        /* HostSite（ui_window.c 私有） */
    IOleObject       *ole;
    IOleInPlaceObject *inplace;
    IWebBrowser2     *browser;
    UiExternal external;
    /* 原生回退控件（mode = NATIVE） */
    HWND nat_current, nat_total;
    HWND nat_current_lab, nat_total_lab;
    HWND nat_status, nat_remaining;
    HWND nat_cancel, nat_confirm;
    /* 模态结果（CONFIRM / MESSAGE） */
    int result_button;
};

/* ui_native.c：原生回退界面的建控件 / 布局 / 命令路由 / 状态应用。 */
void ui_native_build(struct UiWindow *win);
void ui_native_on_size(struct UiWindow *win);
int  ui_native_command(struct UiWindow *win, WPARAM wp);   /* 命中返回 1 */
void ui_native_status(struct UiWindow *win, const wchar_t *text);
void ui_native_progress_total(struct UiWindow *win, int percent);
void ui_native_progress_current(struct UiWindow *win, int percent);
void ui_native_remaining(struct UiWindow *win, const wchar_t *text);
void ui_native_swap_button(struct UiWindow *win);
void ui_native_announce(struct UiWindow *win, const wchar_t *ver_text);

#endif /* UPDATER_UI_INTERNAL_H */
