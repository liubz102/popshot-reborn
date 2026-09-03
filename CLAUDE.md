# 炮炮火枪手复活工程 —— 指针

**开工前第一件事：读 `develop_history/V0.3_合成与商店/CLAUDE.md`**，
再按它指的顺序读 `.claude/PROGRESS.md` → `FINDINGS.md` → `DECISIONS.md`。
当前版本是 **V0.3 后半 · 装备合成与商店**；
V0.1 / V0.2 / **V0.3 前半（房间 bot）** 已完工，在 `develop_history/` 里只读参考
（bot 代码仍在跑，那套 FINDINGS 仍然有效，引用时写全「V0.3bot §xxx」）。

**这个文件只是路标，不是进度记录。任何新发现 / 新决定都写进上面那套文件，不要写这里。**

## ★ 启动 / 停止游戏：**项目根目录下的三个脚本**，agent 自己跑就行

```text
start.bat        一键启动（服务端 + 客户端，精简日志，正常游玩用这个）
start-debug.bat  同上，但开调试日志（要看包 / 查 bug 用这个）
stop.bat         一键停止（客户端 + 服务端 + 中继，按端口的 OwningProcess 找，不误杀别的 python）
```

- 用 PowerShell 的 `Start-Process` 跑，别用 Git Bash（这两个 bat 里有 `pause`）。
- **改完 `server/` 的代码必须重启服务端才生效**（铁律 7）：`stop.bat` 再 `start.bat`。
- ⚠ 「启动游戏」和「用鼠标键盘操作游戏」是两件事：脚本负责前者；后者要
  computer-use 授权，而开始菜单里那个「炮炮火枪手」指向的是**只读原版**
  `game_org\...\bigshot.exe`，不是 `game_patched` —— 别拿它当入口。

三条最容易致命的铁律（完整版在当前版本的 CLAUDE.md 里）：

1. `game_org/` 和 `原版安装包/` **只读**，改什么都去 `game_patched/`。
2. **不要让 GameGuard 跑起来** —— `game_patched/GameGuard.des` 必须保持改名状态。
3. `.bat` 用 **CRLF + UTF-8 无 BOM**，`.ps1` 用 **CRLF + UTF-8 有 BOM**，
   `.py`/`.sh`/`.json`/`.md` 用 **LF 无 BOM**。
   ★ 写含中文的文件一律用 Write/Edit 工具 —— 这台机器的 Git Bash heredoc 按 CP936 落盘。
