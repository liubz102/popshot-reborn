/* --------------------------------------------------------------------------
   ui_external.h —— window.external 的 IDispatch（页面按钮/拖动回调宿主）。

   打过补丁的原版模板里注入了：
     onclick="window.external.OnButton('btnClose');return false;"
     onmousedown="window.external.DragMove()"
   这里实现 OnButton / DragMove 两个方法（GetIDsOfNames 按名字发 dispid）。
   原版 NGMDll 用 COM 事件下沉接点击 —— 我们走 external 回调，效果一样。
   -------------------------------------------------------------------------- */
#ifndef UPDATER_UI_EXTERNAL_H
#define UPDATER_UI_EXTERNAL_H

#include <windows.h>
#include <oleidl.h>
#include <oaidl.h>

typedef void (*ui_ext_button_fn)(void *user, const wchar_t *button_id);
typedef void (*ui_ext_drag_fn)(void *user);

typedef struct UiExternal {
    /* 首成员必须是 vtbl 指针 —— 结构体地址即可直接当 IDispatch* 用。 */
    const IDispatchVtbl *lpVtbl;
    LONG refs;
    void *user;
    ui_ext_button_fn on_button;
    ui_ext_drag_fn on_drag;
} UiExternal;

/* 初始化（vtbl 绑定 + 回调）。之后 p 可直接强转成 IDispatch* 交给
   IDocHostUIHandler::GetExternal。 */
void ui_external_init(UiExternal *p, void *user,
                      ui_ext_button_fn on_button, ui_ext_drag_fn on_drag);

#endif /* UPDATER_UI_EXTERNAL_H */
