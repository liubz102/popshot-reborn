/* --------------------------------------------------------------------------
   probe.h —— 服务器探针：向游戏服重演一次握手，问「我该升到哪个版本」。

   这是 tools\update_client.py probe_server 的 C 移植：
     裸发编码版本号 int32（SimpleCipher 整条流加密）-> 读 0xFE 控制帧
     [FE][未用][u16 LE 载荷长][int32 结果码 + 可选 u16 字数 + UTF-16LE 文案]
     -> 从拒绝文案按 [vV]数字.数字[.数字] 抠目标版本。
   -------------------------------------------------------------------------- */
#ifndef UPDATER_PROBE_H
#define UPDATER_PROBE_H

#include "util.h"

typedef enum ProbeStatus {
    PROBE_UNREACHABLE = 0,    /* 连不上/说了听不懂的话 -> 调用方退回最新版 */
    PROBE_OK,                 /* 结果码 0：服务器认这个版本，无需更新 */
    PROBE_REJECTED,           /* 非零：要更新（wanted = 该升到的版本） */
    PROBE_ERROR               /* 参数/内存错误 */
} ProbeStatus;

typedef struct ProbeResult {
    ProbeStatus status;
    Ver  wanted;              /* REJECTED 时有效（文案抠不出为 all-0） */
    int  wanted_valid;
    wchar_t message[256];     /* 服务器文案（诊断/日志用） */
} ProbeResult;

int probe_parse_frame(const unsigned char *plain, size_t len,
                      int *result_code, wchar_t *message, size_t msg_cap);
/* 从拒绝文案抠版本（[vV]数字.数字[.数字]，第一个匹配）。 */
int probe_parse_wanted(const wchar_t *message, Ver *out);

/* host:port 握手探针。返回 1 = result 有效（status 也已填）。 */
int probe_server(const wchar_t *host, int port, const Ver *local_version,
                 ProbeResult *out);

#endif /* UPDATER_PROBE_H */
