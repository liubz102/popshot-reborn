/* --------------------------------------------------------------------------
   config.h —— server.config / BUILD.ver 的最小解析（探针和本地版本用）。

   server.config 格式与 server\config.py、tools\launch.ps1 一致：
   key = value、# 或 ; 注释、UTF-8/UTF-16 BOM 都可能、[IPv6] 去括号。
   BUILD.ver 是 JSON，但只需扫第一个 "version" 键（bshook 同款兜底扫描）。
   -------------------------------------------------------------------------- */
#ifndef UPDATER_CONFIG_H
#define UPDATER_CONFIG_H

#include "util.h"

/* server.config -> server_address（缺省 192.168.1.100，与 config.py 同）。
   返回 1 成功。 */
int cfg_server_address(const wchar_t *root, wchar_t *out, size_t cap);

/* 包根 BUILD.ver -> 版本。返回 1 成功，0 认不出。 */
int cfg_local_version(const wchar_t *root, Ver *out);

/* 包根可写试探（建了就删 .update-write-test）。 */
int cfg_root_writable(const wchar_t *root);

#endif /* UPDATER_CONFIG_H */
