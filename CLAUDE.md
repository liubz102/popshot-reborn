# 炮炮火枪手复活工程 —— 指针

**开工前第一件事：读 `develop_history/V0.3_bot 功能开发/CLAUDE.md`**，
再按它指的顺序读 `.claude/PROGRESS.md` → `FINDINGS.md` → `DECISIONS.md`。
当前版本是 **V0.3 房间 bot 功能开发**；V0.1 / V0.2 已完工，在 `develop_history/` 里只读参考。

**这个文件只是路标，不是进度记录。任何新发现 / 新决定都写进上面那套文件，不要写这里。**

三条最容易致命的铁律（完整版在 V0.3 的 CLAUDE.md 里）：

1. `game_org/` 和 `原版安装包/` **只读**，改什么都去 `game_patched/`。
2. **不要让 GameGuard 跑起来** —— `game_patched/GameGuard.des` 必须保持改名状态。
3. `.bat` 用 **CRLF + UTF-8 无 BOM**，`.ps1` 用 **CRLF + UTF-8 有 BOM**，
   `.py`/`.sh`/`.json`/`.md` 用 **LF 无 BOM**。
   ★ 写含中文的文件一律用 Write/Edit 工具 —— 这台机器的 Git Bash heredoc 按 CP936 落盘。
