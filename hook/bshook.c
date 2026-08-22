/* ==========================================================================
 * bshook.dll —— 注入到 BigShot.exe 里的观测/补丁模块
 *
 * 阶段 0/1（当前）：只观测，不改行为。
 *   - 记录进程信息、命令行、当前目录
 *   - 轮询模块加载，抓出 GameGuard / SeData / nmcogame 到底有没有被加载、什么时候
 *   - 轮询顶层窗口 + 子控件文字，把 GameGuard 错误框的**完整原文**抓下来
 *   - 记录 ASProtect 解壳完成的时机（用 .text 首字节是否变化判断）
 *
 * 阶段 2：GameGuard 校验点使用 DR0 + VEH，在执行瞬间改寄存器，不改游戏代码
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
#include <stdlib.h>
#include <string.h>
#include <wchar.h>
#include <intrin.h>
#include "gg_bypass.h"
/* ★ 端口号**全部**来自这里，别在本文件里写死任何一个 ——
   它由 tools/gen_ports_h.py 从 server/config.py 生成，build.bat 每次编译
   前都会重新跑一遍。要改端口只改 server/config.py 一处。 */
#include "ports.h"
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
    case 0x33: case 0x85:                            /* xor r/m32,r32 / test r/m32,r32 */
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
/* hook MessageBoxW/A —— 只**观测并放行**，绝不再抑制。                        */
/*                                                                            */
/* ⚠ V0.1 阶段2 这两个 detour 是直接 `return IDOK`（不真正弹框）的：那时要抓的  */
/* 是「谁调用了 GameGuard 的错误框」，而且自动化跑起来没人点确定。             */
/* **那个抑制一直留到了 V0.2，把登录失败的提示框也一起吞了** ——               */
/* 用户输错密码时游戏「一点反应都没有」，日志里却明明白白写着                  */
/* `cap="登录失败" text="认证服务器失败 (20000)"`（FINDINGS §128）。            */
/* 现在 GameGuard 早就绕过了，没有任何框需要被吞掉：一律转发给真的 MessageBox。 */
/* -------------------------------------------------------------------------- */
typedef int (WINAPI *MsgBoxW_t)(HWND, LPCWSTR, LPCWSTR, UINT);
typedef int (WINAPI *MsgBoxA_t)(HWND, LPCSTR, LPCSTR, UINT);
static MsgBoxW_t s_MessageBoxW = NULL;
static MsgBoxA_t s_MessageBoxA = NULL;
static volatile LONG g_hooks_installed = 0;

static void signal_gameguard_failed(void);
static int  gameguard_already_hit(void);
static int  gameguard_retry_allowed(void);

/* 这个框是不是「Game guard文件不存在或已变更，请重新安装Game guard。」？
   中文部分在不同版本里可能变，`Game guard` / `GameGuard` 这半截是拉丁字母，
   直接在 UTF-8 里按大小写不敏感找子串最稳。 */
static int looks_like_gameguard_error(const char *u8)
{
    const char *p;

    if (!u8) return 0;
    for (p = u8; *p; p++) {
        if ((p[0] == 'g' || p[0] == 'G') &&
            _strnicmp(p, "game", 4) == 0) {
            const char *q = p + 4;
            while (*q == ' ') q++;
            if (_strnicmp(q, "guard", 5) == 0) return 1;
        }
    }
    return 0;
}

/* GameGuard 那个错误框 = 绕过失败的硬证据。报给 bsloader，让它自己重来一次。
   返回 1 表示「这一次别真弹给玩家看」（还能重来），0 表示照常弹。 */
static int handle_gameguard_error_box(const char *u8text, const char *u8cap)
{
    if (gameguard_already_hit()) return 0;   /* 已经绕过了，那就是别的框 */
    if (!looks_like_gameguard_error(u8text) &&
        !looks_like_gameguard_error(u8cap)) return 0;

    bslog("HWBP    ★★ GameGuard 绕过失败：客户端弹出了「%s」——"
          " DR0 从头到尾没命中过（§179）", u8text ? u8text : "");
    signal_gameguard_failed();
    if (!gameguard_retry_allowed()) {
        bslog("HWBP    已经是最后一次尝试，错误框照常弹给玩家看");
        return 0;
    }
    bslog("HWBP    先吃掉这个框，bsloader 会自动重来一次");
    return 1;
}

static int WINAPI det_MessageBoxW(HWND hWnd, LPCWSTR text, LPCWSTR cap, UINT type)
{
    void *ra = _ReturnAddress();
    char u8t[3072], u8c[512];
    w2u8(cap, u8c, sizeof(u8c));
    w2u8(text, u8t, sizeof(u8t));
    bslog("★MSGBOXW caller=%08X type=%08x cap=\"%s\"",
          (unsigned)(UINT_PTR)ra, type, u8c);
    bslog("         text=\"%s\"", u8t);
    log_ebp_chain("★MSGBOXW");
    if (handle_gameguard_error_box(u8t, u8c)) return IDOK;
    if (!s_MessageBoxW) return IDOK;            /* 理论上不会发生 */
    return s_MessageBoxW(hWnd, text, cap, type);
}

static int WINAPI det_MessageBoxA(HWND hWnd, LPCSTR text, LPCSTR cap, UINT type)
{
    void *ra = _ReturnAddress();
    bslog("★MSGBOXA caller=%08X type=%08x cap=\"%s\" text=\"%s\"",
          (unsigned)(UINT_PTR)ra, type, cap ? cap : "(null)", text ? text : "(null)");
    log_ebp_chain("★MSGBOXA");
    if (handle_gameguard_error_box(text, cap)) return IDOK;
    if (!s_MessageBoxA) return IDOK;
    return s_MessageBoxA(hWnd, text, cap, type);
}

/* ========================================================================== */
/* V0.2 登录框改造：本机服务器 / 远程服务器 + 指向我们自己的注册页             */
/*                                                                            */
/* 登录框是 #32770 标题 "PopShot"，控件 id 实测（tools\gui_probe.py enum）：   */
/*   1004 用户名 Edit      1005 密码 Edit        1006 「开始」                */
/*   1011 单选钮「炮火连天(电信)」  1012 单选钮「枪林弹雨(网通)」             */
/*   1010 Static「注册成为世纪天成用户」= 那条蓝色链接                        */
/*   1014 Static「选择分区:」        1017/1018/1019 底部说明文字              */
/*   1015 Static「用户名:」(245,310) 1016 Static「密码:」(245,338)，都是 49x14 */
/* 对话框客户区 530x527；1011/1012 在 (98,310) / (98,331)，都是 126x18。      */
/*                                                                            */
/* 配置从**环境变量**来（tools\launch.ps1 解析 server.config 后设进来），      */
/* 这样 C 这边不用碰 UTF-8 配置文件的解析和编码（决策 D065）。                */
/* ========================================================================== */
#define IDC_RADIO_LOCAL    1011
#define IDC_RADIO_ONLINE   1012
#define IDC_REGISTER_LINK  1010
#define IDC_LOGIN_START    1006

/* ★★ 「远程服务器」单选钮 1012 的宽度**上限**，位置一律不动（D098）。       */
/*                                                                            */
/* 为什么有这么个上限（§173）：1012 的右边 x=245 起就是「密码:」那条 Static   */
/* （x=[245,294] y=[338,352]）。1012 一旦加宽加高到把它包进去，两边又都没有   */
/* WS_CLIPSIBLINGS、1012 的 z 序还更靠上 —— 玩家点一次「开始」（客户端禁用    */
/* 单选钮、我们再解禁，1012 重绘两次）就会把「密码:」的字擦掉，而 Static      */
/* 不知道自己被擦，从此不再重画。用户 2026-08-12 报的就是这个。               */
/* 「用户名:」在 y=[310,324]，落在 1012 上面，所以它不受影响 —— 和现象一致。  */
/*                                                                            */
/* 98 + 145 = 243 < 245 ⇒ 两个矩形不相交，纵向再高也压不到它。               */
/* ⚠ 加控件高度是安全的（下面到 y=415 的 1017 之前都是空的），**加宽度不是**。 */
#define RADIO_ONLINE_W     145
#define RADIO_ONLINE_H     36

#ifndef BS_MULTILINE
#define BS_MULTILINE 0x00002000L
#endif

static wchar_t  g_server_addr[256]    = L"127.0.0.1";
/* ★ 下面这些**不是**可配置项，只是「同一个常量在 C 这边的副本」，
   源头全在 server/config.py（经 ports.h 生成）。以前它们各写一个字面量、
   再靠 POPSHOT_*_PORT 环境变量在运行时对齐 —— 那既是重复劳动，也是一类
   「改了这边没改那边」的故障：症状往往不是报错，而是某个功能悄悄不工作。

   ⚠ 例外是下面两个注册页端口：那两个**真的**来自玩家的 server.config，
     所以仍然由启动脚本用环境变量传进来，这里的值只是缺省。 */
static unsigned g_server_reg_port     = POPSHOT_DEFAULT_REGISTER_PORT;
static unsigned g_local_reg_port      = POPSHOT_DEFAULT_REGISTER_PORT;
static unsigned g_relay_auth_port     = POPSHOT_RELAY_AUTH_PORT;
static unsigned g_relay_game_port     = POPSHOT_RELAY_GAME_PORT;
static unsigned g_relay_peer_port     = POPSHOT_RELAY_PEER_PORT;
/* 位置数据 UDP 旁路的本机端口（`server/relay.py` 听这个口）。和游戏服中继
   同号，理由见 `server/config.py` 的 `RELAY_UDP_SYNC_PORT`。 */
static unsigned g_relay_udp_sync_port = POPSHOT_RELAY_UDP_SYNC_PORT;
/* ★ 原版 `UDPBinder` 写死要 bind 的 UDP 端口（`0x5bba92(0x1e6c)`，§153），
   和我们把它改写成的号。改写在 `det_bind` 里做。

   为什么非改不可：下行的位置数据要投进这个口，而「这个口是不是游戏在听」
   必须是**确定**的。7788 是个谁都可能占的低位号，被别的程序占着的话数据就
   投进黑洞 —— 表现是「所有人在你屏幕上定住」，比不开这个功能还糟。
   换成我们自己的号之后：启动脚本先确认它空着，再由游戏去 bind，
   bind 成功由本函数告诉本机中继 —— 三步都是硬的。 */
static unsigned g_game_udp_port       = POPSHOT_GAME_ORIGINAL_UDP_PORT;
static unsigned g_client_udp_port     = POPSHOT_CLIENT_UDP_PORT;
/* 客户端写死的两个端口（V0.1 §24 / §40），只作为「要不要映射」的判据。 */
static unsigned g_auth_port           = POPSHOT_AUTH_PORT;
static unsigned g_game_port           = POPSHOT_GAME_PORT;
/* 原版 TCP 中继的端口（里程碑 J.3 / D078 / D079）。
   前两个是客户端写死的，这个不是 —— 客户端连哪儿完全由服务端在
   `0x0210 gspJoinRelay` 里给的地址说了算，我们让服务端固定填
   `127.0.0.1:27798`，再在这里按模式把它映射出去，和上面两个一个套路。 */
static unsigned g_peer_relay_port     = POPSHOT_PEER_RELAY_PORT;

static HWND         g_login_dlg   = NULL;
static int          g_dlg_styled  = 0;      /* 文案 / 尺寸只改一次 */
static volatile LONG g_online     = 0;      /* 界面当前选择：1 =「远程服务器」 */
/* 玩家点「开始」时把界面选择冻结成这一轮登录的路由。认证服、游戏服和
   战斗中继必须一直用同一份快照，不能被原客户端后续清空单选状态所影响。 */
static volatile LONG g_route_online = 0;
static volatile LONG g_route_locked = 0;
static wchar_t      g_link_text[512] = L"";
/* 登录框本身也要子类化：只在玩家真的点单选钮时更新 g_online。
   原客户端点「开始」后会临时清掉单选状态；若继续每 100ms 从控件反推模式，
   认证刚走完远程服，游戏服就会被误切回本机（票据因此不属于同一台服务器）。 */
static HWND         s_login_hwnd   = NULL;
static WNDPROC      s_login_oldproc = NULL;
/* 被我们子类化的那条注册链接（实现在下面「注册链接的点击」一段）。 */
static HWND         s_link_hwnd   = NULL;

static unsigned env_uint(const char *name, unsigned fallback)
{
    char buf[32];
    DWORD n = GetEnvironmentVariableA(name, buf, sizeof(buf));
    unsigned value;
    if (n == 0 || n >= sizeof(buf)) return fallback;
    value = (unsigned)strtoul(buf, NULL, 10);
    return (value >= 1 && value <= 65535) ? value : fallback;
}

static void read_online_config(void)
{
    wchar_t buf[256];
    DWORD n = GetEnvironmentVariableW(L"POPSHOT_SERVER_ADDRESS", buf,
                                      sizeof(buf) / sizeof(buf[0]));
    char u8[768];
    if (n > 0 && n < sizeof(buf) / sizeof(buf[0])) {
        /* IPv6 可能被写成 [2001:db8::1]，去方括号；拼 URL 时再加回去。 */
        wchar_t *start = buf;
        size_t len;
        while (*start == L' ') start++;
        len = wcslen(start);
        while (len && start[len - 1] == L' ') start[--len] = 0;
        if (len >= 2 && start[0] == L'[' && start[len - 1] == L']') {
            start[len - 1] = 0;
            start++;
        }
        if (*start) wcsncpy(g_server_addr, start, 255), g_server_addr[255] = 0;
    }
    g_server_reg_port = env_uint("POPSHOT_SERVER_REG_PORT", g_server_reg_port);
    g_local_reg_port  = env_uint("POPSHOT_LOCAL_REG_PORT",  g_local_reg_port);
    /* ★ 中继/同步那几个端口**不再读环境变量** —— 它们是编译期就定死的常量
       （ports.h ← server/config.py），启动脚本读的是同一份，没有对不齐的余地。
       只有下面两个注册页端口来自玩家的 server.config，才需要传进来。 */
    bslog("CFG     远程服务器=%s 注册页端口 远端=%u 本机=%u；中继端口 %u/%u/%u"
          "；位置 UDP 旁路 127.0.0.1:%u；游戏收位置的 UDP 口 %u（原版 %u）",
          w2u8(g_server_addr, u8, sizeof(u8)),
          g_server_reg_port, g_local_reg_port,
          g_relay_auth_port, g_relay_game_port, g_relay_peer_port,
          g_relay_udp_sync_port, g_client_udp_port, g_game_udp_port);
}

static int selected_online_mode(void)
{
    return (int)InterlockedCompareExchange(&g_online, 0, 0);
}

/* 当前该显示 / 打开哪台服务器：本机固定 localhost，远程用配置里的地址。 */
static const wchar_t *current_reg_host(void)
{
    return selected_online_mode() ? g_server_addr : L"localhost";
}

static unsigned current_reg_port(void)
{
    return selected_online_mode() ? g_server_reg_port : g_local_reg_port;
}

/* 拼注册页 URL。IPv6 字面量要加方括号，否则冒号会被当成端口分隔符。 */
static void build_register_url(wchar_t *out, int cch)
{
    const wchar_t *host = current_reg_host();
    if (wcschr(host, L':'))
        _snwprintf(out, cch, L"http://[%s]:%u/", host, current_reg_port());
    else
        _snwprintf(out, cch, L"http://%s:%u/", host, current_reg_port());
    out[cch - 1] = 0;
}

int popshot_online_mode(void)
{
    if (InterlockedCompareExchange(&g_route_locked, 0, 0))
        return (int)InterlockedCompareExchange(&g_route_online, 0, 0);
    return selected_online_mode();
}

static void set_online_mode(int online)
{
    LONG value = online ? 1 : 0;
    if (InterlockedExchange(&g_online, value) != value)
        bslog("LOGIN   分区切换 -> %s", value ? "远程服务器" : "本机服务器");
}

static void lock_online_mode(void)
{
    LONG online = selected_online_mode();
    InterlockedExchange(&g_route_online, online);
    InterlockedExchange(&g_route_locked, 1);
    bslog("LOGIN   本轮登录路由锁定 -> %s",
          online ? "远程服务器" : "本机服务器");
}

unsigned popshot_map_port(unsigned port)
{
    /* 正常路径在「开始」按钮的 BN_CLICKED 中锁定；这一层兜底保证即便登录框
       子类化失败，第一次认证连接也会冻结选择，后面的游戏连接不会换服务器。 */
    if (port == g_auth_port && !InterlockedCompareExchange(&g_route_locked, 0, 0))
        lock_online_mode();
    if (!popshot_online_mode()) return port;
    if (port == g_auth_port) return g_relay_auth_port;
    if (port == g_game_port) return g_relay_game_port;
    /* 原版 TCP 中继（D078 / D079）。前两条是客户端写死的端口，这一条不是：
       连哪儿由服务端在 `0x0210` 里说，我们让它固定说 `127.0.0.1:27798`，
       选本机服务器时那就是本机服务端的中继口、选远程服务器时在这里换成
       本机中继的 27808。
       ★ 本机服务器走的是「不映射」那条路，所以这行只在远程模式下生效，
         和上面两条完全同构。 */
    if (port == g_peer_relay_port) return g_relay_peer_port;
    return port;
}

static BOOL CALLBACK find_login_dlg(HWND h, LPARAM lp)
{
    DWORD pid = 0;
    wchar_t cls[32], title[64];
    (void)lp;
    GetWindowThreadProcessId(h, &pid);
    if (pid != GetCurrentProcessId()) return TRUE;
    cls[0] = title[0] = 0;
    GetClassNameW(h, cls, 32);
    if (wcscmp(cls, L"#32770") != 0) return TRUE;
    GetWindowTextW(h, title, 64);
    if (wcscmp(title, L"PopShot") != 0) return TRUE;
    /* 必须有那两个单选钮，才是登录框而不是别的对话框（比如错误提示）。 */
    if (!GetDlgItem(h, IDC_RADIO_LOCAL) || !GetDlgItem(h, IDC_RADIO_ONLINE))
        return TRUE;
    g_login_dlg = h;
    return FALSE;
}

/* 把控件挪成指定的宽 / 高，位置不动。 */
static void resize_ctrl(HWND dlg, int id, int cx, int cy)
{
    HWND h = GetDlgItem(dlg, id);
    RECT r;
    POINT pt;
    if (!h || !GetWindowRect(h, &r)) return;
    pt.x = r.left; pt.y = r.top;
    ScreenToClient(dlg, &pt);
    SetWindowPos(h, NULL, pt.x, pt.y, cx, cy, SWP_NOZORDER | SWP_NOACTIVATE);
    InvalidateRect(h, NULL, TRUE);
}

/* 把某个控件占的那块**连同压在下面的兄弟控件**一起重画。
   `InvalidateRect(子控件)` 只让它自己重画；被它盖住的兄弟收不到通知，
   所以要对父窗口的那块矩形来一发带 RDW_ALLCHILDREN 的 RedrawWindow。 */
static void redraw_area_of(HWND dlg, int id)
{
    HWND h = GetDlgItem(dlg, id);
    RECT r;
    POINT tl, br;
    if (!h || !GetWindowRect(h, &r)) return;
    tl.x = r.left;  tl.y = r.top;
    br.x = r.right; br.y = r.bottom;
    ScreenToClient(dlg, &tl);
    ScreenToClient(dlg, &br);
    r.left = tl.x; r.top = tl.y; r.right = br.x; r.bottom = br.y;
    RedrawWindow(dlg, &r, NULL,
                 RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN | RDW_UPDATENOW);
}

/* 单选钮的 BN_CLICKED 会送到父对话框。只认这个明确的用户选择事件，不能在
   登录进行中继续轮询 BM_GETCHECK：原客户端会禁用并清掉这两个按钮的勾选，
   那是界面内部状态，不代表玩家把「远程服务器」改回了「本机服务器」。 */
static LRESULT CALLBACK login_wndproc(HWND h, UINT msg, WPARAM wp, LPARAM lp)
{
    WNDPROC oldproc = s_login_oldproc;
    LRESULT result;

    if (msg == WM_COMMAND && HIWORD(wp) == BN_CLICKED) {
        int id = LOWORD(wp);
        if (id == IDC_RADIO_LOCAL)
            set_online_mode(0);
        else if (id == IDC_RADIO_ONLINE)
            set_online_mode(1);
        else if (id == IDC_LOGIN_START)
            lock_online_mode();
    }

    result = CallWindowProcW(oldproc, h, msg, wp, lp);
    if (msg == WM_NCDESTROY && h == s_login_hwnd) {
        s_login_hwnd = NULL;
        s_login_oldproc = NULL;
    }
    return result;
}

static void hook_login_dialog(HWND dlg)
{
    UINT local_checked, online_checked;

    if (!dlg || dlg == s_login_hwnd) return;
    s_login_oldproc = (WNDPROC)SetWindowLongPtrW(
        dlg, GWLP_WNDPROC, (LONG_PTR)login_wndproc);
    if (!s_login_oldproc) {
        bslog("LOGIN   !! 登录框子类化失败，分区选择无法可靠锁定");
        return;
    }
    s_login_hwnd = dlg;

    /* 补住极小的启动窗口：如果玩家在子类化完成前已经选过一次，就从当前
       控件状态初始化；只有恰好一个按钮被选中时才采信，两个都没选时保留模式。 */
    local_checked = IsDlgButtonChecked(dlg, IDC_RADIO_LOCAL);
    online_checked = IsDlgButtonChecked(dlg, IDC_RADIO_ONLINE);
    if (online_checked == BST_CHECKED && local_checked != BST_CHECKED)
        set_online_mode(1);
    else if (local_checked == BST_CHECKED && online_checked != BST_CHECKED)
        set_online_mode(0);
}

/* 定义在下面「注册链接的点击」一段。 */
static void hook_register_link(HWND dlg);

static void style_login_dialog(HWND dlg)
{
    HWND online = GetDlgItem(dlg, IDC_RADIO_ONLINE);

    SetDlgItemTextW(dlg, IDC_RADIO_LOCAL, L"本机服务器");
    SetDlgItemTextW(dlg, IDC_RADIO_ONLINE,
                    L"远程服务器\n(IP设置:server.config)");

    /* 「远程服务器(IP设置:server.config)」比原来的「枪林弹雨(网通)」长得多，
       126 像素的原控件会把它裁掉，所以改成**两行**：加 BS_MULTILINE 再把控件
       放高，文案里那个换行符就是断行处（按钮的 DrawText 带 DT_WORDBREAK，认 \n）。

       ★ 宽度只能到 RADIO_ONLINE_W（145），**位置一个像素都不动**（D098）：
       右边 x=245 起就是「密码:」那条 Static，压上去就会把它的字擦掉（§173）。
       145 宽刚好放得下这两行 —— 实机 PrintWindow 抓图逐字核对过，
       第二行的右括号完整。⚠ 以后改文案要**先在真控件上试排版再定**，
       别用 GetTextExtentPoint32W 算：那条路上 ctypes 的默认 restype 会把
       64 位 HFONT 截断，量到的其实是系统默认字体，结果偏大三成（§175）。 */
    if (online) {
        LONG style = GetWindowLongW(online, GWL_STYLE);
        SetWindowLongW(online, GWL_STYLE, style | BS_MULTILINE);
        resize_ctrl(dlg, IDC_RADIO_ONLINE, RADIO_ONLINE_W, RADIO_ONLINE_H);
    }

    /* 注册链接那条 Static 原来只有 152 像素（刚好装下「注册成为世纪天成用户」）。
       换成「在服务器 xxx 上注册用户」之后 xxx 可能是个长域名，直接加宽到底。 */
    resize_ctrl(dlg, IDC_REGISTER_LINK, 460, 18);
    hook_register_link(dlg);
    hook_login_dialog(dlg);

    bslog("LOGIN   登录框已改造：分区单选钮 -> 本机服务器 / 远程服务器，注册链接指向我们自己的服务器");
}

/* 每 100 毫秒跑一次：发现 / 修饰登录框、解禁单选钮并更新链接文字。
   分区选择本身由 login_wndproc 的 BN_CLICKED 跟踪，不能在这里轮询覆盖。 */
static void poll_login_dialog(void)
{
    wchar_t want[512];
    char u8[1536];

    if (g_login_dlg && !IsWindow(g_login_dlg)) {
        /* 登录成功后对话框被销毁。**保留最后一次的模式** —— 之后连游戏服
           时还要用它决定连本机还是连中继。 */
        g_login_dlg = NULL;
        g_dlg_styled = 0;
        s_login_hwnd = NULL;
        s_login_oldproc = NULL;
        s_link_hwnd = NULL;      /* 对话框重建时要重新子类化那条链接 */
        return;
    }
    if (!g_login_dlg) {
        EnumWindows(find_login_dlg, 0);
        if (!g_login_dlg) return;
    }
    if (!g_dlg_styled) {
        style_login_dialog(g_login_dlg);
        g_dlg_styled = 1;
        g_link_text[0] = 0;
    }

    /* ★ 客户端在第一次点「开始」之后就把两个分区单选钮**永久禁用**了
       （原版的想法是「服务器选定了就不许再换」）。登录失败时它不会解禁，
       于是玩家想从「本机服务器」改成「远程服务器」只能重启游戏。对话框还在 = 还没登录成功，
       这时候允许换分区没有任何副作用，所以我们每一轮都把它解禁回来。 */
    {
        HWND local = GetDlgItem(g_login_dlg, IDC_RADIO_LOCAL);
        HWND remote = GetDlgItem(g_login_dlg, IDC_RADIO_ONLINE);
        if ((local && !IsWindowEnabled(local)) ||
            (remote && !IsWindowEnabled(remote))) {
            if (local) EnableWindow(local, TRUE);
            if (remote) EnableWindow(remote, TRUE);
            /* ★ 兜底重画：禁用 + 解禁让两个单选钮各重绘了一次，被它们盖住的
               兄弟控件（Static 不会自己知道字被擦了）要跟着补一发。
               几何上现在已经不重叠了（RADIO_ONLINE_W / §173），这一发是
               为了别的机器上字体或 DPI 不同、控件尺寸和实测对不上的情况。
               只在真的检测到被禁用时跑，一次登录失败最多一发，不会闪。 */
            redraw_area_of(g_login_dlg, IDC_RADIO_LOCAL);
            redraw_area_of(g_login_dlg, IDC_RADIO_ONLINE);
            bslog("LOGIN   分区单选钮被客户端禁用了，已解禁（登录失败后还要能换分区）");
        }
    }

    _snwprintf(want, 512, L"在服务器 %s 上注册用户", current_reg_host());
    want[511] = 0;
    if (wcscmp(want, g_link_text) != 0) {
        wcscpy(g_link_text, want);
        SetDlgItemTextW(g_login_dlg, IDC_REGISTER_LINK, want);
        bslog("LOGIN   注册链接 -> \"%s\"", w2u8(want, u8, sizeof(u8)));
    }
}

/* -------------------------------------------------------------------------- */
/* 注册链接：把已经停机的世纪天成注册页换成我们自己的                          */
/*                                                                            */
/* 原 URL = http://member.tiancity.com/Registration/PopshotReg.aspx（V0.1 §14）*/
/* 只认「注册」那一条，不碰「您忘记密码了吗?」——那是另一个链接，另一件事。   */
/* -------------------------------------------------------------------------- */
typedef HINSTANCE (WINAPI *ShellExecuteW_t)(HWND, LPCWSTR, LPCWSTR, LPCWSTR,
                                            LPCWSTR, INT);
typedef HINSTANCE (WINAPI *ShellExecuteA_t)(HWND, LPCSTR, LPCSTR, LPCSTR,
                                            LPCSTR, INT);
static ShellExecuteW_t s_ShellExecuteW = NULL;
static ShellExecuteA_t s_ShellExecuteA = NULL;

static int is_register_url_w(const wchar_t *s)
{
    if (!s) return 0;
    return (wcsstr(s, L"PopshotReg") || wcsstr(s, L"Registration") ||
            wcsstr(s, L"popshotreg") || wcsstr(s, L"registration")) ? 1 : 0;
}

static HINSTANCE WINAPI det_ShellExecuteW(HWND hwnd, LPCWSTR verb, LPCWSTR file,
                                          LPCWSTR params, LPCWSTR dir, INT show)
{
    char u8[1536];
    wchar_t url[512];
    bslog("★SHELL ShellExecuteW(\"%s\")", w2u8(file ? file : L"(null)", u8, sizeof(u8)));
    if (is_register_url_w(file)) {
        build_register_url(url, 512);
        bslog("★SHELL 注册链接改写 -> \"%s\"", w2u8(url, u8, sizeof(u8)));
        return s_ShellExecuteW(hwnd, verb, url, params, dir, show);
    }
    return s_ShellExecuteW(hwnd, verb, file, params, dir, show);
}

static HINSTANCE WINAPI det_ShellExecuteA(HWND hwnd, LPCSTR verb, LPCSTR file,
                                          LPCSTR params, LPCSTR dir, INT show)
{
    wchar_t wide[512], url[512];
    char u8[1536];
    bslog("★SHELL ShellExecuteA(\"%s\")", file ? file : "(null)");
    if (file) {
        MultiByteToWideChar(CP_ACP, 0, file, -1, wide, 512);
        wide[511] = 0;
        if (is_register_url_w(wide)) {
            build_register_url(url, 512);
            bslog("★SHELL 注册链接改写 -> \"%s\"", w2u8(url, u8, sizeof(u8)));
            /* 我们的 URL 全是 ASCII，直接用宽字符版打开最省事。 */
            if (s_ShellExecuteW)
                return s_ShellExecuteW(hwnd, NULL, url, NULL, NULL, show);
        }
    }
    return s_ShellExecuteA(hwnd, verb, file, params, dir, show);
}

/* 直接指向 shell32!ShellExecuteW 的入口（不是蹦床）。我们自己开注册页时用它 ——
   走一遍自己的 detour 也无所谓：我们的 URL 不含 "Registration"，不会被再改写。 */
static ShellExecuteW_t s_ShellExecuteW_raw = NULL;

/* ★ 只用 `GetModuleHandleA`，**绝不在这里 LoadLibrary**。
   本 DLL 是在 EXE 入口点之前用 APC 注入的，`watch_thread` 跑起来时主线程还在
   ntdll 的 loader 里；从旁边的线程调 `LoadLibrary` 会卡在 loader 锁上，
   实测让 `bsloader` 等不到「DR0 已武装」握手而超时退出
   （`bsloader.err: 等待 bshook.dll 初始化握手 (GetLastError=1460)`）——
   而且是**时有时无**的，第一次跑还成功过。
   shell32 没加载就下一轮再来（`watch_thread` 每 100 毫秒调一次）；
   真到用户点链接那一刻还没有，再当场加载也来得及（进程早就起完了）。 */
static void install_shell_hooks(void)
{
    HMODULE sh;
    if (s_ShellExecuteW_raw) return;             /* 已装 */
    sh = GetModuleHandleA("shell32.dll");
    if (!sh) return;                             /* 还没加载，下一轮再看 */
    s_ShellExecuteW_raw =
        (ShellExecuteW_t)GetProcAddress(sh, "ShellExecuteW");
    s_ShellExecuteW = (ShellExecuteW_t)install_inline_hook(
        (void *)GetProcAddress(sh, "ShellExecuteW"),
        (void *)det_ShellExecuteW, "ShellExecuteW");
    s_ShellExecuteA = (ShellExecuteA_t)install_inline_hook(
        (void *)GetProcAddress(sh, "ShellExecuteA"),
        (void *)det_ShellExecuteA, "ShellExecuteA");
}

/* -------------------------------------------------------------------------- */
/* 注册链接的点击：**客户端自己根本处理不了**，我们接管                        */
/*                                                                            */
/* 实测（V0.2 里程碑 H）：id=1010 那条 Static **没有 SS_NOTIFY** ——           */
/* 没有这个样式的 Static 对 WM_NCHITTEST 返回 HTTRANSPARENT，鼠标消息压根到不了 */
/* 它身上（`WindowFromPoint` 在链接位置返回的是它下面那个分组 Button）。       */
/* 真点上去什么都不发生，也没有任何 ShellExecute 调用。                        */
/*                                                                            */
/* 所以不去猜原版怎么处理的，直接：给它加上 SS_NOTIFY + 子类化窗口过程，       */
/* 自己在 WM_LBUTTONUP 里打开我们的注册页。顺手把鼠标指针换成手型。            */
/* -------------------------------------------------------------------------- */
#ifndef SS_NOTIFY
#define SS_NOTIFY 0x00000100L
#endif

static WNDPROC s_link_oldproc = NULL;

static LRESULT CALLBACK link_wndproc(HWND h, UINT msg, WPARAM wp, LPARAM lp)
{
    wchar_t url[512];
    char u8[1536];

    switch (msg) {
    /* ★ 按下这一半**必须自己吃掉，不能交给原来的 Static 窗口过程**：
       加了 SS_NOTIFY 之后，Static 的默认处理会在 **WM_LBUTTONDOWN** 那一刻
       给对话框发 `WM_COMMAND/STN_CLICKED`，而客户端的对话框过程里**真的有**
       这条链接的处理器 —— 它去开那个早就停机的 member.tiancity.com。
       于是一次点击弹出两个网页：先是它的死链接，再是我们的注册页（§129）。
       双击 / 非客户区按下一并吃掉，堵住同一条路的其它入口。 */
    case WM_LBUTTONDOWN:
    case WM_LBUTTONDBLCLK:
    case WM_NCLBUTTONDOWN:
        return 0;
    case WM_LBUTTONUP:
        build_register_url(url, 512);
        bslog("LOGIN   点了注册链接 -> \"%s\"", w2u8(url, u8, sizeof(u8)));
        if (!s_ShellExecuteW_raw) {
            /* 启动期不敢 LoadLibrary（见 install_shell_hooks 的说明），
               但用户点下去这一刻进程早就起完了，现加载是安全的。 */
            HMODULE sh = LoadLibraryA("shell32.dll");
            if (sh) s_ShellExecuteW_raw =
                (ShellExecuteW_t)GetProcAddress(sh, "ShellExecuteW");
        }
        if (s_ShellExecuteW_raw)
            s_ShellExecuteW_raw(NULL, L"open", url, NULL, NULL, SW_SHOWNORMAL);
        else
            bslog("LOGIN   !! 没拿到 ShellExecuteW，打不开注册页");
        return 0;
    case WM_SETCURSOR:
        /* 本文件按 ANSI 编译，`IDC_HAND` 展开成 MAKEINTRESOURCEA，
           传给宽字符版会类型不符 —— 显式用 MAKEINTRESOURCEW。 */
        SetCursor(LoadCursorW(NULL, MAKEINTRESOURCEW(32649)));
        return TRUE;
    default:
        break;
    }
    return CallWindowProcW(s_link_oldproc, h, msg, wp, lp);
}

static void hook_register_link(HWND dlg)
{
    HWND link = GetDlgItem(dlg, IDC_REGISTER_LINK);
    LONG style;
    if (!link || link == s_link_hwnd) return;
    style = GetWindowLongW(link, GWL_STYLE);
    SetWindowLongW(link, GWL_STYLE, style | SS_NOTIFY);
    /* ★ 光加 SS_NOTIFY 还不够：那条 Static 在 z 序上**压在分组框下面**
       （EnumChildWindows 按 z 序返回，分组 Button 排在所有 Static 前面），
       点上去 `WindowFromPoint` 拿到的是分组框，鼠标消息到不了链接。
       把它提到最上面才真的可点。 */
    SetWindowPos(link, HWND_TOP, 0, 0, 0, 0,
                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
    s_link_oldproc = (WNDPROC)SetWindowLongPtrW(link, GWLP_WNDPROC,
                                                (LONG_PTR)link_wndproc);
    s_link_hwnd = link;
    bslog("LOGIN   注册链接已接管（原版这条 Static 没有 SS_NOTIFY 且被分组框压住，"
          "点了没反应）");
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

typedef int (WINAPI *bind_t)(SOCKET_T, const struct sockaddr_min *, int);
typedef int (WINAPI *connect_t)(SOCKET_T, const struct sockaddr_min *, int);
typedef int (WINAPI *WSAConnect_t)(SOCKET_T, const struct sockaddr_min *, int,
                                   void *, void *, void *, void *);
typedef void *(WINAPI *gethostbyname_t)(const char *);
typedef int (WINAPI *getaddrinfo_t)(const char *, const char *, const void *, void *);

static bind_t          s_bind = NULL;
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

/* 阶段3：把游戏 TCP 连接重定向到 127.0.0.1。原始目标已在 log_sockaddr 记下。
   置 0 则纯观测不改写。 */
static int g_redirect = 1;

/* V0.2：登录框里选了「远程服务器」。定义在下面的「登录框改造」一段。 */
int  popshot_online_mode(void);
unsigned popshot_map_port(unsigned port);

/* 游戏成功 bind 了「收位置数据的 UDP 口」之后告诉本机中继。
   定义在下面「位置数据的 UDP 旁路」一段。 */
static void sync_on_udp_bound(void);

/* 若是 IPv4 且开启重定向：地址改成 127.0.0.1，端口按当前模式映射。
   返回 1 并填好 out；否则返回 0。

   ★ 本机服务器 / 远程服务器的区分就在这里，靠**端口**而不是靠别处传状态（决策 D066）：
       本机服务器（单选钮 1011）-> 127.0.0.1:47611 / 27799 = 本机服务端
       远程服务器（单选钮 1012）-> 127.0.0.1:47621 / 27809 = 本机中继，它再连远端
   玩家点「开始」时会冻结一次模式；这一轮后续所有 `connect` 都使用同一份快照，
   不再读取会被原客户端清空的单选钮状态。 */
static int make_localhost(const struct sockaddr_min *name, int namelen, struct sockaddr_in_min *out)
{
    unsigned port, mapped;
    if (!g_redirect || !name || namelen < 16 || name->sa_family != AF_INET_MIN) return 0;
    memcpy(out, name, sizeof(*out));
    port = ((unsigned)out->sin_port[0] << 8) | out->sin_port[1];
    mapped = popshot_map_port(port);
    if (mapped != port) {
        out->sin_port[0] = (unsigned char)((mapped >> 8) & 0xff);
        out->sin_port[1] = (unsigned char)(mapped & 0xff);
    }
    out->sin_addr[0] = 127; out->sin_addr[1] = 0; out->sin_addr[2] = 0; out->sin_addr[3] = 1;
    bslog("★WS2 重定向 -> 127.0.0.1:%u  (原端口 %u, 模式=%s)", mapped, port,
          popshot_online_mode() ? "远程服务器" : "本机服务器");
    return 1;
}

/* 定义在下方 SnowCipher 段：把 SNOW 日志计数器清零。
   启动时加载 Pack\*.pkn 会把配额一次性烧光，网络阶段就什么都记不到了，
   所以每次 connect 都重开一个干净的记录窗口。 */
void snow_log_reset(void);

/* ★ 把原版写死的 UDP 7788 改写成我们自己的号（`g_client_udp_port`）。

   原版 `GameSession` 构造时 `new UDPBinder` 之后就 bind 7788（§153），
   下行的位置数据要投进的就是这个口。7788 是个谁都可能占的低位号 ——
   被别人占着的话我们投进去的数据就石沉大海，而**在外面根本看不出来**
   （UDP 没有连接、没有回执），表现是「所有人在你屏幕上定住」。

   换成我们自己的号，「这个口是不是游戏在听」就变成三步硬判据：
     1. 启动脚本先确认它空着（占用就直接报错不启动）；
     2. 游戏在这里 bind 它；
     3. bind **成功**之后由 `sync_on_udp_bound()` 告诉本机中继可以投了。

   ⚠ 只改端口号 7788 这一个条件，其余 bind 一律原样放行 —— 客户端的 TCP
     socket 不显式 bind，所以这个判据是唯一的。 */
static int WINAPI det_bind(SOCKET_T s, const struct sockaddr_min *name, int namelen)
{
    struct sockaddr_in_min sa;
    unsigned port;
    int rc;

    if (!name || namelen < 16 || name->sa_family != AF_INET_MIN)
        return s_bind(s, name, namelen);
    memcpy(&sa, name, sizeof(sa));
    port = ((unsigned)sa.sin_port[0] << 8) | sa.sin_port[1];
    if (port != g_game_udp_port)
        return s_bind(s, name, namelen);

    sa.sin_port[0] = (unsigned char)((g_client_udp_port >> 8) & 0xff);
    sa.sin_port[1] = (unsigned char)(g_client_udp_port & 0xff);
    rc = s_bind(s, (const struct sockaddr_min *)&sa, (int)sizeof(sa));
    if (rc == 0) {
        bslog("★WS2 bind %u -> %u（游戏收位置数据的 UDP 口，已改写）",
              port, g_client_udp_port);
        sync_on_udp_bound();
    } else {
        /* 到这一步还失败，说明启动脚本的端口检查之后又被人抢了。
           原版会弹「…(Bind Fail)」，我们只多记一行 —— 下行照旧走 TCP。 */
        bslog("★WS2 !! bind %u 失败，位置数据的下行继续走 TCP（不影响游戏）",
              g_client_udp_port);
    }
    return rc;
}

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

    s_bind = (bind_t)install_inline_hook(
        (void *)GetProcAddress(ws2, "bind"), (void *)det_bind, "ws2:bind");
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

    install_ws2_hooks();    /* ws2_32 是静态导入, 此时已加载 */
    install_shell_hooks();  /* 注册链接改写（V0.2 里程碑 H）*/
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
/* 阶段2 —— GameGuard 校验用 DR0 + VEH 在执行瞬间绕过                          */
/*                                                                            */
/*   校验点 va=0x54b0fc: call 0x5611d0 (E8 CF 60 01 00)                        */
/*   DR0 在该指令执行前触发 EXCEPTION_SINGLE_STEP；处理器把 EAX 设成 0x755，  */
/*   EIP 前移 5 字节，等价于执行 `mov eax,0x755`。代码页一个字节都不改，      */
/*   因此 ASProtect 的后台 CRC 无论早晚运行都看不到变化。                      */
/*                                                                            */
/*   DLL 内部线程等**目标指令真的解壳**后设置 DR0，再向 bsloader 发“已武装”     */
/*   握手；命中前每 10ms 复查一次 DR0，被壳/安全软件清掉就补回去（§134）。      */
/* -------------------------------------------------------------------------- */
static const unsigned char GG_ORIG[POPSHOT_GG_CHECK_INSN_LEN] =
    POPSHOT_GG_ORIG_BYTES;
static const unsigned char GG_OLD_PATCH[POPSHOT_GG_CHECK_INSN_LEN] =
    POPSHOT_GG_OLD_PATCH_BYTES;

static PVOID         g_gg_veh = NULL;
static volatile LONG g_gg_break_state = 0;    /* 1=成功，2=旧 patch，-1=字节不符 */
static volatile LONG g_gg_break_reported = 0;
static DWORD         g_main_thread_id = 0;
/* DllMain 跑到哪一刻的 tick。武装线程用它算「我被加载器锁压了多久」（§179）。 */
static DWORD         g_dllmain_tick = 0;

/* 回报给 bsloader 的两个结果事件（DllMain 里打开，进程活多久就留多久）。 */
static HANDLE        g_gg_hit_event = NULL;
static HANDLE        g_gg_failed_event = NULL;
static volatile LONG g_gg_hit_signaled = 0;
static volatile LONG g_gg_failed_signaled = 0;
/* "1" = bsloader 还能再重来一次，那就别把 GameGuard 的错误框弹给玩家看。 */
static volatile LONG g_gg_retry_allowed = 0;

static int gameguard_already_hit(void)
{
    return InterlockedCompareExchange(&g_gg_break_state, 0, 0) > 0;
}

static int gameguard_retry_allowed(void)
{
    return InterlockedCompareExchange(&g_gg_retry_allowed, 0, 0) != 0;
}

/* 「DR0 命中过」—— 绕过成功的唯一硬证据，只报一次。 */
static void signal_gameguard_hit(void)
{
    if (InterlockedExchange(&g_gg_hit_signaled, 1)) return;
    if (g_gg_hit_event) SetEvent(g_gg_hit_event);
}

/* 「客户端弹了 GameGuard 的错误框」—— 绕过失败的硬证据，只报一次。 */
static void signal_gameguard_failed(void)
{
    if (InterlockedExchange(&g_gg_failed_signaled, 1)) return;
    if (g_gg_failed_event) SetEvent(g_gg_failed_event);
}

/* 命中之前每隔这么久复查一次 DR0 还在不在。10ms 足够快（从解壳到执行到
   0x54b0fc 有好几秒），而每轮只是挂起-读-恢复主线程一次，开销可以忽略。 */
#define GG_BREAKPOINT_WATCHDOG_MS 10u

static void clear_dr0(CONTEXT *ctx)
{
    ctx->Dr0 = 0;
    ctx->Dr6 = 0;
    ctx->Dr7 &= ~(DWORD)POPSHOT_DR0_CONTROL_MASK;
}

static LONG CALLBACK gameguard_veh(EXCEPTION_POINTERS *ep)
{
#if defined(_M_IX86)
    CONTEXT *ctx;
    const unsigned char *code;

    if (!ep || !ep->ExceptionRecord || !ep->ContextRecord) return EXCEPTION_CONTINUE_SEARCH;
    if (ep->ExceptionRecord->ExceptionCode != EXCEPTION_SINGLE_STEP)
        return EXCEPTION_CONTINUE_SEARCH;

    ctx = ep->ContextRecord;
    if (ctx->Eip != (DWORD)POPSHOT_GG_CHECK_VA) return EXCEPTION_CONTINUE_SEARCH;

    /* 无论签名是否匹配都先撤掉本断点，避免异常风暴。字节不符时让原指令执行，
       后台线程会写出明确诊断；绝不在未知版本上盲目改 EIP。 */
    clear_dr0(ctx);
    code = (const unsigned char *)POPSHOT_GG_CHECK_VA;

    /* ★ 两条成功分支都就地告诉 bsloader「绕过成功」，它才好立刻停掉外部的
       补武装（§179）—— 别指望 watch_thread 去报，那条线程可能还被加载器锁
       压着。这里只有一次 InterlockedExchange + NtSetEvent，不分配、不取锁，
       在 VEH 里做是安全的；而且只有 EIP 正好等于校验点时才会走到。 */
    if (memcmp(code, GG_ORIG, POPSHOT_GG_CHECK_INSN_LEN) == 0) {
        ctx->Eax = (DWORD)POPSHOT_GG_SUCCESS_CODE;
        ctx->Eip += (DWORD)POPSHOT_GG_CHECK_INSN_LEN;
        InterlockedExchange(&g_gg_break_state, 1);
        signal_gameguard_hit();
    } else if (memcmp(code, GG_OLD_PATCH, POPSHOT_GG_CHECK_INSN_LEN) == 0) {
        /* 已经是旧版 `mov eax,0x755`：撤断点后从原 EIP 正常执行即可。 */
        InterlockedExchange(&g_gg_break_state, 2);
        signal_gameguard_hit();
    } else {
        /* 字节签名不认识 —— 这不算绕过成功，**不要**报 HIT，
           让 bsloader 按「失败」处理（多半会重来一次）。 */
        InterlockedExchange(&g_gg_break_state, -1);
    }
    return EXCEPTION_CONTINUE_EXECUTION;
#else
    (void)ep;
    return EXCEPTION_CONTINUE_SEARCH;
#endif
}

static void report_gameguard_breakpoint(void)
{
    LONG state = InterlockedCompareExchange(&g_gg_break_state, 0, 0);
    if (!state || InterlockedExchange(&g_gg_break_reported, 1)) return;

    if (state == 1) {
        signal_gameguard_hit();
        bslog("HWBP    ★GameGuard 校验 @ %08X：DR0 命中，EAX=0x755，跳过状态取值调用",
              (unsigned)POPSHOT_GG_CHECK_VA);
    } else if (state == 2) {
        signal_gameguard_hit();   /* 旧 patch 也算绕过成功，别让 bsloader 白重来 */
        bslog("HWBP    GameGuard 校验 @ %08X 已是旧版内存 patch，撤掉 DR0 后继续",
              (unsigned)POPSHOT_GG_CHECK_VA);
    } else {
        bslog("HWBP    !! GameGuard 校验 @ %08X 已执行但指令签名不符，未绕过",
              (unsigned)POPSHOT_GG_CHECK_VA);
    }
}

/* 0x54b0fc 这 5 个字节能不能安全读？ASProtect 解壳前那一段可能还没提交，
   或者带着 PAGE_GUARD。VirtualQuery 只问页属性，不碰内容 —— 不像
   IsBadReadPtr 那样会真去踩一脚，把壳的 guard page 异常吃掉。 */
static int gameguard_code_readable(void)
{
    MEMORY_BASIC_INFORMATION mbi;
    const DWORD readable = PAGE_READONLY | PAGE_READWRITE | PAGE_WRITECOPY |
                           PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE |
                           PAGE_EXECUTE_WRITECOPY;

    if (!VirtualQuery((LPCVOID)POPSHOT_GG_CHECK_VA, &mbi, sizeof(mbi)))
        return 0;
    if (mbi.State != MEM_COMMIT) return 0;
    if (mbi.Protect & (PAGE_GUARD | PAGE_NOACCESS)) return 0;
    if (!(mbi.Protect & readable)) return 0;
    /* 这条指令不能跨到下一页去（跨了就得再查一次；实际不会，留个保险）。 */
    if ((BYTE *)POPSHOT_GG_CHECK_VA + POPSHOT_GG_CHECK_INSN_LEN >
        (BYTE *)mbi.BaseAddress + mbi.RegionSize) return 0;
    return 1;
}

/* 目标指令解壳了吗？1 = 原版 call，2 = 兼容的旧内存 patch，0 = 还是密文/未知。 */
static int gameguard_instruction_state(void)
{
    const unsigned char *code = (const unsigned char *)POPSHOT_GG_CHECK_VA;

    if (!gameguard_code_readable()) return 0;
    if (memcmp(code, GG_ORIG, POPSHOT_GG_CHECK_INSN_LEN) == 0) return 1;
    if (memcmp(code, GG_OLD_PATCH, POPSHOT_GG_CHECK_INSN_LEN) == 0) return 2;
    return 0;
}

/* 在主线程上保证 DR0 == 目标执行断点。
   返回 1 = 这次写进去/补回去了，2 = 本来就对，3 = 断点已经命中，0 = API 失败。
   ★ 挂起期间**绝不能调 bslog** —— 主线程可能正拿着日志的锁，那是死锁。 */
static int ensure_gameguard_breakpoint(HANDLE main_thread, DWORD *eip_out,
                                       DWORD *old_dr0_out, DWORD *old_dr7_out,
                                       DWORD *error_out)
{
    CONTEXT ctx;
    int changed = 0;

    if (error_out) *error_out = ERROR_SUCCESS;
    if (SuspendThread(main_thread) == (DWORD)-1) {
        if (error_out) *error_out = GetLastError();
        return 0;
    }

    /* 挂起之后再查一次：VEH 可能就在这一瞬间命中并撤掉了 DR0，
       此时再武装一遍就等于在同一个地址上放了个永远不会被撤的断点。 */
    if (InterlockedCompareExchange(&g_gg_break_state, 0, 0) != 0) {
        ResumeThread(main_thread);
        return 3;
    }

    ZeroMemory(&ctx, sizeof(ctx));
    ctx.ContextFlags = CONTEXT_CONTROL | CONTEXT_DEBUG_REGISTERS;
    if (!GetThreadContext(main_thread, &ctx)) {
        if (error_out) *error_out = GetLastError();
        ResumeThread(main_thread);
        return 0;
    }

    if (eip_out) *eip_out = ctx.Eip;
    if (old_dr0_out) *old_dr0_out = ctx.Dr0;
    if (old_dr7_out) *old_dr7_out = ctx.Dr7;

    if (ctx.Dr0 != (DWORD)POPSHOT_GG_CHECK_VA ||
        (ctx.Dr7 & (DWORD)POPSHOT_DR0_CONTROL_MASK) !=
            (DWORD)POPSHOT_DR0_LOCAL_ENABLE) {
        ctx.Dr0 = (DWORD)POPSHOT_GG_CHECK_VA;
        ctx.Dr6 = 0;
        ctx.Dr7 &= ~(DWORD)POPSHOT_DR0_CONTROL_MASK;
        ctx.Dr7 |= (DWORD)POPSHOT_DR0_LOCAL_ENABLE;
        if (!SetThreadContext(main_thread, &ctx)) {
            if (error_out) *error_out = GetLastError();
            ResumeThread(main_thread);
            return 0;
        }
        changed = 1;
    }

    if (ResumeThread(main_thread) == (DWORD)-1) {
        if (error_out) *error_out = GetLastError();
        return 0;
    }
    return changed ? 1 : 2;
}

static DWORD WINAPI arm_gameguard_breakpoint_thread(LPVOID param)
{
    HANDLE ready_event = (HANDLE)param;
    HANDLE main_thread;
    DWORD budget;
    DWORD started;
    DWORD elapsed;
    DWORD ticks;
    DWORD armed_eip = 0;
    DWORD old_dr0 = 0;
    DWORD old_dr7 = 0;
    DWORD error = ERROR_SUCCESS;
    DWORD repair_count = 0;
    int instruction_state = 0;
    int arm_result = 0;

    /* SYNCHRONIZE 是为了 WaitForSingleObject(main_thread, 0) —— 主线程没了
       就别再空转到超时。 */
    main_thread = OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT |
                             THREAD_SET_CONTEXT | THREAD_QUERY_INFORMATION |
                             SYNCHRONIZE,
                             FALSE, g_main_thread_id);
    if (!main_thread) {
        bslog("HWBP    !! OpenThread(main tid=%lu) 失败 err=%lu",
              (unsigned long)g_main_thread_id, (unsigned long)GetLastError());
        CloseHandle(ready_event);
        return 1;
    }

    /* ★★ 什么时候才能武装 DR0 —— 踩过两个坑（§124 + §134）：

       坑一（V0.2 会话 01 修，§124）：循环上限按**次数**算，把毫秒常量当成了
       循环次数。`Sleep(1)` 按系统定时器精度取整（默认 15.6ms），于是
       10000「毫秒」实际上是 10000 次 × 最多 15.6ms；反过来 bsloader 只等
       10 秒就判超时。表现是**时有时无的启动失败**
       （`bsloader.err: 等待 bshook.dll 初始化握手 (GetLastError=1460)`）。
       所以上限必须按 `GetTickCount()` 算，并留 2 秒余量让 DLL 先写下
       失败原因，再轮到 bsloader 报超时。

       坑二（V0.2 会话 03 修，§134）：判据是「主线程 EIP 已经不在
       ntdll/kernel32/kernelbase/bshook 里」—— 这根本证明不了 APC 已经收尾。
       别人的机器上主线程一注入完就回到了**主模块自己的 ASProtect 壳**
       （EIP=0x007b27xx，5 毫秒就满足条件），DR0 在壳还在跑的时候就写下去，
       随后壳 NtContinue 恢复旧 CONTEXT 把 Dr0 清回 0。日志上写着「已武装」，
       断点却永远不命中，游戏弹「Game guard文件不存在或已变更」。
       我这台机器只是碰巧慢了 720ms，采样时壳已经跑完 —— 纯运气。

       现在换成一个**因果性**的判据：0x54b0fc 那 5 个字节变成已知明文
       （原版 call 或旧 patch）。ASProtect 解壳是整段一次性做完的
       （UNPACK 日志里 base+0x1000 一步从密文变明文），字节对上就说明壳
       已经真的跑过了。再加一条 10ms 的守护回合，被清掉就补回去。

       坑三（V0.2 会话 15，§179）：**这条线程自己就可能好几秒才被调度到。**
       它是在 DllMain 里 CreateThread 出来的，线程入口要等加载器锁放开才跑；
       用户那台机器的日志里，本线程和另外两条观测线程的第一行日志都卡在
       注入后 **3.3 秒**（三条同一毫秒一起解冻），而 GameGuard 校验在那之后
       只有 2.2 秒就执行了。余量全看别人机器的加载器锁攥多久 —— 这就是
       「有概率启动报错」的race。所以现在 **bsloader 从进程外也武装一遍**
       （它不受加载器锁约束），本线程只要一跑起来就接管守护；
       等它跑起来时断点可能**已经命中过了**（下面 arm_result == 3 那一支）。 */
    bslog("HWBP    武装线程开始运行（DllMain 之后 %lu ms），指令状态=%d",
          (unsigned long)(GetTickCount() - g_dllmain_tick),
          gameguard_instruction_state());

    budget = POPSHOT_BSHOOK_READY_TIMEOUT - 2000u;
    started = GetTickCount();
    for (ticks = 0; !g_stop; ticks++) {
        if ((DWORD)(GetTickCount() - started) >= budget) break;
        instruction_state = gameguard_instruction_state();
        if (instruction_state != 0) {
            arm_result = ensure_gameguard_breakpoint(main_thread, &armed_eip,
                                                     &old_dr0, &old_dr7, &error);
            if (arm_result != 0) break;
        }
        if (WaitForSingleObject(main_thread, 0) == WAIT_OBJECT_0) break;
        Sleep(1);
    }
    elapsed = (DWORD)(GetTickCount() - started);
    if (arm_result != 1 && arm_result != 2 && arm_result != 3 &&
        error == ERROR_SUCCESS) {
        error = (WaitForSingleObject(main_thread, 0) == WAIT_OBJECT_0)
                    ? ERROR_PROCESS_ABORTED : ERROR_TIMEOUT;
    }

    if (arm_result == 3) {
        /* bsloader 抢在前面武装了，而且断点在本线程被调度到之前就命中了。
           这是正常且理想的路径 —— 别再武装一次（那会留下一个永不撤销的
           断点），直接回报就绪。 */
        signal_gameguard_hit();
        bslog("HWBP    断点在武装线程启动前就已命中（bsloader 已从外部武装）");
        if (!SetEvent(ready_event)) {
            bslog("HWBP    !! SetEvent(bsloader ready) 失败 err=%lu",
                  (unsigned long)GetLastError());
        }
        CloseHandle(ready_event);
        CloseHandle(main_thread);
        return 0;
    }

    if (arm_result == 1 || arm_result == 2) {
        bslog("HWBP    目标指令已解壳（%lu ms / %lu 轮，%s），"
              "主线程 EIP=%08X，DR0=%08X 已武装%s",
              (unsigned long)elapsed, (unsigned long)ticks,
              instruction_state == 1 ? "原始 call" : "旧 patch",
              (unsigned)armed_eip, (unsigned)POPSHOT_GG_CHECK_VA,
              arm_result == 2 ? "（bsloader 已提前武装，这里只是接管守护）" : "");
        if (!SetEvent(ready_event)) {
            bslog("HWBP    !! SetEvent(bsloader ready) 失败 err=%lu",
                  (unsigned long)GetLastError());
        }
    } else {
        /* ★ 兜底：等不到已知签名也要**照样武装**，绝不能比改之前更差。
           「一直等不到」的可能原因是别人手里的 exe 被别的东西改过 5 个字节，
           或者这台机器上解壳走了另一条路。这时退回旧行为（直接武装 + 守护）
           至少还有机会命中；命中后 VEH 自己会做签名判定，对不上就只撤断点、
           让原指令正常执行，不会瞎改 EIP。 */
        bslog("HWBP    !! 等不到目标指令解出已知签名"
              "（%lu ms / %lu 轮，指令状态=%d，err=%lu）——"
              "仍然武装 DR0 并守护，靠 VEH 的签名判定兜底",
              (unsigned long)elapsed, (unsigned long)ticks,
              instruction_state, (unsigned long)error);
        arm_result = ensure_gameguard_breakpoint(main_thread, &armed_eip,
                                                 &old_dr0, &old_dr7, &error);
        if (arm_result != 1 && arm_result != 2) {
            bslog("HWBP    !! 兜底武装也失败了 err=%lu", (unsigned long)error);
            CloseHandle(main_thread);
            CloseHandle(ready_event);
            return 1;
        }
        bslog("HWBP    兜底已武装 DR0=%08X（主线程 EIP=%08X）",
              (unsigned)POPSHOT_GG_CHECK_VA, (unsigned)armed_eip);
        if (!SetEvent(ready_event)) {
            bslog("HWBP    !! SetEvent(bsloader ready) 失败 err=%lu",
                  (unsigned long)GetLastError());
        }
    }

    CloseHandle(ready_event);

    /* 命中之前一直守着。某些 Windows / 驱动 / 安全软件组合会在
       SetThreadContext 成功之后再恢复一份旧 CONTEXT；只写一次的话日志上
       是「已武装」，断点却永不命中（§134 就是这么炸的）。
       从解壳到执行到 0x54b0fc 只有几秒，命中后立刻退出，开销可以忽略。 */
    while (!g_stop && InterlockedCompareExchange(&g_gg_break_state, 0, 0) == 0) {
        Sleep(GG_BREAKPOINT_WATCHDOG_MS);
        if (WaitForSingleObject(main_thread, 0) == WAIT_OBJECT_0) break;

        arm_result = ensure_gameguard_breakpoint(main_thread, &armed_eip,
                                                 &old_dr0, &old_dr7, &error);
        if (arm_result == 1) {
            repair_count++;
            bslog("HWBP    !! DR0 被清掉了（原 Dr0=%08X Dr7=%08X），已补回"
                  "（第 %lu 次）",
                  (unsigned)old_dr0, (unsigned)old_dr7,
                  (unsigned long)repair_count);
        } else if (arm_result == 0) {
            bslog("HWBP    !! 守护 DR0 时线程上下文操作失败 err=%lu",
                  (unsigned long)error);
            break;
        }
    }
    /* ★ 命中的回报不能只挂在 watch_thread 上（它同样可能被加载器锁压着）。
       这里是「守护到命中为止」的唯一出口，就近报一次最保险。 */
    if (InterlockedCompareExchange(&g_gg_break_state, 0, 0) > 0)
        signal_gameguard_hit();

    CloseHandle(main_thread);
    return 0;
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
/* 握手版本号补丁 —— 客户端连游戏服时裸发的 int32 版本号（V0.2 版本管理）     */
/*                                                                            */
/*   原版客户端在 ServerConnection::OnConnect(0x54d965) 里写死发 311：        */
/*     0x54d98f  c7 45 f0 37 01 00 00    mov dword [ebp-0x10], 311            */
/*   这 4 个字节（0x54d992 起）是整条 SimpleCipher 加密流的开头，服务端解开    */
/*   后当版本号用。这里把它补丁成「BUILD.ver 里的版本号」的编码：            */
/*     wire = major*1000000 + minor*1000 + patch   （server/versioning.py）   */
/*   —— 仍是原样 4 个字节，流布局一个位都不动；服务端收到 311 就知道对面是    */
/*   没上报版本的旧版客户端。指令特征串全镜像唯一（re/BigShot_*.img 核对过）。*/
/*                                                                            */
/*   ★ 版本号**不编译进本 DLL**：每次启动读 <root>\BUILD.ver（打包脚本写入， */
/*     开发环境由 tools/launch.ps1 生成）。日常发版只换 BUILD.ver，不用重编。 */
/* -------------------------------------------------------------------------- */

#define HS_VER_VA 0x0054d98fu
static const unsigned char HS_VER_ORIG[7] =
    { 0xc7, 0x45, 0xf0, 0x37, 0x01, 0x00, 0x00 };
static volatile LONG g_hsver_patched = 0;
static long g_hsver_wire = 0;        /* 0 = 没读到 BUILD.ver，保持 311 上报 */
static char g_hsver_text[40] = "";   /* 日志用，如 "V0.2.7" */

/* 包根目录 = 本 DLL（<root>\hook\bin\bshook.dll）往上三级，算法同 open_log。 */
static void package_root_dir(char *out, size_t cap)
{
    char *p;
    GetModuleFileNameA(GetModuleHandleA("bshook.dll"), out, (DWORD)cap);
    p = strrchr(out, '\\'); if (p) *p = 0;   /* -> <root>\hook\bin */
    p = strrchr(out, '\\'); if (p) *p = 0;   /* -> <root>\hook     */
    p = strrchr(out, '\\'); if (p) *p = 0;   /* -> <root>          */
}

/* 读 <root>\BUILD.ver 里的 "version":"Vx.y.z" -> 编码 wire。
   返回 1 成功；0 = 文件不存在 / 认不出（调用方按「保持 311」处理）。
   JSON 是我们自己脚本写的，只认 "version" 这一个键，不做完整解析。 */
static int read_build_ver(void)
{
    char path[MAX_PATH * 2];
    char buf[2048];
    HANDLE f;
    DWORD got = 0;
    char *key, *val, *p;
    unsigned seg[3], wire;
    int i, digits;
    size_t vlen;

    package_root_dir(path, sizeof(path));
    strcat(path, "\\BUILD.ver");
    f = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ, NULL,
                    OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (f == INVALID_HANDLE_VALUE) return 0;
    if (!ReadFile(f, buf, sizeof(buf) - 1, &got, NULL)) got = 0;
    CloseHandle(f);
    buf[got] = 0;

    key = strstr(buf, "\"version\"");
    if (!key) return 0;
    val = strchr(key + 9, '"');            /* 键结束后的下一个引号 = 值的开头 */
    if (!val) return 0;
    val++;
    p = strchr(val, '"');                  /* 值的结尾 */
    if (!p) return 0;
    vlen = (size_t)(p - val);
    if (vlen == 0 || vlen >= sizeof(g_hsver_text)) return 0;
    memcpy(g_hsver_text, val, vlen);
    g_hsver_text[vlen] = 0;

    /* 解析 Vx.y.z / vx.y.z / x.y.z：前后空白、v/V 大小写都收（同服务端） */
    p = g_hsver_text;
    while (*p == ' ' || *p == '\t') p++;
    if (*p == 'v' || *p == 'V') p++;
    for (i = 0; i < 3; i++) {
        unsigned v = 0;
        digits = 0;
        while (*p >= '0' && *p <= '9') {
            v = v * 10u + (unsigned)(*p - '0');
            p++;
            if (++digits > 4) return 0;
        }
        if (!digits) return 0;
        seg[i] = v;
        if (*p == '.') p++;
        else break;
    }
    while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') p++;
    if (*p) return 0;                      /* 值后面还挂着别的东西 = 认不出 */
    if (seg[0] > 2146u || seg[1] > 999u || seg[2] > 999u) return 0;
    wire = seg[0] * 1000000u + seg[1] * 1000u + seg[2];
    if (wire < 1000u || wire == 311u) return 0;   /* 撞原版保留值/小数字区间 */
    g_hsver_wire = (long)wire;
    return 1;
}

static int try_patch_handshake_version(void)
{
    unsigned char *p = (unsigned char *)HS_VER_VA;
    unsigned char want[7];
    DWORD oldp;

    if (g_hsver_patched) return 1;
    if (g_hsver_wire < 1000) return 1;     /* 没版本号可写：保持 311 */
    if (IsBadReadPtr(p, 7)) return 0;
    memcpy(want, HS_VER_ORIG, 7);
    want[3] = (unsigned char)(g_hsver_wire         & 0xff);
    want[4] = (unsigned char)((g_hsver_wire >> 8)  & 0xff);
    want[5] = (unsigned char)((g_hsver_wire >> 16) & 0xff);
    want[6] = (unsigned char)((g_hsver_wire >> 24) & 0xff);
    if (memcmp(p, want, 7) == 0) {         /* 已是补丁后的样子 */
        InterlockedExchange(&g_hsver_patched, 1);
        return 1;
    }
    if (memcmp(p, HS_VER_ORIG, 7) != 0) return 0;   /* 还没解壳到这里，继续等 */

    if (!VirtualProtect(p, 7, PAGE_EXECUTE_READWRITE, &oldp)) {
        bslog("PATCH   握手版本号: VirtualProtect 失败 err=%lu",
              (unsigned long)GetLastError());
        return 0;
    }
    memcpy(p, want, 7);
    VirtualProtect(p, 7, oldp, &oldp);
    FlushInstructionCache(GetCurrentProcess(), p, 7);
    InterlockedExchange(&g_hsver_patched, 1);
    bslog("PATCH   ★握手版本号 @ %08X: 311 -> %ld（BUILD.ver %s）",
          (unsigned)HS_VER_VA, g_hsver_wire, g_hsver_text);
    return 1;
}

/* -------------------------------------------------------------------------- */
/* 道具视觉同步 patch —— 让远端角色也创建反射盾牌特效                         */
/*                                                                            */
/*   服务端的 0x040a 已经把 10303「反射」和 10314「全队反射」广播给房内所有人；*/
/*   远端客户端也确实把 attr=3 加进角色属性表，所以子弹碰撞会正常反射。        */
/*                                                                            */
/*   问题在 Character::AddAttrVisual(0x508e88) 的 attr=3 分支：               */
/*     0x5090e2  call GetMySeat                                               */
/*     0x5090e7  cmp  [esi+0x2ac], eax       ; 特效目标座位 vs 本机座位       */
/*     0x5090ed  jne  0x5097cd              ; 远端目标直接返回                */
/*     0x5090f3  push "Item/Reflect/Efx/ReflectMark00.efx"                    */
/*                                                                            */
/*   把这条 6 字节 near JNE 换成 NOP，保留前面的座位查询/比较，仅取消返回。    */
/*   普通反射与全队反射最终都走 attr=3，因此一处修复同时覆盖两种道具。        */
/*   完整结论及同类道具审计见 FINDINGS §206、DECISIONS D123。                 */
/* -------------------------------------------------------------------------- */
#define REFLECT_VISUAL_SIG_VA     0x005090e2u
#define REFLECT_VISUAL_PATCH_OFF  11
#define REFLECT_VISUAL_PATCH_LEN  6
static const unsigned char REFLECT_VISUAL_SIG[22] = {
    0xE8,0x96,0x0E,0xF0,0xFF,             /* call GetMySeat */
    0x39,0x86,0xAC,0x02,0x00,0x00,        /* cmp [esi+0x2ac],eax */
    0x0F,0x85,0xDA,0x06,0x00,0x00,        /* jne 0x5097cd */
    0x68,0x68,0x4B,0x68,0x00              /* push ReflectMark00.efx */
};
static const unsigned char REFLECT_VISUAL_PATCH[REFLECT_VISUAL_PATCH_LEN] = {
    0x90,0x90,0x90,0x90,0x90,0x90
};
static volatile LONG g_reflect_visual_patched = 0;

static int try_patch_reflect_visual(void)
{
    unsigned char *base = (unsigned char *)REFLECT_VISUAL_SIG_VA;
    unsigned char *p = base + REFLECT_VISUAL_PATCH_OFF;
    DWORD oldp;

    if (g_reflect_visual_patched) return 1;
    if (IsBadReadPtr(base, sizeof(REFLECT_VISUAL_SIG))) return 0;
    if (memcmp(p, REFLECT_VISUAL_PATCH, REFLECT_VISUAL_PATCH_LEN) == 0) {
        InterlockedExchange(&g_reflect_visual_patched, 1);
        return 1;
    }
    if (memcmp(base, REFLECT_VISUAL_SIG, sizeof(REFLECT_VISUAL_SIG)) != 0)
        return 0;                               /* 还没解壳，或并非已确认的客户端版本 */

    if (!VirtualProtect(p, REFLECT_VISUAL_PATCH_LEN, PAGE_EXECUTE_READWRITE, &oldp)) {
        bslog("PATCH   反射道具视觉: VirtualProtect 失败 err=%lu",
              (unsigned long)GetLastError());
        return 0;
    }
    memcpy(p, REFLECT_VISUAL_PATCH, REFLECT_VISUAL_PATCH_LEN);
    VirtualProtect(p, REFLECT_VISUAL_PATCH_LEN, oldp, &oldp);
    FlushInstructionCache(GetCurrentProcess(), p, REFLECT_VISUAL_PATCH_LEN);
    InterlockedExchange(&g_reflect_visual_patched, 1);
    bslog("PATCH   ★反射道具视觉 @ %08X: 远端角色也创建 ReflectMark00.efx"
          "（普通反射 10303 + 全队反射 10314）",
          (unsigned)(REFLECT_VISUAL_SIG_VA + REFLECT_VISUAL_PATCH_OFF));
    return 1;
}

/* -------------------------------------------------------------------------- */
/* 地图等级门槛 patch —— 选图 / 开局都不再看 MinLevel（§221 / D142）           */
/*                                                                            */
/*   等级门槛有两道，不在同一处：                                              */
/*                                                                            */
/*   【第一道：列表过滤】0x40b5d0 是全客户端唯一的「按模式/等级/人数上限过滤    */
/*    地图列表」函数，调用方覆盖随机挑图（0x40b6e5，进房自动挑 + randomMapBtn）、*/
/*    房间设定「地图」下拉框（0x46534e）、主题切换重填（0x464017）、建房候选    */
/*    （0x463c4d）和天梯选图。它内部的等级检查：                                */
/*                                                                            */
/*     0x40b623  mov eax,[ebx+0x28]      ; 记录的 MinLevel                     */
/*     0x40b626  cmp [ebp+0x10],eax      ; 参数里的玩家等级                    */
/*     0x40b629  jl  0x40b65a            ; ★ 等级不够 -> 跳过这张图            */
/*     0x40b62B  mov eax,[ebx+0x34]      ; MaxUser（人数上限）                 */
/*     0x40b62E  cmp [ebp+0x1c],eax                                          */
/*     0x40b631  jg  0x40b65a            ; 人数超上限 -> 跳过（保留）          */
/*                                                                            */
/*   把 0x40b629 的 jl NOP 掉：目录里全部地图对任何等级可见、可随机中出。       */
/*                                                                            */
/*   【第二道：开局校验】0x468176（返回 1 = 拦下并给一句话）在**开始游戏**时    */
/*    拿「当前房间地图的 MinLevel」（0x464848）和「房主等级」（座位玩家       */
/*    +0x10 的 u16）直接比，不经过 0x40b5d0 —— 只 patch 第一道时表现为：       */
/*    下拉框里选得到，点「游戏开始」却弹「等级太低，无法选择地图。」           */
/*    （Chinese.ini 键 레벨이 낮아서 맵을 선택할 수 없습니다，全客户端只有     */
/*    0x4682b4 一处弹它；闯关的孪生消息 레벨이 낮아서 퀘스트를 … 在 0x4682d6）：*/
/*                                                                            */
/*     0x468277  movzx ecx,word [edi+0x10] ; 房主等级                          */
/*     0x46827B  cmp  ecx,eax              ; vs 当前地图 MinLevel              */
/*     0x46827D  jge  0x468316             ; ★ 够 -> 放行去人数上限检查        */
/*     0x468283  ……弹「等级太低」并返回 1                                     */
/*                                                                            */
/*   把 jge（0F 8D 93 00 00 00）换成无条件 jmp（E9 94 00 00 00 90）：          */
/*    永远走「放行」，紧随其后的人数上限检查（0x468316 起）原样保留。          */
/*                                                                            */
/*   两道的人数上限检查都**保留** —— 房间人数超过地图 MaxUser 时地图本来就     */
/*   装不下（沙漠01/入口类是 4 人、카멜궁是 6 人），那是容量不是门槛。          */
/*   用户参照「对战模式等级限制默认解除」（§203 / D120）拍板一并解除（D142）。  */
/*                                                                            */
/*   范围外：闯关建房「任务」下拉框另有自己的一道等级检查（0x4368ca）——         */
/*   那边关卡记录的 MinLevel 在 map.ini 里全被注释掉（默认 1），本来就不拦。    */
/*                                                                            */
/*   设环境变量 BSHOOK_KEEP_MAP_LEVEL_LOCK=1 保留原版地图等级门槛。            */
/* -------------------------------------------------------------------------- */
#define MAP_LVL_SITE_COUNT 2
static const struct {
    unsigned int va;
    unsigned int len;
    unsigned int off;
    unsigned int n;
    const unsigned char *sig;
    const unsigned char *fix;
    const char *what;
} MAP_LVL_SITES[MAP_LVL_SITE_COUNT] = {
    { 0x0040b623u, 16, 6, 2,
      (const unsigned char *)"\x8B\x43\x28\x39\x45\x10\x7C\x2F\x8B\x43\x34\x39\x45\x1C\x7F\x27",
      (const unsigned char *)"\x90\x90",          /* NOP 掉 jl（等级不够不再跳过） */
      "地图列表过滤 0x40b5d0（MinLevel 判定旁路）" },
    { 0x00468277u, 12, 6, 6,
      (const unsigned char *)"\x0F\xB7\x4F\x10\x3B\xC8\x0F\x8D\x93\x00\x00\x00",
      (const unsigned char *)"\xE9\x94\x00\x00\x00\x90",  /* jge -> jmp（永远放行） */
      "开局校验 0x468176（等级不够也放行，人数上限检查保留）" },
};
static volatile LONG g_map_lvl_patched = 0;

static int map_level_lock_kept(void)
{
    char buf[8];
    DWORD n = GetEnvironmentVariableA("BSHOOK_KEEP_MAP_LEVEL_LOCK", buf, sizeof(buf));
    return n > 0 && n < sizeof(buf) && buf[0] != '0';
}

static int try_patch_map_level_gate(void)
{
    int i, done = 0;

    if (g_map_lvl_patched) return 1;
    for (i = 0; i < MAP_LVL_SITE_COUNT; i++) {
        unsigned char *base = (unsigned char *)MAP_LVL_SITES[i].va;
        unsigned char *p = base + MAP_LVL_SITES[i].off;
        DWORD oldp;

        if (IsBadReadPtr(base, MAP_LVL_SITES[i].len)) continue;
        if (memcmp(p, MAP_LVL_SITES[i].fix, MAP_LVL_SITES[i].n) == 0) { done++; continue; }
        if (memcmp(base, MAP_LVL_SITES[i].sig, MAP_LVL_SITES[i].len) != 0)
            continue;                            /* 还没解壳到这里，继续等 */

        if (!VirtualProtect(p, MAP_LVL_SITES[i].n, PAGE_EXECUTE_READWRITE, &oldp)) {
            bslog("PATCH   地图等级门槛(%s): VirtualProtect 失败 err=%lu",
                  MAP_LVL_SITES[i].what, (unsigned long)GetLastError());
            continue;
        }
        memcpy(p, MAP_LVL_SITES[i].fix, MAP_LVL_SITES[i].n);
        VirtualProtect(p, MAP_LVL_SITES[i].n, oldp, &oldp);
        FlushInstructionCache(GetCurrentProcess(), p, MAP_LVL_SITES[i].n);
        bslog("PATCH   ★地图等级门槛 @ %08X: %s",
              (unsigned)(MAP_LVL_SITES[i].va + MAP_LVL_SITES[i].off),
              MAP_LVL_SITES[i].what);
        done++;
    }
    if (done == MAP_LVL_SITE_COUNT) {
        InterlockedExchange(&g_map_lvl_patched, 1);
        return 1;
    }
    return 0;
}

/* -------------------------------------------------------------------------- */
/* 联机闪退修复 —— 中文输入法（IME）候选窗在 stage 切换后踩已释放的聊天输入框  */
/*                                                                            */
/*   实测（bug调查/3，12 份 mdmp 全部同一现场）：收到 0x0402「全员加载完成，   */
/*   一起进 stage 7」后 ~1 秒内 C0000005 @ 0x42516A，坏指针 = 0xFFFFFF00       */
/*   （9 份）或已复用的随机堆垃圾（3 份）。逐层还原（脱壳镜像 + dump 现场）：  */
/*                                                                            */
/*     · 0x42515E SumRect(head=edx, out=eax)：沿 +0x28 父指针链累加每层的     */
/*       +0x10/+0x14（宽/高），子控件坐标 -> 屏幕坐标；                        */
/*     · UiImeCandidates（输入法候选窗）的布局方法 0x430102（vtable+0xC）在   */
/*       候选窗可见时（正在打拼音）读 [UI根+0x10] 当 head —— 那一格是          */
/*       「当前活动编辑框」：聊天输入 UiEdit 激活（0x42F0DE）时登记，          */
/*       正常关闭（0x42F12B）时清空；                                           */
/*     · 0x0402 切 stage 7 拆 UI 时把聊天输入框**直接销毁**。控件析构里的      */
/*       根缓存清理 0x4269AB 清了 +0xC/+0x18/+0x14/+0x1C 四个缓存，           */
/*       ★ 唯独漏了 +0x10 —— [UI根+0x10] 从此指着已释放内存；                  */
/*     · 下一帧候选窗布局沿坏指针读 [0xFFFFFF00+0x10] -> 崩。                  */
/*   只有 IME 候选窗还亮着（打字中/刚打完）的客户端中招，所以同房有人崩有人   */
/*   不崩；也所以 8-14（旧服）与 8-15（GPT 改版服）两代服务器崩得一模一样     */
/*   —— 纯客户端 UI bug，和服务端时序无关。                                    */
/*                                                                            */
/*   两处配套 patch：                                                          */
/*   A) 0x4269AB 头 5 字节（push esi; mov esi,ecx; xor ecx,ecx —— 正好 5 字节）*/
/*      换成 E9 跳 detour：补上「将亡控件 == [根+0x10] 时把它清空」，其余      */
/*      原样（跳回 0x4269B0 继续原有的四个缓存清理）。修的正是原版漏掉的      */
/*      那一次缓存失效。                                                       */
/*   B) 0x42515E 头部 inline hook：head 为空/野值（<0x10000）时输出全零矩形    */
/*      返回。★ 必须配 A：A 生效后 head=0 成为合法状态，而原版对 head=0       */
/*      会走 je 0x425177 去读 [edx+0x1C] —— 0x425177 那两条根本不判空。       */
/*                                                                            */
/*   设 BSHOOK_KEEP_IME_CRASH=1 可保留原版行为（闪退复现/对照用）。           */
/* -------------------------------------------------------------------------- */
#define UI_ROOT_CACHE_CLEAR_VA  0x004269ABu  /* 控件析构时的根缓存清理        */
#define UI_ROOT_CACHE_SIG_LEN   8
static const unsigned char UI_ROOT_CACHE_SIG[UI_ROOT_CACHE_SIG_LEN] = {
    0x56, 0x8B, 0xF1,                   /* push esi; mov esi, ecx            */
    0x33, 0xC9,                         /* xor ecx, ecx                      */
    0x39, 0x46, 0x0C                    /* cmp [esi+0xC], eax                */
};

#define SUM_RECT_VA             0x0042515Eu  /* SumRect：子->屏幕坐标换算    */
#define SUM_RECT_SIG_LEN        8
static const unsigned char SUM_RECT_SIG[SUM_RECT_SIG_LEN] = {
    0x56, 0x57,                         /* push esi; push edi                */
    0x33, 0xF6, 0x33, 0xFF,             /* xor esi, esi; xor edi, edi        */
    0x85, 0xD2                          /* test edx, edx                     */
};

/* detour 里要用的立即数不带后缀（MSVC 内联汇编不吃 0x…u 这种写法） */
#define UI_ROOT_CACHE_RETURN_TO 0x004269B0

static __declspec(naked) void ui_root_cache_clear_detour(void)
{
    __asm {
        push esi                            /* 被偷走的原指令，逐条补回 */
        mov  esi, ecx
        xor  ecx, ecx
        cmp  dword ptr [esi + 0x10], eax    /* ★原版漏掉的：活动编辑框缓存 */
        jne  uicc_keep
        mov  dword ptr [esi + 0x10], ecx    /* 将亡控件正是它 -> 清空 */
    uicc_keep:
        push UI_ROOT_CACHE_RETURN_TO        /* 回原函数继续清 +0xC/+0x18/+0x14/+0x1C */
        ret
    }
}

static void *g_sum_rect_trampoline = NULL;

static __declspec(naked) void sum_rect_guard_detour(void)
{
    __asm {
        cmp  edx, 0x10000                   /* head 为空/野值：别去碰 */
        jae  srg_ok
        mov  dword ptr [eax], 0             /* 输出全零矩形（原版会去读 [edx+0x1C] 崩） */
        mov  dword ptr [eax + 4], 0
        mov  dword ptr [eax + 8], 0
        mov  dword ptr [eax + 0xC], 0
        ret
    srg_ok:
        jmp  dword ptr [g_sum_rect_trampoline]
    }
}

static volatile LONG g_ime_cache_patched  = 0;
static volatile LONG g_ime_sumrect_patched = 0;

static int ime_crash_fix_keep_original(void)
{
    /* BSHOOK_KEEP_IME_CRASH=1 → 保留原版（闪退复现/对照用）。
       注意别照抄 afk/region 那两个 *_disabled() 的写法：它们的返回值
       语义是反的（未设置返回 TRUE），抄了必翻车 —— 冒烟测试抓到过。 */
    char buf[8];
    DWORD n = GetEnvironmentVariableA("BSHOOK_KEEP_IME_CRASH", buf, sizeof(buf));
    return n > 0 && n < sizeof(buf) && buf[0] != '0';
}

static int try_patch_ime_cache_clear(void)
{
    unsigned char *p = (unsigned char *)UI_ROOT_CACHE_CLEAR_VA;
    DWORD oldp;

    if (g_ime_cache_patched) return 1;
    if (IsBadReadPtr(p, UI_ROOT_CACHE_SIG_LEN)) return 0;
    {
        /* 幂等：已打过就是「E9 <跳到我们 detour 的 rel32>」 */
        if (p[0] == 0xE9
            && (DWORD)(*(int *)(p + 1))
                   == (DWORD)((UINT_PTR)&ui_root_cache_clear_detour
                              - (UINT_PTR)(p + 5))) {
            InterlockedExchange(&g_ime_cache_patched, 1);
            return 1;
        }
    }
    if (memcmp(p, UI_ROOT_CACHE_SIG, UI_ROOT_CACHE_SIG_LEN) != 0)
        return 0;                          /* 还没解壳到这里，或不是已确认的版本 */

    if (!VirtualProtect(p, 5, PAGE_EXECUTE_READWRITE, &oldp)) {
        bslog("PATCH   IME 缓存清理: VirtualProtect 失败 err=%lu",
              (unsigned long)GetLastError());
        return 0;
    }
    p[0] = 0xE9;
    *(DWORD *)(p + 1) = (DWORD)((UINT_PTR)&ui_root_cache_clear_detour
                                - (UINT_PTR)(p + 5));
    VirtualProtect(p, 5, oldp, &oldp);
    FlushInstructionCache(GetCurrentProcess(), p, 5);
    InterlockedExchange(&g_ime_cache_patched, 1);
    bslog("PATCH   ★IME 闪退修复1/2 @ %08X: 控件销毁时把 [UI根+0x10]"
          "（活动编辑框）一并清掉 —— 原版只清 +0xC/+0x18/+0x14/+0x1C",
          (unsigned)UI_ROOT_CACHE_CLEAR_VA);
    return 1;
}

static int try_patch_sum_rect_guard(void)
{
    unsigned char *p = (unsigned char *)SUM_RECT_VA;

    if (g_ime_sumrect_patched) return 1;
    if (IsBadReadPtr(p, SUM_RECT_SIG_LEN)) return 0;
    if (g_sum_rect_trampoline == NULL) {
        if (memcmp(p, SUM_RECT_SIG, SUM_RECT_SIG_LEN) != 0)
            return 0;                      /* 还没解壳到这里，或不是已确认的版本 */
        g_sum_rect_trampoline = install_inline_hook((void *)SUM_RECT_VA,
                                                    sum_rect_guard_detour,
                                                    "SumRect 空头防护");
        if (!g_sum_rect_trampoline) {
            g_sum_rect_trampoline = NULL;
            return 0;
        }
    }
    InterlockedExchange(&g_ime_sumrect_patched, 1);
    bslog("PATCH   ★IME 闪退修复2/2 @ %08X: 坐标换算头指针为空/野值时输出"
          "全零矩形（修复1/2 生效后 head=0 合法，原版这里会读 [0+0x1C]）",
          (unsigned)SUM_RECT_VA);
    return 1;
}

/* -------------------------------------------------------------------------- */
/* IME 闪退修复 3/3 —— 候选窗布局 0x430102 里 SumRect 之后还有一处裸解引用    */
/*                                                                            */
/*   bug调查/5：6 份 mdmp 全部 C0000005 @ 0x4301BD，读 0x110。0x430102 是      */
/*   UiImeCandidates 的布局方法（修复 1/2 注释里的同一函数）：                 */
/*                                                                            */
/*     0x43019C  mov eax,[0x72e2b4]        ; UI 根                            */
/*     0x4301A1  mov esi,[eax+0x10]        ; 活动编辑框（修复1/2 后可为 0）    */
/*     0x4301A9  call SumRect              ; ← 修复 2/2 已护住，返回全零矩形   */
/*     0x4301AE  mov eax,[ebp-0x24]        ; SumRect 输出                     */
/*     0x4301B1  mov edx,[ebp-0x20]                                           */
/*     0x4301B4  add esi,0x110                                               */
/*     0x4301BA  lea edi,[ebp-0x14]                                          */
/*     0x4301BD  movsd ×5                  ; ★ 拷 [编辑框+0x110] 起 20 字节   */
/*               ……（后面还拿拷出的第 5 个 dword 当 this 连发方法调用）        */
/*                                                                            */
/*   修复 1/2 只护住了 SumRect：编辑框销毁后 [+0x10]=0，SumRect 平安返回，     */
/*   movsd 却照样去读 [0x110] —— 崩溃点从 0x42516A 挪到了 0x4301BD。          */
/*   零填充拷贝不够：第 5 个 dword 会被当 this 用，必须整段跳过。              */
/*   跳到 0x4301FF（函数自己的「候选窗不可见」早退尾部，esi 需先还原成 this，  */
/*   [ebp-4] 此刻存的就是它，0x4301C8 的 mov esi,[ebp-4] 可证）—— 候选窗保持   */
/*   原位，等下一帧再布局。                                                    */
/*                                                                            */
/*   ★ bug调查/6：本补丁此前的三个地址整体错位了 1 个字节（特征码锚在          */
/*     0x4301B3 / 恢复点 0x4301BC / 早退 0x4301FE），而 0x4301B3 实际是           */
/*     mov edx,[ebp-0x20] 的最后一个字节 —— 特征串在任何机器上都永远对不上        */
/*     （玩家与开发机的日志同样是「超时未能 patch」），补丁从未生效，             */
/*     玩家端两份新 mdmp 仍崩在 0x4301BD 读 0x110。已按 dump 实测字节校正。      */
/*                                                                            */
/*   ★ bug调查/7：6 的校正把早退地址也跟着 +1 到 0x4301FF —— 错了。0x4301FE    */
/*     才是 xor ebx,ebx 的指令边界（函数自己的两处早退 0x430120 / 0x430132      */
/*     跳的正是它），0x4301FF 落在指令中间。实测后果：打字玩家的聊天框随        */
/*     0x0402 拆 UI 被清（修复1/2 生效），detour 走早退支路跳到 0x4301FF，      */
/*     CPU 从那里解码出 6 字节 FPU 指令 db 8b c6 e8 e7 4e（fisttp              */
/*     [ebx+0x4EE7E8C6]，凑巧可读不fault），下一条正落在 0x430205 的 ff ff      */
/*     上 —— C000001D 非法指令，三份 mdmp 同一现场。改回 0x4301FE。             */
/* -------------------------------------------------------------------------- */
#define IME_CAND_LAYOUT_COPY_VA 0x004301B4u /* add esi,0x110; lea edi,[ebp-0x14] */
#define IME_CAND_SIG_LEN        11
static const unsigned char IME_CAND_SIG[IME_CAND_SIG_LEN] = {
    0x81, 0xC6, 0x10, 0x01, 0x00, 0x00,   /* add esi, 0x110                    */
    0x8D, 0x7D, 0xEC,                     /* lea edi, [ebp-0x14]               */
    0xA5, 0xA5,                           /* movsd; movsd                      */
};

#define IME_CAND_COPY_RESUME    0x004301BD  /* 回到 5 个 movsd                  */
#define IME_CAND_EARLY_OUT      0x004301FE  /* 函数自己的「不可见」早退尾部      */

static __declspec(naked) void ime_cand_layout_guard_detour(void)
{
    __asm {
        cmp  esi, 0x10000                   /* 活动编辑框为空/野值：别去拷 */
        jae  iclg_have_edit
        mov  esi, [ebp - 4]                 /* 还原 this（UiImeCandidates） */
        push IME_CAND_EARLY_OUT             /* 走函数自己的早退尾部 */
        ret
    iclg_have_edit:
        add  esi, 0x110                     /* 被偷走的原指令，逐条补回 */
        lea  edi, [ebp - 0x14]
        push IME_CAND_COPY_RESUME
        ret
    }
}

static volatile LONG g_ime_cand_patched = 0;

static int try_patch_ime_cand_layout_guard(void)
{
    unsigned char *p = (unsigned char *)IME_CAND_LAYOUT_COPY_VA;
    DWORD oldp;

    if (g_ime_cand_patched) return 1;
    if (IsBadReadPtr(p, IME_CAND_SIG_LEN)) return 0;
    {
        /* 幂等：已打过就是「E9 <跳到我们 detour 的 rel32>」+ 4 个 NOP */
        if (p[0] == 0xE9
            && (DWORD)(*(int *)(p + 1))
                   == (DWORD)((UINT_PTR)&ime_cand_layout_guard_detour
                              - (UINT_PTR)(p + 5))) {
            InterlockedExchange(&g_ime_cand_patched, 1);
            return 1;
        }
    }
    if (memcmp(p, IME_CAND_SIG, IME_CAND_SIG_LEN) != 0)
        return 0;                          /* 还没解壳到这里，或不是已确认的版本 */

    /* 覆盖 add esi,0x110(6B)+lea edi(3B) 共 9 字节：E9 rel32 + 4×NOP */
    if (!VirtualProtect(p, 9, PAGE_EXECUTE_READWRITE, &oldp)) {
        bslog("PATCH   IME 候选窗布局: VirtualProtect 失败 err=%lu",
              (unsigned long)GetLastError());
        return 0;
    }
    p[0] = 0xE9;
    *(DWORD *)(p + 1) = (DWORD)((UINT_PTR)&ime_cand_layout_guard_detour
                                - (UINT_PTR)(p + 5));
    p[5] = 0x90; p[6] = 0x90; p[7] = 0x90; p[8] = 0x90;
    VirtualProtect(p, 9, oldp, &oldp);
    FlushInstructionCache(GetCurrentProcess(), p, 9);
    InterlockedExchange(&g_ime_cand_patched, 1);
    bslog("PATCH   ★IME 闪退修复3/3 @ %08X: 候选窗布局时活动编辑框为空"
          "则整段跳过定位（修复1/2 生效后 [编辑框]=0 合法，原版这里"
          "会拷 [0+0x110] 崩 —— bug调查/5 的 6 连崩点）",
          (unsigned)IME_CAND_LAYOUT_COPY_VA);
    return 1;
}

/* -------------------------------------------------------------------------- */
/* 单机化 patch —— 解锁被「地区掩码」关掉的关卡（神秘岛以外的第 5/6/7 关）    */
/*                                                                            */
/*   Data/map.ini 里每张地图都有一行 OpenLocale（注释写着                     */
/*   `1 - 한국, 2 - 일본, 4 - 중국`，按位或）。中国版跑起来时全局             */
/*   `[[0x72e320]]` = 2，客户端的掩码测试都拿 `1 << 2 = 4` 去和它 test：      */
/*                                                                            */
/*     0x40b419  地图目录加载（`0x40b2a1`，启动时读 map.ini）                 */
/*               掩码不匹配 -> 0x40b47a 把记录直接 delete 掉                  */
/*     0x4368cf  「建立房间(任务)」对话框填「任务」下拉框（`0x4365e1`）        */
/*               掩码不匹配 -> 跳过这一条                                     */
/*     0x4653a8  对战房间「设定」的「地图」下拉框填充（0x4652b3 里的循环）     */
/*               掩码不匹配 -> 跳过这一条（第五处 patch，见下面）              */
/*                                                                            */
/*   而 map.ini 里：                                                          */
/*     QuestId 1 불프로그 / 2 드라카 / 3 비밀의 섬 / 4 자미로건쉽  OpenLocale=7 */
/*     QuestId 5 다크나이트 / 6 브레그마 / 7 자미로 비밀 연구소     OpenLocale=3 */
/*     QuestId 8 푸른 하늘                                        OpenLocale=0 */
/*   —— **4 个关卡不是资源缺失，是中国版当年没上线**（地图文件全都在）。      */
/*                                                                            */
/*   改法：第二处（任务下拉框）把「地区序号」当成 0（韩国）来算，             */
/*   `mov ecx,[...]` -> `xor ecx,ecx`，掩码里带 bit0 的都放行（7 / 3 / 1）。   */
/*   第一处（目录加载）会话 39 起升级为**把掩码判定整个旁路**（NOP 掉 je，     */
/*   见下面「全部解锁」那段）—— 掩码为 0 的条目也保留；缺文件的 Quest08 /     */
/*   Festivalm01 靠「任何列表都选不到」兜底（任务表没有 id 8、무투전模式       */
/*   在中国区建房下拉里被隐藏）。                                             */
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
/*   ── 第五处：对战房间「设定」里「地图」下拉框的地域掩码 ──                  */
/*                                                                            */
/*   前两处只管**地图目录**和「建房(任务)」的任务下拉框。对战房间是另一条路：    */
/*                                                                            */
/*     · 建房对话框（CreateRoomNewUI.ui）**根本没有地图控件** —— 对战房的       */
/*       0x0201 建房请求里地图名恒为空串，地图是进房之后客户端自己挑的；         */
/*     · 挑图走 0x468176 / 0x469c17（randomMapBtn「랜덤」按钮 / 进房自动挑）    */
/*       -> 0x40b6e5：先 0x40b5d0 按模式/等级/人数上限过滤内存目录，            */
/*       再拿随机数取一张 —— **这条链不看 OpenLocale**；                        */
/*     · 而房间设定里「地图」下拉框（SelectPvpMap.ui 的 mapCB，                 */
/*       [dlg+0x594]）的填充（0x4652b3 虚函数里 0x46534e 的循环）**单独再做     */
/*       一次** `1 << 地区序号` 的掩码测试（0x4653a8），用的还是真实地区 2。     */
/*                                                                            */
/*   后果（用户实测报到）：第一处 patch 放进目录的 OpenLocale=1/3 地图          */
/*   （韩服活动图 Festival 系列 + Iceria/Desert/Garden 等，.map 和 BGM 都在     */
/*   包里、能正常玩）**偶尔会随机成为新房间的地图**，但在「地图」下拉列表里     */
/*   永远找不到 —— 因为只有下拉框那一处还在按中国区过滤。                      */
/*                                                                            */
/*   ── 会话 39 复测后用户拍板「全部解锁」：第一处和第五处升级为               */
/*   **把掩码判定整个旁路**（NOP 掉 je），不再只是「当成韩国区」：              */
/*                                                                            */
/*     · 第一处 0x40b42a `je 0x40b442`（74 16 -> 90 90）：不跳 = 走 0x40b42c   */
/*       的「插入目录」分支；跳 = 0x40b442 的「析构 + 释放记录」（0x40b47a）。 */
/*       NOP 之后 map.ini 里**没写 OpenLocale（掩码 0）的条目也保留** ——       */
/*       沙漠（Desert01/02/03）、카멜궁 1~3층（Camel00/01/02）、                */
/*       CamelCulvert02 这些「全世界都没开放」的图（文件都在包里）进目录；      */
/*       连带进来的还有缺文件的 Festivalm01（Mutu 限定）和 Quest08/Quest08_1   */
/*       （QuestId=8，不在建房任务表 0x6dc52c {3,2,1,4,5,6,7} 里）——           */
/*       两者在对战/闯关的任何列表里都选不到，只会安静地躺在目录里。            */
/*     · 第五处 0x4653be `je 0x4654b3`（0F 84 EF 00 00 00 -> 6×90）：不跳 =   */
/*       加进「地图」下拉框。只把地区序号当 0 还挡掩码 0 的图，所以同样旁路。  */
/*                                                                            */
/*   等级门槛（MinLevel）后来也按用户要求一并解除了 —— 见下面独立的            */
/*   「地图等级门槛 patch」（0x40b623，D142），不挂在本组、有单独的回退开关。   */
/* -------------------------------------------------------------------------- */
#define REGION_PATCH_COUNT 5
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
    { 0x0040b419u, 19, 17, 2,
      (const unsigned char *)"\xA1\x20\xE3\x72\x00\x8B\x08\x8B\x53\x48"
                             "\x33\xC0\x40\xD3\xE0\x85\xC2\x74\x16",
      (const unsigned char *)"\x90\x90",          /* NOP 掉 je：掩码不匹配也不删记录 */
      "地图目录加载（掩码判定整个旁路 —— 全部解锁）" },
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
    { 0x004653a8u, 28, 22, 6,
      (const unsigned char *)"\xA1\x20\xE3\x72\x00\x8B\x08\x8B\x33\x8B\x56"
                             "\x48\x33\xC0\x40\xD3\xE0\x83\xC3\x04\x85\xC2"
                             "\x0F\x84\xEF\x00\x00\x00",
      (const unsigned char *)"\x90\x90\x90\x90\x90\x90",  /* NOP 掉 je：掩码 0 也进列表 */
      "对战房间「地图」下拉框（掩码判定整个旁路 —— 全部解锁）" },
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

/* SNOW_LOG_MAX_CRYPT：逐包日志的条数上限。
 * ★ bug调查/11 的教训：8000 条只够 ~16 分钟战斗（8Hz×每次 2 行），那局
 *   打到 22:47 加密日志就停了，最后两局（正是出问题的两局）客户端侧一片
 *   空白。提到 80000（约 160 分钟战斗）；debug 模式的日志文件会到 ~110MB，
 *   排障时这比丢证据便宜。 */
#define SNOW_MAX_LOG_BYTES 256
#define SNOW_LOG_MAX_KEYS  4000
#define SNOW_LOG_MAX_CRYPT 80000

static int try_hook_snow(void);

/* -------------------------------------------------------------------------- */
/* 位置数据的 UDP 旁路 —— 客户端这一侧（bug调查/9）。                          */
/*                                                                            */
/* ## 它解决什么                                                              */
/*                                                                            */
/* 实测：客户端**发**得非常准（间隔 p50=128ms / p95=130ms），但同一批包到跨境 */
/* 服务器时变成 p95=432ms、33% 成串到达 —— 每秒一次「停 0.43 秒、3 发一起到」。*/
/* 发 5405 收 5405，一发不丢，所以不是丢包，是 TCP 重传时的**队头阻塞**。      */
/* 客户端不做插值也不回滚，于是这个抖动 100% 变成别人屏幕上的瞬移。            */
/*                                                                            */
/* ## 这里做的事（只有一件）                                                   */
/*                                                                            */
/* 在 `SimpleCipher::Encrypt` 的入口 —— 也就是**加密之前**，能看到明文帧的     */
/* 唯一位置 —— 认出「内层 0x4001 位置心跳」，额外从一条自己的 UDP socket      */
/* 发一份到本机中继（`server/relay.py`），由它转给服务器。                     */
/*                                                                            */
/* ★★ **原来那份 TCP 照发不误，一个字节都不改。** 这条 UDP 通道是「多走一份」，*/
/*    不是「改走 UDP」。所以它整条不通（防火墙、NAT、服务端是旧版、中继没起来）*/
/*    都**没有任何后果** —— 服务端按索引去重，UDP 没到就用 TCP 那份。          */
/*    这就是全部的「回退逻辑」：没有回退逻辑。                                  */
/*                                                                            */
/* ★ **只镜像位置**。开火/命中/伤害（内层 < 0x4000）走的是客户端的可靠队列，   */
/*   丢一发就整局错位（FINDINGS §217），它们任何时候都只走 TCP。               */
/*                                                                            */
/* ## 索引                                                                     */
/*                                                                            */
/* 每发心跳盖一个递增索引，服务端拿它和自己数的 TCP 发数对齐去重。            */
/* **两边的起点都是「这条游戏连接的登录包」** —— 我们在看到 `0x0100`          */
/* gcpReqLogin 时归零，服务端那边是一个新的 `Conn`，计数器同样从 0 起。        */
/* -------------------------------------------------------------------------- */
#define SYNC_MAGIC0 'P'
#define SYNC_MAGIC1 'S'
#define SYNC_MAGIC2 'U'
#define SYNC_VERSION 1
#define SYNC_MSG_HELLO 1
#define SYNC_MSG_DATA  3
/* HELLO 的标志位：「游戏这边收位置数据的 UDP 口已经 bind 成功，可以往这儿投」。
   和 `server/udpsync.py` 的 `HELLO_FLAG_DOWNLINK` 是同一位。 */
#define SYNC_FLAG_DOWNLINK 0x01
/* 帧头 10 字节（RawPacket）：+0 魔数 0xff，+8 u16 opcode。§156。 */
#define FRAME_HEADER 10
/* UdpPacket 头 12 字节，内层 opcode 在 +10。§151。 */
#define PEER_HEADER 12
#define PEER_OPCODE_AT 10
#define PEER_HEARTBEAT 0x4001
/* 游戏帧 `0x040e`；接上原版 rcp 中继时通道 A 改走 rcp 帧 opcode 3（§149）。 */
#define OP_PEER_DATA_UP 0x040e
#define OP_RCP_DATA_UP  0x0003
#define OP_REQ_LOGIN    0x0100

/* 和上面那几个 ws2 typedef 同一套规矩：不含 winsock 头，手写签名 + WINAPI。 */
typedef SOCKET_T (WINAPI *socket_t)(int, int, int);
typedef int (WINAPI *sendto_t)(SOCKET_T, const char *, int, int,
                               const struct sockaddr_min *, int);
typedef int (WINAPI *ioctlsocket_t)(SOCKET_T, long, unsigned long *);

static SOCKET_T g_sync_sock = (SOCKET_T)~(UINT_PTR)0;   /* INVALID_SOCKET */
static socket_t s_ws2_socket = NULL;
static sendto_t s_ws2_sendto = NULL;
static struct sockaddr_in_min g_sync_target;
static volatile LONG g_sync_index = 0;
static volatile LONG g_sync_ready = 0;
static volatile LONG g_sync_failed = 0;   /* 建不起来就永远放弃，别每帧重试 */
static volatile LONG g_sync_sent = 0;
/* 游戏那个收位置数据的 UDP 口 bind 成功了没有（`det_bind` 置 1）。
   ★ 这是下行**唯一**的准入依据，而且是权威的 —— 不是「我 bind 不上所以
     大概是游戏占着」那种间接推断。 */
static volatile LONG g_sync_udp_bound = 0;
static char g_sync_ticket[128];

/* 建 socket。**非阻塞**，而且失败就永远放弃 —— 这是游戏的网络线程，
   任何一点阻塞都会变成掉帧。 */
static int sync_open(void)
{
    HMODULE ws2;
    unsigned long nonblocking = 1;
    ioctlsocket_t ioctl_fn;

    if (InterlockedCompareExchange(&g_sync_ready, 0, 0)) return 1;
    /* ★ 失败过一次就再也不试：这条路是「锦上添花」，而 sync_open 会走到
       GetProcAddress + socket()，每帧重试一遍是实打实的掉帧。 */
    if (InterlockedCompareExchange(&g_sync_failed, 0, 0)) return 0;
    ws2 = GetModuleHandleA("ws2_32.dll");
    if (!ws2) { InterlockedExchange(&g_sync_failed, 1); return 0; }
    if (!s_ws2_socket) {
        s_ws2_socket = (socket_t)GetProcAddress(ws2, "socket");
        s_ws2_sendto = (sendto_t)GetProcAddress(ws2, "sendto");
    }
    ioctl_fn = (ioctlsocket_t)GetProcAddress(ws2, "ioctlsocket");
    if (!s_ws2_socket || !s_ws2_sendto) {
        InterlockedExchange(&g_sync_failed, 1);
        return 0;
    }
    /* AF_INET=2, SOCK_DGRAM=2, IPPROTO_UDP=17 */
    g_sync_sock = s_ws2_socket(2, 2, 17);
    if (g_sync_sock == (SOCKET_T)~(UINT_PTR)0) {
        InterlockedExchange(&g_sync_failed, 1);
        bslog("SYNC    !! 建不了 UDP socket，位置数据继续走 TCP（不影响游戏）");
        return 0;
    }
    /* FIONBIO = 0x8004667E。★ 非阻塞是硬要求：这是游戏的网络线程。 */
    if (ioctl_fn) ioctl_fn(g_sync_sock, (long)0x8004667E, &nonblocking);

    memset(&g_sync_target, 0, sizeof(g_sync_target));
    g_sync_target.sin_family = AF_INET_MIN;
    g_sync_target.sin_port[0] = (unsigned char)((g_relay_udp_sync_port >> 8) & 0xff);
    g_sync_target.sin_port[1] = (unsigned char)(g_relay_udp_sync_port & 0xff);
    g_sync_target.sin_addr[0] = 127;
    g_sync_target.sin_addr[1] = 0;
    g_sync_target.sin_addr[2] = 0;
    g_sync_target.sin_addr[3] = 1;
    InterlockedExchange(&g_sync_ready, 1);
    bslog("SYNC    位置数据 UDP 旁路已就绪 -> 127.0.0.1:%u"
          "（只镜像位置心跳；开火/伤害照旧走 TCP）", g_relay_udp_sync_port);
    return 1;
}

static void sync_send_raw(const unsigned char *data, int len)
{
    if (!InterlockedCompareExchange(&g_sync_ready, 0, 0)) return;
    /* 送不出去就算了。**绝不重试、绝不阻塞、绝不报错** —— TCP 那份在跑。 */
    s_ws2_sendto(g_sync_sock, (const char *)data, len, 0,
                 (const struct sockaddr_min *)&g_sync_target,
                 (int)sizeof(g_sync_target));
}

static int sync_put_header(unsigned char *buf, int kind, int count)
{
    buf[0] = SYNC_MAGIC0; buf[1] = SYNC_MAGIC1; buf[2] = SYNC_MAGIC2;
    buf[3] = SYNC_VERSION;
    buf[4] = (unsigned char)kind;
    buf[5] = (unsigned char)count;
    buf[6] = 0; buf[7] = 0;
    return 8;
}

/* 发一发 `HELLO`：票据 + 标志位。标志位现在只有一位 —— 「游戏那个收位置
   数据的 UDP 口已经 bind 成功」。中继把它原样转告服务端，服务端据此决定
   要不要给这个玩家发下行 UDP。 */
static void sync_send_hello(void)
{
    unsigned char buf[8 + 2 + 128 + 1];
    int n, chars = (int)strlen(g_sync_ticket);

    if (chars <= 0 || chars > 126) return;
    if (!sync_open()) return;
    n = sync_put_header(buf, SYNC_MSG_HELLO, 0);
    buf[n++] = (unsigned char)(chars & 0xff);
    buf[n++] = (unsigned char)((chars >> 8) & 0xff);
    memcpy(buf + n, g_sync_ticket, (size_t)chars);
    n += chars;
    buf[n++] = (unsigned char)(
        InterlockedCompareExchange(&g_sync_udp_bound, 0, 0) ? SYNC_FLAG_DOWNLINK : 0);
    sync_send_raw(buf, n);
}

/* 从 `0x0100 gcpReqLogin` 的载荷里取票据（首字段 wstring：u16 字符数 +
   UTF-16LE，V0.1 §44），顺便把索引归零并发一发 HELLO。

   ★ 索引归零点必须和服务端一致：那边是「新建一条 `Conn`」，这边是
     「发出一发登录包」—— 一条游戏连接正好一发。 */
static void sync_on_login(const unsigned char *payload, int len)
{
    int chars, i;

    if (len < 2) return;
    chars = payload[0] | (payload[1] << 8);
    if (chars <= 0 || chars > 126 || len < 2 + chars * 2) return;
    for (i = 0; i < chars; i++) {
        unsigned short wc = (unsigned short)(payload[2 + i * 2] |
                                             (payload[3 + i * 2] << 8));
        if (wc == 0 || wc > 0x7f) return;      /* 票据是 32 个十六进制字符 */
        g_sync_ticket[i] = (char)wc;
    }
    g_sync_ticket[chars] = 0;
    InterlockedExchange(&g_sync_index, 0);
    InterlockedExchange(&g_sync_sent, 0);
    /* ★ 「那个 UDP 口已经绑好了」也要跟着清：这一发登录包意味着又要新建一个
       `GameSession`，它会重新 bind 一次（§153/§154）。不清的话，在新的 bind
       真的成功之前我们就告诉服务端「可以投了」—— 那段窗口里的位置数据白扔。
       重连时客户端会**原样重放同一张票据**（§171），所以不能靠票据变没变来判。 */
    InterlockedExchange(&g_sync_udp_bound, 0);
    sync_send_hello();
    bslog("SYNC    登录包已发出，位置数据 UDP 旁路重新开始计数（票据 %.8s…）",
          g_sync_ticket);
}

/* 游戏成功 bind 了收位置数据的那个 UDP 口 —— 告诉本机中继可以往这儿投了。
   （前向声明在上面 ws2 那一段；`det_bind` 调它。） */
static void sync_on_udp_bound(void)
{
    if (InterlockedExchange(&g_sync_udp_bound, 1)) return;   /* 只报一次 */
    if (!g_sync_ticket[0]) return;      /* 还没登录：下一发 HELLO 自会带上 */
    sync_send_hello();
    bslog("SYNC    ★ 下行已就绪：位置数据可以直接投进游戏的 UDP %u",
          g_client_udp_port);
}

/* 一整个 `UdpPacket` -> 一个数据报。冗余捎带交给 `relay.py` 做，
   这里保持最笨：一份就是一份。 */
static void sync_mirror_peer(const unsigned char *udp, int len)
{
    unsigned char buf[8 + 6 + 512];
    LONG index;
    int n;

    if (len < PEER_HEADER || len > 512) return;
    if (!sync_open()) return;
    index = InterlockedIncrement(&g_sync_index) - 1;
    n = sync_put_header(buf, SYNC_MSG_DATA, 1);
    buf[n++] = (unsigned char)(index & 0xff);
    buf[n++] = (unsigned char)((index >> 8) & 0xff);
    buf[n++] = (unsigned char)((index >> 16) & 0xff);
    buf[n++] = (unsigned char)((index >> 24) & 0xff);
    buf[n++] = (unsigned char)(len & 0xff);
    buf[n++] = (unsigned char)((len >> 8) & 0xff);
    memcpy(buf + n, udp, (size_t)len);
    n += len;
    sync_send_raw(buf, n);
    if (InterlockedIncrement(&g_sync_sent) == 1)
        bslog("SYNC    第一发位置数据已镜像到 UDP 旁路（索引 %ld，%d 字节）",
              (long)index, len);
}

/* `SimpleCipher::Encrypt` 的入口钩子会把每一帧**明文**喂到这里。

   ⚠ 这是游戏的网络线程，本函数必须便宜到可以忽略：正常情况下只读两个 u16
     比一下就返回。 */
static void sync_on_plain_frame(const unsigned char *frame, int len)
{
    unsigned opcode, inner;
    const unsigned char *udp;
    int udplen;

    /* 只在「远程服务器」模式下做。本机 / 局域网走的是环回或局域网，
       没有跨境那种丢包，多发一份纯属浪费。 */
    if (!popshot_online_mode()) return;
    if (len < FRAME_HEADER + 2 || frame[0] != 0xff) return;
    opcode = (unsigned)(frame[8] | (frame[9] << 8));
    if (opcode == OP_REQ_LOGIN) {
        sync_on_login(frame + FRAME_HEADER, len - FRAME_HEADER);
        return;
    }
    if (opcode != OP_PEER_DATA_UP && opcode != OP_RCP_DATA_UP) return;
    udp = frame + FRAME_HEADER;
    udplen = len - FRAME_HEADER;
    if (udplen < PEER_HEADER || udp[0] != 0xff) return;
    inner = (unsigned)(udp[PEER_OPCODE_AT] | (udp[PEER_OPCODE_AT + 1] << 8));
    /* ★ 铁律：只有位置心跳能走 UDP。其余（开火/命中/伤害/讨重传）一律不碰。 */
    if (inner != PEER_HEARTBEAT) return;
    sync_mirror_peer(udp, udplen);
}

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
    /* ★ 位置数据的 UDP 旁路（见本文件「位置数据的 UDP 旁路」一段）。
       `kind == 2` 是 `SimpleCipher::Encrypt` —— **加密之前**，也是整个进程里
       唯一能看到出站明文帧的地方。

       ⚠ 它和日志无关：`BSHOOK_VERBOSE_LOG` 关着的时候这一句照样要走，
         所以 `SimpleCipher::Encrypt` 这个钩子现在是**无条件安装**的
         （另外那四个 cipher 钩子仍然只在详细日志模式下装，它们才是 §105
         里「登录后等 100 秒」的元凶）。
       ⚠ 正常路径的开销 = 读两个 u16 比一下就返回。 */
    if (kind == 2 && len > 0 && !IsBadReadPtr(src, (UINT_PTR)len))
        sync_on_plain_frame((const unsigned char *)src, len);

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

/* `SimpleCipher::Encrypt` 的钩子 —— **幂等**，两条路都可能来装它：

   * 精简模式：`patch_thread` 为了位置数据的 UDP 旁路装它（只装这一个）；
   * 详细模式：`try_hook_snow` 连同另外四个 cipher 钩子一起装。

   ★ 它和另外四个的区别是**代价**：那四个每次加解密都要格式化 + 写日志，
     是全部日志量的 99%，也是 §105 里「登录后等 100 秒进大厅」的元凶；
     这一个在精简模式下只做「读两个 u16 比一下」，可以常驻。 */
static int install_simple_enc_hook(void)
{
    if (s_simple_enc) return 1;
    if (IsBadReadPtr((const void *)SIMPLE_ENC_VA, 6)) return 0;
    if (memcmp((const void *)SIMPLE_ENC_VA, SIMPLE_SIG, 6) != 0) return 0;  /* 还没解壳 */
    s_simple_enc = install_inline_hook((void *)SIMPLE_ENC_VA,
                                       (void *)det_simple_enc,
                                       "SimpleCipher::Encrypt");
    return s_simple_enc != NULL;
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
    install_simple_enc_hook();
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

#define CODE_PATCH_DELAY_MS 2500

static DWORD WINAPI patch_thread(LPVOID param)
{
    int ticks = 0;
    (void)param;
    /* GameGuard 已由 DR0 + VEH 在执行瞬间处理，不经过本线程，也不修改代码。
       下面仍有地区锁、挂机计时器和诊断 detour 会改游戏代码；它们必须晚于
       ASProtect 启动早期的后台完整性校验，因此暂时保留经本机验证的 2.5 秒门槛。 */
    for (ticks = 0; !g_stop && ticks < CODE_PATCH_DELAY_MS / 20; ticks++) Sleep(20);
    bslog("PATCH   非 GameGuard 代码补丁：延迟 %d ms 后开始", CODE_PATCH_DELAY_MS);

    /* 其余 patch 打在同一个窗口里（解壳已完成、完整性校验窗口已过）。
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
                  "（0x40b419 / 0x4368cf / 0x4f67d1 / 0x46631d / 0x4653a8 "
                  "的特征串一直对不上）");
    }

    for (ticks = 0; !g_stop && !g_reflect_visual_patched && ticks < 2000; ticks++) {
        if (try_patch_reflect_visual()) break;
        Sleep(2);
    }
    if (!g_reflect_visual_patched)
        bslog("PATCH   !! 超时未能 patch 反射道具视觉"
              "（0x5090e2 的特征串一直对不上）");

    /* 地图等级门槛（D142）：不赶时机 —— 0x40b5d0 第一次跑要到进房选图，
       远晚于 +2.5s 的解壳窗口。 */
    if (map_level_lock_kept()) {
        bslog("PATCH   BSHOOK_KEEP_MAP_LEVEL_LOCK 已设，保留原版地图等级门槛");
    } else {
        for (ticks = 0; !g_stop && !g_map_lvl_patched && ticks < 2000; ticks++) {
            if (try_patch_map_level_gate()) break;
            Sleep(2);
        }
        if (!g_map_lvl_patched)
            bslog("PATCH   !! 超时未能 patch 地图等级门槛"
                  "（0x40b623 的特征串一直对不上）");
    }

    /* IME 闪退修复（联机主崩溃，bug调查/3 + bug调查/5）：三处配套，缺一不可。
       不赶时机（解壳后随时可打），但和其它 patch 一样要等特征串出现。 */
    if (ime_crash_fix_keep_original()) {
        bslog("PATCH   BSHOOK_KEEP_IME_CRASH 已设，保留原版 IME 闪退行为");
    } else {
        for (ticks = 0; !g_stop && ticks < 2000; ticks++) {
            if (try_patch_ime_cache_clear() && try_patch_sum_rect_guard()
                && try_patch_ime_cand_layout_guard()) break;
            Sleep(2);
        }
        if (!g_ime_cache_patched || !g_ime_sumrect_patched
            || !g_ime_cand_patched)
            bslog("PATCH   !! 超时未能 patch IME 闪退修复"
                  "（0x4269AB / 0x42515E / 0x4301B4 特征串一直对不上）");
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

    /* 握手版本号（版本管理）：不赶时机 —— OnConnect 最早也要等玩家在登录
       界面点「开始」才执行，远晚于解壳窗口；和其它 patch 一样等特征串。
       ★ 无论补没补上都要在日志里留一行版本：拿到玩家 log 一眼看出版本，
       这本来就是做版本管理的初衷。 */
    if (read_build_ver()) {
        bslog("PATCH   BUILD.ver 版本 %s -> 握手版本号 %ld"
              "（服务端 online.log 里记的就是它）", g_hsver_text, g_hsver_wire);
        for (ticks = 0; !g_stop && !g_hsver_patched && ticks < 2000; ticks++) {
            if (try_patch_handshake_version()) break;
            Sleep(2);
        }
        if (!g_hsver_patched)
            bslog("PATCH   !! 超时未能 patch 握手版本号"
                  "（0x54d98f 一直不是 c7 45 f0 37 01 00 00）"
                  "—— 握手将按原版 311 上报");
    } else {
        bslog("PATCH   !! 包根目录没有可用的 BUILD.ver（缺文件或内容认不出），"
              "握手按原版 311 上报 —— 服务端开了版本门禁时会按旧版客户端处理");
    }

    /* SnowCipher hook 紧跟在其它代码 patch 之后装：
       此时已过了 ASProtect 的完整性校验窗口（+2.5s 实测安全），
       而资源加载(Pack\*.pkn)和登录连接都还没开始，能完整观测到全部加解密。

       ★ 精简模式**根本不装**：这三个 detour 每次加解密都要格式化 + 写日志，
       是全部日志量的 99%，也是启动慢 / 战斗卡的根因（FINDINGS §105）。
       协议早就解完了，日常游玩不需要它。 */
    if (!g_verbose) {
        /* ★ 精简模式仍然要装 `SimpleCipher::Encrypt` 这一个 —— 位置数据的
           UDP 旁路靠它认出出站的位置心跳（见「位置数据的 UDP 旁路」一段）。
           它不写任何日志，代价是每帧读两个 u16。
           另外四个（Snow x3 + Simple::Decrypt）照旧只在详细模式下装。 */
        for (ticks = 0; !g_stop && ticks < 200; ticks++) {
            if (install_simple_enc_hook()) break;
            Sleep(50);
        }
        bslog("SNOW    精简日志模式：只装 SimpleCipher::Encrypt（位置 UDP 旁路用，"
              "不写日志）；逐包 dump 要 BSHOOK_VERBOSE_LOG=1");
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
        install_shell_hooks();   /* shell32 是后加载的，装上为止每轮试一次 */
        report_gameguard_breakpoint();
        poll_modules();
        EnumWindows(dump_window, 0);
        poll_login_dialog();   /* V0.2：分区单选钮 + 注册链接（里程碑 H）*/
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

/* 按环境变量里的名字打开 bsloader 建好的那个事件。名字没传 = 老版本
   bsloader（或手工注入），返回 NULL，调用方各自决定要不要当致命错误。 */
static HANDLE open_loader_event(const char *env_name)
{
    char name[128];
    DWORD n = GetEnvironmentVariableA(env_name, name, sizeof(name));
    if (n == 0 || n >= sizeof(name)) return NULL;
    return OpenEventA(EVENT_MODIFY_STATE, FALSE, name);
}

static void read_gg_retry_flag(void)
{
    char buf[8];
    DWORD n = GetEnvironmentVariableA(POPSHOT_BSHOOK_RETRY_ENV, buf, sizeof(buf));
    InterlockedExchange(&g_gg_retry_allowed,
                        (n > 0 && n < sizeof(buf) && buf[0] != '0') ? 1 : 0);
}

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID reserved)
{
    HANDLE th;
    HANDLE ready_event;
    HANDLE injected_event;
    (void)reserved;

    switch (reason) {
    case DLL_PROCESS_ATTACH:
        DisableThreadLibraryCalls(inst);
        InitializeCriticalSection(&g_cs);
        g_main_thread_id = GetCurrentThreadId(); /* LoadLibrary APC 正在这条主线程上执行 */
        g_dllmain_tick = GetTickCount();
        read_log_level();
        open_log();
        banner();
        read_online_config();   /* V0.2：server.config 经环境变量传进来 */
        read_gg_retry_flag();

        ready_event = open_loader_event(POPSHOT_BSHOOK_READY_ENV);
        if (!ready_event) {
            bslog("HWBP    !! 找不到 bsloader 就绪事件，拒绝在没有 DR0 握手的情况下继续");
            return FALSE;
        }
        /* 这两个只是回报结果用的，老版本 bsloader 没有也照跑。 */
        g_gg_hit_event = open_loader_event(POPSHOT_BSHOOK_HIT_ENV);
        g_gg_failed_event = open_loader_event(POPSHOT_BSHOOK_FAILED_ENV);

        g_gg_veh = AddVectoredExceptionHandler(1, gameguard_veh);
        if (!g_gg_veh) {
            bslog("HWBP    !! AddVectoredExceptionHandler 失败 err=%lu",
                  (unsigned long)GetLastError());
            CloseHandle(ready_event);
            return FALSE;
        }
        bslog("HWBP    GameGuard VEH 已安装，等待 DR0 命中 %08X"
              "（本次%s允许自动重来）",
              (unsigned)POPSHOT_GG_CHECK_VA,
              gameguard_retry_allowed() ? "" : "不");

        /* ★ 在这里、而不是等武装线程跑起来才告诉 bsloader「VEH 装好了」：
           它收到这一发就可以从进程外武装 DR0，不必等加载器锁放开（§179）。
           顺序是硬约束 —— 必须在 AddVectoredExceptionHandler 成功之后置位，
           否则 bsloader 可能在没人处理单步异常时就把断点摆上去。 */
        injected_event = open_loader_event(POPSHOT_BSHOOK_INJECTED_ENV);
        if (injected_event) {
            SetEvent(injected_event);
            CloseHandle(injected_event);
        }

        /* 线程入口要等 DllMain 返回后才会运行。它观察主线程真正离开 APC 恢复路径，
           然后设置 DR0 并通知 bsloader；ready_event 的所有权一并交给它。 */
        th = CreateThread(NULL, 0, arm_gameguard_breakpoint_thread, ready_event, 0, NULL);
        if (!th) {
            bslog("HWBP    !! 创建 DR0 武装线程失败 err=%lu", (unsigned long)GetLastError());
            CloseHandle(ready_event);
            RemoveVectoredExceptionHandler(g_gg_veh);
            g_gg_veh = NULL;
            return FALSE;
        }
        CloseHandle(th);

        th = CreateThread(NULL, 0, patch_thread, NULL, 0, NULL);
        if (th) CloseHandle(th);
        /* TODO 阶段3: install_ws2_hooks(); */
        th = CreateThread(NULL, 0, watch_thread, NULL, 0, NULL);
        if (th) CloseHandle(th);
        break;

    case DLL_PROCESS_DETACH:
        g_stop = 1;
        if (g_gg_veh) {
            RemoveVectoredExceptionHandler(g_gg_veh);
            g_gg_veh = NULL;
        }
        bslog("================ process detach ================");
        break;
    }
    return TRUE;
}
