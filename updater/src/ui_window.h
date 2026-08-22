/* --------------------------------------------------------------------------
   ui_window.h —— 更新器 UI 门面：原版模板（IE 控件）渲染 + 原生回退。

   渲染链（吸取上一版 res:// 翻车的教训，三段逐级回退）：
     1. 素材解到 %TEMP%\popshot-ui-<pid>\，Navigate file:///（主选）
     2. about:blank + document.write，图片内嵌 data: URI（回退 1）
     3. 原生 Win32 控件窗口（回退 2，系统没有 WebOC 时用；更新不中断）
   --ui-mode 1/2/3 强制指定某一档（验证用）。

   线程模型：所有 ui_* 门面函数线程安全（worker 线程 PostMessage 到 UI
   线程执行）；模态框（确认管理员/消息框）阻塞调用方直到玩家点按钮。
   -------------------------------------------------------------------------- */
#ifndef UPDATER_UI_WINDOW_H
#define UPDATER_UI_WINDOW_H

#include <windows.h>
#include <oleidl.h>
#include "util.h"
#include "ui_external.h"

enum UiWindowKind { UI_WIN_PATCH, UI_WIN_CONFIRM, UI_WIN_MESSAGE };
enum UiButton     { UI_BTN_NONE = 0, UI_BTN_CLOSE, UI_BTN_CANCEL,
                    UI_BTN_CONFIRM, UI_BTN_CONTINUE, UI_BTN_STOP };
enum UiRenderMode { UI_MODE_AUTO = 0, UI_MODE_IE_FILE = 1,
                    UI_MODE_IE_WRITE = 2, UI_MODE_NATIVE = 3 };
enum UiStage      { UI_STAGE_CHECK = 0, UI_STAGE_DOWNLOAD,
                    UI_STAGE_APPLY, UI_STAGE_FINAL };

struct UiWindow;

/* ---- 生命周期（UI 线程） ----------------------------------------------- */

int  ui_init(const wchar_t *package_root, int forced_mode, int noui);
void ui_shutdown(void);
const wchar_t *ui_temp_dir(void);
/* selftest 用：嵌入资源是否都能 FindResource 到。返回缺的个数。 */
int  ui_missing_resources(void);
/* 主窗口（原版 PATCH 模板）。返回 0 失败（调用方退 NOUI 模式）。 */
int  ui_window_create_patch(void);
void ui_pump_until_quit(HANDLE worker_thread);
void ui_request_quit(int exit_code);
void ui_set_stage(int stage);

/* ---- 线程安全门面（worker 线程调用） ----------------------------------- */

void ui_status(const wchar_t *text);
void ui_progress_total(int percent);
void ui_progress_current(int percent);
void ui_remaining(const wchar_t *text);        /* NULL = 隐藏剩余时间行 */
void ui_swap_button(void);                     /* 取消 -> 确认 */
void ui_announce_version(const wchar_t *ver_text);
void ui_announce_error(const wchar_t *detail); /* 失败详情进公告区 */
int  ui_confirm_admin(void);                   /* 模态：1=继续 0=取消 */
int  ui_message_box(const wchar_t *text, int show_cancel, int alert_icon);
int  ui_cancel_requested(void);                /* 读者：worker 的下载回调 */

#endif /* UPDATER_UI_WINDOW_H */
