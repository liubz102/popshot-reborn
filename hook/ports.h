/* ========================================================================
 *  ports.h —— 端口号常量。
 *
 *  ★★ 【自动生成，不要手改】
 *      源头是 server/config.py，生成器是 tools/gen_ports_h.py。
 *      要改端口只改 server/config.py 一处，重新编译即可
 *（build.bat 会自己重新生成）。
 *
 *  这里的每一个号在 Python 那边都有同名常量，两边分叉会被
 *  server/test_ports.py 当场抓住。
 * ====================================================================== */
#ifndef POPSHOT_PORTS_H
#define POPSHOT_PORTS_H

#define POPSHOT_AUTH_PORT              47611   /* 认证服（客户端写死，V0.1 §24） */
#define POPSHOT_GAME_PORT              27799   /* 游戏服（客户端写死，V0.1 §40） */
#define POPSHOT_CONTROL_PORT           27800   /* 调试控制通道（只绑 127.0.0.1） */
#define POPSHOT_PEER_RELAY_PORT        27798   /* 原版 TCP 中继（服务端侧，D078/D079） */
#define POPSHOT_UDP_SYNC_PORT          27799   /* 位置数据的 UDP 通道（和游戏服 TCP 同号） */
#define POPSHOT_RELAY_AUTH_PORT        47621   /* 本机中继：认证 */
#define POPSHOT_RELAY_GAME_PORT        27809   /* 本机中继：游戏 */
#define POPSHOT_RELAY_PEER_PORT        27808   /* 本机中继：战斗中继 */
#define POPSHOT_RELAY_UDP_SYNC_PORT    27809   /* 本机中继：位置数据（UDP） */
#define POPSHOT_GAME_ORIGINAL_UDP_PORT 7788   /* 原版 UDPBinder 写死要 bind 的口（§153），要改写掉 */
#define POPSHOT_CLIENT_UDP_PORT        27807   /* 改写成这个号：游戏【接收】位置数据的 UDP 口 */
#define POPSHOT_DEFAULT_REGISTER_PORT  27810   /* 注册页默认端口（server.config 可改，这里只是缺省） */

#endif /* POPSHOT_PORTS_H */
