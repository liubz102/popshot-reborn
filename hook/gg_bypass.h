#ifndef POPSHOT_GG_BYPASS_H
#define POPSHOT_GG_BYPASS_H

/* bsloader.exe 与 bshook.dll 之间的 GameGuard 绕过契约。
 * 两边必须由同一次 hook\build.bat 构建，避免地址或握手名漂移。 */
#define POPSHOT_GG_CHECK_VA          0x0054B0FCu
#define POPSHOT_GG_SUCCESS_CODE      0x00000755u
#define POPSHOT_GG_CHECK_INSN_LEN    5u

/* 校验点那 5 个字节的两种已知明文。解壳完成的**因果性**判据就是它
 * （§134）—— 两个二进制都要用，所以定义放在这里，别再各写一份。 */
#define POPSHOT_GG_ORIG_BYTES        { 0xE8, 0xCF, 0x60, 0x01, 0x00 } /* call 0x5611d0 */
#define POPSHOT_GG_OLD_PATCH_BYTES   { 0xB8, 0x55, 0x07, 0x00, 0x00 } /* 旧版内存 patch */

/* 四个命名事件（名字由 bsloader 生成后经环境变量传给 DLL）：
 *
 *   INJECTED  DLL 装好 VEH 了 —— 在这之前 bsloader **绝不能**从外部武装 DR0，
 *             否则断点命中时没人处理那个单步异常。DllMain 里就置位，
 *             不受加载器锁 / 线程调度影响（§179）。
 *   READY     DR0 已武装（DLL 内的武装线程置位；它一置位就接管守护，
 *             bsloader 停止外部补位）。
 *   HIT       DR0 真的命中过了 —— 绕过成功的唯一硬证据。
 *   FAILED    客户端弹了「Game guard 文件不存在或已变更」——绕过失败的硬证据，
 *             bsloader 见到它就重来一次，不必等玩家去点那个框。
 */
#define POPSHOT_BSHOOK_READY_ENV     "BSHOOK_READY_EVENT"
#define POPSHOT_BSHOOK_INJECTED_ENV  "BSHOOK_INJECTED_EVENT"
#define POPSHOT_BSHOOK_HIT_ENV       "BSHOOK_HIT_EVENT"
#define POPSHOT_BSHOOK_FAILED_ENV    "BSHOOK_FAILED_EVENT"

/* "1" = 这一次失败了还能再来一次，DLL 就把 GameGuard 那个错误框**吃掉**
 * （玩家看到的只是窗口闪一下，bsloader 随即自动重来）。最后一次尝试传 "0"，
 * 框照常弹出来 —— 全都失败了还不给提示，比弹框更糟。 */
#define POPSHOT_BSHOOK_RETRY_ENV     "BSHOOK_GG_RETRY"

/* bsloader 等「DR0 已武装」握手的毫秒数。DLL 那边的武装循环用它减 2000 当上限，
 * 好在 bsloader 判超时之前先把失败原因写进日志（bshook.c 的 §124 注释）。
 * 机器忙的时候 ASProtect 解壳本身就要好几秒，10 秒偏紧。 */
#define POPSHOT_BSHOOK_READY_TIMEOUT 15000u

/* DLL 注入本身（DllMain 跑到装完 VEH）等多久。走的是主线程上的 APC，
 * 不排队不抢锁，正常几毫秒；等不到就是根本没注进去（多半被安全软件拦了）。 */
#define POPSHOT_BSHOOK_INJECT_TIMEOUT 10000u

/* bsloader 外部武装的轮询间隔。DLL 内的守护是 10ms，这里更密一点：
 * 这段窗口正是「解壳刚完成、DLL 的线程还没被调度到」的高危期。 */
#define POPSHOT_GG_EXTERNAL_POLL_MS  2u

/* GameGuard 绕过失败时最多整体重来几次（含第一次）。 */
#define POPSHOT_GG_MAX_ATTEMPTS      3

/* DR0：L0/G0 + RW0/LEN0 对应的位。执行断点要求 RW0=00、LEN0=00。 */
#define POPSHOT_DR0_CONTROL_MASK     0x000F0003u
#define POPSHOT_DR0_LOCAL_ENABLE     0x00000001u

#endif
