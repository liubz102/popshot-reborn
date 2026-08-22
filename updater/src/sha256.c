/* --------------------------------------------------------------------------
   sha256.c —— 见 sha256.h。
   -------------------------------------------------------------------------- */
#define WIN32_LEAN_AND_MEAN
#define _CRT_SECURE_NO_WARNINGS
#include <windows.h>
#include <bcrypt.h>
#include <stdio.h>
#include <wchar.h>
#include "sha256.h"

#pragma comment(lib, "bcrypt.lib")

int sha256_begin(Sha256 *s)
{
    NTSTATUS st;
    DWORD got_dummy = 0;

    s->alg = NULL;
    s->hash = NULL;
    s->obj = NULL;
    s->objlen = 0;

    st = BCryptOpenAlgorithmProvider(&s->alg, BCRYPT_SHA256_ALGORITHM, NULL, 0);
    if (!BCRYPT_SUCCESS(st)) return 0;
    st = BCryptGetProperty(s->alg, BCRYPT_OBJECT_LENGTH,
                           (PUCHAR)&s->objlen, sizeof(s->objlen),
                           &got_dummy, 0);
    if (!BCRYPT_SUCCESS(st)) { BCryptCloseAlgorithmProvider(s->alg, 0); s->alg = NULL; return 0; }
    s->obj = (unsigned char *)HeapAlloc(GetProcessHeap(), 0, s->objlen);
    if (!s->obj) { BCryptCloseAlgorithmProvider(s->alg, 0); s->alg = NULL; return 0; }
    st = BCryptCreateHash(s->alg, &s->hash, s->obj, s->objlen,
                          NULL, 0, 0);
    if (!BCRYPT_SUCCESS(st)) {
        HeapFree(GetProcessHeap(), 0, s->obj);
        s->obj = NULL;
        BCryptCloseAlgorithmProvider(s->alg, 0);
        s->alg = NULL;
        return 0;
    }
    return 1;
}

int sha256_update(Sha256 *s, const void *data, unsigned long len)
{
    NTSTATUS st = BCryptHashData(s->hash, (PUCHAR)data, len, 0);
    return BCRYPT_SUCCESS(st) ? 1 : 0;
}

int sha256_finish(Sha256 *s, wchar_t hex_out[65])
{
    unsigned char digest[32];
    NTSTATUS st;
    int i;
    static const wchar_t hex[] = L"0123456789abcdef";

    st = BCryptFinishHash(s->hash, digest, sizeof(digest), 0);
    if (!BCRYPT_SUCCESS(st)) return 0;
    for (i = 0; i < 32; i++) {
        hex_out[i * 2]     = hex[digest[i] >> 4];
        hex_out[i * 2 + 1] = hex[digest[i] & 15];
    }
    hex_out[64] = 0;
    return 1;
}

void sha256_end(Sha256 *s)
{
    if (s->hash) { BCryptDestroyHash(s->hash); s->hash = NULL; }
    if (s->obj) { HeapFree(GetProcessHeap(), 0, s->obj); s->obj = NULL; }
    if (s->alg) { BCryptCloseAlgorithmProvider(s->alg, 0); s->alg = NULL; }
}

int sha256_file(const wchar_t *path, wchar_t hex_out[65])
{
    HANDLE f;
    static unsigned char buf[1 << 20];
    DWORD got;
    Sha256 s;

    f = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, NULL,
                    OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (f == INVALID_HANDLE_VALUE) return 0;
    if (!sha256_begin(&s)) { CloseHandle(f); return 0; }
    for (;;) {
        if (!ReadFile(f, buf, sizeof(buf), &got, NULL)) {
            sha256_end(&s);
            CloseHandle(f);
            return 0;
        }
        if (!got) break;
        if (!sha256_update(&s, buf, got)) {
            sha256_end(&s);
            CloseHandle(f);
            return 0;
        }
    }
    CloseHandle(f);
    if (!sha256_finish(&s, hex_out)) { sha256_end(&s); return 0; }
    sha256_end(&s);
    return 1;
}
