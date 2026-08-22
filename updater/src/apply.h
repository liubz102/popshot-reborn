/* --------------------------------------------------------------------------
   apply.h —— 覆盖引擎：staging 同盘整包解压 -> 剥顶层 -> 逐文件搬 ->
   BUILD.ver 最后写（提交点，失败重跑幂等）。

   玩家数据永不覆盖（PROTECTED_PATHS，update_client.py 同款清单）。
   被占文件（运行的 exe）改名 .update_old 让位 —— 遗留清理归启动脚本
   （launch.ps1，用户拍板），这里改名前先清掉上次遗留的同名防挡道。
   -------------------------------------------------------------------------- */
#ifndef UPDATER_APPLY_H
#define UPDATER_APPLY_H

#include <windows.h>
#include "zip.h"

/* 进度回调：done/total 按文件数。 */
typedef void (*apply_progress_fn)(void *user, int done, int total);

/* zip 里相对路径（\ 或 /）是否命中玩家数据排除清单。selftest 钉住。 */
int apply_is_protected(const wchar_t *rel);

/* 应用更新。返回 1 成功；err 收失败原因。
   zip_bad（可 NULL）：1 = 失败在 zip 本身（打不开/解压/CRC），调用方
   应丢弃下载缓存逼下次重下（毒缓存死循环，§239 真机踩坑）。 */
int apply_update(const wchar_t *zip_path, const wchar_t *root,
                 apply_progress_fn progress, void *user,
                 int *moved_out, int *zip_bad,
                 wchar_t *err, size_t err_cap);

#endif /* UPDATER_APPLY_H */
