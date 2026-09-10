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

/* ★★★ 异步写盘（用户 2026-09-01：「把所有 log 改成异步写入，不要阻塞游戏进程」）
 *
 * 改之前：`bslog_emit` 在**游戏线程上**做 格式化 → 进临界区 → WriteFile →
 * FlushFileBuffers（上面注释里自己记的 **2.0 ms/条**，而且是**握着锁**等的）
 * → 出临界区 → OutputDebugStringA。于是：
 *   - 打日志的那条线程每条停 2 ms；
 *   - 临界区盖住了 flush ⇒ 别的线程（watch_thread 10 Hz、patch_thread）
 *     一打日志就把渲染/网络线程一起拖住 —— 「护航效应」；
 *   - 弹体诊断是**每帧 × 每弹体**的（见 `proj_tick_log`），60 fps 的 16.7 ms
 *     预算里光日志就吃掉大半 —— 用户报的「子弹一多就突发性掉帧」正是这个。
 *
 * 改之后：**环形缓冲 + 一条专用写盘线程**。
 *   - 生产者只做「格式化 + memcpy 进环 + 可能一次 SetEvent」，微秒级；
 *   - 写线程整批取走、**一次 WriteFile 写一批**，磁盘的账全记在它头上。
 *
 * ★ flush 的判据是**「这一批里有没有关键记录」**，不是计时器也不是计数
 *   （铁律 10）：忙的时候一批几百条只 flush 一次，闲的时候一批就一条、
 *   和改之前一样一条一 flush。关键事件的持久性一点没降。
 *
 * ★ 崩溃安全性：`WriteFile` 过的字节即使没 flush 也已经在 OS 文件缓存里，
 *   进程被 TerminateProcess 掉也不丢，只有整机断电才丢 —— 这正是下面
 *   「详细日志不 flush」那条早就在用的理由，现在对关键日志同样成立，
 *   因为 flush 只是从「每条」变成「每批」，没有取消。真正的风险窗口只剩
 *   「还在环里、写线程还没取走」的那几微秒。
 */
#define BSLOG_RING_BYTES   (1 << 22)      /* 4 MB。这是**内存预算**，不是判据 */
#define BSLOG_BATCH_BYTES  (64 * 1024)    /* 写线程一批最多搬这么多 */

static char   g_ring[BSLOG_RING_BYTES];
static DWORD  g_ring_head = 0;            /* 写线程从这儿取 */
static DWORD  g_ring_tail = 0;            /* 生产者往这儿放 */
static DWORD  g_ring_used = 0;
static DWORD  g_dropped   = 0;            /* 环满丢掉了几条（写线程负责补报） */
static HANDLE g_log_evt    = NULL;        /* 自动重置：环「从空变非空」时置位 */
static HANDLE g_log_thread = NULL;

/* 环里一条记录 = [长度低字节][长度高字节][detail 标志][正文 长度字节]。
   正文最长 8192，3 + 8192 一定塞得进 BSLOG_BATCH_BYTES，所以写线程每批
   至少能搬走一条，不会卡死在「一条都放不下」上。 */
#define BSLOG_HDR 3

/* 下面四个 ring_* 都**要求调用方已经持有 g_cs**（收尾路径除外，见 bslog_shutdown）。 */
static void ring_put(const void *src, DWORD n)
{
    DWORD first = BSLOG_RING_BYTES - g_ring_tail;
    if (first > n) first = n;
    memcpy(g_ring + g_ring_tail, src, first);
    if (n > first) memcpy(g_ring, (const char *)src + first, n - first);
    g_ring_tail = (g_ring_tail + n) % BSLOG_RING_BYTES;
    g_ring_used += n;
}

static void ring_get(void *dst, DWORD n)
{
    DWORD first = BSLOG_RING_BYTES - g_ring_head;
    if (first > n) first = n;
    memcpy(dst, g_ring + g_ring_head, first);
    if (n > first) memcpy((char *)dst + first, g_ring, n - first);
    g_ring_head = (g_ring_head + n) % BSLOG_RING_BYTES;
    g_ring_used -= n;
}

/* 环头那条记录的**整条**长度（含 3 字节头）；环空返回 0。不移动 head。 */
static DWORD ring_peek(void)
{
    DWORD lo, hi;
    if (g_ring_used < BSLOG_HDR) return 0;
    lo = (unsigned char)g_ring[g_ring_head];
    hi = (unsigned char)g_ring[(g_ring_head + 1) % BSLOG_RING_BYTES];
    return BSLOG_HDR + lo + (hi << 8);
}

/* 直接写盘的底层（写线程 / 收尾路径用，**不经过环**）。 */
static void bslog_write_raw(const char *text, DWORD n)
{
    DWORD written;
    if (g_log != INVALID_HANDLE_VALUE && n)
        WriteFile(g_log, text, n, &written, NULL);
}

/* 把环里现在有的记录搬一批写出去。返回这一批搬了多少字节（0 = 环空）。
   `take_lock = 0` 只给收尾路径用（那时没有并发方，见 bslog_shutdown）。 */
static DWORD bslog_drain_once(int take_lock)
{
    /* ★ 只有写线程和收尾路径会进这个函数，两者不并发，所以 static 缓冲安全，
       也避免了在 4 MB 环之外再往栈上要 64 KB。 */
    static char batch[BSLOG_BATCH_BYTES];
    static char text[BSLOG_BATCH_BYTES];
    DWORD used = 0, dropped = 0, rec, off, textn = 0;
    int   crit = 0;

    if (take_lock) EnterCriticalSection(&g_cs);
    while ((rec = ring_peek()) != 0 && used + rec <= sizeof(batch)) {
        ring_get(batch + used, rec);
        used += rec;
    }
    dropped = g_dropped;
    g_dropped = 0;
    if (take_lock) LeaveCriticalSection(&g_cs);

    if (!used && !dropped) return 0;

    /* 这一段全在锁外做 —— 生产者可以同时往环里放，互不打扰。 */
    for (off = 0; off + BSLOG_HDR <= used; ) {
        DWORD len = (unsigned char)batch[off] + ((DWORD)(unsigned char)batch[off + 1] << 8);
        int   detail = batch[off + 2];
        if (off + BSLOG_HDR + len > used) break;      /* 不该发生，防御 */
        memcpy(text + textn, batch + off + BSLOG_HDR, len);
        textn += len;
        if (!detail) crit = 1;
        off += BSLOG_HDR + len;
    }
    bslog_write_raw(text, textn);

    /* 环满丢过日志就补一行说清楚。**按状态翻转补报**（丢过 → 说一次 → 清零），
       不是按次数也不是按时间（铁律 10）。这一行自己算关键记录。 */
    if (dropped) {
        char note[128];
        int  m = _snprintf(note, sizeof(note) - 1,
                           "LOG     !! 日志缓冲满，丢了 %lu 条\r\n",
                           (unsigned long)dropped);
        if (m > 0) {
            bslog_write_raw(note, (DWORD)m);
            crit = 1;
        }
    }

    if (crit) {
        FlushFileBuffers(g_log);
        /* OutputDebugStringA 在没有调试器时也要走一次 RaiseException，
           有 DebugView 时还要抢全局互斥体 —— 现在它在**写线程**上，
           游戏线程一分钱都不出。仍然只给关键记录用。 */
        for (off = 0; off + BSLOG_HDR <= used; ) {
            DWORD len = (unsigned char)batch[off] + ((DWORD)(unsigned char)batch[off + 1] << 8);
            int   detail = batch[off + 2];
            if (off + BSLOG_HDR + len > used) break;
            if (!detail) {
                char save = batch[off + BSLOG_HDR + len];   /* 借下一条的头字节当结束符 */
                batch[off + BSLOG_HDR + len] = '\0';
                OutputDebugStringA(batch + off + BSLOG_HDR);
                batch[off + BSLOG_HDR + len] = save;
            }
            off += BSLOG_HDR + len;
        }
    }
    return used ? used : 1;
}

static DWORD WINAPI log_writer_thread(void *param)
{
    (void)param;
    for (;;) {
        WaitForSingleObject(g_log_evt, INFINITE);
        while (bslog_drain_once(1)) { }
        if (g_stop) break;
    }
    return 0;
}

/* 收尾：把环里剩下的全排出去。进程退出、以及 DllMain 失败返回之前调。
 *
 * ★ 用 TryEnterCriticalSection 而不是 Enter：DLL_PROCESS_DETACH 跑到的时候
 *   别的线程已经被系统干掉了，万一有一条正好死在临界区里，Enter 会永远等下去。
 *   拿不到就直接排 —— 此时没有并发方，不加锁是安全的。 */
static void bslog_shutdown(void)
{
    int got = TryEnterCriticalSection(&g_cs) ? 1 : 0;
    while (bslog_drain_once(0)) { }
    if (got) LeaveCriticalSection(&g_cs);
    if (g_log != INVALID_HANDLE_VALUE) FlushFileBuffers(g_log);
}

static void bslog_emit(int detail, const char *fmt, va_list ap)
{
    char line[8192];
    unsigned char hdr[BSLOG_HDR];
    SYSTEMTIME st;
    int n;
    int wake = 0;

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

    hdr[0] = (unsigned char)(n & 0xFF);
    hdr[1] = (unsigned char)((n >> 8) & 0xFF);
    hdr[2] = (unsigned char)(detail ? 1 : 0);

    EnterCriticalSection(&g_cs);
    if ((DWORD)n + BSLOG_HDR <= BSLOG_RING_BYTES - g_ring_used) {
        wake = (g_ring_used == 0);
        ring_put(hdr, BSLOG_HDR);
        ring_put(line, (DWORD)n);
    } else {
        /* 环满：**丢掉，绝不阻塞游戏线程**。写线程会补一行说丢了几条。 */
        g_dropped++;
    }
    LeaveCriticalSection(&g_cs);

    /* 只有「环从空变非空」才叫醒写线程 —— 它自己会一直排到空为止，
       所以不会漏唤醒，也不会每条一次系统调用。 */
    if (wake && g_log_evt) SetEvent(g_log_evt);
}

/* 关键事件：任何模式都记，且**这一批**写完就落盘（崩溃时不能丢）。
   「这一批」是异步化之后的口径 —— 见上面 bslog_drain_once 的说明。 */
void bslog(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    bslog_emit(0, fmt, ap);
    va_end(ap);
}

/* 详细/高频事件：只在 `BSHOOK_VERBOSE_LOG=1` 时记，且不 flush、不进 DebugView。 */
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

#include "bot_motion.inc"

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
                    L"远程服务器\n(IP设置:config目录)");

    /* 「远程服务器(IP设置:config目录)」比原来的「枪林弹雨(网通)」长得多，
       126 像素的原控件会把它裁掉，所以改成**两行**：加 BS_MULTILINE 再把控件
       放高，文案里那个换行符就是断行处（按钮的 DrawText 带 DT_WORDBREAK，认 \n）。
       （2026-08-23 前第二行是「(IP设置:server.config)」，配置文件挪进 config\
       后改的；新文案比旧的还短几个像素，但下一条警告依然适用。）

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
/* CreateProcess / WinExec 钩子（V0.2 自动更新）                               */
/*                                                                            */
/* 客户端的升级分支（0x54dbf6 收到拒绝后）拉起更新引导器                      */
/* game_patched\BsPatcherChn.exe（我们的 updater.c，见 tools/updater.c）。     */
/* 它的命令行模板是一整条字符串（"-mode:patch -procid:'%d' …"，V0.1 §14），  */
/* 像是 CreateProcess / WinExec 的用法 —— 三个入口都挂上。                    */
/*                                                                            */
/* 2026-08-22 真机踩坑：新编译的未签名 exe 首次运行可能被杀软拦下，            */
/* CreateProcess 直接失败 —— 客户端对失败一声不吭就退出，玩家看到的是         */
/* 「点了登录、窗口全没了、什么提示都没有」。钩在这里做两件事：               */
/*   1. ★PROC 日志：客户端起的每个进程都记下来（对逆向也有价值）；            */
/*   2. 拉的是 BsPatcher 且失败时，由【游戏进程】自己弹框告诉玩家发生了       */
/*      什么、该怎么办 —— 更新器自己被拦的时候没有机会开口。                  */
/* 成功路径一个字节的行为都不改（原样调回真函数）。                           */
/* -------------------------------------------------------------------------- */
typedef BOOL (WINAPI *CreateProcessW_t)(LPCWSTR, LPWSTR, LPSECURITY_ATTRIBUTES,
                                        LPSECURITY_ATTRIBUTES, BOOL, DWORD,
                                        LPVOID, LPCWSTR, LPSTARTUPINFOW,
                                        LPPROCESS_INFORMATION);
typedef BOOL (WINAPI *CreateProcessA_t)(LPCSTR, LPSTR, LPSECURITY_ATTRIBUTES,
                                        LPSECURITY_ATTRIBUTES, BOOL, DWORD,
                                        LPVOID, LPCSTR, LPSTARTUPINFOA,
                                        LPPROCESS_INFORMATION);
typedef UINT (WINAPI *WinExec_t)(LPCSTR, UINT);
static CreateProcessW_t s_CreateProcessW = NULL;
static CreateProcessA_t s_CreateProcessA = NULL;
static WinExec_t s_WinExec = NULL;

static int is_patcher_w(const wchar_t *app, const wchar_t *cmd)
{
    if ((app && wcsstr(app, L"BsPatcher")) || (cmd && wcsstr(cmd, L"BsPatcher")))
        return 1;
    return 0;
}

static void warn_patcher_launch_failed(DWORD err)
{
    wchar_t text[1024];
    _snwprintf(text, 1024,
        L"自动更新程序启动失败（错误码 %lu，多半是杀毒软件把新更新的"
        L" BsPatcherChn.exe 拦下了）。\n\n"
        L"可以：\n"
        L"  1. 手动双击 game_patched\\BsPatcherChn.exe 再试；\n"
        L"  2. 把游戏目录加进杀毒软件白名单；\n"
        L"  3. 或手动下载完整客户端(QQ群文件或Github)：\n"
        L"https://github.com/liubz102/popshot-reborn/releases",
        (unsigned long)err);
    text[1023] = 0;
    MessageBoxW(NULL, text, L"自动更新", MB_ICONWARNING | MB_OK);
}

static BOOL WINAPI det_CreateProcessW(LPCWSTR app, LPWSTR cmd,
                                      LPSECURITY_ATTRIBUTES pa,
                                      LPSECURITY_ATTRIBUTES ta, BOOL inherit,
                                      DWORD flags, LPVOID env, LPCWSTR dir,
                                      LPSTARTUPINFOW si,
                                      LPPROCESS_INFORMATION pi)
{
    BOOL ok = s_CreateProcessW(app, cmd, pa, ta, inherit, flags, env,
                               dir, si, pi);
    char u8a[1536], u8c[1536];
    bslog("★PROC CreateProcessW(app=\"%s\" cmd=\"%s\") -> %s",
          w2u8(app ? app : L"(null)", u8a, sizeof(u8a)),
          w2u8(cmd ? cmd : L"(null)", u8c, sizeof(u8c)),
          ok ? "ok" : "FAIL");
    if (!ok && is_patcher_w(app, cmd)) {
        bslog("★PROC 更新引导器拉起失败（err=%lu）—— 弹框告知玩家",
              (unsigned long)GetLastError());
        warn_patcher_launch_failed(GetLastError());
    }
    return ok;
}

static BOOL WINAPI det_CreateProcessA(LPCSTR app, LPSTR cmd,
                                      LPSECURITY_ATTRIBUTES pa,
                                      LPSECURITY_ATTRIBUTES ta, BOOL inherit,
                                      DWORD flags, LPVOID env, LPCSTR dir,
                                      LPSTARTUPINFOA si,
                                      LPPROCESS_INFORMATION pi)
{
    wchar_t wapp[512], wcmd[1024];
    BOOL ok = s_CreateProcessA(app, cmd, pa, ta, inherit, flags, env,
                               dir, si, pi);
    bslog("★PROC CreateProcessA(app=\"%s\" cmd=\"%s\") -> %s",
          app ? app : "(null)", cmd ? cmd : "(null)", ok ? "ok" : "FAIL");
    if (app) { MultiByteToWideChar(CP_ACP, 0, app, -1, wapp, 512); wapp[511] = 0; }
    else wapp[0] = 0;
    if (cmd) { MultiByteToWideChar(CP_ACP, 0, cmd, -1, wcmd, 1024); wcmd[1023] = 0; }
    else wcmd[0] = 0;
    if (!ok && is_patcher_w(wapp, wcmd)) {
        bslog("★PROC 更新引导器拉起失败（err=%lu）—— 弹框告知玩家",
              (unsigned long)GetLastError());
        warn_patcher_launch_failed(GetLastError());
    }
    return ok;
}

static UINT WINAPI det_WinExec(LPCSTR cmd, UINT show)
{
    UINT rc = s_WinExec(cmd, show);
    bslog("★PROC WinExec(cmd=\"%s\") -> %u", cmd ? cmd : "(null)", rc);
    if (rc < 32) {                              /* <32 = 失败（WinExec 语义） */
        wchar_t wcmd[1024];
        if (cmd) { MultiByteToWideChar(CP_ACP, 0, cmd, -1, wcmd, 1024); wcmd[1023] = 0; }
        else wcmd[0] = 0;
        if (is_patcher_w(NULL, wcmd)) {
            bslog("★PROC 更新引导器拉起失败（WinExec rc=%u）—— 弹框告知玩家", rc);
            warn_patcher_launch_failed(rc);
        }
    }
    return rc;
}

/* kernel32 常驻必有（本 DLL 的所有 import 都靠它），不像 shell32 要等加载。 */
static void install_process_hooks(void)
{
    HMODULE k32;
    if (s_CreateProcessW) return;                /* 已装 */
    k32 = GetModuleHandleA("kernel32.dll");
    if (!k32) return;
    s_CreateProcessW = (CreateProcessW_t)install_inline_hook(
        (void *)GetProcAddress(k32, "CreateProcessW"),
        (void *)det_CreateProcessW, "CreateProcessW");
    s_CreateProcessA = (CreateProcessA_t)install_inline_hook(
        (void *)GetProcAddress(k32, "CreateProcessA"),
        (void *)det_CreateProcessA, "CreateProcessA");
    s_WinExec = (WinExec_t)install_inline_hook(
        (void *)GetProcAddress(k32, "WinExec"),
        (void *)det_WinExec, "WinExec");
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
/* 玩家等级门槛 patch —— 对战 / 生存 / 5-6 人房 / 天梯 / 新手图                 */
/*                              （V0.3商店 D22，接替服务端的 D120）            */
/*                                                                            */
/*   【为什么必须挪到 hook 里】客户端把 `gspRepLogin` 下发的等级存进全局        */
/*   `[0x72e338]`，而这**一个**数同时被三类代码读：                            */
/*                                                                            */
/*     1. 界面上的等级显示（`0x41a467` 玩家数据栏、`0x465948` 房间信息栏）；    */
/*     2. 原版那几道等级门（下面 7 处）；                                       */
/*     3. ★ **物品的「穿上」判定** —— 商店/仓库 `0x445817` 和房间 `0x46aff1`   */
/*        都拿 `ItemInfo+0x1c`（服务端在 `0x0501` 里发的等级要求）和它比。      */
/*                                                                            */
/*   V0.2 D120 的做法是把下发值抬到 4 来顶开第 2 类，代价是第 1 类（1~3 级号   */
/*   显示成 4 级）和第 3 类（商店的等级门槛跟着放水）一起被改掉。V0.3 商店      */
/*   要按真实等级卖东西，于是反过来：**服务端发真实等级，第 2 类逐个 patch**。 */
/*                                                                            */
/*   ⚠ 第 3 类那两处 (`0x445817` / `0x46aff1`) **不许动** —— 它们正是商店      */
/*   物品等级限制在客户端这一侧的落点。                                        */
/*                                                                            */
/*   七处判据（全是脱壳镜像 `re/BigShot_22524.img` 上核过的唯一特征串）：       */
/*                                                                            */
/*   1) `0x440b08` 频道准入（V0.1 §83）                                        */
/*        33 DB 43              xor ebx,ebx / inc ebx     ; ebx = 1            */
/*        39 1D 38 E3 72 00     cmp [0x72e338], ebx       ; 等级**恰好** == 1  */
/*        0F 85 88 00 00 00     jne 0x440b9c              ; != 1 -> 不拦       */
/*      命中就弹「레벨이 맞지 않아 접속 하실 수 없습니다」（不符合等级要求，     */
/*      无法连接）。jne -> jmp：任何等级都走「不拦」那条路。                    */
/*                                                                            */
/*   2) `0x440cd9` 同一个提示的第二个引用点（`0x440d2a`），判据一模一样。       */
/*      §83 早就写明这句话有**两处**引用，只 patch 一处会漏。                   */
/*                                                                            */
/*   3) `0x465338` / 4) `0x465a2c` 生存模式被强制改回夺分（V0.2 §203）         */
/*        83 3D 38 E3 72 00 04  cmp [0x72e338], 4                             */
/*        7D 0D / 7D 0E         jge 跳过                  ; >= 4 -> 不改       */
/*      两处都是「中国区 + 模式 0(生存) + 房主 + 等级 < 4 -> 模式写成 3(夺分)」，*/
/*      一处在房间设定对话框、一处在房间回调；§203 当时只逆到后者。            */
/*      jge -> jmp：永远走「不改」。                                           */
/*                                                                            */
/*   5) `0x4374d5` 建房(对战)「人数」下拉框                                    */
/*        BE 70 E5 72 00        mov esi, 0x72e570         ; 人数表             */
/*        C7 45 10 05 …         mov [ebp+0x10], 5         ; 5 条              */
/*        3B 5E 08              cmp ebx, [esi+8]          ; 等级 vs 条目要求   */
/*        7C 17                 jl 跳过这一条                                  */
/*      表里 5 条 {2,3,4,5,6} 人，★ 只有 **5 人和 6 人的要求等级是 4**         */
/*      （`0x72e594` / `0x72e5a0`，全客户端的下拉框静态表里唯一两个非 0 门槛）。*/
/*      NOP 掉 jl：五条全部进下拉框。                                          */
/*                                                                            */
/*   6) `0x54f8f2` 建房结果处理器（`0x0201`）里的**新手图强制**               */
/*        39 1D 38 E3 72 00     cmp [0x72e338], ebx       ; ebx = 1（0x54f80c）*/
/*        0F 85 1E 01 00 00     jne 0x54fa1c                                   */
/*      等级**恰好 1** 时，客户端自己把新建房间的地图改成 `Newbe2-1`           */
/*      （지하유적 입구，`map.ini` 里 MaxUser=2）/ `Newbe2-1:NewPvp`。         */
/*      ★ 这条以前从没触发过 —— D120 把 1 级顶成了 4。jne -> jmp。            */
/*                                                                            */
/*   7) `0x43b676` 天梯标签页（V0.1 §62）                                      */
/*        83 3D 38 E3 72 00 06  cmp [0x72e338], 6                             */
/*        0F 8D A6 00 00 00     jge 0x43b729              ; >= 6 -> 放行       */
/*      不够就弹「레벨 %d이상만 플레이하실 수 있습니다」并**不发包**。         */
/*      D120 时代等级顶到 4 也进不去，是用户 2026-09-05 拍板一并解除的。       */
/*                                                                            */
/*   ★ 没有动、也不该动的两处 `cmp [0x72e338], 4`：`0x44c94d` / `0x44cc00` /   */
/*     `0x467eaa` 是角色 3 아이린 的 4 级解锁 —— 她被 `CharacterChanger` 的     */
/*     按钮循环 `0x4f58e8` 显式跳过，界面上本来就放不出来（见                  */
/*     `account_store.PREMIUM_CHARACTER_IDS` 的注释）；`0x43b47e` 是中国区     */
/*     本来就跳过的新手图标。                                                  */
/*                                                                            */
/*   设 BSHOOK_KEEP_PLAYER_LEVEL_LOCK=1 保留原版全部玩家等级门槛。            */
/* -------------------------------------------------------------------------- */
#define PLR_LVL_SITE_COUNT 7
static const struct {
    unsigned int va;
    unsigned int len;
    unsigned int off;
    unsigned int n;
    const unsigned char *sig;
    const unsigned char *fix;
    const char *what;
} PLR_LVL_SITES[PLR_LVL_SITE_COUNT] = {
    { 0x00440b05u, 15, 9, 6,
      (const unsigned char *)"\x33\xDB\x43\x39\x1D\x38\xE3\x72\x00\x0F\x85\x88\x00\x00\x00",
      (const unsigned char *)"\xE9\x89\x00\x00\x00\x90",   /* jne -> jmp 0x440b9c */
      "对战频道准入 0x440af7 第 1 处（等级==1 不再拦）" },
    { 0x00440cd6u, 11, 9, 2,
      (const unsigned char *)"\x33\xF6\x46\x39\x35\x38\xE3\x72\x00\x75\x7A",
      (const unsigned char *)"\xEB\x7A",                   /* jne -> jmp 0x440d5b */
      "对战频道准入第 2 处 0x440cd9（同一句提示的另一个引用）" },
    { 0x00465338u, 13, 7, 2,
      (const unsigned char *)"\x83\x3D\x38\xE3\x72\x00\x04\x7D\x0D\x83\x7E\x04\x05",
      (const unsigned char *)"\xEB\x0D",                   /* jge -> jmp 0x46534e */
      "生存模式强制改夺分 0x465338（房间设定对话框）" },
    { 0x00465a2cu, 15, 7, 2,
      (const unsigned char *)"\x83\x3D\x38\xE3\x72\x00\x04\x7D\x0E\x8B\x86\xCC\x01\x00\x00",
      (const unsigned char *)"\xEB\x0E",                   /* jge -> jmp 0x465a43 */
      "生存模式强制改夺分 0x465a2c（房间回调，V0.2 §203）" },
    { 0x004374c9u, 17, 15, 2,
      (const unsigned char *)"\xBE\x70\xE5\x72\x00\xC7\x45\x10\x05\x00\x00\x00\x3B\x5E\x08\x7C\x17",
      (const unsigned char *)"\x90\x90",                   /* NOP 掉 jl（5/6 人也进表） */
      "建房(对战)人数下拉框 0x4374d5（5 人 / 6 人的要求等级 4）" },
    { 0x0054f8f2u, 15, 6, 6,
      (const unsigned char *)"\x39\x1D\x38\xE3\x72\x00\x0F\x85\x1E\x01\x00\x00\x8D\x4D\xC0",
      (const unsigned char *)"\xE9\x1F\x01\x00\x00\x90",   /* jne -> jmp 0x54fa1c */
      "1 级号建房被塞进新手图 Newbe2-1 @ 0x54f8f2" },
    { 0x0043b676u, 13, 7, 6,
      (const unsigned char *)"\x83\x3D\x38\xE3\x72\x00\x06\x0F\x8D\xA6\x00\x00\x00",
      (const unsigned char *)"\xE9\xA7\x00\x00\x00\x90",   /* jge -> jmp 0x43b729 */
      "天梯标签页等级 >=6 @ 0x43b676" },
};
static volatile LONG g_plr_lvl_patched = 0;

static int player_level_lock_kept(void)
{
    char buf[8];
    DWORD n = GetEnvironmentVariableA("BSHOOK_KEEP_PLAYER_LEVEL_LOCK",
                                      buf, sizeof(buf));
    return n > 0 && n < sizeof(buf) && buf[0] != '0';
}

static int try_patch_player_level_gate(void)
{
    int i, done = 0;

    if (g_plr_lvl_patched) return 1;
    for (i = 0; i < PLR_LVL_SITE_COUNT; i++) {
        unsigned char *base = (unsigned char *)PLR_LVL_SITES[i].va;
        unsigned char *p = base + PLR_LVL_SITES[i].off;
        DWORD oldp;

        if (IsBadReadPtr(base, PLR_LVL_SITES[i].len)) continue;
        if (memcmp(p, PLR_LVL_SITES[i].fix, PLR_LVL_SITES[i].n) == 0) { done++; continue; }
        if (memcmp(base, PLR_LVL_SITES[i].sig, PLR_LVL_SITES[i].len) != 0)
            continue;                            /* 还没解壳到这里，继续等 */

        if (!VirtualProtect(p, PLR_LVL_SITES[i].n, PAGE_EXECUTE_READWRITE, &oldp)) {
            bslog("PATCH   玩家等级门槛(%s): VirtualProtect 失败 err=%lu",
                  PLR_LVL_SITES[i].what, (unsigned long)GetLastError());
            continue;
        }
        memcpy(p, PLR_LVL_SITES[i].fix, PLR_LVL_SITES[i].n);
        VirtualProtect(p, PLR_LVL_SITES[i].n, oldp, &oldp);
        FlushInstructionCache(GetCurrentProcess(), p, PLR_LVL_SITES[i].n);
        bslog("PATCH   ★玩家等级门槛 @ %08X: %s",
              (unsigned)(PLR_LVL_SITES[i].va + PLR_LVL_SITES[i].off),
              PLR_LVL_SITES[i].what);
        done++;
    }
    if (done == PLR_LVL_SITE_COUNT) {
        InterlockedExchange(&g_plr_lvl_patched, 1);
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
/* ★ 「溅射范围加成」提示的空指针闪退（V0.3 合成与商店 §47 / D55）            */
/*                                                                            */
/*   `Projectile::OnExplode`（0x492715）处理一发 `rpExplode` 的最后一步是叫   */
/*   虚表槽 +0x12c = 0x47e776（this=弹体，arg0=命中的角色指针，arg1=flags，    */
/*   arg2=爆炸点）画两种加成提示：flags 0x20「HEAD SHOT!」、                  */
/*   flags 0x200「ESPERAN SPLASH!」（= 射手穿的 IncSplashRange 装备那道 15%   */
/*   门开了，0x47e76b 置位）。目标句柄查不到（`+4` = 0：打空 / 打地形）时      */
/*   arg0 是 NULL，而两支的判空不对称：                                       */
/*                                                                            */
/*     0047e79d  cmp edi, esi ; je 0x47e9be     ; 头射那一支：没目标就不画     */
/*     0047e9be  test byte [ebp+0xd], 2         ; flags & 0x200 ?              */
/*     0047e9c8  [[0x72e320]] == 2 ?            ; 中国区 -> 0x47ea21           */
/*     0047e9d3  韩国区：文字画在爆炸点，不碰目标 —— 没事                       */
/*     0047ea21  中国区：取图 Images/Chinese/Img…（有目标才有地方挂）          */
/*     0047ea6c  mov eax, [edi] ; call [eax+8]  ; ★★ edi = NULL -> 读 0 闪退   */
/*                                                                            */
/*   自己发的 `rpExplode` 也会本地回环处理（V0.2 §151 的 0x405a5d），所以     */
/*   射手自己先崩；服务端在转发时改 flags 救不了他，而且 0x200 在收方还决定    */
/*   溅射范围倍率（0x492785），不能抹。只能在客户端补那一个判空：               */
/*                                                                            */
/*   0x47ea21 头 5 字节（push 7; lea eax,[ebp+0xc] —— 正好 5 字节，都不带      */
/*   相对地址）换成 E9 跳 detour：edi 为空就直接走函数尾 0x47eb0e（和          */
/*   0x47e9c2 那条 je 去的同一个出口，中间没有多压栈），否则补回两条原指令      */
/*   跳回 0x47ea26。表现：打空的那一发不画「溅射加成」图，其余一字不改。        */
/*                                                                            */
/*   设 BSHOOK_KEEP_SPLASH_VISUAL_CRASH=1 保留原版行为（闪退复现 / 对照用）。  */
/* -------------------------------------------------------------------------- */
#define SPLASH_VISUAL_VA        0x0047EA21u
#define SPLASH_VISUAL_SIG_LEN   17
static const unsigned char SPLASH_VISUAL_SIG[SPLASH_VISUAL_SIG_LEN] = {
    0x6A, 0x07,                         /* push 7                            */
    0x8D, 0x45, 0x0C,                   /* lea  eax, [ebp+0xC]               */
    0x50,                               /* push eax                          */
    0xFF, 0x35, 0xA0, 0x9A, 0x6E, 0x00, /* push [0x6e9aa0]                   */
    0xE8, 0x8E, 0x15, 0x00, 0x00        /* call 0x47ffc0（取第 7 号图片名）  */
};

/* detour 里要用的立即数不带后缀（MSVC 内联汇编不吃 0x…u 这种写法） */
#define SPLASH_VISUAL_RETURN_TO 0x0047EA26   /* 补回两条原指令后接着跑        */
#define SPLASH_VISUAL_EXIT      0x0047EB0E   /* 函数尾：mov ecx,[ebp-0xC] … ret 0xC */

static __declspec(naked) void splash_visual_guard_detour(void)
{
    __asm {
        test edi, edi                       /* edi = 命中的角色指针（arg0） */
        jz   svg_skip
        push 7                              /* 被偷走的两条原指令，逐条补回 */
        lea  eax, [ebp + 0xC]
        push SPLASH_VISUAL_RETURN_TO
        ret
    svg_skip:
        push SPLASH_VISUAL_EXIT             /* 没有目标：不画，走原有出口 */
        ret
    }
}

static volatile LONG g_splash_visual_patched = 0;

static int splash_visual_crash_keep_original(void)
{
    /* 语义和 ime_crash_fix_keep_original() 一样：设了才是「保留原版」。 */
    char buf[8];
    DWORD n = GetEnvironmentVariableA("BSHOOK_KEEP_SPLASH_VISUAL_CRASH", buf, sizeof(buf));
    return n > 0 && n < sizeof(buf) && buf[0] != '0';
}

static int try_patch_splash_visual_guard(void)
{
    unsigned char *p = (unsigned char *)SPLASH_VISUAL_VA;
    DWORD oldp;

    if (g_splash_visual_patched) return 1;
    if (IsBadReadPtr(p, SPLASH_VISUAL_SIG_LEN)) return 0;
    /* 幂等：已打过就是「E9 <跳到我们 detour 的 rel32>」 */
    if (p[0] == 0xE9
        && (DWORD)(*(int *)(p + 1))
               == (DWORD)((UINT_PTR)&splash_visual_guard_detour
                          - (UINT_PTR)(p + 5))) {
        InterlockedExchange(&g_splash_visual_patched, 1);
        return 1;
    }
    if (memcmp(p, SPLASH_VISUAL_SIG, SPLASH_VISUAL_SIG_LEN) != 0)
        return 0;                          /* 还没解壳到这里，或不是已确认的版本 */

    if (!VirtualProtect(p, 5, PAGE_EXECUTE_READWRITE, &oldp)) {
        bslog("PATCH   溅射加成提示判空: VirtualProtect 失败 err=%lu",
              (unsigned long)GetLastError());
        return 0;
    }
    p[0] = 0xE9;
    *(DWORD *)(p + 1) = (DWORD)((UINT_PTR)&splash_visual_guard_detour
                                - (UINT_PTR)(p + 5));
    VirtualProtect(p, 5, oldp, &oldp);
    FlushInstructionCache(GetCurrentProcess(), p, 5);
    InterlockedExchange(&g_splash_visual_patched, 1);
    bslog("PATCH   ★溅射加成提示判空 @ %08X: rpExplode 目标句柄查不到（打空）"
          "且 flags 带 0x200 时不再读空指针（原版 0x47ea6c 会闪退）",
          (unsigned)SPLASH_VISUAL_VA);
    return 1;
}

/* -------------------------------------------------------------------------- */
/* ★ 礼物盒里的材料没图标 —— 图标查错了表（V0.3商店 §75，2026-09-10 实机）   */
/*                                                                            */
/*   客户端启动时从 ShopItem-Chn.ini 建了**两张** id→图标路径的表，挂在同一个   */
/*   对象 [0x72e1e0] 上：+0x00 那张来自 `[Stock-N]` 节（商店在卖的 1817 件），   */
/*   +0x14 那张来自 `[Item-N]` 节（全部 808 件，材料 / 卡片都在）。            */
/*   0x4169e9 查第一张、0x416a1a 查第二张（两个函数除了 `add ecx,0x14` 逐字节  */
/*   一样）。礼物槽（0x463289）和接收礼物弹窗（0x461aeb）走的是 0x4169e9 ——    */
/*   原版礼物只能是商店买来的东西，够用；我们从管理页发**材料**，第一张表查   */
/*   不到就回空串，画出来是「(FileNotFound)」。合成 / 仓库 / 结算界面走的是     */
/*   0x416a1a，所以那几处材料图标一直是好的。                                  */
/*                                                                            */
/*   补法：0x4169e9 查不到时不再返回空串，改去查第二张 —— 把 miss 分支头一条   */
/*   `push offset L""`（5 字节，0x4169ff）换成 jmp，detour 里                  */
/*   `mov ecx,[0x72e1e0]; push [ebp+8]; call 0x416a1a; jmp 0x416a14`。          */
/*   0x416a1a 自己会 +0x14 选表、拿栈上那个 id、往 esi 指的字符串里写 ——       */
/*   这几样在 0x4169e9 的栈帧里原样都在（esi 是调用方给的出参，0x4169e9 没动   */
/*   它；ebp 还是它自己的帧），回来 `ret 4` 把 id 弹掉，栈正好平。             */
/*   商店货架 / 浮窗（0x45bd5f 等）也走 0x4169e9，它们查的本来就命中，不受影响。 */
/*                                                                            */
/*   设 BSHOOK_KEEP_GIFT_ICON_MISS=1 保留原版行为（对照用）。                 */
/* -------------------------------------------------------------------------- */
#define ICON_LOOKUP_VA          0x004169E9u
#define ICON_LOOKUP_MISS_VA     0x004169FFu   /* push offset L"" —— 换成 jmp */
#define ICON_LOOKUP_SIG_LEN     47
static const unsigned char ICON_LOOKUP_SIG[ICON_LOOKUP_SIG_LEN] = {
    0x55, 0x8B, 0xEC, 0x51,                   /* push ebp; mov ebp,esp; push ecx */
    0x83, 0x65, 0xFC, 0x00,                   /* and [ebp-4], 0                  */
    0x8D, 0x45, 0x08,                         /* lea eax, [ebp+8]  (&itemId)     */
    0xE8, 0xC6, 0xED, 0xFF, 0xFF,             /* call 0x4157bf     (map find)    */
    0x85, 0xC0,                               /* test eax, eax                   */
    0x8B, 0xCE,                               /* mov ecx, esi                    */
    0x75, 0x0C,                               /* jne 0x416a0b                    */
    0x68, 0xFC, 0xDA, 0x65, 0x00,             /* push offset L""   ← 补丁点      */
    0xE8, 0x70, 0xAF, 0xFE, 0xFF,             /* call 0x401979     (string ctor) */
    0xEB, 0x09,                               /* jmp 0x416a14                    */
    0x83, 0xC0, 0x08, 0x50,                   /* add eax, 8; push eax            */
    0xE8, 0xAA, 0xC2, 0xFE, 0xFF,             /* call 0x402cbe     (copy ctor)   */
    0x8B, 0xC6, 0xC9, 0xC2                    /* mov eax, esi; leave; ret 4      */
};

/* detour 里的立即数不带后缀（MSVC 内联汇编不吃 0x…u） */
#define ICON_LOOKUP_TABLE_PTR   0x0072E1E0    /* [它] = 那个两张表的对象           */
#define ICON_LOOKUP_MAP2_FN     0x00416A1A    /* 查第二张表（[Item-N]）的函数      */
#define ICON_LOOKUP_EXIT        0x00416A14    /* 函数尾：mov eax,esi; leave; ret 4 */

static __declspec(naked) void icon_lookup_fallback_detour(void)
{
    __asm {
        mov  ecx, ICON_LOOKUP_TABLE_PTR
        mov  ecx, dword ptr [ecx]           /* 两张表的宿主对象（调用方就是这么传的） */
        push dword ptr [ebp + 8]            /* itemId（0x4169e9 自己的实参）         */
        mov  eax, ICON_LOOKUP_MAP2_FN
        call eax                            /* 0x416a1a：查 [Item-N] 那张，写进 esi */
        push ICON_LOOKUP_EXIT
        ret
    }
}

static volatile LONG g_icon_lookup_patched = 0;

static int gift_icon_miss_keep_original(void)
{
    char buf[8];
    DWORD n = GetEnvironmentVariableA("BSHOOK_KEEP_GIFT_ICON_MISS", buf, sizeof(buf));
    return n > 0 && n < sizeof(buf) && buf[0] != '0';
}

static int try_patch_gift_icon_fallback(void)
{
    unsigned char *fn = (unsigned char *)ICON_LOOKUP_VA;
    unsigned char *p = (unsigned char *)ICON_LOOKUP_MISS_VA;
    DWORD oldp;

    if (g_icon_lookup_patched) return 1;
    if (IsBadReadPtr(fn, ICON_LOOKUP_SIG_LEN)) return 0;
    /* 幂等：已打过就是「E9 <跳到我们 detour 的 rel32>」 */
    if (p[0] == 0xE9
        && (DWORD)(*(int *)(p + 1))
               == (DWORD)((UINT_PTR)&icon_lookup_fallback_detour
                          - (UINT_PTR)(p + 5))) {
        InterlockedExchange(&g_icon_lookup_patched, 1);
        return 1;
    }
    if (memcmp(fn, ICON_LOOKUP_SIG, ICON_LOOKUP_SIG_LEN) != 0)
        return 0;                          /* 还没解壳到这里，或不是已确认的版本 */

    if (!VirtualProtect(p, 5, PAGE_EXECUTE_READWRITE, &oldp)) {
        bslog("PATCH   礼物盒图标退回物品表: VirtualProtect 失败 err=%lu",
              (unsigned long)GetLastError());
        return 0;
    }
    p[0] = 0xE9;
    *(DWORD *)(p + 1) = (DWORD)((UINT_PTR)&icon_lookup_fallback_detour
                                - (UINT_PTR)(p + 5));
    VirtualProtect(p, 5, oldp, &oldp);
    FlushInstructionCache(GetCurrentProcess(), p, 5);
    InterlockedExchange(&g_icon_lookup_patched, 1);
    bslog("PATCH   ★礼物盒图标退回物品表 @ %08X: 图标按 id 在商店货那张表查不到时"
          "改查 [Item-] 那张（材料 / 卡片的图标在那儿），原版回空串画成 (FileNotFound)",
          (unsigned)ICON_LOOKUP_MISS_VA);
    return 1;
}

/* -------------------------------------------------------------------------- */
/* ★ 「突击技加成」提示的空指针 —— 和上面那个**完全同型**（V0.3 §53）        */
/*                                                                            */
/*   2026-09-09 顺着「把 13 种加成全写进说明文」那一轮扫出来的：              */
/*   `DashDamage` 有自己的一对虚表槽（0x66d5dc：+0x128 = 0x481dfd 置位、      */
/*   +0x12c = 0x481e73 绘制）。0x481dfd 那一支在 `GetEquipBonus(射手, 12 =    */
/*   DashAttack)` > 0 时把伤害 ×(1+x/100) 并置 flags 0x400（**没有概率门**）， */
/*   全 `EquipBonus-Chn.ini` 只有 `220004` 迷你机械青蛙带 DashAttack ——       */
/*   而它**已经上架、可合成**。                                               */
/*                                                                            */
/*   绘制那一支和 0x47e778 一模一样，而且**连头射那道判空都没有**：           */
/*                                                                            */
/*     00481e92  test byte [ebp+0xd], 4        ; flags & 0x400 ?               */
/*     00481e96  je 0x481fe1                   ; 函数尾（没加成就不画）        */
/*     00481e9c  [[0x72e320]] == 2 ?           ; 中国区 -> 0x481ef3            */
/*     00481ea7  韩国区：文字画在爆炸点，不碰目标 —— 没事                       */
/*     00481ef3  中国区：取图 Images/Chinese/Img…（有目标才有地方挂）         */
/*     00481f3d  mov eax, [edi] ; call [eax+8] ; ★★ edi = NULL -> 和 0x47ea6c  */
/*                                               同一个形状                    */
/*                                                                            */
/*   ⚠ **可达性没查实**：`DashDamage` 会不会真以「目标 = 0」进 0x492715       */
/*   `OnExplode`（句柄表 0x474225 里登不登记）没查证 —— 所以这是**兜底**，     */
/*   不是复现过的 bug。代价 6 字节，和 splash 那处一样，按 D55 同一套做法。    */
/*                                                                            */
/*   ★ 和 splash 的三处差别：① 补丁点头一条是 `push ebx`（**调用实参**，不是  */
/*   保存寄存器 —— 这个函数的尾巴 0x481fe1 只 `pop edi; pop esi`），所以要盖  */
/*   **6** 字节（E9 rel32 + 一个 NOP）、detour 里三条原指令都要补回；          */
/*   ② 回跳点 0x481EF9；③ 无目标时的出口是 0x481FE1（= 0x481e96 那条 je 去的  */
/*   同一个地方，中间一个 push 都没有、SEH 状态也没变）。                      */
/*                                                                            */
/*   设 BSHOOK_KEEP_DASH_VISUAL_CRASH=1 保留原版行为（对照用）。              */
/* -------------------------------------------------------------------------- */
#define DASH_VISUAL_VA        0x00481EF3u
#define DASH_VISUAL_PATCH_LEN 6             /* E9 rel32 + NOP，凑够指令边界   */
#define DASH_VISUAL_SIG_LEN   18
static const unsigned char DASH_VISUAL_SIG[DASH_VISUAL_SIG_LEN] = {
    0x53,                               /* push ebx                          */
    0x6A, 0x07,                         /* push 7                            */
    0x8D, 0x45, 0x0C,                   /* lea  eax, [ebp+0xC]               */
    0x50,                               /* push eax                          */
    0xFF, 0x35, 0xA0, 0x9A, 0x6E, 0x00, /* push [0x6e9aa0]                   */
    0xE8, 0xBB, 0xE0, 0xFF, 0xFF        /* call 0x47ffc0（取第 7 号图片名）  */
};

/* detour 里要用的立即数不带后缀（MSVC 内联汇编不吃 0x…u 这种写法） */
#define DASH_VISUAL_RETURN_TO 0x00481EF9   /* 补回三条原指令后接着跑          */
#define DASH_VISUAL_EXIT      0x00481FE1   /* 函数尾：mov ecx,[ebp-0xC] … ret 0xC */

static __declspec(naked) void dash_visual_guard_detour(void)
{
    __asm {
        test edi, edi                       /* edi = 命中的角色指针（arg0） */
        jz   dvg_skip
        push ebx                            /* 被偷走的三条原指令，逐条补回 */
        push 7
        lea  eax, [ebp + 0xC]
        push DASH_VISUAL_RETURN_TO
        ret
    dvg_skip:
        push DASH_VISUAL_EXIT               /* 没有目标：不画，走原有出口 */
        ret
    }
}

static volatile LONG g_dash_visual_patched = 0;

static int dash_visual_crash_keep_original(void)
{
    /* 语义和 splash_visual_crash_keep_original() 一样：设了才是「保留原版」。 */
    char buf[8];
    DWORD n = GetEnvironmentVariableA("BSHOOK_KEEP_DASH_VISUAL_CRASH", buf, sizeof(buf));
    return n > 0 && n < sizeof(buf) && buf[0] != '0';
}

static int try_patch_dash_visual_guard(void)
{
    unsigned char *p = (unsigned char *)DASH_VISUAL_VA;
    DWORD oldp;

    if (g_dash_visual_patched) return 1;
    if (IsBadReadPtr(p, DASH_VISUAL_SIG_LEN)) return 0;
    /* 幂等：已打过就是「E9 <跳到我们 detour 的 rel32>」 */
    if (p[0] == 0xE9
        && (DWORD)(*(int *)(p + 1))
               == (DWORD)((UINT_PTR)&dash_visual_guard_detour
                          - (UINT_PTR)(p + 5))) {
        InterlockedExchange(&g_dash_visual_patched, 1);
        return 1;
    }
    if (memcmp(p, DASH_VISUAL_SIG, DASH_VISUAL_SIG_LEN) != 0)
        return 0;                          /* 还没解壳到这里，或不是已确认的版本 */

    if (!VirtualProtect(p, DASH_VISUAL_PATCH_LEN, PAGE_EXECUTE_READWRITE, &oldp)) {
        bslog("PATCH   突击技加成提示判空: VirtualProtect 失败 err=%lu",
              (unsigned long)GetLastError());
        return 0;
    }
    p[0] = 0xE9;
    *(DWORD *)(p + 1) = (DWORD)((UINT_PTR)&dash_visual_guard_detour
                                - (UINT_PTR)(p + 5));
    p[5] = 0x90;                           /* 第 6 字节补 NOP，别留半条指令   */
    VirtualProtect(p, DASH_VISUAL_PATCH_LEN, oldp, &oldp);
    FlushInstructionCache(GetCurrentProcess(), p, DASH_VISUAL_PATCH_LEN);
    InterlockedExchange(&g_dash_visual_patched, 1);
    bslog("PATCH   ★突击技加成提示判空 @ %08X: rpExplode 目标句柄查不到（打空）"
          "且 flags 带 0x400 时不再读空指针（原版 0x481f3d 和 0x47ea6c 同型）",
          (unsigned)DASH_VISUAL_VA);
    return 1;
}

/* -------------------------------------------------------------------------- */
/* ★ 关掉客户端自己画的那行**绿色加成文字**（V0.3 合成与商店 §59 / D65）      */
/*                                                                            */
/*   原版在仓库 / 合成提示框的 `ItemInfo2Txt` 里画一行亮绿色（0xFF22C701）的   */
/*   加成摘要，内容是客户端**自己**按 itemId 查本地 `EquipBonus-Chn.ini` 生成的 */
/*   （0x414128 -> 0x4136af）。它和服务端下发的说明文（`ItemInfo+0x18`）**重复**，*/
/*   而且原版这一行有三个毛病，全在客户端 pak 里、服务端一个字都改不了：       */
/*                                                                            */
/*     1. `Chinese.ini` 错字：`Spd%+d%%=速度+d%%`（漏了 d 前面的 %）           */
/*        -> 画出来是「速度+d%」；                                            */
/*     2. `Team %d%% %s` / `Self %d%% %s` / `Down` 三条**漏翻**               */
/*        -> 称号上画出来是「Team 5% Down」这种英文；                          */
/*     3. `ItemInfo2Txt` 在 .ui 里只有 240x14（**一行**），属性一多就换行溢出   */
/*        到下面那个框上。                                                    */
/*                                                                            */
/*   我们发的说明文是它的**严格超集**（13 种加成 vs 它的 7 种，外加武器数值、   */
/*   条件加成、exe 里写死的特效），且三个提示框都有 —— 原版商店提示框压根不画   */
/*   加成。所以用户 2026-09-09 拍板：**关掉绿字，白字统一管**。               */
/*                                                                            */
/*   ★ 打在**生成器**上，不是四个绘制点上：                                   */
/*                                                                            */
/*     0041414D  mov  eax, 0x72e440        ; EquipBonus 显示表                */
/*     00414155  call 0x417a76             ; map.find(itemId)                 */
/*     0041415A  mov  esi, [eax]                                              */
/*     0041415C  test esi, esi                                                */
/*     0041415E  je   0x414180             ; ← 查不到就跳过，vector 保持空     */
/*                                                                            */
/*   把这个 `je`（74）改成 `jmp`（EB）**一个字节**，`0x414128` 就永远返回空表， */
/*   四个消费点（仓库提示框 0x455628、合成提示框 0x460059、修理界面           */
/*   0x450614 / 0x45078D）一起哑火。                                          */
/*                                                                            */
/*   ★★ 为什么这一刀几乎零风险：走的就是**原版每天都在走**的那条路 ——         */
/*   武器 / 材料 / 纯外观装备在 `EquipBonus-Chn.ini` 里都没有条目，本来就走     */
/*   `je` 这一支。SEH 状态（`[ebp-4]`）在两条路上完全一样，vector 的构造 /      */
/*   拷贝 / 析构一步不少。                                                    */
/*                                                                            */
/*   设 BSHOOK_KEEP_CLIENT_BONUS_TEXT=1 保留原版那行绿字（对照用）。          */
/* -------------------------------------------------------------------------- */
/*   ★★ **有两个生成器，两个都要堵**（2026-09-09 实机漏了一个）：            */
/*                                                                            */
/*     0x414128(out, itemId)        单件  -> 仓库提示框 / 合成提示框 / 修理界面 */
/*     0x4141AC(out, id列表)        累加  -> ★ **商店提示框**（礼包/套装那种   */
/*                                   「买了会得到这些」的合计加成）             */
/*                                                                            */
/*   两个的循环体一模一样：`map.find(itemId)` -> `test esi/ecx` -> `je 跳过`。 */
/*   前者跳过就返回空表；后者跳过就不往累加器里加，累加器保持全 0，而          */
/*   `0x4136AF` 对**每一项都判零**（`0x41379D` / `0x413A7F` 两条分支都判），    */
/*   全 0 时一条都不输出 ⇒ 同样是空表。                                        */
#define BONUS_TEXT_SITE_COUNT 2
#define BONUS_TEXT_SIG_LEN    19
#define BONUS_TEXT_JE_OFF     17          /* 那个 `74 xx` 在特征串里的位置    */

static const struct {
    UINT_PTR va;                          /* 特征串起点                       */
    unsigned char sig[BONUS_TEXT_SIG_LEN];
    const char *what;
} BONUS_TEXT_SITES[BONUS_TEXT_SITE_COUNT] = {
    { 0x0041414Du, {                      /* 0x414128 里：单件               */
        0xB8, 0x40, 0xE4, 0x72, 0x00,     /* mov  eax, 0x72e440              */
        0x89, 0x5D, 0xFC,                 /* mov  [ebp-4], ebx               */
        0xE8, 0x1C, 0x39, 0x00, 0x00,     /* call 0x417a76（map.find）       */
        0x8B, 0x30,                       /* mov  esi, [eax]                 */
        0x85, 0xF6,                       /* test esi, esi                   */
        0x74, 0x20                        /* je 0x414180  ★ 改这个           */
      }, "仓库 / 合成提示框 / 修理界面" },
    { 0x004141F7u, {                      /* 0x4141AC 里：累加               */
        0xB8, 0x40, 0xE4, 0x72, 0x00,     /* mov  eax, 0x72e440              */
        0x83, 0xC3, 0x04,                 /* add  ebx, 4                     */
        0xE8, 0x72, 0x38, 0x00, 0x00,     /* call 0x417a76（map.find）       */
        0x8B, 0x08,                       /* mov  ecx, [eax]                 */
        0x85, 0xC9,                       /* test ecx, ecx                   */
        0x74, 0x07                        /* je 0x414211  ★ 改这个           */
      }, "商店提示框" },
};

static volatile LONG g_bonus_text_patched = 0;

static int client_bonus_text_keep_original(void)
{
    char buf[8];
    DWORD n = GetEnvironmentVariableA("BSHOOK_KEEP_CLIENT_BONUS_TEXT", buf, sizeof(buf));
    return n > 0 && n < sizeof(buf) && buf[0] != '0';
}

static int try_patch_hide_client_bonus_text(void)
{
    int i, done = 0;

    if (g_bonus_text_patched) return 1;

    for (i = 0; i < BONUS_TEXT_SITE_COUNT; i++) {
        unsigned char *p = (unsigned char *)BONUS_TEXT_SITES[i].va;
        unsigned char *je = p + BONUS_TEXT_JE_OFF;
        const unsigned char *sig = BONUS_TEXT_SITES[i].sig;
        DWORD oldp;

        if (IsBadReadPtr(p, BONUS_TEXT_SIG_LEN)) return 0;
        /* 幂等：已经改成 EB 了就算数（前 17 字节仍要对得上）。 */
        if (je[0] == 0xEB && je[1] == sig[BONUS_TEXT_JE_OFF + 1]
            && memcmp(p, sig, BONUS_TEXT_JE_OFF) == 0) {
            done++;
            continue;
        }
        if (memcmp(p, sig, BONUS_TEXT_SIG_LEN) != 0)
            return 0;                      /* 还没解壳到这里，或不是已确认的版本 */

        if (!VirtualProtect(je, 1, PAGE_EXECUTE_READWRITE, &oldp)) {
            bslog("PATCH   客户端加成绿字: VirtualProtect 失败 err=%lu",
                  (unsigned long)GetLastError());
            return 0;
        }
        je[0] = 0xEB;                      /* je -> jmp，永远跳过 */
        VirtualProtect(je, 1, oldp, &oldp);
        FlushInstructionCache(GetCurrentProcess(), je, 1);
        bslog("PATCH   ★客户端加成绿字已关 @ %08X（%s）",
              (unsigned)(BONUS_TEXT_SITES[i].va + BONUS_TEXT_JE_OFF),
              BONUS_TEXT_SITES[i].what);
        done++;
    }

    if (done != BONUS_TEXT_SITE_COUNT) return 0;
    InterlockedExchange(&g_bonus_text_patched, 1);
    return 1;
}

/* -------------------------------------------------------------------------- */
/* ★★ 岩浆巨龙（Quest02 / 드라카）**永远无敌卡关**                            */
/*                                                                            */
/*   用户 2026-09-10 报的线上现象：闯关「岩浆巨龙」进 boss 房后，**有时**       */
/*   boss 一直无敌、子弹全弹开，那段「布洛克分析出弱点」的剧情不放，关卡        */
/*   再也推不下去。服务端日志（bug调查/16）：当天进 `Quest02_2` 的五局里，      */
/*   四局都在进图 17~22 秒后开始出 `0x0410`（只在打到怪身上才发），出事那局     */
/*   3 分 12 秒**一发都没有** ⇒ 全程零伤害。服务端在 boss 房里既没收也没发     */
/*   别的包 ⇒ 病在客户端。                                                    */
/*                                                                            */
/*   ## 病根（静态逆向，addr 均为 BigShot 内存镜像 VA；会话 27 逐条复核过）     */
/*                                                                            */
/*   boss 的**阶段**在 `[boss+0x2a8]`：构造函数 `0x4b1812` 把它置 **-1** 并调   */
/*   `vf_124(-1)`（所有部件标成「弹开」= 免疫），随后 `0x4b1837` 把状态名        */
/*   `[boss+0x2b0]` 置 `"birth"`。把 -1 抬成 0（= 可以打）的**全客户端只有一处**：*/
/*   任务脚本命令 `$공략포인트`（处理器 `0x4a76ad`，写在 `0x4a76c9`：           */
/*   `[boss+0x2a8]=0` + `vf_124(0)`），而它**只出现在                          */
/*   `Data/Quest/Quest02/Quest02Tracing`** 这一份剧情里。                      */
/*                                                                            */
/*   放那份剧情的是关卡舞台的检查 `0x4a706b`（舞台 vf_80 = `0x4a74f5` 在关卡    */
/*   状态 `[+0x3b0]==2` 时调它），四道门缺一不可：                              */
/*                                                                            */
/*     ① `[stage+0x560] != 0`             boss 已经生成                       */
/*     ② `[0x72e260]->[+0x14] == 0`       **当前没有剧情在播**                 */
/*     ③ `0x4e71c0(...) == 3`             剧情播放器状态                       */
/*     ④ `[stage+0x5fc] == 0` 且 `boss->vf_154(4)`                            */
/*                                                                            */
/*   而 Quest02 那只 boss 的 `vf_154(4)`（`0x4b5a04`）判的是                    */
/*   **`[boss+0x2b0] == L"birth"`** —— 一个只活 `BirthTick`(=120) 个游戏刻的   */
/*   **瞬时状态**。刻长 `[0x6dc528]` = 32 ms（和服务端的 32ms 循环同源），      */
/*   120 刻 = **3.84 秒**；刻数追墙钟（`0x42b4c3`，落后就在同一帧里无上限补跑）。*/
/*   出生一结束，`vf_140("think")` 在 `0x4b1c54` 看见阶段还是 -1，把状态切成    */
/*   `"idle1"` —— 从此 `vf_154(4)` 恒假，**Tracing 永远不会再放，阶段永远 -1**。 */
/*                                                                            */
/*   ★ 「有时」到底是谁吃掉了那 3.84 秒，**没有查清**（会话 27）。两条看上去    */
/*   成立的解释已被证伪：                                                      */
/*     · 不是 `Quest02Boss` 的尾巴。`$createboss` 后面那几行 `fadeout 3000` /    */
/*       `clear` / `fadein 3000` / `wideoff` 的 `Wait=` 都是空的，脚本引擎里     */
/*       `fadeout` / `fadein`（`0x4e6507` / `0x4e653d`）只是把参数交给画面管理器 */
/*       `[0x72e2d8]` 就返回，**不等**；剧情几帧内就结束。真要阻塞 6 秒，窗口   */
/*       会**每次**错过，和四局成功矛盾。                                       */
/*     · 不是 `Quest02Lava` 插队。全代码字符串和地图文件里都没有引用它；       */
/*       Quest02 舞台代码只放 Tracing / Shell / Change / Phase2 / Phase3 /       */
/*       Clear / Intro，`Quest02Boss` 由地图当开场脚本引用。                    */
/*   一个还没证实的候选：`$createboss` 同步加载 boss 模型 / 特效造成卡帧，      */
/*   追赶循环一帧内补跑 ≥120 刻，出生在检查跑到之前就过期了。下面的 DRAKA       */
/*   诊断日志就是为了下次出事时把这一点钉死。                                   */
/*                                                                            */
/*   ★ 别的关卡：Quest01 判的是 `vf_154(0)` = `[boss+0x2f0] > 5`（计数，不过期）；*/
/*     Quest03 的检查 `0x4a7ee0` 不问 boss 状态；Quest05 / Quest06 的            */
/*     `$공략포인트` 在 `Phase2` 里（判 `阶段==1`，持久事实）；Quest01 / Quest04  */
/*     的 boss 没有 `$createboss`。Quest07 的 Map02 / Map03 是 `$createboss` 和   */
/*     `$공략포인트` 同一份脚本挨着写；**Map01 不是**（`Map01Intro` 末尾         */
/*     `$createboss`，`$공략포인트` 在 `Map01Phase2`），那只 boss 是否同形没验。  */
/*                                                                            */
/*   ## 改法：把判据从**瞬时状态**换成**持久事实**（铁律 10 的口径）           */
/*                                                                            */
/*   真正区分「该放 Tracing」和「不该放」的事实是「**弱点还没被点出来**」，     */
/*   也就是 `[boss+0x2a8] == -1`，不是「此刻正在播出生动画」。20 字节原地改：   */
/*                                                                            */
/*     0x4b5a04  push "birth" / lea ecx,[esi+0x2b0] / call 0x4040f5            */
/*               / test eax,eax / jne 0x4b5a00        （20 字节）              */
/*        ->     cmp dword [esi+0x2a8], -1 / je 0x4b5a00 / nop×11              */
/*                                                                            */
/*   新判据是旧判据的严格超集（出生期间阶段本来就是 -1），正常那一路一帧不差；  */
/*   错过窗口的那一路，门②③一开就补放 Tracing，boss 晚几秒变成可打，不再永久   */
/*   卡关。放几次由舞台自己的一次性标志 `[stage+0x5fc]` 管，不动。它对「窗口   */
/*   被谁吃掉」不敏感，所以触发条件没查清也照样有效；但如果线上失败其实是      */
/*   「Tracing 放了 boss 仍无敌」这一类，它就不管用 —— 靠 DRAKA 日志分辨。       */
/*                                                                            */
/*   `vf_154(4)` 的其余调用点（0x4a8a2c / 0x4a9571 / 0x4aa187 / 0x4f71bb）都是  */
/*   别的关卡查自己的 boss 类，不经过这 20 字节。                               */
/*                                                                            */
/*   设 BSHOOK_KEEP_DRAKA_TRACING_RACE=1 保留原版行为（复现 / 对照用）。       */
/* -------------------------------------------------------------------------- */
#define DRAKA_TRACING_VA      0x004B5A04u
#define DRAKA_TRACING_LEN     20

/* 原版：push offset "birth"; lea ecx,[esi+0x2B0]; call 0x4040F5;
         test eax,eax; jne 0x4B5A00                                          */
static const unsigned char DRAKA_TRACING_SIG[DRAKA_TRACING_LEN] = {
    0x68, 0xAC, 0x92, 0x67, 0x00,
    0x8D, 0x8E, 0xB0, 0x02, 0x00, 0x00,
    0xE8, 0xE1, 0xE6, 0xF4, 0xFF,
    0x85, 0xC0,
    0x75, 0xE8
};

/* 修好：cmp dword ptr [esi+0x2A8], -1; je 0x4B5A00; 剩下补 NOP 落到
         0x4B5A18 的 `xor al,al`（= 返回假），长度一字节不差。               */
static const unsigned char DRAKA_TRACING_FIX[DRAKA_TRACING_LEN] = {
    0x83, 0xBE, 0xA8, 0x02, 0x00, 0x00, 0xFF,
    0x74, 0xF3,
    0x90, 0x90, 0x90, 0x90, 0x90, 0x90, 0x90, 0x90, 0x90, 0x90, 0x90
};

static volatile LONG g_draka_tracing_patched = 0;

static int draka_tracing_keep_original(void)
{
    char buf[8];
    DWORD n = GetEnvironmentVariableA("BSHOOK_KEEP_DRAKA_TRACING_RACE",
                                      buf, sizeof(buf));
    return n > 0 && n < sizeof(buf) && buf[0] != '0';
}

static int try_patch_draka_tracing_gate(void)
{
    unsigned char *p = (unsigned char *)DRAKA_TRACING_VA;
    DWORD oldp;

    if (g_draka_tracing_patched) return 1;
    if (IsBadReadPtr(p, DRAKA_TRACING_LEN)) return 0;
    /* 幂等：已经是修好的那 20 字节就算数。 */
    if (memcmp(p, DRAKA_TRACING_FIX, DRAKA_TRACING_LEN) == 0) {
        InterlockedExchange(&g_draka_tracing_patched, 1);
        return 1;
    }
    if (memcmp(p, DRAKA_TRACING_SIG, DRAKA_TRACING_LEN) != 0)
        return 0;                      /* 还没解壳到这里，或不是已确认的版本 */

    if (!VirtualProtect(p, DRAKA_TRACING_LEN, PAGE_EXECUTE_READWRITE, &oldp)) {
        bslog("PATCH   岩浆巨龙弱点剧情: VirtualProtect 失败 err=%lu",
              (unsigned long)GetLastError());
        return 0;
    }
    memcpy(p, DRAKA_TRACING_FIX, DRAKA_TRACING_LEN);
    VirtualProtect(p, DRAKA_TRACING_LEN, oldp, &oldp);
    FlushInstructionCache(GetCurrentProcess(), p, DRAKA_TRACING_LEN);
    InterlockedExchange(&g_draka_tracing_patched, 1);
    bslog("PATCH   ★岩浆巨龙弱点剧情 @ %08X: 判据由「boss 正在放出生动画」"
          "改成「弱点还没点出来（阶段 == -1）」—— 错过那 3.84 秒出生窗口时"
          "不再永久卡关",
          (unsigned)DRAKA_TRACING_VA);
    return 1;
}

/* -------------------------------------------------------------------------- */
/* ★ 岩浆巨龙诊断日志（DRAKA，会话 27）                                        */
/*                                                                            */
/*   目的：下次再卡关时能分辨「Tracing 没放」还是「放了但弱点没点出来」，      */
/*   并量出出生窗口被吃掉了多少。服务端日志分不清这两种，只有客户端能。       */
/*                                                                            */
/*   四个中途内联 hook，每个都只偷**一整条**指令（不存在跳进被偷区中间的      */
/*   问题），一局只打一两行，走 bslog（任何日志级别都记）：                    */
/*                                                                            */
/*     0x4b1812  boss 构造函数 `or [edi+0x2a8],-1`（edi=boss）→ 记创建刻      */
/*     0x4a70b4  舞台检查过了门①②③、Tracing 未放 `mov ecx,[edi+0x560]`        */
/*               （edi=stage）→ 打「门开」那一刻 boss 的状态名 / 阶段 /          */
/*               距创建多少刻。按**状态名翻转**去重（铁律 10）。               */
/*     0x4a712d  真的去排 Tracing `push offset "…Quest02Tracing"`（stage 在      */
/*               [ebp+8]，edi 已被 movsd 用掉）→ 打「Tracing 入队」             */
/*     0x4a76c9  `$공략포인트` 处理器 `mov [ecx+0x2a8],esi`（ecx=boss）→ 打     */
/*               「弱点点出」                                                   */
/*                                                                            */
/*   判读：门开那行若 `状态=idle1`，就是出生窗口在检查跑到之前已过期            */
/*   （原版会永久卡关，补丁会接着放 Tracing）；有 Tracing 入队却没有弱点点出，   */
/*   病就在别处。`BSHOOK_DRAKA_DIAG=0` 可关。                                   */
/* -------------------------------------------------------------------------- */
#define DRAKA_CTOR_VA     0x004B1812u   /* or dword ptr [edi+0x2a8], -1 ; push -1 */
static const unsigned char DRAKA_CTOR_SIG[]  = { 0x83, 0x8F, 0xA8, 0x02, 0x00, 0x00, 0xFF, 0x6A, 0xFF };
#define DRAKA_GATE_VA     0x004A70B4u   /* mov ecx,[edi+0x560]; mov eax,[ecx]; push 4 */
static const unsigned char DRAKA_GATE_SIG[]  = { 0x8B, 0x8F, 0x60, 0x05, 0x00, 0x00, 0x8B, 0x01, 0x6A, 0x04 };
#define DRAKA_QUEUE_VA    0x004A712Du   /* push offset "Data/Quest/Quest02/Quest02Tracing"; call */
static const unsigned char DRAKA_QUEUE_SIG[] = { 0x68, 0xA8, 0x4D, 0x67, 0x00, 0xE8 };
#define DRAKA_POINT_VA    0x004A76C9u   /* mov [ecx+0x2a8], esi; mov eax,[eax] */
static const unsigned char DRAKA_POINT_SIG[] = { 0x89, 0xB1, 0xA8, 0x02, 0x00, 0x00, 0x8B, 0x00 };
#define DRAKA_CTX_PP      0x0072E2B4u   /* [[0x72e2b4]+8] = GameContext，[+0xd4] = 游戏刻（0x409f0e） */

static void *g_draka_ctor_tramp  = NULL;
static void *g_draka_gate_tramp  = NULL;
static void *g_draka_queue_tramp = NULL;
static void *g_draka_point_tramp = NULL;
static volatile LONG g_draka_diag_patched = 0;

static void *g_draka_boss = NULL;        /* 最近创建的那只 boss */
static int   g_draka_birth_tick = 0;     /* 它创建时的游戏刻 */
static char  g_draka_gate_last[32];      /* 门开时上一次打过的状态名（翻转去重） */

static int draka_diag_enabled(void)
{
    char buf[8];
    DWORD n = GetEnvironmentVariableA("BSHOOK_DRAKA_DIAG", buf, sizeof(buf));
    if (n == 0 || n >= sizeof(buf)) return 1;   /* 没设 = 开：一局才几行 */
    return buf[0] != '0';
}

static int draka_tick(void)
{
    unsigned char *root, *ctx;
    if (IsBadReadPtr((void *)DRAKA_CTX_PP, 4)) return -1;
    root = *(unsigned char **)DRAKA_CTX_PP;
    if (!root || IsBadReadPtr(root + 8, 4)) return -1;
    ctx = *(unsigned char **)(root + 8);
    if (!ctx || IsBadReadPtr(ctx + 0xD4, 4)) return -1;
    return *(int *)(ctx + 0xD4);
}

/* [boss+0x2b0] 是宽字符串对象，头 4 字节就是字符缓冲区指针（0x4040f5 就这么读）。 */
static const char *draka_state_name(unsigned char *boss, char *out, int outsz)
{
    const wchar_t *ws;
    out[0] = 0;
    if (!boss || IsBadReadPtr(boss + 0x2B0, 4)) return "?";
    ws = *(const wchar_t **)(boss + 0x2B0);
    if (!ws || IsBadReadPtr(ws, 2)) return "?";
    w2u8(ws, out, outsz);
    return out;
}

static int draka_phase(unsigned char *boss)
{
    if (!boss || IsBadReadPtr(boss + 0x2A8, 4)) return -999;
    return *(int *)(boss + 0x2A8);
}

static int draka_since_birth(unsigned char *boss, int tick)
{
    return (boss && boss == g_draka_boss && tick >= 0) ? tick - g_draka_birth_tick : -1;
}

static void __cdecl draka_ctor_log(void *obj)
{
    g_draka_boss = obj;
    g_draka_birth_tick = draka_tick();
    g_draka_gate_last[0] = 0;
    bslog("DRAKA   boss 创建 @%08X 刻=%d（阶段 -1 = 石壳免疫；状态 birth 只活 120 刻 = 3.84 s）",
          (unsigned)(UINT_PTR)obj, g_draka_birth_tick);
}

static void __cdecl draka_gate_log(void *stage)
{
    unsigned char *s = (unsigned char *)stage;
    unsigned char *boss;
    char name[32];
    int tick;
    if (!s || IsBadReadPtr(s + 0x560, 4)) return;
    boss = *(unsigned char **)(s + 0x560);
    draka_state_name(boss, name, sizeof(name));
    if (strcmp(name, g_draka_gate_last) == 0) return;      /* 状态没翻转就不重复 */
    lstrcpynA(g_draka_gate_last, name, sizeof(g_draka_gate_last));
    tick = draka_tick();
    bslog("DRAKA   门开（无剧情在播、Tracing 未放）刻=%d 距创建 %d 刻  boss 状态=%s 阶段=%d%s",
          tick, draka_since_birth(boss, tick), name, draka_phase(boss),
          (strcmp(name, "birth") == 0) ? "" : "  ← 出生窗口已过，原版到此永久卡关");
}

static void __cdecl draka_queue_log(void *stage)
{
    unsigned char *s = (unsigned char *)stage;
    unsigned char *boss = NULL;
    char name[32];
    int tick = draka_tick();
    if (s && !IsBadReadPtr(s + 0x560, 4)) boss = *(unsigned char **)(s + 0x560);
    bslog("DRAKA   Tracing 入队 刻=%d 距创建 %d 刻  boss 状态=%s 阶段=%d",
          tick, draka_since_birth(boss, tick),
          draka_state_name(boss, name, sizeof(name)), draka_phase(boss));
}

static void __cdecl draka_point_log(void *obj)
{
    unsigned char *boss = (unsigned char *)obj;
    char name[32];
    int tick = draka_tick();
    bslog("DRAKA   $공략포인트：阶段 %d→0，弱点点出 刻=%d 距创建 %d 刻  boss 状态=%s",
          draka_phase(boss), tick, draka_since_birth(boss, tick),
          draka_state_name(boss, name, sizeof(name)));
}

/* 0x4b1812：this 在 edi。 */
static __declspec(naked) void draka_ctor_detour(void)
{
    __asm {
        pushad
        pushfd
        push edi
        call draka_ctor_log
        add  esp, 4
        popfd
        popad
        jmp  dword ptr [g_draka_ctor_tramp]
    }
}

/* 0x4a70b4：stage 在 edi。 */
static __declspec(naked) void draka_gate_detour(void)
{
    __asm {
        pushad
        pushfd
        push edi
        call draka_gate_log
        add  esp, 4
        popfd
        popad
        jmp  dword ptr [g_draka_gate_tramp]
    }
}

/* 0x4a712d：edi 已被 movsd 用掉，stage 是函数参数 [ebp+8]（0x4a7153 就这么取）。 */
static __declspec(naked) void draka_queue_detour(void)
{
    __asm {
        pushad
        pushfd
        push dword ptr [ebp+8]
        call draka_queue_log
        add  esp, 4
        popfd
        popad
        jmp  dword ptr [g_draka_queue_tramp]
    }
}

/* 0x4a76c9：boss 在 ecx。 */
static __declspec(naked) void draka_point_detour(void)
{
    __asm {
        pushad
        pushfd
        push ecx
        call draka_point_log
        add  esp, 4
        popfd
        popad
        jmp  dword ptr [g_draka_point_tramp]
    }
}

static int draka_diag_install_one(unsigned int va, const unsigned char *sig, int sig_len,
                                  void *detour, void **tramp, const char *name)
{
    unsigned char *p = (unsigned char *)va;
    if (*tramp) return 1;
    if (IsBadReadPtr(p, sig_len)) return 0;
    if (memcmp(p, sig, sig_len) != 0) return 0;   /* 还没解壳到这里，或不是这个版本 */
    *tramp = install_inline_hook((void *)va, detour, name);
    return *tramp != NULL;
}

static int try_patch_draka_diag(void)
{
    if (g_draka_diag_patched) return 1;
    if (!draka_diag_install_one(DRAKA_CTOR_VA, DRAKA_CTOR_SIG, sizeof(DRAKA_CTOR_SIG),
                                draka_ctor_detour, &g_draka_ctor_tramp,
                                "岩浆巨龙诊断:boss 创建")) return 0;
    if (!draka_diag_install_one(DRAKA_GATE_VA, DRAKA_GATE_SIG, sizeof(DRAKA_GATE_SIG),
                                draka_gate_detour, &g_draka_gate_tramp,
                                "岩浆巨龙诊断:门开")) return 0;
    if (!draka_diag_install_one(DRAKA_QUEUE_VA, DRAKA_QUEUE_SIG, sizeof(DRAKA_QUEUE_SIG),
                                draka_queue_detour, &g_draka_queue_tramp,
                                "岩浆巨龙诊断:Tracing 入队")) return 0;
    if (!draka_diag_install_one(DRAKA_POINT_VA, DRAKA_POINT_SIG, sizeof(DRAKA_POINT_SIG),
                                draka_point_detour, &g_draka_point_tramp,
                                "岩浆巨龙诊断:弱点点出")) return 0;
    InterlockedExchange(&g_draka_diag_patched, 1);
    bslog("PATCH   ★岩浆巨龙诊断已装 @ %08X / %08X / %08X / %08X：boss 创建 / 门开 /"
          " Tracing 入队 / 弱点点出各打一行 DRAKA（BSHOOK_DRAKA_DIAG=0 可关）",
          (unsigned)DRAKA_CTOR_VA, (unsigned)DRAKA_GATE_VA,
          (unsigned)DRAKA_QUEUE_VA, (unsigned)DRAKA_POINT_VA);
    return 1;
}

/* -------------------------------------------------------------------------- */
/* ★ `BigShot.rpt` 里三种旧闪退的守护（V0.3 合成与商店 §48 / §49 / §50，D56）  */
/*                                                                            */
/*   三处都是原版少一个判空，各补一个：                                       */
/*                                                                            */
/*   A. 0x40f4df StartTutorial(App)：新手教程弹窗点「确认」后写               */
/*      `[[0x72e29c]+0x40]`（LobbyStage），LobbyStage 为空就崩（08-11 那次，   */
/*      C0000005 @ 0x40f4ef，EAX=0）。弹窗是模态的，弹着的时候大厅被拆掉       */
/*      （掉线 / 服务端重启）再点确认就是这条路。为空 → 直接走函数尾 0x40f57b。 */
/*                                                                            */
/*   B. 0x40ed98 主窗口 WndProc 的 WM_CLOSE 分支（0x40ef90 起）：弹「종료 확인」*/
/*      要建字体（0x4249d6 `[0x6e9400]`）。App 析构时先删字体管理器           */
/*      （0x40dbef 置 NULL）再拆内嵌 IE 控件，后者会泵一轮消息 —— 这时队列里   */
/*      还有一条 WM_CLOSE（用户多点了一下 X）就崩在 0x5cedc9（09-05 两次，      */
/*      栈上全是 mshtml / urlmon）。字体管理器为空 → 当「确认框已经在弹」处理，  */
/*      走 0x40f119 返回 0。                                                   */
/*                                                                            */
/*   C. 0x5d2702 CPU 蒙皮循环：每条蒙皮记录 `[表+4+i*4]` 配一个骨骼指针       */
/*      `[表+0x25c+i*4]`（0x5c2556 的构造函数把两段各 0x258 字节清零）。       */
/*      骨骼按名字在角色骨架里找，找不到就是 NULL，0x5d27f1 拿它 +0x58 喂        */
/*      D3DXMatrixMultiply 崩在 0x58cbff（09-06 两次，ECX=0x58）。触发条件是   */
/*      「网格属于别的角色」—— `Bone_Spine_01` / `Bone_Pelvis_01` 只有布洛克   */
/*      的骨架有，把 ch02 的铠甲挂到泰尔身上就全空。数据层 D31a 已经堵住        */
/*      （角色限定只认原版），这里再兜一层：骨骼为空的记录跳过不算              */
/*      （顶点留在原点，模型缺一块，但不崩），退出时打一行计数。               */
/*                                                                            */
/*   设 BSHOOK_KEEP_RPT_CRASHES=1 保留原版三处行为（复现 / 对照用）。          */
/* -------------------------------------------------------------------------- */
#define TUTORIAL_START_VA        0x0040F4EAu
#define TUTORIAL_START_SIG_LEN   16
static const unsigned char TUTORIAL_START_SIG[TUTORIAL_START_SIG_LEN] = {
    0xA1, 0x9C, 0xE2, 0x72, 0x00,       /* mov eax, [0x72e29c]  LobbyStage    */
    0xC6, 0x40, 0x40, 0x01,             /* mov byte [eax+0x40], 1            */
    0x83, 0x60, 0x4C, 0x00,             /* and dword [eax+0x4c], 0           */
    0x8D, 0x48, 0x44                    /* lea ecx, [eax+0x44]               */
};
#define TUTORIAL_START_RETURN_TO 0x0040F4EF
#define TUTORIAL_START_EXIT      0x0040F57B   /* mov ecx,[ebp-0xC] ; mov fs:[0],ecx ; leave ; ret 4 */
#define LOBBY_STAGE_GLOBAL       0x0072E29C

static __declspec(naked) void tutorial_start_guard_detour(void)
{
    __asm {
        mov  eax, LOBBY_STAGE_GLOBAL        /* 被偷走的原指令：eax = [0x72e29c] */
        mov  eax, dword ptr [eax]
        test eax, eax
        jz   tsg_bail
        push TUTORIAL_START_RETURN_TO       /* 大厅在：接着写 [eax+0x40] */
        ret
    tsg_bail:
        push TUTORIAL_START_EXIT            /* 大厅不在：什么都不做，走原有函数尾 */
        ret
    }
}

#define WM_CLOSE_GUARD_VA        0x0040EF90u
#define WM_CLOSE_GUARD_SIG_LEN   16
static const unsigned char WM_CLOSE_GUARD_SIG[WM_CLOSE_GUARD_SIG_LEN] = {
    0x80, 0x3D, 0x5C, 0xE9, 0x72, 0x00, 0x00,   /* cmp byte [0x72e95c], 0  确认框已在弹？ */
    0x0F, 0x85, 0x7C, 0x01, 0x00, 0x00,         /* jne 0x40f119                        */
    0xA1, 0x9C, 0xE2                            /* mov eax, [0x72e29c] ...             */
};
#define WM_CLOSE_GUARD_RETURN_TO 0x0040EF97   /* 那条 jne：靠我们留下的标志位分流 */
#define WM_CLOSE_GUARD_EXIT      0x0040F119   /* xor eax,eax … ret 0x10（已处理） */
#define FONT_MANAGER_GLOBAL      0x006E9400
#define QUIT_BOX_SHOWING_FLAG    0x0072E95C

static __declspec(naked) void wm_close_guard_detour(void)
{
    __asm {
        mov  eax, FONT_MANAGER_GLOBAL
        mov  eax, dword ptr [eax]
        test eax, eax
        jz   wcg_bail
        mov  eax, QUIT_BOX_SHOWING_FLAG     /* 被偷走的原指令 cmp byte [0x72e95c],0 —— */
        cmp  byte ptr [eax], 0              /* 标志位语义不变，eax 在 0x40ef97 之后立刻被覆盖 */
        push WM_CLOSE_GUARD_RETURN_TO       /* push/ret 不动标志位 */
        ret
    wcg_bail:
        push WM_CLOSE_GUARD_EXIT            /* 字体管理器已经没了：当作「确认框在弹」，返回 0 */
        ret
    }
}

#define SKIN_BONE_GUARD_VA       0x005D27F1u
#define SKIN_BONE_GUARD_SIG_LEN  16
static const unsigned char SKIN_BONE_GUARD_SIG[SKIN_BONE_GUARD_SIG_LEN] = {
    0x8B, 0x88, 0x58, 0x02, 0x00, 0x00,   /* mov ecx, [eax+0x258]  这条记录的骨骼 */
    0x8B, 0x00,                           /* mov eax, [eax]                       */
    0x83, 0xC1, 0x58,                     /* add ecx, 0x58        骨骼的世界矩阵   */
    0x51,                                 /* push ecx                             */
    0x83, 0xC0, 0x10,                     /* add eax, 0x10        记录自己的矩阵   */
    0x50                                  /* push eax                             */
};
#define SKIN_BONE_GUARD_RETURN_TO 0x005D27F7
#define SKIN_BONE_GUARD_CONTINUE  0x005D2909   /* 循环尾：inc [ebp+0x68] ; add [ebp+0x6c],4 ; cmp ; jl */

static volatile LONG g_skin_null_bone_skips = 0;

static __declspec(naked) void skin_bone_guard_detour(void)
{
    __asm {
        mov  ecx, dword ptr [eax + 0x258]   /* 被偷走的原指令 */
        test ecx, ecx
        jz   sbg_skip
        push SKIN_BONE_GUARD_RETURN_TO
        ret
    sbg_skip:
        inc  dword ptr [g_skin_null_bone_skips]
        push SKIN_BONE_GUARD_CONTINUE       /* 这条记录没有骨骼：跳过，去下一条 */
        ret
    }
}

static volatile LONG g_rpt_guards_patched = 0;

static int rpt_crashes_keep_original(void)
{
    char buf[8];
    DWORD n = GetEnvironmentVariableA("BSHOOK_KEEP_RPT_CRASHES", buf, sizeof(buf));
    return n > 0 && n < sizeof(buf) && buf[0] != '0';
}

/* 三处共用：签名对上就把头 5 字节换成 E9 跳 detour，多出来的字节补 NOP。
   幂等：已经是「E9 <到我们 detour 的 rel32>」就当打过。 */
static int install_jmp_guard(unsigned int va, const unsigned char *sig, int sig_len,
                             int stolen, void *detour, const char *what)
{
    unsigned char *p = (unsigned char *)va;
    DWORD oldp;
    int i;

    if (IsBadReadPtr(p, sig_len)) return 0;
    if (p[0] == 0xE9
        && (DWORD)(*(int *)(p + 1)) == (DWORD)((UINT_PTR)detour - (UINT_PTR)(p + 5)))
        return 1;
    if (memcmp(p, sig, sig_len) != 0)
        return 0;                          /* 还没解壳到这里，或不是已确认的版本 */
    if (!VirtualProtect(p, stolen, PAGE_EXECUTE_READWRITE, &oldp)) {
        bslog("PATCH   %s: VirtualProtect 失败 err=%lu", what, (unsigned long)GetLastError());
        return 0;
    }
    p[0] = 0xE9;
    *(DWORD *)(p + 1) = (DWORD)((UINT_PTR)detour - (UINT_PTR)(p + 5));
    for (i = 5; i < stolen; i++) p[i] = 0x90;
    VirtualProtect(p, stolen, oldp, &oldp);
    FlushInstructionCache(GetCurrentProcess(), p, stolen);
    return 1;
}

static int try_patch_rpt_crash_guards(void)
{
    int a, b, c;

    if (g_rpt_guards_patched) return 1;
    a = install_jmp_guard(TUTORIAL_START_VA, TUTORIAL_START_SIG, TUTORIAL_START_SIG_LEN,
                          5, tutorial_start_guard_detour, "教程弹窗判空");
    b = install_jmp_guard(WM_CLOSE_GUARD_VA, WM_CLOSE_GUARD_SIG, WM_CLOSE_GUARD_SIG_LEN,
                          7, wm_close_guard_detour, "退出中关窗判空");
    c = install_jmp_guard(SKIN_BONE_GUARD_VA, SKIN_BONE_GUARD_SIG, SKIN_BONE_GUARD_SIG_LEN,
                          6, skin_bone_guard_detour, "蒙皮骨骼判空");
    if (!(a && b && c)) return 0;
    InterlockedExchange(&g_rpt_guards_patched, 1);
    bslog("PATCH   ★旧闪退守护 x3: 教程弹窗大厅为空 @ %08X / 退出中收到 WM_CLOSE @ %08X"
          " / 蒙皮记录没绑上骨骼 @ %08X（BigShot.rpt 08-11 / 09-05 / 09-06 那三种）",
          (unsigned)TUTORIAL_START_VA, (unsigned)WM_CLOSE_GUARD_VA, (unsigned)SKIN_BONE_GUARD_VA);
    return 1;
}

/* -------------------------------------------------------------------------- */
/* ★★★ 崩溃报告器自己会崩 —— 出错地址不在任何已加载模块里就死在 0x5D76CD      */
/*      （V0.3 合成与商店 §65，bug调查/17）                                    */
/*                                                                            */
/*   原版写 `Dump\LastCrashReport.txt` / `BigShot.rpt` 的函数 0x5d7b90：       */
/*                                                                            */
/*     …写完 "Exception code: %08X %s"                                        */
/*     005d7ca5  call 0x5d7695(出错地址, 名字缓冲区, 0x104, &节号, &节内偏移)  */
/*     005d7cad  写 "Fault address:  %08X %02X:%08X %s"                       */
/*                                                                            */
/*   而 0x5d7695 这么找模块：                                                 */
/*                                                                            */
/*     005d76a6  VirtualQuery(addr, &mbi, 0x1c)   ; 失败 -> 返回 0            */
/*     005d76b8  edi = mbi.AllocationBase                                      */
/*     005d76bf  GetModuleFileNameA(edi, 缓冲区, 0x104)  ; 失败 -> 返回 0     */
/*     005d76cd  eax = [edi+0x3c]                 ; ★★ edi == 0 时读空指针    */
/*                                                                            */
/*   出错地址落在**已释放 / 从未提交**的页上时（= 通过野函数指针 call 过去，   */
/*   最常见的崩法），`AllocationBase` 就是 0，而 `GetModuleFileNameA(NULL,…)`  */
/*   按 Win32 语义返回的是**主模块路径**、非 0 ⇒ 判断放行 ⇒ `[0+0x3c]` 崩。   */
/*   崩在异常过滤器里 = 报告写到一半没了、`.mdmp` 根本没生成。                 */
/*                                                                            */
/*   ✅ 实证（bug调查/17 的 5 份现场）：3 份的 `LastCrashReport.txt` 正好断在   */
/*   "Exception code: C0000005 ACCESS_VIOLATION\r\r\n" 之后一个字节不多，      */
/*   `Dump File Name:` 那一行点名的 `.mdmp` 都不存在 —— 全断在这一句上。       */
/*   ⇒ **这三次崩溃的现场我们一个字都没拿到**，修它是拿到后续证据的前提。       */
/*                                                                            */
/*   另一半毛病：三个出参（名字 / 节号 / 节内偏移）原版**从不初始化**，        */
/*   两条失败路径都直接 return，调用方照样把它们打出去 —— 于是 09930195 那份    */
/*   打出来的是 `4DB8E501 00:00C5022C \x02`（栈上的垃圾）。                    */
/*                                                                            */
/*   补法（偷 0x5d76b4 起的 7 字节 = 三条原指令）：                            */
/*     ① 先把三个出参填成「不在模块里」的合法值：名字 = 空串、节号 = 0、       */
/*        节内偏移 = 出错地址本身 —— 之后任何一条失败路径打出来的都是          */
/*        `<地址> 00:<地址>`，不再是垃圾，也不会因为缓冲区没有结尾 0 而跑飞；  */
/*     ② `AllocationBase == 0` 就直接走原版的失败出口 0x5d76c9                */
/*        （`xor al,al ; jmp 0x5d7712` = `pop edi; pop esi; leave; ret`）。    */
/*                                                                            */
/*   设 BSHOOK_KEEP_RPT_CRASHES=1 一并保留原版行为（和上面三处同一个开关）。   */
/* -------------------------------------------------------------------------- */
#define CRASH_RPT_GUARD_VA       0x005D76B4u
#define CRASH_RPT_GUARD_SIG_LEN  17
static const unsigned char CRASH_RPT_GUARD_SIG[CRASH_RPT_GUARD_SIG_LEN] = {
    0x57,                                 /* push edi                          */
    0xFF, 0x75, 0x10,                     /* push [ebp+0x10]   nSize           */
    0x8B, 0x7D, 0xE4,                     /* mov  edi,[ebp-0x1c]  AllocationBase*/
    0xFF, 0x75, 0x0C,                     /* push [ebp+0xc]    名字缓冲区       */
    0x57,                                 /* push edi          hModule          */
    0xFF, 0x15, 0xAC, 0x71, 0x63, 0x00    /* call [0x6371ac]  GetModuleFileNameA*/
};
#define CRASH_RPT_GUARD_STOLEN     7
#define CRASH_RPT_GUARD_RETURN_TO  0x005D76BB   /* push [ebp+0xc] —— 接着原路   */
#define CRASH_RPT_GUARD_EXIT       0x005D76C9   /* xor al,al ; jmp 0x5d7712     */

static volatile LONG g_crash_rpt_guard_hits = 0;

static __declspec(naked) void crash_rpt_guard_detour(void)
{
    __asm {
        /* ① 三个出参先填成「不在任何模块里」的合法值 —— 原版一条失败路径都不填 */
        push eax
        push ecx
        mov  eax, dword ptr [ebp + 0x0C]    /* 模块名缓冲区 */
        mov  byte ptr [eax], 0              /* 空串，保证 %s 有结尾            */
        mov  eax, dword ptr [ebp + 0x14]    /* &节号                           */
        mov  dword ptr [eax], 0
        mov  ecx, dword ptr [ebp + 0x08]    /* 出错地址                        */
        mov  eax, dword ptr [ebp + 0x18]    /* &节内偏移                       */
        mov  dword ptr [eax], ecx           /* 填原始地址，比垃圾有用           */
        pop  ecx
        pop  eax
        /* ② 三条被偷走的原指令 */
        push edi
        push dword ptr [ebp + 0x10]
        mov  edi, dword ptr [ebp - 0x1C]
        test edi, edi
        jz   crg_bail
        push CRASH_RPT_GUARD_RETURN_TO
        ret
    crg_bail:
        /* 地址不在任何映射里：别去 GetModuleFileNameA(NULL) 骗过判断再读 [0+0x3c] */
        add  esp, 4                         /* 丢掉刚压的 nSize（不调那个 API） */
        inc  dword ptr [g_crash_rpt_guard_hits]
        push CRASH_RPT_GUARD_EXIT
        ret
    }
}

/* -------------------------------------------------------------------------- */
/* ★★★ `DashDamage`（突击技对象）野指针守护（V0.3 §66，bug调查/17 崩溃 A）    */
/*                                                                            */
/*   现场（09930195 2026-09-10 10:12:48，`C0000096 PRIV_INSTRUCTION`）：       */
/*   栈是 `GameContext::Process`(0x4904cc) → `Character::ProcessMove`(0x506fed)*/
/*   → `Character::ProcessDash`(0x5077c6) → 0x4814f2，崩在                    */
/*                                                                            */
/*     00481a51  mov eax,[esi]      ; esi = [Character+0x57c] = DashDamage*    */
/*     00481a53  call [eax+0x78]    ; ★ eax = 0x425DECA0（堆地址，不是虚表）   */
/*                                                                            */
/*   ⇒ `[Character+0x57c]` **非空但已经不是对象了**（对象被释放 / 内存被复用）。*/
/*   DEP 对这个 2007 年的 exe 是关的，于是 call 直接跳进堆里执行垃圾字节，      */
/*   撞上特权指令就是 `PRIV_INSTRUCTION`；跳进未提交页就是 `ACCESS_VIOLATION`  */
/*   —— 后者正是上面那三份「报告写到一半」的现场。                             */
/*                                                                            */
/*   `[Character+0x57c]` 全 exe 只有三处解引用，三处都不校验，全补上：          */
/*                                                                            */
/*     0x502182  Character::StartDash  开新的之前先拆旧的（`call [eax+0x20]`） */
/*     0x50794a  Character::ProcessDash 画拖影（`call 0x4814f2`）★ 崩的就是它  */
/*     0x5079aa  Character::ProcessDash 冲刺结束时拆掉（`call [eax+0x20]`）    */
/*                                                                            */
/*   判据是**虚表指针**：`DashDamage` 的主虚表恒为 0x66d5dc（构造函数          */
/*   0x481389 / 0x4813e6 都写这个值，`re/vftables.json` 也认得）。对不上就      */
/*   当「这一格已经没了」跳过 —— 不写回、不释放：万一是 `Character` 自己成了    */
/*   野指针，往 `[ebx+0x57c]` 写回去等于二次破坏。`StartDash` 那一处跳过之后    */
/*   原版自己会在 0x5021f0 用新对象覆盖这一格，所以不会一直卡着。              */
/*                                                                            */
/*   ★ 这三处是**兜底 + 探针**。**根因已经查到了**（V0.3 §78，bug调查/18）：   */
/*   关卡内换图会把世界的对象表整片析构掉，而 `[Character+0x57C]` 不跟着清 ——  */
/*   见下面 `try_patch_mapchange_dangling()`。三处校验保留，当最后一道网。      */
/*                                                                            */
/*   触发时按「指针值翻转」去重打一行日志（同一个野指针只报一次，换了才再报）， */
/*   下次现场靠它就能分辨「真的踩到了」还是「另有病灶」。                      */
/*                                                                            */
/*   逃生门：`BSHOOK_KEEP_RPT_CRASHES=1` 连同 bug调查/17 那五处一起不装；      */
/*   只想复现野指针、又要留着能用的崩溃报告器时用                              */
/*   `BSHOOK_KEEP_DASH_STALE_CRASH=1`（只关掉换图那一处根因修复）。            */
/* -------------------------------------------------------------------------- */
#define DASHOBJ_VFTABLE          0x0066D5DC   /* DashDamage 主虚表             */

#define DASHOBJ_START_VA         0x00502182  /* StartDash：拆旧的             */
#define DASHOBJ_START_SIG_LEN    15
static const unsigned char DASHOBJ_START_SIG[DASHOBJ_START_SIG_LEN] = {
    0x8B, 0x8F, 0x7C, 0x05, 0x00, 0x00,   /* mov ecx,[edi+0x57c]              */
    0x85, 0xC9,                           /* test ecx,ecx                     */
    0x74, 0x0C,                           /* je  0x502198                     */
    0x8B, 0x01,                           /* mov eax,[ecx]                    */
    0xFF, 0x50, 0x20                      /* call [eax+0x20]                  */
};
#define DASHOBJ_START_RETURN_TO  0x00502188   /* test ecx,ecx …（原路）        */
#define DASHOBJ_START_SKIP_TO    0x00502198   /* push 0x30c …（直接建新的）    */

#define DASHOBJ_DRAW_VA          0x0050794A  /* ProcessDash：画拖影           */
#define DASHOBJ_DRAW_SIG_LEN     16
static const unsigned char DASHOBJ_DRAW_SIG[DASHOBJ_DRAW_SIG_LEN] = {
    0x83, 0xBB, 0x7C, 0x05, 0x00, 0x00, 0x00, /* cmp dword [ebx+0x57c],0      */
    0x74, 0x22,                               /* je  0x507975                 */
    0x8B, 0x03,                               /* mov eax,[ebx]                */
    0x8D, 0x4D, 0xDC,                         /* lea ecx,[ebp-0x24]           */
    0x51,                                     /* push ecx                     */
    0x8B                                      /* mov ecx,ebx                  */
};
#define DASHOBJ_DRAW_RETURN_TO   0x00507953   /* `je` 之后那一条（有对象）      */
#define DASHOBJ_DRAW_SKIP_TO     0x00507975   /* `je` 的目标（没对象）          */

#define DASHOBJ_KILL_VA          0x005079AA  /* ProcessDash：冲刺结束拆掉      */
#define DASHOBJ_KILL_SIG_LEN     14
static const unsigned char DASHOBJ_KILL_SIG[DASHOBJ_KILL_SIG_LEN] = {
    0x8B, 0xB3, 0x7C, 0x05, 0x00, 0x00,       /* mov esi,[ebx+0x57c]          */
    0x85, 0xF6,                               /* test esi,esi                 */
    0x0F, 0x84, 0xDE, 0x00, 0x00, 0x00        /* je  0x507a96                 */
};
#define DASHOBJ_KILL_RETURN_TO   0x005079B0   /* test esi,esi …（原路）        */
#define DASHOBJ_KILL_SKIP_TO     0x00507A96   /* mov al,1 ; jmp 0x507ab1      */

/* 「说过了就不再说，直到指针真的变了」—— 按状态翻转去重，不按次数 / 时间窗。 */
static volatile LONG g_dashobj_stale_ptr = 0;
static volatile LONG g_dashobj_stale_hits = 0;

static void __stdcall dashobj_stale_note(unsigned int obj, unsigned int site)
{
    InterlockedIncrement(&g_dashobj_stale_hits);
    if ((LONG)obj == InterlockedExchange(&g_dashobj_stale_ptr, (LONG)obj))
        return;                            /* 同一个野指针，别刷屏 */
    bslog("★DASH   [Character+0x57C] 指着的不是 DashDamage（虚表 %08X != %08X）"
          "@ %08X —— 已跳过不用它（累计 %ld 次）；这一格本该在冲刺结束时清掉",
          obj ? *(unsigned int *)obj : 0u, (unsigned)DASHOBJ_VFTABLE,
          site, (long)g_dashobj_stale_hits);
}

static __declspec(naked) void dashobj_start_guard_detour(void)
{
    __asm {
        mov  ecx, dword ptr [edi + 0x57C]   /* 被偷走的原指令 */
        test ecx, ecx
        jz   dsg_skip                       /* 本来就是空：原版也是跳过 */
        cmp  dword ptr [ecx], DASHOBJ_VFTABLE
        je   dsg_ok
        pushad
        push DASHOBJ_START_VA
        push ecx
        call dashobj_stale_note
        popad
        jmp  dsg_skip
    dsg_ok:
        push DASHOBJ_START_RETURN_TO
        ret
    dsg_skip:
        push DASHOBJ_START_SKIP_TO
        ret
    }
}

static __declspec(naked) void dashobj_draw_guard_detour(void)
{
    __asm {
        mov  eax, dword ptr [ebx + 0x57C]   /* 原指令是 cmp …,0；我们直接取值 */
        test eax, eax
        jz   ddg_skip
        cmp  dword ptr [eax], DASHOBJ_VFTABLE
        je   ddg_ok
        pushad
        push DASHOBJ_DRAW_VA
        push eax
        call dashobj_stale_note
        popad
        jmp  ddg_skip
    ddg_ok:
        push DASHOBJ_DRAW_RETURN_TO         /* 越过那条 je，不依赖标志位 */
        ret
    ddg_skip:
        push DASHOBJ_DRAW_SKIP_TO
        ret
    }
}

static __declspec(naked) void dashobj_kill_guard_detour(void)
{
    __asm {
        mov  esi, dword ptr [ebx + 0x57C]   /* 被偷走的原指令 */
        test esi, esi
        jz   dkg_skip
        cmp  dword ptr [esi], DASHOBJ_VFTABLE
        je   dkg_ok
        pushad
        push DASHOBJ_KILL_VA
        push esi
        call dashobj_stale_note
        popad
        jmp  dkg_skip
    dkg_ok:
        push DASHOBJ_KILL_RETURN_TO
        ret
    dkg_skip:
        push DASHOBJ_KILL_SKIP_TO
        ret
    }
}

/* -------------------------------------------------------------------------- */
/* ★★ `0x0411 gspEndGame` 处理器不判空：没有 GameContext 时读空指针           */
/*     （V0.3 §67，本机 `BigShot.rpt` 2026-09-10 00:05:39 那次）              */
/*                                                                            */
/*     005518de  mov esi,[0x72e2dc]   ; 当前 GameContext（不在关卡里 = NULL）  */
/*     005518e4  call 0x4913fc        ; 弹结算界面，第一句就是                 */
/*     0049140c    cmp byte [esi+4], bl   ★ esi == 0 -> C0000005              */
/*                                                                            */
/*   `[0x72e2dc]` 只在关卡里非空 —— `Character::StartDash`(0x5020e6) 自己就先   */
/*   判了空，这条路上原版忘了。任何一发在关卡外到达的 `0x0411` 都会闪退：      */
/*   控制通道的 `clear` / `endgame`（`tools/quest-clear.bat` 调的就是它）在     */
/*   人不在关卡里时发一发就能复现，线上则是「结算包比舞台拆除晚到」。          */
/*                                                                            */
/*   补法：偷 0x5518de 起 6 字节，GameContext 为空就跳过那一发，直接去          */
/*   0x5518e9（后面 `0x4087f0` 那一发只读 `[0x72e29c]`，和 GameContext 无关）。 */
/* -------------------------------------------------------------------------- */
#define ENDGAME_GUARD_VA         0x005518DEu
#define ENDGAME_GUARD_SIG_LEN    12
static const unsigned char ENDGAME_GUARD_SIG[ENDGAME_GUARD_SIG_LEN] = {
    0x8B, 0x35, 0xDC, 0xE2, 0x72, 0x00,   /* mov esi,[0x72e2dc]  GameContext  */
    0xE8, 0x13, 0xFB, 0xF3, 0xFF, 0xFF    /* call 0x4913fc                    */
};
#define ENDGAME_GUARD_RETURN_TO  0x005518E4   /* call 0x4913fc（原路）         */
#define ENDGAME_GUARD_SKIP_TO    0x005518E9   /* push [0x72e29c] ; call 0x4087f0 */
#define GAME_CONTEXT_GLOBAL      0x0072E2DC

static volatile LONG g_endgame_guard_hits = 0;

static __declspec(naked) void endgame_guard_detour(void)
{
    __asm {
        mov  esi, GAME_CONTEXT_GLOBAL       /* 被偷走的原指令 mov esi,[0x72e2dc] */
        mov  esi, dword ptr [esi]
        test esi, esi
        jz   egg_skip
        push ENDGAME_GUARD_RETURN_TO
        ret
    egg_skip:
        inc  dword ptr [g_endgame_guard_hits]
        push ENDGAME_GUARD_SKIP_TO
        ret
    }
}

static volatile LONG g_crash17_guards_patched = 0;

/* bug调查/17 那一批守护共用一个开关（和 §48~§50 那三处是同一类东西）。 */
static int try_patch_crash17_guards(void)
{
    int a, b, c, d, e;

    if (g_crash17_guards_patched) return 1;
    a = install_jmp_guard(CRASH_RPT_GUARD_VA, CRASH_RPT_GUARD_SIG, CRASH_RPT_GUARD_SIG_LEN,
                          CRASH_RPT_GUARD_STOLEN, crash_rpt_guard_detour,
                          "崩溃报告器找模块判空");
    b = install_jmp_guard(DASHOBJ_START_VA, DASHOBJ_START_SIG, DASHOBJ_START_SIG_LEN,
                          6, dashobj_start_guard_detour, "突击技对象校验(StartDash)");
    c = install_jmp_guard(DASHOBJ_DRAW_VA, DASHOBJ_DRAW_SIG, DASHOBJ_DRAW_SIG_LEN,
                          7, dashobj_draw_guard_detour, "突击技对象校验(绘制)");
    d = install_jmp_guard(DASHOBJ_KILL_VA, DASHOBJ_KILL_SIG, DASHOBJ_KILL_SIG_LEN,
                          6, dashobj_kill_guard_detour, "突击技对象校验(销毁)");
    e = install_jmp_guard(ENDGAME_GUARD_VA, ENDGAME_GUARD_SIG, ENDGAME_GUARD_SIG_LEN,
                          6, endgame_guard_detour, "gspEndGame 判空");
    if (!(a && b && c && d && e)) return 0;
    InterlockedExchange(&g_crash17_guards_patched, 1);
    bslog("PATCH   ★崩溃守护 x5（bug调查/17）: 报告器找模块判空 @ %08X"
          " / 突击技对象校验 @ %08X %08X %08X / gspEndGame 判空 @ %08X",
          (unsigned)CRASH_RPT_GUARD_VA, (unsigned)DASHOBJ_START_VA,
          (unsigned)DASHOBJ_DRAW_VA, (unsigned)DASHOBJ_KILL_VA,
          (unsigned)ENDGAME_GUARD_VA);
    return 1;
}

/* -------------------------------------------------------------------------- */
/* ★★★★★ 换图（进 boss 房）把突击技对象连同场景一起放掉了 —— 这就是            */
/*         §66 那个野指针的**来路**（V0.3 §78，bug调查/18）                    */
/*                                                                            */
/*   现象：闯关走到地图最右边换下一张图（Quest03_6 / Quest06_boss / …），       */
/*   进新图后 1~3 秒**整个房间的人同时**闪退或黑屏未响应。云端 9-10 一天       */
/*   107 条游戏服连接里有 20 条断在这儿，23 份崩溃报告里 13 份是它。            */
/*                                                                            */
/*   链路（全部静态坐实）：                                                    */
/*                                                                            */
/*     Character::StartDash 0x5020d9                                          */
/*       0x5021f0  [Character+0x57C] = new(0x30C)      ← 突击技伤害对象        */
/*       0x502229  call 0x473e7c(世界 [0x72e2d4], 它)  ← ★ 登记进世界          */
/*      （兄弟函数 0x5024ac / 0x5024e2 对 [Character+0x580] 做同样的事）        */
/*                                                                            */
/*     关卡内换图 0x47900a                                                     */
/*       0x479058  call 0x474029  把 6 个角色**摘出**世界（只摘角色）          */
/*       0x4790be  call GameContext::vft+8 = 0x48d2ad                         */
/*                   └ 0x48d326 call 0x473692 ← ★ 把世界的对象表整片清掉      */
/*                     （0x4736a2/0x4736b6/0x4736ca 三轮 list 析构）           */
/*       0x4790d2  call 0x473e7c  把**同一批**角色指针原样挂回去               */
/*                                                                            */
/*   ⇒ 角色对象跨图活着，可它身上那两格指着的东西已经被世界析构了。            */
/*     原版**没有任何一处**在换图时清这两格（全 exe 只有 0x502191 /            */
/*     0x5079d8 / 0x507a8f 三处写 0，都在冲刺自己的状态机里）。                */
/*     下一次 `Character::ProcessDash` 走到 0x50794a 看见「非空」就用：        */
/*                                                                            */
/*       00507970  call 0x4814f2                                              */
/*         00481a51  mov eax,[esi]     ; esi = 已经被释放的那块内存            */
/*         00481a53  call [eax+0x78]   ; ★ 跳进堆里执行垃圾（exe 没开 DEP）    */
/*                                                                            */
/*   **为什么是一屋子人一起死**：每台客户端都在本地模拟全部 6 个角色，         */
/*   那一格在**每台机器上**都同样悬着 ⇒ 谁在换图那一刻正冲刺，全场一起崩。     */
/*   **为什么有时是黑屏而不是闪退**：跳飞的落点在未提交页时，原版崩溃报告器    */
/*   自己也会崩（§65），进程要挂 40~90 秒才退 —— 窗口就一直停在换图加载那一帧  */
/*   （「正在载入 100% / 正在等待其他玩家」）显示「未响应」。                   */
/*                                                                            */
/*   修法：在换图**卸完场景、重挂角色之前**把这两格清零。清零是安全的：        */
/*   对象此刻已经被世界析构掉了，我们只是丢掉一个悬空指针，不释放、不回调；    */
/*   而 `NULL` 本来就是这两格的合法取值（`StartDash` 在 `new` 失败时就写       */
/*   `NULL`，`ProcessDash` 三处解引用前全都判空）。                            */
/*                                                                            */
/*   偷 0x4790cb 起 6 字节（`mov ecx,[0x72e2d4]`，正好一条指令），edi 就是这   */
/*   一轮的角色（0x4790c7 刚判过非空），补完把 ecx 装回去再回 0x4790d1。       */
/*                                                                            */
/*   ★ §66 那三处虚表校验**保留**：它们是最后一道网（万一还有别的路子放掉      */
/*   这个对象），而这里是根因修复。设 BSHOOK_KEEP_DASH_STALE_CRASH=1 时        */
/*   这一处也不装（复现 / 对照用，和三处校验同一个开关）。                     */
/* -------------------------------------------------------------------------- */
#define MAPCHG_CLEAR_VA        0x004790CBu   /* 换图：重新挂回角色的循环体      */
#define MAPCHG_CLEAR_SIG_LEN   13
static const unsigned char MAPCHG_CLEAR_SIG[MAPCHG_CLEAR_SIG_LEN] = {
    0x8B, 0x0D, 0xD4, 0xE2, 0x72, 0x00,   /* mov ecx,[0x72e2d4]  世界        */
    0x57,                                 /* push edi            这个角色    */
    0xE8, 0xA5, 0xAD, 0xFF, 0xFF,         /* call 0x473e7c       挂回世界    */
    0x57                                  /* push edi                        */
};
#define MAPCHG_CLEAR_RETURN_TO 0x004790D1     /* push edi ; call 0x473e7c     */
#define WORLD_MGR_GLOBAL       0x0072E2D4     /* 世界 / 句柄管理器            */
#define CHAR_DASHOBJ_OFF       0x57C          /* 突击技伤害对象               */
#define CHAR_DASHOBJ2_OFF      0x580          /* 突击技的第二个对象           */

static volatile LONG g_mapchg_clear_hits = 0;

static void __stdcall mapchg_clear_note(unsigned int chr, unsigned int a, unsigned int b)
{
    InterlockedIncrement(&g_mapchg_clear_hits);
    bslog("★换图   角色 %08X 跨图还挂着突击技对象（+0x57C=%08X +0x580=%08X）——"
          " 场景已卸，这两格现在是野指针，已清零（累计 %ld 次；不清就是"
          " bug调查/18 那个「进 boss 房全场一起崩」）",
          chr, a, b, (long)g_mapchg_clear_hits);
}

static __declspec(naked) void mapchg_clear_detour(void)
{
    __asm {
        pushad
        mov  eax, dword ptr [edi + CHAR_DASHOBJ_OFF]
        mov  edx, dword ptr [edi + CHAR_DASHOBJ2_OFF]
        mov  ecx, eax
        or   ecx, edx
        jz   mcc_done                       /* 两格都空：这一局没人冲刺过 */
        push edx
        push eax
        push edi
        call mapchg_clear_note
        and  dword ptr [edi + CHAR_DASHOBJ_OFF], 0
        and  dword ptr [edi + CHAR_DASHOBJ2_OFF], 0
    mcc_done:
        popad
        mov  ecx, WORLD_MGR_GLOBAL          /* 被偷走的 mov ecx,[0x72e2d4] */
        mov  ecx, dword ptr [ecx]
        push MAPCHG_CLEAR_RETURN_TO
        ret
    }
}

static volatile LONG g_mapchg_clear_patched = 0;

/* 只关掉这一处根因修复（崩溃报告器那几处照装）—— 想在本机复现「进 boss 房
   全场一起崩」并拿一份完整 dump 时用。 */
static int dash_stale_keep_original(void)
{
    char buf[8];
    DWORD n = GetEnvironmentVariableA("BSHOOK_KEEP_DASH_STALE_CRASH", buf, sizeof(buf));
    return n > 0 && n < sizeof(buf) && buf[0] != '0';
}

static int try_patch_mapchange_dangling(void)
{
    if (g_mapchg_clear_patched) return 1;
    if (!install_jmp_guard(MAPCHG_CLEAR_VA, MAPCHG_CLEAR_SIG, MAPCHG_CLEAR_SIG_LEN,
                           6, mapchg_clear_detour, "换图清突击技野指针"))
        return 0;
    InterlockedExchange(&g_mapchg_clear_patched, 1);
    bslog("PATCH   ★换图野指针根治（bug调查/18）: 卸完场景后清 [角色+0x57C/0x580]"
          " @ %08X —— 进 boss 房不再全场一起崩", (unsigned)MAPCHG_CLEAR_VA);
    return 1;
}

/* -------------------------------------------------------------------------- */
/* ★★★★ 关掉 NEXON 那套信使 —— `nmconew.dll` 一律不让加载                     */
/*        （V0.3 §80，bug调查/18；根治 §68 那一族崩溃，用户 2026-09-10 拍板）  */
/*                                                                            */
/*   §79 里 23 份线上崩溃有 4 份栈**整条都在 `nmconew.dll` 里**（两个落点：    */
/*   `+0x98D62` 自制互斥体 `Enter()` 醒来往已析构的锁里写、`+0x26591`）。      */
/*   触发条件这次看清楚了：**同一个账号在本机重新登录**（新连接把旧的顶掉），  */
/*   新登进来那个客户端 17~31 秒后崩 —— 信使那套东西在拆了重建的缝里翻车。     */
/*                                                                            */
/*   §68 当时不敢动它（基址随机、锁对象已经没了，跳过那两句只是把崩溃挪一行）。 */
/*   ★ 换个位置就干净了：**根本别让它加载**。这套信使连的是停机 15 年的        */
/*   `platform.tiancity.com` / `ngm.nexon.net`，功能早就是零。                 */
/*                                                                            */
/*   为什么这么做是安全的（全部静态坐实，不是「试试看」）：                    */
/*                                                                            */
/*     1. `nmconew.dll` **只有一个加载点** —— `nmcogame.dll` 的                */
/*        `NMCO_CallNMFunc`（`0x10002005` / `0x1000201E` 调它的               */
/*        `DynamicLib::Load`，路径是 `GetModuleFileNameA` + 目录 + 文件名，    */
/*        文件名由 `0x10006AD0` 返回 = `"nmconew.dll"`）。                     */
/*     2. 那个 `Load` 对 `LoadLibraryA` 返回 NULL **判空**（`0x10001458`），   */
/*        `NMCO_CallNMFunc` 随后 `[0x10036168] == 0` 走                       */
/*        `0x1000204F` 那条**设计好的退路**：往日志文件写一行                  */
/*        `"Fail to load messenger module! Version file URL: …"`              */
/*        （`0x10003870` 是写文件的记录器，不是弹框 —— 全模块唯一那个          */
/*        MessageBox 包装 `0x10027CCF` 只被 `0x1002330E` / `0x10025908` 调）， */
/*        然后 `xor eax,eax` **返回 0**。                                     */
/*     3. 客户端侧 `0x5441DB` 拿到返回值就 `cmp eax,1 / jne` 走失败分支、      */
/*        整个包装函数返回 false —— 一条**原版自己就有**的普通失败路径。       */
/*     4. `NMService.exe` 的 exe 名和 `"%s" -domain:%s` 命令行模板             */
/*        **只存在于 `nmconew.dll` 里**（`nmcogame.dll` 里一个字都没有）       */
/*        ⇒ 不加载它，那个进程自然也不会被拉起来。用户要的「不要 NMService」   */
/*        这一条同时就做到了，不用再去拦 `CreateProcess`。                     */
/*                                                                            */
/*   做法：**只改 `nmcogame.dll` 自己的 IAT**（RVA `0x2C030` = 它导入的        */
/*   `kernel32!LoadLibraryA`），换成一个按文件名过滤的桩。不去 inline hook     */
/*   `kernel32!LoadLibraryA` —— 那是全进程的热路径，为了一个 DLL 去动它        */
/*   波及面太大。写之前先核对「这一格现在正好等于                              */
/*   `GetProcAddress(kernel32,"LoadLibraryA")`」，对不上就不写（这比字节特征   */
/*   串更硬：它直接证明这一格就是那个导入槽）。                                */
/*                                                                            */
/*   设 BSHOOK_KEEP_NM=1 保留原版行为（要用信使 / 要复现 §68 时）。            */
/* -------------------------------------------------------------------------- */
#define NMCOGAME_LOADLIBRARYA_IAT_RVA  0x0002C030u

typedef HMODULE (WINAPI *LoadLibraryA_t)(LPCSTR);
static LoadLibraryA_t s_nm_LoadLibraryA = NULL;   /* 真的那一个 */
static volatile LONG g_nm_block_hits = 0;
static volatile LONG g_nm_blocked = 0;

/* 大小写无关地找子串（不用 CRT 的 _stricmp/strstr 组合，短小自证）。 */
static int name_has_ci(const char *hay, const char *needle)
{
    size_t i, j;
    if (!hay || !needle) return 0;
    for (i = 0; hay[i]; i++) {
        for (j = 0; needle[j]; j++) {
            char a = hay[i + j], b = needle[j];
            if (a >= 'A' && a <= 'Z') a = (char)(a - 'A' + 'a');
            if (b >= 'A' && b <= 'Z') b = (char)(b - 'A' + 'a');
            if (a != b) break;
        }
        if (!needle[j]) return 1;
    }
    return 0;
}

static HMODULE WINAPI det_nmcogame_LoadLibraryA(LPCSTR name)
{
    if (name_has_ci(name, "nmconew")) {
        LONG n = InterlockedIncrement(&g_nm_block_hits);
        bslog("★NM     挡下 nmcogame 加载信使模块：\"%s\"（第 %ld 次）—— "
              "NMCO_CallNMFunc 会走它自己的「模块不可用」退路返回 0，"
              "NMService.exe 也不会被拉起来（bug调查/18 §80）",
              name ? name : "(null)", (long)n);
        SetLastError(ERROR_MOD_NOT_FOUND);
        return NULL;
    }
    return s_nm_LoadLibraryA(name);
}

static int nm_keep_original(void)
{
    char buf[8];
    DWORD n = GetEnvironmentVariableA("BSHOOK_KEEP_NM", buf, sizeof(buf));
    return n > 0 && n < sizeof(buf) && buf[0] != '0';
}

static int try_block_nexon_messenger(void)
{
    HMODULE nmcogame, k32;
    LoadLibraryA_t real;
    LoadLibraryA_t *slot;
    DWORD oldp;

    if (g_nm_blocked) return 1;
    nmcogame = GetModuleHandleA("nmcogame.dll");
    if (!nmcogame) return 0;                     /* 还没加载，下一轮再来 */
    k32 = GetModuleHandleA("kernel32.dll");
    if (!k32) return 0;
    real = (LoadLibraryA_t)GetProcAddress(k32, "LoadLibraryA");
    if (!real) return 0;

    slot = (LoadLibraryA_t *)((UINT_PTR)nmcogame + NMCOGAME_LOADLIBRARYA_IAT_RVA);
    if (IsBadReadPtr(slot, sizeof(*slot))) {
        bslog("★NM     nmcogame IAT %08X 读不了，放弃拦截（保留原版行为）",
              (unsigned)(UINT_PTR)slot);
        InterlockedExchange(&g_nm_blocked, 1);   /* 别再重试 */
        return 1;
    }
    if (*slot == det_nmcogame_LoadLibraryA) {    /* 幂等 */
        InterlockedExchange(&g_nm_blocked, 1);
        return 1;
    }
    if (*slot != real) {
        bslog("★NM     nmcogame IAT %08X = %08X，不是 kernel32!LoadLibraryA(%08X)"
              " —— 版本对不上，不动它",
              (unsigned)(UINT_PTR)slot, (unsigned)(UINT_PTR)*slot,
              (unsigned)(UINT_PTR)real);
        InterlockedExchange(&g_nm_blocked, 1);
        return 1;
    }
    if (!VirtualProtect(slot, sizeof(*slot), PAGE_READWRITE, &oldp)) {
        bslog("★NM     nmcogame IAT VirtualProtect 失败 err=%lu",
              (unsigned long)GetLastError());
        return 0;
    }
    s_nm_LoadLibraryA = real;
    *slot = det_nmcogame_LoadLibraryA;
    VirtualProtect(slot, sizeof(*slot), oldp, &oldp);
    InterlockedExchange(&g_nm_blocked, 1);
    bslog("PATCH   ★NEXON 信使已关（bug调查/18）: nmcogame.dll base=%08X "
          "IAT %08X 的 LoadLibraryA 换成过滤桩 —— nmconew.dll 不再加载，"
          "NMService.exe 也不会起（BSHOOK_KEEP_NM=1 可退回原版）",
          (unsigned)(UINT_PTR)nmcogame, (unsigned)(UINT_PTR)slot);
    return 1;
}

/* ========================================================================== */
/* ★ M3b 诊断：弹体全字段快照 —— 查「bot 的子弹别人看不见」（V0.3 §53~§56）   */
/*                                                                            */
/*   已知：bot 的弹体在收方**确实存在**（打得掉血、爆炸动画正常，而            */
/*   `OnExplode` 查不到弹体句柄就整个 return），但**从来没被画出来**；         */
/*   同一台客户端上真人的子弹和自己的子弹都看得见。逐指令读过收侧的            */
/*   `OnFire`（0x491f12）→ 工厂 → `BulletObj::Init`（0x47d6a1）→              */
/*   `ProjectileMgr::Add`（0x473e7c）→ 每帧 tick（0x47de6a），**一处按        */
/*   owner / 座位分流的分支都没有** —— 所以差别一定落在弹体对象的某个字段上。  */
/*                                                                            */
/*   两个 hook 把那个字段直接抓出来，不用再猜：                                */
/*                                                                            */
/*     1. `ProjectileMgr::Add`  每颗弹体登记那一刻打一份**全字段快照**         */
/*        —— 自己开的枪、真人开的枪、bot 开的枪走的是同一个函数，              */
/*        三份快照并排一比，差在哪一格一目了然。                               */
/*     2. `BulletObj` 每帧 tick  按**整数位置翻转**去重打点                    */
/*        —— 弹体动没动、往哪飞、飞到哪没的，一条轨迹就出来了。               */
/*        （不动的弹体只会打第一行，所以静止的子弹不会刷屏。）                 */
/*                                                                            */
/*   ★ 这是**临时诊断**，M3b 收口后连同两个 detour 一起删。                    */
/*     用 BSHOOK_PROJ_DIAG=0 可以关掉（默认开）。                              */
/* ========================================================================== */
#define PROJ_ADD_VA   0x00473E7Cu   /* ProjectileMgr::Add(proj)  __thiscall   */
#define PROJ_TICK_VA  0x0047DE6Au   /* BulletObj vft+0x24：每帧推进 __thiscall*/
#define PROJ_FIRE_VA  0x00491F12u   /* GameContext::OnFire  __thiscall ret 20 */
#define MYSEAT_PP     0x0072E29Cu   /* [[0x72e29c]+0x1cc] = 我的座位号        */

/* 偷 5 字节落在指令边界上，而且偷到的都不是相对跳转（搬到蹦床上不会错位）：
     0x473e7c  b8 4e 6b 62 00     mov eax, 0x626b4e     ← 正好 5 字节
     0x491f12  b8 c4 aa 62 00     mov eax, 0x62aac4     ← 正好 5 字节
     0x47de6a  55 / 8b ec / 83 ec 14                    ← 1+2+3 = 6 字节 */
static const unsigned char PROJ_ADD_SIG[5]  = { 0xB8, 0x4E, 0x6B, 0x62, 0x00 };
static const unsigned char PROJ_TICK_SIG[6] = { 0x55, 0x8B, 0xEC, 0x83, 0xEC, 0x14 };
static const unsigned char PROJ_FIRE_SIG[5] = { 0xB8, 0xC4, 0xAA, 0x62, 0x00 };

static void *g_proj_add_tramp  = NULL;
static void *g_proj_tick_tramp = NULL;
static void *g_proj_fire_tramp = NULL;
static volatile LONG g_proj_diag_patched = 0;

/* ★★ 默认值是**跟着日志级别走**，不是「没设 = 开」（用户 2026-09-01 的卡顿）。
 *
 * 这套 hook 是**每帧 × 每弹体**跑的（见 proj_tick_log）：一次 IsBadReadPtr 探
 * 0x340 字节（≈13 页）+ 64 项线性扫 + 最多 3 条日志 + 两次 20 个 float 的格式化。
 * 以前「没设 = 开」意味着 `start.bat` 正常游玩时它**一直在跑**，精简模式日志里
 * 89% 的行出自它 —— 子弹一多就掉帧的头号原因。
 *
 * 关掉时 patch_thread 压根不装那三个 detour ⇒ 连探测和扫描都省掉，是真的零成本。
 * 要单独查弹体问题时 `BSHOOK_PROJ_DIAG=1` 仍然能在精简模式下强制打开。
 */
static int proj_diag_enabled(void)
{
    char buf[8];
    DWORD n = GetEnvironmentVariableA("BSHOOK_PROJ_DIAG", buf, sizeof(buf));
    if (n == 0 || n >= sizeof(buf)) return g_verbose ? 1 : 0;   /* 没设 = 跟日志级别 */
    return buf[0] != '0';
}

static int proj_my_seat(void)
{
    UINT_PTR *pp = (UINT_PTR *)MYSEAT_PP;
    UINT_PTR ctx;
    if (IsBadReadPtr(pp, 4)) return -1;
    ctx = *pp;
    if (!ctx || IsBadReadPtr((void *)(ctx + 0x1CC), 4)) return -1;
    return (int)*(int *)(ctx + 0x1CC);
}

/* 句柄 -> owner（0x473e65）：h < 100000 是怪/中立(20)，否则 10 + 座位号 */
static int proj_owner_of(int handle)
{
    if (handle < 100000) return 20;
    return (handle - 100000) / 100000 + 10;
}

#define PF(off) (*(float *)(p + (off)))
#define PI(off) (*(int   *)(p + (off)))
#define PU(off) ((unsigned)*(UINT_PTR *)(p + (off)))

static void proj_track_add(void *proj, int handle);
static int proj_is_bullet(unsigned char *p);

/* 把一个小对象的前 n 个 dword 原样倒出来（一行装得下），带 vftable。
   浮点那几格顺手按 float 也印一遍 —— 缩放 / alpha 之类都是 float。 */
/* `detail = 1` 走 bsvlog（每帧级的调用方用它）—— 见 bslog_emit 的说明：
   每帧 × 每弹体的东西不该占 flush + DebugView 那一档。 */
static void proj_dump_obj(int detail, const char *tag, void *obj, int n)
{
    char hex[320], flt[320];
    int i, hx = 0, fx = 0;
    unsigned *w = (unsigned *)obj;

    if (detail && !g_verbose) return;
    if (!obj || IsBadReadPtr(obj, (UINT_PTR)n * 4)) {
        if (detail)
            bsvlog("            %s = %08X（空 / 读不了）", tag,
                   (unsigned)(UINT_PTR)obj);
        else
            bslog("            %s = %08X（空 / 读不了）", tag,
                  (unsigned)(UINT_PTR)obj);
        return;
    }
    for (i = 0; i < n && hx < 280; i++) {
        float f;
        memcpy(&f, &w[i], 4);
        hx += _snprintf(hex + hx, sizeof(hex) - hx - 1, "%08X ", w[i]);
        if (f > -1e6f && f < 1e6f && (f > 1e-6f || f < -1e-6f))
            fx += _snprintf(flt + fx, sizeof(flt) - fx - 1,
                            "[%d]=%.3f ", i, f);
    }
    hex[hx] = 0;
    flt[fx] = 0;
    if (detail)
        bsvlog("            %s @%08X: %s | %s", tag, (unsigned)(UINT_PTR)obj,
               hex, flt);
    else
        bslog("            %s @%08X: %s | %s", tag, (unsigned)(UINT_PTR)obj,
              hex, flt);
}

static void __cdecl proj_add_log(void *proj)
{
    unsigned char *p = (unsigned char *)proj;
    int handle;

    if (!p || IsBadReadPtr(p, 0x340)) return;
    handle = PI(0xD0);
    if (!proj_is_bullet(p)) return;      /* 角色 / 地图物件也走 Add，跳过 */
    proj_track_add(proj, handle);
    bslog("PROJ+   弹体 %08X vft %08X 句柄 %d owner %d（我的座位 %d）"
          " | 发方(+2f8) %d 槽(+15c) %d 武器(+304) %d 定义(+308) %08X",
          (unsigned)(UINT_PTR)p, PU(0x00), handle, proj_owner_of(handle),
          proj_my_seat(), PI(0x2F8), PI(0x15C), PI(0x304), PU(0x308));
    bslog("        位置(+34,38) (%.2f, %.2f)  渲染位(+2c,30) (%.2f, %.2f)"
          "  速度(+120,124) (%.3f, %.3f)  重力(+314) %.4f  状态(+54) %d",
          PF(0x34), PF(0x38), PF(0x2C), PF(0x30),
          PF(0x120), PF(0x124), PF(0x314), PI(0x54));
    bslog("        ★视觉 模型(+e8) %08X  特效(+e4) %08X  弹道线(+30c) %08X"
          "  拖尾(+310) %08X  碰撞型(+32c) %d  寿命(+318,4) %d"
          "  绑定(+8c) %08X",
          PU(0xE8), PU(0xE4), PU(0x30C), PU(0x310), PI(0x32C), PI(0x31C),
          PU(0x8C));
    /* ★ 光看「指针非 0」不够 —— bot 和真人的四个视觉指针全都非 0，
       模式也一样，可屏幕上就是只有真人的看得见。所以把那两个对象**的内容**
       原样倒出来，一格一格比。 */
    proj_dump_obj(0, "特效(+e4)", (void *)(UINT_PTR)PU(0xE4), 20);
    proj_dump_obj(0, "拖尾(+310)", (void *)(UINT_PTR)PU(0x310), 20);
    proj_dump_obj(0, "线(+30c)", (void *)(UINT_PTR)PU(0x30C), 20);
}

/* ★ 只跟踪「`Add` 认出来是子弹」的那些对象 —— `ProjectileMgr` 那张 map 里
   还躺着角色和一堆地图物件（实测 vft 有十来种），全打就淹了。
   判据：`+0x304` 是个像样的武器 id、`+0x308` 是个像样的指针。
   表是环形的，满了覆盖最老的一格。 */
#define PROJ_TRACK_N 64
static struct { void *obj; int handle, ticks; } g_proj_track[PROJ_TRACK_N];
static int g_proj_track_next = 0;

static int proj_is_bullet(unsigned char *p)
{
    int ammo = PI(0x304);
    return ammo >= 1000000 && ammo <= 9999999 && PU(0x308) > 0x10000u;
}

static void proj_track_add(void *proj, int handle)
{
    int i = g_proj_track_next;
    g_proj_track_next = (g_proj_track_next + 1) % PROJ_TRACK_N;
    g_proj_track[i].obj = proj;
    g_proj_track[i].handle = handle;
    g_proj_track[i].ticks = 0;
}

/* ★ **每一次 tick 都打**，不去重 —— 这一版要回答的正是「到底被推进了几次」。
   上一版按位置翻转去重，结果每颗弹体只出现一行，分不清「不动」和「只跑了
   一帧」。子弹寿命就几百毫秒，一颗最多几十行，刷不爆。 */
static void __cdecl proj_tick_log(void *proj)
{
    unsigned char *p = (unsigned char *)proj;
    int i;

    if (!p || IsBadReadPtr(p, 0x340)) return;
    for (i = 0; i < PROJ_TRACK_N; i++) {
        if (g_proj_track[i].obj != proj) continue;
        if (g_proj_track[i].handle != PI(0xD0)) return;   /* 地址被复用了 */
        g_proj_track[i].ticks++;
        /* ★ 走 bsvlog 不走 bslog：这是**每帧 × 每弹体**的，占不起 flush +
           DebugView 那一档（用户 2026-09-01 的掉帧）。 */
        bsvlog("PROJ.   弹体 %08X 句柄 %d owner %d 第%d帧 位置(+34,38)"
               " (%.2f, %.2f) 渲染(+2c,30) (%.2f, %.2f) 速度 (%.2f, %.2f)"
               " 状态 %d 线 %08X",
               (unsigned)(UINT_PTR)p, g_proj_track[i].handle,
               proj_owner_of(g_proj_track[i].handle), g_proj_track[i].ticks,
               PF(0x34), PF(0x38), PF(0x2C), PF(0x30),
               PF(0x120), PF(0x124), PI(0x54), PU(0x30C));
        /* ★ 弹道线 / 拖尾**每帧的内容**：光看「指针非 0」证明不了它被画了
           —— 要看它有没有跟着弹体动。真人和 bot 并排比这几行就够了。 */
        if (PU(0x30C))
            proj_dump_obj(1, "线(+30c)", (void *)(UINT_PTR)PU(0x30C), 20);
        if (PU(0x310))
            proj_dump_obj(1, "拖尾(+310)", (void *)(UINT_PTR)PU(0x310), 20);
        return;
    }
}

#undef PF
#undef PI
#undef PU

/* Add 是 __thiscall(ecx=mgr, [esp+4]=proj)：
   进 detour 时 [esp]=返回地址、[esp+4]=proj；
   pushad(32) + pushfd(4) 之后就是 [esp+0x28]。 */
static __declspec(naked) void proj_add_detour(void)
{
    __asm {
        pushad
        pushfd
        push dword ptr [esp + 0x28]
        call proj_add_log
        add  esp, 4
        popfd
        popad
        jmp  dword ptr [g_proj_add_tramp]
    }
}

/* 每帧 tick 是 __thiscall(ecx=弹体)，没有栈参数。pushad 不动寄存器，
   所以 ecx 在 pushfd 之后仍是原值，直接 push 就行。 */
static __declspec(naked) void proj_tick_detour(void)
{
    __asm {
        pushad
        pushfd
        push ecx
        call proj_tick_log
        add  esp, 4
        popfd
        popad
        jmp  dword ptr [g_proj_tick_tramp]
    }
}

/* -------------------------------------------------------------------------- */
/* `OnFire`（0x491f12）—— 收方**实际解析出来**的那 8 个参数                    */
/*                                                                            */
/*   服务端日志里已经有它发出去的字节了，这里要的是收方**读成了什么**，以及    */
/*   `who` 换算出来的那个角色对象在不在 —— OnFire 里两处按它分流：             */
/*     `esi == 0` 走怪/中立那条（补 EffFire + 按 ctx 的缩放）；                */
/*     `esi != 0` 才播开火动画（`Character::vft+0x13c` = Attack%02d）。        */
/*   另外打一行「这一发该造几颗弹体」的分子分母：外层轮数 = count/SpreadFrags  */
/*   （`0x491fa5` 的整数除法），**商为 0 的话一颗都不造**（0x491fb1 直接跳过   */
/*   整个创建循环），而开火动画照播 —— 那正好是「看得见开枪、看不见子弹」。   */
/* -------------------------------------------------------------------------- */
static void __cdecl proj_fire_log(void *ctx, int who, int slot, int weapon_id,
                                  float *pos, unsigned angle_bits,
                                  unsigned power_bits, int count,
                                  int sender_seat)
{
    int seat = -1;
    UINT_PTR ch = 0;
    float ax, pw, px = 0.0f, py = 0.0f, cx = 0.0f, cy = 0.0f;
    int has_pos = 0, has_char_pos = 0;

    memcpy(&ax, &angle_bits, 4);
    memcpy(&pw, &power_bits, 4);
    if (pos && !IsBadReadPtr(pos, 8)) {
        px = pos[0];
        py = pos[1];
        has_pos = 1;
    }
    if (ctx && who >= 0 && who < 64
        && !IsBadReadPtr((unsigned char *)ctx + 0x244 + who * 4, 4)) {
        UINT_PTR *pp = (UINT_PTR *)MYSEAT_PP;
        seat = *(int *)((unsigned char *)ctx + 0x244 + who * 4);
        if (!IsBadReadPtr(pp, 4) && *pp && seat >= 0 && seat < 8
            && !IsBadReadPtr((void *)(*pp + 0x1D0 + seat * 4), 4))
            ch = *(UINT_PTR *)(*pp + 0x1D0 + seat * 4);
    }
    /* ★★ 射手角色**这一刻**在收方的世界坐标（`[char+0x34]/[0x38]`，§5.6）。
       包里的发射点是服务端算的，角色的位置是收方自己插值 / 积分出来的
       —— 两者要是差得远，子弹就是从一个和角色对不上的地方飞出去的，
       屏幕上当然「看不见 bot 的子弹」。真人自己那一发是同一条链路，
       正好当参照。 */
    if (ch && !IsBadReadPtr((void *)(ch + 0x34), 8)) {
        cx = *(float *)(ch + 0x34);
        cy = *(float *)(ch + 0x38);
        has_char_pos = 1;
    }
    bslog("FIRE>   who %d(座位 %d, 角色 %08X) 发方 %d 槽 %d 武器 %d"
          " 发射点 (%.2f, %.2f) 角度 %.4f 力度 %.3f 颗数(+22) %d"
          "  ← 我的座位 %d",
          who, seat, (unsigned)ch, sender_seat, slot, weapon_id,
          px, py, ax, pw, count, proj_my_seat());
    if (has_char_pos)
        bslog("        ★射手角色位置(+34,38) (%.2f, %.2f)  发射点−角色 "
              "(%+.2f, %+.2f)%s",
              cx, cy, has_pos ? px - cx : 0.0f, has_pos ? py - cy : 0.0f,
              has_pos ? "" : "（包里没有发射点）");
    else
        bslog("        ★射手角色位置 = 读不到（角色对象 %08X）", (unsigned)ch);
}

/* __thiscall(ecx=this) + 8 个栈参数。pushad(0x20)+pushfd(4) 之后
   [esp+0x28] 是第 1 个参数、[esp+0x44] 是第 8 个；每 push 一次 esp 减 4，
   要取的槽位也往上挪 4，所以下面 8 条 `[esp+0x44]` 的偏移**故意都一样**。 */
static __declspec(naked) void proj_fire_detour(void)
{
    __asm {
        pushad
        pushfd
        push dword ptr [esp + 0x44]     /* senderSeat */
        push dword ptr [esp + 0x44]     /* count      */
        push dword ptr [esp + 0x44]     /* power      */
        push dword ptr [esp + 0x44]     /* angle      */
        push dword ptr [esp + 0x44]     /* pos*       */
        push dword ptr [esp + 0x44]     /* weaponId   */
        push dword ptr [esp + 0x44]     /* slot       */
        push dword ptr [esp + 0x44]     /* who        */
        push ecx                        /* this       */
        call proj_fire_log
        add  esp, 0x24
        popfd
        popad
        jmp  dword ptr [g_proj_fire_tramp]
    }
}

/* ========================================================================== */
/* ★★★ 诊断：`Projectile::Move` 的**调用方**是谁（V0.3 §102 / §104）          */
/*                                                                            */
/*   要查的是「弹体撞地形之后的反弹法线」怎么算的。静态分析追到：              */
/*                                                                            */
/*     Move  0x47f603  ── [vft+0x16c] 0x47feab「挡住没」                       */
/*                        └─ [vft+0x168] 0x47f738 扫掠地形                    */
/*                             ↑ 它返回的命中结构里**没有法线**               */
/*                                                                            */
/*   ⇒ 反射一定是 Move 的调用方算的。可 `call [reg+0x158]`（Move 的虚表槽）    */
/*   在整个 exe 里**只有 6 个点，全是 lua_tinker 的绑定桩** —— 真正驱动飞行的  */
/*   那一层静态找不到。                                                       */
/*                                                                            */
/*   所以直接问运行时：在 Move 入口把**返回地址**打出来。按返回地址去重，      */
/*   所以刷不了屏（真正的调用点就那么几个）。                                  */
/*                                                                            */
/*   ★ 顺带把这一发的速度和「这一步走不走得了」一起打 —— 反弹发生的那一        */
/*     tick，速度会在**同一个调用方**里被改写，两行一对比就锁死了。            */
/*                                                                            */
/*   ★ 这是**临时诊断**，查明反弹法线之后连同 detour 一起删。                  */
/*     `BSHOOK_MOVE_DIAG=0` 可以关掉（默认开）。                               */
/* ========================================================================== */
#define PROJ_MOVE_VA  0x0047F603u   /* BulletObj vft+0x158：Move __thiscall    */

/* 0x47f603  55 / 8b ec / 83 ec 18   ← 1+2+3 = 6 字节，和 PROJ_TICK 同一形状 */
static const unsigned char PROJ_MOVE_SIG[6] = { 0x55, 0x8B, 0xEC, 0x83, 0xEC, 0x18 };

static void *g_proj_move_tramp = NULL;

#define MOVE_CALLER_MAX 16
static unsigned g_move_callers[MOVE_CALLER_MAX];
static int g_move_caller_n = 0;

/* 同 proj_diag_enabled：默认跟日志级别走。这一档本身按返回地址去重、全程 ≤16 行，
   量可以忽略，但它挂的是 `Projectile::Move` —— 每帧每弹体都要过一次 detour 跳板。 */
static int move_diag_enabled(void)
{
    char buf[8];
    DWORD n = GetEnvironmentVariableA("BSHOOK_MOVE_DIAG", buf, sizeof(buf));
    if (n == 0 || n >= sizeof(buf)) return g_verbose ? 1 : 0;   /* 没设 = 跟日志级别 */
    return buf[0] != '0';
}

static void __cdecl proj_move_log(void *proj, unsigned ret_addr)
{
    unsigned char *p = (unsigned char *)proj;
    int i;

    for (i = 0; i < g_move_caller_n; i++)
        if (g_move_callers[i] == ret_addr) return;     /* 见过了，别刷屏 */
    if (g_move_caller_n < MOVE_CALLER_MAX)
        g_move_callers[g_move_caller_n++] = ret_addr;

    if (!p || IsBadReadPtr(p, 0x340)) {
        bslog("MOVE>   ★Projectile::Move 的调用方 返回地址 %08X（弹体读不了）",
              ret_addr);
        return;
    }
    bslog("MOVE>   ★Projectile::Move 的调用方 返回地址 %08X"
          " | 弹体 %08X vft %08X 句柄 %d 位置 (%.2f, %.2f) 速度 (%.3f, %.3f)"
          "  ← 反弹法线就在这个调用方里算（§102）",
          ret_addr, (unsigned)(UINT_PTR)p,
          (unsigned)*(UINT_PTR *)(p + 0x00), *(int *)(p + 0xD0),
          *(float *)(p + 0x34), *(float *)(p + 0x38),
          *(float *)(p + 0x120), *(float *)(p + 0x124));
}

/* __thiscall(ecx=弹体) + 一个栈参数。pushad 之后返回地址在 [esp+0x20]。 */
static __declspec(naked) void proj_move_detour(void)
{
    __asm {
        pushad
        pushfd
        push dword ptr [esp + 0x24]
        push ecx
        call proj_move_log
        add  esp, 8
        popfd
        popad
        jmp  dword ptr [g_proj_move_tramp]
    }
}

static int try_patch_move_diag(void)
{
    unsigned char *m = (unsigned char *)PROJ_MOVE_VA;

    if (g_proj_move_tramp != NULL) return 1;
    if (!move_diag_enabled()) return 1;
    if (IsBadReadPtr(m, sizeof(PROJ_MOVE_SIG))) return 0;
    if (memcmp(m, PROJ_MOVE_SIG, sizeof(PROJ_MOVE_SIG)) != 0) return 0;
    g_proj_move_tramp = install_inline_hook((void *)PROJ_MOVE_VA,
                                            proj_move_detour,
                                            "Move 调用方诊断");
    if (!g_proj_move_tramp) return 0;
    bslog("PATCH   ★Move 调用方诊断已装 @ %08X：每个**新**的返回地址打一行"
          "（按地址去重，不刷屏）—— 查反弹法线在谁那里算"
          "（BSHOOK_MOVE_DIAG=0 可关）", (unsigned)PROJ_MOVE_VA);
    return 1;
}

static int try_patch_proj_diag(void)
{
    unsigned char *a = (unsigned char *)PROJ_ADD_VA;
    unsigned char *t = (unsigned char *)PROJ_TICK_VA;

    if (g_proj_diag_patched) return 1;
    if (IsBadReadPtr(a, sizeof(PROJ_ADD_SIG))
        || IsBadReadPtr(t, sizeof(PROJ_TICK_SIG))) return 0;
    if (g_proj_add_tramp == NULL) {
        if (memcmp(a, PROJ_ADD_SIG, sizeof(PROJ_ADD_SIG)) != 0)
            return 0;                      /* 还没解壳到这里，或不是这个版本 */
        g_proj_add_tramp = install_inline_hook((void *)PROJ_ADD_VA,
                                               proj_add_detour,
                                               "弹体登记诊断");
        if (!g_proj_add_tramp) return 0;
    }
    if (g_proj_tick_tramp == NULL) {
        if (memcmp(t, PROJ_TICK_SIG, sizeof(PROJ_TICK_SIG)) != 0)
            return 0;
        g_proj_tick_tramp = install_inline_hook((void *)PROJ_TICK_VA,
                                                proj_tick_detour,
                                                "弹体推进诊断");
        if (!g_proj_tick_tramp) return 0;
    }
    if (g_proj_fire_tramp == NULL) {
        unsigned char *f = (unsigned char *)PROJ_FIRE_VA;
        if (IsBadReadPtr(f, sizeof(PROJ_FIRE_SIG))
            || memcmp(f, PROJ_FIRE_SIG, sizeof(PROJ_FIRE_SIG)) != 0)
            return 0;
        g_proj_fire_tramp = install_inline_hook((void *)PROJ_FIRE_VA,
                                                proj_fire_detour,
                                                "OnFire 参数诊断");
        if (!g_proj_fire_tramp) return 0;
    }
    InterlockedExchange(&g_proj_diag_patched, 1);
    bslog("PATCH   ★弹体诊断已装 @ %08X / %08X / %08X：收到 rpFire 打一行"
          "解析出来的参数，每颗弹体登记时打一份全字段快照，之后按整数位置"
          "翻转打轨迹（BSHOOK_PROJ_DIAG=0 可关）",
          (unsigned)PROJ_FIRE_VA, (unsigned)PROJ_ADD_VA, (unsigned)PROJ_TICK_VA);
    return 1;
}

/* -------------------------------------------------------------------------- */
/* ★ 远端角色逐帧诊断（V0.3 会话 68 / §191）                                     */
/*                                                                            */
/*   每帧每个**远端座位**的角色打一行：位置 / 速度 / 踩地 / 走路方向 / 方向键 /   */
/*   冲刺 / 运动约束。hook 在 0x50d404（`Character` 每帧按速度推位置的那一发，    */
/*   0x507775 直接 call，__thiscall ecx=角色）：它在本帧的行走（0x507660）之后、  */
/*   腾空积分之前跑。心跳解码器 0x5041e1（esi=角色）再打一行「收到心跳前」的     */
/*   快照 —— 和它后面那行 CHAR. 一比，就是这一发心跳把角色拽了多远。            */
/*                                                                            */
/*   这是 bot 卡顿调查缺了十几轮的那份「收方到底画在哪」的证据：以前只有开火     */
/*   那一刻的位置（FIRE>，一局几百发），这个是每帧的。                          */
/*   默认跟日志级别走（同 PROJ.）；`BSHOOK_CHAR_DIAG=1` 在精简模式下也能强制开。  */
/* -------------------------------------------------------------------------- */
#define CHAR_TICK_VA  0x0050D404u   /* push ebp; mov ebp,esp; sub esp,38; push esi; mov esi,ecx */
static const unsigned char CHAR_TICK_SIG[] = { 0x55, 0x8B, 0xEC, 0x83, 0xEC, 0x38, 0x56, 0x8B, 0xF1 };
#define CHAR_READ_VA  0x005041E1u   /* push ebp; mov ebp,esp; sub esp,1c; push 18 */
static const unsigned char CHAR_READ_SIG[] = { 0x55, 0x8B, 0xEC, 0x83, 0xEC, 0x1C, 0x6A, 0x18 };
static void *g_char_tick_tramp = NULL;
static void *g_char_read_tramp = NULL;
static volatile LONG g_char_diag_patched = 0;

static int char_diag_enabled(void)
{
    char buf[8];
    DWORD n = GetEnvironmentVariableA("BSHOOK_CHAR_DIAG", buf, sizeof(buf));
    if (n == 0 || n >= sizeof(buf)) return g_verbose ? 1 : 0;   /* 没设 = 跟日志级别 */
    return buf[0] != '0';
}

/* 这个对象是不是「座位表里登记着的远端角色」：会话 +0x1d0+座位*4 指着它，
   而且座位不是我的。掉落物 / 弹体 / 怪也走 0x50d404，一律不打。 */
static int char_remote_seat(unsigned char *p)
{
    UINT_PTR *pp = (UINT_PTR *)MYSEAT_PP;
    UINT_PTR ctx;
    int seat, mine;
    if (IsBadReadPtr(pp, 4)) return -1;
    ctx = *pp;
    if (!ctx || IsBadReadPtr((void *)(ctx + 0x1CC), 4 + 4 * 6)) return -1;
    seat = *(int *)(p + 0x2AC);
    mine = *(int *)(ctx + 0x1CC);
    if (seat < 0 || seat >= 6 || seat == mine) return -1;
    if (*(UINT_PTR *)(ctx + 0x1D0 + seat * 4) != (UINT_PTR)p) return -1;
    return seat;
}

#define CF(off) (*(float *)(p + (off)))
#define CI(off) (*(int   *)(p + (off)))
#define CB(off) (*(unsigned char *)(p + (off)))

static void __cdecl char_tick_log(void *obj)
{
    unsigned char *p = (unsigned char *)obj;
    int seat;
    if (!p || IsBadReadPtr(p, 0x4D0)) return;
    seat = char_remote_seat(p);
    if (seat < 0) return;
    /* 走 bsvlog：每帧 × 每个远端角色，占不起 flush + DebugView 那一档。 */
    bsvlog("CHAR.   座位 %d 角色 %08X 位置(+34,38) (%.2f, %.2f) 速度(+120,124)"
           " (%.3f, %.3f) 踩地(+128) %d 走向(+4b4) %d 键 %c%c%c%c 冲刺(+4bc) %d"
           " 约束(+164) %d/%d 蹲(+2b5) %d",
           seat, (unsigned)(UINT_PTR)p, CF(0x34), CF(0x38), CF(0x120), CF(0x124),
           CB(0x128), CI(0x4B4),
           (CB(0x2B8) & 1) ? 'L' : '-', (CB(0x2BC) & 1) ? 'U' : '-',
           (CB(0x2C0) & 1) ? 'R' : '-', (CB(0x2C4) & 1) ? 'D' : '-',
           CB(0x4BC), CI(0x164), CI(0x168), CB(0x2B5));
}

static void __cdecl char_read_log(void *obj)
{
    unsigned char *p = (unsigned char *)obj;
    int seat;
    if (!p || IsBadReadPtr(p, 0x4D0)) return;
    seat = char_remote_seat(p);
    if (seat < 0) return;
    bsvlog("HB<     座位 %d 角色 %08X 收心跳前 位置 (%.2f, %.2f) 速度 (%.3f, %.3f)"
           " 踩地 %d 约束 %d",
           seat, (unsigned)(UINT_PTR)p, CF(0x34), CF(0x38), CF(0x120), CF(0x124),
           CB(0x128), CI(0x164));
}
#undef CF
#undef CI
#undef CB

/* 0x50d404 是 __thiscall(ecx=角色)，没有栈参数；pushad 不动 ecx。 */
static __declspec(naked) void char_tick_detour(void)
{
    __asm {
        pushad
        pushfd
        push ecx
        call char_tick_log
        add  esp, 4
        popfd
        popad
        jmp  dword ptr [g_char_tick_tramp]
    }
}

/* 0x5041e1 的 this 在 esi（0x5041f2 起全程 `[esi+…]`），栈上是解码器自己的参数。 */
static __declspec(naked) void char_read_detour(void)
{
    __asm {
        pushad
        pushfd
        push esi
        call char_read_log
        add  esp, 4
        popfd
        popad
        jmp  dword ptr [g_char_read_tramp]
    }
}

static int try_patch_char_diag(void)
{
    unsigned char *t = (unsigned char *)CHAR_TICK_VA;
    unsigned char *r = (unsigned char *)CHAR_READ_VA;

    if (g_char_diag_patched) return 1;
    if (IsBadReadPtr(t, sizeof(CHAR_TICK_SIG))
        || IsBadReadPtr(r, sizeof(CHAR_READ_SIG))) return 0;
    if (g_char_tick_tramp == NULL) {
        if (memcmp(t, CHAR_TICK_SIG, sizeof(CHAR_TICK_SIG)) != 0)
            return 0;                      /* 还没解壳到这里，或不是这个版本 */
        g_char_tick_tramp = install_inline_hook((void *)CHAR_TICK_VA,
                                                char_tick_detour,
                                                "远端角色逐帧诊断");
        if (!g_char_tick_tramp) return 0;
    }
    if (g_char_read_tramp == NULL) {
        if (memcmp(r, CHAR_READ_SIG, sizeof(CHAR_READ_SIG)) != 0)
            return 0;
        g_char_read_tramp = install_inline_hook((void *)CHAR_READ_VA,
                                                char_read_detour,
                                                "远端角色收心跳诊断");
        if (!g_char_read_tramp) return 0;
    }
    InterlockedExchange(&g_char_diag_patched, 1);
    bslog("PATCH   ★远端角色逐帧诊断已装 @ %08X / %08X：每帧每个远端座位打一行"
          " CHAR.，每收一发心跳打一行 HB<（BSHOOK_CHAR_DIAG=0 可关）",
          (unsigned)CHAR_TICK_VA, (unsigned)CHAR_READ_VA);
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

    /* 玩家等级门槛（D22）：和地图那组同一个道理，不赶时机 —— 七处判据最早
       也要等玩家点进大厅/建房对话框才执行。★ 这组**必须**打上，否则服务端
       改发真实等级之后，1~3 级号会退回原版的对战锁。 */
    if (player_level_lock_kept()) {
        bslog("PATCH   BSHOOK_KEEP_PLAYER_LEVEL_LOCK 已设，保留原版玩家等级门槛"
              "（1 级号会进不了对战频道、选不了生存模式和 5/6 人房）");
    } else {
        for (ticks = 0; !g_stop && !g_plr_lvl_patched && ticks < 2000; ticks++) {
            if (try_patch_player_level_gate()) break;
            Sleep(2);
        }
        if (!g_plr_lvl_patched)
            bslog("PATCH   !! 超时未能 patch 玩家等级门槛"
                  "（0x440b05 / 0x440cd6 / 0x465338 / 0x465a2c / 0x4374c9 / "
                  "0x54f8f2 / 0x43b676 的特征串一直对不上）");
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

    /* 溅射加成提示判空（V0.3 合成与商店 §47 / D55）：穿着 IncSplashRange 装备
       （火焰蝙蝠 220003）用溅射武器打空，15% 概率整个客户端闪退。
       和 IME 那组一样不赶时机，只等特征串出现。 */
    if (splash_visual_crash_keep_original()) {
        bslog("PATCH   BSHOOK_KEEP_SPLASH_VISUAL_CRASH 已设，保留原版溅射加成提示"
              "（打空会闪退）");
    } else {
        for (ticks = 0; !g_stop && !g_splash_visual_patched && ticks < 2000; ticks++) {
            if (try_patch_splash_visual_guard()) break;
            Sleep(2);
        }
        if (!g_splash_visual_patched)
            bslog("PATCH   !! 超时未能 patch 溅射加成提示判空"
                  "（0x47EA21 特征串一直对不上）");
    }

    /* 突击技加成提示判空（§53）：和上面那个完全同型，只是补丁点在 DashDamage
       的绘制虚表槽里。带 DashAttack 的只有已上架的 220004 迷你机械青蛙。 */
    if (dash_visual_crash_keep_original()) {
        bslog("PATCH   BSHOOK_KEEP_DASH_VISUAL_CRASH 已设，保留原版突击技加成提示");
    } else {
        for (ticks = 0; !g_stop && !g_dash_visual_patched && ticks < 2000; ticks++) {
            if (try_patch_dash_visual_guard()) break;
            Sleep(2);
        }
        if (!g_dash_visual_patched)
            bslog("PATCH   !! 超时未能 patch 突击技加成提示判空"
                  "（0x481EF3 特征串一直对不上）");
    }

    /* 关掉客户端自己画的那行绿色加成文字（§59 / D65）：和服务端下发的说明文重复，
       而且原版那一行有错字（速度+d%）/ 漏翻（Team…Down）/ 溢出三个毛病。 */
    if (client_bonus_text_keep_original()) {
        bslog("PATCH   BSHOOK_KEEP_CLIENT_BONUS_TEXT 已设，保留原版那行绿色加成文字");
    } else {
        for (ticks = 0; !g_stop && !g_bonus_text_patched && ticks < 2000; ticks++) {
            if (try_patch_hide_client_bonus_text()) break;
            Sleep(2);
        }
        if (!g_bonus_text_patched)
            bslog("PATCH   !! 超时未能 patch 客户端加成绿字"
                  "（0x41414D 特征串一直对不上）");
    }

    /* 礼物盒里材料没图标（V0.3商店 §75，2026-09-10 实机）：礼物槽 / 接收弹窗按 id
       查图标查的是 [Stock-] 那张表，材料只在 [Item-] 那张。查不到就退回第二张。 */
    if (gift_icon_miss_keep_original()) {
        bslog("PATCH   BSHOOK_KEEP_GIFT_ICON_MISS 已设，保留原版礼物盒图标查表"
              "（材料礼物画成 (FileNotFound)）");
    } else {
        for (ticks = 0; !g_stop && !g_icon_lookup_patched && ticks < 2000; ticks++) {
            if (try_patch_gift_icon_fallback()) break;
            Sleep(2);
        }
        if (!g_icon_lookup_patched)
            bslog("PATCH   !! 超时未能 patch 礼物盒图标退回物品表"
                  "（0x4169E9 特征串一直对不上）");
    }

    /* 岩浆巨龙（Quest02）弱点剧情的窗口（用户 2026-09-10 线上报的卡关）：
       原版只在 boss 正放出生动画那 3.84 秒里才肯放 Tracing，窗口一错过
       boss 从此免疫伤害。判据换成「弱点还没点出来」。 */
    if (draka_tracing_keep_original()) {
        bslog("PATCH   BSHOOK_KEEP_DRAKA_TRACING_RACE 已设，保留原版岩浆巨龙"
              "弱点剧情窗口（有概率永久卡关）");
    } else {
        for (ticks = 0; !g_stop && !g_draka_tracing_patched && ticks < 2000; ticks++) {
            if (try_patch_draka_tracing_gate()) break;
            Sleep(2);
        }
        if (!g_draka_tracing_patched)
            bslog("PATCH   !! 超时未能 patch 岩浆巨龙弱点剧情"
                  "（0x4B5A04 特征串一直对不上）");
    }

    /* 岩浆巨龙诊断日志（会话 27）：boss 创建 / 门开 / Tracing 入队 / 弱点点出，
       一局几行、任何日志级别都记 —— 下次卡关靠它分辨病在哪、窗口被吃掉多少。 */
    if (!draka_diag_enabled()) {
        bslog("PATCH   不装岩浆巨龙诊断（BSHOOK_DRAKA_DIAG=0）");
    } else {
        for (ticks = 0; !g_stop && !g_draka_diag_patched && ticks < 2000; ticks++) {
            if (try_patch_draka_diag()) break;
            Sleep(2);
        }
        if (!g_draka_diag_patched)
            bslog("PATCH   !! 超时未能装岩浆巨龙诊断"
                  "（0x4B1812 / 0x4A70B4 / 0x4A712D / 0x4A76C9 特征串一直对不上）");
    }

    /* BigShot.rpt 里三种旧闪退的守护（§48 / §49 / §50，D56）：教程弹窗时大厅为空、
       退出拆到一半又收到 WM_CLOSE、蒙皮记录没绑上骨骼。同样只等特征串出现。 */
    if (rpt_crashes_keep_original()) {
        bslog("PATCH   BSHOOK_KEEP_RPT_CRASHES 已设，保留原版三处空指针行为");
    } else {
        for (ticks = 0; !g_stop && !g_rpt_guards_patched && ticks < 2000; ticks++) {
            if (try_patch_rpt_crash_guards()) break;
            Sleep(2);
        }
        if (!g_rpt_guards_patched)
            bslog("PATCH   !! 超时未能 patch 旧闪退守护"
                  "（0x40F4EA / 0x40EF90 / 0x5D27F1 特征串一直对不上）");
    }

    /* bug调查/17 那一批（§65 / §66 / §67）：崩溃报告器自己判空（拿得到现场的前提）、
       突击技对象野指针校验、gspEndGame 没有 GameContext 时判空。
       和上面三处共用 BSHOOK_KEEP_RPT_CRASHES 开关。 */
    if (rpt_crashes_keep_original()) {
        bslog("PATCH   BSHOOK_KEEP_RPT_CRASHES 已设，bug调查/17 那五处守护一并不装");
    } else {
        for (ticks = 0; !g_stop && !g_crash17_guards_patched && ticks < 2000; ticks++) {
            if (try_patch_crash17_guards()) break;
            Sleep(2);
        }
        if (!g_crash17_guards_patched)
            bslog("PATCH   !! 超时未能 patch bug调查/17 的崩溃守护"
                  "（0x5D76B4 / 0x502182 / 0x50794A / 0x5079AA / 0x5518DE 特征串一直对不上）");
    }

    /* bug调查/18（§78）：换图卸完场景后清掉 [角色+0x57C/0x580] —— §66 那个野指针
       的根因。和上面那五处共用 BSHOOK_KEEP_DASH_STALE_CRASH / KEEP_RPT_CRASHES 开关。 */
    if (rpt_crashes_keep_original() || dash_stale_keep_original()) {
        bslog("PATCH   逃生门已设，不装「换图清突击技野指针」（bug调查/18）");
    } else {
        for (ticks = 0; !g_stop && !g_mapchg_clear_patched && ticks < 2000; ticks++) {
            if (try_patch_mapchange_dangling()) break;
            Sleep(2);
        }
        if (!g_mapchg_clear_patched)
            bslog("PATCH   !! 超时未能 patch 换图野指针根治"
                  "（0x4790CB 特征串一直对不上）");
    }

    /* bug调查/18（§80）：把 NEXON 那套信使整个关掉 —— `nmconew.dll` 不让加载，
       `NMService.exe` 跟着就不会被拉起来。用户 2026-09-10 拍板「没用可以不要」。
       ★ 等的是 `nmcogame.dll` 出现（它是 BigShot.exe 的静态导入，解壳后就在），
       而真正要拦的那一发发生在**玩家点登录**的时候，远在后面。 */
    if (nm_keep_original()) {
        bslog("PATCH   BSHOOK_KEEP_NM 已设，保留 NEXON 信使（nmconew.dll 照常加载）");
    } else {
        for (ticks = 0; !g_stop && !g_nm_blocked && ticks < 2000; ticks++) {
            if (try_block_nexon_messenger()) break;
            Sleep(2);
        }
        if (!g_nm_blocked)
            bslog("PATCH   !! 超时未能关掉 NEXON 信使（nmcogame.dll 一直没出现）");
    }

    /* BSM1 is a functional patch, independent of diagnostic log settings. */
    if (!try_patch_bot_motion())
        bslog("BSM1    !! 运动 hook 特征不匹配，保留原版处理；需要检查客户端版本");

    /* ★ M3b 诊断：弹体全字段快照（临时，查完「看不见 bot 的子弹」就删）。
       两个 hook 的目标函数都在战斗里才第一次跑，远晚于解壳窗口。 */
    if (!proj_diag_enabled()) {
        bslog("PATCH   不装弹体诊断 hook（每帧每弹体都要跑，精简模式默认关；"
              "要查弹体设 BSHOOK_PROJ_DIAG=1）");
    } else {
        for (ticks = 0; !g_stop && !g_proj_diag_patched && ticks < 2000; ticks++) {
            if (try_patch_proj_diag()) break;
            Sleep(2);
        }
        if (!g_proj_diag_patched)
            bslog("PATCH   !! 超时未能装弹体诊断"
                  "（0x473e7c / 0x47de6a 的特征串一直对不上）");
    }

    /* ★ 远端角色逐帧诊断（V0.3 会话 68）：查 bot 卡顿要的「收方到底画在哪」。 */
    if (!char_diag_enabled()) {
        bslog("PATCH   不装远端角色逐帧诊断 hook（每帧每个远端角色一行，精简模式"
              "默认关；要查 bot 同步设 BSHOOK_CHAR_DIAG=1）");
    } else {
        for (ticks = 0; !g_stop && !g_char_diag_patched && ticks < 2000; ticks++) {
            if (try_patch_char_diag()) break;
            Sleep(2);
        }
        if (!g_char_diag_patched)
            bslog("PATCH   !! 超时未能装远端角色逐帧诊断"
                  "（0x50d404 / 0x5041e1 的特征串一直对不上）");
    }

    /* ★ 反弹法线诊断（临时，V0.3 §102）：查 Move 的调用方是谁。 */
    if (!move_diag_enabled()) {
        bslog("PATCH   不装 Move 调用方诊断 hook（精简模式默认关；"
              "要查反弹法线设 BSHOOK_MOVE_DIAG=1）");
    } else {
        int done = 0;
        for (ticks = 0; !g_stop && !done && ticks < 2000; ticks++) {
            if (try_patch_move_diag()) { done = 1; break; }
            Sleep(2);
        }
        if (!done)
            bslog("PATCH   !! 超时未能装 Move 调用方诊断"
                  "（0x47f603 一直不是 55 8B EC 83 EC 18）");
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

/* -------------------------------------------------------------------------- */
/* 地址空间快照 —— 心跳里带一行，用来判「是不是 32 位地址空间吃紧了」          */
/*                                                                            */
/*   bug调查/17 那五次线上闪退里，09930195 那份 dump 的线程栈一路铺到          */
/*   0x4F09xxxx、崩的那个对象落在 0x3CB7xxxx —— 看着像地址空间已经用到 1.2 GB，*/
/*   但**当时没有任何一个数能证实**（Vista 起线程栈本来就是随机落位的）。      */
/*   32 位进程只有 2 GB，真正先出事的是「最大连续空闲块」而不是总量：块一小，  */
/*   new 就开始返回 NULL，而这个 2007 年的引擎有大把地方不判 NULL。            */
/*   心跳本来就每 30 秒一行，顺手把三个数带上，下次崩溃前后一眼可判。          */
/* -------------------------------------------------------------------------- */
static void vm_snapshot(SIZE_T *commit, SIZE_T *reserve, SIZE_T *largest_free)
{
    MEMORY_BASIC_INFORMATION mbi;
    unsigned char *p = NULL;
    SIZE_T c = 0, r = 0, f = 0;

    while (VirtualQuery(p, &mbi, sizeof(mbi)) == sizeof(mbi)) {
        if (mbi.State == MEM_COMMIT)       c += mbi.RegionSize;
        else if (mbi.State == MEM_RESERVE) r += mbi.RegionSize;
        else if (mbi.State == MEM_FREE && mbi.RegionSize > f) f = mbi.RegionSize;
        if (mbi.RegionSize == 0) break;
        if ((SIZE_T)(p + mbi.RegionSize) <= (SIZE_T)p) break;   /* 走到顶了 */
        p += mbi.RegionSize;
    }
    *commit = c;
    *reserve = r;
    *largest_free = f;
}

static DWORD WINAPI watch_thread(LPVOID param)
{
    int ticks = 0;
    (void)param;

    install_hooks();   /* user32 此时必已加载；失败会自动重置标志下一轮重试 */
    while (!g_stop) {
        if (!g_hooks_installed) install_hooks();
        install_shell_hooks();   /* shell32 是后加载的，装上为止每轮试一次 */
        install_process_hooks(); /* CreateProcess/WinExec：升级分支拉起更新器 */
        report_gameguard_breakpoint();
        poll_modules();
        EnumWindows(dump_window, 0);
        poll_login_dialog();   /* V0.2：分区单选钮 + 注册链接（里程碑 H）*/
        poll_unpack();
        Sleep(100);
        if (++ticks % 300 == 0) {
            SIZE_T commit = 0, reserve = 0, largest = 0;
            vm_snapshot(&commit, &reserve, &largest);
            bslog("--- still alive (%d s)；地址空间 已提交 %u MB / 已保留 %u MB"
                  " / 最大空闲块 %u MB ---",
                  ticks / 10, (unsigned)(commit >> 20),
                  (unsigned)(reserve >> 20), (unsigned)(largest >> 20));
        }
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

    /* ★ 写盘线程。自动重置事件 —— 生产者只在「环从空变非空」时置位。
       线程要等 DllMain 返回才真正跑起来（加载器锁），所以 DllMain 里那几条
       banner / HWBP 会先在环里躺一小会儿，正常。DllMain 走失败分支直接
       return FALSE 时由 bslog_shutdown() 就地排空，一条都不会丢。 */
    g_log_evt = CreateEventA(NULL, FALSE, FALSE, NULL);
    if (g_log_evt) {
        g_log_thread = CreateThread(NULL, 0, log_writer_thread, NULL, 0, NULL);
        if (g_log_thread) {
            CloseHandle(g_log_thread);
            g_log_thread = NULL;
        }
    }
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
            bslog_shutdown();   /* 写线程还没跑起来，就地把环排空 */
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
            bslog_shutdown();
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
            bslog_shutdown();
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
        if (g_skin_null_bone_skips)
            bslog("PATCH   蒙皮骨骼判空：这次运行跳过了 %ld 条没绑上骨骼的蒙皮记录"
                  "（有装备模型和角色骨架不配，查 items.json 的角色限定 / D31a）",
                  (long)g_skin_null_bone_skips);
        bslog("================ process detach ================");
        /* ★ 写线程这时候多半已经被系统干掉了（进程退出时先杀线程再 DETACH），
           所以在**当前**线程上就地把环排空 —— 否则最后那几条永远出不去。 */
        bslog_shutdown();
        break;
    }
    return TRUE;
}
