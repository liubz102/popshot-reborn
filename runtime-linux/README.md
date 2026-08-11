# Linux 服务端运行时

**服务端包**（`dist\PopShot-server`）随包发给 Linux 的那份 Python。
和 `runtime/`（Windows 那份）是同一个角色，只是**没有解开**。

- 版本：CPython **3.14.7**（和 `runtime/` 的 Windows 3.14.3 是同一个大版本）
- 构建：`x86_64-unknown-linux-gnu` / `install_only_stripped`
  （基线指令集，云主机上兼容性最好；stripped 省一半体积）
- 来源：<https://github.com/astral-sh/python-build-standalone> 的 `20260807` 发布
- 文件 SHA-256：`a2478d654ed51d443bae21ec20ad927f116b4f5aae4094ab74918a6aa38f0575`
  （旁边的 `.sha256` 就是这个值，打包脚本每次都会核对）
- 上游许可：解开后见 `python/lib/python3.14/LICENSE.txt`

## 为什么留着 `.tar.gz` 不解开

包里的 `python/bin/python3` 是指向 `python3.14` 的**符号链接**，整棵树还带
可执行位（`tar -tvzf` 实测，FINDINGS §163.7）。**打包是在 Windows 上做的**，
在这边解开这两样都保不住。所以原样带走，由服务端包里的 `tools/serverctl.sh`
在第一次 `./start.sh` 时于 Linux 上自己解（决策 D088）。

## 怎么换一个版本

```bat
tools\build.bat -Server -Zip -LinuxRuntime download -PythonSeries 3.14
```

打包脚本会：只认 `<系列>.<纯数字>` 的**稳定版**（`3.15.0rc1` 这类预发布版被规则
挡掉）→ 下载 → 用上游 `SHA256SUMS` 校验 → **存回这个目录** → 写好 `.sha256` 旁证。
本目录里已经有校验得上的文件时**不会重新下载**。

换完记得把上面的版本号和哈希改掉，并把旧的那个 `.tar.gz` + `.sha256` 删掉
（否则两份都会留在 git 历史里，白占几十 MB）。

## 不想带 Linux 运行时

`tools\build.bat -Server -Zip -LinuxRuntime none`。
服务端包在 Linux 上会退回系统的 `python3`（要求 3.10+）。
