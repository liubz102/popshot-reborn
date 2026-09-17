# `test/` —— 测试都在这里

| 东西 | 在哪 |
|---|---|
| 测试入口 | `test/run_tests.py` |
| 测试代码 | `test/test_*.py`（外加不叫 `test_` 的共用工具 `testsupport.py`）|
| 测试**数据**（夹具）| `test/data/<用途>/` |
| 被测代码 | 隔壁的 `server/`（还有 `tools/`、`hook/`）|

```powershell
runtime\python\python.exe test\run_tests.py            # 3.14，默认并行
runtime-win7\python\python.exe test\run_tests.py       # Win7 的 3.8，两套都要绿
runtime\python\python.exe test\run_tests.py test_shop  # 只跑某几个模块 / 某个类
```

## 加一个测试文件：放进来就行，不用登记

全量的名单是 `run_tests.all_modules()` **现扫这个目录**得到的（`test_*.py`），
没有第二份手写清单。

以前有一张手写的 `MODULES` 表，加了模块要记得往里补一笔 —— 这一笔至少漏过三次
（`test_pkn` / `test_packdirs` / `test_questrecord` 进了仓库却从没被跑过）。
漏登记的症状**不是报错，是「全量绿」里静悄悄少几十条用例**，所以名单改成只有
一个来源：目录本身。

## 夹具：一个用途一个子目录

| 目录 | 谁在用 | 是什么 |
|---|---|---|
| `data/pkn/` | `test_pkn.py` 的 `GoldenVolumeTests` | 原版最小的加密卷 `Effects0011.pkn`（300 KB）+ 期望值 `Effects0011.expected.json` |

### 🔴 这里的东西看着「没人用」也别删

`Effects0011.pkn` 是**原版游戏里抢救出来的**一卷，`tools/pkn.py` 的读取器就钉在
它上面：卷头偏移、文件表、每个文件解出来的 sha256（那些 sha256 是从 `Pack_decrypt`
算的，不是读取器自己说的）。删了它，pkn 读取器就只剩「自己和自己对得上」的
round-trip 测试 —— 格式解错了也照样全绿。

2026-09-17 它被当成「无用的测试数据」删过一次（`ad43f621`），当时看不出有人用，
是因为守着它的 `test_pkn` 那阵子**根本不在全量名单里**。现在名单是现扫目录的，
而且金样不在时 `GoldenVolumeTests` 会**当场红**并指回这份说明 —— 不再是静悄悄跳过。

## 两条写测试时的硬规矩

并行（默认按 CPU 数开进程）靠的就是这两条，破了会**随机红**，而随机红比慢贵得多：

1. **别往仓库里写文件。** 要落盘就用 `tempfile.TemporaryDirectory()`。
2. **别写端口字面量。** 要监听就 `bind((host, 0))` 要临时端口。

另外 `run_tests._prepare()` 把 `shopcfg.DATA_DIR` 指到一个**空目录**上，
所以默认是「什么都没配」；要具体规则的用例自己临时改它
（`test_gameserver.MaterialDropTests`、`test_web_admin` 是样板）。

## 不进发布包

打包脚本按**白名单**从根目录挑东西（`tools/build-portable.ps1`、
`tools/build-server-package.ps1`），`test/` 不在名单里。往这里加夹具不用改打包脚本。
