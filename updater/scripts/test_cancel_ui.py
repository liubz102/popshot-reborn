"""test_cancel_ui.py —— 真窗口实测「下载中点取消」整条链路。

动机（V0.2 会话 50）：用户真机反馈下载过程中点「取消」没有用。
e2e 跑 --noui 测不到按钮；本脚本起**真实 UI 窗口**：
    - 本地慢速 http（256 KB/s，8 MB ≈ 32 秒的点击窗口期）
    - 原生档（--ui-mode 3）：UIAutomation 找名「取消」的元素，
      Invoke / MSAA DoDefaultAction / 聚焦回车 三连回退
    - IE 档（--ui-mode 1，默认档）：同上（IE 的 DOM 走 MSAA 桥）
    - stall 场景：服务器只发 64KB 就挂住 —— 复刻慢/断流下
      WinHttpQueryDataAvailable 长时间等不到数据，验证 0.5s 取消节拍
    - 断言：日志出现「已取消更新」+ FINISH-OK，且没有 zip ready / FAIL

★ 需要交互桌面（窗口会短暂弹出）。跑法：
    runtime\\python\\python.exe updater\\scripts\\test_cancel_ui.py [1|3|both|stall|all]
"""

import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
EXE = os.path.join(ROOT, "updater", "bin", "BsPatcherChn.exe")
PORT = 8124

ZIP_SIZE = 8 << 20          # 8 MB
THROTTLE = 256 * 1024       # 256 KB/s → ~32s 下载期

PS_DRIVER = r'''
param([string]$ExePath, [string]$WorkDir, [string]$ManifestUrl, [int]$UiMode)
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$exe = Start-Process -FilePath $ExePath `
    -ArgumentList @('--manifest-url', $ManifestUrl, '--ui-mode', "$UiMode") `
    -WorkingDirectory $WorkDir -PassThru
$deadline = (Get-Date).AddSeconds(25)
while (-not $exe.MainWindowHandle -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 200; $exe.Refresh() }
if (-not $exe.MainWindowHandle) { Write-Output 'NO-WINDOW'; exit 1 }
Start-Sleep -Seconds $(if ($UiMode -eq 1) { 6 } else { 3 })
$root = [System.Windows.Automation.AutomationElement]::FromHandle($exe.MainWindowHandle)
$name = [string][char]0x53D6 + [char]0x6D88
$cond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::NameProperty, $name)
$btn = $null
if ($UiMode -ne 1) {
    $btn = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
    if (-not $btn) { Write-Output 'NO-BUTTON'; $exe.Kill(); exit 1 }
}
$clicked = $false
try {
    if ($UiMode -eq 1) {
        # IE 的 DOM 不投影进 UIA —— 按模板 CSS 直接坐标点击：
        # 取消按钮 55x18，右缘 x=571（right:1px），底缘 y≈508（BtmSec
        # padding 底 4px）。第一下激活窗口，第二下才是真点。
        $rect = $root.Current.BoundingRectangle
        $cx = [int]$rect.X + 543
        $cy = [int]$rect.Y + 499
        $sig0 = @'
[DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
[DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint data, UIntPtr extra);
'@
        $m0 = Add-Type -MemberDefinition $sig0 -Name M32ie -PassThru
        foreach ($round in 1..2) {
            [void]$m0::SetCursorPos($cx, $cy)
            Start-Sleep -Milliseconds 250
            [void]$m0::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
            [void]$m0::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
            Start-Sleep -Milliseconds 400
        }
        Write-Output 'CLICKED(mouse-fixed-coords)'
        $clicked = $true
    } else {
        $pt = $btn.GetClickablePoint()
        $sig = @'
[DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
[DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint data, UIntPtr extra);
'@
        $m = Add-Type -MemberDefinition $sig -Name M32c -PassThru
        [void]$m::SetCursorPos([int]$pt.X, [int]$pt.Y)
        Start-Sleep -Milliseconds 250
        [void]$m::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
        [void]$m::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
        Write-Output 'CLICKED(mouse)'
        $clicked = $true
    }
} catch { }
if (-not $clicked) {
    try {
        $pat2 = [System.Windows.Automation.AutomationElement]::LegacyIAccessiblePattern
        $btn.GetCurrentPattern($pat2).DoDefaultAction()
        Write-Output 'CLICKED(msaa-default-action)'
        $clicked = $true
    } catch { }
}
if (-not $clicked) {
    Add-Type -AssemblyName System.Windows.Forms
    try { $btn.SetFocus() } catch { }
    Start-Sleep -Milliseconds 300
    [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
    Write-Output 'CLICKED(sendkeys)'
}
Start-Sleep -Seconds 8
$exe.CloseMainWindow() | Out-Null
Start-Sleep -Seconds 2
if (-not $exe.HasExited) { $exe.Kill() }
Write-Output 'DONE'
'''

def die(msg):
    print("!! CANCEL-UI FAIL:", msg)
    sys.exit(1)


class SlowHandler(http.server.BaseHTTPRequestHandler):
    def _manifest(self, ver, path):
        body = json.dumps({
            "format": 1, "repo": "test/local",
            "releases": [{
                "version": ver, "date": "2026-08-23",
                "url": "http://127.0.0.1:%d/%s" % (PORT, path),
                "size": ZIP_SIZE, "sha256": SHA,
            }]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/manifest.json":
            self._manifest("9.9.9", "update.zip")
        elif self.path == "/manifest-stall.json":
            self._manifest("9.9.8", "stall.zip")
        elif self.path == "/update.zip":
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(ZIP_SIZE))
            self.end_headers()
            chunk = b"A" * 65536
            sent = 0
            while sent < ZIP_SIZE:
                self.wfile.write(chunk)
                sent += len(chunk)
                time.sleep(len(chunk) / float(THROTTLE))
        elif self.path == "/stall.zip":
            # 断流场景：报 8MB 只发 64KB 就挂住不关连接 —— 复刻「GitHub
            # 慢/断流」下 WinHttpQueryDataAvailable 长时间等不到数据的处境。
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(ZIP_SIZE))
            self.end_headers()
            self.wfile.write(b"A" * 65536)
            time.sleep(120)
        else:
            self.send_error(404)

    def log_message(self, *a):
        pass


SHA = hashlib.sha256(b"A" * (ZIP_SIZE // 65536 * 65536)).hexdigest()
# 8MB 正好整除 64KB，无需补尾


def build_sandbox(tmp):
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
    return sandbox


def run_case(mode, tmp, stall=False):
    tag = "stall" if stall else "mode"
    label = ("%s=%s" % (tag, mode)) + ("(stall)" if stall else "")
    sandbox = build_sandbox(tmp)
    # 每个场景用不同目标版本 → 不同缓存文件名；先清掉，别让上一轮的
    # 半截包把「下载中取消」变成「缓存复用」。
    cache = os.path.join(os.environ.get("TEMP", "."),
                         "popshot-update-%s.zip" %
                         ("9.9.8" if stall else "9.9.9"))
    if os.path.exists(cache):
        os.remove(cache)
    if os.path.exists(os.path.join(sandbox, "logs", "update.lock")):
        os.remove(os.path.join(sandbox, "logs", "update.lock"))
    log = os.path.join(sandbox, "logs", "updater.log")
    script = PS_DRIVER
    ps = os.path.join(tmp, "driver%d%s.ps1" % (mode, "-stall" if stall else ""))
    crlf = script.replace("\r\n", "\n").replace("\n", "\r\n")
    with open(ps, "wb") as f:
        f.write(b"\xef\xbb\xbf" + crlf.encode("utf-8"))
    url = "http://127.0.0.1:%d/manifest%s.json" % (
        PORT, "-stall" if stall else "")
    t0 = time.time()
    out = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", ps,
         "-ExePath", os.path.join(sandbox, "game_patched",
                                  "BsPatcherChn.exe"),
         "-WorkDir", os.path.join(sandbox, "game_patched"),
         "-ManifestUrl", url, "-UiMode", str(mode)],
        capture_output=True, timeout=120)
    stdout = out.stdout.decode("gbk", "replace")
    stderr = out.stderr.decode("gbk", "replace")
    print("%s driver: %s / %r (%.0fs)" %
          (label, stdout.strip().replace("\n", " | "), stderr,
           time.time() - t0))
    if "CLICKED(" not in stdout:
        die("%s 没能点到取消按钮（driver 输出见上）" % label)
    time.sleep(1)
    if not os.path.exists(log):
        die("%s 更新器没写日志" % label)
    text = open(log, encoding="utf-8", errors="replace").read()
    ok = ("已取消更新" in text) and ("FINISH-OK" in text)
    zip_ready = "zip ready" in text
    print("---- %s updater.log ----\n%s---------------------------" %
          (label, text))
    if "download http" not in text:
        die("%s 根本没进下载阶段" % label)
    if zip_ready:
        die("%s 取消没生效：下载一路跑完了（zip ready）" % label)
    if "FAIL" in text:
        die("%s 取消变成了失败：%s" %
            (label, text[text.index("FAIL"):][:120]))
    if not ok:
        die("%s 日志里没有取消成功的痕迹（断流中取消超时？）" % label)
    print("%s CANCEL OK（下载中止 + 已取消提示）" % label)


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    if not os.path.exists(EXE):
        die("找不到 %s" % EXE)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT),
                                            SlowHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    tmp = tempfile.mkdtemp(prefix="popshot-cancel-")
    print("sandbox:", tmp)
    try:
        if which in ("3", "both"):
            run_case(3, tmp)
        if which in ("1", "both"):
            run_case(1, tmp)
        if which in ("stall", "both", "all"):
            run_case(3, tmp, stall=True)
    finally:
        httpd.shutdown()
        time.sleep(0.5)
        shutil.rmtree(tmp, ignore_errors=True)
    print("==== CANCEL-UI PASS ====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
