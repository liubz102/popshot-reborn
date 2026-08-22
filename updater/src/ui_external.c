/* --------------------------------------------------------------------------
   ui_external.c —— 见 ui_external.h。纯手写 IDispatch：只认 OnButton /
   DragMove 两个名字，Invoke 只处理这两个 dispid。
   -------------------------------------------------------------------------- */
#define WIN32_LEAN_AND_MEAN
#define _CRT_SECURE_NO_WARNINGS
#include <windows.h>
#include <oleauto.h>
#include <wchar.h>
#include <string.h>
#include "ui_external.h"

#define DISPID_ONBUTTON 1
#define DISPID_DRAGMOVE 2

static UiExternal *self_of(IDispatch *p)
{
    /* 结构体第一个成员就是 vtbl 槽：接口指针 == 结构体指针。 */
    return (UiExternal *)p;
}

static HRESULT STDMETHODCALLTYPE ext_qi(IDispatch *p, REFIID riid, void **out)
{
    UiExternal *s = self_of(p);
    if (!out) return E_POINTER;
    *out = NULL;
    if (IsEqualIID(riid, &IID_IUnknown) || IsEqualIID(riid, &IID_IDispatch)) {
        *out = p;
        InterlockedIncrement(&s->refs);
        return S_OK;
    }
    return E_NOINTERFACE;
}

static ULONG STDMETHODCALLTYPE ext_addref(IDispatch *p)
{
    return (ULONG)InterlockedIncrement(&self_of(p)->refs);
}

static ULONG STDMETHODCALLTYPE ext_release(IDispatch *p)
{
    UiExternal *s = self_of(p);
    ULONG r = (ULONG)InterlockedDecrement(&s->refs);
    /* 宿主对象随窗口生死，不在这里 free。 */
    return r;
}

static HRESULT STDMETHODCALLTYPE ext_get_type_info_count(IDispatch *p,
                                                         UINT *count)
{
    (void)p;
    if (!count) return E_POINTER;
    *count = 0;
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE ext_get_type_info(IDispatch *p, UINT it,
                                                   LCID lcid, ITypeInfo **ti)
{
    (void)p; (void)it; (void)lcid; (void)ti;
    return E_NOTIMPL;
}

static HRESULT STDMETHODCALLTYPE ext_get_ids_of_names(IDispatch *p,
                                                      REFIID riid,
                                                      LPOLESTR *names,
                                                      UINT cnames, LCID lcid,
                                                      DISPID *dispids)
{
    UINT i;
    (void)p; (void)lcid;
    if (!IsEqualIID(riid, &IID_NULL)) return DISP_E_UNKNOWNINTERFACE;
    if (cnames == 0 || !names || !dispids) return E_INVALIDARG;
    for (i = 0; i < cnames; i++) dispids[i] = DISPID_UNKNOWN;
    if (wcscmp(names[0], L"OnButton") == 0) {
        dispids[0] = DISPID_ONBUTTON;
        return S_OK;
    }
    if (wcscmp(names[0], L"DragMove") == 0) {
        dispids[0] = DISPID_DRAGMOVE;
        return S_OK;
    }
    return DISP_E_UNKNOWNNAME;
}

static HRESULT STDMETHODCALLTYPE ext_invoke(IDispatch *p, DISPID dispid,
                                            REFIID riid, LCID lcid,
                                            WORD flags,
                                            DISPPARAMS *params,
                                            VARIANT *result, EXCEPINFO *excep,
                                            UINT *argerr)
{
    UiExternal *s = self_of(p);
    (void)riid; (void)lcid; (void)flags; (void)excep; (void)argerr;
    if (result) VariantInit(result);
    if (dispid == DISPID_ONBUTTON) {
        UINT i;
        for (i = 0; params && i < params->cArgs; i++) {
            if (params->rgvarg[i].vt == VT_BSTR && params->rgvarg[i].bstrVal) {
                if (s->on_button)
                    s->on_button(s->user, params->rgvarg[i].bstrVal);
                return S_OK;
            }
        }
        return DISP_E_PARAMNOTFOUND;
    }
    if (dispid == DISPID_DRAGMOVE) {
        if (s->on_drag) s->on_drag(s->user);
        return S_OK;
    }
    return DISP_E_MEMBERNOTFOUND;
}

static IDispatchVtbl g_ext_vtbl = {
    ext_qi, ext_addref, ext_release,
    ext_get_type_info_count, ext_get_type_info, ext_get_ids_of_names,
    ext_invoke
};

void ui_external_init(UiExternal *p, void *user,
                      ui_ext_button_fn on_button, ui_ext_drag_fn on_drag)
{
    memset(p, 0, sizeof(*p));
    p->lpVtbl = &g_ext_vtbl;
    p->refs = 1;
    p->user = user;
    p->on_button = on_button;
    p->on_drag = on_drag;
}
