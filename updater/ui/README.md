# updater\ui —— 原版更新界面素材（工程内副本）

- **来源**：`game_patched\NGMResource.dll`（2007 年原版资源 DLL，包里一直带着）。
  `updater\scripts\extract_updater_ui.py` 负责提取（`orig\` = 原始字节，永不改动），
  `updater\scripts\make_patched_ui.py` 负责生成这里的可用副本。
- **嵌入**：`updater\updater.rc` 把下面的文件按**原资源名**（大写）以 RCDATA 嵌入 exe；
  `src\ui_window.c` 的资源表与之一一对应（selftest 盯着两边不许分叉）。
- **渲染**：运行时解到 `%TEMP%\popshot-ui-<pid>\`（`BINARY\` 子目录结构原样保留，
  模板里 `BINARY/xxx.gif` 的相对引用天然解析），IE 控件 `Navigate` file:///。
  **不走 res://** —— 2026-08-22 上一版真机在 res:// 上翻车，这是教训。

## 与原版（orig\）的差异

打补丁只做「纯 ASCII 字节级替换」，其余字节与 orig\ 逐字节一致
（详见 make_patched_ui.py 里的 PATCHES 表）：

| 模板 | 差异 | 原因 |
|---|---|---|
| 三张模板共有 | `TopSec` 注入 `onmousedown → window.external.DragMove()`（btnClose 上不触发） | 我们是无标题栏窗口，拖动得由页面回调宿主（原版由 NGMDll 的原生框架处理） |
| 三张模板共有 | 每个按钮 `<img>` 注入 `onclick → window.external.OnButton('<id>')` | 原版页面没有任何 onclick —— NGMDll 用 COM 事件下沉接点击；我们走 external 回调更直白 |
| TEMPLATE_PATCH.HTML | 公告 iframe `src=""` → `src="about:blank"` | 原版空 src 等 NGMDll 运行时 `ChangeContents()`；显式 about:blank 防个别 IE 版本把空 src 解析成页面自身造成自嵌套 |

公告区内容（原版放 patchimg 死链页面）：运行时由 `ui_window.c` 生成
`announce.html`（546×300，样式仿 global.css），`ChangeContents()` 切进去，
配原版 `IMG_DEFAULT.JPG`（恰好就是 546×300）。

## 文件清单

- `TEMPLATE_PATCH.HTML` 主更新窗（572×473，双进度条）
- `TEMPLATE_CONFIRMRUNADMIN.HTML` 管理员确认框（440×163）
- `TEMPLATE_MESSAGEL.HTML` 消息框（440×133，错误/提示用）
- `global.css`（原样；模板 `<link href="global.css">` 是小写，拷贝时改名）
- `BINARY\*` 42 张图片（原资源名原大写）
- `ICON_109.ico` 原版三尺寸图标（exe + 窗口图标；由 GROUP_ICON 109 + RT_ICON 组装）
