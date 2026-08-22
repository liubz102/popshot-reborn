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

int probe_server(const wchar_t *host, int port, const Ver *local_version,
                 ProbeResult *out)
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
    wire = local_version ? ver_encode_wire(local_version) : -1;
    if (wire < 0) wire = 311;
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
