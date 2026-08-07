#define WIN32_LEAN_AND_MEAN
#define COBJMACROS
#include <windows.h>
#include <d3d9.h>
#include <d3d9on12.h>
#include <dxgi1_4.h>
#include <stdio.h>

static LRESULT CALLBACK probe_wndproc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
{
    return DefWindowProc(hwnd, msg, wp, lp);
}

static void print_display_devices(void)
{
    DWORD adapter_index;
    DISPLAY_DEVICEA adapter;

    printf("Win32 display devices:\n");
    for (adapter_index = 0; ; ++adapter_index) {
        DWORD monitor_index;
        DISPLAY_DEVICEA monitor;

        ZeroMemory(&adapter, sizeof(adapter));
        adapter.cb = sizeof(adapter);
        if (!EnumDisplayDevicesA(NULL, adapter_index, &adapter, 0)) break;
        printf("  adapter[%lu] name=%s string=%s flags=%08lX id=%s\n",
               (unsigned long)adapter_index, adapter.DeviceName,
               adapter.DeviceString, (unsigned long)adapter.StateFlags,
               adapter.DeviceID);
        for (monitor_index = 0; ; ++monitor_index) {
            ZeroMemory(&monitor, sizeof(monitor));
            monitor.cb = sizeof(monitor);
            if (!EnumDisplayDevicesA(adapter.DeviceName, monitor_index,
                                     &monitor, 0)) break;
            printf("    monitor[%lu] name=%s string=%s flags=%08lX id=%s\n",
                   (unsigned long)monitor_index, monitor.DeviceName,
                   monitor.DeviceString, (unsigned long)monitor.StateFlags,
                   monitor.DeviceID);
        }
    }
}

static void print_adapters(const char *label, IDirect3D9 *d3d)
{
    UINT index;
    UINT count = IDirect3D9_GetAdapterCount(d3d);
    printf("%s adapter identifiers (%u):\n", label, count);
    for (index = 0; index < count; ++index) {
        D3DADAPTER_IDENTIFIER9 identifier;
        HRESULT hr;
        ZeroMemory(&identifier, sizeof(identifier));
        hr = IDirect3D9_GetAdapterIdentifier(d3d, index, 0, &identifier);
        printf("  [%u] hr=%08lX desc=%s driver=%s device=%s "
               "vendor=%04lX device_id=%04lX\n",
               index, (unsigned long)hr, identifier.Description,
               identifier.Driver, identifier.DeviceName,
               (unsigned long)identifier.VendorId,
               (unsigned long)identifier.DeviceId);
    }
}

static void try_device(const char *name, IDirect3D9 *d3d, HWND hwnd,
                       const D3DPRESENT_PARAMETERS *source,
                       D3DDEVTYPE device_type, DWORD behavior)
{
    D3DPRESENT_PARAMETERS pp = *source;
    IDirect3DDevice9 *device = NULL;
    HRESULT hr = IDirect3D9_CreateDevice(
        d3d, D3DADAPTER_DEFAULT, device_type, hwnd, behavior, &pp, &device);
    printf("%-28s type=%u behavior=%08lX hr=%08lX device=%p\n",
           name, (unsigned)device_type, (unsigned long)behavior,
           (unsigned long)hr, (void *)device);
    if (device) IDirect3DDevice9_Release(device);
}

int main(void)
{
    WNDCLASSA wc;
    HWND hwnd;
    IDirect3D9 *d3d;
    D3DDISPLAYMODE mode;
    D3DPRESENT_PARAMETERS pp;
    HRESULT hr;
    DWORD hw = D3DCREATE_MULTITHREADED | D3DCREATE_HARDWARE_VERTEXPROCESSING;
    DWORD sw = D3DCREATE_MULTITHREADED | D3DCREATE_SOFTWARE_VERTEXPROCESSING;
    PFN_Direct3DCreate9On12 create_on12;
    D3D9ON12_ARGS on12_args;
    IDirect3D9 *d3d_on12;
    IDXGIFactory4 *factory = NULL;
    IDXGIAdapter *warp_adapter = NULL;
    ID3D12Device *warp_device = NULL;
    ID3D12CommandQueue *warp_queue = NULL;
    D3D12_COMMAND_QUEUE_DESC queue_desc;

    ZeroMemory(&wc, sizeof(wc));
    print_display_devices();
    wc.lpfnWndProc = probe_wndproc;
    wc.hInstance = GetModuleHandle(NULL);
    wc.lpszClassName = "PopShotD3D9Probe";
    RegisterClassA(&wc);
    hwnd = CreateWindowA(wc.lpszClassName, "PopShot D3D9 probe",
                         WS_OVERLAPPEDWINDOW | WS_VISIBLE,
                         CW_USEDEFAULT, CW_USEDEFAULT, 1024, 768,
                         NULL, NULL, wc.hInstance, NULL);
    if (!hwnd) {
        printf("CreateWindow failed: %lu\n", (unsigned long)GetLastError());
        return 2;
    }

    d3d = Direct3DCreate9(D3D_SDK_VERSION);
    if (!d3d) {
        printf("Direct3DCreate9 returned NULL\n");
        DestroyWindow(hwnd);
        return 3;
    }
    print_adapters("native D3D9", d3d);
    ZeroMemory(&mode, sizeof(mode));
    hr = IDirect3D9_GetAdapterDisplayMode(d3d, 0, &mode);
    printf("display hr=%08lX %ux%u@%u fmt=%u adapters=%u\n",
           (unsigned long)hr, mode.Width, mode.Height, mode.RefreshRate,
           mode.Format, IDirect3D9_GetAdapterCount(d3d));
    hr = IDirect3D9_CheckDeviceType(d3d, 0, D3DDEVTYPE_HAL,
                                    mode.Format, D3DFMT_X8R8G8B8, TRUE);
    printf("CheckDeviceType HAL/X8R8G8B8/windowed hr=%08lX\n",
           (unsigned long)hr);

    ZeroMemory(&pp, sizeof(pp));
    pp.Windowed = TRUE;
    pp.SwapEffect = D3DSWAPEFFECT_DISCARD;
    pp.hDeviceWindow = hwnd;
    pp.BackBufferFormat = D3DFMT_UNKNOWN;
    try_device("minimal unknown format", d3d, hwnd, &pp, D3DDEVTYPE_HAL, sw);
    try_device("minimal unknown format", d3d, hwnd, &pp, D3DDEVTYPE_HAL, hw);
    try_device("minimal REF", d3d, hwnd, &pp, D3DDEVTYPE_REF, sw);
    try_device("minimal NULLREF", d3d, hwnd, &pp, D3DDEVTYPE_NULLREF, sw);

    pp.BackBufferWidth = 1024;
    pp.BackBufferHeight = 768;
    pp.BackBufferFormat = D3DFMT_X8R8G8B8;
    pp.BackBufferCount = 1;
    pp.EnableAutoDepthStencil = TRUE;
    pp.AutoDepthStencilFormat = D3DFMT_D16;
    pp.Flags = D3DPRESENTFLAG_LOCKABLE_BACKBUFFER |
               D3DPRESENTFLAG_DISCARD_DEPTHSTENCIL;
    try_device("BigShot exact", d3d, hwnd, &pp, D3DDEVTYPE_HAL, sw);
    try_device("BigShot exact", d3d, hwnd, &pp, D3DDEVTYPE_HAL, hw);
    try_device("BigShot exact REF", d3d, hwnd, &pp, D3DDEVTYPE_REF, sw);

    pp.Flags = 0;
    try_device("exact flags=0", d3d, hwnd, &pp, D3DDEVTYPE_HAL, sw);
    pp.EnableAutoDepthStencil = FALSE;
    pp.AutoDepthStencilFormat = D3DFMT_UNKNOWN;
    try_device("no depth, flags=0", d3d, hwnd, &pp, D3DDEVTYPE_HAL, sw);
    pp.BackBufferFormat = D3DFMT_UNKNOWN;
    try_device("unknown fmt, no depth", d3d, hwnd, &pp, D3DDEVTYPE_HAL, sw);

    create_on12 = (PFN_Direct3DCreate9On12)GetProcAddress(
        GetModuleHandleA("d3d9.dll"), "Direct3DCreate9On12");
    ZeroMemory(&on12_args, sizeof(on12_args));
    on12_args.Enable9On12 = TRUE;
    d3d_on12 = create_on12
        ? create_on12(D3D_SDK_VERSION, &on12_args, 1) : NULL;
    printf("\nDirect3DCreate9On12 export=%p interface=%p\n",
           (void *)create_on12, (void *)d3d_on12);
    if (d3d_on12) {
        print_adapters("D3D9On12", d3d_on12);
        ZeroMemory(&mode, sizeof(mode));
        hr = IDirect3D9_GetAdapterDisplayMode(d3d_on12, 0, &mode);
        printf("9On12 display hr=%08lX %ux%u@%u fmt=%u adapters=%u\n",
               (unsigned long)hr, mode.Width, mode.Height, mode.RefreshRate,
               mode.Format, IDirect3D9_GetAdapterCount(d3d_on12));
        hr = IDirect3D9_CheckDeviceType(d3d_on12, 0, D3DDEVTYPE_HAL,
                                        mode.Format, D3DFMT_X8R8G8B8, TRUE);
        printf("9On12 CheckDeviceType HAL/X8R8G8B8/windowed hr=%08lX\n",
               (unsigned long)hr);
        ZeroMemory(&pp, sizeof(pp));
        pp.Windowed = TRUE;
        pp.SwapEffect = D3DSWAPEFFECT_DISCARD;
        pp.hDeviceWindow = hwnd;
        pp.BackBufferFormat = D3DFMT_UNKNOWN;
        try_device("9On12 minimal", d3d_on12, hwnd, &pp, D3DDEVTYPE_HAL, sw);
        try_device("9On12 minimal HWVP", d3d_on12, hwnd, &pp, D3DDEVTYPE_HAL, hw);
        pp.BackBufferWidth = 1024;
        pp.BackBufferHeight = 768;
        pp.BackBufferFormat = D3DFMT_X8R8G8B8;
        pp.BackBufferCount = 1;
        pp.EnableAutoDepthStencil = TRUE;
        pp.AutoDepthStencilFormat = D3DFMT_D16;
        pp.Flags = D3DPRESENTFLAG_LOCKABLE_BACKBUFFER |
                   D3DPRESENTFLAG_DISCARD_DEPTHSTENCIL;
        try_device("9On12 BigShot exact", d3d_on12, hwnd, &pp,
                   D3DDEVTYPE_HAL, sw);
        IDirect3D9_Release(d3d_on12);
    }

    printf("\n--- explicit D3D12 WARP -> D3D9On12 ---\n");
    hr = CreateDXGIFactory1(&IID_IDXGIFactory4, (void **)&factory);
    printf("CreateDXGIFactory1 hr=%08lX factory=%p\n",
           (unsigned long)hr, (void *)factory);
    if (SUCCEEDED(hr)) {
        hr = IDXGIFactory4_EnumWarpAdapter(
            factory, &IID_IDXGIAdapter, (void **)&warp_adapter);
        printf("EnumWarpAdapter hr=%08lX adapter=%p\n",
               (unsigned long)hr, (void *)warp_adapter);
    }
    if (SUCCEEDED(hr)) {
        hr = D3D12CreateDevice((IUnknown *)warp_adapter,
                               D3D_FEATURE_LEVEL_11_0,
                               &IID_ID3D12Device, (void **)&warp_device);
        printf("D3D12CreateDevice(WARP) hr=%08lX device=%p\n",
               (unsigned long)hr, (void *)warp_device);
    }
    ZeroMemory(&queue_desc, sizeof(queue_desc));
    queue_desc.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    if (SUCCEEDED(hr)) {
        hr = ID3D12Device_CreateCommandQueue(
            warp_device, &queue_desc, &IID_ID3D12CommandQueue,
            (void **)&warp_queue);
        printf("CreateCommandQueue hr=%08lX queue=%p\n",
               (unsigned long)hr, (void *)warp_queue);
    }
    if (SUCCEEDED(hr) && create_on12) {
        ZeroMemory(&on12_args, sizeof(on12_args));
        on12_args.Enable9On12 = TRUE;
        on12_args.pD3D12Device = (IUnknown *)warp_device;
        on12_args.ppD3D12Queues[0] = (IUnknown *)warp_queue;
        on12_args.NumQueues = 1;
        d3d_on12 = create_on12(D3D_SDK_VERSION, &on12_args, 1);
        printf("Direct3DCreate9On12(WARP) interface=%p\n", (void *)d3d_on12);
        if (d3d_on12) {
            print_adapters("D3D9On12 WARP", d3d_on12);
            ZeroMemory(&mode, sizeof(mode));
            hr = IDirect3D9_GetAdapterDisplayMode(d3d_on12, 0, &mode);
            printf("WARP 9On12 display hr=%08lX %ux%u@%u fmt=%u adapters=%u\n",
                   (unsigned long)hr, mode.Width, mode.Height, mode.RefreshRate,
                   mode.Format, IDirect3D9_GetAdapterCount(d3d_on12));
            hr = IDirect3D9_CheckDeviceType(d3d_on12, 0, D3DDEVTYPE_HAL,
                                            mode.Format, D3DFMT_X8R8G8B8, TRUE);
            printf("WARP 9On12 CheckDeviceType hr=%08lX\n", (unsigned long)hr);
            ZeroMemory(&pp, sizeof(pp));
            pp.Windowed = TRUE;
            pp.SwapEffect = D3DSWAPEFFECT_DISCARD;
            pp.hDeviceWindow = hwnd;
            pp.BackBufferFormat = D3DFMT_UNKNOWN;
            try_device("WARP 9On12 minimal", d3d_on12, hwnd, &pp,
                       D3DDEVTYPE_HAL, sw);
            try_device("WARP 9On12 minimal HWVP", d3d_on12, hwnd, &pp,
                       D3DDEVTYPE_HAL, hw);
            pp.BackBufferWidth = 1024;
            pp.BackBufferHeight = 768;
            pp.BackBufferFormat = D3DFMT_X8R8G8B8;
            pp.BackBufferCount = 1;
            pp.EnableAutoDepthStencil = TRUE;
            pp.AutoDepthStencilFormat = D3DFMT_D16;
            pp.Flags = D3DPRESENTFLAG_LOCKABLE_BACKBUFFER |
                       D3DPRESENTFLAG_DISCARD_DEPTHSTENCIL;
            try_device("WARP 9On12 BigShot", d3d_on12, hwnd, &pp,
                       D3DDEVTYPE_HAL, sw);
            try_device("WARP 9On12 BigShot HWVP", d3d_on12, hwnd, &pp,
                       D3DDEVTYPE_HAL, hw);
            IDirect3D9_Release(d3d_on12);
        }
    }
    if (warp_queue) ID3D12CommandQueue_Release(warp_queue);
    if (warp_device) ID3D12Device_Release(warp_device);
    if (warp_adapter) IDXGIAdapter_Release(warp_adapter);
    if (factory) IDXGIFactory4_Release(factory);

    IDirect3D9_Release(d3d);
    DestroyWindow(hwnd);
    return 0;
}
