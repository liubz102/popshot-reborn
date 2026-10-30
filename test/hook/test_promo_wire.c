/*
 * 晋级任务表 0x0F03 的拼装 + 喂给游戏加载器的只读流 —— 原生夹具（X_Mod X23 / D111）。
 *
 * 由 test-promo-wire.bat 编译成 32 位控制台程序独立跑，不需要游戏。
 * **直接 `#include "promo_wire.h"`**（hook/ 下那一份），测的就是 bshook.dll 里用的代码。
 *
 * 要守住的：
 *   1. crc32 和 zlib 逐位相同（"123456789" → CBF43926，空串 → 0）；
 *   2. 分片拼装：一片 / 多片 / 空表都拼得回原样；第 0 片重置（半截丢了照样能收下一份）；
 *      片头对不上、片号跳、偏移跳、多出数据、少一片、crc 错、格式号错、片头不完整 —— 一律整份丢弃；
 *   3. 只读流**按游戏的调法**调虚表（MSVC 32 位：ecx = this、参数压栈、`call [eax+0x18]`），
 *      调完 esp 回到原处（被调方弹 8 字节 / 0 字节，和 thiscall 一致）；
 *   4. 照游戏的读行函数（`0x5d5ba9`：一次 2 字节读到 `\n` 或读不出为止）+ 解析器每行问一次
 *      IsEof（`0x5d4bba`）把整份读完：没有结尾换行、奇数字节都不多读不少读；
 *   5. 别的槽全是 trap，+0x14 / +0x18 恰好是 IsEof / Read。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#if !defined(_MSC_VER)
#define PROMO_FASTCALL          /* 非 MSVC（开发机上顺手跑逻辑）：普通调用 */
#endif
#include "promo_wire.h"

static int g_fail;
static int g_checks;

#define CHECK(cond, ...) do {                                   \
        g_checks++;                                             \
        if (!(cond)) {                                          \
            if (g_fail < 40) { printf("  FAIL: "); printf(__VA_ARGS__); printf("\n"); } \
            g_fail++;                                           \
        }                                                       \
    } while (0)

/* ------------------------------------------------- 按游戏的调法调两个槽 */

#if defined(_MSC_VER) && defined(_M_IX86)
static int game_read(promo_stream *s, void *dst, unsigned n, int *esp_moved)
{
    int got;
    unsigned before, after;
    __asm {
        mov before, esp
        push n
        push dst
        mov ecx, s
        mov eax, [ecx]
        call dword ptr [eax + 0x18]
        mov got, eax
        mov after, esp
    }
    *esp_moved = (int)(after - before);
    return got;
}

static int game_eof(promo_stream *s, int *esp_moved)
{
    int answer;
    unsigned before, after;
    __asm {
        mov before, esp
        mov ecx, s
        mov eax, [ecx]
        call dword ptr [eax + 0x14]
        movzx eax, al
        mov answer, eax
        mov after, esp
    }
    *esp_moved = (int)(after - before);
    return answer;
}
#else
typedef int (*read_fn)(promo_stream *, void *, void *, unsigned);
typedef int (*eof_fn)(promo_stream *);

static int game_read(promo_stream *s, void *dst, unsigned n, int *esp_moved)
{
    *esp_moved = 0;
    return ((read_fn)s->vt[PROMO_VT_READ])(s, NULL, dst, n);
}

static int game_eof(promo_stream *s, int *esp_moved)
{
    *esp_moved = 0;
    return ((eof_fn)s->vt[PROMO_VT_ISEOF])(s) & 0xFF;
}
#endif

/* ------------------------------------------------------- 造片（同 Python） */

static void put16(unsigned char *p, unsigned v) { p[0] = (unsigned char)v; p[1] = (unsigned char)(v >> 8); }
static void put32(unsigned char *p, unsigned v)
{
    p[0] = (unsigned char)v; p[1] = (unsigned char)(v >> 8);
    p[2] = (unsigned char)(v >> 16); p[3] = (unsigned char)(v >> 24);
}

#define MAX_FRAMES 256        /* 「一大堆小片」那条用到 143 片 */

typedef struct { unsigned char *p; unsigned len; } frame_t;

/* `missions.build_table_frames` 的 C 版：data 切成 chunk 一片。返回片数。 */
static int make_frames(const unsigned char *data, unsigned total, unsigned serial,
                       unsigned chunk, frame_t *out)
{
    unsigned crc = promo_crc32(data, total), at = 0;
    int count = total ? (int)((total + chunk - 1) / chunk) : 1, i;
    if (count > MAX_FRAMES) {
        printf("  夹具自己的错：%u 字节 / %u 一片 = %d 片，超过 MAX_FRAMES\n", total, chunk, count);
        exit(2);
    }
    for (i = 0; i < count; i++) {
        unsigned body = total - at < chunk ? total - at : chunk;
        unsigned char *f = (unsigned char *)malloc(PROMO_HEADER_BYTES + body + 1);
        put16(f + 0, PROMO_FORMAT);
        put32(f + 2, serial);
        put32(f + 6, total);
        put32(f + 10, crc);
        put16(f + 14, (unsigned)i);
        put16(f + 16, (unsigned)count);
        put32(f + 18, at);
        memcpy(f + PROMO_HEADER_BYTES, data + at, body);
        out[i].p = f;
        out[i].len = PROMO_HEADER_BYTES + body;
        at += body;
    }
    return count;
}

static void free_frames(frame_t *f, int n)
{
    int i;
    for (i = 0; i < n; i++) free(f[i].p);
}

/* 依次喂，返回最后一片的结果；中途 BAD 就停。 */
static int feed_all(promo_asm *a, frame_t *f, int n, const char **why)
{
    int i, rc = PROMO_PART;
    for (i = 0; i < n; i++) {
        rc = promo_asm_feed(a, f[i].p, f[i].len, why);
        if (rc == PROMO_BAD) return rc;
        if (i < n - 1 && rc != PROMO_PART) return 99;      /* 提前说收齐了 = 错 */
    }
    return rc;
}

static unsigned char *pattern(unsigned n, unsigned seed)
{
    unsigned char *p = (unsigned char *)malloc(n ? n : 1);
    unsigned i, x = seed * 2654435761u + 1u;
    for (i = 0; i < n; i++) { x = x * 1103515245u + 12345u; p[i] = (unsigned char)(x >> 16); }
    return p;
}

/* ------------------------------------------------------------------ 用例 */

static void test_crc(void)
{
    CHECK(promo_crc32((const unsigned char *)"123456789", 9) == 0xCBF43926u, "crc32(123456789)");
    CHECK(promo_crc32((const unsigned char *)"", 0) == 0u, "crc32(空串)");
    CHECK(promo_crc32((const unsigned char *)"a", 1) == 0xE8B7BE43u, "crc32(a)");
}

static void roundtrip(unsigned total, unsigned chunk, unsigned serial)
{
    frame_t f[MAX_FRAMES];
    promo_asm a;
    unsigned char *data = pattern(total, serial), *got;
    unsigned size = 0, ser = 0, crc = 0;
    const char *why = "";
    int n = make_frames(data, total, serial, chunk, f), rc;

    memset(&a, 0, sizeof(a));
    rc = feed_all(&a, f, n, &why);
    CHECK(rc == PROMO_DONE, "%u 字节 / %u 一片：没拼成（rc=%d %s）", total, chunk, rc, why);
    if (rc == PROMO_DONE) {
        got = promo_asm_take(&a, &size, &ser, &crc);
        CHECK(size == total && ser == serial && crc == promo_crc32(data, total),
              "%u 字节：拿回来的长度 / 序号 / crc 不对", total);
        CHECK(got && (total == 0 || memcmp(got, data, total) == 0), "%u 字节：内容不对", total);
        free(got);
    }
    CHECK(a.buf == NULL && !a.active, "%u 字节：拿走之后状态没清", total);
    promo_asm_reset(&a);
    free_frames(f, n);
    free(data);
}

static void test_assembly(void)
{
    roundtrip(0, PROMO_CHUNK_MAX, 1);
    roundtrip(1, PROMO_CHUNK_MAX, 2);
    roundtrip(PROMO_CHUNK_MAX, PROMO_CHUNK_MAX, 3);          /* 正好一片 */
    roundtrip(PROMO_CHUNK_MAX + 1, PROMO_CHUNK_MAX, 4);      /* 多一字节就两片 */
    roundtrip(103117, PROMO_CHUNK_MAX, 5);                   /* 出厂表那个量级，四片 */
    roundtrip(1000, 7, 6);                                   /* 一大堆小片 */
}

/* 先喂半截 A，再从第 0 片喂完整的 B：B 照收（半截的 A 作废）。 */
static void test_restart(void)
{
    frame_t fa[MAX_FRAMES], fb[MAX_FRAMES];
    promo_asm a;
    unsigned char *da = pattern(70000, 11), *db = pattern(50000, 12), *got;
    unsigned size, ser, crc;
    const char *why = "";
    int na = make_frames(da, 70000, 11, PROMO_CHUNK_MAX, fa);
    int nb = make_frames(db, 50000, 12, PROMO_CHUNK_MAX, fb);
    int rc;

    memset(&a, 0, sizeof(a));
    CHECK(promo_asm_feed(&a, fa[0].p, fa[0].len, &why) == PROMO_PART, "A 第 0 片");
    rc = feed_all(&a, fb, nb, &why);
    CHECK(rc == PROMO_DONE, "半截 A 之后的完整 B 没收下（rc=%d %s）", rc, why);
    if (rc == PROMO_DONE) {
        got = promo_asm_take(&a, &size, &ser, &crc);
        CHECK(size == 50000 && ser == 12 && memcmp(got, db, 50000) == 0, "收下的不是 B");
        free(got);
    }
    promo_asm_reset(&a);
    free_frames(fa, na);
    free_frames(fb, nb);
    free(da);
    free(db);
}

/* 坏的那几种：每一种都要整份丢弃、状态清空。 */
static void expect_bad(const char *name, frame_t *f, int n)
{
    promo_asm a;
    const char *why = "";
    int i, rc = PROMO_PART;
    memset(&a, 0, sizeof(a));
    for (i = 0; i < n && rc != PROMO_BAD; i++)
        rc = promo_asm_feed(&a, f[i].p, f[i].len, &why);
    CHECK(rc == PROMO_BAD, "%s：没被拒（rc=%d）", name, rc);
    CHECK(rc != PROMO_BAD || (why && why[0]), "%s：拒了却没说原因", name);
    CHECK(a.buf == NULL && !a.active, "%s：拒了之后状态没清", name);
    promo_asm_reset(&a);
}

static void test_rejects(void)
{
    frame_t f[MAX_FRAMES], g[MAX_FRAMES];
    unsigned char *data = pattern(100000, 21);
    int n = make_frames(data, 100000, 21, PROMO_CHUNK_MAX, f), m;
    frame_t tmp[MAX_FRAMES];
    unsigned char short_head[PROMO_HEADER_BYTES - 1];
    promo_asm a;
    const char *why = "";

    CHECK(n == 4, "100000 字节该是 4 片（%d）", n);

    /* 没收到第 0 片 */
    expect_bad("没有第 0 片", f + 1, 1);
    /* 片号跳 */
    tmp[0] = f[0]; tmp[1] = f[2];
    expect_bad("片号跳", tmp, 2);
    /* 少最后一片（再来一份的第 0 片 = 重置，不算错）—— 直接看最后状态 */
    memset(&a, 0, sizeof(a));
    CHECK(promo_asm_feed(&a, f[0].p, f[0].len, &why) == PROMO_PART
          && promo_asm_feed(&a, f[1].p, f[1].len, &why) == PROMO_PART
          && promo_asm_feed(&a, f[2].p, f[2].len, &why) == PROMO_PART
          && a.active, "少一片时应该停在「还没收齐」");
    promo_asm_reset(&a);

    /* 片头对不上：第 1 片的 serial / crc / total / count 各改一次 */
    {
        static const unsigned at[4] = {2, 10, 6, 16};
        int k;
        for (k = 0; k < 4; k++) {
            unsigned char *copy = (unsigned char *)malloc(f[1].len);
            memcpy(copy, f[1].p, f[1].len);
            copy[at[k]] ^= 0x01;
            tmp[0] = f[0]; tmp[1].p = copy; tmp[1].len = f[1].len;
            expect_bad("片头和第 0 片对不上", tmp, 2);
            free(copy);
        }
    }
    /* 偏移跳 */
    {
        unsigned char *copy = (unsigned char *)malloc(f[1].len);
        memcpy(copy, f[1].p, f[1].len);
        put32(copy + 18, 1);
        tmp[0] = f[0]; tmp[1].p = copy; tmp[1].len = f[1].len;
        expect_bad("偏移不连续", tmp, 2);
        free(copy);
    }
    /* 格式号不认识 */
    {
        unsigned char *copy = (unsigned char *)malloc(f[0].len);
        memcpy(copy, f[0].p, f[0].len);
        put16(copy, PROMO_FORMAT + 1);
        tmp[0].p = copy; tmp[0].len = f[0].len;
        expect_bad("格式号不认识", tmp, 1);
        free(copy);
    }
    /* 片头不完整 */
    memcpy(short_head, f[0].p, sizeof(short_head));
    tmp[0].p = short_head; tmp[0].len = sizeof(short_head);
    expect_bad("片头不完整", tmp, 1);
    /* crc 错（数据被改了一个字节） */
    {
        unsigned char *copy = (unsigned char *)malloc(f[3].len);
        memcpy(copy, f[3].p, f[3].len);
        copy[f[3].len - 1] ^= 0x80;
        tmp[0] = f[0]; tmp[1] = f[1]; tmp[2] = f[2]; tmp[3].p = copy; tmp[3].len = f[3].len;
        expect_bad("crc 不对", tmp, 4);
        free(copy);
    }
    /* 多出数据：最后一片多带 1 字节 */
    {
        unsigned char *copy = (unsigned char *)malloc(f[3].len + 1);
        memcpy(copy, f[3].p, f[3].len);
        copy[f[3].len] = 0;
        tmp[0] = f[0]; tmp[1] = f[1]; tmp[2] = f[2]; tmp[3].p = copy; tmp[3].len = f[3].len + 1;
        expect_bad("数据比总长还多", tmp, 4);
        free(copy);
    }
    /* 总长 / 片数不像话 */
    m = make_frames(data, 10, 22, PROMO_CHUNK_MAX, g);
    put32(g[0].p + 6, PROMO_TOTAL_MAX + 1u);
    expect_bad("总长超上限", g, 1);
    put32(g[0].p + 6, 10);
    put16(g[0].p + 16, 0);
    expect_bad("片数为 0", g, 1);
    put16(g[0].p + 16, 11);
    expect_bad("片数比总长还多", g, 1);
    free_frames(g, m);

    free_frames(f, n);
    free(data);
}

/* --------------------------------------------------- 照游戏读完一整份 */

/* `0x5d5ba9`：一次 Read(…,2) 直到读到 `\n` 或读不出。返回这一行的 wchar 数（含 `\n`）。
   ★ 和游戏一样，那 4 字节的缓冲**不清零**：只读到 1 个字节时高字节是上一次的残值。 */
static int game_read_line(promo_stream *s, unsigned short *line, int cap, int *bad_esp)
{
    unsigned buf = 0;
    int n = 0, got, moved;
    for (;;) {
        got = game_read(s, &buf, 2, &moved);
        if (moved != 0) (*bad_esp)++;
        if (got > 0 && n < cap) line[n++] = (unsigned short)buf;
        if ((unsigned short)buf == '\n' || got == 0) break;
    }
    return n;
}

static void read_like_the_game(const char *name, const unsigned char *data, unsigned size,
                               int want_lines, unsigned want_units)
{
    promo_stream s;
    unsigned short line[512];
    unsigned char bom[2];
    int lines = 0, bad_esp = 0, moved, got, eof;
    unsigned units = 0;

    promo_stream_open(&s, data, size);
    /* 解析器先读两次单字节认 BOM（`0x5d5942`）。 */
    got = game_read(&s, &bom[0], 1, &moved); if (moved) bad_esp++;
    got += game_read(&s, &bom[1], 1, &moved); if (moved) bad_esp++;
    CHECK(got == 2 && bom[0] == 0xFF && bom[1] == 0xFE, "%s：BOM 没读对", name);
    do {
        int n = game_read_line(&s, line, 512, &bad_esp);
        units += (unsigned)n;
        if (n) lines++;
        eof = game_eof(&s, &moved);
        if (moved) bad_esp++;
    } while (!eof);
    CHECK(bad_esp == 0, "%s：有 %d 次调用之后 esp 没回原处", name, bad_esp);
    CHECK(lines == want_lines, "%s：读出 %d 行（该 %d）", name, lines, want_lines);
    CHECK(units == want_units, "%s：读出 %u 个 wchar（该 %u）", name, units, want_units);
    CHECK(s.pos == size, "%s：没读完（%u / %u）", name, s.pos, size);
    CHECK(game_read(&s, line, 2, &moved) == 0 && game_eof(&s, &moved) == 1,
          "%s：读完之后还读得出东西", name);
    CHECK(s.traps == 0, "%s：意外的槽被调了 %u 次", name, s.traps);
}

static void test_stream(void)
{
    /* "[A]\r\nk=v\r\n" UTF-16LE + BOM：两行 */
    static const unsigned char two_lines[] = {
        0xFF,0xFE, '[',0, 'A',0, ']',0, '\r',0, '\n',0, 'k',0, '=',0, 'v',0, '\r',0, '\n',0 };
    /* 最后一行没有换行 */
    static const unsigned char no_tail[] = {
        0xFF,0xFE, '[',0, 'A',0, ']',0, '\r',0, '\n',0, 'k',0, '=',0, 'v',0 };
    /* 奇数字节：最后多半个 wchar */
    static const unsigned char odd[] = {
        0xFF,0xFE, '[',0, 'A',0, ']',0, '\r',0, '\n',0, 'k',0 };
    /* 只有 BOM */
    static const unsigned char only_bom[] = { 0xFF,0xFE };
    int i, others = 0;

    read_like_the_game("两行", two_lines, sizeof(two_lines), 2, 10);
    read_like_the_game("没有结尾换行", no_tail, sizeof(no_tail), 2, 8);
    read_like_the_game("奇数字节", odd, sizeof(odd), 2, 6);
    read_like_the_game("只有 BOM", only_bom, sizeof(only_bom), 0, 0);

    for (i = 0; i < PROMO_VT_SLOTS; i++) {
        if (i == PROMO_VT_ISEOF || i == PROMO_VT_READ) continue;
        if (PROMO_STREAM_VT[i] == (promo_fn)promo_stream_trap) others++;
    }
    CHECK(PROMO_VT_ISEOF * 4 == 0x14 && PROMO_VT_READ * 4 == 0x18, "槽位偏移不对");
    CHECK(PROMO_STREAM_VT[PROMO_VT_ISEOF] == (promo_fn)promo_stream_eof, "+0x14 不是 IsEof");
    CHECK(PROMO_STREAM_VT[PROMO_VT_READ] == (promo_fn)promo_stream_read, "+0x18 不是 Read");
    CHECK(others == PROMO_VT_SLOTS - 2, "别的槽不全是 trap（%d）", others);
}

static void test_count_sections(void)
{
    static const unsigned char ini[] = {
        0xFF,0xFE, '#',0, '[',0, '\r',0, '\n',0,
        '[',0, 'A',0, ']',0, '\r',0, '\n',0, 'k',0, '=',0, '[',0, '\r',0, '\n',0,
        '[',0, 'B',0, ']',0, '\r',0, '\n',0 };
    CHECK(promo_count_sections(ini, sizeof(ini)) == 2, "数节数：行首的 [ 才算");
}

int main(void)
{
    test_crc();
    test_assembly();
    test_restart();
    test_rejects();
    test_stream();
    test_count_sections();
    printf("promo_wire: %d checks, %d failed\n", g_checks, g_fail);
    return g_fail ? 1 : 0;
}
