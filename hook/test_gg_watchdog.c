/*
 * GameGuard DR0 守护回归夹具。
 *
 * 由 test-watchdog.bat 编译成临时 32 位进程，再通过正式 bsloader 注入
 * bshook.dll。夹具在 0x54b0fc 放入客户端原始 call，等待 DLL 武装 DR0，
 * 主动清掉一次调试寄存器，然后执行该地址：
 *   - 守护逻辑恢复 DR0，VEH 返回 0x755 -> exit 0
 *   - DR0 没恢复，原 call 返回 0             -> exit 5
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include "gg_bypass.h"

#define TARGET_PAGE ((LPVOID)0x0054B000u)
#define STUB_PAGE   ((LPVOID)0x00561000u)

/* 让 PE 映像在进程启动之初就覆盖两个固定地址，避免 bshook 的 trampoline
   先占用这段空闲地址。测试脚本关闭 ASLR 并固定 ImageBase=0x00400000。 */
static volatile unsigned char g_image_reservation[0x180000];

static HANDLE        g_cleared_event = NULL;
static DWORD         g_main_thread_id = 0;
static volatile LONG g_cleared = 0;

/* 等 bshook.dll 自己那条武装线程报「已武装」。
   ★ 为什么必须等（V0.2 会话 15）：bsloader 现在也会从进程外武装 DR0 并守护，
   一直守到收到这个握手为止。不等的话，被清掉的 DR0 可能是**bsloader**补回来的，
   本夹具就测不到「DLL 内的守护线程」这件事了。 */
static void wait_for_dll_armed(void)
{
    char name[128];
    DWORD n = GetEnvironmentVariableA(POPSHOT_BSHOOK_READY_ENV, name, sizeof(name));
    HANDLE ready;

    if (n == 0 || n >= sizeof(name)) return;
    ready = OpenEventA(SYNCHRONIZE, FALSE, name);
    if (!ready) return;
    WaitForSingleObject(ready, 5000);
    CloseHandle(ready);
}

static DWORD WINAPI clear_breakpoint_once(LPVOID unused)
{
    HANDLE main_thread;
    DWORD started;
    (void)unused;

    wait_for_dll_armed();
    started = GetTickCount();

    main_thread = OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT |
                             THREAD_SET_CONTEXT, FALSE, g_main_thread_id);
    if (!main_thread) {
        SetEvent(g_cleared_event);
        return 1;
    }

    while ((DWORD)(GetTickCount() - started) < 5000u) {
        CONTEXT ctx;
        if (SuspendThread(main_thread) == (DWORD)-1) break;

        ZeroMemory(&ctx, sizeof(ctx));
        ctx.ContextFlags = CONTEXT_DEBUG_REGISTERS;
        if (GetThreadContext(main_thread, &ctx) &&
            ctx.Dr0 == (DWORD)POPSHOT_GG_CHECK_VA &&
            (ctx.Dr7 & (DWORD)POPSHOT_DR0_LOCAL_ENABLE) != 0) {
            ctx.Dr0 = 0;
            ctx.Dr6 = 0;
            ctx.Dr7 &= ~(DWORD)POPSHOT_DR0_CONTROL_MASK;
            if (SetThreadContext(main_thread, &ctx))
                InterlockedExchange(&g_cleared, 1);
            ResumeThread(main_thread);
            break;
        }

        ResumeThread(main_thread);
        Sleep(1);
    }

    CloseHandle(main_thread);
    SetEvent(g_cleared_event);
    return g_cleared ? 0 : 1;
}

int main(void)
{
    static const unsigned char target_code[] = {
        0xE8, 0xCF, 0x60, 0x01, 0x00, /* call 0x005611d0 */
        0xC3                          /* ret               */
    };
    static const unsigned char failure_stub[] = {
        0x33, 0xC0, /* xor eax,eax */
        0xC3        /* ret         */
    };
    HANDLE worker;
    DWORD result;
    DWORD old_protect;

    g_main_thread_id = GetCurrentThreadId();
    g_image_reservation[0] = 1;
    g_image_reservation[sizeof(g_image_reservation) - 1] = 1;
    if (!VirtualProtect(TARGET_PAGE, 0x1000, PAGE_EXECUTE_READWRITE, &old_protect))
        return 2;
    if (!VirtualProtect(STUB_PAGE, 0x1000, PAGE_EXECUTE_READWRITE, &old_protect))
        return 2;

    CopyMemory((LPVOID)POPSHOT_GG_CHECK_VA, target_code, sizeof(target_code));
    CopyMemory((LPVOID)0x005611D0u, failure_stub, sizeof(failure_stub));
    FlushInstructionCache(GetCurrentProcess(), TARGET_PAGE, 0x1000);
    FlushInstructionCache(GetCurrentProcess(), STUB_PAGE, 0x1000);

    g_cleared_event = CreateEventA(NULL, TRUE, FALSE, NULL);
    if (!g_cleared_event) return 3;
    worker = CreateThread(NULL, 0, clear_breakpoint_once, NULL, 0, NULL);
    if (!worker) return 3;

    if (WaitForSingleObject(g_cleared_event, 6000) != WAIT_OBJECT_0 || !g_cleared)
        return 4;

    /* 给 10ms 周期的守护线程充足时间观察并恢复被清掉的 DR0。 */
    Sleep(100);
    result = ((DWORD (WINAPI *)(void))POPSHOT_GG_CHECK_VA)();
    Sleep(150); /* 让 bshook 的 100ms 观测循环把“DR0 命中”写入日志。 */

    WaitForSingleObject(worker, 1000);
    CloseHandle(worker);
    CloseHandle(g_cleared_event);
    return result == (DWORD)POPSHOT_GG_SUCCESS_CODE ? 0 : 5;
}
