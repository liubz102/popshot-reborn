/* --------------------------------------------------------------------------
   selftest.c —— 回归自检（build.bat 的构建闸门）+ --preview 视觉预览。

   向量来源：
     * cipher   —— server\simple.py 的实测对（37 01 00 00 <-> 53 72 8f 7f）
     * 版本号   —— server\versioning.py 的语义（编码/比较/边界）
     * 0xFE 帧  —— gameserver 的 build_ctrl + w_i32 + w_wstr 镜像
     * sha256   —— 标准向量（"abc" / 空串）
     * 保护清单 —— tools\update_client.py 的 PROTECTED_PATHS 语义
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
    check(apply_is_protected(L"server.config"), "protected server.config");
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

/* ---- --preview：界面视觉核对（假进度跑一圈，截图用） ------------------- */

static DWORD WINAPI preview_worker(LPVOID param)
{
    int i;
    (void)param;
    ui_announce_version(L"V9.9.9");
    ui_status(L"（预览模式）正在演示更新进度……");
    for (i = 0; i <= 100; i += 2) {
        if (i < 70) ui_progress_total(i);
        ui_progress_current(i * 100 / 70 > 100 ? 100 : i * 100 / 70);
        ui_remaining(L"已下载 123.4 MiB  5.6 MiB/s  剩余约 42 秒");
        Sleep(80);
    }
    ui_remaining(NULL);
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
