/* ========================================================================
 *  aimring.h —— 弹匣容量 → 准星外圈帧号。
 *
 *  ★★ 【自动生成，不要手改】
 *      源头是 tools/aimring.py（它同时画出那些帧），
 *      生成物和图集分叉会被 test/test_aimring.py 当场抓住。
 *
 *  原版 0x48ca0d 只认 6 个容量（2/3/6/10/14/18），其余一律返回帧 4 ——
 *  一张没有刻度的光滑圆环。自定义武器配到 15 / 20 发就会撞上它（FINDINGS §43）。
 *  bshook 把 0x48ca0d 整个改写成跳到我们的实现，用下面这张表作答。
 *
 *  原版那 6 个容量仍然返回原版帧号 —— 原版武器的观感一个像素都不变。
 * ====================================================================== */
#ifndef POPSHOT_AIMRING_H
#define POPSHOT_AIMRING_H

#define POPSHOT_AIM_DEFAULT_FRAME  4   /* 光滑圆环：cap < 2 或 cap > 20 */
#define POPSHOT_AIM_DIAL_MIN       2   /* cap <= 1 客户端压根不画外圈 */
#define POPSHOT_AIM_DIAL_MAX       20   /* 用户拍板：超过就用光滑圆环，格子太密反而糊 */
#define POPSHOT_AIM_ORIG_FRAMES    19   /* 原版图集帧数 */
#define POPSHOT_AIM_TOTAL_FRAMES   32   /* 补画之后的帧数 */

/* 下标 = 弹匣容量，值 = 帧号。0 / 1 两格占位，取不到就是默认帧。 */
static const unsigned char POPSHOT_AIM_FRAME[POPSHOT_AIM_DIAL_MAX + 1] = {
    /* cap=0  */  4,   /* 不画外圈 */
    /* cap=1  */  4,   /* 不画外圈 */
    /* cap=2  */  6,   /* 原版 */
    /* cap=3  */  5,   /* 原版 */
    /* cap=4  */ 19,   /* 补画 */
    /* cap=5  */ 20,   /* 补画 */
    /* cap=6  */  6,   /* 原版 */
    /* cap=7  */ 21,   /* 补画 */
    /* cap=8  */ 22,   /* 补画 */
    /* cap=9  */ 23,   /* 补画 */
    /* cap=10 */  8,   /* 原版 */
    /* cap=11 */ 24,   /* 补画 */
    /* cap=12 */ 25,   /* 补画 */
    /* cap=13 */ 26,   /* 补画 */
    /* cap=14 */  7,   /* 原版 */
    /* cap=15 */ 27,   /* 补画 */
    /* cap=16 */ 28,   /* 补画 */
    /* cap=17 */ 29,   /* 补画 */
    /* cap=18 */  9,   /* 原版 */
    /* cap=19 */ 30,   /* 补画 */
    /* cap=20 */ 31,   /* 补画 */
};

#endif /* POPSHOT_AIMRING_H */
