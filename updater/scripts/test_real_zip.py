"""test_real_zip.py —— 拿 dist 里真实的发布 zip 完整跑一遍更新器。

动机（V0.2 会话 50 第三轮，§239）：e2e 的假 zip 只有文件条目，
掩住了「目录条目导致索引错位」的真机 bug —— 真实包 18 个目录条目，
第一发解压就报「zip 损坏/CRC 不过」。这个夹具用玩家实际下载到的
字节（dist\\PopShot-portable-win64_V0-2-*.zip）做端到端验证：

    - 本地 http 直供真实 zip（字节一致，带正确 sha256/size 的 manifest）
    - 沙箱 BUILD.ver = V0.0.1 → 目标 = 包内版本
    - --noui 跑完整链：下载→停进程→staging 解压→覆盖→BUILD.ver 提交

dist zip 不存在就跳过（打包产物不进 git）。跑法：
    runtime\\python\\python.exe updater\\scripts\\test_real_zip.py
"""

import glob
import hashlib
import http.server
import json
import os
import shutil
import sys
import tempfile
import zipfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
EXE = os.path.join(ROOT, "updater", "bin", "BsPatcherChn.exe")
PORT = 8125
CHUNK = 1 << 20


def die(msg):
    print("!! REAL-ZIP FAIL:", msg)
    sys.exit(1)


def pick_zip():
    cands = sorted(glob.glob(os.path.join(
        ROOT, "dist", "PopShot-portable-win64_V0-2-*.zip")), reverse=True)
    return cands[0] if cands else None


def read_version(zip_path):
    """从 zip 里的 BUILD.ver 抠版本号（只读中央目录 + 单条目，不整解）。"""
    import zipfile
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if name.endswith("BUILD.ver"):
                text = zf.read(name).decode("utf-8", "replace")
                import re
                m = re.search(r'"version"\s*:\s*"V?([\d.]+)', text)
                if m:
                    return m.group(1)
    die("zip 里找不到 BUILD.ver 的版本号")


def main():
    zip_path = pick_zip()
    if not os.path.exists(EXE):
        die("找不到 %s" % EXE)
    if not zip_path:
        print("（跳过：dist 里没有 PopShot-portable-win64_V0-2-*.zip）")
        return 0
    print("real zip:", zip_path)
    target_ver = read_version(zip_path)
    print("target version:", target_ver)

    print("hashing zip ...")
    sha = hashlib.sha256()
    with open(zip_path, "rb") as f:
        while True:
            b = f.read(8 << 20)
            if not b:
                break
            sha.update(b)
    digest = sha.hexdigest()
    size = os.path.getsize(zip_path)
    print("sha256:", digest, "size:", size)

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/manifest.json":
                body = json.dumps({
                    "format": 1, "repo": "test/local",
                    "releases": [{
                        "version": target_ver, "date": "2026-08-23",
                        "url": "http://127.0.0.1:%d/real.zip" % PORT,
                        "size": size, "sha256": digest,
                    }]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/real.zip":
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(size))
                self.end_headers()
                with open(zip_path, "rb") as f:
                    while True:
                        b = f.read(CHUNK)
                        if not b:
                            break
                        self.wfile.write(b)
            else:
                self.send_error(404)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    tmp = tempfile.mkdtemp(prefix="popshot-realzip-")
    sandbox = os.path.join(tmp, "sandbox")
    os.makedirs(os.path.join(sandbox, "game_patched"))
    os.makedirs(os.path.join(sandbox, "config"))
    with open(os.path.join(sandbox, "BUILD.ver"), "w",
              encoding="utf-8") as f:
        f.write('{"version": "V0.0.1"}\n')
    with open(os.path.join(sandbox, "config", "server.config"), "w",
              encoding="utf-8") as f:
        f.write("server_address = 127.0.0.1\n")
    shutil.copyfile(EXE, os.path.join(sandbox, "game_patched",
                                      "BsPatcherChn.exe"))
    print("sandbox:", sandbox)

    import subprocess
    env = dict(os.environ)
    env["POPSHOT_UPDATER_NOUI"] = "1"
    t0 = time.time()
    proc = subprocess.run(
        [os.path.join(sandbox, "game_patched", "BsPatcherChn.exe"),
         "--manifest-url", "http://127.0.0.1:%d/manifest.json" % PORT],
        env=env, capture_output=True, timeout=600)
    httpd.shutdown()
    print("updater exit=%d, %.0fs" % (proc.returncode, time.time() - t0))

    log = os.path.join(sandbox, "logs", "updater.log")
    text = open(log, encoding="utf-8", errors="replace").read() \
        if os.path.exists(log) else ""
    print("---- updater.log tail ----")
    print("\n".join(text.splitlines()[-8:]))
    print("--------------------------")

    if "FAIL" in text:
        die("更新器报失败：%s" % text[text.index("FAIL"):][:200])
    ver = open(os.path.join(sandbox, "BUILD.ver"), encoding="utf-8").read()
    if target_ver not in ver:
        die("BUILD.ver 没更新到 V%s：%r" % (target_ver, ver))
    if "applied moved=" not in text:
        die("没有 applied 记录")
    if not os.path.exists(os.path.join(
            sandbox, "game_patched", "BigShot.exe")):
        die("BigShot.exe 没落位 —— 整包覆盖不完整")
    cfg = open(os.path.join(sandbox, "config", "server.config"),
               encoding="utf-8").read()
    if "127.0.0.1" not in cfg:
        die("config/server.config 被覆盖了（保护清单失效）")

    # §240 回归：zip 里的中文名条目（GBK 无 UTF-8 标志位，如
    # 炮炮火枪手.url）必须以正确名字落盘，不许多不少不乱码。
    with zipfile.ZipFile(zip_path) as zf:
        url_entries = [zi for zi in zf.infolist()
                       if zi.filename.lower().endswith(".url")]
    if url_entries:
        zi = url_entries[0]
        if zi.flag_bits & 0x800:
            expect_leaf = zi.filename.rsplit("/", 1)[-1]
        else:
            raw = zi.filename.encode("cp437", "replace")
            expect_leaf = raw.decode("gbk", "replace").rsplit("/", 1)[-1]
        urls_on_disk = [n for n in
                        os.listdir(os.path.join(sandbox, "game_patched"))
                        if n.lower().endswith(".url")]
        print("url on disk:", [repr(n) for n in urls_on_disk],
              "expect:", repr(expect_leaf))
        if len(urls_on_disk) != len(url_entries):
            die("game_patched 的 .url 数不对：zip %d 个 / 落盘 %r"
                % (len(url_entries), urls_on_disk))
        if urls_on_disk[0] != expect_leaf:
            die("中文名条目落盘不对：%r != %r"
                % (urls_on_disk[0], expect_leaf))
        print("gbk-named entry ok:", urls_on_disk[0])

    shutil.rmtree(tmp, ignore_errors=True)
    print("==== REAL-ZIP PASS（真实发布包端到端：下载/解压/覆盖/提交全通）====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
