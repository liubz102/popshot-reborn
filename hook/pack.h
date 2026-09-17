/* ========================================================================
 *  pack.h —— 客户端资源目录名。
 *
 *  ★★ 【自动生成，不要手改】
 *      源头是 server/config.py，生成器是 tools/gen_pack_h.py。
 *      要改目录名只改 server/config.py 一处，重新编译即可
 *（build.bat 会自己重新生成）。
 *
 *  每个名字都给窄串和宽串两个宏；Python 那边有同名常量，两边分叉会被
 *  test/test_packdirs.py 当场抓住。
 * ====================================================================== */
#ifndef POPSHOT_PACK_H
#define POPSHOT_PACK_H

#define POPSHOT_PACK_LEGACY_DIR    "Pack"   /* 原版客户端写死的资源目录（镜像 0x6936b0 L"Pack/"），也是密钥前缀，永远不改 */
#define POPSHOT_PACK_LEGACY_DIR_W  L"Pack"
#define POPSHOT_PACK_DEVELOP_DIR   "Pack_develop"   /* 明文资源树（hook 不用，只为三边同源） */
#define POPSHOT_PACK_DEVELOP_DIR_W L"Pack_develop"
#define POPSHOT_PACK_PUBLISH_DIR   "Pack_publish"   /* 加密卷所在目录：hook 把 Pack\*.pkn 的打开重定向到这里 */
#define POPSHOT_PACK_PUBLISH_DIR_W L"Pack_publish"

#endif /* POPSHOT_PACK_H */
