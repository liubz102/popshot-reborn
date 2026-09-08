/* --------------------------------------------------------------------------
   config.h —— server.config / BUILD.ver / update.config 的最小解析。

   server.config 格式与 server\config.py、tools\launch.ps1 一致：
   key = value、# 或 ; 注释、UTF-8/UTF-16 BOM 都可能、[IPv6] 去括号。
   BUILD.ver 是 JSON，但只需扫第一个 "version" 键（bshook 同款兜底扫描）。
   update.config 是下载加速代理列表：一行一个 http(s):// 地址，# ; 注释。
   -------------------------------------------------------------------------- */
#ifndef UPDATER_CONFIG_H
#define UPDATER_CONFIG_H

#include "util.h"

/* ---- config\update.config：下载加速代理列表（speedtest.c 按它测速选源） -- */

#define PROXY_MAX      32
#define PROXY_URL_CAP  256

typedef struct ProxyList {
    int count;
    int skipped;                              /* 认不出 / 重复 / 超限而忽略的行数 */
    wchar_t url[PROXY_MAX][PROXY_URL_CAP];    /* 末尾 / 已去掉，直接拼在原地址前 */
} ProxyList;

/* 文本 -> 列表：一行一个地址，只认 http:// 或 https:// 开头、中间无空白的行；
   # 或 ; 开头是注释；末尾 / 去掉；大小写不敏感去重；最多 PROXY_MAX 条。
   返回 count。selftest 钉住。 */
int cfg_parse_proxy_list(const wchar_t *text, ProxyList *out);
/* 读 <root>\config\update.config。返回 1 = 文件存在并已解析；
   0 = 没有文件（out->count = 0，调用方只用直连）。 */
int cfg_proxy_list(const wchar_t *root, ProxyList *out);

/* server.config -> server_address（缺省 192.168.1.100，与 config.py 同）。
   返回 1 成功。 */
int cfg_server_address(const wchar_t *root, wchar_t *out, size_t cap);

/* 包根 BUILD.ver -> 版本。返回 1 成功，0 认不出。 */
int cfg_local_version(const wchar_t *root, Ver *out);

/* 包根可写试探（建了就删 .update-write-test）。 */
int cfg_root_writable(const wchar_t *root);

#endif /* UPDATER_CONFIG_H */
