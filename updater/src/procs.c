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


/* 取消探针：main 把 ui_cancel_requested 挂进来。长等待的每一轮看一眼，
   玩家点了取消就让等待提前散场 —— 否则「等游戏退出 15s / 停服务端 10s」
   期间点取消毫无反应（真机反馈「下载过程中点取消没有用」的嫌疑现场之一，
   V0.2 会话 50）。 */
static int (*g_cancel_probe)(void);

void procs_set_cancel_probe(int (*fn)(void))
{
    g_cancel_probe = fn;
}

static int cancel_hit(void)
{
    return g_cancel_probe ? g_cancel_probe() : 0;
}


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
    if (!h) {
        /* ★ 只有「存在但打不开」（保护进程/权限）才当活着。pid 已失效
           （进程死了且最后一个句柄关掉）报 ERROR_INVALID_PARAMETER ——
           那是**死了**。旧代码一律当活人，杀干净了也判「结束不了」
           （真机二连踩，§236/§238）。 */
        return GetLastError() == ERROR_ACCESS_DENIED ? 1 : 0;
    }
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
int procs_collect_tree(DWORD pid, DWORD *out, int cap)
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

    n = procs_collect_tree(pid, ids, 128);
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
        if (cancel_hit()) return -1;           /* 玩家取消：别再等了 */
        alive = 0;
        for (i = 0; i < count; i++)
            if (procs_pid_alive(pids[i])) alive++;
        if (alive) Sleep(sleep_ms);
    }
    return alive;
}

/* pid 的 exe 是否本包两份 runtime 之一的 python.exe（调用方传小写全路径）。 */
static int image_is_package(DWORD pid, const wchar_t *modern_lc,
                            const wchar_t *legacy_lc)
{
    wchar_t image[MAX_PATH * 2];
    if (!procs_image_path(pid, image, MAX_PATH * 2)) return 0;
    _wcslwr(image);
    return !wcscmp(image, modern_lc) || !wcscmp(image, legacy_lc);
}

/* 停进程目标：攥着句柄等信号。按 pid 反复重开是靠不住的 —— 进程死后
   最后一个句柄一关 pid 就失效（OpenProcess 报 INVALID_PARAMETER），
   旧代码把这当「活着」，杀干净了也等不满 10 秒（§238 真机二连踩）。 */
typedef struct {
    DWORD  pid;
    HANDLE proc;      /* SYNCHRONIZE|PROCESS_TERMINATE；打不开为 NULL */
    int    denied;   /* 打不开且是权限问题：活着但动不了它 */
} KillTarget;

static int target_add(KillTarget *list, int n, int cap, DWORD pid)
{
    HANDLE h;
    int i;
    for (i = 0; i < n; i++)
        if (list[i].pid == pid) return n;      /* 去重 */
    if (n >= cap) return n;
    h = OpenProcess(SYNCHRONIZE | PROCESS_TERMINATE, FALSE, pid);
    list[n].pid = pid;
    list[n].proc = h;
    list[n].denied = (h == NULL && GetLastError() == ERROR_ACCESS_DENIED);
    if (list[n].denied)
        log_line("kill target pid=%lu denied (protected/elevated?)",
                 (unsigned long)pid);
    return n + 1;
}

/* 等全部目标变成已信号（死透）。denied 的永远算活着。
   返回仍活着的个数；-1 = 等待期间玩家取消。 */
static int wait_targets_gone(KillTarget *list, int n, DWORD total_ms)
{
    ULONGLONG deadline = GetTickCount64() + total_ms;
    for (;;) {
        int alive = 0, i;
        if (cancel_hit()) return -1;
        for (i = 0; i < n; i++) {
            if (list[i].denied) { alive++; continue; }
            if (list[i].proc &&
                WaitForSingleObject(list[i].proc, 50) != WAIT_OBJECT_0)
                alive++;
        }
        if (!alive) return 0;
        if (GetTickCount64() >= deadline) return alive;
    }
}

int procs_stop_package_pythons(const wchar_t *root, int *ended_out)
{
    DWORD pids[PROCS_MAX_PIDS];
    KillTarget mine[PROCS_MAX_PIDS];
    int n, i, mine_n = 0, alive;
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
        /* 先收树再开杀：爹死了孩子变孤儿就收不齐。树里成员要过路径
           核验才进等待名单 —— ppid 会因 PID 复用指错爹，conhost 这类
           保护进程杀了必失败，都不能进「停不掉」的判定（§236）。 */
        {
            DWORD tree[64];
            int tn = procs_collect_tree(pids[i], tree, 64), k;
            mine_n = target_add(mine, mine_n, PROCS_MAX_PIDS, pids[i]);
            for (k = 0; k < tn; k++)
                if (image_is_package(tree[k], modern_lc, legacy_lc))
                    mine_n = target_add(mine, mine_n, PROCS_MAX_PIDS, tree[k]);
        }
        procs_tree_kill(pids[i]);
    }
    /* 等不干净就补杀一轮（首轮 TerminateProcess 可能因时序没杀成），
       仍不行才认输 —— 判定只基于自己攥着的句柄（signaled = 死透）。 */
    if (mine_n) {
        alive = wait_targets_gone(mine, mine_n, 5000);
        if (alive > 0) {
            for (i = 0; i < mine_n; i++)
                if (!mine[i].denied && mine[i].proc &&
                    WaitForSingleObject(mine[i].proc, 0) != WAIT_OBJECT_0)
                    TerminateProcess(mine[i].proc, 1);
            alive = wait_targets_gone(mine, mine_n, 5000);
        }
        if (alive == -1)
            *ended_out = 2;                    /* 等待中玩家取消 */
        else if (alive > 0)
            *ended_out = 1;
        for (i = 0; i < mine_n; i++)
            if (mine[i].proc) CloseHandle(mine[i].proc);
    }
    return mine_n;
}

/* 等游戏退出。返回：
     0  游戏已经不在（或安静等退成功）
     1  超时还在（调用方走「询问玩家后强杀」流程，传回 PID 列表）
     2  强杀后仍结束不了（异常）
     3  等待期间玩家取消 */
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
        if (cancel_hit()) return 3;
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
