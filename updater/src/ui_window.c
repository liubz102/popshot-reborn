/* --------------------------------------------------------------------------
   ui_window.c —— 见 ui_window.h。

   职责：
     * 素材落盘：内嵌 RCDATA（原版模板/图片，见 updater.rc）-> %TEMP%\popshot-ui-<pid>\
     * 窗口：WS_POPUP 无标题栏（模板自带边框美术和右上关闭按钮）
     * IE 控件宿主：纯 C 的 OLE 样板（IOleClientSite / IOleInPlaceSite /
       IOleInPlaceFrame / IOleControlSite / IDocHostUIHandler，不用 ATL）
     * 渲染链：file://（主选）-> about:blank+document.write+data:URI（回退 1）
       -> ui_native.c 原生控件（回退 2）
     * worker 线程门面：PostMessage 到 UI 线程，模态框带事件回执
   -------------------------------------------------------------------------- */
#define WIN32_LEAN_AND_MEAN
#define _CRT_SECURE_NO_WARNINGS
#include <windows.h>
#include <ole2.h>
#include <oleauto.h>
#include <exdisp.h>
#include <mshtmhst.h>
#include <mshtml.h>
#include <commctrl.h>
#include <stdio.h>
#include <wchar.h>
#include <wctype.h>
#include <string.h>
#include <stdlib.h>
#include "ui_window.h"
#include "ui_internal.h"
#include "ui_external.h"
#include "util.h"
#include "log.h"

#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "oleaut32.lib")
#pragma comment(lib, "uuid.lib")

/* ---- worker -> UI 的私有消息 ------------------------------------------ */
#define UM_STATUS       (WM_APP + 1)     /* lparam = heap wchar*   */
#define UM_PROG_TOTAL   (WM_APP + 2)     /* wparam = percent       */
#define UM_PROG_CURRENT (WM_APP + 3)     /* wparam = percent       */
#define UM_REMAINING    (WM_APP + 4)     /* lparam = heap wchar* / NULL 隐藏 */
#define UM_SWAP_BUTTON  (WM_APP + 5)
#define UM_ANNOUNCE     (WM_APP + 6)     /* lparam = heap wchar*（目标版本）*/
#define UM_ANNOUNCE_ERR (WM_APP + 8)     /* lparam = heap wchar*（失败原因）*/
#define UM_MODAL        (WM_APP + 7)     /* lparam = ModalReq*     */
#define UM_BUTTON       (WM_APP + 0x51)  /* lparam = 静态 wchar*（按钮 id） */

/* ---- 模态请求（worker -> UI 线程，带事件回执） -------------------------- */
typedef struct ModalReq {
    int kind;                    /* 0 = 管理员确认，1 = 消息框 */
    wchar_t text[600];
    int show_cancel;
    int alert_icon;              /* 0=无图标 1=警示(caution) 2=错误(cancel) */
    int result;                  /* UI_BTN_* 或 confirm 的 0/1 */
    HANDLE done;
} ModalReq;

/* ---- 资源表：名字与 updater.rc 一一对应（selftest 盯住） ---------------- */
typedef struct UiResMap {
    const wchar_t *res;      /* RCDATA 资源名（= 原版 DLL 资源名） */
    const wchar_t *rel;      /* 临时目录内的落盘相对路径 */
} UiResMap;

static const UiResMap UI_RESOURCES[] = {
    { L"TEMPLATE_PATCH.HTML",           L"TEMPLATE_PATCH.HTML" },
    { L"TEMPLATE_CONFIRMRUNADMIN.HTML", L"TEMPLATE_CONFIRMRUNADMIN.HTML" },
    { L"TEMPLATE_MESSAGEL.HTML",        L"TEMPLATE_MESSAGEL.HTML" },
    { L"GLOBAL.CSS",                    L"global.css" },
    { L"IMG_DEFAULT.JPG",               L"IMG_DEFAULT.JPG" },
    { L"BG_BAR.GIF",           L"BINARY\\BG_BAR.GIF" },
    { L"BG_BAR_ON.GIF",        L"BINARY\\BG_BAR_ON.GIF" },
    { L"BG_BODY.GIF",          L"BINARY\\BG_BODY.GIF" },
    { L"BG_SMALL_PROGRESS.GIF",L"BINARY\\BG_SMALL_PROGRESS.GIF" },
    { L"BG_TITLE.GIF",         L"BINARY\\BG_TITLE.GIF" },
    { L"BT_AGREE_NOT.GIF",     L"BINARY\\BT_AGREE_NOT.GIF" },
    { L"BT_AGREE_STIP.GIF",    L"BINARY\\BT_AGREE_STIP.GIF" },
    { L"BT_CANCEL.GIF",        L"BINARY\\BT_CANCEL.GIF" },
    { L"BT_CANCEL_S.GIF",      L"BINARY\\BT_CANCEL_S.GIF" },
    { L"BT_CFM_S.GIF",         L"BINARY\\BT_CFM_S.GIF" },
    { L"BT_CHANGE_FOLDER.GIF", L"BINARY\\BT_CHANGE_FOLDER.GIF" },
    { L"BT_CLOSE.GIF",         L"BINARY\\BT_CLOSE.GIF" },
    { L"BT_INSTALL.GIF",       L"BINARY\\BT_INSTALL.GIF" },
    { L"BUL_ARR_GR.GIF",       L"BINARY\\BUL_ARR_GR.GIF" },
    { L"BUL_CIRCLE_BL.GIF",    L"BINARY\\BUL_CIRCLE_BL.GIF" },
    { L"BUL_DOT_GR.GIF",       L"BINARY\\BUL_DOT_GR.GIF" },
    { L"ICO_CANCEL.GIF",       L"BINARY\\ICO_CANCEL.GIF" },
    { L"ICO_CAUTION.GIF",      L"BINARY\\ICO_CAUTION.GIF" },
    { L"LN_SPACE.GIF",         L"BINARY\\LN_SPACE.GIF" },
    { L"ROUND1_BTM1.GIF",      L"BINARY\\ROUND1_BTM1.GIF" },
    { L"ROUND1_BTM2.GIF",      L"BINARY\\ROUND1_BTM2.GIF" },
    { L"ROUND1_BTM3.GIF",      L"BINARY\\ROUND1_BTM3.GIF" },
    { L"ROUND1_BTM4.GIF",      L"BINARY\\ROUND1_BTM4.GIF" },
    { L"ROUND1_TOP1.GIF",      L"BINARY\\ROUND1_TOP1.GIF" },
    { L"ROUND1_TOP2.GIF",      L"BINARY\\ROUND1_TOP2.GIF" },
    { L"ROUND1_TOP3.GIF",      L"BINARY\\ROUND1_TOP3.GIF" },
    { L"ROUND1_TOP4.GIF",      L"BINARY\\ROUND1_TOP4.GIF" },
    { L"ROUND2_BTM1.GIF",      L"BINARY\\ROUND2_BTM1.GIF" },
    { L"ROUND2_BTM2.GIF",      L"BINARY\\ROUND2_BTM2.GIF" },
    { L"ROUND2_BTM3.GIF",      L"BINARY\\ROUND2_BTM3.GIF" },
    { L"ROUND2_BTM4.GIF",      L"BINARY\\ROUND2_BTM4.GIF" },
    { L"ROUND2_TOP1.GIF",      L"BINARY\\ROUND2_TOP1.GIF" },
    { L"ROUND2_TOP2.GIF",      L"BINARY\\ROUND2_TOP2.GIF" },
    { L"ROUND2_TOP3.GIF",      L"BINARY\\ROUND2_TOP3.GIF" },
    { L"ROUND2_TOP4.GIF",      L"BINARY\\ROUND2_TOP4.GIF" },
    { L"ROUND3_BTM1.GIF",      L"BINARY\\ROUND3_BTM1.GIF" },
    { L"ROUND3_BTM2.GIF",      L"BINARY\\ROUND3_BTM2.GIF" },
    { L"ROUND3_TOP1.GIF",      L"BINARY\\ROUND3_TOP1.GIF" },
    { L"ROUND3_TOP2.GIF",      L"BINARY\\ROUND3_TOP2.GIF" },
    { L"TIANCITY_LOGO.GIF",    L"BINARY\\TIANCITY_LOGO.GIF" },
    { L"TIANCITY_LOGO2.GIF",   L"BINARY\\TIANCITY_LOGO2.GIF" },
};
#define UI_RESOURCE_COUNT (sizeof(UI_RESOURCES) / sizeof(UI_RESOURCES[0]))

/* ---- 全局状态 ---------------------------------------------------------- */
static wchar_t g_root[MAX_PATH * 2];
static wchar_t g_ui_dir[MAX_PATH * 2];       /* %TEMP%\popshot-ui-<pid> */
static struct UiWindow *g_main_wnd;
static int g_mode_forced = UI_MODE_AUTO;
static int g_noui;
static volatile LONG g_cancel;
static volatile LONG g_stage = UI_STAGE_CHECK;
static volatile LONG g_exit_code;
static int g_ole_inited;
static int g_com_inited;
static int g_class_registered;
static const wchar_t *WC_UPDATER = L"PopShotUpdaterWnd";

int ui_cancel_requested(void) { return g_cancel; }
void ui_set_stage(int stage) { InterlockedExchange(&g_stage, (LONG)stage); }

/* ====================================================================== */
/*  COM 宿主对象（五个接口聚合进一个结构体，各自 vtbl，公共引用计数）        */
/* ====================================================================== */

typedef struct HostSite {
    IOleClientSite    clientSite;      /* 每个成员的首字段都是 vtbl 槽 */
    IOleInPlaceSite   inPlaceSite;
    IOleInPlaceFrame  inPlaceFrame;
    IOleControlSite   controlSite;
    IDocHostUIHandler docHost;
    LONG refs;
    struct UiWindow *win;
} HostSite;

#define SITE_FROM(iface, member) \
    ((HostSite *)((char *)(iface) - offsetof(HostSite, member)))

static void site_qi(HostSite *s, REFIID riid, void **out)
{
    *out = NULL;
    if (IsEqualIID(riid, &IID_IUnknown) ||
        IsEqualIID(riid, &IID_IOleClientSite)) {
        *out = &s->clientSite;
    } else if (IsEqualIID(riid, &IID_IOleInPlaceSite)) {
        *out = &s->inPlaceSite;
    } else if (IsEqualIID(riid, &IID_IOleWindow) ||
               IsEqualIID(riid, &IID_IOleInPlaceUIWindow) ||
               IsEqualIID(riid, &IID_IOleInPlaceFrame)) {
        *out = &s->inPlaceFrame;
    } else if (IsEqualIID(riid, &IID_IOleControlSite)) {
        *out = &s->controlSite;
    } else if (IsEqualIID(riid, &IID_IDocHostUIHandler)) {
        *out = &s->docHost;
    } else {
        return;
    }
    InterlockedIncrement(&s->refs);
}

#define SITE_QI(iface, member)                                              \
static HRESULT STDMETHODCALLTYPE site_##member##_qi(                       \
        iface *self, REFIID riid, void **out)                               \
{                                                                           \
    if (!out) return E_POINTER;                                             \
    site_qi(SITE_FROM(self, member), riid, out);                            \
    return *out ? S_OK : E_NOINTERFACE;                                     \
}                                                                           \
static ULONG STDMETHODCALLTYPE site_##member##_addref(iface *self)          \
{ return (ULONG)InterlockedIncrement(&SITE_FROM(self, member)->refs); }     \
static ULONG STDMETHODCALLTYPE site_##member##_release(iface *self)          \
{ return (ULONG)InterlockedDecrement(&SITE_FROM(self, member)->refs); }

SITE_QI(IOleClientSite, clientSite)
SITE_QI(IOleInPlaceSite, inPlaceSite)
SITE_QI(IOleInPlaceFrame, inPlaceFrame)
SITE_QI(IOleControlSite, controlSite)
SITE_QI(IDocHostUIHandler, docHost)

/* IOleClientSite -------------------------------------------------------- */

static HRESULT STDMETHODCALLTYPE cs_save(IOleClientSite *s)
{ (void)s; return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE cs_get_moniker(IOleClientSite *s, DWORD a,
    DWORD b, IMoniker **m)
{ (void)s; (void)a; (void)b; (void)m; return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE cs_get_container(IOleClientSite *s,
    IOleContainer **c)
{ (void)s; (void)c; return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE cs_show_object(IOleClientSite *s)
{ (void)s; return S_OK; }
static HRESULT STDMETHODCALLTYPE cs_on_show_window(IOleClientSite *s, BOOL f)
{ (void)s; (void)f; return S_OK; }
static HRESULT STDMETHODCALLTYPE cs_request_new_layout(IOleClientSite *s)
{ (void)s; return E_NOTIMPL; }

static IOleClientSiteVtbl g_cs_vtbl = {
    site_clientSite_qi, site_clientSite_addref, site_clientSite_release,
    cs_save, cs_get_moniker, cs_get_container, cs_show_object,
    cs_on_show_window, cs_request_new_layout
};

/* IOleInPlaceSite ------------------------------------------------------- */

static HRESULT STDMETHODCALLTYPE ips_get_window(IOleInPlaceSite *s, HWND *h)
{
    HostSite *hs = SITE_FROM(s, inPlaceSite);
    if (!h) return E_POINTER;
    *h = hs->win->hwnd;
    return S_OK;
}
static HRESULT STDMETHODCALLTYPE ips_ctx_help(IOleInPlaceSite *s, BOOL e)
{ (void)s; (void)e; return S_OK; }
static HRESULT STDMETHODCALLTYPE ips_can_inplace(IOleInPlaceSite *s)
{ (void)s; return S_OK; }
static HRESULT STDMETHODCALLTYPE ips_on_inplace_activate(IOleInPlaceSite *s)
{ (void)s; return S_OK; }
static HRESULT STDMETHODCALLTYPE ips_on_ui_activate(IOleInPlaceSite *s)
{ (void)s; return S_OK; }
static HRESULT STDMETHODCALLTYPE ips_get_window_ctx(IOleInPlaceSite *s,
    IOleInPlaceFrame **frame, IOleInPlaceUIWindow **doc,
    LPRECT rect, LPRECT clip, LPOLEINPLACEFRAMEINFO info)
{
    HostSite *hs = SITE_FROM(s, inPlaceSite);
    RECT rc;
    if (frame) {
        *frame = &hs->inPlaceFrame;
        InterlockedIncrement(&hs->refs);
    }
    if (doc) {
        *doc = (IOleInPlaceUIWindow *)&hs->inPlaceFrame;
        InterlockedIncrement(&hs->refs);
    }
    GetClientRect(hs->win->hwnd, &rc);
    if (rect) *rect = rc;
    if (clip) *clip = rc;
    if (info) {
        info->fMDIApp = FALSE;
        info->hwndFrame = hs->win->hwnd;
        info->haccel = NULL;
        info->cAccelEntries = 0;
    }
    return S_OK;
}
static HRESULT STDMETHODCALLTYPE ips_discard_state(IOleInPlaceSite *s)
{ (void)s; return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE ips_on_ui_deactivate(IOleInPlaceSite *s,
    BOOL f)
{ (void)s; (void)f; return S_OK; }
static HRESULT STDMETHODCALLTYPE ips_on_inplace_deactivate(IOleInPlaceSite *s)
{ (void)s; return S_OK; }
static HRESULT STDMETHODCALLTYPE ips_deactivate_undo(IOleInPlaceSite *s)
{ (void)s; return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE ips_scroll(IOleInPlaceSite *s, SIZE e)
{ (void)s; (void)e; return S_OK; }
static HRESULT STDMETHODCALLTYPE ips_on_pos_rect(IOleInPlaceSite *s,
    LPCRECT r)
{ (void)s; (void)r; return S_OK; }

static IOleInPlaceSiteVtbl g_ips_vtbl = {
    site_inPlaceSite_qi, site_inPlaceSite_addref, site_inPlaceSite_release,
    ips_get_window, ips_ctx_help, ips_can_inplace, ips_on_inplace_activate,
    ips_on_ui_activate, ips_get_window_ctx, ips_scroll, ips_on_ui_deactivate,
    ips_on_inplace_deactivate, ips_discard_state, ips_deactivate_undo,
    ips_on_pos_rect
};

/* IOleInPlaceFrame / IOleInPlaceUIWindow -------------------------------- */

static HRESULT STDMETHODCALLTYPE ipf_get_window(IOleInPlaceFrame *s, HWND *h)
{
    HostSite *hs = SITE_FROM(s, inPlaceFrame);
    if (!h) return E_POINTER;
    *h = hs->win->hwnd;
    return S_OK;
}
static HRESULT STDMETHODCALLTYPE ipf_ctx_help(IOleInPlaceFrame *s, BOOL e)
{ (void)s; (void)e; return S_OK; }
static HRESULT STDMETHODCALLTYPE ipf_get_border(IOleInPlaceFrame *s, LPRECT r)
{ (void)s; (void)r; return S_OK; }
static HRESULT STDMETHODCALLTYPE ipf_req_border(IOleInPlaceFrame *s,
    LPCBORDERWIDTHS b)
{ (void)s; (void)b; return S_OK; }
static HRESULT STDMETHODCALLTYPE ipf_set_border(IOleInPlaceFrame *s,
    LPCBORDERWIDTHS b)
{ (void)s; (void)b; return S_OK; }
static HRESULT STDMETHODCALLTYPE ipf_set_active(IOleInPlaceFrame *s,
    IOleInPlaceActiveObject *o, LPCOLESTR n)
{ (void)s; (void)o; (void)n; return S_OK; }
static HRESULT STDMETHODCALLTYPE ipf_insert_menus(IOleInPlaceFrame *s,
    HMENU h, LPOLEMENUGROUPWIDTHS w)
{ (void)s; (void)h; (void)w; return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE ipf_set_menu(IOleInPlaceFrame *s,
    HMENU m, HOLEMENU hm, HWND hw)
{ (void)s; (void)m; (void)hm; (void)hw; return S_OK; }
static HRESULT STDMETHODCALLTYPE ipf_remove_menus(IOleInPlaceFrame *s,
    HMENU m)
{ (void)s; (void)m; return S_OK; }
static HRESULT STDMETHODCALLTYPE ipf_translate_accel(IOleInPlaceFrame *s,
    LPMSG m, WORD w)
{ (void)s; (void)m; (void)w; return S_FALSE; }
static HRESULT STDMETHODCALLTYPE ipf_set_status(IOleInPlaceFrame *s,
    LPCOLESTR t)
{ (void)s; (void)t; return S_OK; }
static HRESULT STDMETHODCALLTYPE ipf_enable_modeless(IOleInPlaceFrame *s,
    BOOL e)
{ (void)s; (void)e; return S_OK; }

static IOleInPlaceFrameVtbl g_ipf_vtbl = {
    site_inPlaceFrame_qi, site_inPlaceFrame_addref, site_inPlaceFrame_release,
    ipf_get_window, ipf_ctx_help, ipf_get_border, ipf_req_border,
    ipf_set_border, ipf_set_active, ipf_insert_menus, ipf_set_menu,
    ipf_remove_menus, ipf_set_status, ipf_enable_modeless, ipf_translate_accel
};

/* IOleControlSite ------------------------------------------------------- */

static HRESULT STDMETHODCALLTYPE ocs_on_info_changed(IOleControlSite *s)
{ (void)s; return S_OK; }
static HRESULT STDMETHODCALLTYPE ocs_lock_inplace(IOleControlSite *s, BOOL f)
{ (void)s; (void)f; return S_OK; }
static HRESULT STDMETHODCALLTYPE ocs_get_extended(IOleControlSite *s,
    IDispatch **d)
{ (void)s; (void)d; return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE ocs_transform_coords(IOleControlSite *s,
    POINTL *p, POINTF *f, DWORD d)
{ (void)s; (void)p; (void)f; (void)d; return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE ocs_translate_accel(IOleControlSite *s,
    MSG *m, DWORD g)
{ (void)s; (void)m; (void)g; return S_FALSE; }
static HRESULT STDMETHODCALLTYPE ocs_on_focus(IOleControlSite *s, BOOL f)
{ (void)s; (void)f; return S_OK; }
static HRESULT STDMETHODCALLTYPE ocs_show_frame(IOleControlSite *s)
{ (void)s; return E_NOTIMPL; }

static IOleControlSiteVtbl g_ocs_vtbl = {
    site_controlSite_qi, site_controlSite_addref, site_controlSite_release,
    ocs_on_info_changed, ocs_lock_inplace, ocs_get_extended,
    ocs_transform_coords, ocs_translate_accel, ocs_on_focus, ocs_show_frame
};

/* IDocHostUIHandler ----------------------------------------------------- */

static HRESULT STDMETHODCALLTYPE duh_show_context_menu(IDocHostUIHandler *s,
    DWORD a, POINT *p, IUnknown *c, IDispatch *d)
{ (void)s; (void)a; (void)p; (void)c; (void)d; return S_OK; }  /* 屏蔽 IE 右键 */
static HRESULT STDMETHODCALLTYPE duh_get_host_info(IDocHostUIHandler *s,
    DOCHOSTUIINFO *info)
{
    (void)s;
    if (!info) return E_POINTER;
    info->dwFlags |= DOCHOSTUIFLAG_DIALOG |
                     DOCHOSTUIFLAG_DISABLE_HELP_MENU |
                     DOCHOSTUIFLAG_NO3DOUTERBORDER |
                     DOCHOSTUIFLAG_SCROLL_NO;
    return S_OK;
}
static HRESULT STDMETHODCALLTYPE duh_show_ui(IDocHostUIHandler *s, DWORD d,
    IOleInPlaceActiveObject *a, IOleCommandTarget *c, IOleInPlaceFrame *f,
    IOleInPlaceUIWindow *w)
{ (void)s; (void)d; (void)a; (void)c; (void)f; (void)w; return S_OK; }
static HRESULT STDMETHODCALLTYPE duh_hide_ui(IDocHostUIHandler *s)
{ (void)s; return S_OK; }
static HRESULT STDMETHODCALLTYPE duh_update_ui(IDocHostUIHandler *s)
{ (void)s; return S_OK; }
static HRESULT STDMETHODCALLTYPE duh_enable_modeless(IDocHostUIHandler *s, BOOL f)
{ (void)s; (void)f; return S_OK; }
static HRESULT STDMETHODCALLTYPE duh_on_doc_activate(IDocHostUIHandler *s, BOOL f)
{ (void)s; (void)f; return S_OK; }
static HRESULT STDMETHODCALLTYPE duh_on_frame_activate(IDocHostUIHandler *s,
    BOOL f)
{ (void)s; (void)f; return S_OK; }
static HRESULT STDMETHODCALLTYPE duh_resize_border(IDocHostUIHandler *s,
    LPCRECT r, IOleInPlaceUIWindow *w, BOOL f)
{ (void)s; (void)r; (void)w; (void)f; return S_OK; }
static HRESULT STDMETHODCALLTYPE duh_translate_accel(IDocHostUIHandler *s,
    LPMSG m, const GUID *g, DWORD c)
{ (void)s; (void)m; (void)g; (void)c; return S_FALSE; }
static HRESULT STDMETHODCALLTYPE duh_get_option_key_path(IDocHostUIHandler *s,
    LPOLESTR *k, DWORD d)
{ (void)s; (void)k; (void)d; return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE duh_get_drop_target(IDocHostUIHandler *s,
    IDropTarget *t, IDropTarget **o)
{ (void)s; (void)t; (void)o; return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE duh_get_external(IDocHostUIHandler *s,
    IDispatch **out)
{
    HostSite *hs = SITE_FROM(s, docHost);
    if (!out) return E_POINTER;
    *out = (IDispatch *)&hs->win->external;
    hs->win->external.lpVtbl->AddRef((IDispatch *)&hs->win->external);
    return S_OK;
}
static HRESULT STDMETHODCALLTYPE duh_translate_url(IDocHostUIHandler *s,
    DWORD d, OLECHAR *i, OLECHAR **o)
{ (void)s; (void)d; (void)i; (void)o; return S_FALSE; }
static HRESULT STDMETHODCALLTYPE duh_filter_data(IDocHostUIHandler *s,
    IDataObject *d, IDataObject **o)
{ (void)s; (void)d; (void)o; return S_FALSE; }

static IDocHostUIHandlerVtbl g_duh_vtbl = {
    site_docHost_qi, site_docHost_addref, site_docHost_release,
    duh_show_context_menu, duh_get_host_info, duh_show_ui, duh_hide_ui,
    duh_update_ui, duh_enable_modeless, duh_on_doc_activate,
    duh_on_frame_activate, duh_resize_border, duh_translate_accel,
    duh_get_option_key_path, duh_get_drop_target, duh_get_external,
    duh_translate_url, duh_filter_data
};

/* ====================================================================== */
/*  可增长宽字符串 + 大小写不敏感替换（document.write 回退路径用）          */
/* ====================================================================== */

typedef struct WBuf {
    wchar_t *p;
    size_t len, cap;
} WBuf;

static int wbuf_init(WBuf *b, size_t cap)
{
    b->p = (wchar_t *)malloc(cap * sizeof(wchar_t));
    b->cap = cap;
    b->len = 0;
    if (b->p) b->p[0] = 0;
    return b->p != NULL;
}

static void wbuf_free(WBuf *b)
{
    if (b->p) free(b->p);
    b->p = NULL;
    b->len = b->cap = 0;
}

static int wbuf_reserve(WBuf *b, size_t need)
{
    if (b->len + need + 1 <= b->cap) return 1;
    while (b->cap < b->len + need + 1) b->cap *= 2;
    b->p = (wchar_t *)realloc(b->p, b->cap * sizeof(wchar_t));
    return b->p != NULL;
}

static int wbuf_append(WBuf *b, const wchar_t *s)
{
    size_t n = wcslen(s);
    if (!wbuf_reserve(b, n)) return 0;
    wcscpy(b->p + b->len, s);
    b->len += n;
    return 1;
}

static wchar_t *wcsinstr(const wchar_t *hay, const wchar_t *needle)
{
    size_t n = wcslen(needle);
    if (!n) return (wchar_t *)hay;
    for (; *hay; hay++) {
        size_t i = 0;
        while (i < n && towlower(hay[i]) == towlower(needle[i])) i++;
        if (i == n) return (wchar_t *)hay;
    }
    return NULL;
}

static int wbuf_replace_ci(WBuf *b, const wchar_t *find, const wchar_t *repl)
{
    size_t n = wcslen(find);
    size_t rn = wcslen(repl);
    wchar_t *at = b->p;
    int count = 0;
    if (!b->p) return 0;
    while ((at = wcsinstr(at, find)) != NULL) {
        size_t tail = wcslen(at + n);
        if (!wbuf_reserve(b, rn)) return -1;
        memmove(at + rn, at + n, (tail + 1) * sizeof(wchar_t));
        memcpy(at, repl, rn * sizeof(wchar_t));
        at += rn;
        b->len = b->len - n + rn;
        count++;
    }
    return count;
}

/* ====================================================================== */
/*  资源读取 / 素材落盘 / announce.html                                    */
/* ====================================================================== */

static int load_res_bytes(const wchar_t *name, unsigned char **out,
                          size_t *out_len)
{
    HRSRC r = FindResourceW(NULL, name, RT_RCDATA);
    HGLOBAL g;
    void *p;
    DWORD size;
    if (!r) return 0;
    g = LoadResource(NULL, r);
    if (!g) return 0;
    p = LockResource(g);
    size = SizeofResource(NULL, r);
    if (!p || !size) return 0;
    *out = (unsigned char *)malloc(size);
    if (!*out) return 0;
    memcpy(*out, p, size);
    *out_len = size;
    return 1;
}

static int write_file_bytes(const wchar_t *path, const void *data, size_t len)
{
    HANDLE f = CreateFileW(path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                           FILE_ATTRIBUTE_NORMAL, NULL);
    DWORD wrote;
    if (f == INVALID_HANDLE_VALUE) return 0;
    if (!WriteFile(f, data, (DWORD)len, &wrote, NULL) || wrote != len) {
        CloseHandle(f);
        return 0;
    }
    CloseHandle(f);
    return 1;
}

int ui_missing_resources(void)
{
    size_t i;
    int missing = 0;
    for (i = 0; i < UI_RESOURCE_COUNT; i++)
        if (!FindResourceW(NULL, UI_RESOURCES[i].res, RT_RCDATA)) {
            log_line("missing resource %ls", UI_RESOURCES[i].res);
            missing++;
        }
    return missing;
}

static int materialize_ui_dir(void)
{
    size_t i;
    wchar_t base[MAX_PATH], dir[MAX_PATH * 2];

    if (!GetTempPathW(MAX_PATH, base)) return 0;
    {
        wchar_t pid[32];
        u64_to_wide(GetCurrentProcessId(), pid, 32);
        _snwprintf(g_ui_dir, MAX_PATH * 2, L"%spopshot-ui-%s", base, pid);
        g_ui_dir[MAX_PATH * 2 - 1] = 0;
    }
    if (file_exists(g_ui_dir)) delete_tree(g_ui_dir);
    wcscpy(dir, g_ui_dir);
    if (!ensure_dir(dir)) return 0;
    {
        /* BINARY 子目录（模板按 BINARY/xxx.gif 相对路径引用图片）。 */
        wchar_t bin[MAX_PATH * 2];
        path_join(bin, MAX_PATH * 2, g_ui_dir, L"BINARY");
        if (!ensure_dir(bin)) return 0;
    }

    for (i = 0; i < UI_RESOURCE_COUNT; i++) {
        unsigned char *data;
        size_t len;
        wchar_t path[MAX_PATH * 2];
        const wchar_t *rel = UI_RESOURCES[i].rel;
        wchar_t *slash = wcsrchr(rel, L'\\');
        if (slash) {
            wchar_t parent[MAX_PATH * 2];
            wcsncpy(parent, rel, (size_t)(slash - rel));
            parent[slash - rel] = 0;
            path_join(path, MAX_PATH * 2, g_ui_dir, parent);
            if (!ensure_dir(path)) return 0;
        }
        if (!load_res_bytes(UI_RESOURCES[i].res, &data, &len)) return 0;
        path_join(path, MAX_PATH * 2, g_ui_dir, rel);
        {
            int ok = write_file_bytes(path, data, len);
            free(data);
            if (!ok) return 0;
        }
    }
    return 1;
}

const wchar_t *ui_temp_dir(void) { return g_ui_dir; }

/* HTML 转义（& < > -> 实体），错误详情拼进公告页用。 */
static void html_escape(const wchar_t *text, wchar_t *out, size_t cap)
{
    const wchar_t *i = text;
    wchar_t *o = out;
    while (*i && (size_t)(o - out) < cap - 8) {
        if (*i == L'&')      { wcscpy(o, L"&amp;");  o += 5; i++; }
        else if (*i == L'<') { wcscpy(o, L"&lt;");   o += 4; i++; }
        else if (*i == L'>') { wcscpy(o, L"&gt;");   o += 4; i++; }
        else                 { *o++ = *i++; }
    }
    *o = 0;
}

static const wchar_t *ANNOUNCE_CSS =
    L"<style>body{margin:0;padding:0;font:12px dotum,serif;"
    L"color:#757575;background:#fff;overflow:hidden}"
    L"img{border:0;display:block;margin:0 auto}"
    L"p{margin:5px 14px;line-height:18px;text-align:center}"
    L"b{color:#2A96D4;font-weight:normal}"
    L"</style>";

/* 公告区 iframe（加高后 546x332，用户拍板不缩图）：整张 546x300 海报
   原尺寸居中 + 一行文字（~28px），总高 ~328 < 332 不出滚动条。 */
static const wchar_t *ANNOUNCE_IMG =
    L"<img src=\"IMG_DEFAULT.JPG\" width=\"546\" height=\"300\">";

/* 公告页（原版公告 iframe 的内容本来由 NGMDll 运行时填，这里生成自己的）。
   ver_text == NULL/空 -> 建窗时的默认公告（正在检查更新）；
   有版本 -> 发现新版本（真机踩坑修复：公告区不能等到选定目标才填，
   一打开就该有内容，否则中间一大块空白）。 */
static int write_announce_html(const wchar_t *ver_text)
{
    wchar_t wbody[1024];
    char body[2048];
    wchar_t path[MAX_PATH * 2];

    if (ver_text && *ver_text) {
        wchar_t vtext[64];
        wcsncpy(vtext, ver_text, 63);
        vtext[63] = 0;
        _snwprintf(wbody, 1024,
            L"<!DOCTYPE HTML PUBLIC \"-//W3C//DTD HTML 4.01 Transitional//EN\">\n"
            L"<html><head><meta http-equiv=\"Content-Type\" "
            L"content=\"text/html; charset=gb2312\">\n"
            L"%ls</head>\n"
            L"<body>\n"
            L"%ls\n"
            L"<p>发现新版本 <b>%s</b>，正在自动更新……</p>\n"
            L"</body></html>\n", ANNOUNCE_CSS, ANNOUNCE_IMG, vtext);
    } else {
        _snwprintf(wbody, 1024,
            L"<!DOCTYPE HTML PUBLIC \"-//W3C//DTD HTML 4.01 Transitional//EN\">\n"
            L"<html><head><meta http-equiv=\"Content-Type\" "
            L"content=\"text/html; charset=gb2312\">\n"
            L"%ls</head>\n"
            L"<body>\n"
            L"%ls\n"
            L"<p>正在检查更新，请稍候……</p>\n"
            L"</body></html>\n", ANNOUNCE_CSS, ANNOUNCE_IMG);
    }
    wbody[1023] = 0;
    if (wide_to_gbk(wbody, wcslen(wbody), body, sizeof(body)) < 0)
        return 0;
    path_join(path, MAX_PATH * 2, g_ui_dir, L"announce.html");
    return write_file_bytes(path, body, strlen(body));
}

/* 错误公告页（真机踩坑修复：失败详情原来挤在底部 11px 的单行小字里，
   既显示不全也不显眼；现在公告区整块换成错误说明 + 手动下载地址）。 */
static int write_error_announce_html(const wchar_t *detail)
{
    wchar_t wbody[1600];
    char body[3200];
    wchar_t path[MAX_PATH * 2];
    wchar_t esc[768];

    html_escape(detail ? detail : L"", esc, 768);
    _snwprintf(wbody, 1600,
        L"<!DOCTYPE HTML PUBLIC \"-//W3C//DTD HTML 4.01 Transitional//EN\">\n"
        L"<html><head><meta http-equiv=\"Content-Type\" "
        L"content=\"text/html; charset=gb2312\">\n"
        L"<style>body{margin:0;padding:0;font:12px dotum,serif;"
        L"color:#757575;background:#fff;overflow:hidden}"
        L"p{margin:10px 20px;line-height:170%%;text-align:left}"
        L"</style></head>\n"
        L"<body>\n"
        L"<p><b style=\"color:#FE0000\">自动更新没能完成</b></p>\n"
        L"<p>%s</p>\n"
        L"<p>请手动下载完整客户端：</p>\n"
        L"<p><b>https://github.com/liubz102/popshot-reborn/releases</b></p>\n"
        L"</body></html>\n", esc);
    wbody[1599] = 0;
    if (wide_to_gbk(wbody, wcslen(wbody), body, sizeof(body)) < 0)
        return 0;
    path_join(path, MAX_PATH * 2, g_ui_dir, L"announce.html");
    return write_file_bytes(path, body, strlen(body));
}

/* 宽字符版公告正文（document.write 回退路径用 data: URI 装载）。
   无图（素材在临时目录里，回退路径不保证读得到），纯文字居中。 */
static int announce_data_uri(wchar_t *out, size_t cap, const wchar_t *ver_text)
{
    wchar_t wbody[1024];
    char body[2048];
    char b64[2800];

    if (ver_text && *ver_text) {
        wchar_t vtext[64];
        wcsncpy(vtext, ver_text, 63);
        vtext[63] = 0;
        _snwprintf(wbody, 1024,
            L"<html><head><meta http-equiv=\"Content-Type\" "
            L"content=\"text/html; charset=gb2312\">%ls</head>"
            L"<body style=\"padding-top:120px\">"
            L"<p>发现新版本 <b>%s</b></p>"
            L"<p>正在自动更新……</p></body></html>",
            ANNOUNCE_CSS, vtext);
    } else {
        _snwprintf(wbody, 1024,
            L"<html><head><meta http-equiv=\"Content-Type\" "
            L"content=\"text/html; charset=gb2312\">%ls</head>"
            L"<body style=\"padding-top:130px\">"
            L"<p>正在检查更新，请稍候……</p></body></html>",
            ANNOUNCE_CSS);
    }
    wbody[1023] = 0;
    if (wide_to_gbk(wbody, wcslen(wbody), body, sizeof(body)) < 0) return 0;
    {
        int n = base64_encode((const unsigned char *)body, strlen(body), b64,
                              sizeof(b64));
        if (n <= 0) return 0;
    }
    _snwprintf(out, cap, L"data:text/html;base64,%hs", b64);
    out[cap - 1] = 0;
    return 1;
}

/* 错误公告的 data: URI 版（document.write 回退路径）。 */
static int error_announce_data_uri(wchar_t *out, size_t cap,
                                   const wchar_t *detail)
{
    wchar_t wbody[1600];
    char body[3200];
    char b64[4400];
    wchar_t esc[768];

    html_escape(detail ? detail : L"", esc, 768);
    _snwprintf(wbody, 1600,
        L"<html><head><meta http-equiv=\"Content-Type\" "
        L"content=\"text/html; charset=gb2312\">"
        L"<style>body{margin:0;padding:0;font:12px dotum,serif;"
        L"color:#757575;background:#fff;overflow:hidden}"
        L"p{margin:10px 20px;line-height:170%%;text-align:left}"
        L"</style></head>"
        L"<body><p><b style=\"color:#FE0000\">自动更新没能完成</b></p>"
        L"<p>%s</p><p>请手动下载完整客户端：</p>"
        L"<p><b>https://github.com/liubz102/popshot-reborn/releases</b></p>"
        L"</body></html>", esc);
    wbody[1599] = 0;
    if (wide_to_gbk(wbody, wcslen(wbody), body, sizeof(body)) < 0) return 0;
    {
        int n = base64_encode((const unsigned char *)body, strlen(body), b64,
                              sizeof(b64));
        if (n <= 0) return 0;
    }
    _snwprintf(out, cap, L"data:text/html;base64,%hs", b64);
    out[cap - 1] = 0;
    return 1;
}

/* ====================================================================== */
/*  浏览器驱动（execScript / 渲染链 / document.write 回退）                 */
/* ====================================================================== */

static void js_escape(const wchar_t *text, wchar_t *out, size_t cap)
{
    const wchar_t *i = text;
    wchar_t *o = out;
    while (*i && (size_t)(o - out) < cap - 6) {
        if (*i == L'\'' || *i == L'\\' || *i == L'"') {
            *o++ = L'\\';
            *o++ = *i++;
        } else if (*i == L'\r' || *i == L'\n') {
            *o++ = L' ';
            i++;
        } else {
            *o++ = *i++;
        }
    }
    *o = 0;
}

static int exec_js(struct UiWindow *w, const wchar_t *js)
{
    IDispatch *doc_disp = NULL;
    IHTMLDocument2 *doc = NULL;
    IHTMLWindow2 *win2 = NULL;
    VARIANT dummy;
    HRESULT hr = E_FAIL;
    BSTR code, lang;

    if (!w || !w->browser) return 0;
    hr = w->browser->lpVtbl->get_Document(w->browser, &doc_disp);
    if (!SUCCEEDED(hr) || !doc_disp) return 0;
    hr = doc_disp->lpVtbl->QueryInterface(doc_disp, &IID_IHTMLDocument2,
                                          (void **)&doc);
    doc_disp->lpVtbl->Release(doc_disp);
    if (!SUCCEEDED(hr) || !doc) return 0;
    hr = doc->lpVtbl->get_Script(doc, (IDispatch **)(void *)&win2);
    doc->lpVtbl->Release(doc);
    if (!SUCCEEDED(hr) || !win2) return 0;
    code = SysAllocString(js);
    lang = SysAllocString(L"jscript");
    VariantInit(&dummy);
    if (code && lang)
        hr = win2->lpVtbl->execScript(win2, code, lang, &dummy);
    else
        hr = E_OUTOFMEMORY;
    if (code) SysFreeString(code);
    if (lang) SysFreeString(lang);
    VariantClear(&dummy);
    win2->lpVtbl->Release(win2);
    return SUCCEEDED(hr);
}

static int wait_browser_ready(struct UiWindow *w, DWORD timeout_ms)
{
    DWORD start = GetTickCount();
    for (;;) {
        MSG msg;
        READYSTATE rs = READYSTATE_UNINITIALIZED;
        if (w->browser)
            w->browser->lpVtbl->get_ReadyState(w->browser, &rs);
        if (rs == READYSTATE_COMPLETE) {
            IDispatch *d = NULL;
            w->browser->lpVtbl->get_Document(w->browser, &d);
            if (d) {
                d->lpVtbl->Release(d);
                return 1;
            }
        }
        while (PeekMessageW(&msg, NULL, 0, 0, PM_REMOVE)) {
            if (msg.message == WM_QUIT) return 0;
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
        if (GetTickCount() - start > timeout_ms) return 0;
        Sleep(30);
    }
}

static void browser_set_rect(struct UiWindow *w)
{
    RECT rc;
    if (!w->inplace) return;
    GetClientRect(w->hwnd, &rc);
    w->inplace->lpVtbl->SetObjectRects(w->inplace, &rc, &rc);
}

static int browser_navigate(struct UiWindow *w, const wchar_t *url)
{
    VARIANT v;
    BSTR burl = SysAllocString(url);
    int ok;
    if (!burl) return 0;
    VariantInit(&v);
    ok = SUCCEEDED(w->browser->lpVtbl->Navigate(w->browser, burl, &v, &v,
                                                &v, &v));
    SysFreeString(burl);
    VariantClear(&v);
    return ok;
}

/* data: URI 版图片（document.write 回退用）。gif / jpeg 之外不认。 */
static int image_data_uri(const wchar_t *res_name, wchar_t *out, size_t cap)
{
    unsigned char *data;
    size_t len;
    char b64[270000];          /* IMG_DEFAULT.JPG ~200KB -> base64 ~270KB */
    int n;
    const wchar_t *mime = NULL;
    size_t nl = wcslen(res_name);

    if (nl > 4) {
        if (wide_ieq(res_name + nl - 4, L".GIF")) mime = L"image/gif";
        else if (wide_ieq(res_name + nl - 4, L".JPG")) mime = L"image/jpeg";
    }
    if (!mime) return 0;
    if (!load_res_bytes(res_name, &data, &len)) return 0;
    n = base64_encode(data, len, b64, sizeof(b64));
    free(data);
    if (n <= 0) return 0;
    _snwprintf(out, cap, L"data:%ls;base64,%hs", mime, b64);
    out[cap - 1] = 0;
    return 1;
}

/* 把模板里所有 BINARY/xxx 引用换成内嵌 data: URI，<link global.css> 换成
   <style>。返回装配好的宽字符串（调用方 free）。 */
static wchar_t *build_inline_document(const wchar_t *template_res)
{
    unsigned char *raw;
    size_t rawlen;
    WBuf html, css, uri;
    wchar_t wide[65536];
    size_t i;
    wchar_t *link_at, *lt, *gt;

    if (!load_res_bytes(template_res, &raw, &rawlen)) return NULL;
    if (gbk_to_wide((const char *)raw, rawlen, wide, 65536) < 0) {
        free(raw);
        return NULL;
    }
    free(raw);

    if (!wbuf_init(&html, wcslen(wide) + 16)) return NULL;
    wbuf_append(&html, wide);
    if (!wbuf_init(&css, 8192)) { wbuf_free(&html); return NULL; }
    {
        unsigned char *css_raw;
        size_t css_len;
        wchar_t css_w[16384];
        if (load_res_bytes(L"GLOBAL.CSS", &css_raw, &css_len)) {
            if (gbk_to_wide((const char *)css_raw, css_len, css_w, 16384) >= 0)
                wbuf_append(&css, css_w);
            free(css_raw);
        }
    }
    if (!wbuf_init(&uri, 8192)) {
        wbuf_free(&html);
        wbuf_free(&css);
        return NULL;
    }

    /* css 里 url(BINARY/x.gif) 也替换。 */
    for (i = 0; i < UI_RESOURCE_COUNT; i++) {
        const wchar_t *res = UI_RESOURCES[i].res;
        size_t nl = wcslen(res);
        const wchar_t *dot = nl > 4 ? res + nl - 4 : L"";
        if (wide_ieq(dot, L".GIF") || wide_ieq(dot, L".JPG")) {
            wchar_t find_[128];
            int replaced;
            if (!image_data_uri(res, uri.p ? uri.p : L"", uri.cap))
                continue;
            uri.len = wcslen(uri.p);
            _snwprintf(find_, 128, L"BINARY/%ls", res);
            find_[127] = 0;
            replaced = wbuf_replace_ci(&html, find_, uri.p);
            wbuf_replace_ci(&css, find_, uri.p);
            (void)replaced;
        }
    }

    /* <link ... global.css ...> -> <style>…</style>。 */
    link_at = wcsinstr(html.p, L"global.css");
    if (link_at) {
        lt = link_at;
        while (lt > html.p && *lt != L'<') lt--;
        gt = link_at;
        while (*gt && *gt != L'>') gt++;
        if (*lt == L'<' && *gt == L'>') {
            WBuf out;
            size_t head = (size_t)(lt - html.p);
            size_t tail = wcslen(gt);
            if (wbuf_init(&out, html.len + css.len + 32)) {
                wchar_t saved = *lt;
                *lt = 0;
                wbuf_append(&out, html.p);
                *lt = saved;
                wbuf_append(&out, L"<style type=\"text/css\">");
                wbuf_append(&out, css.p ? css.p : L"");
                wbuf_append(&out, L"</style>");
                wbuf_append(&out, gt + 1);
                (void)head; (void)tail;
                wbuf_free(&html);
                wbuf_free(&css);
                wbuf_free(&uri);
                return out.p;
            }
        }
    }
    wbuf_free(&css);
    wbuf_free(&uri);
    return html.p;
}

/* about:blank + document.write 直写（渲染链第 2 档）。 */
static int render_by_write(struct UiWindow *w, const wchar_t *template_res)
{
    wchar_t *doc_html;
    IHTMLDocument2 *doc = NULL;
    IDispatch *doc_disp = NULL;
    SAFEARRAY *psa;
    VARIANT var;
    BSTR text;
    VARIANT v_true, v_null;
    int ok = 0;

    if (!browser_navigate(w, L"about:blank")) return 0;
    if (!wait_browser_ready(w, 5000)) return 0;
    if (FAILED(w->browser->lpVtbl->get_Document(w->browser, &doc_disp)) ||
        !doc_disp)
        return 0;
    if (FAILED(doc_disp->lpVtbl->QueryInterface(doc_disp, &IID_IHTMLDocument2,
                                                (void **)&doc))) {
        doc_disp->lpVtbl->Release(doc_disp);
        return 0;
    }
    doc_disp->lpVtbl->Release(doc_disp);

    doc_html = build_inline_document(template_res);
    if (!doc_html) { doc->lpVtbl->Release(doc); return 0; }

    VariantInit(&v_true);
    v_true.vt = VT_BOOL;
    v_true.boolVal = VARIANT_TRUE;
    VariantInit(&v_null);
    v_null.vt = VT_NULL;
    {
        IDispatch *ret = NULL;
        doc->lpVtbl->open(doc, L"text/html", v_null, v_null, v_true, &ret);
        if (ret) ret->lpVtbl->Release(ret);
    }
    text = SysAllocString(doc_html);
    psa = SafeArrayCreateVector(VT_VARIANT, 0, 1);
    if (text && psa) {
        LONG idx = 0;
        VariantInit(&var);
        var.vt = VT_BSTR;
        var.bstrVal = text;
        SafeArrayPutElement(psa, &idx, &var);
        if (SUCCEEDED(doc->lpVtbl->write(doc, psa)))
            ok = 1;
        VariantClear(&var);
    }
    if (text) SysFreeString(text);
    if (psa) SafeArrayDestroy(psa);
    doc->lpVtbl->close(doc);
    doc->lpVtbl->Release(doc);
    free(doc_html);
    return ok;
}

static const wchar_t *template_res_for(int kind)
{
    switch (kind) {
    case UI_WIN_CONFIRM: return L"TEMPLATE_CONFIRMRUNADMIN.HTML";
    case UI_WIN_MESSAGE: return L"TEMPLATE_MESSAGEL.HTML";
    default:             return L"TEMPLATE_PATCH.HTML";
    }
}

static void window_size_for(int kind, int *w, int *h)
{
    switch (kind) {
    case UI_WIN_CONFIRM: *w = 440; *h = 163; break;
    case UI_WIN_MESSAGE: *w = 440; *h = 133; break;
    default:             *w = 572; *h = 473; break;
    }
}

/* ====================================================================== */
/*  窗口过程 / 按钮回调 / 状态应用                                        */
/* ====================================================================== */

static void on_button(void *user, const wchar_t *id);
static void on_drag(void *user);
static void handle_modal(ModalReq *req);

static void window_apply_status(struct UiWindow *w, const wchar_t *text)
{
    wchar_t esc[1024], js[1400];
    if (w->mode == UI_MODE_NATIVE) { ui_native_status(w, text); return; }
    js_escape(text, esc, 1024);
    if (w->kind == UI_WIN_MESSAGE) {
        _snwprintf(js, 1400,
                   L"document.all.Message.innerHTML='%s'", esc);
    } else {
        _snwprintf(js, 1400,
                   L"document.all.TextCurrent.innerHTML='%s';ShowTextCurrent(1)",
                   esc);
    }
    js[1399] = 0;
    exec_js(w, js);
}

static void window_apply_remaining(struct UiWindow *w, const wchar_t *text)
{
    if (w->mode == UI_MODE_NATIVE) { ui_native_remaining(w, text); return; }
    if (text && *text) {
        wchar_t esc[256], js[600];
        js_escape(text, esc, 256);
        _snwprintf(js, 600,
                   L"document.all.TextRemainingTime.innerHTML='%s';"
                   L"ShowRemainingTime(1)", esc);
        js[599] = 0;
        exec_js(w, js);
    } else {
        exec_js(w, L"ShowRemainingTime(0)");
    }
}

static void window_finish_or_close(struct UiWindow *w)
{
    /* PATCH 窗口：关闭请求（按钮/系统）。写文件阶段拒绝关闭。 */
    if (w->kind == UI_WIN_PATCH) {
        if (g_stage == UI_STAGE_APPLY) {
            window_apply_status(w, L"正在写入文件，请稍候……");
            return;
        }
        InterlockedExchange(&g_cancel, 1);
        DestroyWindow(w->hwnd);
        PostQuitMessage((int)g_exit_code);
        return;
    }
    w->result_button = UI_BTN_CLOSE;
}

static void on_button(void *user, const wchar_t *id)
{
    struct UiWindow *w = (struct UiWindow *)user;
    if (wide_ieq(id, L"btnClose")) {
        window_finish_or_close(w);
        return;
    }
    if (wide_ieq(id, L"btnCancel")) {
        if (w->kind == UI_WIN_MESSAGE) { w->result_button = UI_BTN_CANCEL; return; }
        if (w->kind == UI_WIN_PATCH) {
            if (g_stage == UI_STAGE_APPLY) {
                window_apply_status(w, L"正在写入文件，不能取消，请稍候……");
                return;
            }
            if (g_stage == UI_STAGE_FINAL) return;   /* 收尾态按钮已换 */
            InterlockedExchange(&g_cancel, 1);
            window_apply_status(w, L"正在取消……");
            return;
        }
        return;
    }
    if (wide_ieq(id, L"btnConfirm")) {
        if (w->kind == UI_WIN_PATCH) {
            if (g_stage == UI_STAGE_FINAL) {
                DestroyWindow(w->hwnd);
                PostQuitMessage((int)g_exit_code);
            }
            return;
        }
        w->result_button = UI_BTN_CONFIRM;
        return;
    }
    if (wide_ieq(id, L"btnContinue")) { w->result_button = UI_BTN_CONTINUE; return; }
    if (wide_ieq(id, L"btnStop"))     { w->result_button = UI_BTN_STOP; return; }
}

static void on_drag(void *user)
{
    struct UiWindow *w = (struct UiWindow *)user;
    ReleaseCapture();
    SendMessageW(w->hwnd, WM_NCLBUTTONDOWN, HTCAPTION, 0);
}

static LRESULT CALLBACK UiWndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
{
    struct UiWindow *w = (struct UiWindow *)GetWindowLongPtrW(hwnd,
                                                             GWLP_USERDATA);
    switch (msg) {
    case WM_CREATE:
        return 0;
    case WM_SIZE:
        if (w) {
            browser_set_rect(w);
            ui_native_on_size(w);
        }
        return 0;
    case WM_ERASEBKGND:
        {
            HDC dc = (HDC)wp;
            RECT rc;
            HBRUSH br = (HBRUSH)GetStockObject(WHITE_BRUSH);
            GetClientRect(hwnd, &rc);
            FillRect(dc, &rc, br);
        }
        return 1;
    case WM_CLOSE:
        if (w) window_finish_or_close(w);
        return 0;
    case WM_COMMAND:
        if (w && w->mode == UI_MODE_NATIVE && ui_native_command(w, wp))
            return 0;
        break;
    case WM_GETMINMAXINFO:
        {
            /* 锁死客户区尺寸（模板按像素画的边框）。 */
            MINMAXINFO *mmi = (MINMAXINFO *)lp;
            mmi->ptMaxTrackSize.x = 2000;
            mmi->ptMaxTrackSize.y = 2000;
        }
        return 0;

    case UM_BUTTON:
        if (w) on_button(w, (const wchar_t *)lp);
        return 0;
    case UM_STATUS:
        if (w) window_apply_status(w, (const wchar_t *)lp);
        free((void *)lp);
        return 0;
    case UM_REMAINING:
        if (w) window_apply_remaining(w, (const wchar_t *)lp);
        free((void *)lp);
        return 0;
    case UM_PROG_TOTAL:
        if (w) {
            if (w->mode == UI_MODE_NATIVE)
                ui_native_progress_total(w, (int)wp);
            else {
                wchar_t js[64];
                _snwprintf(js, 64, L"SetProgressTotal(%d)", (int)wp);
                js[63] = 0;
                exec_js(w, js);
            }
        }
        return 0;
    case UM_PROG_CURRENT:
        if (w) {
            if (w->mode == UI_MODE_NATIVE)
                ui_native_progress_current(w, (int)wp);
            else {
                wchar_t js[64];
                _snwprintf(js, 64, L"SetProgressCurrent(%d)", (int)wp);
                js[63] = 0;
                exec_js(w, js);
            }
        }
        return 0;
    case UM_SWAP_BUTTON:
        if (w) {
            if (w->mode == UI_MODE_NATIVE) ui_native_swap_button(w);
            else exec_js(w, L"ChangeButton()");
        }
        return 0;
    case UM_ANNOUNCE:
        if (w) {
            const wchar_t *ver = (const wchar_t *)lp;
            if (w->mode == UI_MODE_NATIVE) {
                ui_native_announce(w, ver);
            } else if (w->mode == UI_MODE_IE_WRITE) {
                /* 回退路径：临时目录可能写不了，公告页走 data: URI。 */
                wchar_t uri[2048], js[4096];
                if (announce_data_uri(uri, 2048, ver)) {
                    _snwprintf(js, 4096, L"ChangeContents('%s')", uri);
                    js[4095] = 0;
                    exec_js(w, js);
                }
            } else {
                write_announce_html(ver);
                exec_js(w, L"ChangeContents('announce.html')");
            }
        }
        free((void *)lp);
        return 0;
    case UM_ANNOUNCE_ERR:
        if (w) {
            const wchar_t *detail = (const wchar_t *)lp;
            if (w->mode == UI_MODE_NATIVE) {
                wchar_t line[128];
                _snwprintf(line, 128, L"更新失败：%ls", detail ? detail : L"?");
                line[127] = 0;
                ui_native_status(w, line);
            } else if (w->mode == UI_MODE_IE_WRITE) {
                wchar_t uri[4096], js[8192];
                if (error_announce_data_uri(uri, 4096, detail)) {
                    _snwprintf(js, 8192, L"ChangeContents('%s')", uri);
                    js[8191] = 0;
                    exec_js(w, js);
                }
            } else {
                write_error_announce_html(detail);
                exec_js(w, L"ChangeContents('announce.html')");
            }
        }
        free((void *)lp);
        return 0;
    case UM_MODAL:
        handle_modal((ModalReq *)lp);
        return 0;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

/* ====================================================================== */
/*  窗口创建（渲染链在这里逐级回退）                                        */
/* ====================================================================== */

/* 高度自适应（真机踩坑二修）：模板注释的设计高（473/163/133）按 2007 年
   IE6 的排版算的，现代 Trident + 字体回退下实际内容高度会多出几像素，
   写死窗口高就会把底部圆角边框裁掉（DOCHOSTUIFLAG_SCROLL_NO 挡住滚动
   直接切）。页面就绪后量 body/documentElement 的 scrollHeight，内容
   更高就加高窗口 —— 对字体差异 / 显示缩放都免疫。设计高是下限。 */
static void window_fit_content(struct UiWindow *w)
{
    IDispatch *disp = NULL;
    IHTMLDocument2 *doc = NULL;
    IHTMLElement *body = NULL;
    IHTMLElement2 *el2 = NULL;
    long body_h = 0, doc_h = 0;
    int want;

    if (!w->browser) return;
    if (FAILED(w->browser->lpVtbl->get_Document(w->browser, &disp)) || !disp)
        return;
    if (FAILED(disp->lpVtbl->QueryInterface(disp, &IID_IHTMLDocument2,
                                            (void **)&doc))) {
        disp->lpVtbl->Release(disp);
        return;
    }
    disp->lpVtbl->Release(disp);

    if (SUCCEEDED(doc->lpVtbl->get_body(doc, &body)) && body) {
        if (SUCCEEDED(body->lpVtbl->QueryInterface(body, &IID_IHTMLElement2,
                                                   (void **)&el2))) {
            el2->lpVtbl->get_scrollHeight(el2, &body_h);
            el2->lpVtbl->Release(el2);
        }
        body->lpVtbl->Release(body);
    }
    {
        /* documentElement 防一手怪异模式下 body 汇报不准。 */
        IHTMLDocument3 *doc3 = NULL;
        if (SUCCEEDED(doc->lpVtbl->QueryInterface(doc, &IID_IHTMLDocument3,
                                                  (void **)&doc3))) {
            IHTMLElement *root = NULL;
            if (SUCCEEDED(doc3->lpVtbl->get_documentElement(doc3, &root)) &&
                root) {
                if (SUCCEEDED(root->lpVtbl->QueryInterface(root,
                        &IID_IHTMLElement2, (void **)&el2))) {
                    el2->lpVtbl->get_scrollHeight(el2, &doc_h);
                    el2->lpVtbl->Release(el2);
                }
                root->lpVtbl->Release(root);
            }
            doc3->lpVtbl->Release(doc3);
        }
    }
    doc->lpVtbl->Release(doc);

    want = body_h > doc_h ? (int)body_h : (int)doc_h;
    if (want < w->height) want = w->height;         /* 不小于设计尺寸 */
    if (want > w->height + 100) want = w->height + 100;   /* 保险丝 */
    {
        RECT rc, wr;
        GetClientRect(w->hwnd, &rc);
        if (want > rc.bottom) {
            GetWindowRect(w->hwnd, &wr);
            SetWindowPos(w->hwnd, NULL, 0, 0,
                         wr.right - wr.left,
                         (wr.bottom - wr.top) + (want - rc.bottom),
                         SWP_NOMOVE | SWP_NOZORDER);
            log_line("ui: fit content height %d -> %d", rc.bottom, want);
        }
    }
}

static struct UiWindow *window_create(int kind)
{
    struct UiWindow *w;
    WNDCLASSEXW wc;
    HINSTANCE inst = GetModuleHandleW(NULL);
    int width, height;

    if (!g_class_registered) {
        ZeroMemory(&wc, sizeof(wc));
        wc.cbSize = sizeof(wc);
        wc.style = CS_HREDRAW | CS_VREDRAW;
        wc.lpfnWndProc = UiWndProc;
        wc.hInstance = inst;
        wc.hIcon = LoadIconW(inst, MAKEINTRESOURCEW(1));
        wc.hIconSm = wc.hIcon;
        wc.hCursor = LoadCursorW(NULL, (LPCWSTR)IDC_ARROW);
        wc.hbrBackground = (HBRUSH)GetStockObject(WHITE_BRUSH);
        wc.lpszClassName = WC_UPDATER;
        if (!RegisterClassExW(&wc)) return NULL;
        g_class_registered = 1;
    }

    w = (struct UiWindow *)calloc(1, sizeof(*w));
    if (!w) return NULL;
    w->kind = kind;
    w->result_button = UI_BTN_NONE;
    window_size_for(kind, &width, &height);
    w->width = width;
    w->height = height;

    w->hwnd = CreateWindowExW(0, WC_UPDATER, L"炮炮火枪手 自动更新",
                              WS_POPUP | WS_VISIBLE,
                              (GetSystemMetrics(SM_CXSCREEN) - width) / 2,
                              (GetSystemMetrics(SM_CYSCREEN) - height) / 2,
                              width, height,
                              NULL, NULL, inst, NULL);
    if (!w->hwnd) { free(w); return NULL; }
    SetWindowLongPtrW(w->hwnd, GWLP_USERDATA, (LONG_PTR)w);
    ui_external_init(&w->external, w, on_button, on_drag);

    /* ---- 渲染链：file:// -> document.write -> 原生 --------------------- */
    if (g_mode_forced != UI_MODE_NATIVE) {
        HostSite *site = (HostSite *)calloc(1, sizeof(HostSite));
        IOleObject *ole = NULL;
        HRESULT hr;
        RECT rc;

        hr = CoCreateInstance(&CLSID_WebBrowser, NULL, CLSCTX_INPROC_SERVER,
                              &IID_IOleObject, (void **)&ole);
        if (SUCCEEDED(hr) && ole && site) {
            site->clientSite.lpVtbl = &g_cs_vtbl;
            site->inPlaceSite.lpVtbl = &g_ips_vtbl;
            site->inPlaceFrame.lpVtbl = &g_ipf_vtbl;
            site->controlSite.lpVtbl = &g_ocs_vtbl;
            site->docHost.lpVtbl = &g_duh_vtbl;
            site->refs = 1;
            site->win = w;
            w->site = site;
            w->ole = ole;
            w->mode = UI_MODE_IE_FILE;
            ole->lpVtbl->SetClientSite(ole, &site->clientSite);
            ole->lpVtbl->SetHostNames(ole, L"PopShot", L"Update");
            OleSetContainedObject((IUnknown *)ole, TRUE);
            GetClientRect(w->hwnd, &rc);
            ole->lpVtbl->DoVerb(ole, OLEIVERB_INPLACEACTIVATE, NULL,
                                &site->clientSite, 0, w->hwnd, &rc);
            ole->lpVtbl->QueryInterface(ole, &IID_IWebBrowser2,
                                        (void **)&w->browser);
            ole->lpVtbl->QueryInterface(ole, &IID_IOleInPlaceObject,
                                        (void **)&w->inplace);
            browser_set_rect(w);
            if (w->browser)
                w->browser->lpVtbl->put_Silent(w->browser, VARIANT_TRUE);

            {
                int done = 0;
                if (g_mode_forced != UI_MODE_IE_WRITE) {
                    wchar_t path[MAX_PATH * 2], url[MAX_PATH * 2 + 16];
                    path_join(path, MAX_PATH * 2, g_ui_dir,
                              template_res_for(kind));
                    file_url_from_path(path, url, MAX_PATH * 2 + 16);
                    if (browser_navigate(w, url) &&
                        wait_browser_ready(w, 10000))
                        done = 1;
                    else
                        log_line("ui: file:// render failed, fallback");
                }
                if (!done && g_mode_forced != UI_MODE_IE_FILE) {
                    w->mode = UI_MODE_IE_WRITE;
                    if (render_by_write(w, template_res_for(kind)))
                        done = 1;
                    else
                        log_line("ui: document.write render failed, fallback");
                }
                if (!done)
                    w->mode = UI_MODE_NATIVE;
            }
        } else {
            if (ole) ole->lpVtbl->Release(ole);
            if (site) free(site);
            w->mode = UI_MODE_NATIVE;
        }
    } else {
        w->mode = UI_MODE_NATIVE;
    }

    if (w->mode == UI_MODE_NATIVE) {
        /* 释放没派上用场的浏览器对象，PATCH 主窗换成原生控件套件。 */
        if (w->browser) { w->browser->lpVtbl->Release(w->browser); w->browser = NULL; }
        if (w->inplace) { w->inplace->lpVtbl->Release(w->inplace); w->inplace = NULL; }
        if (w->ole) {
            w->ole->lpVtbl->Close(w->ole, OLECLOSE_NOSAVE);
            w->ole->lpVtbl->Release(w->ole);
            w->ole = NULL;
        }
        if (w->site) { free(w->site); w->site = NULL; }
        if (kind == UI_WIN_PATCH) ui_native_build(w);
    } else {
        window_fit_content(w);
        if (kind == UI_WIN_PATCH) {
            /* 公告区一打开就有内容（真机踩坑修复：不能等选定目标版本，
               否则检查/失败阶段中间是一大块空白）。 */
            write_announce_html(NULL);
            exec_js(w, L"ChangeContents('announce.html')");
        }
    }
    log_line("ui: window kind=%d render mode=%d", kind, w->mode);
    return w;
}

static void window_destroy(struct UiWindow *w)
{
    if (!w) return;
    if (w->hwnd) {
        SetWindowLongPtrW(w->hwnd, GWLP_USERDATA, 0);
        if (IsWindow(w->hwnd)) DestroyWindow(w->hwnd);
    }
    if (w->browser) w->browser->lpVtbl->Release(w->browser);
    if (w->inplace) w->inplace->lpVtbl->Release(w->inplace);
    if (w->ole) {
        w->ole->lpVtbl->Close(w->ole, OLECLOSE_NOSAVE);
        w->ole->lpVtbl->Release(w->ole);
    }
    if (w->site) free(w->site);
    free(w);
}

/* ====================================================================== */
/*  模态（CONFIRMRUNADMIN / MESSAGEL）                                     */
/* ====================================================================== */

static int run_modal(struct UiWindow *m, const wchar_t *text,
                     int alert_icon, int show_cancel)
{
    MSG msg;
    if (m->mode != UI_MODE_NATIVE) {
        while (m->result_button == UI_BTN_NONE &&
               GetMessageW(&msg, NULL, 0, 0) > 0) {
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
        if (m->result_button == UI_BTN_NONE) PostQuitMessage(0);  /* WM_QUIT 穿透 */
    } else {
        /* 原生回退的模态：MessageBox 即可。 */
        if (m->kind == 0) {
            m->result_button = MessageBoxW(NULL,
                    L"继续运行需要管理员权限。\n\n如果想继续，请点击「是」。",
                    L"自动更新", MB_ICONQUESTION | MB_YESNO) == IDYES
                        ? UI_BTN_CONTINUE : UI_BTN_STOP;
        } else {
            UINT mb = alert_icon ? MB_ICONWARNING : MB_ICONINFORMATION;
            mb |= show_cancel ? MB_OKCANCEL : MB_OK;
            m->result_button =
                MessageBoxW(NULL, text ? text : L"", L"自动更新", mb) == IDOK
                    ? UI_BTN_CONFIRM : UI_BTN_CANCEL;
        }
    }
    return m->result_button;
}

static void handle_modal(ModalReq *req)
{
    struct UiWindow *m;
    if (g_main_wnd && IsWindow(g_main_wnd->hwnd))
        EnableWindow(g_main_wnd->hwnd, FALSE);

    if (req->kind == 0) {
        /* 管理员确认：原版 CONFIRMRUNADMIN 模板（440x163）。 */
        m = window_create(UI_WIN_CONFIRM);
        if (m) {
            if (m->mode != UI_MODE_NATIVE) {
                run_modal(m, NULL, 0, 0);
                req->result = (m->result_button == UI_BTN_CONTINUE) ? 1 : 0;
            } else {
                run_modal(m, NULL, 0, 0);
                req->result = (m->result_button == UI_BTN_CONTINUE) ? 1 : 0;
            }
            window_destroy(m);
        } else {
            req->result = MessageBoxW(NULL,
                    L"继续运行需要管理员权限。\n\n如果想继续，请点击「是」。",
                    L"自动更新", MB_ICONQUESTION | MB_YESNO) == IDYES ? 1 : 0;
        }
    } else {
        /* 消息框：原版 MESSAGEL 模板（440x133）。 */
        m = window_create(UI_WIN_MESSAGE);
        if (m && m->mode != UI_MODE_NATIVE) {
            wchar_t esc[600], js[1024];
            js_escape(req->text, esc, 600);
            _snwprintf(js, 1024,
                       L"document.all.Message.innerHTML='%s';"
                       L"ChangeType(%d);%ls",
                       esc, req->alert_icon ? req->alert_icon : 0,
                       req->show_cancel ? L"ShowBtnCancel();" : L"");
            js[1023] = 0;
            exec_js(m, js);
            run_modal(m, req->text, req->alert_icon, req->show_cancel);
            req->result = m->result_button;
        } else {
            if (m) window_destroy(m);
            {
                struct UiWindow stub;
                memset(&stub, 0, sizeof(stub));
                stub.kind = UI_WIN_MESSAGE;
                stub.mode = UI_MODE_NATIVE;
                run_modal(&stub, req->text, req->alert_icon, req->show_cancel);
                req->result = stub.result_button;
            }
        }
        if (m) window_destroy(m);
    }

    if (g_main_wnd && IsWindow(g_main_wnd->hwnd))
        EnableWindow(g_main_wnd->hwnd, TRUE);
    SetEvent(req->done);
}

/* ====================================================================== */
/*  对外门面                                                              */
/* ====================================================================== */

int ui_init(const wchar_t *package_root, int forced_mode, int noui)
{
    INITCOMMONCONTROLSEX icc;
    wcsncpy(g_root, package_root, MAX_PATH * 2 - 1);
    g_root[MAX_PATH * 2 - 1] = 0;
    g_mode_forced = forced_mode;
    g_noui = noui;
    if (noui) return 1;

    icc.dwSize = sizeof(icc);
    icc.dwICC = ICC_WIN95_CLASSES;
    InitCommonControlsEx(&icc);

    if (SUCCEEDED(OleInitialize(NULL))) g_ole_inited = 1;
    else if (SUCCEEDED(CoInitialize(NULL))) g_com_inited = 1;

    if (!materialize_ui_dir()) {
        log_line("ui: materialize failed (temp=%ls)", g_ui_dir);
        /* 素材都进 exe 了还落不了盘 = 磁盘满之类，走原生界面。 */
        g_mode_forced = UI_MODE_NATIVE;
    }
    return 1;
}

void ui_shutdown(void)
{
    if (g_main_wnd) {
        window_destroy(g_main_wnd);
        g_main_wnd = NULL;
    }
    if (g_ui_dir[0]) delete_tree(g_ui_dir);
    if (g_ole_inited) { OleUninitialize(); g_ole_inited = 0; }
    if (g_com_inited) { CoUninitialize(); g_com_inited = 0; }
}

int ui_window_create_patch(void)
{
    struct UiWindow *w = window_create(UI_WIN_PATCH);
    if (!w) return 0;
    /* 初始状态：正在检查更新。 */
    window_apply_status(w, L"正在检查更新……");
    g_main_wnd = w;
    return 1;
}

void ui_pump_until_quit(HANDLE worker_thread)
{
    MSG msg;
    if (g_noui) {
        if (worker_thread) WaitForSingleObject(worker_thread, INFINITE);
        return;
    }
    while (GetMessageW(&msg, NULL, 0, 0) > 0) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }
}

void ui_request_quit(int exit_code)
{
    InterlockedExchange(&g_exit_code, (LONG)exit_code);
    InterlockedExchange(&g_cancel, 1);
    if (g_noui) return;
    PostQuitMessage(exit_code);
}

static void post_wstr(UINT msg, const wchar_t *text)
{
    wchar_t *copy;
    if (g_noui) return;
    if (!g_main_wnd || !IsWindow(g_main_wnd->hwnd)) return;
    copy = _wcsdup(text);
    if (!copy) return;
    if (!PostMessageW(g_main_wnd->hwnd, msg, 0, (LPARAM)copy))
        free(copy);
}

void ui_status(const wchar_t *text)
{
    log_line("ui: %ls", text);
    post_wstr(UM_STATUS, text);
}

void ui_remaining(const wchar_t *text)
{
    if (g_noui) return;
    if (!g_main_wnd || !IsWindow(g_main_wnd->hwnd)) return;
    if (!text) {
        PostMessageW(g_main_wnd->hwnd, UM_REMAINING, 0, 0);
        return;
    }
    post_wstr(UM_REMAINING, text);
}

void ui_progress_total(int percent)
{
    if (g_noui || !g_main_wnd || !IsWindow(g_main_wnd->hwnd)) return;
    PostMessageW(g_main_wnd->hwnd, UM_PROG_TOTAL, (WPARAM)percent, 0);
}

void ui_progress_current(int percent)
{
    if (g_noui || !g_main_wnd || !IsWindow(g_main_wnd->hwnd)) return;
    PostMessageW(g_main_wnd->hwnd, UM_PROG_CURRENT, (WPARAM)percent, 0);
}

void ui_swap_button(void)
{
    if (g_noui || !g_main_wnd || !IsWindow(g_main_wnd->hwnd)) return;
    PostMessageW(g_main_wnd->hwnd, UM_SWAP_BUTTON, 0, 0);
}

void ui_announce_version(const wchar_t *ver_text)
{
    if (g_noui) return;
    if (!g_main_wnd || !IsWindow(g_main_wnd->hwnd)) return;
    {
        wchar_t *copy = _wcsdup(ver_text ? ver_text : L"");
        if (!copy) return;
        if (!PostMessageW(g_main_wnd->hwnd, UM_ANNOUNCE, 0, (LPARAM)copy))
            free(copy);
    }
}

void ui_announce_error(const wchar_t *detail)
{
    if (g_noui) return;
    if (!g_main_wnd || !IsWindow(g_main_wnd->hwnd)) return;
    {
        wchar_t *copy = _wcsdup(detail ? detail : L"未知原因");
        if (!copy) return;
        if (!PostMessageW(g_main_wnd->hwnd, UM_ANNOUNCE_ERR, 0, (LPARAM)copy))
            free(copy);
    }
}

static int modal_and_wait(ModalReq *req, int default_result)
{
    if (g_noui || !g_main_wnd || !IsWindow(g_main_wnd->hwnd)) {
        req->result = default_result;
        return 1;
    }
    req->done = CreateEventW(NULL, FALSE, FALSE, NULL);
    if (!req->done) {
        req->result = default_result;
        return 1;
    }
    if (!PostMessageW(g_main_wnd->hwnd, UM_MODAL, 0, (LPARAM)req)) {
        CloseHandle(req->done);
        req->result = default_result;
        return 1;
    }
    WaitForSingleObject(req->done, INFINITE);
    CloseHandle(req->done);
    return 1;
}

int ui_confirm_admin(void)
{
    ModalReq req;
    memset(&req, 0, sizeof(req));
    req.kind = 0;
    log_line("ui: confirm-admin dialog");
    modal_and_wait(&req, 1);          /* 无 UI 环境（自动化）：默认继续 */
    return req.result;
}

int ui_message_box(const wchar_t *text, int show_cancel, int alert_icon)
{
    ModalReq req;
    memset(&req, 0, sizeof(req));
    req.kind = 1;
    wcsncpy(req.text, text ? text : L"", 599);
    req.show_cancel = show_cancel;
    req.alert_icon = alert_icon;
    log_line("ui: message-box %ls", req.text);
    modal_and_wait(&req, UI_BTN_CONFIRM);
    return req.result;
}
