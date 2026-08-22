/* --------------------------------------------------------------------------
   procs.c —— 见 procs.h。
   -------------------------------------------------------------------------- */
#define WIN32_LEAN_AND_MEAN
#define _CRT_SECURE_NO_WARNINGS
#include <windows.h>
#include <tlhelp32.h>
#include <stdio.h>
#include <wchar.h>
#include <string.h>
#include "procs.h"
#include "util.h"
#include "log.h"


int procs_by_name(const wchar_t *name, DWORD *out, int cap, DWORD extra_pid)
{
    HANDLE snap;
    PROCESSENTRY32W ent;
    BOOL ok;
    int n = 0;

    snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) {
        if (extra_pid && n < cap) out[n++] = extra_pid;
        return n;
    }
    ZeroMemory(&ent, sizeof(ent));
    ZeroMemory(&ent, sizeof(ent));
    ent.dwSize = sizeof(ent);
    ok = Process32FirstW(snap, &ent);
    while (ok) {
        if (wide_ieq(ent.szExeFile, name) && n < cap)
            out[n++] = ent.th32ProcessID;
        ok = Process32NextW(snap, &ent);
    }
    CloseHandle(snap);
    if (extra_pid) {
        int i, dup = 0;
        for (i = 0; i < n; i++)
            if (out[i] == extra_pid) dup = 1;
        if (!dup && n < cap) out[n++] = extra_pid;
    }
    return n;
}

int procs_pid_alive(DWORD pid)
{
    HANDLE h;
    DWORD r;
    if (!pid) return 0;
    h = OpenProcess(SYNCHRONIZE, FALSE, pid);
    if (!h) return 1;                    /* 权限不够也当活着 */
    r = WaitForSingleObject(h, 0);
    CloseHandle(h);
    return r != WAIT_OBJECT_0;
}

int procs_image_path(DWORD pid, wchar_t *out, size_t cap)
{
    HANDLE h;
    DWORD size = (DWORD)cap;
    h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!h) return 0;
    if (!QueryFullProcessImageNameW(h, 0, out, &size)) {
        CloseHandle(h);
        return 0;
    }
    CloseHandle(h);
    return 1;
}

/* 收齐 pid 及其全部后代（拍快照建父子表，DFS）。 */
static int collect_tree(DWORD pid, DWORD *out, int cap)
{
    HANDLE snap;
    PROCESSENTRY32W ent;
    BOOL ok;
    int n = 0;
    int scanned = 0;

    if (n < cap) out[n++] = pid;
    /* 多轮扫描：进程树可以在快照间隔里长出新叶子，扫到不再变为止。 */
    for (scanned = 0; scanned < 10; scanned++) {
        int before = n;
        snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if (snap == INVALID_HANDLE_VALUE) break;
        ZeroMemory(&ent, sizeof(ent));
    ent.dwSize = sizeof(ent);
        ok = Process32FirstW(snap, &ent);
        while (ok) {
            int i, parent_in = 0;
            for (i = 0; i < n; i++) {
                if (out[i] == ent.th32ParentProcessID) { parent_in = 1; break; }
            }
            if (parent_in) {
                int dup = 0;
                for (i = 0; i < n; i++)
                    if (out[i] == ent.th32ProcessID) dup = 1;
                if (!dup && n < cap) out[n++] = ent.th32ProcessID;
            }
            ok = Process32NextW(snap, &ent);
        }
        CloseHandle(snap);
        if (n == before) break;
    }
    return n;
}

int procs_tree_kill(DWORD pid)
{
    DWORD ids[128];
    int n, i, killed = 0;

    n = collect_tree(pid, ids, 128);
    /* 先杀深的（数组后面的），父母死后孩子变孤儿就找不到爹了 —— 其实
       先收齐了无所谓顺序，都 TerminateProcess 一遍即可。 */
    for (i = 0; i < n; i++) {
        HANDLE h = OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, FALSE, ids[i]);
        if (h) {
            if (TerminateProcess(h, 1)) killed++;
            WaitForSingleObject(h, 3000);
            CloseHandle(h);
        }
    }
    return killed;
}

int procs_wait_gone(const DWORD *pids, int count, int tries, int sleep_ms)
{
    int alive = count;
    int t, i;
    for (t = 0; t < tries && alive; t++) {
        alive = 0;
        for (i = 0; i < count; i++)
            if (procs_pid_alive(pids[i])) alive++;
        if (alive) Sleep(sleep_ms);
    }
    return alive;
}

int procs_stop_package_pythons(const wchar_t *root, int *ended_out)
{
    DWORD pids[PROCS_MAX_PIDS];
    int n, i, stopped = 0, alive;
    wchar_t modern[MAX_PATH * 2], legacy[MAX_PATH * 2], image[MAX_PATH * 2];
    wchar_t modern_lc[MAX_PATH * 2], legacy_lc[MAX_PATH * 2];

    *ended_out = 0;
    path_join(modern, MAX_PATH * 2, root, L"runtime\\python\\python.exe");
    path_join(legacy, MAX_PATH * 2, root, L"runtime-win7\\python\\python.exe");
    wcscpy(modern_lc, modern); _wcslwr(modern_lc);
    wcscpy(legacy_lc, legacy); _wcslwr(legacy_lc);

    n = procs_by_name(L"python.exe", pids, PROCS_MAX_PIDS, 0);
    /* 两份 runtime 都查，按 exe 完整路径精确匹配 —— 别的 python 一个不碰。 */
    for (i = 0; i < n; i++) {
        if (!procs_image_path(pids[i], image, MAX_PATH * 2)) continue;
        _wcslwr(image);
        if (wcscmp(image, modern_lc) && wcscmp(image, legacy_lc)) continue;
        log_line("stop package python pid=%lu image=%ls",
                 (unsigned long)pids[i], image);
        procs_tree_kill(pids[i]);
        stopped++;
    }
    if (stopped) {
        alive = 0;
        for (i = 0; i < n; i++)
            if (procs_pid_alive(pids[i])) alive++;
        /* 收尾再等一把（句柄释放有零点几秒延迟）。 */
        if (alive && procs_wait_gone(pids, n, 20, 250) != 0)
            *ended_out = 1;
    }
    return stopped;
}

int procs_wait_game_exit(DWORD procid, DWORD *still_out, int *still_count)
{
    DWORD pids[PROCS_MAX_PIDS];
    int n, i;
    ULONGLONG deadline;

    n = procs_by_name(L"BigShot.exe", pids, PROCS_MAX_PIDS, procid);
    for (i = 0; i < n; i++)
        if (!procs_pid_alive(pids[i])) {
            int k;
            for (k = i; k < n - 1; k++) pids[k] = pids[k + 1];
            n--; i--;
        }
    if (!n) return 0;

    deadline = GetTickCount64() + 15000;
    while (GetTickCount64() < deadline) {
        int alive = 0;
        for (i = 0; i < n; i++)
            if (procs_pid_alive(pids[i])) alive++;
        if (!alive) return 0;
        Sleep(500);
    }
    /* 超时还在 —— 把活着的交回去，由 UI 询问玩家。 */
    *still_count = 0;
    for (i = 0; i < n; i++)
        if (procs_pid_alive(pids[i]) && *still_count < PROCS_MAX_PIDS)
            still_out[(*still_count)++] = pids[i];
    if (!*still_count) return 0;
    return 1;
}
