/* ==========================================================================
 * bsloader.exe —— 拉起 BigShot.exe 并把 bshook.dll 注进去
 *
 * 注入手法：CREATE_SUSPENDED + QueueUserAPC(LoadLibraryA)
 *
 *   为什么不用 CreateRemoteThread：CREATE_SUSPENDED 的进程里只映射了 ntdll，
 *   kernel32 还没加载，直接 CreateRemoteThread(LoadLibraryA) 会崩。
 *
 *   为什么 APC 可行：ntdll 的 LdrInitializeThunk 在完成进程初始化（= 所有静态
 *   导入 DLL 都已加载）之后、跳到 EXE 入口点之前会调用 NtTestAlert，
 *   这时挂起的 APC 就被投递执行。于是 bshook.dll 的加载时机正好卡在
 *   「静态导入全部就绪」和「ASProtect 壳开始跑」之间 —— 这正是我们要的。
 *
 * GameGuard 绕过不用固定延时改代码：bshook.dll 先注册 VEH，等目标校验指令
 * **真正解壳**后设置并守护 DR0。主线程执行到状态读取指令时，VEH 直接改
 * EAX/EIP；游戏代码字节始终不变。
 *
 * ★ V0.2 会话 15（§179）：DR0 现在**两边一起武装**。
 *   DLL 里那条武装线程是 DllMain 里 CreateThread 出来的，线程入口要等加载器锁
 *   放开才会被调度到 —— 实测别人的机器上三条 DLL 线程一起卡了 3.3 秒，
 *   而 GameGuard 校验在那之后只有 2.2 秒就执行了。这段余量完全看运气，
 *   于是「有概率启动报错 Game guard 文件不存在或已变更」。
 *   bsloader 是**另一个进程**，不受目标进程的加载器锁约束：它在 DLL 报
 *   「VEH 已装好」之后就开始隔 2ms 读一次 0x54b0fc，一解壳就从外部
 *   SuspendThread + SetThreadContext 把 DR0 摆上去，直到 DLL 那条线程
 *   跑起来接管为止。两边写的是同一个值，抢在一起也没有副作用。
 *
 * ★ 还留了一层兜底：绕过真失败了（DLL 报 FAILED，或者进程在 DR0 命中前就退了）
 *   就整体重来一次，最多 POPSHOT_GG_MAX_ATTEMPTS 次。前几次把客户端那个
 *   错误框吃掉（玩家只看到窗口闪一下），最后一次照常弹出来。
 *
 * 用法：
 *   bsloader.exe                    用默认目标 <项目根>\game_patched\BigShot.exe
 *   bsloader.exe <exe> [args...]    指定目标
 *
 * 注意：工作目录必须设成游戏目录，否则客户端找不到 Pack\*.pkn。
 * ========================================================================== */

#define _CRT_SECURE_NO_WARNINGS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "gg_bypass.h"

/* 一次启动尝试用到的全部句柄和名字。 */
typedef struct {
    HANDLE ready;
    HANDLE injected;
    HANDLE hit;
    HANDLE failed;
    char   ready_name[128];
    char   injected_name[128];
    char   hit_name[128];
    char   failed_name[128];
} gg_events;

/* 一次尝试的结果。 */
enum {
    ATTEMPT_OK = 0,        /* 绕过成功，游戏正常跑完 */
    ATTEMPT_GG_FAILED,     /* GameGuard 绕过失败，值得重来 */
    ATTEMPT_FATAL          /* 起不来（找不到文件 / API 失败），重来也没用 */
};

static const unsigned char GG_ORIG[POPSHOT_GG_CHECK_INSN_LEN] =
    POPSHOT_GG_ORIG_BYTES;
static const unsigned char GG_OLD_PATCH[POPSHOT_GG_CHECK_INSN_LEN] =
    POPSHOT_GG_OLD_PATCH_BYTES;

static void die(const char *what)
{
    DWORD e = GetLastError();
    fprintf(stderr, "[bsloader] 失败: %s (GetLastError=%lu)\n", what, (unsigned long)e);
    exit(1);
}

/* 取本 exe 所在目录 */
static void self_dir(char *out, size_t n)
{
    char *p;
    GetModuleFileNameA(NULL, out, (DWORD)n);
    p = strrchr(out, '\\');
    if (p) *p = 0;
}

/* 取上一级目录（就地修改） */
static void parent_dir(char *path)
{
    char *p = strrchr(path, '\\');
    if (p) *p = 0;
}

static void kill_child(PROCESS_INFORMATION *pi)
{
    TerminateProcess(pi->hProcess, 1);
    WaitForSingleObject(pi->hProcess, 2000);
    CloseHandle(pi->hThread);
    CloseHandle(pi->hProcess);
}

/* ---------------------------------------------------------------------- */
/* 四个握手事件                                                            */
/* ---------------------------------------------------------------------- */

static int make_event(HANDLE *out, char *name, size_t name_size,
                      const char *tag, unsigned attempt)
{
    _snprintf(name, name_size, "Local\\PopShotBshook%s_%lu_%lu_%u",
              tag, (unsigned long)GetCurrentProcessId(),
              (unsigned long)GetTickCount(), attempt);
    name[name_size - 1] = 0;
    *out = CreateEventA(NULL, TRUE, FALSE, name);
    return *out != NULL;
}

static void close_events(gg_events *ev)
{
    if (ev->ready)    CloseHandle(ev->ready);
    if (ev->injected) CloseHandle(ev->injected);
    if (ev->hit)      CloseHandle(ev->hit);
    if (ev->failed)   CloseHandle(ev->failed);
    ZeroMemory(ev, sizeof(*ev));
}

static int create_events(gg_events *ev, unsigned attempt)
{
    ZeroMemory(ev, sizeof(*ev));
    if (make_event(&ev->ready, ev->ready_name, sizeof(ev->ready_name),
                   "Ready", attempt) &&
        make_event(&ev->injected, ev->injected_name, sizeof(ev->injected_name),
                   "Injected", attempt) &&
        make_event(&ev->hit, ev->hit_name, sizeof(ev->hit_name),
                   "Hit", attempt) &&
        make_event(&ev->failed, ev->failed_name, sizeof(ev->failed_name),
                   "Failed", attempt))
        return 1;
    close_events(ev);
    return 0;
}

static int signaled(HANDLE h)
{
    return h && WaitForSingleObject(h, 0) == WAIT_OBJECT_0;
}

/* ---------------------------------------------------------------------- */
/* 从进程外武装 DR0（§179）                                                */
/* ---------------------------------------------------------------------- */

/* 目标指令解壳了吗？1 = 原版 call，2 = 兼容的旧内存 patch，0 = 还是密文/读不到。
   判据和 DLL 里那份必须完全一致（两边共用 gg_bypass.h 的字节定义）。 */
static int remote_instruction_state(HANDLE process)
{
    unsigned char code[POPSHOT_GG_CHECK_INSN_LEN];
    SIZE_T got = 0;

    if (!ReadProcessMemory(process, (LPCVOID)(UINT_PTR)POPSHOT_GG_CHECK_VA,
                           code, sizeof(code), &got) || got != sizeof(code))
        return 0;
    if (memcmp(code, GG_ORIG, sizeof(code)) == 0) return 1;
    if (memcmp(code, GG_OLD_PATCH, sizeof(code)) == 0) return 2;
    return 0;
}

/* 保证主线程的 DR0 == 校验点。返回 1 = 这次写进去了，2 = 本来就对，0 = 失败。
   和 DLL 里的 ensure_gameguard_breakpoint 是同一套动作、同一组值，
   两边同时做也只是把同一个值写两遍。 */
static int arm_remote_breakpoint(HANDLE thread)
{
    CONTEXT ctx;
    int changed = 0;

    if (SuspendThread(thread) == (DWORD)-1) return 0;

    ZeroMemory(&ctx, sizeof(ctx));
    ctx.ContextFlags = CONTEXT_CONTROL | CONTEXT_DEBUG_REGISTERS;
    if (!GetThreadContext(thread, &ctx)) {
        ResumeThread(thread);
        return 0;
    }
    if (ctx.Dr0 != (DWORD)POPSHOT_GG_CHECK_VA ||
        (ctx.Dr7 & (DWORD)POPSHOT_DR0_CONTROL_MASK) !=
            (DWORD)POPSHOT_DR0_LOCAL_ENABLE) {
        ctx.Dr0 = (DWORD)POPSHOT_GG_CHECK_VA;
        ctx.Dr6 = 0;
        ctx.Dr7 &= ~(DWORD)POPSHOT_DR0_CONTROL_MASK;
        ctx.Dr7 |= (DWORD)POPSHOT_DR0_LOCAL_ENABLE;
        if (!SetThreadContext(thread, &ctx)) {
            ResumeThread(thread);
            return 0;
        }
        changed = 1;
    }
    if (ResumeThread(thread) == (DWORD)-1) return 0;
    return changed ? 1 : 2;
}

/* 从「VEH 已装好」一直守到「DLL 自己的武装线程接管」。
   期间每 2ms 看一眼：解壳了就武装 / 补武装。 */
static void external_arm_loop(PROCESS_INFORMATION *pi, gg_events *ev)
{
    DWORD started = GetTickCount();
    unsigned repairs = 0;
    int first = 1;

    while ((DWORD)(GetTickCount() - started) < POPSHOT_BSHOOK_READY_TIMEOUT) {
        if (signaled(ev->hit)) break;        /* 已经绕过去了，收工 */
        if (signaled(ev->ready)) break;      /* DLL 那条线程接管了 */
        if (signaled(ev->failed)) break;
        if (WaitForSingleObject(pi->hProcess, 0) == WAIT_OBJECT_0) break;

        if (remote_instruction_state(pi->hProcess) != 0) {
            int r = arm_remote_breakpoint(pi->hThread);
            if (r == 1) {
                if (first) {
                    printf("[bsloader] 目标已解壳，已从进程外武装 DR0=%08lX"
                           "（注入后 %lu ms）\n",
                           (unsigned long)POPSHOT_GG_CHECK_VA,
                           (unsigned long)(GetTickCount() - started));
                    first = 0;
                } else {
                    repairs++;
                }
            } else if (r == 0) {
                /* 线程没了或者被别人挂着，交给 DLL 那条线程去处理 */
                break;
            }
            /* 命中之后 VEH 会把 Dr7 撤掉，我们这里会看成「被清掉」再补回去。
               无害（再命中一次照样返回 0x755），而且 ready 一到就停。 */
        }
        Sleep(POPSHOT_GG_EXTERNAL_POLL_MS);
    }
    if (repairs)
        printf("[bsloader] 外部守护期间补写 DR0 %u 次\n", repairs);
}

/* ---------------------------------------------------------------------- */

static void set_env(const char *name, const char *value)
{
    if (!SetEnvironmentVariableA(name, value)) die("SetEnvironmentVariable");
}

/* 跑一次完整的「启动 → 注入 → 武装 → 等游戏退出」。 */
static int run_attempt(const char *target, const char *workdir,
                       char *cmdline, const char *dllpath,
                       const char *root, unsigned attempt, unsigned attempts)
{
    STARTUPINFOA si;
    PROCESS_INFORMATION pi;
    gg_events ev;
    HMODULE k32;
    FARPROC pLoadLibraryA;
    LPVOID remote;
    SIZE_T len;
    HANDLE waits[2];
    DWORD wait_result;
    int retry_allowed = (attempt + 1 < attempts);

    if (!create_events(&ev, attempt)) die("CreateEvent(bshook 握手)");

    set_env(POPSHOT_BSHOOK_READY_ENV,    ev.ready_name);
    set_env(POPSHOT_BSHOOK_INJECTED_ENV, ev.injected_name);
    set_env(POPSHOT_BSHOOK_HIT_ENV,      ev.hit_name);
    set_env(POPSHOT_BSHOOK_FAILED_ENV,   ev.failed_name);
    set_env(POPSHOT_BSHOOK_RETRY_ENV,    retry_allowed ? "1" : "0");

    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    ZeroMemory(&pi, sizeof(pi));

    if (!CreateProcessA(target, cmdline, NULL, NULL, FALSE,
                        CREATE_SUSPENDED, NULL, workdir, &si, &pi)) {
        DWORD error = GetLastError();
        close_events(&ev);
        SetLastError(error);
        die("CreateProcess");
    }

    printf("[bsloader] pid=%lu tid=%lu (挂起中)\n",
           (unsigned long)pi.dwProcessId, (unsigned long)pi.dwThreadId);

    /* --- 写入 DLL 路径 --------------------------------------------------- */
    len = strlen(dllpath) + 1;
    remote = VirtualAllocEx(pi.hProcess, NULL, len, MEM_COMMIT | MEM_RESERVE,
                            PAGE_READWRITE);
    if (!remote) { kill_child(&pi); close_events(&ev); die("VirtualAllocEx"); }
    if (!WriteProcessMemory(pi.hProcess, remote, dllpath, len, NULL)) {
        kill_child(&pi); close_events(&ev); die("WriteProcessMemory");
    }

    /* kernel32 在同一次开机内所有进程的基址相同，本进程取到的地址在目标里同样有效 */
    k32 = GetModuleHandleA("kernel32.dll");
    pLoadLibraryA = GetProcAddress(k32, "LoadLibraryA");
    if (!pLoadLibraryA) {
        kill_child(&pi); close_events(&ev); die("GetProcAddress(LoadLibraryA)");
    }

    /* --- APC 注入 -------------------------------------------------------- */
    if (!QueueUserAPC((PAPCFUNC)pLoadLibraryA, pi.hThread, (ULONG_PTR)remote)) {
        kill_child(&pi); close_events(&ev); die("QueueUserAPC");
    }
    printf("[bsloader] APC 已挂载，恢复主线程\n");

    if (ResumeThread(pi.hThread) == (DWORD)-1) {
        kill_child(&pi); close_events(&ev); die("ResumeThread");
    }

    /* --- ① 等 DLL 说「VEH 装好了」------------------------------------- */
    /*     在这之前绝不能武装 DR0：断点命中时没人处理那个单步异常。 */
    waits[0] = ev.injected;
    waits[1] = pi.hProcess;
    wait_result = WaitForMultipleObjects(2, waits, FALSE,
                                         POPSHOT_BSHOOK_INJECT_TIMEOUT);
    if (wait_result != WAIT_OBJECT_0) {
        fprintf(stderr,
                "[bsloader] bshook.dll 没能注入（%s）。\n"
                "[bsloader] 多半是安全软件拦了注入或把 bshook.dll 隔离了；\n"
                "[bsloader] 请把游戏目录整个加进杀毒软件的信任列表再试。\n",
                wait_result == WAIT_OBJECT_0 + 1 ? "游戏进程提前退出" : "等了 10 秒没动静");
        kill_child(&pi);
        close_events(&ev);
        return ATTEMPT_GG_FAILED;
    }

    /* --- ② 外部武装 + 等 DLL 接管 -------------------------------------- */
    external_arm_loop(&pi, &ev);

    waits[0] = ev.ready;
    waits[1] = pi.hProcess;
    wait_result = WaitForMultipleObjects(2, waits, FALSE, 2000);
    if (wait_result == WAIT_OBJECT_0) {
        printf("[bsloader] bshook 已就绪，GameGuard 目标已解壳，执行断点 DR0=%08lX\n",
               (unsigned long)POPSHOT_GG_CHECK_VA);
    } else if (wait_result == WAIT_TIMEOUT) {
        /* DLL 的武装线程还没被调度到。DR0 已经由本进程摆上去了，
           所以这不是失败 —— 继续等结果就行。 */
        printf("[bsloader] bshook 的武装线程还没跑起来（被加载器锁压着），"
               "DR0 已由 bsloader 从外部武装，继续\n");
    }

    /* --- ③ 等结果：命中 / 失败 / 进程退出 ------------------------------- */
    printf("[bsloader] 已启动，等待游戏退出…（日志在 %s\\logs\\）\n", root);
    for (;;) {
        HANDLE result_waits[3];
        result_waits[0] = ev.hit;
        result_waits[1] = ev.failed;
        result_waits[2] = pi.hProcess;
        wait_result = WaitForMultipleObjects(3, result_waits, FALSE, INFINITE);

        if (wait_result == WAIT_OBJECT_0) break;               /* 命中，正常 */
        if (wait_result == WAIT_OBJECT_0 + 1) {                /* 客户端报错 */
            fprintf(stderr, "[bsloader] GameGuard 绕过失败（客户端弹了错误框）\n");
            kill_child(&pi);
            close_events(&ev);
            return ATTEMPT_GG_FAILED;
        }
        /* 进程退出了。命中过就是正常退出，没命中过就是绕过失败。 */
        {
            DWORD code = 0;
            GetExitCodeProcess(pi.hProcess, &code);
            if (!signaled(ev.hit)) {
                fprintf(stderr,
                        "[bsloader] 游戏在 DR0 命中之前就退出了"
                        "（exit code = %lu）—— GameGuard 绕过失败\n",
                        (unsigned long)code);
                CloseHandle(pi.hThread);
                CloseHandle(pi.hProcess);
                close_events(&ev);
                return ATTEMPT_GG_FAILED;
            }
            printf("[bsloader] 游戏退出，exit code = %lu (0x%08lX)\n",
                   (unsigned long)code, (unsigned long)code);
            CloseHandle(pi.hThread);
            CloseHandle(pi.hProcess);
            close_events(&ev);
            return ATTEMPT_OK;
        }
    }

    /* 命中之后就只等游戏退出。 */
    WaitForSingleObject(pi.hProcess, INFINITE);
    {
        DWORD code = 0;
        GetExitCodeProcess(pi.hProcess, &code);
        printf("[bsloader] 游戏退出，exit code = %lu (0x%08lX)\n",
               (unsigned long)code, (unsigned long)code);
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    close_events(&ev);
    return ATTEMPT_OK;
}

int main(int argc, char **argv)
{
    char dlldir[MAX_PATH], dllpath[MAX_PATH];
    char root[MAX_PATH], target[MAX_PATH], workdir[MAX_PATH];
    char cmdline[MAX_PATH * 4];
    char *p;
    unsigned attempt;
    int i;
    int result = ATTEMPT_GG_FAILED;

    /* 源码是 UTF-8（/utf-8 编译），控制台默认 CP936 会乱码 */
    SetConsoleOutputCP(CP_UTF8);
    /* ★ launch.ps1 把两个流重定向到 logs\bsloader.out/err，管道默认是全缓冲的
       —— 不关缓冲的话，进程活着的时候日志里一个字都没有，正好在出问题
       （游戏卡着、自动重来）时最需要它。 */
    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);

    /* --- 路径推导 ------------------------------------------------------- */
    /* 本 exe 在 <root>\hook\bin\bsloader.exe → root 要往上两级 */
    self_dir(dlldir, sizeof(dlldir));
    _snprintf(dllpath, sizeof(dllpath), "%s\\bshook.dll", dlldir);

    strcpy(root, dlldir);
    parent_dir(root);            /* -> <root>\hook  */
    parent_dir(root);            /* -> <root>       */

    if (argc > 1) {
        strncpy(target, argv[1], sizeof(target) - 1);
        target[sizeof(target) - 1] = 0;
    } else {
        _snprintf(target, sizeof(target), "%s\\game_patched\\BigShot.exe", root);
    }

    strcpy(workdir, target);
    parent_dir(workdir);

    /* 组命令行：argv[0] 用目标 exe 路径，其余透传 */
    _snprintf(cmdline, sizeof(cmdline), "\"%s\"", target);
    for (i = 2; i < argc; i++) {
        p = cmdline + strlen(cmdline);
        _snprintf(p, sizeof(cmdline) - (p - cmdline), " %s", argv[i]);
    }

    printf("[bsloader] 目标   : %s\n", target);
    printf("[bsloader] 工作目录: %s\n", workdir);
    printf("[bsloader] 注入   : %s\n", dllpath);

    if (GetFileAttributesA(target) == INVALID_FILE_ATTRIBUTES) {
        fprintf(stderr, "[bsloader] 找不到目标程序: %s\n", target);
        return 1;
    }
    if (GetFileAttributesA(dllpath) == INVALID_FILE_ATTRIBUTES) {
        fprintf(stderr, "[bsloader] 找不到 bshook.dll: %s\n", dllpath);
        return 1;
    }

    for (attempt = 0; attempt < POPSHOT_GG_MAX_ATTEMPTS; attempt++) {
        if (attempt) {
            /* 前缀里那个 ASCII 记号是给 hook\test-retry.bat 抓的
               （findstr 在 CP936 控制台里认不了中文）。 */
            printf("[bsloader] GG-RETRY %u/%u ★ 上一次 GameGuard 绕过失败，"
                   "自动重来一次\n",
                   attempt + 1, (unsigned)POPSHOT_GG_MAX_ATTEMPTS);
            /* 上一个进程刚被杀掉，等互斥体 BigShot_Assa 真的放开再来，
               否则新实例会秒退（V0.1 §9）。 */
            Sleep(1500);
        }
        result = run_attempt(target, workdir, cmdline, dllpath, root,
                             attempt, POPSHOT_GG_MAX_ATTEMPTS);
        if (result != ATTEMPT_GG_FAILED) break;
    }

    if (result != ATTEMPT_OK) {
        fprintf(stderr,
                "[bsloader] GG-GIVEUP 连续 %u 次都没能绕过 GameGuard。\n"
                "[bsloader] 请把 %s\\logs\\bshook_*.log 发回来。\n",
                (unsigned)POPSHOT_GG_MAX_ATTEMPTS, root);
        return 1;
    }
    return 0;
}
