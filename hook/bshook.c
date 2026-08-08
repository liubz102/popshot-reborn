/* ==========================================================================
 * bshook.dll —— 注入到 BigShot.exe 里的观测/补丁模块
 *
 * 阶段 0/1（当前）：只观测，不改行为。
 *   - 记录进程信息、命令行、当前目录
 *   - 轮询模块加载，抓出 GameGuard / SeData / nmcogame 到底有没有被加载、什么时候
 *   - 轮询顶层窗口 + 子控件文字，把 GameGuard 错误框的**完整原文**抓下来
 *   - 记录 ASProtect 解壳完成的时机（用 .text 首字节是否变化判断）
 *
 * 阶段 2：在这里加 GameGuard 初始化调用点的内存 patch
 * 阶段 3：在这里加 ws2_32 hook（connect/send/recv 重定向到 127.0.0.1 + 落盘）
 *
 * 注入方式见 bsloader.c：CREATE_SUSPENDED + QueueUserAPC(LoadLibraryA)，
 * 因此本 DLL 在 EXE 入口点（= ASProtect 壳入口）执行**之前**就已加载完毕。
 * ========================================================================== */

#define _CRT_SECURE_NO_WARNINGS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <tlhelp32.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include <intrin.h>
#pragma intrinsic(_ReturnAddress)

/* -------------------------------------------------------------------------- */
/* 日志                                                                        */
/* -------------------------------------------------------------------------- */

static HANDLE            g_log = INVALID_HANDLE_VALUE;
static CRITICAL_SECTION  g_cs;
static volatile LONG     g_stop = 0;

/* ★ 日志级别（0 = 精简，1 = 详细）。环境变量 `BSHOOK_VERBOSE_LOG=1` 打开。
 *
 * 为什么要有这个开关（会话 14 实测，见 FINDINGS §105）：
 *   `bslog` 每条都 `FlushFileBuffers` —— 实测**每条 2.0 毫秒**（中位数）。
 *   详细模式下一次会话写 9.6 万行日志，光是等 flush 就是 **197 秒**。
 *   「登录后要等 100 秒才进大厅」「战斗中一卡一卡」全是这么来的，
 *   跟渲染、跟服务端都没关系。
 *
 * 修法两件事，各治一半：
 *   ① 详细日志走 `bsvlog` —— 不 flush、不 OutputDebugString。
 *      **这条是提速的大头**：实测详细模式也从 ~100 秒降到 15.1 秒。
 *   ② 精简模式不安装 SnowCipher hook —— 日志量的 99% 出自那里。
 *      省的是磁盘（4.2 MB → 18.7 KB）、格式化 CPU 和日志可读性，不再是启动时间。
 * 关键事件（PATCH / HOOK / MSGBOX / WS2 / D3D / 崩溃）两种模式都照记且照 flush，
 * 排查问题该有的证据一条不少。 */
static volatile LONG     g_verbose = 0;

static void bslog_emit(int detail, const char *fmt, va_list ap)
{
    char line[8192];
    SYSTEMTIME st;
    int n;

    GetLocalTime(&st);
    n = _snprintf(line, sizeof(line) - 4, "[%02u:%02u:%02u.%03u] ",
                  st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);
    if (n < 0) n = 0;

    {
        int m = _vsnprintf(line + n, sizeof(line) - n - 4, fmt, ap);
        if (m > 0) n += m;
    }

    line[n++] = '\r';
    line[n++] = '\n';
    line[n]   = '\0';

    EnterCriticalSection(&g_cs);
    if (g_log != INVALID_HANDLE_VALUE) {
        DWORD written;
        WriteFile(g_log, line, (DWORD)n, &written, NULL);
        /* 详细日志只写进系统文件缓存，不同步落盘：进程正常/异常退出时
           OS 都会把缓存写回，只有整机断电才丢 —— 换来的是 2ms → ~2us。 */
        if (!detail) FlushFileBuffers(g_log);
    }
    LeaveCriticalSection(&g_cs);

    /* OutputDebugStringA 在没有调试器时也要走一次 RaiseException，
       高频路径上同样是负担，详细日志一并跳过。 */
    if (!detail) OutputDebugStringA(line);
}

/* 关键事件：任何模式都记，且立刻落盘（崩溃时不能丢）。 */
void bslog(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    bslog_emit(0, fmt, ap);
    va_end(ap);
}

/* 详细/高频事件：只在 `BSHOOK_VERBOSE_LOG=1` 时记，且不 flush。 */
void bsvlog(const char *fmt, ...)
{
    va_list ap;
    if (!g_verbose) return;
    va_start(ap, fmt);
    bslog_emit(1, fmt, ap);
    va_end(ap);
}

/* 宽字符转 UTF-8。
   必须用：GetWindowTextA / Module32First 这些 A 版返回的是 CP936 字节，
   混进 UTF-8 日志就是乱码 —— 而 GameGuard 报错原文正是我们最想要的证据。 */
static const char *w2u8(const wchar_t *ws, char *out, int outsz)
{
    if (!ws || !*ws) { out[0] = 0; return out; }
    if (WideCharToMultiByte(CP_UTF8, 0, ws, -1, out, outsz, NULL, NULL) == 0)
        out[0] = 0;
    return out;
}

/* -------------------------------------------------------------------------- */
/* 极简 x86 长度反汇编器 —— 只为算出内联 hook 要偷几个字节（>=5）             */
/* 覆盖常见函数序言指令；遇到不认识的 opcode 返回 0（放弃 hook，安全）        */
/* -------------------------------------------------------------------------- */
static int insn_len(const unsigned char *p)
{
    unsigned char op = p[0];
    /* 前缀 */
    if (op == 0x66 || op == 0x67 || op == 0xF2 || op == 0xF3) return 1 + insn_len(p + 1);
    switch (op) {
    case 0x50: case 0x51: case 0x52: case 0x53:      /* push r32 */
    case 0x54: case 0x55: case 0x56: case 0x57:
    case 0x58: case 0x59: case 0x5A: case 0x5B:      /* pop r32  */
    case 0x5C: case 0x5D: case 0x5E: case 0x5F:
    case 0x90: case 0xC3: case 0xC9:                 /* nop/ret/leave */
        return 1;
    case 0x6A:                                       /* push imm8 */
        return 2;
    case 0x68:                                       /* push imm32 */
        return 5;
    case 0xB8: case 0xB9: case 0xBA: case 0xBB:      /* mov r32, imm32 */
    case 0xBC: case 0xBD: case 0xBE: case 0xBF:
        return 5;
    case 0xE9: case 0xE8:                            /* jmp/call rel32 */
        return 5;
    case 0xEB:                                       /* jmp rel8 */
        return 2;
    case 0x8B: case 0x89:                            /* mov r/m32,r32 / mov r32,r/m32 */
    {
        unsigned char modrm = p[1];
        unsigned char mod = modrm >> 6, rm = modrm & 7;
        int len = 2;
        if (mod != 3 && rm == 4) len += 1;           /* SIB */
        if (mod == 1) len += 1;                       /* disp8 */
        else if (mod == 2) len += 4;                  /* disp32 */
        else if (mod == 0 && rm == 5) len += 4;       /* disp32 (no base) */
        else if (mod == 0 && rm == 4 && (p[2] & 7) == 5) len += 4; /* SIB base=5 */
        return len;
    }
    case 0x83:                                       /* grp1 r/m32, imm8 (sub/add esp,..) */
    {
        unsigned char modrm = p[1];
        unsigned char mod = modrm >> 6, rm = modrm & 7;
        int len = 2;
        if (mod != 3 && rm == 4) len += 1;
        if (mod == 1) len += 1;
        else if (mod == 2) len += 4;
        else if (mod == 0 && rm == 5) len += 4;
        return len + 1;                               /* + imm8 */
    }
    case 0x81:                                       /* grp1 r/m32, imm32 */
    {
        unsigned char modrm = p[1];
        unsigned char mod = modrm >> 6, rm = modrm & 7;
        int len = 2;
        if (mod != 3 && rm == 4) len += 1;
        if (mod == 1) len += 1;
        else if (mod == 2) len += 4;
        else if (mod == 0 && rm == 5) len += 4;
        return len + 4;                               /* + imm32 */
    }
    case 0xFF:                                        /* grp5 (push/call/jmp r/m) */
    {
        unsigned char modrm = p[1];
        unsigned char mod = modrm >> 6, rm = modrm & 7;
        int len = 2;
        if (mod != 3 && rm == 4) len += 1;
        if (mod == 1) len += 1;
        else if (mod == 2) len += 4;
        else if (mod == 0 && rm == 5) len += 4;
        return len;
    }
    }
    return 0; /* 不认识 */
}

/* -------------------------------------------------------------------------- */
/* 通用内联 hook：在 target 头部写 E9 跳到 detour，返回可调用原函数的蹦床      */
/* 偷够 >=5 字节（按指令边界），蹦床 = [偷到的字节][E9 跳回 target+n]          */
/* -------------------------------------------------------------------------- */
static void *install_inline_hook(void *target, void *detour, const char *name)
{
    unsigned char *t = (unsigned char *)target;
    unsigned char *tramp;
    DWORD oldp;
    int stolen = 0, guard = 0;

    if (!t) { bslog("HOOK    %s: target=NULL, 跳过", name); return NULL; }

    bslog("HOOK    %s @ %08X 序言: %02x %02x %02x %02x %02x %02x %02x %02x",
          name, (unsigned)(UINT_PTR)t,
          t[0], t[1], t[2], t[3], t[4], t[5], t[6], t[7]);

    while (stolen < 5 && guard++ < 8) {
        int l = insn_len(t + stolen);
        if (l <= 0) { bslog("HOOK    %s: 未知 opcode %02x @ +%d, 放弃", name, t[stolen], stolen); return NULL; }
        stolen += l;
    }
    if (stolen < 5) { bslog("HOOK    %s: 偷不够 5 字节, 放弃", name); return NULL; }

    tramp = (unsigned char *)VirtualAlloc(NULL, 32, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    if (!tramp) { bslog("HOOK    %s: VirtualAlloc 失败", name); return NULL; }
    memcpy(tramp, t, stolen);
    tramp[stolen] = 0xE9;
    *(DWORD *)(tramp + stolen + 1) = (DWORD)((UINT_PTR)(t + stolen) - (UINT_PTR)(tramp + stolen + 5));

    if (!VirtualProtect(t, 5, PAGE_EXECUTE_READWRITE, &oldp)) {
        bslog("HOOK    %s: VirtualProtect 失败", name); return NULL;
    }
    t[0] = 0xE9;
    *(DWORD *)(t + 1) = (DWORD)((UINT_PTR)detour - (UINT_PTR)(t + 5));
    VirtualProtect(t, 5, oldp, &oldp);
    FlushInstructionCache(GetCurrentProcess(), t, 5);

    bslog("HOOK    %s: 安装成功, 偷了 %d 字节, 蹦床 @ %08X",
          name, stolen, (unsigned)(UINT_PTR)tramp);
    return tramp;
}

/* 主模块地址范围，用来在栈里筛出「自己人」的返回地址 */
static UINT_PTR g_mod_lo = 0, g_mod_hi = 0;

/* 沿 EBP 链回溯，打印每一帧的返回地址（干净，不会被栈上的文本缓冲污染）。
   标注落在主模块范围内的（= 我们能 patch 的调用点）。 */
static void log_ebp_chain(const char *tag)
{
    UINT_PTR frame = 0;
    int i;
#if defined(_M_IX86)
    __asm { mov eax, ebp }
    __asm { mov frame, eax }
#endif
    bslog("%s  EBP链回溯(主模块 %08X..%08X):", tag, (unsigned)g_mod_lo, (unsigned)g_mod_hi);
    for (i = 0; i < 32; i++) {
        UINT_PTR *fp = (UINT_PTR *)frame;
        UINT_PTR ra, next;
        if (!frame || (frame & 3) || IsBadReadPtr(fp, 8)) break;
        next = fp[0];
        ra   = fp[1];
        {
            const char *inmod = (ra >= g_mod_lo && ra < g_mod_hi) ? " <<< 主模块" : "";
            bslog("        #%02d frame=%08X ret=%08X%s", i, (unsigned)frame, (unsigned)ra, inmod);
        }
        if (next <= frame) break;   /* 帧指针必须递增 */
        frame = next;
    }
}

/* -------------------------------------------------------------------------- */
/* 阶段2 观测：hook MessageBoxW/A —— GameGuard 失败时弹的「公告」框走这里。     */
/* 目的：抓到「谁调用了错误框」= 失败分支地址，再据此静态定位校验条件并 patch。 */
/* 自动化无人点框，故直接返回 IDOK 抑制（= 等价于用户点确定，见 FINDINGS §18）。*/
/* -------------------------------------------------------------------------- */
typedef int (WINAPI *MsgBoxW_t)(HWND, LPCWSTR, LPCWSTR, UINT);
typedef int (WINAPI *MsgBoxA_t)(HWND, LPCSTR, LPCSTR, UINT);
static MsgBoxW_t s_MessageBoxW = NULL;
static MsgBoxA_t s_MessageBoxA = NULL;
static volatile LONG g_hooks_installed = 0;

static int WINAPI det_MessageBoxW(HWND hWnd, LPCWSTR text, LPCWSTR cap, UINT type)
{
    void *ra = _ReturnAddress();
    char u8t[3072], u8c[512];
    bslog("★MSGBOXW caller=%08X type=%08x cap=\"%s\"",
          (unsigned)(UINT_PTR)ra, type, w2u8(cap, u8c, sizeof(u8c)));
    bslog("         text=\"%s\"", w2u8(text, u8t, sizeof(u8t)));
    log_ebp_chain("★MSGBOXW");
    return IDOK; /* 抑制，不真正弹框 */
}

static int WINAPI det_MessageBoxA(HWND hWnd, LPCSTR text, LPCSTR cap, UINT type)
{
    void *ra = _ReturnAddress();
    bslog("★MSGBOXA caller=%08X type=%08x cap=\"%s\" text=\"%s\"",
          (unsigned)(UINT_PTR)ra, type, cap ? cap : "(null)", text ? text : "(null)");
    log_ebp_chain("★MSGBOXA");
    return IDOK;
}

/* -------------------------------------------------------------------------- */
/* 阶段3 观测：hook ws2_32 —— 查明客户端要连哪个服务器 IP:端口 + DNS 解析。   */
/* 本步只观测不改写（先摸清全貌，再选择性重定向，遵循 D012 的先观测原则）。    */
/* 不用 winsock 头/不链 ws2_32.lib：手写最小 sockaddr_in 布局 + 手动字节序。   */
/* -------------------------------------------------------------------------- */
typedef UINT_PTR SOCKET_T;
struct sockaddr_min { short sa_family; unsigned char sa_data[14]; };
struct sockaddr_in_min {                 /* 与 sockaddr_in 二进制一致(16字节) */
    short         sin_family;
    unsigned char sin_port[2];           /* 网络序（大端） */
    unsigned char sin_addr[4];           /* 网络序：直接就是 a.b.c.d */
    unsigned char sin_zero[8];
};
#define AF_INET_MIN 2

typedef int (WINAPI *connect_t)(SOCKET_T, const struct sockaddr_min *, int);
typedef int (WINAPI *WSAConnect_t)(SOCKET_T, const struct sockaddr_min *, int,
                                   void *, void *, void *, void *);
typedef void *(WINAPI *gethostbyname_t)(const char *);
typedef int (WINAPI *getaddrinfo_t)(const char *, const char *, const void *, void *);

static connect_t       s_connect = NULL;
static WSAConnect_t    s_WSAConnect = NULL;
static gethostbyname_t s_gethostbyname = NULL;
static getaddrinfo_t   s_getaddrinfo = NULL;

static void log_sockaddr(const char *api, SOCKET_T s, const struct sockaddr_min *name, int namelen)
{
    void *ra = _ReturnAddress();
    if (name && namelen >= 16 && name->sa_family == AF_INET_MIN) {
        const struct sockaddr_in_min *in = (const struct sockaddr_in_min *)name;
        unsigned port = ((unsigned)in->sin_port[0] << 8) | in->sin_port[1];
        bslog("★WS2 %s s=%u -> %u.%u.%u.%u:%u  (caller=%08X)",
              api, (unsigned)s,
              in->sin_addr[0], in->sin_addr[1], in->sin_addr[2], in->sin_addr[3],
              port, (unsigned)(UINT_PTR)ra);
    } else {
        bslog("★WS2 %s s=%u family=%d namelen=%d (非IPv4, caller=%08X)",
              api, (unsigned)s, name ? name->sa_family : -1, namelen, (unsigned)(UINT_PTR)ra);
    }
}

/* 阶段3：把游戏 TCP 连接重定向到 127.0.0.1（端口不变）。原始目标已在 log_sockaddr 记下。
   置 0 则纯观测不改写。 */
static int g_redirect = 1;

/* 若是 IPv4 且开启重定向：把地址改成 127.0.0.1，返回 1 并填好 out；否则返回 0。 */
static int make_localhost(const struct sockaddr_min *name, int namelen, struct sockaddr_in_min *out)
{
    if (!g_redirect || !name || namelen < 16 || name->sa_family != AF_INET_MIN) return 0;
    memcpy(out, name, sizeof(*out));
    out->sin_addr[0] = 127; out->sin_addr[1] = 0; out->sin_addr[2] = 0; out->sin_addr[3] = 1;
    bslog("★WS2 重定向 -> 127.0.0.1:%u", ((unsigned)out->sin_port[0] << 8) | out->sin_port[1]);
    return 1;
}

/* 定义在下方 SnowCipher 段：把 SNOW 日志计数器清零。
   启动时加载 Pack\*.pkn 会把配额一次性烧光，网络阶段就什么都记不到了，
   所以每次 connect 都重开一个干净的记录窗口。 */
void snow_log_reset(void);

static int WINAPI det_connect(SOCKET_T s, const struct sockaddr_min *name, int namelen)
{
    struct sockaddr_in_min sa;
    log_sockaddr("connect", s, name, namelen);
    snow_log_reset();
    if (make_localhost(name, namelen, &sa))
        return s_connect(s, (const struct sockaddr_min *)&sa, (int)sizeof(sa));
    return s_connect(s, name, namelen);
}

static int WINAPI det_WSAConnect(SOCKET_T s, const struct sockaddr_min *name, int namelen,
                                 void *ci, void *co, void *sq, void *gq)
{
    struct sockaddr_in_min sa;
    log_sockaddr("WSAConnect", s, name, namelen);
    if (make_localhost(name, namelen, &sa))
        return s_WSAConnect(s, (const struct sockaddr_min *)&sa, (int)sizeof(sa), ci, co, sq, gq);
    return s_WSAConnect(s, name, namelen, ci, co, sq, gq);
}

static void *WINAPI det_gethostbyname(const char *nm)
{
    bslog("★WS2 gethostbyname(\"%s\")  caller=%08X", nm ? nm : "(null)",
          (unsigned)(UINT_PTR)_ReturnAddress());
    return s_gethostbyname(nm);
}

static int WINAPI det_getaddrinfo(const char *node, const char *service,
                                  const void *hints, void *res)
{
    bslog("★WS2 getaddrinfo(node=\"%s\", service=\"%s\")  caller=%08X",
          node ? node : "(null)", service ? service : "(null)",
          (unsigned)(UINT_PTR)_ReturnAddress());
    return s_getaddrinfo(node, service, hints, res);
}

static void install_ws2_hooks(void)
{
    HMODULE ws2 = GetModuleHandleA("ws2_32.dll");
    if (!ws2) { bslog("HOOK    ws2_32 尚未加载, 稍后重试"); return; }
    if (s_connect) return; /* 已装 */

    s_connect = (connect_t)install_inline_hook(
        (void *)GetProcAddress(ws2, "connect"), (void *)det_connect, "ws2:connect");
    s_WSAConnect = (WSAConnect_t)install_inline_hook(
        (void *)GetProcAddress(ws2, "WSAConnect"), (void *)det_WSAConnect, "ws2:WSAConnect");
    s_gethostbyname = (gethostbyname_t)install_inline_hook(
        (void *)GetProcAddress(ws2, "gethostbyname"), (void *)det_gethostbyname, "ws2:gethostbyname");
    s_getaddrinfo = (getaddrinfo_t)install_inline_hook(
        (void *)GetProcAddress(ws2, "getaddrinfo"), (void *)det_getaddrinfo, "ws2:getaddrinfo");
}

static void install_hooks(void)
{
    HMODULE u32;
    HMODULE self;
    IMAGE_DOS_HEADER *dos;
    IMAGE_NT_HEADERS *nt;

    if (InterlockedExchange(&g_hooks_installed, 1)) return;

    /* 主模块范围（用于栈回溯筛选） */
    self = GetModuleHandleA(NULL);
    dos = (IMAGE_DOS_HEADER *)self;
    nt = (IMAGE_NT_HEADERS *)((BYTE *)self + dos->e_lfanew);
    g_mod_lo = (UINT_PTR)self;
    g_mod_hi = g_mod_lo + nt->OptionalHeader.SizeOfImage;
    bslog("HOOK    主模块范围 %08X..%08X (SizeOfImage=%08X)",
          (unsigned)g_mod_lo, (unsigned)g_mod_hi, (unsigned)nt->OptionalHeader.SizeOfImage);

    u32 = GetModuleHandleA("user32.dll");
    if (!u32) { bslog("HOOK    user32 尚未加载, 等下一轮"); g_hooks_installed = 0; return; }

    s_MessageBoxW = (MsgBoxW_t)install_inline_hook(
        (void *)GetProcAddress(u32, "MessageBoxW"), (void *)det_MessageBoxW, "MessageBoxW");
    s_MessageBoxA = (MsgBoxA_t)install_inline_hook(
        (void *)GetProcAddress(u32, "MessageBoxA"), (void *)det_MessageBoxA, "MessageBoxA");

    install_ws2_hooks();  /* ws2_32 是静态导入, 此时已加载 */
}

/* 十六进制 dump，阶段 3 抓包会大量用到。
   `detail` 非 0 时走 bsvlog（精简模式下整块不输出）。 */
static void bslog_hex_ex(int detail, const char *tag, const unsigned char *p, int len)
{
    char line[128];
    int i, j, n;

    if (detail && !g_verbose) return;
    if (detail) bsvlog("%s  (%d bytes)", tag, len);
    else        bslog ("%s  (%d bytes)", tag, len);
    for (i = 0; i < len; i += 16) {
        n = _snprintf(line, sizeof(line), "    %04x  ", i);
        for (j = 0; j < 16; j++) {
            if (i + j < len) n += _snprintf(line + n, sizeof(line) - n, "%02x ", p[i + j]);
            else             n += _snprintf(line + n, sizeof(line) - n, "   ");
            if (j == 7) { line[n++] = ' '; line[n] = 0; }
        }
        n += _snprintf(line + n, sizeof(line) - n, " |");
        for (j = 0; j < 16 && i + j < len; j++) {
            unsigned char c = p[i + j];
            line[n++] = (c >= 0x20 && c < 0x7f) ? (char)c : '.';
        }
        line[n++] = '|';
        line[n]   = 0;
        if (detail) bsvlog("%s", line);
        else        bslog ("%s", line);
    }
}

void bslog_hex(const char *tag, const unsigned char *p, int len)
{
    bslog_hex_ex(0, tag, p, len);
}

void bsvlog_hex(const char *tag, const unsigned char *p, int len)
{
    bslog_hex_ex(1, tag, p, len);
}

/* -------------------------------------------------------------------------- */
/* 观测线程：模块加载 / 窗口出现 / 解壳时机                                     */
/* -------------------------------------------------------------------------- */

#define MAX_SEEN 512
static HMODULE g_seen_mod[MAX_SEEN];
static int     g_seen_mod_n = 0;
static HWND    g_seen_wnd[MAX_SEEN];
static int     g_seen_wnd_n = 0;

static int seen_mod(HMODULE h)
{
    int i;
    for (i = 0; i < g_seen_mod_n; i++) if (g_seen_mod[i] == h) return 1;
    if (g_seen_mod_n < MAX_SEEN) g_seen_mod[g_seen_mod_n++] = h;
    return 0;
}

static int seen_wnd(HWND h)
{
    int i;
    for (i = 0; i < g_seen_wnd_n; i++) if (g_seen_wnd[i] == h) return 1;
    if (g_seen_wnd_n < MAX_SEEN) g_seen_wnd[g_seen_wnd_n++] = h;
    return 0;
}

static void poll_modules(void)
{
    HANDLE snap;
    MODULEENTRY32W me;
    char u8[MAX_PATH * 3];

    snap = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE, GetCurrentProcessId());
    if (snap == INVALID_HANDLE_VALUE) return;

    me.dwSize = sizeof(me);
    if (Module32FirstW(snap, &me)) {
        do {
            if (!seen_mod(me.hModule)) {
                bslog("MODULE  base=%08X size=%08X  %s",
                      (unsigned)(UINT_PTR)me.modBaseAddr, (unsigned)me.modBaseSize,
                      w2u8(me.szExePath, u8, sizeof(u8)));
            }
        } while (Module32NextW(snap, &me));
    }
    CloseHandle(snap);
}

static BOOL CALLBACK dump_child(HWND h, LPARAM lp)
{
    wchar_t cls[64], txt[1024];
    char u8c[192], u8t[3072];
    (void)lp;

    cls[0] = txt[0] = 0;
    GetClassNameW(h, cls, 64);
    GetWindowTextW(h, txt, 1024);
    if (txt[0])
        bslog("      child [%s] \"%s\"",
              w2u8(cls, u8c, sizeof(u8c)), w2u8(txt, u8t, sizeof(u8t)));
    return TRUE;
}

static BOOL CALLBACK dump_window(HWND h, LPARAM lp)
{
    DWORD pid = 0;
    wchar_t cls[64], txt[1024];
    char u8c[192], u8t[3072];
    (void)lp;

    GetWindowThreadProcessId(h, &pid);
    if (pid != GetCurrentProcessId()) return TRUE;
    if (seen_wnd(h)) return TRUE;

    cls[0] = txt[0] = 0;
    GetClassNameW(h, cls, 64);
    GetWindowTextW(h, txt, 1024);
    bslog("WINDOW  hwnd=%08X visible=%d class=[%s] title=\"%s\"",
          (unsigned)(UINT_PTR)h, IsWindowVisible(h) ? 1 : 0,
          w2u8(cls, u8c, sizeof(u8c)), w2u8(txt, u8t, sizeof(u8t)));
    EnumChildWindows(h, dump_child, 0);
    return TRUE;
}

/* ASProtect 解壳观测：第一个节（RVA 0x1000）在解壳前是密文，
   壳跑完后会变成真正的机器码。这里只记录首 16 字节何时发生变化。 */
static void poll_unpack(void)
{
    static unsigned char prev[16];
    static int have_prev = 0;
    static int changes = 0;
    unsigned char *code = (unsigned char *)GetModuleHandleA(NULL) + 0x1000;

    if (IsBadReadPtr(code, 16)) return;

    if (!have_prev) {
        memcpy(prev, code, 16);
        have_prev = 1;
        bslog("UNPACK  base+0x1000 initial: %02x %02x %02x %02x %02x %02x %02x %02x",
              prev[0], prev[1], prev[2], prev[3], prev[4], prev[5], prev[6], prev[7]);
        return;
    }
    if (memcmp(prev, code, 16) != 0 && changes < 8) {
        changes++;
        memcpy(prev, code, 16);
        bslog("UNPACK  base+0x1000 changed (#%d): %02x %02x %02x %02x %02x %02x %02x %02x",
              changes, prev[0], prev[1], prev[2], prev[3], prev[4], prev[5], prev[6], prev[7]);
    }
}

/* -------------------------------------------------------------------------- */
/* 阶段2 patch —— 让 GameGuard 校验取到成功码 0x755                            */
/*                                                                            */
/*   校验点 va=0x54b0fc:  call 0x5611d0 (E8 CF 60 01 00)                       */
/*   patch 成:            mov eax, 0x755 (B8 55 07 00 00)                      */
/*   → 校验函数 0x54b042 走成功路径返回 al=1，nProtect 状态取值器被跳过。      */
/*                                                                            */
/*   时机：必须等 ASProtect 把该区域解壳出来（字节变成 E8 CF 60 01 00）后再写。*/
/*   由独立 patch 线程 5ms 一轮紧盯，命中即写，远早于 ~5s 的校验执行。         */
/* -------------------------------------------------------------------------- */
#define GG_CHECK_VA 0x0054b0fcu
#define GG_PATCH_DELAY_MS 2500       /* 见 patch_thread 里的时机说明 */
static const unsigned char GG_ORIG[5]  = { 0xE8, 0xCF, 0x60, 0x01, 0x00 }; /* call 0x5611d0 */
static const unsigned char GG_PATCH[5] = { 0xB8, 0x55, 0x07, 0x00, 0x00 }; /* mov eax,0x755 */
static volatile LONG g_gg_patched = 0;

static int try_patch_gameguard(void)
{
    unsigned char *p = (unsigned char *)GG_CHECK_VA;
    DWORD oldp;

    if (g_gg_patched) return 1;
    if (IsBadReadPtr(p, 5)) return 0;
    if (memcmp(p, GG_PATCH, 5) == 0) {          /* 已是 patch 后的样子 */
        InterlockedExchange(&g_gg_patched, 1);
        return 1;
    }
    if (memcmp(p, GG_ORIG, 5) != 0) return 0;   /* 还没解壳到这里，继续等 */

    if (!VirtualProtect(p, 5, PAGE_EXECUTE_READWRITE, &oldp)) {
        bslog("PATCH   GameGuard: VirtualProtect 失败 err=%lu", (unsigned long)GetLastError());
        return 0;
    }
    memcpy(p, GG_PATCH, 5);
    VirtualProtect(p, 5, oldp, &oldp);
    FlushInstructionCache(GetCurrentProcess(), p, 5);
    InterlockedExchange(&g_gg_patched, 1);
    bslog("PATCH   ★GameGuard 校验 @ %08X: call 0x5611d0 -> mov eax,0x755 (成功码 0x755)",
          (unsigned)GG_CHECK_VA);
    return 1;
}

/* -------------------------------------------------------------------------- */
/* 单机化 patch —— 把房间的「90 秒没动作就踢回大厅」拉长到实际上不会触发      */
/*                                                                            */
/*   0x4082ae  LobbyStage::ResetIdleTimer():                                  */
/*       push 0x15f90            ; 90000 ms                                   */
/*       add  ecx, 0x3e8         ; -> LobbyStage + 0x3e8 的 Timer             */
/*       call 0x5d5e37           ; Timer::Start(ms)                           */
/*                                                                            */
/*   读它的只有一处：RoomStage::Update 0x46761c 的 Timer::IsExpired()，        */
/*   超时就弹「90秒无任何动作，返回至游戏大厅。」并发 gcpLeaveSession(0x0203)。*/
/*                                                                            */
/*   这个计时器只被四件事重置：窗口过程收到 WM_L/M/RBUTTONUP 或 WM_KEYUP      */
/*   (0x40ee3d)、LobbyStage::ResetSession (0x40563b)、提示框弹完自己重置      */
/*   (0x4676cb)、战斗里的 0x4906ac。**没有任何一条是收包触发的**，服务端      */
/*   够不着，只能改客户端。                                                   */
/*                                                                            */
/*   把时长换成 0x40000000 ms（约 12.4 天）而不是直接跳过判定：机制原样保留，  */
/*   deadline = start + 时长 也不会溢出成负数。                               */
/*                                                                            */
/*   设环境变量 BSHOOK_KEEP_AFK_KICK=1 可以保留原版 90 秒行为。               */
/* -------------------------------------------------------------------------- */
#define AFK_TIMER_VA 0x004082aeu
static const unsigned char AFK_ORIG[5]  = { 0x68, 0x90, 0x5F, 0x01, 0x00 }; /* push 90000     */
static const unsigned char AFK_PATCH[5] = { 0x68, 0x00, 0x00, 0x00, 0x40 }; /* push 0x40000000 */
static volatile LONG g_afk_patched = 0;

static int afk_kick_disabled(void)
{
    char buf[8];
    DWORD n = GetEnvironmentVariableA("BSHOOK_KEEP_AFK_KICK", buf, sizeof(buf));
    return !(n > 0 && n < sizeof(buf) && buf[0] != '0');
}

static int try_patch_afk_timer(void)
{
    unsigned char *p = (unsigned char *)AFK_TIMER_VA;
    DWORD oldp;

    if (g_afk_patched) return 1;
    if (IsBadReadPtr(p, 5)) return 0;
    if (memcmp(p, AFK_PATCH, 5) == 0) {         /* 已是 patch 后的样子 */
        InterlockedExchange(&g_afk_patched, 1);
        return 1;
    }
    if (memcmp(p, AFK_ORIG, 5) != 0) return 0;  /* 还没解壳到这里，继续等 */

    if (!VirtualProtect(p, 5, PAGE_EXECUTE_READWRITE, &oldp)) {
        bslog("PATCH   挂机踢出: VirtualProtect 失败 err=%lu", (unsigned long)GetLastError());
        return 0;
    }
    memcpy(p, AFK_PATCH, 5);
    VirtualProtect(p, 5, oldp, &oldp);
    FlushInstructionCache(GetCurrentProcess(), p, 5);
    InterlockedExchange(&g_afk_patched, 1);
    bslog("PATCH   ★房间挂机踢出 @ %08X: 90000ms -> 0x40000000ms（约 12.4 天）",
          (unsigned)AFK_TIMER_VA);
    return 1;
}

/* -------------------------------------------------------------------------- */
/* 单机化 patch —— 解锁被「地区掩码」关掉的关卡（神秘岛以外的第 5/6/7 关）    */
/*                                                                            */
/*   Data/map.ini 里每张地图都有一行 OpenLocale（注释写着                     */
/*   `1 - 한국, 2 - 일본, 4 - 중국`，按位或）。中国版跑起来时全局             */
/*   `[[0x72e320]]` = 2，客户端两处都拿 `1 << 2 = 4` 去和这个掩码 test：      */
/*                                                                            */
/*     0x40b419  地图目录加载（`0x40b2a1`，启动时读 map.ini）                 */
/*               掩码不匹配 -> 0x40b47a 把记录直接 delete 掉                  */
/*     0x4368cf  「建立房间(任务)」对话框填「任务」下拉框（`0x4365e1`）        */
/*               掩码不匹配 -> 跳过这一条                                     */
/*                                                                            */
/*   而 map.ini 里：                                                          */
/*     QuestId 1 불프로그 / 2 드라카 / 3 비밀의 섬 / 4 자미로건쉽  OpenLocale=7 */
/*     QuestId 5 다크나이트 / 6 브레그마 / 7 자미로 비밀 연구소     OpenLocale=3 */
/*     QuestId 8 푸른 하늘                                        OpenLocale=0 */
/*   —— **4 个关卡不是资源缺失，是中国版当年没上线**（地图文件全都在）。      */
/*                                                                            */
/*   改法：把这两处的「地区序号」当成 0（韩国）来算，也就是                   */
/*   `mov ecx,[...]` -> `xor ecx,ecx`，2 字节换 2 字节。                      */
/*   这样掩码里带 bit0 的都放行（7 / 3 / 1），掩码为 0 的仍然被挡住 ——        */
/*   Quest08 和一堆没写 OpenLocale 的条目（含缺文件的 Festivalm01）           */
/*   照样进不来。比直接 NOP 掉判定保守。                                      */
/*                                                                            */
/*   时机：两处都要**早于**启动时的 map.ini 加载。patch 线程在 +2.5s 打，      */
/*   那时资源加载还没开始（见 patch_thread 里 SnowCipher 那段的说明）。       */
/*                                                                            */
/*   ── 第三处：시리아 마스（角色 110）的战斗内换人图标 ──                    */
/*                                                                            */
/*   服务端把 11 个商城角色全放出来之后，**进关卡瞬间必崩**                   */
/*   （C0000005 @ 0x430857，调用链 0x40bd40 -> 0x477bab -> 0x4f5970           */
/*    -> 0x4f682a -> 0x430857）。战斗内的 `CharacterChanger` 给每个可选角色   */
/*   建一个按钮，图标取自 `Images/NewUI2/BigChrIcons.smf`，                    */
/*   下标由 `0x4f676e` 起的 switch 按角色 id 硬编码：                         */
/*       0/1/2 -> (id*2, id*2+1)   100 -> (6,7)    101 -> (8,9)               */
/*       103 -> (0x0a,0x0b)  102 -> (0x0c,0x0d)  104 -> (0x0c,0x0e)           */
/*       105 -> (0x0c,0x0f)  106 -> (0x10,0x11)  107 -> (0x12,0x13)           */
/*       108 -> (0x14,0x15)  109 -> (0x16,0x17)                               */
/*       110 -> (0x18,0x19)  3 -> (0x1a,0x1b)                                 */
/*   而这张图集是**按地区换的**（`0x558916` 把路径映射到                      */
/*   `Images/Chinese/BigChrIcons_CN.smf`），中国版那份只有 **24** 帧          */
/*   （0..23），韩国版 28 帧。下标 0x18/0x19 越界，`0x430854` 就从图集数组外  */
/*   取到垃圾指针。—— 图是真没有，不是判定挡住的。                            */
/*                                                                            */
/*   改法：把 110 的图标对改成 106 시리아 的 (0x10,0x11)，5 字节换 5 字节。    */
/*   战斗内换人条上它会显示成「시리아」的头像，模型/名字/数值都不受影响。      */
/*   （角色 3 아이린 要的 0x1a/0x1b 同样越界，但它被 `0x4f58f1` 显式跳过，    */
/*     根本不会建按钮，不用管。）                                             */
/*                                                                            */
/*   ── 第四处：待机房间里「关卡 ◀ ▶」按钮的关卡环 ──                        */
/*                                                                            */
/*   上面两处只管**地图目录**和**建房对话框的下拉框**。进了待机房间之后，     */
/*   右侧 `DlgSelectQuestMap` 的 `stageLBtn` / `stageRBtn` 用的是**另一张**   */
/*   表 —— `DlgSelectQuestMap::OnEvent`（`0x466264`）当场按地区**写死**       */
/*   一个关卡 id 的环形数组（`0x466727` 是 push_back）：                      */
/*       locale 0 韩 / 1 日  -> 0x466364: [3,2,1,4,5,6,7,3]                   */
/*       locale 2 中         -> 0x466329: [3,2,1,4,3]        ★ 只有 4 关     */
/*   （首元素 3 在末尾重复一次，两个方向才都能绕回去。）                      */
/*   它和 map.ini / OpenLocale **一点关系都没有**，所以会话 21 的两个 patch  */
/*   管不到它 —— 表现就是「新关卡只能在建房界面选，进房间就换不过去」。       */
/*   实测日志里房间按 ◀ ▶ 发出的 0x0302 正好循环 3→2→1→4→3。                 */
/*                                                                            */
/*   改法：`0x46631d` 的 `je 0x466364`(74 45) -> `jmp`(EB 45)，2 字节换       */
/*   2 字节，让中国区也走韩/日那条 7 关的分支。中国区专属的 4 关分支          */
/*   （`0x46631f`..`0x466362`）就此变成死代码，别的地区判定一个不动 ——       */
/*   尤其**不碰** `0x466309` 那句「中国版难度上限 3 档」。                    */
/*                                                                            */
/*   设环境变量 BSHOOK_KEEP_REGION_LOCK=1 可以整组保留原版行为。              */
/* -------------------------------------------------------------------------- */
#define REGION_PATCH_COUNT 4
/* 每处都验一段上下文再动手：2 字节的特征太短，光比 `8B 08` 容易撞上密文。 */
static const struct {
    unsigned int va;          /* 特征串起始 VA                    */
    unsigned int len;         /* 特征串长度                       */
    unsigned int off;         /* 要改的字节在特征串里的偏移        */
    unsigned int n;           /* 要改几个字节                     */
    const unsigned char *sig; /* 原始字节                         */
    const unsigned char *fix; /* 替换字节                         */
    const char *what;
} REGION_SITES[REGION_PATCH_COUNT] = {
    { 0x0040b419u, 19, 5, 2,
      (const unsigned char *)"\xA1\x20\xE3\x72\x00\x8B\x08\x8B\x53\x48"
                             "\x33\xC0\x40\xD3\xE0\x85\xC2\x74\x16",
      (const unsigned char *)"\x33\xC9",          /* xor ecx,ecx */
      "地图目录加载（地区序号 2 中国 -> 0 韩国）" },
    { 0x004368cfu, 20, 6, 2,
      (const unsigned char *)"\x8B\x0D\x20\xE3\x72\x00\x8B\x09\x8B\x70"
                             "\x48\x33\xD2\x42\xD3\xE2\x85\xD6\x74\x1A",
      (const unsigned char *)"\x33\xC9",          /* xor ecx,ecx */
      "建房「任务」下拉框（地区序号 2 中国 -> 0 韩国）" },
    { 0x004f67d1u, 15, 8, 5,
      (const unsigned char *)"\x8D\x04\x3F\x8D\x48\x01\xEB\x22"
                             "\x6A\x18\x58\x6A\x19\xEB\x1A",
      (const unsigned char *)"\x6A\x10\x58\x6A\x11", /* push 0x10 / pop eax / push 0x11 */
      "角色 110 战斗内图标（0x18/0x19 越界 -> 借用 106 的 0x10/0x11）" },
    { 0x00466318u, 17, 5, 2,
      (const unsigned char *)"\x2B\xC1\x88\x5D\xFC\x74\x45\x48\x74\x42"
                             "\x48\x0F\x85\xC3\x00\x00\x00",
      (const unsigned char *)"\xEB\x45",          /* je -> jmp（永远走韩/日分支）*/
      "房间「关卡 ◀ ▶」的关卡环（4 关 -> 7 关）" },
};
static volatile LONG g_region_patched = 0;

static int region_lock_disabled(void)
{
    char buf[8];
    DWORD n = GetEnvironmentVariableA("BSHOOK_KEEP_REGION_LOCK", buf, sizeof(buf));
    return !(n > 0 && n < sizeof(buf) && buf[0] != '0');
}

/* 返回 1 表示三处都已就位（本轮打的或之前就打过）。 */
static int try_patch_region_lock(void)
{
    int i, done = 0;

    if (g_region_patched) return 1;
    for (i = 0; i < REGION_PATCH_COUNT; i++) {
        unsigned char *base = (unsigned char *)REGION_SITES[i].va;
        unsigned char *p = base + REGION_SITES[i].off;
        unsigned int n = REGION_SITES[i].n;
        DWORD oldp;

        if (IsBadReadPtr(base, REGION_SITES[i].len)) continue;
        if (memcmp(p, REGION_SITES[i].fix, n) == 0) { done++; continue; }
        if (memcmp(base, REGION_SITES[i].sig, REGION_SITES[i].len) != 0)
            continue;                            /* 还没解壳到这里，继续等 */

        if (!VirtualProtect(p, n, PAGE_EXECUTE_READWRITE, &oldp)) {
            bslog("PATCH   地区差异(%s): VirtualProtect 失败 err=%lu",
                  REGION_SITES[i].what, (unsigned long)GetLastError());
            continue;
        }
        memcpy(p, REGION_SITES[i].fix, n);
        VirtualProtect(p, n, oldp, &oldp);
        FlushInstructionCache(GetCurrentProcess(), p, n);
        bslog("PATCH   ★地区差异 @ %08X: %s",
              (unsigned)(REGION_SITES[i].va + REGION_SITES[i].off),
              REGION_SITES[i].what);
        done++;
    }
    if (done == REGION_PATCH_COUNT) {
        InterlockedExchange(&g_region_patched, 1);
        return 1;
    }
    return 0;
}

/* -------------------------------------------------------------------------- */
/* 阶段4 观测 —— SnowCipher（包加密，SNOW 2.0）                                */
/*                                                                            */
/*   0x5dc7bc  loadkey(key, keysize, iv0..iv3)   ecx=state   ret 0x18         */
/*   0x5dd200  Encrypt(dst, src, len)            ecx=this    ret 0x0c         */
/*   0x5dd242  Decrypt(dst, src, len)            ecx=this    ret 0x0c         */
/*                                                                            */
/*   目的：拿到 ①密钥/IV ②明文包体。三者都在游戏自有代码里，                  */
/*   所以同样要等 ASProtect 解壳（按序言特征字节判定）后再装 hook。            */
/* -------------------------------------------------------------------------- */
#define SNOW_LOADKEY_VA 0x005dc7bcu
#define SNOW_ENC_VA     0x005dd200u
#define SNOW_DEC_VA     0x005dd242u
static const unsigned char SNOW_SIG_LOADKEY[6] = { 0x55,0x8b,0xec,0x51,0x8b,0xc1 };
static const unsigned char SNOW_SIG_CRYPT[6]   = { 0x55,0x8b,0xec,0x56,0x8b,0xf1 };

static void *s_snow_loadkey = NULL;
static void *s_snow_enc     = NULL;
static void *s_snow_dec     = NULL;
static volatile LONG g_snow_hooked = 0;
static LONG g_key_n = 0, g_enc_n = 0, g_dec_n = 0;

#define SNOW_MAX_LOG_BYTES 256
#define SNOW_LOG_MAX_KEYS  4000
#define SNOW_LOG_MAX_CRYPT 8000

static int try_hook_snow(void);

/* 连接建立之前一律**不落盘** —— 启动时加载 Pack\*.pkn 会产生上万次解密，
   每条日志都 FlushFileBuffers，光记日志就能把游戏拖到进不了登录界面。
   密钥则先进环形缓冲（网络用的 cipher 可能在 connect 之前就构造好了），
   connect 一到就把最近几把倒出来，然后开闸全记。

   ★ 这一整套只在 `BSHOOK_VERBOSE_LOG=1` 时才存在：cipher hook 本身在精简模式下
   就不安装（见 patch_thread）。协议已经解完了（FINDINGS §28–§34），日常游玩
   不需要逐包 dump —— 而它正是「登录后等 100 秒进大厅」的元凶（§105）。 */
#define SNOW_KEYRING 16
static unsigned char  g_keyring[SNOW_KEYRING][32];
static int            g_keyring_len[SNOW_KEYRING];
static unsigned       g_keyring_ksz[SNOW_KEYRING];
static LONG           g_keyring_seq = 0;
static volatile LONG  g_snow_log_on = 0;

void snow_log_reset(void)
{
    LONG total, i, first;

    /* 精简模式压根没装 cipher hook，这里没有任何可倒的东西。 */
    if (!g_verbose) return;

    InterlockedExchange(&g_key_n, 0);
    InterlockedExchange(&g_enc_n, 0);
    InterlockedExchange(&g_dec_n, 0);
    /* 兜底：连接来得比 hook 安装还早时在这里补装。 */
    if (!g_snow_hooked) try_hook_snow();

    total = g_keyring_seq;
    first = total > SNOW_KEYRING ? total - SNOW_KEYRING : 0;
    bsvlog("SNOW    —— connect 到达：连接前共 %ld 次 loadkey，下面是最后 %ld 把 ——",
          (long)total, (long)(total - first));
    for (i = first; i < total; i++) {
        int slot = (int)(i % SNOW_KEYRING);
        bsvlog("SNOW    loadkey[%ld] keysize=%u", (long)i, g_keyring_ksz[slot]);
        if (g_keyring_len[slot] > 0)
            bsvlog_hex("SNOW    key", g_keyring[slot], g_keyring_len[slot]);
    }
    InterlockedExchange(&g_snow_log_on, 1);
    bsvlog("SNOW    —— 开闸，开始记录本次连接的全部加解密 ——");
}

static void __stdcall on_loadkey(void *state, const unsigned char *key, unsigned ksz,
                                 unsigned iv0, unsigned iv1, unsigned iv2, unsigned iv3)
{
    int n = (int)(ksz / 8);
    LONG seq;
    if (n <= 0 || n > 32) n = 16;
    if (!g_snow_log_on) {                       /* 连接前：只进环形缓冲 */
        LONG idx = InterlockedIncrement(&g_keyring_seq) - 1;
        int slot = (int)(idx % SNOW_KEYRING);
        g_keyring_ksz[slot] = ksz;
        g_keyring_len[slot] = IsBadReadPtr(key, n) ? 0 : n;
        if (g_keyring_len[slot]) memcpy(g_keyring[slot], key, n);
        /* 头两把直接落盘：配合下面的 GT 输入/输出配对，用来校验
           server/snow.py 的 SNOW 2.0 实现是否与客户端逐位一致。 */
        if (idx < 2 && g_keyring_len[slot]) {
            bsvlog("SNOW    GT loadkey[%ld] keysize=%u IV=%08X %08X %08X %08X",
                  (long)idx, ksz, iv0, iv1, iv2, iv3);
            bsvlog_hex("SNOW    GT key", key, n);
        }
        return;
    }
    seq = InterlockedIncrement(&g_key_n);
    if (seq > SNOW_LOG_MAX_KEYS) return;
    bsvlog("SNOW    ★loadkey#%ld state=%08X keysize=%u IV=%08X %08X %08X %08X",
          (long)seq, (unsigned)(UINT_PTR)state, ksz, iv0, iv1, iv2, iv3);
    if (!IsBadReadPtr(key, n)) bsvlog_hex("SNOW    key", key, n);
}

static const char *const CRYPT_NAME[4] = {
    "Snow::Encrypt", "Snow::Decrypt", "Simple::Encrypt", "Simple::Decrypt"
};

static void __stdcall on_crypt_real(int kind, void *self, void *dst, void *src, int len);
static LONG g_gt_n = 0;

/* 原函数返回后记结果 —— 有了「同一次调用的入/出」才能验证算法。 */
static void __stdcall on_crypt_out(int kind, void *buf, int len)
{
    int n = len;
    if (g_gt_n > 8) return;
    if (n > 64) n = 64;
    if (n > 0 && !IsBadReadPtr(buf, n))
        bsvlog_hex("SNOW    GT 出", (const unsigned char *)buf, n);
}

static void __stdcall on_crypt(int kind, void *self, void *dst, void *src, int len)
{
    if (!g_snow_log_on) {          /* 连接前只留头 8 次做算法基准 */
        LONG g = InterlockedIncrement(&g_gt_n);
        if (g <= 8) {
            int m = len > 64 ? 64 : len;
            bsvlog("SNOW    GT %s#%ld this=%08X len=%d", CRYPT_NAME[kind & 3], (long)g,
                  (unsigned)(UINT_PTR)self, len);
            if (m > 0 && !IsBadReadPtr(src, m))
                bsvlog_hex("SNOW    GT 入", (const unsigned char *)src, m);
        }
    }
    on_crypt_real(kind, self, dst, src, len);
}

static void __stdcall on_crypt_real(int kind, void *self, void *dst, void *src, int len)
{
    LONG seq;
    int n = len;
    if (!g_snow_log_on) return;                 /* 连接前不记，见 snow_log_reset */
    seq = InterlockedIncrement((kind & 1) ? &g_dec_n : &g_enc_n);
    if (seq > SNOW_LOG_MAX_CRYPT) return;
    bsvlog("SNOW    ★%s#%ld this=%08X dst=%08X src=%08X len=%d",
          CRYPT_NAME[kind & 3], (long)seq,
          (unsigned)(UINT_PTR)self, (unsigned)(UINT_PTR)dst,
          (unsigned)(UINT_PTR)src, len);
    if (n > SNOW_MAX_LOG_BYTES) n = SNOW_MAX_LOG_BYTES;
    if (n > 0 && !IsBadReadPtr(src, n))
        bsvlog_hex("SNOW    入", (const unsigned char *)src, n);
}

/* 入口 detour：pushad(32)+pushfd(4)=36 字节，原参数整体上移 36。
   loadkey 入口栈: [+0]ret [+4]key [+8]ksz [+0xc]iv0 [+0x10]iv1 [+0x14]iv2 [+0x18]iv3 */
static __declspec(naked) void det_snow_loadkey(void)
{
    __asm {
        pushad
        pushfd
        push dword ptr [esp+60]      /* iv3 = 24+36 */
        push dword ptr [esp+60]      /* iv2 = 20+36+4 */
        push dword ptr [esp+60]      /* iv1 */
        push dword ptr [esp+60]      /* iv0 */
        push dword ptr [esp+60]      /* keysize */
        push dword ptr [esp+60]      /* key */
        push ecx                     /* state */
        call on_loadkey
        popfd
        popad
        push s_snow_loadkey
        ret
    }
}

/* crypt 入口栈: [+0]ret [+4]dst [+8]src [+0xc]len  → +36 后 40/44/48 */
static __declspec(naked) void det_snow_enc(void)
{
    __asm {
        pushad
        pushfd
        push dword ptr [esp+48]      /* len */
        push dword ptr [esp+48]      /* src */
        push dword ptr [esp+48]      /* dst */
        push ecx                     /* this */
        push 0                       /* is_dec = 0 */
        call on_crypt
        popfd
        popad
        push s_snow_enc
        ret
    }
}

/* Snow::Decrypt 做成完整包装：先记输入，调原函数，再记输出。
   pkn 解密走的就是这条路，(key, 密文, 明文) 三元组是校验 snow.py 的唯一标准答案。 */
static __declspec(naked) void det_snow_dec(void)
{
    __asm {
        push ebp
        mov  ebp, esp                /* ebp+8=dst  ebp+0xc=src  ebp+0x10=len  ecx=this */
        pushad
        push dword ptr [ebp+0x10]
        push dword ptr [ebp+0x0c]
        push dword ptr [ebp+8]
        push ecx
        push 1
        call on_crypt
        popad
        push dword ptr [ebp+0x10]
        push dword ptr [ebp+0x0c]
        push dword ptr [ebp+8]
        mov  eax, s_snow_dec
        call eax                     /* 原函数 ret 0xc，自己清参数 */
        pushad
        push dword ptr [ebp+0x10]
        push dword ptr [ebp+8]
        push 1
        call on_crypt_out
        popad
        pop  ebp
        ret  0x0c
    }
}

/* SimpleCipher（ICipher 的另一个实现，块大小 1 字节，双表逐字节加减）
   0x5bc449 Encrypt / 0x5bc49d Decrypt，签名与 SnowCipher 完全一致。 */
#define SIMPLE_ENC_VA 0x005bc449u
#define SIMPLE_DEC_VA 0x005bc49du
static const unsigned char SIMPLE_SIG[6] = { 0x55,0x8b,0xec,0x8b,0x45,0x10 };
static void *s_simple_enc = NULL;
static void *s_simple_dec = NULL;

static __declspec(naked) void det_simple_enc(void)
{
    __asm {
        pushad
        pushfd
        push dword ptr [esp+48]
        push dword ptr [esp+48]
        push dword ptr [esp+48]
        push ecx
        push 2
        call on_crypt
        popfd
        popad
        push s_simple_enc
        ret
    }
}

static __declspec(naked) void det_simple_dec(void)
{
    __asm {
        pushad
        pushfd
        push dword ptr [esp+48]
        push dword ptr [esp+48]
        push dword ptr [esp+48]
        push ecx
        push 3
        call on_crypt
        popfd
        popad
        push s_simple_dec
        ret
    }
}

static int try_hook_snow(void)
{
    const unsigned char *a = (const unsigned char *)SNOW_LOADKEY_VA;
    const unsigned char *b = (const unsigned char *)SNOW_ENC_VA;
    const unsigned char *c = (const unsigned char *)SNOW_DEC_VA;

    if (g_snow_hooked) return 1;
    if (IsBadReadPtr(a, 6) || IsBadReadPtr(b, 6) || IsBadReadPtr(c, 6)) return 0;
    if (memcmp(a, SNOW_SIG_LOADKEY, 6) != 0) return 0;   /* 还没解壳到这里 */
    if (memcmp(b, SNOW_SIG_CRYPT, 6) != 0) return 0;
    if (memcmp(c, SNOW_SIG_CRYPT, 6) != 0) return 0;

    s_snow_loadkey = install_inline_hook((void *)SNOW_LOADKEY_VA, (void *)det_snow_loadkey, "SnowCipher::loadkey");
    s_snow_enc     = install_inline_hook((void *)SNOW_ENC_VA,     (void *)det_snow_enc,     "SnowCipher::Encrypt");
    s_snow_dec     = install_inline_hook((void *)SNOW_DEC_VA,     (void *)det_snow_dec,     "SnowCipher::Decrypt");
    if (!IsBadReadPtr((const void *)SIMPLE_ENC_VA, 6) &&
        memcmp((const void *)SIMPLE_ENC_VA, SIMPLE_SIG, 6) == 0)
        s_simple_enc = install_inline_hook((void *)SIMPLE_ENC_VA, (void *)det_simple_enc, "SimpleCipher::Encrypt");
    if (!IsBadReadPtr((const void *)SIMPLE_DEC_VA, 6) &&
        memcmp((const void *)SIMPLE_DEC_VA, SIMPLE_SIG, 6) == 0)
        s_simple_dec = install_inline_hook((void *)SIMPLE_DEC_VA, (void *)det_simple_dec, "SimpleCipher::Decrypt");
    InterlockedExchange(&g_snow_hooked, 1);
    bslog("SNOW    ★cipher hook 安装完毕（Snow x3 + Simple x2）");
    return 1;
}

/* -------------------------------------------------------------------------- */
/* 阶段5 诊断 —— 记录图像引擎初始化的输入和最终 HRESULT。                     */
/*                                                                            */
/* BigShot::RendererInit 0x5bfad4 是 __thiscall：                              */
/*   (width, height, color_depth, mode_flag) -> HRESULT                        */
/* 失败时上层 0x40d4c9 只弹“图像引擎初始化失败”，原错误码会丢失。            */
/* fastcall 的 ECX/EDX 正好兼容 thiscall detour：ECX=self，EDX 为占位参数，    */
/* 四个真正参数仍在栈上，并由 detour ret 0x10 清理。                           */
/* -------------------------------------------------------------------------- */
#define RENDER_INIT_VA 0x005bfad4u
static const unsigned char RENDER_INIT_SIG[5] = { 0xB8,0xC5,0x96,0x61,0x00 };
typedef LONG (__fastcall *render_init_t)(void *, void *, int, int, int, int);
static render_init_t s_render_init = NULL;
static volatile LONG g_render_hooked = 0;

typedef LONG (WINAPI *d3d_create_device_t)(void *, unsigned, unsigned, HWND,
                                           DWORD, void *, void **);
static d3d_create_device_t s_d3d_create_device = NULL;
static volatile LONG g_d3d_create_device_hooked = 0;

struct d3d_display_mode_diag {
    DWORD width, height, refresh_rate, format;
};

static void log_d3d_adapter_state(void *d3d)
{
    void **vft;
    struct d3d_display_mode_diag mode = {0, 0, 0, 0};
    typedef LONG (WINAPI *get_mode_t)(void *, unsigned,
                                      struct d3d_display_mode_diag *);
    typedef LONG (WINAPI *check_type_t)(void *, unsigned, unsigned,
                                        unsigned, unsigned, int);
    get_mode_t get_mode;
    check_type_t check_type;
    LONG hr_mode, hr_type;
    if (!d3d || IsBadReadPtr(d3d, sizeof(void *))) return;
    vft = *(void ***)d3d;
    if (!vft || IsBadReadPtr(vft, 10 * sizeof(void *))) return;
    get_mode = (get_mode_t)vft[8];
    check_type = (check_type_t)vft[9];
    hr_mode = get_mode(d3d, 0, &mode);
    hr_type = check_type(d3d, 0, 1, mode.format, 22, 1);
    bslog("D3D     adapter mode hr=0x%08X %lux%lu refresh=%lu fmt=%lu; "
          "CheckDeviceType(HAL, backFmt=22, windowed=1)=0x%08X",
          (unsigned)hr_mode, (unsigned long)mode.width,
          (unsigned long)mode.height, (unsigned long)mode.refresh_rate,
          (unsigned long)mode.format, (unsigned)hr_type);
}

static LONG WINAPI det_d3d_create_device(void *d3d, unsigned adapter,
                                         unsigned device_type, HWND focus,
                                         DWORD behavior, void *present,
                                         void **device_out)
{
    LONG hr;
    const DWORD *pp = (const DWORD *)present;
    if (pp && !IsBadReadPtr(pp, 14 * sizeof(DWORD))) {
        bslog("D3D     CreateDevice adapter=%u type=%u focus=%08X behavior=0x%08lX "
              "bb=%lux%lu fmt=%lu count=%lu windowed=%lu deviceWnd=%08X",
              adapter, device_type, (unsigned)(UINT_PTR)focus,
              (unsigned long)behavior, (unsigned long)pp[0],
              (unsigned long)pp[1], (unsigned long)pp[2],
              (unsigned long)pp[3], (unsigned long)pp[8], (unsigned)pp[7]);
    }
    hr = s_d3d_create_device(d3d, adapter, device_type, focus, behavior,
                             present, device_out);
    bslog("D3D     CreateDevice -> HRESULT=0x%08X device=%08X",
          (unsigned)hr,
          (unsigned)(UINT_PTR)(device_out && !IsBadReadPtr(device_out, sizeof(void *))
                               ? *device_out : NULL));
    return hr;
}

static int try_hook_d3d_create_device(void *d3d)
{
    void **vft;
    void **entry;
    DWORD oldp;
    if (g_d3d_create_device_hooked) return 1;
    if (!d3d || IsBadReadPtr(d3d, sizeof(void *))) return 0;
    vft = *(void ***)d3d;
    if (!vft || IsBadReadPtr(vft, 17 * sizeof(void *))) return 0;
    entry = &vft[16];
    s_d3d_create_device = (d3d_create_device_t)*entry;
    if (!s_d3d_create_device) return 0;
    if (!VirtualProtect(entry, sizeof(void *), PAGE_EXECUTE_READWRITE, &oldp))
        return 0;
    *entry = (void *)det_d3d_create_device;
    VirtualProtect(entry, sizeof(void *), oldp, &oldp);
    FlushInstructionCache(GetCurrentProcess(), entry, sizeof(void *));
    InterlockedExchange(&g_d3d_create_device_hooked, 1);
    bslog("HOOK    IDirect3D9::CreateDevice vft=%08X original=%08X",
          (unsigned)(UINT_PTR)vft, (unsigned)(UINT_PTR)s_d3d_create_device);
    return 1;
}

static LONG __fastcall det_render_init(void *self, void *edx_unused,
                                       int width, int height,
                                       int color_depth, int mode_flag)
{
    LONG hr;
    HWND focus = NULL;
    RECT rc = {0, 0, 0, 0};
    char cls[96] = "";
    void *d3d = NULL;
    (void)edx_unused;
    if (!IsBadReadPtr(self, 0x274)) {
        d3d = *(void **)self;
        focus = *(HWND *)((unsigned char *)self + 0x270);
    }
    if (focus) {
        GetClientRect(focus, &rc);
        GetClassNameA(focus, cls, sizeof(cls));
    }
    log_d3d_adapter_state(d3d);
    try_hook_d3d_create_device(d3d);
    bslog("D3D     RendererInit enter self=%08X %dx%d colorDepth=%d modeFlag=%d",
          (unsigned)(UINT_PTR)self, width, height, color_depth, mode_flag);
    bslog("D3D     d3d=%08X focus=%08X IsWindow=%d visible=%d class=%s client=%ldx%ld",
          (unsigned)(UINT_PTR)d3d, (unsigned)(UINT_PTR)focus,
          focus ? IsWindow(focus) : 0, focus ? IsWindowVisible(focus) : 0,
          cls[0] ? cls : "(none)", rc.right - rc.left, rc.bottom - rc.top);
    hr = s_render_init(self, NULL, width, height, color_depth, mode_flag);
    if (!IsBadReadPtr((unsigned char *)self + 8, 14 * sizeof(DWORD))) {
        const DWORD *pp = (const DWORD *)((const unsigned char *)self + 8);
        bslog("D3D     PresentParameters bb=%lux%lu fmt=%lu count=%lu ms=%lu quality=%lu swap=%lu",
              (unsigned long)pp[0], (unsigned long)pp[1],
              (unsigned long)pp[2], (unsigned long)pp[3],
              (unsigned long)pp[4], (unsigned long)pp[5],
              (unsigned long)pp[6]);
        bslog("D3D     PresentParameters deviceWnd=%08X windowed=%lu depth=%lu depthFmt=%lu flags=%lu refresh=%lu interval=%lu",
              (unsigned)pp[7], (unsigned long)pp[8],
              (unsigned long)pp[9], (unsigned long)pp[10],
              (unsigned long)pp[11], (unsigned long)pp[12],
              (unsigned long)pp[13]);
    }
    bslog("D3D     RendererInit leave HRESULT=0x%08X (%ld)",
          (unsigned)hr, (long)hr);
    return hr;
}

static int try_hook_render_init(void)
{
    const unsigned char *p = (const unsigned char *)RENDER_INIT_VA;
    if (g_render_hooked) return 1;
    if (IsBadReadPtr(p, sizeof(RENDER_INIT_SIG))) return 0;
    if (memcmp(p, RENDER_INIT_SIG, sizeof(RENDER_INIT_SIG)) != 0) return 0;
    s_render_init = (render_init_t)install_inline_hook(
        (void *)RENDER_INIT_VA, (void *)det_render_init, "RendererInit");
    if (!s_render_init) return 0;
    InterlockedExchange(&g_render_hooked, 1);
    return 1;
}

static DWORD WINAPI patch_thread(LPVOID param)
{
    int ticks = 0;
    (void)param;
    /* ★ 时机是这里唯一重要的事，实测结论（会话03）：
       ASProtect 在启动早期（约 +1.5s）跑一次代码完整性校验，命中就弹
       「Protection Error / Error: 15」并卡在模态框上。
       所以 patch 必须**晚于**那次校验、又必须**早于** GameGuard 校验点
       0x40d034 的执行（约 +5s，见 FINDINGS §9）。中间这段就是唯一的窗口。
       早打（+0.7s）实测只有 1/4 概率蒙混过关，延后到 +2.5s 才稳。 */
    for (ticks = 0; !g_stop && ticks < GG_PATCH_DELAY_MS / 20; ticks++) Sleep(20);
    ticks = 0;
    while (!g_stop && !g_gg_patched && ticks < 2000) {
        if (try_patch_gameguard()) break;
        Sleep(2);
        ticks++;
    }
    bslog("PATCH   patch 线程：延迟 %d ms 后盯了 %d 轮（目标 %08X）",
          GG_PATCH_DELAY_MS, ticks, (unsigned)GG_CHECK_VA);
    if (!g_gg_patched)
        bslog("PATCH   !! 超时未能 patch（0x54b0fc 一直不是预期字节）");

    /* 剩下两处 patch 打在同一个窗口里（解壳已完成、完整性校验窗口已过）。
       ★ **地区锁排在挂机计时器前面，因为只有它是有时限的**：
       0x40b419 属于启动时的 map.ini 加载，一旦跑完地图目录就已经建好，
       再 patch 也补不回被 delete 掉的记录。+2.5s 时资源加载还没开始
       （见下面 SnowCipher 那段的说明），所以来得及 —— 但要是先去死等
       0x4082ae 那 4 秒，就可能刚好错过。挂机计时器反过来完全不急，
       它第一次被执行要等到进大厅。 */
    if (!region_lock_disabled()) {
        bslog("PATCH   BSHOOK_KEEP_REGION_LOCK 已设，保留原版地区差异"
              "（任务只剩 4 关；★ 这时服务端也必须把角色 110 关掉，否则进关卡会崩）");
    } else {
        for (ticks = 0; !g_stop && !g_region_patched && ticks < 2000; ticks++) {
            if (try_patch_region_lock()) break;
            Sleep(2);
        }
        if (!g_region_patched)
            bslog("PATCH   !! 超时未能 patch 地区差异"
                  "（0x40b419 / 0x4368cf / 0x4f67d1 / 0x46631d "
                  "的特征串一直对不上）");
    }

    if (!afk_kick_disabled()) {
        bslog("PATCH   BSHOOK_KEEP_AFK_KICK 已设，保留原版 90 秒挂机踢出");
    } else {
        for (ticks = 0; !g_stop && !g_afk_patched && ticks < 2000; ticks++) {
            if (try_patch_afk_timer()) break;
            Sleep(2);
        }
        if (!g_afk_patched)
            bslog("PATCH   !! 超时未能 patch 挂机计时器"
                  "（0x4082ae 一直不是 68 90 5F 01 00）");
    }

    /* SnowCipher hook 紧跟在 GameGuard patch 之后装：
       此时已过了 ASProtect 的完整性校验窗口（+2.5s 那次 patch 实测 5/5 安全），
       而资源加载(Pack\*.pkn)和登录连接都还没开始，能完整观测到全部加解密。

       ★ 精简模式**根本不装**：这三个 detour 每次加解密都要格式化 + 写日志，
       是全部日志量的 99%，也是启动慢 / 战斗卡的根因（FINDINGS §105）。
       协议早就解完了，日常游玩不需要它。 */
    if (!g_verbose) {
        bslog("SNOW    精简日志模式：跳过 cipher hook（BSHOOK_VERBOSE_LOG=1 开启逐包 dump）");
    } else {
        for (ticks = 0; !g_stop && !g_snow_hooked && ticks < 200; ticks++) {
            if (try_hook_snow()) break;
            Sleep(50);
        }
        if (!g_snow_hooked)
            bslog("SNOW    !! 未能装 SnowCipher hook（序言字节不符）");
    }

    /* 同样等到完整性校验窗口过去后再装，只记录参数和返回值，不改结果。 */
    for (ticks = 0; !g_stop && !g_render_hooked && ticks < 200; ticks++) {
        if (try_hook_render_init()) break;
        Sleep(50);
    }
    if (!g_render_hooked)
        bslog("D3D     !! 未能装 RendererInit hook（序言字节不符）");
    return 0;
}

static DWORD WINAPI watch_thread(LPVOID param)
{
    int ticks = 0;
    (void)param;

    install_hooks();   /* user32 此时必已加载；失败会自动重置标志下一轮重试 */
    while (!g_stop) {
        if (!g_hooks_installed) install_hooks();
        poll_modules();
        EnumWindows(dump_window, 0);
        poll_unpack();
        Sleep(100);
        if (++ticks % 300 == 0) bslog("--- still alive (%d s) ---", ticks / 10);
    }
    return 0;
}

/* -------------------------------------------------------------------------- */
/* 入口                                                                        */
/* -------------------------------------------------------------------------- */

/* 读日志级别。必须在任何 bslog/bsvlog 之前调用。 */
static void read_log_level(void)
{
    char buf[8];
    DWORD n = GetEnvironmentVariableA("BSHOOK_VERBOSE_LOG", buf, sizeof(buf));
    InterlockedExchange(&g_verbose,
                        (n > 0 && n < sizeof(buf) && buf[0] != '0') ? 1 : 0);
}

static void open_log(void)
{
    char path[MAX_PATH * 2];
    char *p;
    SYSTEMTIME st;

    /* 本 DLL 在 <root>\hook\bin\bshook.dll，日志要写到 <root>\logs\ */
    GetModuleFileNameA(GetModuleHandleA("bshook.dll"), path, MAX_PATH);
    p = strrchr(path, '\\'); if (p) *p = 0;   /* -> <root>\hook\bin */
    p = strrchr(path, '\\'); if (p) *p = 0;   /* -> <root>\hook     */
    p = strrchr(path, '\\'); if (p) *p = 0;   /* -> <root>          */
    strcat(path, "\\logs");
    CreateDirectoryA(path, NULL);

    GetLocalTime(&st);
    _snprintf(path + strlen(path), sizeof(path) - strlen(path),
              "\\bshook_%04u%02u%02u_%02u%02u%02u_pid%u.log",
              st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond,
              (unsigned)GetCurrentProcessId());

    g_log = CreateFileA(path, GENERIC_WRITE, FILE_SHARE_READ, NULL,
                        CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
}

static void banner(void)
{
    wchar_t wbuf[MAX_PATH * 2];
    char u8[MAX_PATH * 6];

    bslog("================ bshook injected ================");
    GetModuleFileNameW(NULL, wbuf, MAX_PATH * 2);
    bslog("exe      : %s", w2u8(wbuf, u8, sizeof(u8)));
    bslog("cmdline  : %s", w2u8(GetCommandLineW(), u8, sizeof(u8)));
    GetCurrentDirectoryW(MAX_PATH * 2, wbuf);
    bslog("cwd      : %s", w2u8(wbuf, u8, sizeof(u8)));
    bslog("pid      : %u", (unsigned)GetCurrentProcessId());
    bslog("imagebase: %08X", (unsigned)(UINT_PTR)GetModuleHandleA(NULL));
    bslog("日志级别 : %s", g_verbose ? "详细（BSHOOK_VERBOSE_LOG=1，含逐包 dump，日志 4MB 起）"
                                     : "精简（只记关键事件；调试时设 BSHOOK_VERBOSE_LOG=1）");
}

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID reserved)
{
    HANDLE th;
    (void)reserved;

    switch (reason) {
    case DLL_PROCESS_ATTACH:
        DisableThreadLibraryCalls(inst);
        InitializeCriticalSection(&g_cs);
        /* 读环境变量是纯用户态查表，微秒级，不会挤占下面 patch 线程的时间窗；
           但它必须排在 patch 线程之前 —— 那个线程会按级别决定装不装 cipher hook。 */
        read_log_level();
        /* ★ patch 线程必须第一个起：它要和 ASProtect 的 CRC 基准计算抢时间，
           晚 0.6s（等 open_log 建完文件）就会输，弹 Protection Error。 */
        th = CreateThread(NULL, 0, patch_thread, NULL, 0, NULL);
        if (th) CloseHandle(th);
        open_log();
        banner();
        /* TODO 阶段3: install_ws2_hooks(); */
        th = CreateThread(NULL, 0, watch_thread, NULL, 0, NULL);
        if (th) CloseHandle(th);
        break;

    case DLL_PROCESS_DETACH:
        g_stop = 1;
        bslog("================ process detach ================");
        break;
    }
    return TRUE;
}
