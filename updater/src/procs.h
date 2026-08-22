/* --------------------------------------------------------------------------
   procs.h —— 进程管理：Toolhelp 快照、按 exe 完整路径精确停本机服务端、
   等游戏退出。纪律与 update_client.py 一致：绝不按进程名乱杀别的 python。
   -------------------------------------------------------------------------- */
#ifndef UPDATER_PROCS_H
#define UPDATER_PROCS_H

#include <windows.h>

#define PROCS_MAX_PIDS 256

/* 按进程名（大小写无关，如 L"BigShot.exe" / L"python.exe"）收集 PID。
   extra_pid 追加进去（-procid 传来的）。返回个数，-1 失败。 */
int procs_by_name(const wchar_t *name, DWORD *out, int cap, DWORD extra_pid);

/* PID 还活着吗（探询权限不够也算活着 —— 宁可多等一眼）。 */
int procs_pid_alive(DWORD pid);

/* PID -> exe 完整路径（QueryFullProcessImageNameW）。0 失败。 */
int procs_image_path(DWORD pid, wchar_t *out, size_t cap);

/* 树杀一个进程（先按父子关系收齐再 TerminateProcess）。返回杀掉的个数。 */
int procs_tree_kill(DWORD pid);

/* 等一批 PID 消失（tries 次、每回 sleep_ms）。返回还活着的个数。 */
int procs_wait_gone(const DWORD *pids, int count, int tries, int sleep_ms);

/* 停掉从本包 runtime 跑起来的 python（服务端/中继）。
   返回停掉的个数；ended_out 收「停不掉」标记（0=全停干净）。 */
int procs_stop_package_pythons(const wchar_t *root, int *ended_out);

/* 等游戏退出。返回：
     0  游戏已经不在（或安静等退成功）
     1  超时还在（调用方走「询问玩家后强杀」流程，传回 PID 列表）
     2  强杀后仍结束不了（异常） */
int procs_wait_game_exit(DWORD procid, DWORD *still_out, int *still_count);

#endif /* UPDATER_PROCS_H */
