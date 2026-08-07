# 内置 Python 运行时

- 版本：CPython 3.14.3，Windows x64 embeddable distribution
- 来源：https://www.python.org/ftp/python/3.14.3/python-3.14.3-embeddable-amd64.zip
- 下载文件 SHA-256：`e69d3609130b1c06948620651d0f0ab2183ff978c2b174ddf3d3cae7ff226b89`
- 上游许可：见 `python\LICENSE.txt`

该运行时由 `tools\launch.ps1` 按相对路径调用。`python314._pth` 保持上游默认的
隔离配置，不读取目标电脑上安装的 Python 包。

