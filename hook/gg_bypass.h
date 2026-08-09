#ifndef POPSHOT_GG_BYPASS_H
#define POPSHOT_GG_BYPASS_H

/* bsloader.exe 与 bshook.dll 之间的 GameGuard 绕过契约。
 * 两边必须由同一次 hook\build.bat 构建，避免地址或握手名漂移。 */
#define POPSHOT_GG_CHECK_VA          0x0054B0FCu
#define POPSHOT_GG_SUCCESS_CODE      0x00000755u
#define POPSHOT_GG_CHECK_INSN_LEN    5u

#define POPSHOT_BSHOOK_READY_ENV     "BSHOOK_READY_EVENT"
#define POPSHOT_BSHOOK_READY_TIMEOUT 10000u

/* DR0：L0/G0 + RW0/LEN0 对应的位。执行断点要求 RW0=00、LEN0=00。 */
#define POPSHOT_DR0_CONTROL_MASK     0x000F0003u
#define POPSHOT_DR0_LOCAL_ENABLE     0x00000001u

#endif
