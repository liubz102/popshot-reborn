/* --------------------------------------------------------------------------
   probe.c —— 见 probe.h。
   -------------------------------------------------------------------------- */
#define WIN32_LEAN_AND_MEAN
#define _CRT_SECURE_NO_WARNINGS
#define _WINSOCK_DEPRECATED_NO_WARNINGS
#include <windows.h>
#include <winsock2.h>
#include <ws2tcpip.h>
#include <stdio.h>
#include <wchar.h>
#include <string.h>
#include "probe.h"
#include "cipher.h"
#include "log.h"
#include "sha256.h"

#pragma comment(lib, "ws2_32.lib")

#define PROBE_TIMEOUT_MS 3000

int probe_parse_frame(const unsigned char *plain, size_t len,
                      int *result_code, wchar_t *message, size_t msg_cap)
{
    unsigned payload_len;
    const unsigned char *payload;
    int code;

    if (len < 4 || plain[0] != 0xFE) return 0;
    payload_len = plain[2] | (plain[3] << 8);
    if (len < 4 + payload_len) return 0;
    payload = plain + 4;
    code = (int)((unsigned)payload[0] | ((unsigned)payload[1] << 8) |
                 ((unsigned)payload[2] << 16) | ((unsigned)payload[3] << 24));
    *result_code = code;
    message[0] = 0;
    if (payload_len >= 6) {
        unsigned chars = payload[4] | (payload[5] << 8);
        if (4 + 2 + chars * 2 <= payload_len) {
            /* UTF-16LE -> wchar（x86 小端直拷）。 */
            size_t n = chars;
            if (n > msg_cap - 1) n = msg_cap - 1;
            memcpy(message, payload + 6, n * 2);
            message[n] = 0;
        }
    }
    return 1;
}

int probe_parse_wanted(const wchar_t *message, Ver *out)
{
    const wchar_t *p = message;
    while (p && *p) {
        if (*p == L'v' || *p == L'V') {
            const wchar_t *q = p + 1;
            int segments[3];
            int count = 0, ok = 1;
            for (;;) {
                int digits = 0, value = 0;
                while (*q >= L'0' && *q <= L'9') {
                    value = value * 10 + (*q - L'0');
                    q++; digits++;
                }
                if (!digits) { ok = 0; break; }
                if (count < 3) segments[count] = value;
                count++;
                if (*q != L'.') break;
                q++;
            }
            /* [vV]数字.数字[.数字]：至少两段，至多三段。 */
            if (ok && count >= 2 && count <= 3) {
                int i;
                for (i = count; i < 3; i++) segments[i] = 0;
                out->major = segments[0];
                out->minor = segments[1];
                out->patch = segments[2];
                return 1;
            }
        }
        p++;
    }
    return 0;
}

/* 把 hook 的 SHA-256 折进上报值 —— 和 `hook/bshook.c` 的 `hsver_pick_wire()`
   同一套（布局和算法见 server/versioning.py 的 WIRE_V2_*）。

   ★ 为什么探针也要算：服务端现在会校验这个校验位，探针要是照旧发「裸版本号」，
   就会被一律判成「本该带校验位却没带」⇒ **`PROBE_OK` 永远不可能出现**，
   「服务器已接受当前版本，无需更新」这条路就废了。探针必须和真客户端
   **发一模一样的 4 个字节**。

   算不出来（找不到 DLL 之类）就退回旧编码 —— 那会被服务端判成「对不上」
   并要求更新，属于安全的一边（大不了多下一次包）。 */
#define PROBE_V2_FLAG      0x80000000u
#define PROBE_V2_VER_MAX   15999999u
#define PROBE_V2_TAG_SHIFT 24
#define PROBE_V2_TAG_MASK  0x7Fu

static int probe_hex_nibble(wchar_t c)
{
    if (c >= L'0' && c <= L'9') return (int)(c - L'0');
    if (c >= L'a' && c <= L'f') return (int)(c - L'a') + 10;
    if (c >= L'A' && c <= L'F') return (int)(c - L'A') + 10;
    return -1;
}

unsigned long probe_encode_wire(const Ver *v, const wchar_t *hook_dll)
{
    wchar_t hex[65], hex2[65];
    unsigned char buf[36];
    Sha256 s;
    long plain;
    unsigned int wire_v;
    int i, hi, lo;

    plain = v ? ver_encode_wire(v) : -1;
    if (plain < 0) return 311u;                  /* 编不出来：按原版上报 */
    wire_v = (unsigned int)plain;
    if (!hook_dll || !*hook_dll || wire_v > PROBE_V2_VER_MAX) return wire_v;

    if (!sha256_file(hook_dll, hex)) {
        log_line("probe: cannot hash %ls, falling back to plain wire", hook_dll);
        return wire_v;
    }
    for (i = 0; i < 32; i++) {
        hi = probe_hex_nibble(hex[i * 2]);
        lo = probe_hex_nibble(hex[i * 2 + 1]);
        if (hi < 0 || lo < 0) return wire_v;
        buf[i] = (unsigned char)((hi << 4) | lo);
    }
    buf[32] = (unsigned char)(wire_v & 0xffu);
    buf[33] = (unsigned char)((wire_v >> 8) & 0xffu);
    buf[34] = (unsigned char)((wire_v >> 16) & 0xffu);
    buf[35] = (unsigned char)((wire_v >> 24) & 0xffu);

    if (!sha256_begin(&s)) return wire_v;
    if (!sha256_update(&s, buf, sizeof(buf)) || !sha256_finish(&s, hex2)) {
        sha256_end(&s);
        return wire_v;
    }
    sha256_end(&s);
    hi = probe_hex_nibble(hex2[0]);
    lo = probe_hex_nibble(hex2[1]);
    if (hi < 0 || lo < 0) return wire_v;
    return PROBE_V2_FLAG
         | ((unsigned long)(((hi << 4) | lo) & PROBE_V2_TAG_MASK)
            << PROBE_V2_TAG_SHIFT)
         | wire_v;
}

int probe_server(const wchar_t *host, int port, const Ver *local_version,
                 const wchar_t *hook_dll, ProbeResult *out)
{
    WSADATA wsa;
    SOCKET sock = INVALID_SOCKET;
    struct sockaddr_in sa;
    unsigned long addr;
    u_long nb = 1;
    SimpleCipher c2s, s2c;
    unsigned char wire_le[4];
    unsigned char cipher_out[4];
    unsigned char plain[2048];
    size_t have = 0;
    int result_code = 0;
    long wire;
    int rc = 0;
    char host_mb[256];

    memset(out, 0, sizeof(*out));
    out->status = PROBE_UNREACHABLE;
    if (!host || !*host || port <= 0) return 0;

    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        out->status = PROBE_ERROR;
        return 0;
    }

    /* 域名/IP 都是 ASCII；宽转窄后走老 API（IPv6 玩家直接连 IP 的场景探针
       不支持 —— 原版客户端同样只认 IPv4，保持一致）。 */
    WideCharToMultiByte(CP_UTF8, 0, host, -1, host_mb, sizeof(host_mb),
                        NULL, NULL);
    host_mb[sizeof(host_mb) - 1] = 0;
    addr = inet_addr(host_mb);
    if (addr == INADDR_NONE) {
        struct hostent *he = gethostbyname(host_mb);
        if (!he || !he->h_addr_list || !he->h_addr_list[0]) goto unreachable;
        memcpy(&addr, he->h_addr_list[0], 4);
    }

    sock = socket(AF_INET, SOCK_STREAM, 0);
    if (sock == INVALID_SOCKET) goto unreachable;
    memset(&sa, 0, sizeof(sa));
    sa.sin_family = AF_INET;
    sa.sin_port = htons((u_short)port);
    sa.sin_addr.s_addr = addr;

    /* 非阻塞 connect + select 超时（python create_connection(timeout=3)）。 */
    ioctlsocket(sock, FIONBIO, &nb);
    if (connect(sock, (struct sockaddr *)&sa, sizeof(sa)) == SOCKET_ERROR) {
        fd_set wset;
        struct timeval tv;
        int cre;
        if (WSAGetLastError() != WSAEWOULDBLOCK) goto unreachable;
        FD_ZERO(&wset);
        FD_SET(sock, &wset);
        tv.tv_sec = PROBE_TIMEOUT_MS / 1000;
        tv.tv_usec = (PROBE_TIMEOUT_MS % 1000) * 1000;
        cre = select(0, NULL, &wset, NULL, &tv);
        if (cre <= 0) goto unreachable;
    }

    /* 发：编码版本号 int32 LE，整条流过 SimpleCipher（客户端->服务端 (0,1)）。
       本地版本编不出来（<0.1.0 的怪包）按旧版 311 上报。 */
    wire = (long)probe_encode_wire(local_version, hook_dll);
    wire_le[0] = (unsigned char)(wire & 0xFF);
    wire_le[1] = (unsigned char)((wire >> 8) & 0xFF);
    wire_le[2] = (unsigned char)((wire >> 16) & 0xFF);
    wire_le[3] = (unsigned char)((wire >> 24) & 0xFF);
    cipher_client_to_server(&c2s);
    cipher_encrypt(&c2s, wire_le, cipher_out, 4);
    {
        int sent = 0;
        while (sent < 4) {
            int n = send(sock, (const char *)cipher_out + sent, 4 - sent, 0);
            if (n <= 0) goto unreachable;
            sent += n;
        }
    }

    /* 收：解密直到凑满 0xFE 帧或超时（服务端->客户端 (5,3)）。 */
    cipher_server_to_client(&s2c);
    {
        ULONGLONG deadline = GetTickCount64() + PROBE_TIMEOUT_MS;
        for (;;) {
            unsigned char chunk[4096];
            unsigned char dec[4096];
            int n;
            fd_set rset;
            struct timeval tv;
            ULONGLONG left;
            int got;

            left = deadline - GetTickCount64();
            if (left > PROBE_TIMEOUT_MS) break;      /* 回绕 = 超时 */
            FD_ZERO(&rset);
            FD_SET(sock, &rset);
            tv.tv_sec = (long)(left / 1000);
            tv.tv_usec = (long)(left % 1000) * 1000;
            got = select(0, &rset, NULL, NULL, &tv);
            if (got <= 0) break;
            n = recv(sock, (char *)chunk, sizeof(chunk), 0);
            if (n <= 0) break;
            cipher_decrypt(&s2c, chunk, dec, (size_t)n);
            if (have + (size_t)n > sizeof(plain)) {
                memcpy(plain + have, dec, sizeof(plain) - have);
                have = sizeof(plain);
            } else {
                memcpy(plain + have, dec, (size_t)n);
                have += (size_t)n;
            }
            if (have >= 4 && plain[0] == 0xFE) {
                unsigned need = 4 + (unsigned)(plain[2] | (plain[3] << 8));
                if (have >= need) break;
            }
        }
    }

    if (!probe_parse_frame(plain, have, &result_code,
                           out->message, 256)) {
        log_line("probe frame unparseable (%u bytes)", (unsigned)have);
        goto unreachable;
    }
    if (result_code == 0) {
        out->status = PROBE_OK;
    } else {
        out->status = PROBE_REJECTED;
        out->wanted_valid = probe_parse_wanted(out->message, &out->wanted);
    }
    rc = 1;

unreachable:
    if (sock != INVALID_SOCKET) closesocket(sock);
    WSACleanup();
    return rc;
}
