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

/* 取消探针：返回非 0 = 玩家点了取消。 */
typedef int (*net_cancel_fn)(void);

/* 小文件（manifest）取进内存。返回 1 成功；err_out 收错误原因（可 NULL）。
   window_ms > 0 时整次取件（含建连；各阶段超时同值）限时，到点 err_out =
   L"expired"；cancel（可 NULL）每 <=200ms 问一次，取消 err_out = L"cancelled"。
   window_ms = 0 且 cancel = NULL 就是老行为（默认超时、不可打断）。 */
int net_get_memory(const wchar_t *url, char *buf, size_t cap, size_t *out_len,
                   unsigned window_ms, net_cancel_fn cancel,
                   wchar_t *err_out, size_t err_cap);

/* 大文件下载到 dest，边下边喂哈希（hash 可 NULL）+ 进度回调（可 NULL）。
   expected_size>=0 时按它校验长度。返回 1 成功。取消（回调返 0）返回 0，
   err_out = L"cancelled"。 */
int net_download_file(const wchar_t *url, const wchar_t *dest,
                      long long expected_size,
                      Sha256 *hash, net_progress_fn progress, void *user,
                      wchar_t *err_out, size_t err_cap);

/* 测速探针的分格记录：格 i = [i*bucket_ms, (i+1)*bucket_ms) 内收到的字节
   （从探针开始、建连前起算）。speedtest.c 拿它算「任意连续 1 秒的最大字节数」。 */
#define NET_TRACE_BUCKETS 128
typedef struct NetTrace {
    unsigned bucket_ms;                        /* 每格多少毫秒（0 = 没记） */
    int nbuckets;                              /* 有效格数 = window/bucket + 1 */
    unsigned long long bucket[NET_TRACE_BUCKETS];
} NetTrace;

/* 测速探针（speedtest.c 的注入实现）：从 url 下载，window_ms 一到就停
   （窗口含建连；解析 / 连接 / 发送 / 接收各阶段超时也都是 window_ms），
   回窗口内收到的字节数、实际用时（min(窗口, 提前收完的用时)）和按 bucket_ms
   分格的字节记录（trace_out 可 NULL；格数超过 NET_TRACE_BUCKETS 时格自动加倍）。
   cancel 每 <=200ms 问一次。note_out（可 NULL）收结束原因：
   complete / expired / cancelled / 出错文案。
   返回 1 = 有结果（到点、收完、出错都算一个结果）；0 = 被取消。
   ★ 每次调用自己开 WinHTTP 会话，几路并行互不相干（一组几个同时测）。 */
int net_probe_speed(const wchar_t *url, unsigned window_ms, unsigned bucket_ms,
                    net_cancel_fn cancel,
                    unsigned long long *bytes_out, unsigned *elapsed_out,
                    NetTrace *trace_out, wchar_t *note_out, size_t note_cap);

#endif /* UPDATER_NET_HTTP_H */
