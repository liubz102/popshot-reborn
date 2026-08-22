/* --------------------------------------------------------------------------
   cipher.c —— 见 cipher.h。表和语义与 server\simple.py 逐字节一致
   （selftest 钉住：37 01 00 00 <-> 53 72 8f 7f，流状态连续性）。
   -------------------------------------------------------------------------- */
#include "cipher.h"

/* tblA@0x64dd64 49 字节（Aleph One 经典 execve("/bin/sh") shellcode）、
   tblB@0x64dd98 24 字节。 */
static const unsigned char TBL_A[49] = {
    0xeb,0x1a,0x5e,0x31,0xc0,0x88,0x46,0x07,0x8d,0x1e,0x89,0x5e,0x08,0x89,
    0x46,0x0c,0xb0,0x0b,0x89,0xf3,0x8d,0x4e,0x08,0x8d,0x56,0x0c,0xcd,0x80,
    0xe8,0xe1,0xff,0xff,0xff,0x2f,0x62,0x69,0x6e,0x2f,0x73,0x68,0x23,0x41,
    0x41,0x41,0x41,0x42,0x42,0x42,0x42
};
static const unsigned char TBL_B[24] = {
    0x38,0x31,0x57,0x31,0x4e,0x31,0x4f,0x31,0x34,0x31,0x4f,0x31,
    0x47,0x31,0x62,0x31,0x00,0xac,0x48,0x31,0x57,0x31,0x31,0x31
};

void cipher_client_to_server(SimpleCipher *c) { c->i1 = 0; c->i2 = 1; }
void cipher_server_to_client(SimpleCipher *c) { c->i1 = 5; c->i2 = 3; }

static void cipher_step(SimpleCipher *c, const unsigned char *src,
                        unsigned char *dst, size_t len, int add)
{
    size_t k;
    int i1 = c->i1, i2 = c->i2;
    for (k = 0; k < len; k++) {
        int v = add ? (src[k] + TBL_A[i1] + TBL_B[i2])
                    : (src[k] - TBL_A[i1] - TBL_B[i2]);
        dst[k] = (unsigned char)v;
        i1 = (i1 + 1) % 49;
        i2 = (i2 + 1) % 24;
    }
    c->i1 = i1;
    c->i2 = i2;
}

void cipher_encrypt(SimpleCipher *c, const unsigned char *src,
                    unsigned char *dst, size_t len)
{
    cipher_step(c, src, dst, len, 1);
}

void cipher_decrypt(SimpleCipher *c, const unsigned char *src,
                    unsigned char *dst, size_t len)
{
    cipher_step(c, src, dst, len, 0);
}
