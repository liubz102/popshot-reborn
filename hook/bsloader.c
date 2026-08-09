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
 * GameGuard 绕过不用固定延时改代码：bshook.dll 先注册 VEH，再由内部线程
 * 等主线程离开注入 APC 后设置 DR0，最后通过命名事件回报“已武装”。主线程
 * 真正执行到状态读取指令时，VEH 直接改 EAX/EIP；游戏代码字节始终不变。
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

static void abort_child(PROCESS_INFORMATION *pi, const char *what, DWORD error)
{
    TerminateProcess(pi->hProcess, 1);
    WaitForSingleObject(pi->hProcess, 2000);
    CloseHandle(pi->hThread);
    CloseHandle(pi->hProcess);
    SetLastError(error);
    die(what);
}

int main(int argc, char **argv)
{
    char dlldir[MAX_PATH], dllpath[MAX_PATH];
    char root[MAX_PATH], target[MAX_PATH], workdir[MAX_PATH];
    char cmdline[MAX_PATH * 4];
    STARTUPINFOA si;
    PROCESS_INFORMATION pi;
    HMODULE k32;
    FARPROC pLoadLibraryA;
    LPVOID remote;
    SIZE_T len;
    HANDLE ready_event;
    HANDLE waits[2];
    DWORD wait_result;
    DWORD error;
    char ready_event_name[128];
    char previous_ready_event[128];
    DWORD previous_ready_event_len;
    int had_previous_ready_event;
    char *p;
    int i;

    /* 源码是 UTF-8（/utf-8 编译），控制台默认 CP936 会乱码 */
    SetConsoleOutputCP(CP_UTF8);

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

    _snprintf(ready_event_name, sizeof(ready_event_name),
              "Local\\PopShotBshookReady_%lu_%lu",
              (unsigned long)GetCurrentProcessId(), (unsigned long)GetTickCount());
    ready_event = CreateEventA(NULL, TRUE, FALSE, ready_event_name);
    if (!ready_event) die("CreateEvent(bshook ready)");

    previous_ready_event_len = GetEnvironmentVariableA(
        POPSHOT_BSHOOK_READY_ENV, previous_ready_event, sizeof(previous_ready_event));
    had_previous_ready_event = previous_ready_event_len > 0 &&
                               previous_ready_event_len < sizeof(previous_ready_event);
    if (!SetEnvironmentVariableA(POPSHOT_BSHOOK_READY_ENV, ready_event_name))
        die("SetEnvironmentVariable(bshook ready)");

    /* --- 挂起启动 -------------------------------------------------------- */
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    ZeroMemory(&pi, sizeof(pi));

    if (!CreateProcessA(target, cmdline, NULL, NULL, FALSE,
                        CREATE_SUSPENDED, NULL, workdir, &si, &pi)) {
        error = GetLastError();
        if (had_previous_ready_event)
            SetEnvironmentVariableA(POPSHOT_BSHOOK_READY_ENV, previous_ready_event);
        else
            SetEnvironmentVariableA(POPSHOT_BSHOOK_READY_ENV, NULL);
        CloseHandle(ready_event);
        SetLastError(error);
        die("CreateProcess");
    }
    if (had_previous_ready_event)
        SetEnvironmentVariableA(POPSHOT_BSHOOK_READY_ENV, previous_ready_event);
    else
        SetEnvironmentVariableA(POPSHOT_BSHOOK_READY_ENV, NULL);

    printf("[bsloader] pid=%lu tid=%lu (挂起中)\n",
           (unsigned long)pi.dwProcessId, (unsigned long)pi.dwThreadId);

    /* --- 写入 DLL 路径 --------------------------------------------------- */
    len = strlen(dllpath) + 1;
    remote = VirtualAllocEx(pi.hProcess, NULL, len, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!remote) die("VirtualAllocEx");
    if (!WriteProcessMemory(pi.hProcess, remote, dllpath, len, NULL))
        die("WriteProcessMemory");

    /* kernel32 在同一次开机内所有进程的基址相同，本进程取到的地址在目标里同样有效 */
    k32 = GetModuleHandleA("kernel32.dll");
    pLoadLibraryA = GetProcAddress(k32, "LoadLibraryA");
    if (!pLoadLibraryA) die("GetProcAddress(LoadLibraryA)");

    /* --- APC 注入 -------------------------------------------------------- */
    if (!QueueUserAPC((PAPCFUNC)pLoadLibraryA, pi.hThread, (ULONG_PTR)remote)) {
        die("QueueUserAPC");
    }
    printf("[bsloader] APC 已挂载，恢复主线程\n");

    if (ResumeThread(pi.hThread) == (DWORD)-1) die("ResumeThread");

    /* DLL 内的专用线程会等主线程离开 LoadLibrary APC 的恢复路径，再设置 DR0；
       否则 APC 返回时恢复旧 CONTEXT，会把过早设置的调试寄存器覆盖掉。
       等待的是“断点已经武装”的握手，不是游戏运行到某个固定毫秒数。 */
    waits[0] = ready_event;
    waits[1] = pi.hProcess;
    wait_result = WaitForMultipleObjects(2, waits, FALSE, POPSHOT_BSHOOK_READY_TIMEOUT);
    if (wait_result == WAIT_OBJECT_0 + 1) {
        CloseHandle(ready_event);
        abort_child(&pi, "bshook.dll 初始化前游戏已经退出", ERROR_DLL_INIT_FAILED);
    }
    if (wait_result != WAIT_OBJECT_0) {
        error = (wait_result == WAIT_TIMEOUT) ? ERROR_TIMEOUT : GetLastError();
        CloseHandle(ready_event);
        abort_child(&pi, "等待 bshook.dll 初始化握手", error);
    }
    CloseHandle(ready_event);

    printf("[bsloader] bshook 已就绪，GameGuard 执行断点 DR0=%08lX\n",
           (unsigned long)POPSHOT_GG_CHECK_VA);

    printf("[bsloader] 已启动，等待游戏退出…（日志在 %s\\logs\\）\n", root);
    WaitForSingleObject(pi.hProcess, INFINITE);
    {
        DWORD code = 0;
        GetExitCodeProcess(pi.hProcess, &code);
        printf("[bsloader] 游戏退出，exit code = %lu (0x%08lX)\n",
               (unsigned long)code, (unsigned long)code);
    }

    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}
