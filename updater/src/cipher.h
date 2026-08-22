/* --------------------------------------------------------------------------
   cipher.h —— SimpleCipher（游戏服 27799 那层流密码）的 C 移植。

   源：server\simple.py（逆向自 BigShot_22524.exe vftable 0x64dd54）。
   dst[i] = src[i] + tblA[i1] + tblB[i2]，每字节 i1=(i1+1)%49、i2=(i2+1)%24，
   状态跨调用保持（整条 TCP 流是连续的）。
   客户端->服务端初态 (0,1)，服务端->客户端初态 (5,3)。
   -------------------------------------------------------------------------- */
#ifndef UPDATER_CIPHER_H
#define UPDATER_CIPHER_H

#include <stddef.h>

typedef struct SimpleCipher {
    int i1, i2;
} SimpleCipher;

void cipher_client_to_server(SimpleCipher *c);   /* (0,1) */
void cipher_server_to_client(SimpleCipher *c);   /* (5,3) */
void cipher_encrypt(SimpleCipher *c, const unsigned char *src,
                    unsigned char *dst, size_t len);
void cipher_decrypt(SimpleCipher *c, const unsigned char *src,
                    unsigned char *dst, size_t len);

#endif /* UPDATER_CIPHER_H */
