/* --------------------------------------------------------------------------
   net_http.h —— WinHTTP 下载（manifest / 全量 zip）。

   走系统代理、自动跟随重定向、尽力开 TLS1.2（Win7 未打 KB3140245 的机器
   协商不出来 —— 已知取舍，见 DECISIONS D156：报错走手动下载兜底）。
   -------------------------------------------------------------------------- */
#ifndef UPDATER_NET_HTTP_H
#define UPDATER_NET_HTTP_H

#include <windows.h>
#include "sha256.h"

/* 进度回调：done/total 字节（total=0 表示服务器没给长度）。
   返回 0 = 玩家点了取消，中断下载。 */
typedef int (*net_progress_fn)(void *user,
                               unsigned long long done,
                               unsigned long long total);

/* 小文件（manifest）取进内存。返回 1 成功；err_out 收错误原因（可 NULL）。 */
int net_get_memory(const wchar_t *url, char *buf, size_t cap, size_t *out_len,
                   wchar_t *err_out, size_t err_cap);

/* 大文件下载到 dest，边下边喂哈希（hash 可 NULL）+ 进度回调（可 NULL）。
   expected_size>=0 时按它校验长度。返回 1 成功。取消（回调返 0）返回 0，
   err_out = L"cancelled"。 */
int net_download_file(const wchar_t *url, const wchar_t *dest,
                      long long expected_size,
                      Sha256 *hash, net_progress_fn progress, void *user,
                      wchar_t *err_out, size_t err_cap);

#endif /* UPDATER_NET_HTTP_H */
