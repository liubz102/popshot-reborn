/* --------------------------------------------------------------------------
   selftest.c —— 回归自检（build.bat 的构建闸门）+ --preview 视觉预览。

   向量来源：
     * cipher   —— server\simple.py 的实测对（37 01 00 00 <-> 53 72 8f 7f）
     * 版本号   —— server\versioning.py 的语义（编码/比较/边界）
     * 0xFE 帧  —— gameserver 的 build_ctrl + w_i32 + w_wstr 镜像
     * sha256   —— 标准向量（"abc" / 空串）
     * 保护清单 —— tools\update_client.py 的 PROTECTED_PATHS 语义
     * 代理列表 —— config\update.config 的解析规则（config.c）
     * 选源     —— speedtest.c 的编排规则，用假测速函数钉住
                   （直连够快不测代理 / 组内取最快 / 达标即停 / 全不达标取相对最快 / 取消）
     * 兜底     —— manifest 的代理兜底（直连败 → 随机顺序逐个试、每个一次 / 全败 / 取消）
     * 资源     —— updater.rc 嵌的界面素材逐个 FindResource

   selftest 是 GUI 子系统程序：AttachConsole(ATTACH_PARENT_PROCESS) 让
   构建脚本能看见输出；结果同时写 exe 旁的 selftest.log。
   -------------------------------------------------------------------------- */
#define WIN32_LEAN_AND_MEAN
#define _CRT_SECURE_NO_WARNINGS
#include <windows.h>
#include <stdio.h>
#include <wchar.h>
#include <string.h>
#include "util.h"
#include "cipher.h"
#include "sha256.h"
#include "manifest.h"
#include "probe.h"
#include "apply.h"
#include "config.h"
#include "speedtest.h"
#include "ui_window.h"

static int g_fail;
static int g_total;
static FILE *g_logf;

static void say(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    if (g_logf) {
        va_start(ap, fmt);
        vfprintf(g_logf, fmt, ap);
        va_end(ap);
    }
}

static void check(int ok, const char *what)
{
    g_total++;
    if (ok) {
        say("PASS  %s\n", what);
    } else {
        g_fail++;
        say("FAIL  %s\n", what);
    }
}

static void check_ver(const wchar_t *text, int expect_ok,
                      int e1, int e2, int e3)
{
    Ver v;
    int ok = ver_parse(text, &v);
    char label[128];
    char text8[64];
    wide_to_utf8(text, text8, sizeof(text8));
    if (expect_ok)
        _snprintf(label, sizeof(label), "ver_parse(%s) == %d.%d.%d",
                  text8, e1, e2, e3);
    else
        _snprintf(label, sizeof(label), "ver_parse(%s) rejected", text8);
    label[sizeof(label) - 1] = 0;
    check(ok == expect_ok && (!expect_ok ||
          (v.major == e1 && v.minor == e2 && v.patch == e3)), label);
}

static void cipher_tests(void)
{
    SimpleCipher c, d;
    unsigned char in[4] = { 0x37, 0x01, 0x00, 0x00 };
    unsigned char out[4], back[4];

    cipher_client_to_server(&c);
    cipher_encrypt(&c, in, out, 4);
    check(out[0] == 0x53 && out[1] == 0x72 && out[2] == 0x8F && out[3] == 0x7F,
          "cipher encrypt 37 01 00 00 -> 53 72 8f 7f");

    cipher_client_to_server(&d);
    cipher_decrypt(&d, out, back, 4);
    check(memcmp(in, back, 4) == 0, "cipher decrypt roundtrip");

    {
        /* 流连续性：一次处理 60 字节 == 分 20/40 两次（server->client 键）。 */
        unsigned char src[60], one[60], two[60];
        SimpleCipher a, b;
        int i;
        for (i = 0; i < 60; i++) src[i] = (unsigned char)i;
        cipher_server_to_client(&a);
        cipher_encrypt(&a, src, one, 60);
        cipher_server_to_client(&b);
        cipher_encrypt(&b, src, two, 20);
        cipher_encrypt(&b, src + 20, two + 20, 40);
        check(memcmp(one, two, 60) == 0, "cipher stream continuity (5,3)");
    }
}

static void version_tests(void)
{
    Ver a, b, v;

    check_ver(L"0.2.7", 1, 0, 2, 7);
    check_ver(L"v0.2", 1, 0, 2, 0);
    check_ver(L"V1.2.3", 1, 1, 2, 3);
    check_ver(L"5", 1, 5, 0, 0);
    check_ver(L"# comment\r\n0.2.8\n", 1, 0, 2, 8);
    check_ver(L"abc", 0, 0, 0, 0);
    check_ver(L"0.2.3.4", 0, 0, 0, 0);
    check_ver(L"3000.1.1", 0, 0, 0, 0);
    check_ver(L"0.2.1000", 0, 0, 0, 0);
    check_ver(L"1..2", 0, 0, 0, 0);
    check_ver(L"", 0, 0, 0, 0);

    a.major = 0; a.minor = 2; a.patch = 7;
    check(ver_encode_wire(&a) == 2007, "encode_wire(0.2.7) == 2007");
    b.major = 0; b.minor = 0; b.patch = 311;
    check(ver_encode_wire(&b) == -1, "encode_wire(0.0.311) rejected (311)");
    b.patch = 5;
    check(ver_encode_wire(&b) == -1, "encode_wire(0.0.5) rejected (<1000)");
    b.major = 1; b.minor = 0; b.patch = 0;
    check(ver_encode_wire(&b) == 1000000, "encode_wire(1.0.0) == 1000000");

    v.major = 0; v.minor = 2; v.patch = 6;
    check(ver_cmp(&v, &a) < 0, "ver_cmp(0.2.6 < 0.2.7)");
    check(ver_cmp(&a, &a) == 0, "ver_cmp(0.2.7 == 0.2.7)");
}

static void frame_tests(void)
{
    /* [FE][00][u16 载荷长][int32 结果码=1][u16 字数=6][UTF-16LE "V0.2.8"] */
    unsigned char frame[4 + 4 + 2 + 12];
    int code = 0;
    wchar_t msg[64];
    Ver w;

    frame[0] = 0xFE; frame[1] = 0x00;
    frame[2] = 0x12; frame[3] = 0x00;             /* 载荷 18 字节 */
    frame[4] = 0x01; frame[5] = 0x00; frame[6] = 0x00; frame[7] = 0x00;
    frame[8] = 0x06; frame[9] = 0x00;
    {
        static const wchar_t text[] = L"V0.2.8";
        memcpy(frame + 10, text, 12);
    }
    check(probe_parse_frame(frame, sizeof(frame), &code, msg, 64) &&
          code == 1 && wcscmp(msg, L"V0.2.8") == 0,
          "0xFE frame parse (code=1, text=V0.2.8)");

    check(probe_parse_frame(frame, 5, &code, msg, 64) == 0,
          "0xFE frame rejects truncated input");

    {
        static const wchar_t reject[] =
            L"客户端版本过旧，请更新到 V0.2.8 后再连接。";
        check(probe_parse_wanted(reject, &w) &&
              w.major == 0 && w.minor == 2 && w.patch == 8,
              "wanted-version regex from reject message");
    }
    {
        static const wchar_t none[] = L"没有版本号的文案";
        check(probe_parse_wanted(none, &w) == 0,
              "wanted-version regex no-match");
    }
}

static void manifest_tests(void)
{
    Manifest m;
    static const char *good =
        "{\n"
        "  \"format\": 1,\n"
        "  \"repo\": \"liubz102/popshot-reborn\",\n"
        "  \"releases\": [\n"
        "    {\"version\": \"0.2.8\", \"date\": \"2026-08-23\", "
        "\"url\": \"https://x/a.zip\", \"size\": 123, \"sha256\": "
        "\"0000000000000000000000000000000000000000000000000000000000000001\"},\n"
        "    {\"version\": \"0.2.7\", \"date\": \"2026-08-22\", "
        "\"url\": \"https://x/b.zip\", \"size\": 456, \"sha256\": "
        "\"0000000000000000000000000000000000000000000000000000000000000002\"}\n"
        "  ]\n"
        "}\n";

    check(manifest_parse(good, &m) && m.count == 2 &&
          wcscmp(m.entries[0].version_text, L"0.2.8") == 0 &&
          m.entries[0].size == 123 &&
          wcscmp(m.entries[1].url, L"https://x/b.zip") == 0,
          "manifest parse (2 releases, [0] newest)");

    check(manifest_parse("{\"releases\":[]}", &m) == 0,
          "manifest rejects empty releases");
    check(manifest_parse("{\"releases\":[{\"version\":\"0.2\"}]}",
                         &m) == 0,
          "manifest rejects entry missing url/sha256");
    check(manifest_parse("not json at all", &m) == 0,
          "manifest rejects garbage");
}

static void protected_tests(void)
{
    check(apply_is_protected(L"config/server.config"),
          "protected config/server.config");
    check(apply_is_protected(L"logs/online.log"), "protected logs/…");
    check(apply_is_protected(L"logs"), "protected logs itself");
    check(apply_is_protected(L"logs\\x\\y"), "protected logs\\… (backslash)");
    check(apply_is_protected(L"game_patched/UserConfig.ini"),
          "protected UserConfig.ini");
    check(apply_is_protected(L"server/data/accounts.json"),
          "protected accounts.json");
    check(apply_is_protected(L"game_patched/Dump/xxx.dmp"),
          "protected Dump/…");
    check(!apply_is_protected(L"game_patched/BigShot.exe"),
          "BigShot.exe NOT protected");
    check(!apply_is_protected(L"tools/x.py"), "tools/x.py NOT protected");
    check(!apply_is_protected(L"BUILD.ver"), "BUILD.ver NOT protected");
}

static void hash_tests(void)
{
    Sha256 s;
    wchar_t hex[65];
    static const char *abc = "abc";

    if (sha256_begin(&s)) {
        sha256_update(&s, abc, 3);
        sha256_finish(&s, hex);
        sha256_end(&s);
        check(wcscmp(hex,
              L"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
              == 0, "sha256(abc) vector");
    } else {
        check(0, "sha256_begin (CNG)");
    }
    if (sha256_begin(&s)) {
        sha256_finish(&s, hex);
        sha256_end(&s);
        check(wcscmp(hex,
              L"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
              == 0, "sha256(empty) vector");
    }
}

static void util_tests(void)
{
    wchar_t w[16];
    char b64[16];
    wchar_t url[256];

    /* “关闭” 的 gb2312 = B9 D8 B1 D5。 */
    check(gbk_to_wide("\xb9\xd8\xb1\xd5", 4, w, 16) == 2 &&
          w[0] == 0x5173 && w[1] == 0x95ED,
          "gbk_to_wide(关闭)");

    check(base64_encode((const unsigned char *)"abc", 3, b64, 16) == 4 &&
          strcmp(b64, "YWJj") == 0, "base64(abc)");
    check(base64_encode((const unsigned char *)"ab", 2, b64, 16) == 4 &&
          strcmp(b64, "YWI=") == 0, "base64(ab)");

    file_url_from_path(L"C:\\a b\\x.exe", url, 256);
    check(wcscmp(url, L"file:///C:/a%20b/x.exe") == 0,
          "file URL encode (space -> %20)");
}

/* ---- 代理列表解析（config\update.config） -------------------------------- */

static void proxylist_tests(void)
{
    ProxyList pl;
    static const wchar_t *text =
        L"\xFEFF" L"# 注释行\r\n"
        L"https://cdn.gh-proxy.com/\r\n"
        L"  https://gh-proxy.com  \r\n"
        L"; 分号也是注释\n"
        L"\n"
        L"ftp://nope.example\n"
        L"not a url\n"
        L"https://GH-PROXY.com\n"
        L"http://127.0.0.1:8123/fast\n"
        L"https://has space.com/x\n";
    int n = cfg_parse_proxy_list(text, &pl);

    check(n == 3 && pl.count == 3, "proxy list: 3 usable of 10 lines");
    check(pl.count >= 1 && wcscmp(pl.url[0], L"https://cdn.gh-proxy.com") == 0,
          "proxy list strips trailing slash");
    check(pl.count >= 2 && wcscmp(pl.url[1], L"https://gh-proxy.com") == 0,
          "proxy list trims spaces / CR");
    check(pl.count >= 3 && wcscmp(pl.url[2], L"http://127.0.0.1:8123/fast") == 0,
          "proxy list keeps a path prefix");
    check(pl.skipped == 4,
          "proxy list counts ignored lines (ftp / garbage / dup / inner space)");
    check(cfg_parse_proxy_list(L"", &pl) == 0 && pl.count == 0,
          "proxy list: empty text");
    check(cfg_parse_proxy_list(L"# c\n; d\n\n", &pl) == 0 && pl.skipped == 0,
          "proxy list: comments only");
    {
        static wchar_t many[PROXY_MAX * 40 + 64];
        int i;
        many[0] = 0;
        for (i = 0; i < PROXY_MAX + 1; i++) {
            wchar_t line[40];
            _snwprintf(line, 40, L"https://p%d.test\n", i);
            line[39] = 0;
            wcscat(many, line);
        }
        n = cfg_parse_proxy_list(many, &pl);
        check(n == PROXY_MAX && pl.skipped == 1, "proxy list caps at PROXY_MAX");
    }
    {
        wchar_t url[256];
        speed_compose_url(L"https://gh-proxy.com", L"https://github.com/a/b.zip",
                          url, 256);
        check(wcscmp(url, L"https://gh-proxy.com/https://github.com/a/b.zip") == 0,
              "compose proxy url = <proxy>/<original>");
        speed_compose_url(NULL, L"https://github.com/a/b.zip", url, 256);
        check(wcscmp(url, L"https://github.com/a/b.zip") == 0,
              "compose direct url unchanged");
    }
}

/* ---- 选源编排（speedtest.c），假测速函数：elapsed 固定 1000ms，
        bytes 就是 B/s；每个来源被测几次、第几次调用报取消都可控 ---------- */

typedef struct FakeNet {
    const wchar_t *base;                   /* 原地址 */
    unsigned long long direct_bytes;
    unsigned long long proxy_bytes[PROXY_MAX];
    LONG calls;
    LONG called[PROXY_MAX + 1];            /* [0] 直连，[i+1] 代理 i */
    LONG cancel_from;                      /* 第几次调用起报取消（0 = 不取消） */
} FakeNet;

static void fake_measure(void *user, const wchar_t *url, SpeedSample *out)
{
    FakeNet *f = (FakeNet *)user;
    LONG n = InterlockedIncrement(&f->calls);
    int i;

    memset(out, 0, sizeof(*out));
    out->elapsed_ms = 1000;
    if (wcscmp(url, f->base) == 0) {
        out->bytes = f->direct_bytes;
        InterlockedIncrement(&f->called[0]);
    } else {
        for (i = 0; i < PROXY_MAX; i++) {
            wchar_t prefix[64];
            _snwprintf(prefix, 64, L"https://p%d.test/", i);
            prefix[63] = 0;
            if (wcsncmp(url, prefix, wcslen(prefix)) == 0) {
                out->bytes = f->proxy_bytes[i];
                InterlockedIncrement(&f->called[i + 1]);
                break;
            }
        }
    }
    if (f->cancel_from && n >= f->cancel_from) out->cancelled = 1;
}

static void fake_proxies(ProxyList *pl, int n)
{
    int i;
    memset(pl, 0, sizeof(*pl));
    for (i = 0; i < n; i++) {
        _snwprintf(pl->url[i], PROXY_URL_CAP, L"https://p%d.test", i);
        pl->url[i][PROXY_URL_CAP - 1] = 0;
    }
    pl->count = n;
}

#define MIB (1ULL << 20)

static void speedtest_tests(void)
{
    static ProxyList pl;
    FakeNet f;
    SpeedPick pick;
    const wchar_t *base = L"https://github.com/x/y.zip";
    int rc;

    /* 直连够快：一个代理都不测。 */
    memset(&f, 0, sizeof(f)); f.base = base; f.direct_bytes = 2 * MIB;
    fake_proxies(&pl, 6);
    rc = speedtest_pick(base, &pl, fake_measure, NULL, &f, &pick);
    check(rc == 1 && pick.index == -1 && pick.qualified && pick.measured == 1 &&
          f.calls == 1, "speedtest: fast direct -> direct, proxies untouched");

    /* 直连慢，第一组里两个达标 -> 组内最快；第二组一个都没测。 */
    memset(&f, 0, sizeof(f)); f.base = base; f.direct_bytes = MIB / 5;
    f.proxy_bytes[0] = MIB / 2; f.proxy_bytes[1] = 3 * MIB;
    f.proxy_bytes[2] = 2 * MIB; f.proxy_bytes[3] = 0;
    f.proxy_bytes[4] = 9 * MIB; f.proxy_bytes[5] = 9 * MIB;
    fake_proxies(&pl, 6);
    rc = speedtest_pick(base, &pl, fake_measure, NULL, &f, &pick);
    check(rc == 1 && pick.index == 1 && pick.qualified && pick.bps == 3 * MIB,
          "speedtest: group 1 has two qualified -> fastest of the group");
    check(pick.measured == 5 && f.called[5] == 0 && f.called[6] == 0,
          "speedtest: group 2 never measured once group 1 qualified");
    check(f.called[1] == 1 && f.called[2] == 1 && f.called[3] == 1 &&
          f.called[4] == 1, "speedtest: each proxy of group 1 measured once");

    /* 第一组全不行、第二组达标。 */
    memset(&f, 0, sizeof(f)); f.base = base; f.direct_bytes = MIB / 5;
    f.proxy_bytes[4] = 5 * MIB;
    fake_proxies(&pl, 6);
    rc = speedtest_pick(base, &pl, fake_measure, NULL, &f, &pick);
    check(rc == 1 && pick.index == 4 && pick.qualified && pick.measured == 7,
          "speedtest: group 1 all slow -> group 2 picks");

    /* 全不达标：所有来源里相对最快（代理 4 在第二组）。 */
    memset(&f, 0, sizeof(f)); f.base = base; f.direct_bytes = MIB * 3 / 10;
    f.proxy_bytes[0] = MIB / 2; f.proxy_bytes[1] = MIB / 10;
    f.proxy_bytes[3] = MIB * 4 / 10; f.proxy_bytes[4] = MIB * 9 / 10;
    fake_proxies(&pl, 5);
    rc = speedtest_pick(base, &pl, fake_measure, NULL, &f, &pick);
    check(rc == 1 && pick.index == 4 && !pick.qualified && pick.measured == 6 &&
          pick.bps == MIB * 9 / 10,
          "speedtest: none qualified -> best-effort fastest overall (proxy)");

    /* 全不达标且直连最快。 */
    memset(&f, 0, sizeof(f)); f.base = base; f.direct_bytes = MIB * 8 / 10;
    f.proxy_bytes[0] = MIB / 2; f.proxy_bytes[1] = MIB / 10;
    fake_proxies(&pl, 2);
    rc = speedtest_pick(base, &pl, fake_measure, NULL, &f, &pick);
    check(rc == 1 && pick.index == -1 && !pick.qualified && pick.measured == 3,
          "speedtest: none qualified -> best-effort direct");

    /* 恰好 1 MiB/s 不算达标（「大于」）；多 1 字节才算。 */
    memset(&f, 0, sizeof(f)); f.base = base; f.direct_bytes = MIB;
    f.proxy_bytes[0] = MIB + 1;
    fake_proxies(&pl, 1);
    rc = speedtest_pick(base, &pl, fake_measure, NULL, &f, &pick);
    check(rc == 1 && pick.index == 0 && pick.qualified,
          "speedtest: threshold is strictly greater than 1 MiB/s");

    /* 代理列表为空：不测速直接直连。 */
    memset(&f, 0, sizeof(f)); f.base = base;
    fake_proxies(&pl, 0);
    rc = speedtest_pick(base, &pl, fake_measure, NULL, &f, &pick);
    check(rc == 1 && pick.index == -1 && pick.measured == 0 && f.calls == 0,
          "speedtest: empty proxy list -> direct without measuring");

    /* 取消：直连那一测就取消 / 第一组里取消（第二组不再起）。 */
    memset(&f, 0, sizeof(f)); f.base = base; f.cancel_from = 1;
    fake_proxies(&pl, 4);
    check(speedtest_pick(base, &pl, fake_measure, NULL, &f, &pick) == 0,
          "speedtest: cancel during direct probe -> 0");
    memset(&f, 0, sizeof(f)); f.base = base; f.cancel_from = 3;
    fake_proxies(&pl, 8);
    check(speedtest_pick(base, &pl, fake_measure, NULL, &f, &pick) == 0 &&
          f.calls == 5,
          "speedtest: cancel inside group 1 -> 0, group 2 never started");

    {
        SpeedSample s;
        memset(&s, 0, sizeof(s));
        s.bytes = 5000; s.elapsed_ms = 0;
        check(speed_bps(&s) == 5000000ULL, "speed_bps: 0 ms counts as 1 ms");
        s.elapsed_ms = 5000; s.bytes = 5 * MIB;
        check(speed_bps(&s) == MIB, "speed_bps: 5 MiB in 5 s = 1 MiB/s");
    }
}

/* ---- manifest 的代理兜底（proxy_fetch_fallback）：假取件函数 -------------
        直连 / 各代理成不成可控，记下每个代理被试几次和先后顺序。 */

typedef struct FakeFetch {
    const wchar_t *base;
    int direct_ok;
    int ok_index;                 /* 哪个代理成功；-1 = 全败 */
    int cancel_on_call;           /* 第几次调用报取消（0 = 不） */
    int calls;
    int seen[PROXY_MAX];
    int order[PROXY_MAX];         /* 代理被试的先后 */
    int order_n;
} FakeFetch;

static int fake_fetch(void *user, const wchar_t *url, wchar_t *err, size_t cap)
{
    FakeFetch *f = (FakeFetch *)user;
    int i;
    f->calls++;
    if (f->cancel_on_call && f->calls == f->cancel_on_call) {
        _snwprintf(err, cap, L"cancelled");
        return 0;
    }
    if (wcscmp(url, f->base) == 0) {
        if (f->direct_ok) return 1;
        _snwprintf(err, cap, L"direct down");
        return 0;
    }
    for (i = 0; i < PROXY_MAX; i++) {
        wchar_t prefix[64];
        _snwprintf(prefix, 64, L"https://p%d.test/", i);
        prefix[63] = 0;
        if (wcsncmp(url, prefix, wcslen(prefix)) == 0) {
            f->seen[i]++;
            if (f->order_n < PROXY_MAX) f->order[f->order_n++] = i;
            if (i == f->ok_index) return 1;
            _snwprintf(err, cap, L"proxy %d down", i);
            return 0;
        }
    }
    _snwprintf(err, cap, L"unknown url");
    return 0;
}

static int is_permutation(const int *order, int n)
{
    unsigned seen = 0;
    int i;
    for (i = 0; i < n; i++) {
        if (order[i] < 0 || order[i] >= n || (seen & (1u << order[i]))) return 0;
        seen |= 1u << order[i];
    }
    return 1;
}

static void fallback_tests(void)
{
    static ProxyList pl;
    FakeFetch f;
    wchar_t err[128];
    int picked, attempts, rc;
    const wchar_t *base = L"https://github.com/x/manifest.json";

    /* 洗牌：是排列；不同 seed 至少给出不同顺序；n=1 / n=0 不炸。 */
    {
        int order[PROXY_MAX], first[PROXY_MAX], i, s, all_same = 1, perm = 1;
        for (s = 1; s <= 8; s++) {
            for (i = 0; i < 6; i++) order[i] = i;
            proxy_shuffle(order, 6, (unsigned)s * 7919u);
            if (!is_permutation(order, 6)) perm = 0;
            if (s == 1) memcpy(first, order, sizeof(int) * 6);
            else if (memcmp(first, order, sizeof(int) * 6) != 0) all_same = 0;
        }
        check(perm, "proxy_shuffle keeps a permutation (8 seeds)");
        check(!all_same, "proxy_shuffle: different seeds give different orders");
        order[0] = 0;
        proxy_shuffle(order, 1, 3);
        check(order[0] == 0, "proxy_shuffle n=1");
        proxy_shuffle(order, 0, 3);
        check(1, "proxy_shuffle n=0 no crash");
    }

    /* 直连就成：只试一次。 */
    memset(&f, 0, sizeof(f)); f.base = base; f.direct_ok = 1; f.ok_index = -1;
    fake_proxies(&pl, 6);
    rc = proxy_fetch_fallback(L"t", base, &pl, 1, fake_fetch, NULL, &f,
                              &picked, &attempts, err, 128);
    check(rc == 1 && picked == -1 && attempts == 1 && f.calls == 1,
          "fallback: direct ok -> one attempt");

    /* 直连败、代理 3 成：每个代理最多试一次，成了就停。 */
    memset(&f, 0, sizeof(f)); f.base = base; f.ok_index = 3;
    fake_proxies(&pl, 6);
    rc = proxy_fetch_fallback(L"t", base, &pl, 42, fake_fetch, NULL, &f,
                              &picked, &attempts, err, 128);
    {
        int i, once = 1;
        for (i = 0; i < 6; i++) if (f.seen[i] > 1) once = 0;
        check(rc == 1 && picked == 3 && once && f.seen[3] == 1 &&
              attempts == f.calls && attempts <= 7,
              "fallback: direct down -> proxies until one works, each once");
        check(f.order_n >= 1 && f.order[f.order_n - 1] == 3,
              "fallback: stops right after the working proxy");
    }

    /* 全败：直连 + 每个代理各一次，err 是最后一次的。 */
    memset(&f, 0, sizeof(f)); f.base = base; f.ok_index = -1;
    fake_proxies(&pl, 5);
    rc = proxy_fetch_fallback(L"t", base, &pl, 7, fake_fetch, NULL, &f,
                              &picked, &attempts, err, 128);
    {
        int i, all_once = 1;
        wchar_t want[64];
        for (i = 0; i < 5; i++) if (f.seen[i] != 1) all_once = 0;
        _snwprintf(want, 64, L"proxy %d down", f.order_n ? f.order[f.order_n - 1] : -1);
        want[63] = 0;
        check(rc == 0 && attempts == 6 && all_once && is_permutation(f.order, 5),
              "fallback: all down -> 1 + n attempts, every proxy once");
        check(wcscmp(err, want) == 0, "fallback: err is the last attempt's reason");
    }

    /* 随机顺序跟着 seed 变（两个 seed、6 个代理全败，顺序不同）。 */
    {
        int o1[PROXY_MAX], o2[PROXY_MAX];
        memset(&f, 0, sizeof(f)); f.base = base; f.ok_index = -1;
        fake_proxies(&pl, 6);
        proxy_fetch_fallback(L"t", base, &pl, 1, fake_fetch, NULL, &f,
                             &picked, &attempts, err, 128);
        memcpy(o1, f.order, sizeof(int) * 6);
        memset(&f, 0, sizeof(f)); f.base = base; f.ok_index = -1;
        proxy_fetch_fallback(L"t", base, &pl, 2, fake_fetch, NULL, &f,
                             &picked, &attempts, err, 128);
        memcpy(o2, f.order, sizeof(int) * 6);
        check(memcmp(o1, o2, sizeof(int) * 6) != 0,
              "fallback: proxy order follows the seed");
    }

    /* 没有代理、直连败：只试一次就报失败。 */
    memset(&f, 0, sizeof(f)); f.base = base; f.ok_index = -1;
    fake_proxies(&pl, 0);
    rc = proxy_fetch_fallback(L"t", base, &pl, 1, fake_fetch, NULL, &f,
                              &picked, &attempts, err, 128);
    check(rc == 0 && attempts == 1 && wcscmp(err, L"direct down") == 0,
          "fallback: no proxies -> direct failure reported");

    /* 取消：第 2 次调用报取消 -> 立刻停，err = cancelled。 */
    memset(&f, 0, sizeof(f)); f.base = base; f.ok_index = -1; f.cancel_on_call = 2;
    fake_proxies(&pl, 6);
    rc = proxy_fetch_fallback(L"t", base, &pl, 1, fake_fetch, NULL, &f,
                              &picked, &attempts, err, 128);
    check(rc == 0 && f.calls == 2 && wcscmp(err, L"cancelled") == 0,
          "fallback: cancel stops the chain");
}

/* ---- --preview：界面视觉核对（假进度跑一圈，截图用） ------------------- */

static DWORD WINAPI preview_worker(LPVOID param)
{
    int i;
    (void)param;
    ui_announce_version(L"V9.9.9");
    ui_status(L"（预览模式）正在演示更新进度……");
    /* 口径同 main.c 的 PHASE_* 里程碑：「目前」=当前步骤，「全部」=全流程。
       下载段 2→60，停进程 60→64，覆盖段 64→99，收尾 100。 */
    ui_set_stage(UI_STAGE_DOWNLOAD);
    for (i = 0; i <= 100; i += 2) {
        ui_progress_current(i);
        ui_progress_total(2 + 58 * i / 100);
        ui_remaining(L"已下载 123.4 MiB  5.6 MiB/s  剩余约 42 秒");
        Sleep(30);
    }
    ui_remaining(NULL);
    ui_progress_current(0);
    ui_progress_total(62);
    Sleep(400);
    ui_progress_total(64);
    Sleep(400);
    ui_set_stage(UI_STAGE_APPLY);
    for (i = 0; i <= 100; i += 2) {
        ui_progress_current(i);
        ui_progress_total(64 + 35 * i / 100);
        Sleep(30);
    }
    ui_progress_total(100);
    ui_progress_current(100);
    ui_set_stage(UI_STAGE_FINAL);
    ui_status(L"（预览模式）更新完成，请关闭本窗口后运行 start.bat 重新启动游戏。");
    ui_swap_button();
    return 0;
}

int selftest_run(int preview)
{
    wchar_t self[MAX_PATH * 2], logpath[MAX_PATH * 2], *slash;
    HANDLE worker;

    /* GUI 子系统：借用父进程的控制台（构建脚本），不行就只写日志文件。 */
    if (AttachConsole(ATTACH_PARENT_PROCESS)) {
        FILE *con = freopen("CONOUT$", "w", stdout);
        (void)con;
    }
    module_path(self, MAX_PATH * 2);
    slash = wcsrchr(self, L'\\');
    if (slash) *slash = 0;
    _snwprintf(logpath, MAX_PATH * 2, L"%s\\selftest.log", self);
    logpath[MAX_PATH * 2 - 1] = 0;
    g_logf = _wfopen(logpath, L"w");

    if (preview) {
        wchar_t root[MAX_PATH * 2];
        say("preview mode: patch UI with fake progress\n");
        package_root(root, MAX_PATH * 2);
        ui_init(root, 0, 0);
        if (!ui_window_create_patch()) {
            say("FAIL  preview window\n");
            if (g_logf) fclose(g_logf);
            return 1;
        }
        worker = CreateThread(NULL, 0, preview_worker, NULL, 0, NULL);
        ui_pump_until_quit(worker);
        if (worker) { WaitForSingleObject(worker, 15000); CloseHandle(worker); }
        ui_shutdown();
        if (g_logf) fclose(g_logf);
        return 0;
    }

    say("=== updater selftest ===\n");
    cipher_tests();
    version_tests();
    frame_tests();
    manifest_tests();
    protected_tests();
    hash_tests();
    util_tests();
    proxylist_tests();
    speedtest_tests();
    fallback_tests();
    {
        int missing = ui_missing_resources();
        check(missing == 0, "all embedded UI resources present");
        (void)missing;
    }
    say("=== %d checks, %d failed ===\n", g_total, g_fail);
    if (g_logf) fclose(g_logf);
    FreeConsole();
    return g_fail;
}
