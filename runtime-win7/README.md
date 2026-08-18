# Win7 兼容运行时

**老版 Windows 上用的那份 Python。** 和 `runtime/`（主力那份）是同一个角色，
只在 **Windows 10 以下**的机器上才会被启动脚本选中。

> ## ★ 只进客户端包
>
> 这份运行时的唯一目的，是让**个别还在用 Win7 的玩家能一键启动游戏**
> （客户端要在本机跑起假服务端和本地中继，都要 Python）。
>
> **服务端包故意不带它** —— 架服务端不考虑老系统（D133）。
> `tools/build-portable.ps1` 会拷它，`tools/build-server-package.ps1` **不会**。
> 有人拿 Win7 当服务端主机时，`serverctl.ps1` 会直接说「不支持，请换台机器」。

- 版本：CPython **3.8.10** —— **最后一个支持 Windows 7 的 Python**
  （官方从 3.9 起不再支持 Win7，从 3.13 起连 Win8.1 都不支持）
- 构建：`embed-**win32**`（32 位）
- 来源：<https://www.python.org/ftp/python/3.8.10/python-3.8.10-embed-win32.zip>
- 官方 MD5：`659adf421e90fba0f56a9631f79e70fb`
  （python.org 的 3.8.10 发布页公布值，下载后逐字节核对过）
- 文件 SHA-256：`760dc79bcb434ee80b1001a30bb6f798287881851bac6d8137867894d40ef1fc`
- 上游许可：`python/LICENSE.txt`（PSF License）

## 为什么是 32 位

Win7 那边的机器是 32 位还是 64 位我们不知道，而 **32 位的 Python 在两种机器上都能跑**。
服务端是纯 socket + 少量结构体打包，用不着 64 位的地址空间，选 win32 覆盖面最大。

## 为什么还塞了 41 个 `api-ms-win-*.dll` 和 `ucrtbase.dll`

python.org 的 embeddable 包**不含 Universal CRT**。Win10 的 UCRT 在系统里，
Win7 却要靠 **KB2999226** 这个更新才有 —— 而 Win7 早就停止更新了，
一台没打全补丁的机器上 `python.exe` 会直接弹「缺少 api-ms-win-crt-*.dll」的模态框，
**一键启动当场变成让玩家去找补丁**。

所以按微软文档允许的 **app-local 部署**方式，把 UCRT 的可重发行 DLL 和 `python.exe`
放在同一个目录里，玩家什么都不用装。这 41 个文件来自 Windows 10 SDK 的
`Redist\ucrt\DLLs\x86\`（微软为这个用途提供的那一份）。

`vcruntime140.dll` 上游包里本来就有，不用额外补。

## 验过什么

- `server/` 全部代码用 3.8.10 `compileall` 通过；
- **`server/run_tests.py` 的 797 项在 3.8.10 下全绿**（和 3.14 上一样）；
- `socket / select / struct / threading / ssl / sqlite3 / hashlib` 均可导入
  （`ssl` 带的是 OpenSSL 1.1.1k）。

## 注意：这份只给「本机假服务端 + 本地中继」用

客户端本体（`BigShot.exe` + `bshook.dll`）是 32 位原生程序，和 Python 无关，
换运行时影响不到它。启动脚本只拿这份 Python 去跑客户端包里的
`server\app.py`（本机假服务端）和 `server\relay.py`（连远程服务器时的本地中继）
—— 也正因为这两样绕不过去，Win7 玩家没有 Python 就连**单机**都玩不了。

## 怎么在 Win10 上验这条路

```bat
set POPSHOT_FORCE_LEGACY=1
start.bat
```

`tools\wincompat.ps1` 认这个环境变量：设了就**连运行时一起**按老系统挑，
不用真找一台 Win7 才能测。
