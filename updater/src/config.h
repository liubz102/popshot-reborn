/* --------------------------------------------------------------------------
   config.h —— server.config / BUILD.ver / update.config 的最小解析。

   server.config 格式与 server\config.py、tools\launch.ps1 一致：
   key = value、# 或 ; 注释、UTF-8/UTF-16 BOM 都可能、[IPv6] 去括号。
   BUILD.ver 是 JSON，但只需扫第一个 "version" 键（bshook 同款兜底扫描）。
   update.config 是「更新源」：一行 manifest_url = <地址> + 一行一个代理地址。
   -------------------------------------------------------------------------- */
#ifndef UPDATER_CONFIG_H
#define UPDATER_CONFIG_H

#include "util.h"

/* ---- config\update.config：更新源（manifest 地址 + 下载加速代理列表） -----
   ★ 权威的那一份在**服务器上**（main.c 开工时 HTTP 取回来），本机这份只是
   引导和兜底。两份格式一模一样，所以解析器只有这一套。 */

#define PROXY_MAX          32
#define PROXY_URL_CAP      256
#define MANIFEST_URL_CAP   1024

typedef struct ProxyList {
    int count;
    int skipped;                              /* 认不出 / 重复 / 超限而忽略的行数 */
    wchar_t url[PROXY_MAX][PROXY_URL_CAP];    /* 末尾 / 已去掉，直接拼在原地址前 */
} ProxyList;

typedef struct UpdateConfig {
    ProxyList proxies;
    wchar_t manifest_url[MANIFEST_URL_CAP];   /* 空 = 没写，调用方退回内置默认 */
} UpdateConfig;

/* 文本 -> 更新源。逐行看：
     # 或 ; 开头、空行            -> 跳过，不计 skipped
     key = value（含空格都行）    -> 认得的键存下来，不计 skipped；认不出的键
                                     计 skipped（和从前一样，老配置不回归）
     http:// 或 https:// 开头     -> 代理，规则同下面 cfg_parse_proxy_list
   目前只认一个键：manifest_url。返回代理条数。selftest 钉住。 */
int cfg_parse_update_config(const wchar_t *text, UpdateConfig *out);
/* 读 <root>\config\update.config。返回 1 = 文件存在并已解析；
   0 = 没有文件（out 已清零，调用方只用直连 + 内置 manifest 地址）。 */
int cfg_update_config(const wchar_t *root, UpdateConfig *out);

/* 只要代理列表的旧入口（selftest 钉着这两个签名，别改）。
   一行一个地址，只认 http:// 或 https:// 开头、中间无空白的行；
   # 或 ; 开头是注释；末尾 / 去掉；大小写不敏感去重；最多 PROXY_MAX 条。
   返回 count。 */
int cfg_parse_proxy_list(const wchar_t *text, ProxyList *out);
int cfg_proxy_list(const wchar_t *root, ProxyList *out);

/* server.config -> server_address（缺省 192.168.1.100，与 config.py 同）。
   返回 1 成功。 */
int cfg_server_address(const wchar_t *root, wchar_t *out, size_t cap);
/* server.config -> server_register_port（服务器上注册页 / 管理页 / 更新源
   接口的 HTTP 端口）。认不出、超范围、没有这一行都回 config.py 的默认
   27810 —— 这一项只影响「问不问得到服务器」，fail-open。 */
int cfg_server_register_port(const wchar_t *root);

/* 包根 BUILD.ver -> 版本。返回 1 成功，0 认不出。 */
int cfg_local_version(const wchar_t *root, Ver *out);

/* 包根可写试探（建了就删 .update-write-test）。 */
int cfg_root_writable(const wchar_t *root);

#endif /* UPDATER_CONFIG_H */
