/* ===========================================================================
   admin.js —— 管理页的前台（V0.3 合成与商店 M8 / D16）

   ## 一句话架构

   登录后 `GET /admin/api/catalog` 拿一次「物品表 + **字段描述表** + 图集元信息」，
   之后每个标签页 `GET /admin/api/config/{name}` 拿到 JSON 原文，
   **解析成一个数组当模型**，照着字段描述表生成输入框；保存时把数组原样
   `JSON.stringify` 回去 `POST` 到同一个接口 —— **服务端那道 `validate_*`
   一个字都没改**，前台只是换了个人机界面（D14 早就写明的退路）。

   ## ★ 「以后新增的字段也要同步显示在画面上」是怎么保证的

   两道：

   1. 字段表来自服务端的 `shopcfg.SCHEMA`，而它**贴着 validator 放**，
      两边对不上有测试报红 ⇒ 给 validator 加字段，画面上自动多一个框。
   2. **万一 SCHEMA 一时没跟上**：数据里有、字段表里没有的键，
      这里按值的类型退回通用输入框（数字/文本/开关），标一个「·未登记」。
      对象和数组画成只读的 JSON —— 看得见、改不了，但**绝不会被保存时吃掉**
      （模型就是原对象，没人动过的键原样留着）。

   ## 模型是「原对象本身」

   `CFG[which].entries` 就是要存回去的那个数组，输入框直接改它的元素。
   好处是**没登记的键天然幸存**，也不用维护一套「界面 → 数据」的搬运。
   ========================================================================= */
"use strict";

var $ = function (id) { return document.getElementById(id); };

function el(tag, cls, text) {
  var node = document.createElement(tag);
  if (cls) { node.className = cls; }
  if (text !== undefined && text !== null) { node.textContent = String(text); }
  return node;
}

/** 把 `**这样**` 画成粗体，其余原样。★ **只给页面说明**（`SCHEMA` 的 `help`）用。
 *
 * 那几段说明是写在服务端 `shopcfg.SCHEMA` 里的，作者一直按 Markdown 的习惯
 * 标重点；而这边是 `textContent` 画的 ⇒ 星号原样显示在画面上
 * （V0.3商店之前物品库 / 商店货架 / 合成配方三页都有，7 行）。
 *
 * ★ **不引入 Markdown**：只认这一种记号，而且**逐段 `textContent`** 塞进去
 * —— 说明文是运营看得到、但改不了的服务端常量，不过这条路本来也不该有
 * `innerHTML`。
 * ★ 星号**没配对**（个数是奇数）时整行按原样画，一个星号都不吃 ——
 *   宁可显示一个星号，也不要把半句话吞掉。
 */
function emphasised(node, text) {
  var line = String(text === undefined || text === null ? "" : text);
  var parts = line.split("**");
  if (parts.length % 2 === 0) {          // 星号个数是奇数 = 没配对
    node.textContent = line;
    return node;
  }
  parts.forEach(function (chunk, at) {
    if (!chunk) { return; }
    // 偶数段是正文、奇数段是重点。
    node.appendChild(at % 2 === 1 ? el("b", null, chunk)
                                  : document.createTextNode(chunk));
  });
  return node;
}

/** 嵌在页面里的那条 `.msg`。★ **只剩登录页**在用（用户 2026-09-09：
 *  「管理员账号」页原来把回执写在面板最底下，那儿离「删除」「添加」
 *  「改密码」三个钮都隔着好几栏，删完人根本看不见 —— 全改成右上角浮条了）。
 *  登录框那条留着：它就贴在「登录」按钮下面，本来就在视线里；而且没登录时
 *  标题栏是空的，浮条钉在标题栏下面反倒像飘着。 */
function say(node, text, ok) {
  node.textContent = text || "";
  node.className = "msg" + (text ? (ok ? " ok" : " bad") : "");
}

/* ------------------------------------------- 右上角浮条 toast（D39）
   配置页的回执（「已保存」「保存中……」「哪一条不合规」）原来是列表底下
   那条 `.msg` —— 列表下面现在什么都不放了，而且那个位置离「保存」按钮
   十万八千里，存完根本看不见。改成右上角浮条（用户 2026-09-06）：
   **不挡操作**（容器 `pointer-events: none`，只有浮条本身吃点击）。

   ★ **一次只留一条**，和原来那条 `.msg` 一个口径 —— 它本来就是被后一条
     盖掉的，攒一摞在角上没人看。
   ★ 成功的过几秒自己走，**出错的一直留着**等人点掉：报错里写着是哪一条
     字段不合规，几秒钟读不完，而且它常常还配着一张高亮的卡片。
   ⚠ `TOAST_MS` 是**停留时长**，不是铁律 10 说的那种时序阈值 —— 它不用来
     判断任何事情的先后，早一秒晚一秒都不改变程序的结论。 */
var TOAST_MS = 3600;

/** 浮条钉在**标题栏下面**、靠右（用户 2026-09-07：原来贴着窗口顶，会压住
 *  「退出登录」）。标题栏多高只有浏览器知道（字体、窗口窄了换行都会变），
 *  所以量一下再放；窗口一变宽窄再量一次（`wire()` 里挂了 resize）。 */
function placeToasts() {
  var bar = document.querySelector(".topbar");
  var top = bar ? Math.round(bar.getBoundingClientRect().bottom) + 8 : 64;
  $("toasts").style.top = top + "px";
}

function toast(text, ok) {
  var host = $("toasts");
  host.textContent = "";
  if (!text) { return null; }
  placeToasts();
  var node = el("div", "toast" + (ok ? "" : " bad"));
  node.appendChild(el("span", "x", "✕"));
  node.appendChild(document.createTextNode(text));
  host.appendChild(node);
  // 下一帧才加 `.in` —— 同一帧里加上，浏览器看不到「从右边滑进来」这个变化。
  requestAnimationFrame(function () { node.classList.add("in"); });
  var timer = 0;
  function close() {
    if (timer) { clearTimeout(timer); timer = 0; }
    // ★ 先断掉点击再淡出：万一 `transitionend` 没来（浏览器设了「减少动效」），
    //   留在角上的那个空壳也不会挡住底下的东西。
    node.style.pointerEvents = "none";
    node.classList.remove("in");
    node.addEventListener("transitionend", function () { node.remove(); },
                          {once: true});
  }
  node.onclick = close;
  if (ok) { timer = setTimeout(close, TOAST_MS); }
  return node;
}

async function api(path, payload) {
  var options = {credentials: "same-origin"};
  if (payload !== undefined) {
    options.method = "POST";
    options.headers = {"Content-Type": "application/json"};
    options.body = JSON.stringify(payload);
  }
  var response = await fetch(path, options);
  try {
    return await response.json();
  } catch (error) {
    return {ok: false, message: "服务端回了一段不是 JSON 的东西（HTTP "
                                + response.status + "）"};
  }
}

/* ======================================================================
   确认对话框（D38）—— 替掉 `window.confirm`

   `window.confirm` 的样式跟这一页完全不搭，而且装不下「冲突 / 未冲突」
   两张清单（用户 2026-09-06）。这里画一个自己的，返回 Promise。

   ⚠ 唯一换不掉的是 `beforeunload` 那一发：关标签页时只有浏览器自己那个框，
     页面无权画东西。
   ====================================================================== */

var DIALOG = null;            // {resolve, single, danger, picked} —— 正开着的那一个

/** 弹一个对话框。返回 `Promise<boolean>`（确定 = true）。
 *
 *  options = {title, lead, lists: [{label, bad, rows: [{label, reason}]}],
 *             ok, cancel, danger, checks: [{key, label, checked, warn}]}
 *  `cancel` 传 `null` = 只有一个「知道了」，一定 resolve(false)。
 *  `danger: true` = 确定钮是红的，**焦点给取消钮、Enter 不算确定**
 *    （回滚 / 删备份这种手一抖就没法挽回的事，别让回车替人做决定）。
 *  `checks` = 正文里画几个复选框；这时 resolve 的是 `false | [勾了的 key]`，
 *    一个都没勾时确定钮点不动。
 */
function ask(options) {
  closeDialog(false);         // 上一个还开着就当它被取消了
  hideTip();                  // 物品浮窗 z-index 60，不收起来会盖住对话框
  $("dialogTitle").textContent = options.title || "确认";

  var body = $("dialogBody");
  body.textContent = "";
  if (options.lead) { body.appendChild(el("p", "lead", options.lead)); }
  (options.lists || []).forEach(function (spec) {
    if (!spec.rows || !spec.rows.length) { return; }
    var box = el("div", "dlg-list" + (spec.bad ? " bad" : ""));
    box.appendChild(el("b", null, spec.label));
    var ul = document.createElement("ul");
    spec.rows.forEach(function (row) {
      var li = el("li", null, row.label);
      // 「为什么撞了」跟在名字后面 —— 只说「冲突」人猜不到是哪种冲突。
      if (row.reason) {
        li.appendChild(el("span", "why", "　—— " + row.reason));
      }
      ul.appendChild(li);
    });
    box.appendChild(ul);
    body.appendChild(box);
  });

  var buttons = $("dialogButtons");
  buttons.textContent = "";
  var no = null;
  if (options.cancel !== null) {
    no = el("button", "btn", options.cancel || "取消");
    no.onclick = function () { closeDialog(false); };
    buttons.appendChild(no);
  }
  var yes = el("button", "btn " + (options.danger ? "btn-danger" : "btn-primary"),
               options.ok || "确定");
  yes.onclick = function () { closeDialog(true); };
  buttons.appendChild(yes);

  var boxes = [];
  if (options.checks && options.checks.length) {
    var checks = el("div", "checks");
    options.checks.forEach(function (spec) {
      var label = document.createElement("label");
      var input = document.createElement("input");
      input.type = "checkbox";
      input.checked = !!spec.checked;
      input.setAttribute("data-key", spec.key);
      input.onchange = function () {
        yes.disabled = !boxes.some(function (box) { return box.checked; });
      };
      label.appendChild(input);
      var text = el("span", null, spec.label);
      if (spec.warn) { text.appendChild(el("span", "warn", spec.warn)); }
      label.appendChild(text);
      checks.appendChild(label);
      boxes.push(input);
    });
    body.appendChild(checks);
    yes.disabled = !boxes.some(function (box) { return box.checked; });
  }

  $("dialog").classList.remove("hidden");
  // 危险操作把焦点给「取消」：回车落在它身上什么都不会发生。
  if (options.danger && no) { no.focus(); } else { yes.focus(); }
  return new Promise(function (resolve) {
    DIALOG = {
      resolve: resolve, single: options.cancel === null,
      danger: !!options.danger,
      picked: boxes.length ? function () {
        return boxes.filter(function (box) { return box.checked; })
                    .map(function (box) { return box.getAttribute("data-key"); });
      } : null,
    };
  });
}

function closeDialog(answer) {
  if (!DIALOG) { return; }
  var open = DIALOG;
  var value = false;
  if (!open.single && answer) {
    value = open.picked ? open.picked() : true;
    // 带复选框的框一个都没勾 = 没有可确定的东西，当取消。
    if (open.picked && !value.length) { value = false; }
  }
  DIALOG = null;
  $("dialog").classList.add("hidden");
  open.resolve(value);
}

/* 每个接口都可能因为会话过期回这一句 —— 统一在这里踢回登录页。 */
function bounced(result) {
  if (result && !result.ok && result.message === "请先登录管理页") {
    showLoggedOut(result.message);
    return true;
  }
  return false;
}

/* ======================================================================
   全局状态
   ====================================================================== */

var CAT = null;             // /admin/api/catalog 的结果
var BYID = {};              // itemId -> 物品
var CFG = {};               // {shop: {format, entries, snapshot, warnings, hadNotes}}
var CURRENT = "items";      // 当前标签页（物品库是另外两份的地基，排最前）
var FILTER = {};            // 每个标签页各自的筛选条件

//: 走 `#cfgPanel` 那套壳的标签页（读 / 存 / 脏标记 / 三方合并全共用）。
//  ★ `rewards` 也在里面 —— 它画的是两张二维表格而不是卡片列表，但
//    「读一份 json、改、保存、撞车了合并」这一整条链一个字都不用改（D72）。
var CONFIGS = ["items", "shop", "recipe", "drops", "cards", "rewards"];

/** 一共几份运营配置 / 分别叫什么。
 *
 * ★★ **文案里的份数一律现数，别写死**（D72c）。2026-09-10 加第五份
 *   （金币 / 经验获取）时，页面上还有四处白纸黑字写着「四份」「四页」——
 *   刷新的回执、按钮提示、「设为运营」的说明。数字写死在文案里，加一份配置
 *   就得记得回来改，而漏改**不会报错**，只会一直骗人。
 */
function configCount() { return CONFIGS.length; }

function configTitles() {
  return CONFIGS.map(function (which) {
    return ((CAT && CAT.schema[which]) || {}).title || which;
  });
}

//: 「金币 / 经验获取」页顶上那两个切换按钮。两张表的行列完全一样，
//: 切的只是格子里填哪一对字段（用户 2026-09-10）。
var REWARD_VIEWS = [
  {id: "money", label: "金币", win: "win_money", lose: "lose_money"},
  {id: "exp", label: "经验", win: "win_exp", lose: "lose_exp"}
];

/* ======================================================================
   物品图标 —— 一张图集切出来
   ====================================================================== */

function iconStyle(node, cell, px) {
  var icons = CAT && CAT.icons;
  if (!icons || cell === null || cell === undefined) { return false; }
  var scale = px / icons.size;
  node.style.width = px + "px";
  node.style.height = px + "px";
  node.style.backgroundImage = "url(" + icons.url + ")";
  node.style.backgroundSize =
    (icons.width * scale) + "px " + (icons.height * scale) + "px";
  node.style.backgroundPosition =
    (-(cell % icons.cols) * px) + "px " + (-Math.floor(cell / icons.cols) * px) + "px";
  return true;
}

/** 一个物品格子。`listed` 为真时画橙色星芒底（照原版 ShopCabinetSlot.png）。 */
function slotNode(itemId, px, listed, clickable) {
  var box = el("div", "slot" + (listed ? " on" : "") + (clickable ? " pick" : ""));
  var pad = Math.round(px * 0.16);
  box.style.width = (px + pad) + "px";
  box.style.height = (px + pad) + "px";
  var item = BYID[itemId];
  var ic = el("div", "ic");
  if (item && iconStyle(ic, item.cell, px)) {
    box.appendChild(ic);
  } else {
    // 图集里没有它（原版素材本来就缺几个），或者图集根本没生成。
    box.appendChild(el("div", "noicon", "?"));
  }
  // ★ 原生 `title` 换成自绘浮窗（D26）—— 两个一起弹会叠在一块儿。
  tipFor(box, itemId);
  return box;
}

function itemLabel(itemId) {
  var item = BYID[itemId];
  if (!item) { return "#" + itemId + "（物品表里没有这个 id）"; }
  var bits = [itemName(itemId)];
  if (item.name_kr && item.name_kr !== itemName(itemId)) { bits.push(item.name_kr); }
  return bits.join(" / ") + "  #" + item.id;
}

/** 「类别 · 角色 · 系列级别」。
 *
 * `skipCharacter` 给**物品库**用：那一页的卡片上就摆着「角色限定」下拉框，
 * 这行小字再写一个原版角色，两个数对不上时人只会更糊涂（管理员刚把它
 * 改成「不限」，小字还写着「泰尔」）。
 */
function itemMeta(itemId, skipCharacter) {
  var item = BYID[itemId];
  if (!item) { return "★ 物品表里没有这个 id"; }
  // ★ 自定义武器（X3）写「武器（自定义）」—— 词是服务端发的（`custom_weapon_kind`）。
  var bits = [item.custom ? (CAT.custom_weapon_kind || "武器（自定义）")
                          : (CAT.kinds[item.kind] || item.kind)];
  if (item.character !== undefined && !skipCharacter) {
    bits.push(CAT.characters[String(item.character)] || ("角色" + item.character));
  }
  if (item.series) {
    bits.push((CAT.series[item.series] || item.series) + (item.tier || ""));
  }
  return bits.join(" · ");
}

/* ======================================================================
   物品浮窗 —— 鼠标停一秒，在指针旁边画一张小卡（用户 2026-09-05，D26）

   ## 内容和游戏里那张是同一份

   中间那段说明取的是 `catalog()` 里的 `desc`，服务端那边就是
   `shopcfg.item_desc_zh()` —— 和 `0x0501` 的 `ItemInfo+0x18`（游戏内提示框
   下半那块，FINDINGS §31③）同一个函数。所以管理页和游戏里看到的数字
   一定一致，不会出现「网页说伤害 4、游戏里说 5」。

   ## 谁会弹

   **事件委托到 `document`**，判据是元素身上有没有 `data-item` ——
   这样动态生成的格子（每次 `renderCurrent()` 都重建）不用各自挂监听器，
   新加的画面只要给格子写上 `data-item` 就自动有浮窗。

   挂 `data-item` 的地方：`slotNode()` 的图标格（商店卡片 / 配方产物 /
   材料格 / 掉落规则 / 玩家页都用它）、选择器的格子、玩家页那两行名字。
   ★ 有输入框的卡片**整张不挂** —— 正在改价格时头上冒一张卡挡着看不见。
   ====================================================================== */

/** 停多久才弹。★ **100 ms** —— 用户先要「1 秒左右」，实际用下来嫌慢，
    2026-09-05 当天就改成了 0.1 秒。
    ★ 这不是「等一等就好了」的时序阈值（铁律 10）—— 它是**人机交互的
    停留判据**：手从 A 划到 B 的路上会扫过一堆格子，一点都不等就会一路
    炸出十几张卡。这个数**该由手感定**，不是由某台机器上的观测定。 */
var TIP_DELAY_MS = 100;

/** 浮窗顶上那张大图的边长。
    ★ **44 × 4 = 176**：44 是商店卡片和「选择物品」弹窗里格子的边长
    （`renderShop` / `paintPicker`），也就是用户说的「原来的」那个大小；
    用户 2026-09-05 要求在浮窗里放大到 4 倍看清楚。
    ⚠ 图集里的原图只有 **64×64**（`itemicons.json` 的 `size`），176 是
    2.75 倍**放大**，所以配了 `image-rendering: pixelated` —— 双线性插值
    会把 2007 年的像素图糊成一团。 */
var TIP_ICON_PX = 176;

var TIP = {node: null, timer: 0, id: null, x: 0, y: 0};

function tipHost() {
  if (!TIP.node) {
    TIP.node = el("div", "itip hidden");
    document.body.appendChild(TIP.node);
  }
  return TIP.node;
}

/** 这件东西现在在商店里是什么状态。★ 取**当前标签页模型**：
    刚改完还没保存也照着新值说。 */
function shopEntryOf(itemId) {
  var found = null;
  ((CFG.shop && CFG.shop.entries) || []).forEach(function (entry) {
    if (entry && Number(entry.id) === Number(itemId)) { found = entry; }
  });
  return found;
}

function paintTip(itemId) {
  var box = tipHost();
  var item = BYID[itemId];
  box.textContent = "";
  var art = el("div", "t-icon");
  var big = el("div", "ic");
  if (item && iconStyle(big, item.cell, TIP_ICON_PX)) {
    art.appendChild(big);
  } else {
    // 图集里没有它（原版素材本来就缺几个），或者图集根本没生成。
    art.appendChild(el("div", "noicon", "?"));
  }
  box.appendChild(art);
  box.appendChild(el("div", "t-name", itemName(itemId)));
  box.appendChild(el("div", "t-meta", itemMeta(itemId) + "　#" + itemId));
  // ★ 韩文原名**只在浮窗里**画：物品库的卡片上那一行放不下，会把上架状态
  //   挤掉（用户 2026-09-06）。要看原名把鼠标停上去就有。
  if (item && item.name_kr && item.name_kr !== itemName(itemId)) {
    box.appendChild(el("div", "t-meta", item.name_kr));
  }
  if (item && item.desc) {
    // ★ `desc` 有两层分隔，别只切一层（切漏了浮窗里就直接冒出裸竖线）：
    //   `|`  = **分段**，客户端把它画成上下两个独立的文本框
    //          （`ItemInfo0Txt` 数值 / `ItemInfo1Txt` 特殊效果，V0.3商店 §31③）
    //          —— 这里一段一个 `.t-body`，虚线边框天然把两块隔开；
    //   `\n` = 段内**换行**，一行一个 div，别指望 white-space 去还原。
    item.desc.split("|").forEach(function (segment) {
      if (!segment) { return; }
      var body = el("div", "t-body");
      segment.split("\n").forEach(function (line) {
        body.appendChild(el("div", null, line));
      });
      box.appendChild(body);
    });
  }
  // ---- 等级 / 角色限定：取**物品库**那一份（D31），没登记就说清楚 ----
  //
  // ★ **穿不上身的东西不画这一行**（用户 2026-09-13）：判据是 `part_flag`
  //   （占不占装备槽），和 `shopcfg` 那张「哪些物品有等级 / 角色限定」用的是
  //   同一条 —— 客户端在**穿的那一刻**才读这两个字段，材料 / 消耗品 / 礼包
  //   读了也没人用，那一行永远是「不限角色　不限等级」。
  //   ⚠ 它写的是**这件东西自己**的限制；停在称号卡片上时很容易被当成
  //   「合出来那个称号的限制」误读，而卡片的提示框本来就已经很长了。
  //   称号自己有 `part_flag`（`0x2000`），停在称号上照画不误。
  // ★ 没登记的**照画** —— 那一行这时是句诊断（名字 / 等级都在退回原版数据）。
  var rule = itemRuleOf(itemId);
  if ((item && item.part_flag) || !rule.known) {
    var line = el("div", "t-meta");
    line.appendChild(document.createTextNode(
      (rule.character === null ? "不限角色"
        : (CAT.characters[String(rule.character)] || ("角色" + rule.character)))
      + "　" + (rule.level > 1 ? ("需 " + rule.level + " 级") : "不限等级")
      + (rule.known ? "" : "（物品库里没登记）")));
    box.appendChild(line);
  }

  // ---- 上架状态：商店 / 合成 / 未上架（互斥），带上代价 ----
  var where = listingOf(itemId);
  var shop = el("div", "t-shop");
  if (where === "shop") {
    var entry = shopEntryOf(itemId);
    shop.appendChild(el("span", "on", "★ 商店在卖"));
    shop.appendChild(el("span", null, ((entry && entry.price) || 0) + " 金币"));
  } else if (where === "recipe") {
    var recipe = recipeOf(itemId);
    shop.appendChild(el("span", "on", "★ 合成产出"));
    shop.appendChild(el("span", null, ((recipe && recipe.cost) || 0) + " 金币"));
    ((recipe && recipe.materials) || []).forEach(function (need) {
      var name = itemName(need.id);
      shop.appendChild(el("span", null, name + "×" + need.count));
    });
  } else {
    shop.appendChild(el("span", null, "未上架"));
  }
  box.appendChild(shop);

  // ---- 卖出单价（用户 2026-09-12）----
  // ★ **只在「装备卖出」页画**：别的页面上这一行没有意义，而且报价是跟着
  //   那一页一起取回来的（`SELL.quotes` 只有那个玩家手上那几件）。
  if (TAB === "sell" && SELL && SELL.quotes) {
    var quote = SELL.quotes[itemId];
    var sell = el("div", "t-shop");
    if (!quote) {
      sell.appendChild(el("span", null, "卖出：不在你的仓库里"));
    } else if (!quote.sellable) {
      sell.appendChild(el("span", null, "暂不可出售"));
    } else {
      sell.appendChild(el("span", "on", "◆ 卖出单价"));
      sell.appendChild(el("span", null, quote.unit + " 金币"));
      Object.keys(quote.materials || {}).forEach(function (mid) {
        sell.appendChild(el("span", null,
                            "退 " + itemName(Number(mid))
                            + "×" + quote.materials[mid]));
      });
    }
    box.appendChild(sell);
  }
}

/** 这件东西现在**上架在哪条配方**里；没有就 `null`。 */
function recipeOf(itemId) {
  var found = null;
  ((CFG.recipe && CFG.recipe.entries) || []).forEach(function (entry) {
    if (entry && entry.listed && Number(entry.result) === Number(itemId)) {
      found = entry;
    }
  });
  return found;
}

/** `itemId` → 物品库里那一条。
 *
 * ★ 这是一张**索引**，不是每次现扫一遍 —— 物品库有 800 多条，而
 *   `matches()` 会对每一条记录问一次「它的角色限定是什么」，现扫就是
 *   80 万次比较。索引在 `loadConfig("items")` 里重建（那是**唯一**会
 *   换掉 `entries` 数组的地方）；卡片上改名字 / 改等级是**就地改**同一个
 *   对象，索引里指着的就是它，不会过期。
 */
var ITEMS_BY_ID = {};

function reindexItems() {
  ITEMS_BY_ID = {};
  ((CFG.items && CFG.items.entries) || []).forEach(function (entry) {
    if (entry && entry.id !== undefined) { ITEMS_BY_ID[Number(entry.id)] = entry; }
  });
}

/** 这件东西的中文名。★ **唯一出处是物品库**（D31）；物品库里没登记就退回
    物品表里自动翻的那一份（和服务端 `shopcfg.name_of()` 同一条退路）。 */
function itemName(itemId) {
  var entry = ITEMS_BY_ID[Number(itemId)];
  if (entry && entry.name) { return entry.name; }
  var item = BYID[itemId];
  return (item && item.name) || ("#" + itemId);
}

/** 物品库里登记的等级 / 角色限定。没登记就退回原版数据（和服务端
    `shopcfg.rule_of()` 同一条退路），并且**说出来是退回来的**。 */
function itemRuleOf(itemId) {
  var found = ITEMS_BY_ID[Number(itemId)];
  if (found) {
    return {level: Number(found.level) || 1,
            character: (found.character === undefined
                        || found.character === null) ? null : found.character,
            known: true};
  }
  var item = BYID[itemId];
  return {level: 1,
          character: (item && item.character !== undefined)
            ? item.character : null,
          known: false};
}

function showTip(itemId) {
  paintTip(itemId);
  var box = tipHost();
  box.classList.remove("hidden");
  TIP.id = itemId;
  // 先画出来才量得到尺寸；量完再决定往左还是往右、往上还是往下。
  var rect = box.getBoundingClientRect();
  var pad = 14;
  var x = TIP.x + pad;
  var y = TIP.y + 18;
  if (x + rect.width > window.innerWidth - 6) {
    x = Math.max(6, TIP.x - pad - rect.width);
  }
  if (y + rect.height > window.innerHeight - 6) {
    y = Math.max(6, TIP.y - 12 - rect.height);
  }
  box.style.left = Math.round(x) + "px";
  box.style.top = Math.round(y) + "px";
}

function hideTip() {
  window.clearTimeout(TIP.timer);
  TIP.timer = 0;
  TIP.id = null;
  if (TIP.node) { TIP.node.classList.add("hidden"); }
}

function wireTips() {
  document.addEventListener("mousemove", function (event) {
    TIP.x = event.clientX;
    TIP.y = event.clientY;
  }, true);
  document.addEventListener("mouseover", function (event) {
    var host = event.target.closest ? event.target.closest("[data-item]") : null;
    if (!host) { return; }
    var itemId = Number(host.getAttribute("data-item"));
    if (TIP.id === itemId) { return; }        // 已经在显示这一件了
    window.clearTimeout(TIP.timer);
    TIP.timer = window.setTimeout(function () { showTip(itemId); },
                                  TIP_DELAY_MS);
  });
  document.addEventListener("mouseout", function (event) {
    var host = event.target.closest ? event.target.closest("[data-item]") : null;
    if (!host) { return; }
    var to = (event.relatedTarget && event.relatedTarget.closest)
      ? event.relatedTarget.closest("[data-item]") : null;
    // ★ 挪到**同一件东西**的另一个挂点不算离开 —— 一行里图标和名字是两个
    //   挂点（见 `ownNode`），不判这一条的话从图标滑到名字会闪一下、
    //   900 ms 重新数一遍。
    if (to && to.getAttribute("data-item") === host.getAttribute("data-item")) {
      return;
    }
    hideTip();
  });
  // 点了、滚了、按了键 —— 一律收起来。浮窗只在「停着看」的时候有意义。
  document.addEventListener("mousedown", hideTip, true);
  document.addEventListener("wheel", hideTip, true);
  document.addEventListener("scroll", hideTip, true);
  document.addEventListener("keydown", hideTip, true);
}

/** 给一个元素挂上「悬停显示这件东西」。 */
function tipFor(node, itemId) {
  node.setAttribute("data-item", String(itemId));
  return node;
}

/* ======================================================================
   字段 —— 照 SCHEMA 生成输入框
   ====================================================================== */

/** 数据里有、字段表里没有的键：按值的类型猜一个能用的控件。 */
function guessSpec(key, value) {
  var type = "text";
  if (typeof value === "boolean") { type = "bool"; }
  else if (typeof value === "number") { type = "int"; }
  else if (value !== null && typeof value === "object") { type = "json"; }
  return {key: key, label: key, type: type, optional: true, unknown: true};
}

//: 字段表（服务端 `shopcfg.SCHEMA`）里给金币字段标的**单位**。
//  ★ 它是「这一格装的是钱」的唯一判据（见 `fieldNode`）—— 服务端那边改了
//    这个字，前台这一格就该跟着改，两处写的是同一个词。
var MONEY_SUFFIX = "金币";

/** 一个字段的 DOM。**直接改 `entry[spec.key]`** —— 模型就是那个对象。 */
function fieldNode(spec, entry, onChange) {
  if (spec.type === "bool") {
    return toggleNode(spec, entry, onChange);
  }
  var wrap = el("div", "field" + (spec.unknown ? " unknown" : "")
                       + (spec.type === "text" ? " wide" : ""));
  // 哪个字段：掉落行要按它找到「模式 / 关卡 / 难度」那三个下拉（`dropRow`）。
  wrap.setAttribute("data-key", spec.key);
  var lab = el("span", "lab", spec.label || spec.key);
  if (spec.help) { lab.title = spec.help; }
  wrap.appendChild(lab);

  if (spec.readonly) {
    // ★ `choice` 的只读展示要写**选项的名字**，不是它的值 ——
    //   「角色限定：1」谁也看不出那是瑞娜。
    wrap.appendChild(el("span", "ro", spec.type === "choice"
                                      ? choiceLabel(spec, entry[spec.key])
                                      : format(entry[spec.key])));
    return wrap;
  }
  if (spec.type === "json") {
    // 画不出通用编辑器的东西（对象 / 数组）。看得见、改不了，**但会原样存回去**。
    var ro = el("code", "ro", JSON.stringify(entry[spec.key]));
    ro.title = "这个字段管理页还不认识，保存时会原样留着";
    wrap.appendChild(ro);
    return wrap;
  }
  if (spec.type === "choice") {
    wrap.appendChild(choiceNode(spec, entry, onChange));
    return wrap;
  }

  var input = document.createElement("input");
  var value = entry[spec.key];
  if (spec.type === "int") {
    input.type = "number";
    if (spec.min !== undefined) { input.min = spec.min; }
    if (spec.max !== undefined) { input.max = spec.max; }
    // ★ 装金币的格子要宽一档（用户 2026-09-13）：默认那 66px 实测**正好
    //   只装得下 5 位**，而商店价格 / 合成花费早就上六位了。
    //   ★★ 判据是**字段自己写的单位**（`SCHEMA` 里的 `suffix: "金币"`），
    //     不是一张写死的 key 清单 —— 以后再加一个标了「金币」的字段，
    //     它自动跟着变宽，没人需要回来改这一行。
    if (spec.suffix === MONEY_SUFFIX) { input.classList.add("num-money"); }
  } else {
    input.type = "text";
  }
  input.value = (value === undefined || value === null) ? "" : String(value);
  if (spec.optional) { input.placeholder = spec.empty_label || "不限"; }
  input.oninput = function () {
    var raw = input.value.trim();
    input.classList.remove("bad");
    if (raw === "") {
      // 可选字段留空 = 这个键干脆不写进 json（`stage` / `difficulty` / `note`）。
      if (spec.optional) { delete entry[spec.key]; }
      else { entry[spec.key] = spec.type === "int" ? "" : ""; input.classList.add("bad"); }
    } else if (spec.type === "int") {
      var num = Number(raw);
      if (!isFinite(num) || String(Math.trunc(num)) !== raw.replace(/^\+/, "")) {
        // ★ 不擅自改成 0：原样存回去，让服务端那句
        //   「price 不是整数：'abc'」来说话（它比前台的话准）。
        entry[spec.key] = raw;
        input.classList.add("bad");
      } else {
        entry[spec.key] = Math.trunc(num);
      }
    } else {
      entry[spec.key] = raw;
    }
    onChange();
  };
  if (spec.suffix) {
    var box = el("div", "with-suffix");
    box.appendChild(input);
    box.appendChild(el("span", "sfx", spec.suffix));
    wrap.appendChild(box);
  } else {
    wrap.appendChild(input);
  }
  return wrap;
}

/** 一个 `choice` 字段当前值的**显示名**。表里没有这一项就原样写值
 *  —— 和 `choiceNode` 里那句「表里没有这一项」是同一条思路：
 *  「看到的」和「存着的」必须对得上。 */
function choiceLabel(spec, value) {
  if (value === undefined || value === null) {
    return spec.empty_label || "不限";
  }
  var found = "";
  (spec.options || []).forEach(function (option) {
    if (String(option.value) === String(value)) { found = option.label; }
  });
  return found || String(value);
}

function choiceNode(spec, entry, onChange) {
  var select = document.createElement("select");
  var values = {};
  if (spec.optional) {
    select.appendChild(el("option", null, spec.empty_label || "不限"));
    select.lastChild.value = "";
  }
  (spec.options || []).forEach(function (option) {
    var node = el("option", null, option.label);
    node.value = String(option.value);
    values[node.value] = option.value;         // 数字选项别退化成字符串
    // ★ 逐项说明（`SCHEMA` 里那个 `help`）挂成 tooltip —— 统计指标那一格
    //   有 16 个选项，光看名字说不清「暴击」数的到底是什么。出处仍然只有
    //   服务端那张表，前端一个字都不抄。
    if (option.help) { node.title = option.help; }
    select.appendChild(node);
  });
  var current = entry[spec.key];
  var key = (current === undefined || current === null) ? "" : String(current);
  // ★ 数据里的值不在选项里（手改进来的关卡号、以后新加的模式……）：
  //   **补一个选项把它显示出来**。不补的话 `select.value = key` 会落空、
  //   下拉显示成空白，人以为这条没设过；而 entry 里那个值其实还在
  //   —— 「看到的」和「要存的」对不上是最难查的一种错。
  if (key !== "" && !(key in values)) {
    var extra = el("option", null, key + "（表里没有这一项）");
    extra.value = key;
    values[key] = current;
    select.appendChild(extra);
  }
  select.value = key;
  select.onchange = function () {
    if (select.value === "") {
      if (spec.optional) { delete entry[spec.key]; }
      else { entry[spec.key] = null; }
    } else {
      entry[spec.key] = values[select.value];
    }
    onChange();
  };
  return select;
}

function toggleNode(spec, entry, onChange) {
  var wrap = el("label", "toggle" + (entry[spec.key] ? " on" : "")
                         + (spec.unknown ? " unknown" : ""));
  // 哪个字段 —— 和 `fieldNode` 那一句一样。`restFields` 的调用方按它找格子
  // （`cardRow` 要锁 / 要联动的就是这几个）。
  wrap.setAttribute("data-key", spec.key);
  wrap.appendChild(el("span", "track"));
  wrap.appendChild(el("span", "lab", spec.label || spec.key));
  if (spec.help) { wrap.title = spec.help; }
  wrap.onclick = function (event) {
    event.preventDefault();
    entry[spec.key] = !entry[spec.key];
    wrap.classList.toggle("on", !!entry[spec.key]);
    onChange();
  };
  return wrap;
}

function format(value) {
  if (value === undefined || value === null) { return "—"; }
  if (typeof value === "object") { return JSON.stringify(value); }
  return String(value);
}

/** 把「除了这几个键之外」的字段按顺序摆出来 —— 新增的字段自动混在里面。 */
function restFields(which, entry, skip, onChange) {
  var nodes = [];
  var known = {};
  (CAT.schema[which].fields || []).forEach(function (spec) {
    known[spec.key] = true;
    if (skip.indexOf(spec.key) < 0) {
      nodes.push(fieldNode(spec, entry, onChange));
    }
  });
  // ★ 第二道保险：字段表还没登记、但数据里确实有的键。
  Object.keys(entry).forEach(function (key) {
    if (!known[key] && skip.indexOf(key) < 0) {
      nodes.push(fieldNode(guessSpec(key, entry[key]), entry, onChange));
    }
  });
  return nodes;
}

/* ======================================================================
   配置：读 / 存 / 脏标记
   ====================================================================== */

function listKey(which) { return CAT.schema[which].list_key; }

function snapshot(which) {
  return JSON.stringify(CFG[which] && CFG[which].entries);
}

function isDirty(which) {
  return CFG[which] && snapshot(which) !== CFG[which].snapshot;
}

/* ----------------------------------------- 「这一条改过了」（用户 2026-09-14）

   工具条上那句「● 有未保存的修改」只说了「有」，没说「在哪」——
   一页几百条，改完一格挪开眼睛就找不回来了。⇒ 每条记录进页面时按原样
   留一份**基线**，和基线不一样的那一行整行换个边框（`.edited`，样式在
   `admin.css` 的「改过了」那一节）。

   ★ 基线存在 `WeakMap` 里、键是**记录对象本身**，不是下标：删一条
     （`killButton`）、加一条（`addEntry`）都会让下标整体挪位，按下标存的话
     删掉第 3 条会让后面每一条都变成「改过了」。记录对象本身从进页面到
     保存为止是同一个（架构那一段：「模型是原对象本身」），拿它当键最稳。
   ★ 比的是 `stableJson`（键序无关）—— 改一格常常是**删掉一个键再加回来**
     （`fieldNode` 里可选字段留空那一支），直接 `JSON.stringify` 比的话
     键序一变就成了假阳性（同 `cardEditDirty` 那条理由）。
   ★ 基线在 `adoptConfig` 里和 `snapshot()` **同一时刻**记下：
     `fillItems` / `fillRewards` / `fillCards` 补出来的那些也算基线的一部分，
     不然一进页面几百条全亮着。 */
var ROW_BASE = new WeakMap();

/** 记下「磁盘上那份」。`baseCount` 是当时有几条 —— 删掉的那些在画面上
 *  没有行可标，只能靠它数出来。 */
function markBase(which) {
  var cfg = CFG[which];
  var count = 0;
  (cfg.entries || []).forEach(function (entry) {
    if (entry && typeof entry === "object") {
      ROW_BASE.set(entry, stableJson(entry));
      count += 1;
    }
  });
  cfg.baseCount = count;
}

/** 这一条和进页面时那份比，改过没有。**没有基线 = 新加的**，也算改过。 */
function entryEdited(entry) {
  if (!entry || typeof entry !== "object") { return false; }
  var was = ROW_BASE.get(entry);
  return was === undefined || was !== stableJson(entry);
}

/** 改 / 增 / 删 各几条。 */
function editCounts(which) {
  var cfg = CFG[which] || {};
  var out = {changed: 0, added: 0, removed: 0};
  var kept = 0;
  (cfg.entries || []).forEach(function (entry) {
    if (!entry || typeof entry !== "object") { return; }
    var was = ROW_BASE.get(entry);
    if (was === undefined) { out.added += 1; return; }
    kept += 1;
    if (was !== stableJson(entry)) { out.changed += 1; }
  });
  out.removed = Math.max(0, (cfg.baseCount || 0) - kept);
  return out;
}

/** 把 `.edited` 刷到画出来的那些行上。
 *
 * ★ **画完统一扫一遍**，不在 6 个渲染器里各写一句（同 `lockList()` 那条
 *   理由）：判据是「元素身上有没有 `data-index`」—— 那个属性本来就是
 *   `markBadCard` 定位用的，每个渲染器都已经挂了，以后新加的画面只要
 *   照挂就自动被收进来。
 * ★ 奖励表的 `data-index` 挂在**格子**上（一行里有好几条记录），
 *   一条记录的两格一起亮，正好是它在画面上占的那一块。 */
function paintEdited(host, which) {
  var entries = (CFG[which] || {}).entries || [];
  Array.prototype.forEach.call(
    host.querySelectorAll("[data-index]"), function (node) {
      var entry = entries[Number(node.getAttribute("data-index"))];
      node.classList.toggle("edited", entryEdited(entry));
    });
}

async function loadConfig(which) {
  var result = await api("/admin/api/config/" + which);
  if (bounced(result)) { return false; }
  if (!result.ok) { toast(result.message, false); return false; }
  return adoptConfig(which, result.text, result.warnings, result.path);
}

/** 把服务端给的那份**原文**变成页面模型。读一份配置和保存成功后走同一条路。
 *
 * ★★ `base` 记的就是这份原文 —— 保存时把它带回去做三方合并（D36）。
 *    必须是**服务端那一份**，不是 `fillItems()` 补过的模型：base 的含义是
 *    「磁盘上当时是什么样」，掺了前台补的东西就不是了，服务端会把补出来的
 *    几百条当成「我改的」，一改就撞车。
 */
function adoptConfig(which, text, warnings, path) {
  var raw;
  try {
    raw = JSON.parse(text);
  } catch (error) {
    toast("服务端上那份 " + which + ".json 不是合法 JSON："
          + error.message, false);
    return false;
  }
  var entries = raw[listKey(which)];
  CFG[which] = {
    format: raw.format,
    base: text,
    entries: Array.isArray(entries) ? entries : [],
    warnings: warnings || [],
    // 保存的回文里没有 path（它不会变），沿用上一次读到的。
    path: (path === undefined) ? ((CFG[which] || {}).path || "") : (path || ""),
    // 老文件里可能还留着 `_说明`。保存后它会消失（D16），先说一声。
    hadNotes: Object.keys(raw).some(function (k) { return k.charAt(0) === "_"; })
  };
  if (which === "items") {
    fillItems();
    reindexItems();
  }
  if (which === "rewards") { fillRewards(); }
  if (which === "cards") { fillCards(); }
  CFG[which].snapshot = snapshot(which);
  // ★ 和 `snapshot()` 同一时刻：两个说的是同一件事（「磁盘上当时是什么样」），
  //   一个管整页那句话、一个管每一行的边框，分开记迟早对不上。
  markBase(which);
  return true;
}

/** 物品库**永远是全物品表**：文件里缺的那几条现补一条默认的，
 *  穿不上身却带着 `level` / `character` 的把那两个键抹掉。
 *
 * ★ 为什么在前台补而不是让服务端补：服务端**从不回写配置**（D10）——
 *   文件是用户的，它只读、只校验。而页面必须能列出全部 808 件东西
 *   （用户 2026-09-06：「材料 / 礼包 / 消耗品筛出来是 0」），
 *   所以缺的这一段只能在这儿补齐，等用户按保存时一起落盘。
 * ★ 在 `snapshot()` **之前**做完 ⇒ 一进页面不会莫名其妙显示「有未保存的修改」。
 */
function fillItems() {
  var have = {};
  // ★ **文件里原来的顺序原样留着**，缺的补在末尾 —— 保存时才不会平白
  //   把整份 json 重排一遍（那种 diff 谁也看不出到底改了什么）。
  CFG.items.entries.forEach(function (entry) {
    if (entry && entry.id !== undefined) { have[Number(entry.id)] = entry; }
  });
  CAT.items.forEach(function (item) {
    if (have[item.id] === undefined) {
      CFG.items.entries.push({id: item.id, name: item.name});
      have[item.id] = null;               // 占个位，别重复补
    }
  });
  CFG.items.entries.forEach(function (entry) {
    if (!entry || entry.id === undefined) { return; }
    var item = BYID[Number(entry.id)];
    if (item && item.part_flag) {
      if (entry.level === undefined) { entry.level = 1; }
      // ★ 角色限定**只认原版**（D31a）：文件里写歪了就在这儿掰回来，
      //   下次保存一起落盘。服务端也不看文件里那份，两边一个口径。
      if (item.character === undefined || item.character === null) {
        delete entry.character;
      } else {
        entry.character = item.character;
      }
    } else {
      // 穿不上身的东西客户端根本不读这两个字段
      // （`shopcfg.has_level_and_character`），留在文件里只会让人以为它有用。
      delete entry.level;
      delete entry.character;
    }
  });
  // ★ 物品表里没有的 id **不偷偷删**：服务端会拒收整份文件并指出是哪一条，
  //   比它从页面上悄悄消失强。
}

/** 奖励表：文件里缺的档位按内置默认表补出来（D72）。
 *
 * ★ 和 `fillItems()` 一个套路、一个理由：这一页是两张**固定的**二维表格，
 *   文件里少一行不能让画面上少一格 —— 那一格就再也改不回来了。
 *   服务端那边同样按内置默认值兜底（`gameserver._reward_row`），两边一个口径。
 * ★ 必须在 `snapshot()` **之前**跑完，否则一进页面就显示「有未保存的修改」。
 */
function fillRewards() {
  var have = {};
  CFG.rewards.entries.forEach(function (entry) {
    have[rewardKey(entry)] = true;
  });
  (CAT.reward_defaults || []).forEach(function (row) {
    var key = rewardKey(row);
    if (have[key]) { return; }
    // ★ 拷一份：`CAT.reward_defaults` 是全页共用的那一份，直接塞进模型的话
    //   改一个格子会把「默认值」也改掉，「放弃修改」就退不回去了。
    var copy = {};
    Object.keys(row).forEach(function (name) { copy[name] = row[name]; });
    CFG.rewards.entries.push(copy);
    have[key] = true;
  });
}

/** 称号卡片页**永远列全 17 张卡片**：文件里缺的那几张现补一条默认规则。
 *
 * ★ 和 `fillItems` / `fillRewards` 一个道理（也一个理由）：服务端**从不回写
 *   配置**（D10），而这一页少一行就等于「那张卡片不存在」—— 运营根本不会
 *   想到去别处找它。缺的这一段在前台补齐，等用户按保存时一起落盘。
 * ★ 补出来的那一条**默认是关着的吗？** 不是 —— `card_defaults` 是设计表那份
 *   （`listed: true`），和「新开一台服务器拿到的」完全一致。这一页要是
 *   补出一堆关着的行，运营会以为默认就不发卡片。
 * ★ 在 `snapshot()` **之前**做完 ⇒ 一进页面不会莫名其妙显示「有未保存的修改」。
 */
function fillCards() {
  var have = {};
  CFG.cards.entries.forEach(function (entry) {
    if (entry && entry.card !== undefined) { have[entry.card] = true; }
  });
  (CAT.card_defaults || []).forEach(function (row) {
    if (have[row.card]) { return; }
    // ★ 拷一份：`CAT.card_defaults` 是全页共用的那一份（同 `fillRewards`）。
    //   ★★ 条件那一格是**数组**，得连里面的对象一起拷（`cardDraft` 同款）
    //   —— 浅拷的话，在弹窗里改一条条件会把 catalog 里那份也改掉，
    //   刷新之前谁也看不出来。
    CFG.cards.entries.push(cardDraft(row));
    have[row.card] = true;
  });
}

/** 奖励表里一条记录的**身份**。★ 和服务端 `cfgmerge.KEY_FIELDS.rewards`
 *  是同一套口径（模式 / 对战模式 / 道具战 / 队伍 / 关卡 / 难度）。 */
function rewardKey(entry) {
  if (!entry) { return ""; }
  if (entry.mode === "bonus") { return "bonus"; }
  if (entry.mode === "pvp") {
    return ["pvp", entry.pvp_mode, entry.item_mode ? 1 : 0,
            Number(entry.team) || 0].join("|");
  }
  return ["quest", entry.stage, entry.difficulty].join("|");
}

function collect(which) {
  var payload = {};
  payload.format = (CFG[which].format === undefined) ? 1 : CFG[which].format;
  payload[listKey(which)] = CFG[which].entries;
  return payload;
}

/** 商店和合成**互斥**（用户 2026-09-06）：一件东西不能两边都上架。
 *
 * 这一发只**找出**另一边那些跟着撞车的条目（`[{entry, id}]`），
 * 关不关它们的 `listed` 由 `saveConfig` 在**自己这一份存成功之后**决定
 * （D33 —— 顺序反过来的话，写不进文件时会落到「两边都没有」）。
 *
 * ★ 为什么在前台做而不是让服务端拒收：服务端一次只收一份文件，
 *   「先存哪一份」都会在中间那一刻出现「两边都上架」而被拒 —— 那是个
 *   解不开的死结。前台手里两份都有，能一次把话说完再一起存。
 *   （服务端那道 `validate_*` 一个字没动，还是最后的护栏。）
 */
//: 商店 ⇄ 合成互斥，另一边是谁。★ 和服务端 `cfgmerge.OTHER_LISTING` 同一张表。
var OTHER_LISTING = {shop: "recipe", recipe: "shop"};

function listingClash(which) {
  var other = OTHER_LISTING[which];
  if (!CFG[other]) { return []; }
  var mine = {};
  CFG[which].entries.forEach(function (entry) {
    if (entry && entry.listed) {
      mine[Number(which === "shop" ? entry.id : entry.result)] = true;
    }
  });
  var clash = [];
  CFG[other].entries.forEach(function (entry) {
    if (!entry || !entry.listed) { return; }
    var id = Number(other === "shop" ? entry.id : entry.result);
    if (mine[id]) { clash.push({entry: entry, id: id}); }
  });
  return clash;
}

/** 撞车了就问一句；管理员点「取消」返回 `false`。 */
function confirmClash(which, clash) {
  var rows = clash.map(function (row) { return {label: itemName(row.id)}; });
  return ask({
    title: CAT.schema[which].title + "：和另一边撞了",
    lead: (which === "shop")
      ? "以下物品已在合成中上架，若继续选择在商店上架，则自动下架合成。"
      : "以下物品已在商店上架，若继续选择在合成中上架，则自动下架商店。",
    lists: [{label: "会被自动下架的：", rows: rows}],
    ok: "继续", cancel: "取消"});
}

/** 真正发保存请求的那一发。`only` = 「只提交这几条」（单独提交未冲突物品）。
 *
 * ★ 保存**不是**整份覆盖了（D36）：带上 `base`（我打开这一页时服务端给我的
 *   那份），服务端拿它和磁盘上最新那份做三方合并。商店 / 合成还要带
 *   `cross_base` —— 「我在商店上架、对方同时在合成上架」这种撞车，
 *   我手上那份另一边的副本是旧的，**只有服务端看得见**。
 */
async function postConfig(which, only) {
  var payload = {text: JSON.stringify(collect(which), null, 2),
                 base: CFG[which].base};
  var other = OTHER_LISTING[which];
  if (other && CFG[other]) { payload.cross_base = CFG[other].base; }
  if (only) { payload.only = only; }
  var result = await api("/admin/api/config/" + which, payload);
  if (bounced(result)) { return false; }
  if (result.conflict) { return await onConflict(which, result); }
  if (!result.ok) {
    toast(CAT.schema[which].title + "：" + result.message, false);
    if (which === CURRENT) { markBadCard(result.message); }
    return false;
  }
  // ★ 换成**落盘后的那一份** —— 别人改的东西这一刻就出现在画面上
  //   （用户 2026-09-06：「保存后画面直接更新显示 merge 后的最新状态」）。
  adoptConfig(which, result.text, []);
  // ★★ 物品表也跟着重取（用户 2026-09-13：「实时根据设置值变化」）：
  //   称号卡片的 `desc` 是服务端从 `cards.json` / `recipe.json` **现算**的
  //   （`shopcfg.item_desc_zh`），`CAT` 里那份是**进页面那一刻**的快照。
  //   不重取的症状正是用户看到的那个：刚把「完美卡片」加进 [队内间谍] 的
  //   配方并保存，停在那张卡上的浮窗里却还是只有 [完美胜利者] 一个称号
  //   —— **而且不报错**，人只会以为没保存上。服务端保存时已经
  //   `invalidate_catalog()` 了，缺的就是前台这一问。
  //   ★ 不按「哪几页会影响说明」开白名单：判据要落在结果上，不落在
  //     「谁记得把新加的那一页也写进白名单」上（同 `lockList()`）。
  var fresh = await loadCatalog();
  if (which === CURRENT) { renderCurrent(); }
  // 重取失败时**不要**盖掉 `loadCatalog` 报的那句（同 `refreshConfigs`）
  // —— 存是存上了，但画面上那份说明是旧的，这件事得让人看见。
  if (fresh) { toast(result.message, true); }
  return true;
}

/** 服务端说撞车了：按用户给的那段话问一句，确定就单独提交未冲突的那些。 */
async function onConflict(which, result) {
  var conflicts = result.conflicts || [];
  var mergeable = result.mergeable || [];
  var bad = {label: "冲突的物品：", bad: true, rows: conflicts};
  if (!mergeable.length) {
    // 一件能单独提交的都没有 ⇒ 别问「是否单独提交」，那是个没有答案的问题。
    await ask({
      title: CAT.schema[which].title + "：保存冲突",
      lead: "刚才有另一个人修改了相同的物品，冲突物品需要刷新页面后重新修改。",
      lists: [bad], ok: "知道了", cancel: null});
    toast("没有保存 —— 另一个人刚改了同样的东西，"
          + "按「放弃修改」拿最新的那份再改一次。", false);
    return false;
  }
  var go = await ask({
    title: CAT.schema[which].title + "：保存冲突",
    lead: "刚才有另一个人修改了相同的物品，冲突物品需要刷新页面后重新修改，"
        + "未冲突的物品可以自动合并，是否单独提交未冲突物品？",
    lists: [bad, {label: "未冲突的物品：", rows: mergeable}],
    ok: "单独提交未冲突物品", cancel: "取消"});
  if (!go) {
    toast("已取消，什么都没保存。", false);
    return false;
  }
  // ★ **重新发一次，服务端会重新判一遍** —— 从我按下确定到这一发落地之间，
  //   第三个人可能刚好也改了同一条（用户 2026-09-06 明确要求）。
  var ok = await postConfig(which, mergeable.map(function (row) {
    return row.key;
  }));
  if (ok) {
    toast("已单独提交 " + mergeable.length + " 件未冲突的；冲突的 "
          + conflicts.length + " 件已换成服务端上最新的内容，请重新修改。", false);
  }
  return ok;
}

async function saveConfig(which, skipClashCheck) {
  var clash = [];
  if (!skipClashCheck && OTHER_LISTING[which]) {
    clash = listingClash(which);
    if (clash.length && !(await confirmClash(which, clash))) {
      toast("已取消，什么都没保存。", false);
      return false;
    }
  }
  toast("保存中……", true);
  clearBadCards();
  if (!(await postConfig(which, null))) { return false; }

  // ★ **这一份存成功了才去动另一边**（2026-09-06 改的顺序）：
  //   反过来先存另一边的话，只要自己这一份存失败（文件被编辑器占着就会），
  //   那件东西就变成「合成里已经下架、商店里没存上」—— 两头都没有，
  //   而管理员看到的只是一句报错，根本想不到东西已经从合成里没了。
  //   现在最坏的结果是「两边都还上着架」，下次按保存会再问一遍、还能救。
  if (clash.length) {
    var other = OTHER_LISTING[which];
    clash.forEach(function (row) { row.entry.listed = false; });
    if (!(await saveConfig(other, true))) {
      toast(CAT.schema[which].title + "已保存，但「"
            + CAT.schema[other].title + "」的自动下架没能存进去 —— "
            + "这件东西现在两边都上着架。切到那一页按一次保存。", false);
      return false;
    }
    toast("已保存，并把 " + clash.length + " 件东西从「"
          + CAT.schema[other].title + "」下架了。", true);
  }
  return true;
}

/** 「↻ 刷新」：把**几份配置全部**重新读一遍（用户 2026-09-06）。
 *
 * ★ 为什么不只读当前这一页：它们**不是各管各的**。
 *   ① 商店和合成**互斥**（D33）—— 别人刚在商店里上架了一件东西，服务端
 *      已经把它从合成里下掉了；只刷新合成那一页的话，商店那份还是旧的，
 *      画面上两边都写着「上架」，按一次保存就撞车。
 *   ② 中文名 / 等级 / 角色限定的唯一出处是**物品库**（D31），另外三页
 *      画的全是它 —— 物品库不跟着刷，名字就还是旧的。
 *
 * ★ 有没保存的改动先问一句（和 `refreshAccounts` 同一个口径）。
 *   `force` = 别问（数据备份页回滚完调它：回滚确认框里已经列过会丢的那几页）。
 */
async function refreshConfigs(force) {
  var dirty = force ? [] : CONFIGS.filter(isDirty);
  if (dirty.length) {
    var go = await ask({
      title: "还有没保存的改动",
      lead: "刷新会拿服务端上那份盖掉，以下几页的改动会丢：",
      lists: [{label: "有未保存改动的：", bad: true,
               rows: dirty.map(function (which) {
                 return {label: CAT.schema[which].title};
               })}],
      ok: "刷新"});
    if (!go) { return; }
  }
  toast("刷新中……", true);
  // ★ 物品表也重取一遍（V0.3商店）：称号卡片的**说明**是从 `cards.json` /
  //   `recipe.json` 现算的（`shopcfg.item_desc_zh`），改完条件不重取的话，
  //   浮窗里那句「获得条件：……」会一直停在登录那一刻的版本 ——
  //   而且**不会有任何报错**，人只会以为没保存上。
  var ok = await loadCatalog();
  for (var i = 0; i < CONFIGS.length; i += 1) {
    ok = (await loadConfig(CONFIGS[i])) && ok;
  }
  if (!CFG[CURRENT]) { return false; }
  renderCurrent();
  // ★ 失败时**不要**盖掉 `loadConfig` 报的那句 —— 「已刷新」压在「读不到
  //   drops.json」上面，用户看到的就是「点了刷新，然后什么都没变」。
  if (ok) {
    toast("已刷新：" + configCount() + " 份配置都换成服务端上最新的了。", true);
  }
  return ok;
}

/** 服务端的错误里带着下标（`recipes[3].materials[1].id：…`），定位过去。
 *
 * ★ 那张卡可能被筛选挡住了（搜索串、下拉、分类标签任何一样都可能）——
 *   不清掉筛选的话「报了个错但画面上什么都没高亮」，比不报还难查。
 *   没有换页栏了（D68）⇒ 清完筛选它一定在列表里，滚过去就行。
 */
function markBadCard(message) {
  var match = /\[(\d+)\]/.exec(message || "");
  if (!match) { return; }
  var index = Number(match[1]);
  var list = $("cfgList");
  var selector = '[data-index="' + index + '"]';
  var card = list.querySelector(selector);
  if (!card) {
    FILTER[CURRENT] = emptyFilter();     // 被筛掉了，先把筛选清掉
    renderToolbar(CURRENT);
    repaintList();
    card = list.querySelector(selector);
  }
  if (!card) { return; }
  card.classList.add("bad", "flash");
  revealIfHidden(card);
}

function clearBadCards() {
  Array.prototype.forEach.call(
    $("cfgList").querySelectorAll(".bad"),
    function (node) { node.classList.remove("bad", "flash"); });
}

function touched() {
  var dirty = isDirty(CURRENT);
  var node = $("cfgDirty");
  node.textContent = dirty ? "● 有未保存的修改" : "已和服务端一致";
  node.className = "dirty" + (dirty ? "" : " clean");
  $("cfgSave").disabled = !dirty;
  $("cfgReset").disabled = !dirty;
  // ★ 删掉的那几条**只能靠一句话说**：行都没了，边框标在哪儿都不对
  //   （改过的和新增的自己会亮，不用在这儿再数一遍）。
  // ★★ 写在**面板标题**那一格，不写在工具条上那句话后面：工具条是一行 flex，
  //   掉落页那一条筛选开着时在 1320 里只剩 70 px 余量（CSS 里量过的那一处），
  //   再往「有未保存的修改」后面接七个字，整条当场折成两行。
  var gone = dirty ? editCounts(CURRENT).removed : 0;
  $("cfgCount").textContent =
    CFG[CURRENT].entries.length + " " + CAT.schema[CURRENT].unit
    + (gone ? "（删掉 " + gone + " 条还没保存）" : "");
  paintEdited($("cfgList"), CURRENT);
}

/* ======================================================================
   分类标签 —— 两行按钮，四处共用（用户 2026-09-09，D68）

   照**游戏仓库界面**那棵标签树（§41，服务端 `shop.WAREHOUSE_TABS` 随物品表
   一起发）：上一行 7 个大分类 + 前面一个「全部」，下一行当前大分类的小分类。
   2026-09-07 先给「修改仓库」弹窗做的（D47）；用户 2026-09-09 说这套比下拉框
   顺手，物品库 / 商店货架 / 合成配方 / 「选择物品」弹窗全换成它 ——
   原来的「类别」下拉（`kind`，shopdata 的抽表口径）和「只看上架」开关一起删了。
   材料掉落页**不换**：它按 模式 / 关卡 / 难度 筛，不按物品分类。

   每件物品落在哪一格由服务端算好放在 `item.wh` 里（客户端自己的两个分类
   函数翻过来的），这儿只做匹配：精确相等，或大分类按高半字收。
   ★ 标签上的件数是**过了其它筛选之后**的数（搜索串 / 角色 / 上架状态）——
     标签是筛选的一维，不是独立的目录；「泰尔 · 未上架」一选，一眼看得出
     每个分类里还剩几件，点过去不会是空的。
   ====================================================================== */

function whTabs() {
  return (CAT && CAT.warehouse) || [];
}

/** 这件东西在游戏仓库里的标签 id；物品表里没有的当 `0`（哪个标签都不收）。 */
function whCategory(itemId) {
  var item = BYID[itemId];
  return item && item.wh !== undefined ? item.wh : 0;
}

/** 客户端 `0x412852~0x412868` 那条规则：`-1` 全收；`0` 两边都不通配；
 *  精确相等；父标签（低半字为 0）按高半字收。 */
function whMatches(requested, cat) {
  if (requested === -1) { return true; }
  if (!cat || !requested) { return false; }
  if (requested === cat) { return true; }
  if ((requested & 0xFFFF) !== 0) { return false; }
  return (requested >>> 16) === (cat >>> 16);
}

/** 一份「什么都没选」的标签状态：大分类「全部」（-1），没选小分类。 */
function anyTab() {
  return {big: -1, sub: null};
}

/** 现在选中的是哪一格：小分类优先，没选小分类就是大分类（或「全部」= -1）。 */
function tabRequested(sel) {
  return sel.sub !== null ? sel.sub : sel.big;
}

/** 画两行标签。`sel` = {big, sub}（点了**就地改**它）；`count(id)` = 这一格里
 *  有几件；点了哪格叫 `onPick()`（那时 `sel` 已经改好了）。
 *  件数为 0 的格子照画、只是不带角标 —— 「原来一件宠物都没登记」也是信息。
 *  「人物 → 英雄」是 `0`，客户端遇到 0 直接返回空 —— 永远是空格子，不画。 */
function paintCatTabs(bigHost, subHost, sel, count, onPick) {
  function tabButton(id, label, on, big, sub) {
    var button = el("button", "cat" + (on ? " on" : ""), label);
    var n = count(id);
    if (n) { button.appendChild(el("span", "n", String(n))); }
    button.onclick = function () {
      sel.big = big;
      sel.sub = sub;
      onPick();
    };
    return button;
  }
  bigHost.textContent = "";
  bigHost.appendChild(tabButton(-1, "全部", sel.big === -1, -1, null));
  whTabs().forEach(function (tab) {
    bigHost.appendChild(tabButton(tab.id, tab.label, sel.big === tab.id,
                                  tab.id, null));
  });

  subHost.textContent = "";
  var big = whTabs().filter(function (tab) { return tab.id === sel.big; })[0];
  var children = big ? (big.children || []).filter(function (child) {
    return child.id !== 0;
  }) : [];
  subHost.classList.toggle("hidden", !children.length);
  if (!children.length) { return; }
  subHost.appendChild(tabButton(big.id, "全部" + big.label, sel.sub === null,
                                big.id, null));
  children.forEach(function (child) {
    subHost.appendChild(tabButton(child.id, child.label, sel.sub === child.id,
                                  big.id, child.id));
  });
}

/* ======================================================================
   渲染
   ====================================================================== */

function renderCurrent() {
  var which = CURRENT;
  var schema = CAT.schema[which];
  $("cfgTitle").textContent = schema.title;

  var help = $("cfgHelp");
  help.textContent = "";
  // ★ 只读身份先说一句（D74）：下面那几条说明里满是「随便改」「改完保存
  //   即刻生效」，不先讲清楚「你这个身份改不了」，人会以为是页面坏了。
  if (isReadOnly()) {
    help.appendChild(el("li", "ro-note",
                        "★ 你现在是用游戏账号登录的「只读」身份 —— "
                        + "下面这几页随便看，但一个字都改不了"
                        + "（要能改请联系系统管理员开权限）。"));
  }
  (schema.help || []).forEach(function (line) {
    help.appendChild(emphasised(el("li"), line));
  });

  var notes = [];
  (CFG[which].warnings || []).forEach(function (warning) {
    notes.push("⚠ 服务端现在用的不是这份文件：" + warning);
  });
  if (CFG[which].hadNotes) {
    notes.push("这份文件里还留着旧的 _说明 —— 那几句话已经画在上面了，"
               + "下次保存会把它从文件里去掉。");
  }
  // ★ 只在**真有话说**的时候弹 —— 这一发每次重画（换标签、存完盘）都会走到，
  //   无条件调 `toast("")` 会把刚弹出来的「已保存」清掉。
  if (notes.length) { toast(notes.join("\n"), false); }

  // ★ 物品库没有「添加」：条目由 `shop_items.json` 定死，加不出新物品。
  //   「金币 / 经验获取」也没有：档位是 8 + 21 + 1 三十格固定的表格，
  //   加一条出来在画面上根本没有位置放（D72）。
  //   ★ 「称号卡片掉落」同理（V0.3商店）：卡片是原版那 17 张，加不出新的，
  //     而且**一张卡片只能有一条规则** —— 有「添加」就等于请人去造重复主键。
  $("cfgAdd").classList.toggle(
    "hidden",
    which === "items" || which === "rewards" || which === "cards");

  renderToolbar(which);
  repaintList();
}

/* ------------------------------------------------------------ 工具条
   一行：搜索框 + 两个下拉（角色 / 上架状态，D68）+「↻ 刷新」+「筛出 x / y」；
   分类走下面那两行标签（`paintCfgTabs`）。材料掉落页例外：它没有分类标签，
   两个下拉换成 模式 / 关卡 / 难度（D48）。 */
function renderToolbar(which) {
  var bar = $("cfgToolbar");
  bar.textContent = "";
  if (!FILTER[which]) { FILTER[which] = emptyFilter(); }
  var filter = FILTER[which];

  // ★ 「金币 / 经验获取」页没有筛选条（D72）：档位固定 30 格，一屏就画完了，
  //   搜什么、筛什么都没有意义。工具条上只留下面那个「↻ 刷新」。
  //   金币 / 经验的切换在下一行（`paintCfgTabs`），不挤在这儿。
  // ★ 称号卡片页 2026-09-13 第四轮也把筛选条清空了（用户点的题）：
  //   17 行一屏就看完了，一排下拉换不来任何东西。留下的只有「全部 /
  //   成就卡片 / 武器卡片」那个分组切换和一颗「查看本人达成进度」。
  if (which !== "rewards" && which !== "cards") {
    var search = document.createElement("input");
    search.type = "text";
    search.placeholder = "搜 中文名 / 韩文名 / id";
    search.value = filter.q;
    search.oninput = function () {
      filter.q = search.value.trim();
      repaintList();
    };
    bar.appendChild(search);
  }

  // ★ 称号卡片页也不画「角色」下拉：17 张卡片一张都不限角色，
  //   那个下拉只会一选就空。
  if (which !== "drops" && which !== "rewards" && which !== "cards") {
    // ★ 角色 / 上架状态两个下拉（用户 2026-09-09，D68）：和「选择物品」弹窗、
    //   「修改仓库」弹窗**同一份选项、同一条判据**（`characterOptions` /
    //   `LISTING_FILTER_OPTIONS` / `dropdownsMatch`）。「类别」下拉（shopdata 的
    //   抽表口径）让位给游戏仓库那棵树的分类标签，商店 / 合成的「只看上架」开关
    //   也一起删了。
    bar.appendChild(selectFilter(filter, "character", "全部角色",
                                 characterOptions()));
  }
  if (which === "items") {
    // ★ 上架状态**只有物品库有**（用户 2026-09-09 第三轮，D68b）：商店货架 /
    //   合成配方两页本来就是「上架了什么」的清单，在那儿按上架状态筛没有意义
    //   —— 一选别的档位就是空的。
    bar.appendChild(selectFilter(filter, "listing", "全部上架状态",
                                 LISTING_FILTER_OPTIONS));
  }
  if (which === "drops") {
    var modeSelect = selectFilter(filter, "mode", "全部模式",
      [{value: "quest", label: "闯关"}, {value: "pvp", label: "对战"}]);
    bar.appendChild(modeSelect);
    // ★ 关卡 / 难度两个筛选（用户 2026-09-07）。选项照规则行上那两个下拉
    //   （`SCHEMA.drops`，一个出处），前面多一项「不限」= 只看没指定关卡 /
    //   难度的规则。对战没有关卡和难度（`PVP_LOCKED_KEYS`）⇒ 模式筛成「对战」
    //   时这两个下拉清空并锁住，和规则行上的表现一致。
    var scoped = PVP_LOCKED_KEYS.map(function (key) {
      var spec = dropSpec(key);
      var options = [{value: "none", label: spec.empty_label || "不限"}];
      (spec.options || []).forEach(function (option) { options.push(option); });
      var select = selectFilter(filter, key, "全部" + (spec.label || key), options);
      bar.appendChild(select);
      return select;
    });
    var lockForPvp = function () {
      var pvp = filter.mode === "pvp";
      scoped.forEach(function (select, at) {
        if (pvp) { filter[PVP_LOCKED_KEYS[at]] = ""; select.value = ""; }
        select.disabled = pvp;
        select.title = pvp ? "对战没有关卡和难度" : "";
      });
    };
    // `selectFilter` 自己的 onchange 先跑（写 `filter.mode` + 重画），这一发
    // 排在它后面：清掉两个子筛选再重画一次。
    modeSelect.addEventListener("change", function () {
      lockForPvp();
      repaintList();
    });
    lockForPvp();
  }
  if (which === "cards") {
    // ★★ 这一页的筛选条**只剩分组切换**（用户 2026-09-13 第四轮点的题）：
    //   模式 / 关卡 / 难度 / 统计范围 / 武器 / 状态那六个下拉全删了。
    //   17 行一屏就看完，一排下拉换不来任何东西；而且条件挪进弹窗之后，
    //   「按武器筛」这种口径还得先解释「有没有哪条条件是这样的」。
    bar.appendChild(cardGroupSwitch());
  }
  if (which === "rewards") {
    // ★ 「金币 / 经验」切换挪进工具条、放**最左边**（用户 2026-09-10，D72b）。
    //   原来它独占分类标签那一整行 —— 一行只放两个钮太浪费版面。
    bar.appendChild(rewardViewSwitch());
  }

  // ★ 「↻ 刷新」排在筛选控件**后面**，**每个配置页都有**（用户 2026-09-06）
  //   —— 点哪一页的都是把 `CONFIGS` 那几份一起重读，见 `refreshConfigs()`。
  if (which === "rewards") {
    // ★ 这一钮**故意长得不一样**（用户 2026-09-10，D72b）：它不是「切到哪一半」
    //   的开关，是「弹一张只读的表出来」—— 深棕底 + 琥珀边（照面板标题栏那身
    //   衣服），一眼就能和左边那两个分开，又还在这一页的配色里。
    // ★ 排在「↻ 刷新」**前面**（用户第二轮）：它和左边那两个都是「看什么」，
    //   刷新是「重读一遍」，两类事分开摆。
    var curve = el("button", "btn btn-sm btn-ref", "▤ 等级经验对应表");
    curve.title = "打开等级与经验的对应表（只读）";
    curve.onclick = openLevelModal;
    bar.appendChild(curve);
  }

  var refresh = el("button", "btn btn-sm", "↻ 刷新");
  refresh.title = "重新读一遍服务端上的 " + configCount()
                  + " 份配置（不只是这一页）";
  refresh.onclick = function () { refreshConfigs(); };
  bar.appendChild(refresh);

  // ★★ 这一行**最多只能有一个 `.grow`**（`margin-left: auto`）：两个的话
  //   flex 会把空隙在它们之间平分，两样东西被拆到两处去（`.tool-right`
  //   那段注释里记着同一个坑）。⇒ 称号卡片页拿这一格放那颗钮，
  //   「筛出 x / y」那一小段就不画了（用户 2026-09-13 第六轮点名删掉的）。
  if (which === "cards") {
    // 「查看本人达成进度」：**只看自己**。它压根不发 `name`，服务端那一句
    // （`_admin_card_progress`）才是「不能看别人」的门 ⇒ 三档身份都画。
    // ★ 靠右贴着「已和服务端一致」那一组；只读身份下那一组整个藏起来
    //   （`applyReadOnly`），这颗钮就自然落到整行最右边 —— 正是用户要的。
    var mine = el("button", "btn btn-sm grow", "查看本人达成进度");
    mine.title = "只看你自己那个同名游戏账号的进度";
    mine.onclick = function () { openCardProgress(null); };
    bar.appendChild(mine);
    return;
  }
  var shown = el("span", "grow");
  shown.id = "cfgShown";
  bar.appendChild(shown);
}

/** 绑在筛选条件 `filter[key]` 上的一个下拉。改了就重画列表
 *  （分类标签上的件数跟着变，见 `paintCfgTabs`）。 */
function selectFilter(filter, key, allLabel, options) {
  var select = fillSelect(document.createElement("select"), allLabel, options,
                          filter[key]);
  select.onchange = function () {
    filter[key] = select.value;
    repaintList();
  };
  return select;
}

/** 往一个 `<select>` 里填「全部…」+ 选项并选中 `value`（空串 = 第一项）。
 *  筛选条和两个弹窗里的下拉都从这儿出 —— 选项怎么写只有一处。 */
function fillSelect(select, allLabel, options, value) {
  select.textContent = "";
  var first = el("option", null, allLabel);
  first.value = "";
  select.appendChild(first);
  options.forEach(function (option) {
    var node = el("option", null, option.label);
    node.value = String(option.value);
    select.appendChild(node);
  });
  select.value = value || "";
  return select;
}

/** 「角色」下拉的选项：泰尔 / 卡希尔 / 布洛克（`CAT.characters`，服务端
 *  `shopcfg.CHARACTER_ZH`）。★ 列**全部角色**而不是「这批条目里出现过的」
 *  —— 筛出空列表也是有用的信息（「原来泰尔一件鞋都没有」）。 */
function characterOptions() {
  return Object.keys(CAT.characters).map(function (cid) {
    return {value: cid, label: CAT.characters[cid]};
  });
}

/** 角色 / 上架状态两个下拉共用的判据（D68）—— 四处筛的是同一件事，写一处。
 *  `want` = {character, listing}，空串 = 不筛。
 *  ★ 角色按**物品库里那份角色限定**筛（D31），不看条目自己带的键 ——
 *    在物品库里把一件东西改成「不限」之后，它就不该再出现在「泰尔」这一档里
 *    （`character` 那个键是**删掉**表示不限的，拿 `undefined` 退回原版数据
 *    会让「改成不限」看上去没生效）。
 *  ★ 上架状态看**当前页面模型**（D42a）：没保存的改动也算。 */
function dropdownsMatch(itemId, want) {
  if (want.character && String(itemRuleOf(itemId).character) !== want.character) {
    return false;
  }
  return listingMatches(itemId, want.listing);
}

/** 一份空的筛选条件。★ 加字段时只改这一处 —— 页面上有三个地方要「清筛选」。
 *  `big` / `sub` 是分类标签（`anyTab()` 那两个字段），掉落页用不上但留着不碍事。 */
function emptyFilter() {
  return {q: "", character: "", listing: "", big: -1, sub: null,
          mode: "", stage: "", difficulty: "",
          // 称号卡片页：武器 / 统计范围 / 能不能获得 / 看哪一组
          // （成就 · 武器 · 全部）。
          weapon: "", scope: "", gettable: "", group: CARD_GROUPS[0].id,
          // 「金币 / 经验获取」页当前看的是哪一半（`REWARD_VIEWS`）。
          view: REWARD_VIEWS[0].id};
}

/** `SCHEMA.drops` 里某个字段的描述（下拉选项从这儿取，不另抄一份）。 */
function dropSpec(key) {
  var found = null;
  (CAT.schema.drops.fields || []).forEach(function (spec) {
    if (spec.key === key) { found = spec; }
  });
  return found || {key: key};
}

/** 掉落页的关卡 / 难度筛选：`""` = 不筛；`"none"` = 只看没指定的
 *  （规则行上显示「不限」）；其余 = 号码相等。 */
function dropFieldMatches(value, wanted) {
  if (!wanted) { return true; }
  var unset = (value === undefined || value === null || value === "");
  if (wanted === "none") { return unset; }
  return !unset && String(value) === wanted;
}

//: 每个配置标签页画成什么样。★ 加一份配置时只要在这儿登记一行。
//  （函数声明会被提升，所以写在它们前面没问题。）
var RENDERERS = {items: renderItems, shop: renderShop,
                 recipe: renderRecipe, drops: renderDrops,
                 cards: renderCards, rewards: renderRewards};

function repaintList() {
  var list = $("cfgList");
  // ★ 重画**一律不动滚动条**（用户 2026-09-07）：清空再填是同一个任务里做完的，
  //   浏览器本来就不会动它；这一发是把「不许动」写死，免得日后谁往重画里插
  //   一句读版面的代码，位置就被夹没了。
  var keep = list.scrollTop;
  list.textContent = "";
  // ★ 「金币 / 经验获取」页从这儿分岔（D72）：它没有筛选、没有分类、也不排序
  //   —— 档位是固定的两张表，画的永远是全部。下面那一整套（`filteredEntries`
  //   / `narrowToTab` / 「筛出 x / y」）全是**按物品**算的，这一页一件物品
  //   都没有，走进去只会拿 `undefined` 去查物品表。
  if (CURRENT === "rewards") {
    paintCfgTabs(CURRENT, []);
    var shownLabel = $("cfgShown");
    if (shownLabel) { shownLabel.textContent = ""; }
    RENDERERS.rewards(list, CFG.rewards.entries);
    lockList();
    list.scrollTop = keep;
    touched();
    return;
  }
  // 先过筛选条（搜索 / 下拉），分类标签上的件数从这儿数；再按当前标签收一遍
  // 才是画出来的那批。★ 换页栏没有了（D68）：筛完剩多少画多少，滚动条在列表上。
  var filtered = filteredEntries(CURRENT);
  paintCfgTabs(CURRENT, filtered);
  var rows = narrowToTab(CURRENT, filtered);
  var total = CFG[CURRENT].entries.length;
  var label = $("cfgShown");
  if (label) {
    // ★ 「筛出 x / y」只有**没有分类标签**的那几页写（用户 2026-09-09，D68a）：
    //   另外几页的分类标签上已经带着件数，再写一遍是重复的。一样多就不写。
    //   ★ 称号卡片页 2026-09-13 第六轮不画了（用户点名删掉）——
    //   那一格让给了「查看本人达成进度」，见 `renderToolbar`。
    var wantsCount = (CURRENT === "drops");
    label.textContent = (wantsCount && rows.length !== total)
      ? ("筛出 " + rows.length + " / " + total) : "";
  }
  if (!rows.length) {
    // ★ 只读身份下不能写「点『添加』」—— 那个钮在他画面上根本没有（D74）。
    list.appendChild(el("div", "list-empty",
                        total ? "没有符合筛选条件的条目"
                              : (isReadOnly() ? "这一份配置还是空的"
                                              : "还没有条目 —— 点「添加」")));
  }
  RENDERERS[CURRENT](list, rows);
  lockList();
  list.scrollTop = keep;
  touched();
}

/** 只读身份下，把刚画出来的列表**整个锁掉**（D74）。
 *
 * ★★ 为什么是「画完统一扫一遍」，而不是在每个控件工厂里各加一句
 *    `if (isReadOnly())`：这一页有 6 个渲染器、十几处生成控件的地方
 *    （`fieldNode` / `choiceNode` / `toggleNode` / `killButton` /
 *    `materialSlots` / `slotNode` 的选择器格子 / 奖励表的格子……），
 *    漏一个的症状是**玩家改得动、按不了保存、也不会收到任何报错**。
 *    扫一遍的判据是「结果」而不是「谁记得加那一句」—— 以后新加的控件
 *    自动被收进来。
 * ★ 判据成立的前提：`#cfgList` 里**只有编辑控件**。筛选条 / 分类标签 /
 *   「↻ 刷新」都在它外面（`#cfgToolbar` / `#cfgCats`），那些是「看」的东西，
 *   一个都不能动。
 * ★ 浮窗（`tipFor`）不受影响：它挂在 `mouseover` 上，也是「看」的东西。
 */
function lockList() {
  if (!isReadOnly()) { return; }
  var host = $("cfgList");
  function each(sel, fn) {
    Array.prototype.forEach.call(host.querySelectorAll(sel), fn);
  }
  // 输入框 / 下拉：**锁住**而不是换成纯文字 —— 用户要的是「看得见、
  // 改不了」，值还得原样摆在原来的位置上。
  each("input, select, textarea", function (node) { node.disabled = true; });
  // 按钮：整个拿掉。列表里的按钮没有一个是「看」的（✕ 删掉这一条、
  // 「移除」一种材料），留着灰的只会让人一直去点。
  each("button", function (node) { node.parentNode.removeChild(node); });
  // 「＋ 加一种材料」那个空格子：整格拿掉（它就是个「添加」按钮）。
  each(".slot.empty", function (node) {
    var cell = node.parentNode;                    // `.mat`
    cell.parentNode.removeChild(cell);
  });
  // 点了会弹「选择物品」的格子：摘掉 onclick，顺手去掉手型和「可点」的样子。
  each(".slot.pick", function (node) {
    node.onclick = null;
    node.classList.remove("pick");
  });
  // 布尔开关是个 `<label>`（不是 `<input>`），上面那一遍收不到它。
  each(".toggle", function (node) {
    node.onclick = null;
    node.classList.add("locked");
  });
}

/** 配置页那两行分类标签，钉在筛选条和列表之间（滚动区外面，原来换页栏的
 *  位置）。掉落页整块藏起来 —— 它不按物品分类。 */
function paintCfgTabs(which, filtered) {
  var bigHost = $("cfgCats");
  var subHost = $("cfgSubCats");
  // ★ 奖励页两行都藏起来：它不按物品分类，而「金币 / 经验」那个切换
  //   2026-09-10 挪进工具条了（D72b，见 `rewardViewSwitch`）。
  // ★ 称号卡片页也藏：17 张卡片在仓库树里全归「收集品 → 卡片」一格，
  //   画出来是「全部(17) / 收集品(17) / 卡片(17)」三个一模一样的钮
  //   —— 两行版面换零信息。分组切换在工具条上（`CARD_GROUPS`）。
  var hide = (which === "drops" || which === "rewards" || which === "cards");
  bigHost.classList.toggle("hidden", hide);
  if (hide) {
    bigHost.textContent = "";
    subHost.textContent = "";
    subHost.classList.add("hidden");
    return;
  }
  if (!FILTER[which]) { FILTER[which] = emptyFilter(); }
  paintCatTabs(bigHost, subHost, FILTER[which], function (id) {
    return filtered.filter(function (row) { return whMatches(id, row.cat); }).length;
  }, repaintList);
}

/** 只在这张卡**不在可视区里**时才把它挪进来，而且挪最少的距离（`nearest`，
 *  不做动画）；已经在视野里就一动不动。★ 全页只剩两处会主动动滚动条 ——
 *  保存报错定位到出错那张卡、「添加」定位到新卡 —— 都走这儿
 *  （用户 2026-09-07：不要自动调整滚动条，能删的都删）。 */
function revealIfHidden(card) {
  var list = $("cfgList");
  var box = list.getBoundingClientRect();
  var it = card.getBoundingClientRect();
  if (it.top >= box.top && it.bottom <= box.bottom) { return; }
  card.scrollIntoView({block: "nearest"});
}

/** 这一条说的是**哪件物品**。
 *
 *  ★ 合成配方的 `id` 是**配方号**不是物品 id（`SCHEMA.recipe` 里那一栏叫
 *    「配方号」），它的物品在 `result` 上 —— 一起当 `entry.id` 用的话，
 *    「按角色筛 / 按名字搜合成配方」拿去查的是编号 1、2、3 那几件东西，
 *    筛出来的全是错的。
 */
function entryItemId(which, entry) {
  if (which === "recipe") { return entry.result; }
  // 称号卡片那一页的物品在 `card` 上（同配方的 `result`，道理一样）。
  if (which === "cards") { return entry.card; }
  return (entry.id === undefined) ? entry.material : entry.id;
}

//: 称号卡片页顶上那个分组切换。★ 判据从 catalog 现取（`CAT.weapon_cards`），
//  **不在这儿写死 id 段** —— 那是服务端 `shopcfg.WEAPON_CARDS` 的事。
//  ★ 为什么不用 `paintCatTabs` 那棵仓库树：17 张卡片在树里全归
//    「收集品 → 卡片」一格，画出来是三个一模一样的钮。
var CARD_GROUPS = [
  {id: "all", label: "全部"},
  {id: "deed", label: "成就卡片"},
  {id: "weapon", label: "武器卡片"}
];

function cardGroupOf(cardId) {
  var weapons = CAT.weapon_cards || [];
  return (weapons.indexOf(Number(cardId)) >= 0) ? "weapon" : "deed";
}

/** 一条记录过不过**筛选条**（搜索串 / 下拉；分类标签另算，见 `narrowToTab`）。 */
function matches(which, entry) {
  var filter = FILTER[which] || {};
  var itemId = entryItemId(which, entry);
  var item = BYID[itemId];
  // ★ 拿 `entryItemId` 不拿 `entry.id`：合成配方的 `id` 是配方号。以前这一句
  //   写的是 `entry.id`，那时只有物品库有「上架状态」筛选，没暴露出来。
  if (!dropdownsMatch(itemId, filter)) { return false; }
  // ★ 称号卡片页 2026-09-13 第四轮只剩**一个**筛选（成就卡 / 武器卡）——
  //   模式 / 关卡 / 难度 / 统计范围 / 武器 / 状态那六个下拉一起删了。
  if (which === "cards") {
    if (filter.group && filter.group !== "all"
        && cardGroupOf(itemId) !== filter.group) {
      return false;
    }
  } else if (filter.mode && (entry.mode || "quest") !== filter.mode) {
    return false;
  }
  if (which === "drops") {
    if (!dropFieldMatches(entry.stage, filter.stage)) { return false; }
    if (!dropFieldMatches(entry.difficulty, filter.difficulty)) { return false; }
  }
  if (filter.q) {
    var hay = [itemName(itemId), entry.note, String(itemId),
               item ? item.name_kr : "", item ? item.name : ""]
      .join(" ").toLowerCase();
    if (hay.indexOf(filter.q.toLowerCase()) < 0) { return false; }
  }
  return true;
}

/** 过了筛选条的那些记录，`[{entry, index, cat}]`，**还没按分类标签收**
 *  （标签上的件数要从这儿数）。**下标一律用原数组的**，服务端报错才对得上。 */
function filteredEntries(which) {
  var rows = [];
  CFG[which].entries.forEach(function (entry, index) {
    if (matches(which, entry)) {
      rows.push({entry: entry, index: index,
                 cat: whCategory(entryItemId(which, entry))});
    }
  });
  return rows;
}

/** 再按当前分类标签收一遍 = 画面上那份。掉落页的标签一直是「全部」，
 *  它只做排序（顺序在 `dropRank` 里定）。 */
function narrowToTab(which, rows) {
  var requested = tabRequested(FILTER[which] || anyTab());
  var out = rows.filter(function (row) { return whMatches(requested, row.cat); });
  if (which === "drops") {
    out.sort(function (a, b) {
      return (dropRank(a.entry) - dropRank(b.entry)) || (a.index - b.index);
    });
  }
  if (which === "cards") {
    // ★ 按卡片 id 排：成就卡（6xxxx）在前、武器卡（1x0xxx）在后，
    //   和物品表、和游戏仓库里的顺序一致。**每次都排**是安全的 ——
    //   这一页的顺序只看 `card`，而 `card` 是只读的，改哪一格都不会让行乱跳
    //   （`dropRank` 那套 WeakMap 是因为掉落页的排序键自己可编辑）。
    out.sort(function (a, b) {
      return (Number(a.entry.card) - Number(b.entry.card))
             || (a.index - b.index);
    });
  }
  return out;
}

/** 材料掉落的显示顺序：模式 → 关卡 → 难度 → 材料 递增（用户 2026-09-06），
 *  「不限」排在具体号码前面，闯关排在对战前面。
 *
 *  ★★ **只在整份数据换掉时排一次**（读盘 / 刷新 / 保存回执都会换掉
 *  `CFG.drops.entries` 这个数组），之后编辑**不再重排**：改完材料那一行
 *  当场跳到别处，视野里的内容整体换一截，看上去就是「滚动条自己滚了」
 *  （用户 2026-09-07）。名次按条目**对象**记（WeakMap），删条目不影响，
 *  新加的没有名次 ⇒ 排最后。`index` 仍是原数组的下标 —— 服务端报错、
 *  三方合并（D36）认的都是它。 */
var DROP_ORDER = {entries: null, rank: null};

function dropSortKey(entry) {
  function num(v) {
    return (v === undefined || v === null || v === "") ? 0 : (Number(v) || 0);
  }
  return [(entry.mode || "quest") === "quest" ? 0 : 1,
          num(entry.stage), num(entry.difficulty), num(entry.material)];
}

function dropRank(entry) {
  if (DROP_ORDER.entries !== CFG.drops.entries) {
    DROP_ORDER.entries = CFG.drops.entries;
    DROP_ORDER.rank = new WeakMap();
    CFG.drops.entries.map(function (item, index) {
      return {entry: item, index: index, key: dropSortKey(item)};
    }).sort(function (a, b) {
      for (var i = 0; i < a.key.length; i++) {
        if (a.key[i] !== b.key[i]) { return a.key[i] - b.key[i]; }
      }
      return a.index - b.index;
    }).forEach(function (row, at) {
      if (row.entry && typeof row.entry === "object") {
        DROP_ORDER.rank.set(row.entry, at);
      }
    });
  }
  var at = (entry && typeof entry === "object") ? DROP_ORDER.rank.get(entry)
                                                : undefined;
  return (at === undefined) ? Infinity : at;
}

function killButton(which, index) {
  var button = el("button", "kill", "✕");
  button.title = "删掉这一条";
  button.onclick = function () {
    CFG[which].entries.splice(index, 1);
    repaintList();
  };
  return button;
}

/* -------------------------------------------------- 物品库：卡片网格 */

/** 这件东西现在**上架在哪儿**：`"shop"` / `"recipe"` / `""`（都没有）。
 *
 * ★ 取的是**当前页面模型**（还没保存的改动也算）——
 *   管理员刚把一件东西勾上，浮窗和筛选就该跟着说。
 * ★ 商店和合成**互斥**（用户 2026-09-06）；真出现两边都上架的脏数据时
 *   先说商店 —— 保存时那道确认框会把它掰回互斥。
 */
function listingOf(itemId) {
  var id = Number(itemId);
  var where = "";
  ((CFG.recipe && CFG.recipe.entries) || []).forEach(function (entry) {
    if (entry && entry.listed && Number(entry.result) === id) { where = "recipe"; }
  });
  ((CFG.shop && CFG.shop.entries) || []).forEach(function (entry) {
    if (entry && entry.listed && Number(entry.id) === id) { where = "shop"; }
  });
  return where;
}

var LISTING_ZH = {shop: "商店", recipe: "合成", "": "未上架"};

/** 「上架状态」筛选的四个选项。★ 物品库那一页的筛选条和「选择物品」弹窗
 *  **共用这一份**（弹窗那个是用户 2026-09-09 要的）—— 同一个筛选出现在两处，
 *  抄两份迟早会分岔（选项名、顺序、判据各歪一点）。
 *  空值（下拉第一项）= 不筛。 */
var LISTING_FILTER_OPTIONS = [
  {value: "none", label: "只看未上架"},
  {value: "any", label: "只看已上架"},
  {value: "shop", label: "只看上架商店"},
  {value: "recipe", label: "只看上架合成"}];

/** 这件东西过不过「上架状态」筛选。
 *
 * ★ 「已上架」= **商店或合成**，哪一边都算。两边互斥是保存时那道确认框的事
 *   （D33），筛选这儿只问「上没上」。
 * ★ `want` 为空就直接放行 —— 不筛的时候一次 `listingOf` 都不跑
 *   （它要把货架和配方两份从头扫一遍，弹窗里每敲一个字都会重画）。
 */
function listingMatches(itemId, want) {
  if (!want) { return true; }
  var where = listingOf(itemId);              // "shop" / "recipe" / ""
  if (want === "any") { return !!where; }
  if (want === "none") { return !where; }
  return where === want;
}

/** 这件东西要不要「等级 / 角色限定」两栏。
    ★ 和服务端 `shopcfg.has_level_and_character()` **同一条判据**：
    `part_flag != 0`（占装备槽）。两边对不上的话，页面上填得进去、
    服务端存进去又当没看见 —— 那种「改了没反应」最难查。 */
function wearable(itemId) {
  var item = BYID[itemId];
  return !!(item && item.part_flag);
}

function renderItems(list, rows) {
  var grid = el("div", "grid");
  rows.forEach(function (row) {
    var entry = row.entry, index = row.index;
    var where = listingOf(entry.id);
    var card = el("div", "item-card" + (where ? " listed" : ""));
    card.setAttribute("data-index", index);
    // ★ 物品库**没有 ✕ 也没有「＋ 添加」**：这一页就是全物品表，条目由
    //   `shop_items.json` 决定，删掉一条只会让那件东西从页面上消失
    //   （等级和角色限定悄悄退回默认），没有人会想要这个。

    card.appendChild(slotNode(entry.id, 44, !!where, false));

    var col = el("div", "col");
    var nm = el("div", "nm");
    nm.appendChild(fieldNode({key: "name", label: "中文名", type: "text"},
                             entry, touched).lastChild);
    col.appendChild(nm);
    // ★ 上架状态排在**最前面**、韩文名一个字都不画：这一行放不下就会被
    //   省略号截掉，而「这东西现在在哪儿卖」比韩文原名重要得多
    //   （用户 2026-09-06：韩文太长，把上架信息挤没了）。韩文名在浮窗里看。
    var meta = el("div", "meta");
    meta.appendChild(el("b", "where", LISTING_ZH[where]));
    tipFor(meta, entry.id);
    meta.appendChild(document.createTextNode(
      "　" + itemMeta(entry.id, wearable(entry.id)) + " "));
    meta.appendChild(el("code", null, "#" + entry.id));
    col.appendChild(meta);

    // 穿不上身的东西（材料 / 礼包 / 消耗品 / 角色卡）没有这两栏 ——
    // 客户端根本不读，画出来只会让人以为「给材料设个 5 级就要 5 级才能捡」。
    if (wearable(entry.id)) {
      var nums = el("div", "nums");
      restFields("items", entry, ["id", "name", "kind"], touched)
        .forEach(function (node) { nums.appendChild(node); });
      // ★ 武器卡片右下角「自定义属性」（X3，用户 2026-09-19）：自定义武器改两套
      //   数值 + 说明文，原版武器只改说明文 —— 弹窗里按 `custom` 分。
      var item = BYID[entry.id];
      if (item && item.kind === "weapon") {
        var btn = el("button", "btn btn-sm weapon-btn", "自定义属性");
        btn.type = "button";
        btn.title = item.custom ? "改这把武器在任务 / 对战模式下的数值，以及说明文"
                                : "原版武器只能改说明文";
        btn.onclick = function () { openWeaponModal(entry.id); };
        nums.appendChild(btn);
      }
      col.appendChild(nums);
    }

    card.appendChild(col);
    grid.appendChild(card);
  });
  list.appendChild(grid);
}

/* -------------------------------------------------- 商店货架：卡片网格 */
function renderShop(list, rows) {
  var grid = el("div", "grid");
  rows.forEach(function (row) {
    var entry = row.entry, index = row.index;
    var card = el("div", "item-card" + (entry.listed ? " listed" : ""));
    card.setAttribute("data-index", index);
    card.appendChild(killButton("shop", index));

    var slot = slotNode(entry.id, 44, entry.listed, true);
    slot.onclick = function () {
      openPicker({selected: entry.id, onPick: function (item) {
        adoptItem(entry, "id", item);
        repaintList();
      }});
    };
    card.appendChild(slot);

    var col = el("div", "col");
    // ★ 中文名在**物品库**那一页改（D31），这里只显示。
    col.appendChild(el("div", "nm ro", itemName(entry.id)));
    col.appendChild(metaLine(entry.id, entry.kind));

    var nums = el("div", "nums");
    restFields("shop", entry, ["id", "name", "kind"], function () {
      card.classList.toggle("listed", !!entry.listed);
      slot.classList.toggle("on", !!entry.listed);
      touched();
    }).forEach(function (node) { nums.appendChild(node); });
    col.appendChild(nums);

    card.appendChild(col);
    grid.appendChild(card);
  });
  list.appendChild(grid);
}

function metaLine(itemId, kind) {
  // 卡片上那行小字也挂浮窗：一行放不下会被省略号截掉，浮窗里看得全，
  // 顺带把加成 / 武器数值也一起给了（D26）。
  var meta = tipFor(el("div", "meta"), itemId);
  meta.appendChild(document.createTextNode(itemMeta(itemId) + " "));
  meta.appendChild(el("code", null, "#" + itemId));
  var item = BYID[itemId];
  if (item && item.name_kr) {
    meta.appendChild(document.createTextNode(" " + item.name_kr));
  }
  if (kind && item && item.kind !== kind) {
    meta.appendChild(el("b", null, "  ★ kind 和物品对不上，保存后会被服务端改正"));
  }
  return meta;
}

/** 换了物品之后，跟着它走的那几个字段一起更新。
 *  ★ 名字不在这里跟 —— 中文名归物品库（D31），这两页只是显示它。 */
function adoptItem(entry, key, item) {
  entry[key] = item.id;
  if ("kind" in entry) { entry.kind = item.kind; }
  touched();
}

/* -------------------------------------------------- 合成配方：配方卡 */

/** 字段表里叫 `key` 的那一栏；没登记就 `null`。
 *
 * 给「要把某一栏从 `restFields` 里拿出来单独摆」用（配方卡的
 * 花费 / 上架）。拿不到时**别自己编一份 spec**：直接返回 `null`，
 * 让调用方把这个键留给 `restFields`（它会当「未登记字段」画出来），
 * 否则字段表一改名，那个键就从页面上悄悄消失、又被原样存回去。 */
function fieldSpec(which, key) {
  var found = null;
  ((CAT.schema[which] || {}).fields || []).forEach(function (spec) {
    if (spec.key === key) { found = spec; }
  });
  return found;
}

function renderRecipe(list, rows) {
  rows.forEach(function (row) {
    var entry = row.entry, index = row.index;
    var card = el("div", "recipe-card" + (entry.listed ? " listed" : ""));
    card.setAttribute("data-index", index);
    card.appendChild(killButton("recipe", index));
    card.appendChild(el("span", "rid", "配方 #" + (entry.id === undefined ? "?" : entry.id)));

    var head = el("div", "recipe-head");

    // 代价那一侧：4 个材料格 + 花费。金币和材料是**同一侧**的代价，
    // 摆在一块儿才读得出「材料 + 金币 ➜ 产物」（用户 2026-09-08）。
    var into = el("div", "recipe-in");
    into.appendChild(materialSlots(entry, card));
    var costSpec = fieldSpec("recipe", "cost");
    if (costSpec) { into.appendChild(fieldNode(costSpec, entry, touched)); }
    head.appendChild(into);

    // ★ 箭头是**产物那一侧**的头一个，不是 `head` 的直接子节点：窄屏上这一行
    //   要折的时候，得让「➜ 产物」一起掉到第二行去，别把一个光秃秃的箭头
    //   留在材料那一行的末尾。
    var out = el("div", "recipe-out");
    out.appendChild(el("span", "arrow", "➜"));
    var slot = slotNode(entry.result, 52, entry.listed, true);
    slot.onclick = function () {
      openPicker({selected: entry.result, onPick: function (item) {
        adoptItem(entry, "result", item);
        repaintList();
      }});
    };
    out.appendChild(slot);
    var col = el("div", "col");
    // ★ 中文名在**物品库**那一页改（D31），这里只显示。
    col.appendChild(el("div", "nm ro", itemName(entry.result)));
    col.appendChild(metaLine(entry.result));
    out.appendChild(col);
    head.appendChild(out);

    var relist = function () {
      card.classList.toggle("listed", !!entry.listed);
      slot.classList.toggle("on", !!entry.listed);
      touched();
    };
    // 上架是整条配方的总开关，不属于「材料」也不属于「产物」——
    // 甩到整行最右边（CSS 里的 `margin-left: auto`）。
    var listedSpec = fieldSpec("recipe", "listed");
    if (listedSpec) { head.appendChild(fieldNode(listedSpec, entry, relist)); }
    card.appendChild(head);

    // 第二行只剩**字段表之外的键**；一个都没就不画，别留一条空虚线。
    var skip = ["id", "result", "name", "materials"];
    if (costSpec) { skip.push("cost"); }
    if (listedSpec) { skip.push("listed"); }
    var extra = restFields("recipe", entry, skip, relist);
    if (extra.length) {
      var foot = el("div", "recipe-foot");
      extra.forEach(function (node) { foot.appendChild(node); });
      card.appendChild(foot);
    }
    list.appendChild(card);
  });
}

/** 固定画 `max_materials` 格 —— 原版合成界面只有 4 个槽，第 5 种玩家看不见。 */
function materialSlots(entry, card) {
  var box = el("div", "mat-slots");
  if (!Array.isArray(entry.materials)) { entry.materials = []; }
  var spec = fieldSpec("recipe", "materials");
  var max = (spec && spec.max) || CAT.max_materials || 4;
  var countSpec = {key: "count", label: "数量", type: "int", min: 1, max: 800};
  (spec && spec.fields || []).forEach(function (field) {
    if (field.key === "count") { countSpec = field; }
  });

  for (var slotNo = 0; slotNo < max; slotNo += 1) {
    (function (position) {
      var material = entry.materials[position];
      if (!material) {
        var empty = el("div", "mat");
        var add = el("div", "slot empty", "＋");
        add.style.width = "50px";
        add.style.height = "50px";
        add.title = "加一种材料";
        add.onclick = function () {
          openPicker({kinds: ["material"], onPick: function (item) {
            entry.materials.push({id: item.id, count: 1});
            repaintList();
          }});
        };
        empty.appendChild(add);
        box.appendChild(empty);
        return;
      }
      var cell = el("div", "mat");
      var slot = slotNode(material.id, 40, true, true);
      slot.onclick = function () {
        openPicker({kinds: ["material"], selected: material.id,
          onPick: function (item) { material.id = item.id; repaintList(); }});
      };
      cell.appendChild(slot);
      // ★ 名字问物品库（`itemName`，D31）—— 以前读的是物品表里自动翻的那份，
      //   在物品库里把「龙之泪」改成「龙之血」之后，这一格还写着旧名字
      //   （用户 2026-09-06 报的）。
      cell.appendChild(el("div", "nmz", itemName(material.id)));
      var count = fieldNode(countSpec, material, touched);
      cell.appendChild(count.lastChild);
      var drop = el("button", "btn btn-sm drop", "移除");
      drop.onclick = function () {
        entry.materials.splice(position, 1);
        repaintList();
      };
      cell.appendChild(drop);
      box.appendChild(cell);
    }(slotNo));
  }
  return box;
}

/* -------------------------------------------------- 材料掉落：规则行 */
function renderDrops(list, rows) {
  rows.forEach(function (line) {
    list.appendChild(dropRow(line.entry, line.index));
  });
}

//: 对战局结算时是拿 `quest_id=0, difficulty=0` 去匹配规则的
//  （`gameserver.quest_materials` 的调用点）—— 一条对战规则只要写了关卡
//  或难度就**永远不掉**。所以模式选成「对战」时这两栏自动清成「不限」并
//  锁住（用户 2026-09-07）。筛选条上的那两个下拉也照这张表锁。
var PVP_LOCKED_KEYS = ["stage", "difficulty"];

/** 一条掉落规则那一行。改「模式」时整行重画一遍 —— 关卡 / 难度锁不锁
 *  只在这一处决定，别在别处再写一份。 */
function dropRow(entry, index) {
  var row = el("div", "rule-row");
  row.setAttribute("data-index", index);
  row.appendChild(killButton("drops", index));

  // 整块挂浮窗：材料格是定宽的（D68b），名字太长会截成省略号，停上去看全名。
  var who = tipFor(el("div", "who"), entry.material);
  var slot = slotNode(entry.material, 36, true, true);
  slot.onclick = function () {
    openPicker({kinds: ["material"], selected: entry.material,
      onPick: function (item) { adoptItem(entry, "material", item);
                                repaintList(); }});
  };
  who.appendChild(slot);
  // ★ 同上：名字只认物品库那一份（D31）。
  who.appendChild(el("div", "nmz", itemName(entry.material)));

  var pvp = entry.mode === "pvp";
  var placed = false;
  restFields("drops", entry, ["material"], touched).forEach(function (node) {
    var key = node.getAttribute("data-key");
    var select = node.querySelector ? node.querySelector("select") : null;
    if (key === "mode" && select) {
      // `choiceNode` 自己的 onchange 先跑（写 `entry.mode`），这一发排在它
      // 后面：切成对战就把关卡 / 难度删掉，然后整行按新模式重画。
      select.addEventListener("change", function () {
        if (entry.mode === "pvp") {
          PVP_LOCKED_KEYS.forEach(function (locked) { delete entry[locked]; });
        }
        row.parentNode.replaceChild(dropRow(entry, index), row);
        touched();
      });
    }
    if (pvp && select && PVP_LOCKED_KEYS.indexOf(key) >= 0) {
      // ★ 只锁不改：磁盘上要是有一条手改出来的「对战 + 关卡 3」，这儿照实
      //   显示那个 3（锁着）—— 渲染时悄悄删掉它会让页面一打开就「有未保存
      //   的修改」。切一次模式它就清掉了。
      select.disabled = true;
      select.title = "对战没有关卡和难度 —— 模式改成「闯关」才能选";
    }
    // 材料格子插在「难度」后面，和原来 json 里的字段顺序一致。
    row.appendChild(node);
    if (!placed && key === "difficulty") {
      row.appendChild(who);
      placed = true;
    }
  });
  if (!placed) { row.appendChild(who); }
  return row;
}

/* ------------------------------------------------ 称号卡片掉落：一卡一行
   用户 2026-09-13 点的题（V0.3商店）。和「材料掉落」的三处刻意不同：

     · **卡片格不可点** —— 一张卡片只能有一条规则，让它可点等于请人去造
       重复主键。这条约束靠画面本身兜住，不靠保存时报错。
     · **没有删除、没有「添加」** —— 17 张是原版定死的，删一行会被
       `fillCards()` 立刻补回来。「这一版不给」= 把「能获得」关掉。
     · **「能获得」排在最前**，紧挨卡片名：这一页多半时间是在看
       「哪些开着」，开关得一眼看得见。关着的行整行淡下去（`.off`）。

   ★★ **2026-09-13 第三轮重排（用户点的题）：行里不放设置项。**
     前两版都是把十来个下拉摊在行里 —— 第一版自由换行、第二版三段 grid。
     格子再怎么对齐，一行十来个控件终究得人自己在脑子里读成一句话。
     现在一行只有四样：**图标+名称 / 能获得 / 那句现算的说明 / 两颗钮**，
     具体设置全在两个弹窗里（`openCardMode` / `openCardCond`）。

   ★★ **那句说明的「词」全部来自服务端**（`CAT.card_text`，出处是
     `shopcfg.CARD_PHRASES` 那一套）。句子的**骨架**这儿和
     `shopcfg.describe_card_rule()` 各写了一遍 —— 因为弹窗要**实时**画它
     （改一格就变），来回问服务端太慢。⇒ 所以这儿一个中文字都不写死：
     改服务端那张表，游戏提示框和这一页同时改口。

   ★ 两个弹窗里的格子**照 `SCHEMA.cards` 现画**（D16）：顶层字段除了
     卡片 / 能获得 / 条件三样都归「对局模式」窗，条件的子字段表归
     「达成条件」窗 —— 服务端加一格，画面上自动多一格。
   -------------------------------------------------------------------- */

/** `SCHEMA.cards` 里某个字段的描述（下拉选项从这儿取，不另抄一份）。 */
function cardSpec(key) {
  var found = null;
  (((CAT.schema || {}).cards || {}).fields || []).forEach(function (spec) {
    if (spec.key === key) { found = spec; }
  });
  return found || {key: key, label: key, type: "int", options: []};
}

/** 「达成条件」里**一条条件**的某个字段描述（`SCHEMA.cards.conditions.fields`）。 */
function cardCondSpec(key) {
  var found = null;
  (cardSpec("conditions").fields || []).forEach(function (spec) {
    if (spec.key === key) { found = spec; }
  });
  return found || {key: key, label: key, type: "int", options: []};
}

/** 说明文用到的词表 / 判据参数。★ 两份都来自服务端，缺了也不要炸页面。 */
function cardWords() { return CAT.card_text || {}; }
function cardLimits() { return CAT.card_limits || {}; }

/** 一条指标的描述（`CAT.card_metrics` 那一份）。 */
function cardMetricInfo(metric) {
  return (CAT.card_metrics || {})[metric] || {};
}

/** 这条规则的条件列表（永远是个数组，直接改它就是改模型）。 */
function cardConditions(entry) {
  if (!Array.isArray(entry.conditions)) { entry.conditions = []; }
  return entry.conditions;
}

/* ---------------------------------------------- 那句说明文（实时算）

   ★★ 和服务端 `shopcfg.card_mode_text` / `card_condition_text` /
     `card_conditions_text` / `describe_card_rule` **一一对应**，
     顺序和分支都照抄 —— 两边只要有一处念得不一样，运营在这一页看到的
     和游戏里提示框写的就对不上了。改这儿先去改那边。 */

/** 「在对战模式中，」这半句。 */
function cardModeText(entry) {
  var words = cardWords();
  var phrases = words.phrases || {};
  var text = (words.modes || {})[entry.mode];
  text = text ? (text + (phrases.mode_suffix || "")) : (phrases.any_mode || "");
  if (entry.stage !== undefined && entry.stage !== null) {
    var quest = (words.quests || {})[String(entry.stage)];
    text += "「" + (quest ? (entry.stage + " · " + quest) : entry.stage) + "」";
  }
  if (entry.difficulty !== undefined && entry.difficulty !== null) {
    var hard = (words.difficulties || {})[String(entry.difficulty)];
    text += (phrases.open || "") + (hard || entry.difficulty)
            + (phrases.close || "");
  }
  return (phrases.head || "%s").replace("%s", text);
}

/** 一条条件念成人话。`withScope` 为假时不写「一局内的 / 玩家累计」。 */
function cardCondText(cond, withScope) {
  var words = cardWords();
  var limits = cardLimits();
  var info = cardMetricInfo(cond.metric);
  var label = info.label || cond.metric || "";
  // 带武器维度时加前缀（「左轮手枪（泰尔）击杀数」），和服务端
  // `card_metric_label()` 同一个规矩：指标分不到武器时那一格不算数。
  if (info.weapon && cond.weapon) {
    label = ((words.weapons || {})[String(cond.weapon)] || cond.weapon) + label;
  }
  var head = withScope
    ? ((words.scope_prefix || {})[cond.scope || limits.scope_match] || "") : "";
  var values = info.values || [];
  if (values.length) {
    // 枚举指标没有「大于等于 1」这种说法：**本局结果为胜利 / 通关**。
    var shown = String(cond.threshold);
    values.forEach(function (option) {
      if (option.value === cond.threshold) { shown = option.label; }
    });
    return head + label + ((words.phrases || {}).is || "") + shown;
  }
  var op = (cond.scope === limits.scope_total)
    ? (words.every || "")
    : ((words.ops || {})[cond.op || limits.op_ge] || "");
  return head + label + op + cond.threshold + (info.unit || "");
}

/** 整串条件念成人话，**连接词混用时自己把括号写出来**。 */
function cardCondsText(conditions) {
  var words = cardWords();
  var limits = cardLimits();
  var phrases = words.phrases || {};
  var list = conditions || [];
  if (!list.length) { return ""; }
  // 所有条件同一档统计范围 ⇒ 前缀提到最前面只写一次。
  var scopes = {};
  list.forEach(function (cond) {
    scopes[cond.scope || limits.scope_match] = true;
  });
  var shared = (Object.keys(scopes).length === 1)
    ? ((words.scope_prefix || {})[list[0].scope || limits.scope_match] || "")
    : "";
  var joins = {};
  list.slice(1).forEach(function (cond) {
    joins[cond.join || limits.join_and] = true;
  });
  var mixed = Object.keys(joins).length > 1;
  var text = cardCondText(list[0], !shared);
  list.slice(1).forEach(function (cond, at) {
    // ★ `at` 从 0 起（切掉了第一条）⇒ 第三条开始才套括号，和服务端
    //   那句 `at >= 2` 是同一个位置。
    if (mixed && at >= 1) {
      text = (phrases.open || "") + text + (phrases.close || "");
    }
    text += (words.joins || {})[cond.join || limits.join_and] || "";
    text += cardCondText(cond, !shared);
  });
  return shared + text;
}

/** 整条规则那句话。★ 关着的 / 配得不对的各说一句固定的。 */
function cardRuleText(entry) {
  var phrases = cardWords().phrases || {};
  if (!entry.listed) { return phrases.off || ""; }
  if (cardRuleProblem(entry)) { return phrases.bad || ""; }
  return cardModeText(entry) + cardCondsText(entry.conditions)
         + (phrases.tail || "%s").replace("%s", phrases.card || "");
}

/* ---------------------------------------------- 「条件无效」的判据

   ★★ 和服务端 `shopcfg._validate_card_conditions` 的护栏一一对应。
     两边都在的理由：服务端那份**说了算**（保存时会拒），这一份是为了
     「改一格就当场看得见哪儿不对、保存钮按不动」。判据的**参数**
     （阈值下限、样本指标、条数上限）全从服务端拿（`CAT.card_limits`），
     这儿只写那几句提示。 */

/** 这条规则有没有毛病；没有就返回 `""`。 */
function cardRuleProblem(entry) {
  var limits = cardLimits();
  var list = entry.conditions || [];
  if (!Array.isArray(entry.conditions) || !list.length) {
    return "至少要有一条达成条件";
  }
  if (limits.max_conditions && list.length > limits.max_conditions) {
    return "最多 " + limits.max_conditions + " 条条件";
  }
  for (var at = 0; at < list.length; at += 1) {
    var why = cardCondProblem(list[at], at);
    if (why) { return "第 " + (at + 1) + " 条：" + why; }
  }
  // 比率指标（命中率）的样本下限：现在它是一条**普通条件**，
  // 所以护栏也换成「这串条件里有没有它」。
  var ratio = list.some(function (cond) {
    return cardMetricInfo(cond.metric).ratio;
  });
  if (!ratio) { return ""; }
  var ratioName = "";
  list.forEach(function (cond) {
    if (cardMetricInfo(cond.metric).ratio) {
      ratioName = cardMetricInfo(cond.metric).label;
    }
  });
  var sample = cardMetricInfo(limits.sample_metric).label || limits.sample_metric;
  var floor = list.some(function (cond) {
    return cond.metric === limits.sample_metric
      && (cond.scope || limits.scope_match) === limits.scope_match
      && (cond.op || limits.op_ge) === limits.op_ge && cond.threshold >= 1;
  });
  if (!floor) {
    return "用了「" + ratioName + "」就必须再加一条「" + sample
      + ((cardWords().ops || {})[limits.op_ge] || "") + " N」"
      + "—— 开一枪中一枪也是 100%";
  }
  if (list.some(function (c) { return c.join === limits.join_or; })) {
    return "用了「" + ratioName + "」的规则里不能出现「"
      + ((cardWords().joins || {})[limits.join_or] || "") + "」"
      + "—— 那样开枪数下限可以被绕开";
  }
  return "";
}

/** 一条条件有没有毛病；没有就返回 `""`。 */
function cardCondProblem(cond, at) {
  var limits = cardLimits();
  var words = cardWords();
  var info = cardMetricInfo(cond.metric);
  if (!info.label) { return "还没选统计指标"; }
  var scope = cond.scope || limits.scope_match;
  var scopes = info.scopes || [];
  if (scopes.length && scopes.indexOf(scope) < 0) {
    var wanted = scopes.map(function (key) {
      return choiceLabel(cardCondSpec("scope"), key);
    }).join(" / ");
    return "「" + info.label + "」只有" + wanted + "那一档才有意义";
  }
  var op = cond.op || limits.op_ge;
  var values = info.values || [];
  if (values.length && op !== limits.op_eq) {
    return "「" + info.label + "」只有两种取值，比较符只能是「"
      + ((words.ops || {})[limits.op_eq] || "") + "」";
  }
  if (scope === limits.scope_total && op !== limits.op_ge) {
    return "累计那一档只有「" + (words.every || "") + "」";
  }
  if (typeof cond.threshold !== "number"
      || Math.trunc(cond.threshold) !== cond.threshold) {
    return "数值要填一个整数";
  }
  var low = (limits.op_min || {})[op];
  if (low === undefined) { low = 1; }
  if (cond.threshold < low) {
    return "「" + ((words.ops || {})[op] || "") + "」的数值不能小于 " + low
      + (low ? "（填 0 的话每局都成立，等于白送）" : "");
  }
  if (limits.max_threshold && cond.threshold > limits.max_threshold) {
    return "数值最多 " + limits.max_threshold;
  }
  if (values.length && !values.some(function (o) {
    return o.value === cond.threshold;
  })) { return "「" + info.label + "」只能选表里那两种取值"; }
  if (cond.weapon !== undefined && cond.weapon !== null) {
    if (!info.weapon) { return "「" + info.label + "」分不到武器上"; }
    if (op !== limits.op_ge) {
      return "按武器统计时比较符只能是「"
        + ((words.ops || {})[limits.op_ge] || "") + "」"
        + "—— 别的方向对每一个没用过这把枪的人都成立";
    }
  }
  if (at === 0 && cond.join !== undefined) { return "第一条不该有连接词"; }
  return "";
}

/* ------------------------------------------------------------ 列表 */

/** 工具条最左那个「全部 / 成就卡片 / 武器卡片」分段切换。 */
function cardGroupSwitch() {
  var filter = FILTER.cards || (FILTER.cards = emptyFilter());
  var counts = {all: 0, deed: 0, weapon: 0};
  CFG.cards.entries.forEach(function (entry) {
    counts.all += 1;
    counts[cardGroupOf(entry.card)] += 1;
  });
  var seg = el("span", "seg");
  CARD_GROUPS.forEach(function (group) {
    var on = (filter.group || "all") === group.id;
    var button = el("button", "cat" + (on ? " on" : ""), group.label);
    button.appendChild(el("span", "n", String(counts[group.id])));
    button.onclick = function () {
      filter.group = group.id;
      // 选中态在按钮自己身上 ⇒ 整条工具条重画（同 `rewardViewSwitch`）。
      renderToolbar("cards");
      repaintList();
    };
    seg.appendChild(button);
  });
  return seg;
}

function renderCards(list, rows) {
  rows.forEach(function (line) {
    list.appendChild(cardRow(line.entry, line.index));
  });
}

/** 一张卡片一行：图标+名称 / 能获得 / 说明文 / 两颗钮。 */
function cardRow(entry, index) {
  var row = el("div", "rule-row card-row" + (entry.listed ? "" : " off"));
  row.setAttribute("data-index", index);

  // 卡片那一格：**不可点**（见本节开头）。整块挂浮窗，名字太长截成省略号。
  var who = tipFor(el("div", "who"), entry.card);
  var slot = slotNode(entry.card, 36, !!entry.listed, false);
  who.appendChild(slot);
  who.appendChild(el("div", "nmz", itemName(entry.card)));
  row.appendChild(who);

  var say = el("div", "card-say");
  var paint = function () {
    say.textContent = cardRuleText(entry);
    say.classList.toggle("bad-text",
                         !entry.listed || !!cardRuleProblem(entry));
    // 配得不对时把原因挂成 tooltip —— 行里只写「条件无效」四个字
    // （用户点名要的），到底哪儿不对进弹窗一看便知，鼠标停一下也能看见。
    say.title = entry.listed ? cardRuleProblem(entry) : "";
  };

  // 「能获得」一翻，整行跟着淡 / 亮，图标的上架底也跟着变（同 `renderRecipe`
  // 的 `relist`）—— 开关是 `label` 不是 `input`，收不到 `change` 事件。
  row.appendChild(fieldNode(cardSpec("listed"), entry, function () {
    row.classList.toggle("off", !entry.listed);
    slot.classList.toggle("on", !!entry.listed);
    paint();
    touched();
  }));

  paint();
  row.appendChild(say);

  // ★ 两颗钮的字**取自 SCHEMA 的 label**（「对局模式」= `mode` 那一格、
  //   「达成条件」= `conditions` 那一格），不在这儿写死 —— 服务端改了
  //   叫法，钮上的字跟着改。
  var acts = el("div", "card-acts");
  [[cardSpec("mode"), openCardMode],
   [cardSpec("conditions"), openCardCond]].forEach(function (pair) {
    var button = el("button", "btn btn-sm", pair[0].label || pair[0].key);
    if (pair[0].help) { button.title = pair[0].help; }
    button.onclick = function () { pair[1](entry); };
    acts.appendChild(button);
  });
  row.appendChild(acts);
  return row;
}

/* ------------------------------------ 两个弹窗共用的那几件事 */

//: 正开着的那个弹窗：`{which, entry, draft}`。两个窗不会同时开。
var CARD_EDIT = null;

/** 按键名排序的 JSON —— 拿来比「改没改过」。
 *
 * ★ 不能直接 `JSON.stringify` 比：改一格常常是**给对象补一个键**
 *   （比如条件行补上 `join`），键的顺序一变，两串就不相等了 ——
 *   那会变成「什么都没改也说有未保存的修改」。
 */
function stableJson(value) {
  if (Array.isArray(value)) {
    return "[" + value.map(stableJson).join(",") + "]";
  }
  if (value && typeof value === "object") {
    return "{" + Object.keys(value).sort().map(function (key) {
      return JSON.stringify(key) + ":" + stableJson(value[key]);
    }).join(",") + "}";
  }
  return JSON.stringify(value === undefined ? null : value);
}

/** 弹窗里那几格改过没有（`CARD_EDIT.keys` = 这个窗管着哪几个键）。 */
function cardEditDirty() {
  if (!CARD_EDIT) { return false; }
  var pick = function (source) {
    var out = {};
    CARD_EDIT.keys.forEach(function (key) { out[key] = source[key]; });
    return stableJson(out);
  };
  return pick(CARD_EDIT.entry) !== pick(CARD_EDIT.draft);
}

/** 弹窗里改的是**一份拷贝**：点「取消」就该什么都没发生。 */
function cardDraft(entry) {
  var copy = {};
  Object.keys(entry).forEach(function (key) { copy[key] = entry[key]; });
  copy.conditions = (entry.conditions || []).map(function (cond) {
    var one = {};
    Object.keys(cond).forEach(function (key) { one[key] = cond[key]; });
    return one;
  });
  return copy;
}

/** 弹窗顶上那句实时说明（和行里那句是同一个函数算的）。 */
function paintCardSay() {
  if (!CARD_EDIT) { return; }
  var cond = CARD_EDIT.which === "cond";
  var box = $(cond ? "cardCondSay" : "cardModeSay");
  var why = cardRuleProblem(CARD_EDIT.draft);
  // ★ 弹窗里**当它是开着的**来念：这两个窗设的是「怎么才给」，而
  //   「这一版给不给」是行上那个开关的事 —— 关着的时候也得看得见
  //   自己正在配什么。
  var shown = cardDraft(CARD_EDIT.draft);
  shown.listed = true;
  box.textContent = cardRuleText(shown);
  box.classList.toggle("bad-text", !!why);
  // ★ 排在下面那句 `if (!cond) return` **前面** —— 「对局模式」那个窗
  //   走的就是那一支，排在后面的话它那几格永远标不上。
  paintCardEdited();
  if (!cond) { return; }
  // 「条件无效」四个字底下补一行说清哪儿不对 —— 只写四个字的话，
  // 人得自己一格格试出来是哪一条错了。
  $("cardCondWhy").textContent = why;
  $("cardCondWhy").classList.toggle("hidden", !why);
  // ★ 配不出来的东西**存不进去**（用户 2026-09-13：「此时无法保存」）。
  $("cardCondSave").disabled = !!why;
}

/** 弹窗里改过的那几格 / 那几行套上「改过了」那一圈（用户 2026-09-14）。
 *
 * 比的是**打开弹窗那一刻的规则**（`CARD_EDIT.entry`），和 `cardEditDirty()`
 * 一个口径 —— 那一句管「关窗前要不要问」，这一圈管「到底动了哪一格」。
 * ★ 条件行按**位置**比：删掉第一条之后后面全体上移，那几行确实都变了
 *   （存回去的就是新的顺序），照实标出来比假装没动强。 */
function paintCardEdited() {
  if (!CARD_EDIT) { return; }
  var entry = CARD_EDIT.entry, draft = CARD_EDIT.draft;
  if (CARD_EDIT.which === "mode") {
    Array.prototype.forEach.call(
      $("cardModeFields").querySelectorAll("[data-key]"), function (node) {
        var key = node.getAttribute("data-key");
        node.classList.toggle(
          "edited", stableJson(draft[key]) !== stableJson(entry[key]));
      });
    return;
  }
  var was = entry.conditions || [];
  Array.prototype.forEach.call(
    $("cardCondRows").querySelectorAll("[data-at]"), function (row) {
      var at = Number(row.getAttribute("data-at"));
      row.classList.toggle(
        "edited",
        stableJson((draft.conditions || [])[at]) !== stableJson(was[at]));
    });
}

/** 把弹窗里改好的那份写回规则，然后整页重画一遍。 */
function saveCardEdit() {
  if (!CARD_EDIT) { return; }
  var entry = CARD_EDIT.entry;
  var draft = CARD_EDIT.draft;
  CARD_EDIT.keys.forEach(function (key) {
    if (draft[key] === undefined) { delete entry[key]; }
    else { entry[key] = draft[key]; }
  });
  // ★ 存完再关 —— 这时 `cardEditDirty()` 已经是假的，不会再弹「未保存」。
  CARD_EDIT = null;
  $("cardModeModal").classList.add("hidden");
  $("cardCondModal").classList.add("hidden");
  touched();
  // ★ 整页重画而不是只改那一行：说明文变了以后，筛选条（统计范围 /
  //   武器）该不该继续留着这一行也跟着变。
  repaintList();
}

/** 关掉弹窗。**有没保存的改动就先问一句**（用户 2026-09-13 第五轮）。
 *
 * ★ 「取消」「✕」「Esc」三条路都走这儿 —— 三处各写一遍确认的话，
 *   总有一条会漏（而漏掉的那条正好是手一滑最容易碰到的）。
 * ★ 两个窗一个待遇：丢的都是「刚敲进去还没存的东西」。
 */
async function closeCardEdit() {
  if (CARD_EDIT && cardEditDirty()) {
    var go = await ask({
      title: "有未保存的修改",
      lead: "「" + CARD_EDIT.title + "」里改过的东西还没保存，关掉就丢了。",
      ok: "关掉不保存", cancel: "回去接着改", danger: true});
    if (!go) { return; }
  }
  CARD_EDIT = null;
  $("cardModeModal").classList.add("hidden");
  $("cardCondModal").classList.add("hidden");
}

/* ------------------------------------- 弹窗①「对局模式」：算哪一局

   模式 / 关卡 / 难度三格。★ **关卡和难度只在选了「闯关」时才画** ——
   对战没有关卡（服务端直接拒），而「不限」谈不上是哪一关。
   ★ 格子照 `SCHEMA.cards` 现取（除了卡片 / 能获得 / 条件三样），
     服务端加一个「算哪一局」的新字段，这个窗里自动多一格。 */

//: 这两格只有「闯关」才有意义（同 `dropRow` 的 `PVP_LOCKED_KEYS`）。
var CARD_QUEST_ONLY_KEYS = PVP_LOCKED_KEYS;
//: 不归「对局模式」窗管的三样。
var CARD_MODE_SKIP_KEYS = ["card", "listed", "conditions"];

function openCardMode(entry) {
  // ★ 这个窗管哪几个键**照 SCHEMA 现算**：服务端给「算哪一局」加一个字段，
  //   它自动跟着存、也自动算进「改过没有」（D16）。
  var keys = (((CAT.schema || {}).cards || {}).fields || [])
    .map(function (spec) { return spec.key; })
    .filter(function (key) { return CARD_MODE_SKIP_KEYS.indexOf(key) < 0; });
  CARD_EDIT = {which: "mode", entry: entry, draft: cardDraft(entry),
               keys: keys, title: cardSpec("mode").label || ""};
  $("cardModeTitle").textContent =
    CARD_EDIT.title + "　—— " + itemName(entry.card);
  renderCardMode();
  $("cardModeModal").classList.remove("hidden");
}

function renderCardMode() {
  if (!CARD_EDIT || CARD_EDIT.which !== "mode") { return; }
  var draft = CARD_EDIT.draft;
  var host = $("cardModeFields");
  host.textContent = "";
  (((CAT.schema || {}).cards || {}).fields || []).forEach(function (spec) {
    if (CARD_MODE_SKIP_KEYS.indexOf(spec.key) >= 0) { return; }
    // ★ 关卡 / 难度只有「闯关」才画（用户点名的联动）。**但磁盘上真有值的
    //   照画** —— 手改进来的值不能从画面上消失，不然那个人既看不见它、
    //   也没法把它去掉（同上一版「用不上但有值就画出来」那条）。
    if (CARD_QUEST_ONLY_KEYS.indexOf(spec.key) >= 0 && draft.mode !== "quest"
        && draft[spec.key] === undefined) { return; }
    var node = fieldNode(spec, draft, paintCardSay);
    var control = node.querySelector ? node.querySelector("select, input") : null;
    if (control && spec.key === "mode") {
      // `choiceNode` 自己的 onchange 先跑（写进 draft），这一发排在后面。
      control.addEventListener("change", function () {
        // 对战 / 不限都没有关卡和难度 —— 切过去就把那两格清掉（留着的话
        // 服务端会拒，而人看不出为什么）。
        if (draft.mode !== "quest") {
          CARD_QUEST_ONLY_KEYS.forEach(function (key) { delete draft[key]; });
        }
        renderCardMode();
      });
    }
    host.appendChild(node);
  });
  paintCardSay();
}

/* -------------------------------- 弹窗②「达成条件」：一行一条 and / or */

function openCardCond(entry) {
  CARD_EDIT = {which: "cond", entry: entry, draft: cardDraft(entry),
               keys: ["conditions"],
               title: cardSpec("conditions").label || ""};
  if (!cardConditions(CARD_EDIT.draft).length) {
    CARD_EDIT.draft.conditions.push(cardNewCondition(0));
  }
  $("cardCondTitle").textContent =
    CARD_EDIT.title + "　—— " + itemName(entry.card);
  renderCardCond();
  $("cardCondModal").classList.remove("hidden");
}

/** 新加一条条件时的样子。★ 指标取下拉里的**第一项**（服务端排的序），
 *  别在这儿写死一个 key。 */
function cardNewCondition(at) {
  var limits = cardLimits();
  var first = (cardCondSpec("metric").options || [])[0] || {};
  var cond = {scope: limits.scope_match, metric: first.value,
              op: limits.op_ge, threshold: 1};
  if (at > 0) { cond.join = limits.join_and; }
  cardCondFix(cond);
  return cond;
}

function renderCardCond() {
  if (!CARD_EDIT || CARD_EDIT.which !== "cond") { return; }
  var list = cardConditions(CARD_EDIT.draft);
  var host = $("cardCondRows");
  host.textContent = "";
  list.forEach(function (cond, at) {
    host.appendChild(cardCondRow(cond, at, list));
  });
  var max = cardLimits().max_conditions || list.length;
  var count = $("cardCondCount");
  count.textContent = list.length + " / " + max;
  count.classList.toggle("full", list.length >= max);
  $("cardCondAdd").disabled = list.length >= max;
  paintCardSay();
}

/** 「达成条件」弹窗里的一行。
 *
 * ★ 格子**照子字段表现画**（`SCHEMA.cards.conditions.fields`，顺序就是
 *   服务端写的顺序，也就是 CSS 里那几列的顺序）。
 * ★★ 用不上的格子画成**空白占位**（`.cond-gap`）而不是整个不画 ——
 *   分不到武器的指标没有「武器」、枚举指标和累计档没有「比较」，
 *   但那一列的位置得留着：不留的话换个指标整行都会横着跳一段。 */
function cardCondRow(cond, at, list) {
  var limits = cardLimits();
  var info = cardMetricInfo(cond.metric);
  var row = el("div", "card-cond");
  row.setAttribute("data-at", at);        // 「改过了」那一圈按它找行
  var gap = function () { row.appendChild(el("div", "cond-gap")); };
  (cardSpec("conditions").fields || []).forEach(function (spec) {
    if (spec.key === "join") {
      // 第一条没有连接词（服务端也拦着不许写）—— 空着，别摆个「连接」
      // 在那儿，那看着像一个值。
      if (at === 0) { return gap(); }
      if (cond.join === undefined) { cond.join = limits.join_and; }
    }
    if (spec.key === "weapon" && !info.weapon) { return gap(); }
    // ★ 「比较」那一格**永远画出来**（用户 2026-09-13 第五轮）：累计档和
    //   「本局结果」各自只有一种说法，但空着一格看上去像是坏了 ——
    //   改成**下拉里只剩那一项**（`cardOpOptions`），一眼就知道没得选。
    if (spec.key === "op") { return row.appendChild(
      cardCondField(cardOpOptions(cond, info), cond)); }
    row.appendChild(spec.key === "threshold"
      ? cardCondValueField(cond, info)
      : cardCondField(spec.key === "metric" ? cardMetricOptions(cond) : spec,
                      cond));
  });

  var kill = el("button", "kill", "✕");
  if (list.length <= 1) {
    // 只剩一条时不给删 —— 删光了这条规则就说不出「什么时候给」。
    // ★ 用 `aria-disabled` 不用 `disabled`：浏览器不给 disabled 的钮派发
    //   鼠标事件，那句 `title` 提示根本弹不出来（D97i 踩过）。
    kill.setAttribute("aria-disabled", "true");
    kill.title = "至少要留一条";
  } else {
    kill.title = "删掉这一条";
    kill.onclick = function () {
      list.splice(at, 1);
      // 删掉的要是第一条，新的第一条不该再带连接词。
      if (list.length) { delete list[0].join; }
      renderCardCond();
    };
  }
  row.appendChild(kill);
  return row;
}

/** 条件行里的一格（下拉）。改完把这条条件收拾干净、整块重画。 */
function cardCondField(spec, cond) {
  // ★ 改之**前**那个指标是什么，`cardCondFix` 要用它（见那边的注释）。
  var was = cond.metric;
  var node = fieldNode(spec, cond, paintCardSay);
  var control = node.querySelector ? node.querySelector("select, input") : null;
  if (control) {
    // `choiceNode` 自己的 onchange 先跑（写进 cond），这一发排在后面。
    control.addEventListener("change", function () {
      cardCondFix(cond, was);
      renderCardCond();
    });
  }
  return node;
}

/** 「数值」那一格：枚举指标画成下拉，别的画数字框（带指标自己的单位）。 */
function cardCondValueField(cond, info) {
  var base = cardCondSpec("threshold");
  var values = info.values || [];
  if (values.length) {
    // 枚举指标（「本局结果」）：值只有两个，下拉比数字框说得清。
    return cardCondField({key: "threshold", label: base.label, type: "choice",
                          options: values}, cond);
  }
  var spec = {};
  Object.keys(base).forEach(function (name) { spec[name] = base[name]; });
  // ★ 下限跟着比较符走（服务端是同一张 `CARD_OP_MIN`）：「大于等于 0」
  //   恒成立所以卡在 1，而「小于等于 / 等于」要的正是 0。
  var limits = cardLimits();
  var low = (limits.op_min || {})[cond.op || limits.op_ge];
  spec.min = (low === undefined) ? 1 : low;
  // 单位来自指标表（命中率那个 `%`）—— 句子里和输入框后缀是同一个字。
  if (info.unit) { spec.suffix = info.unit; }
  // ★ 数字框走的是 `oninput`（不是 `change`）⇒ 说明文一边打字一边跟着变。
  return fieldNode(spec, cond, paintCardSay);
}

/** 「统计指标」那一格：**按当前统计范围过滤选项**。
 *
 *  「对局数 / 胜利·通关次数」只有累计档有意义，「本局结果 / 命中率」只有
 *  一局内有意义 —— 让人选得到，配出来的只能是废规则。判据来自服务端的
 *  `scopes`，不是这儿写死的清单。
 *
 *  ★★ 多一个**空选项**（用户 2026-09-13 第五轮）：统计范围一改，原来那个
 *  指标可能就不在表里了 —— 这时这一格空着等人重选，顶上写「条件无效」。
 *  第一版是把统计范围**弹回去**，改都改不动，人只会以为下拉坏了。 */
function cardMetricOptions(cond) {
  var spec = cardCondSpec("metric");
  var scope = cond.scope || cardLimits().scope_match;
  var copy = {};
  Object.keys(spec).forEach(function (name) { copy[name] = spec[name]; });
  copy.optional = true;
  copy.empty_label = "请选择";
  copy.options = (spec.options || []).filter(function (option) {
    var scopes = cardMetricInfo(option.value).scopes;
    return !scopes || !scopes.length || scopes.indexOf(scope) >= 0;
  });
  return copy;
}

/** 「比较」那一格：**按指标和统计范围过滤选项**。
 *
 *  累计档只有「大于等于」（它念成「每满 N」），枚举指标只有「等于」。
 *  ★ 格子照画、只是下拉里剩一项 —— 空着一格看上去像是坏了。 */
function cardOpOptions(cond, info) {
  var limits = cardLimits();
  var spec = cardCondSpec("op");
  var only = null;
  if ((info.values || []).length) { only = limits.op_eq; }
  else if (cond.scope === limits.scope_total) { only = limits.op_ge; }
  var copy = {};
  Object.keys(spec).forEach(function (name) { copy[name] = spec[name]; });
  if (only !== null) {
    copy.options = (spec.options || []).filter(function (option) {
      return option.value === only;
    });
  }
  return copy;
}

/* ------------------------------------- 达成进度弹窗（用户 2026-09-13 第四轮）

   两个入口、**同一份内容**：
     · 「玩家仓库」每一行那颗「卡片进度」—— 看那个玩家的（★系统管理员）；
     · 「称号卡片掉落」工具条上那颗「查看本人达成进度」—— 只看自己的。

   ★★ 「只能看自己」这件事**不靠前端**：那颗钮压根不发 `name`，而服务端
     `_admin_card_progress` 里写死了「写了别人的名字才要系统管理员」。
   ★ 进度**只有「玩家累计」那种条件才有**：一局内的攒不住（打完就清），
     那一格写的是「每局游戏内计算」，不画一根永远停在 0 的进度条。 */

//: 正开着的那一份：`{username, mine, rows, filter}`。
var CARD_PROG = null;

//: 统计范围那个筛选。★ 口径是「**含**」：一张卡片里只要有一条那一档的条件
//  就列出来，而且**两种条件都照画** —— 用户点名要的（「累计条件和局内条件
//  都显示」）。判据用的是服务端发下来的 `scope`，不是这儿另立的名目。
var CARD_PROG_SCOPES = [
  {id: "all", label: "全部"},
  {id: "match", label: "含局内条件"},
  {id: "total", label: "含累计条件"},
];

async function openCardProgress(username) {
  var result = await api("/admin/api/cards/progress"
    + (username ? ("?name=" + encodeURIComponent(username)) : ""));
  if (bounced(result)) { return; }
  if (!result.ok) { toast(result.message, false); return; }
  CARD_PROG = {username: result.username, mine: !!result.mine,
               nickname: result.nickname || "", rows: result.rows || [],
               filter: {group: CARD_GROUPS[0].id, scope: "all"}};
  renderCardProgress();
  $("cardProgressModal").classList.remove("hidden");
}

function closeCardProgress() {
  CARD_PROG = null;
  $("cardProgressModal").classList.add("hidden");
}

/** 这张卡片过不过当前那两个筛选。 */
function cardProgMatches(row) {
  var filter = CARD_PROG.filter;
  if (filter.group !== "all" && cardGroupOf(row.card) !== filter.group) {
    return false;
  }
  if (filter.scope === "all") { return true; }
  return (row.conditions || []).some(function (cond) {
    return cond.scope === filter.scope;
  });
}

function renderCardProgress() {
  if (!CARD_PROG) { return; }
  // 标题里写清**这是谁的** —— 两个入口长一个样，不写就分不出看的是谁。
  var who = CARD_PROG.username
    + (CARD_PROG.nickname ? ("（" + CARD_PROG.nickname + "）") : "");
  $("cardProgressTitle").textContent =
    (CARD_PROG.mine ? "我的卡片进度" : "卡片进度") + "　—— " + who;

  var bar = $("cardProgressBar");
  bar.textContent = "";
  // 种类切换复用掉落页那一组（`CARD_GROUPS`）—— 同一件事只有一份说法。
  [[CARD_GROUPS, "group"], [CARD_PROG_SCOPES, "scope"]].forEach(function (pair) {
    var seg = el("span", "seg");
    pair[0].forEach(function (item) {
      var on = CARD_PROG.filter[pair[1]] === item.id;
      var button = el("button", "cat" + (on ? " on" : ""), item.label);
      button.appendChild(el("span", "n", String(
        CARD_PROG.rows.filter(function (row) {
          if (pair[1] === "group") {
            return item.id === "all" || cardGroupOf(row.card) === item.id;
          }
          return item.id === "all" || (row.conditions || []).some(
            function (cond) { return cond.scope === item.id; });
        }).length)));
      button.onclick = function () {
        CARD_PROG.filter[pair[1]] = item.id;
        renderCardProgress();
      };
      seg.appendChild(button);
    });
    bar.appendChild(seg);
  });

  var host = $("cardProgressBody");
  host.textContent = "";
  var shown = CARD_PROG.rows.filter(cardProgMatches);
  if (!shown.length) {
    host.appendChild(el("div", "list-empty", "没有符合筛选条件的卡片"));
    return;
  }
  shown.forEach(function (row) { host.appendChild(cardProgRow(row)); });
}

function cardProgRow(row) {
  var box = el("div", "card-prog" + (row.listed ? "" : " off"));
  var who = tipFor(el("div", "who"), row.card);
  who.appendChild(slotNode(row.card, 36, !!row.listed, false));
  var name = el("div", "nmz", itemName(row.card));
  name.appendChild(el("span", "got" + (row.granted ? "" : " none"),
                      "已获得 " + row.granted + " 张"));
  who.appendChild(name);
  box.appendChild(who);

  var list = el("div", "card-prog-conds");
  if (!row.listed) {
    // 关着的卡片没有「还差多少」可言 —— 说清是「拿不到」，不是「差 0」。
    list.appendChild(el("div", "card-prog-none", row.text));
  } else if (!(row.conditions || []).length) {
    list.appendChild(el("div", "card-prog-none", row.text));
  } else {
    row.conditions.forEach(function (cond) {
      list.appendChild(cardProgCond(cond));
    });
  }
  box.appendChild(list);
  return box;
}

function cardProgCond(cond) {
  var line = el("div", "card-prog-cond");
  line.appendChild(el("div", "txt", cond.text));
  if (cond.need === undefined) {
    // 一局内：攒不住，进度这回事对它不成立。
    line.appendChild(el("div", "per-match", "每局游戏内计算"));
    return line;
  }
  var have = Math.max(0, Number(cond.have) || 0);
  var need = Math.max(1, Number(cond.need) || 1);
  line.appendChild(el("div", "num", have + " / " + need));
  var full = have >= need;
  var bar = el("div", "card-prog-bar" + (full ? " full" : ""));
  var fill = el("i");
  fill.style.width = Math.min(100, Math.round(have * 100 / need)) + "%";
  bar.appendChild(fill);
  // 攒满了但还没结算 —— 说一声，免得有人以为卡住了。
  bar.title = full ? "已经攒够，下一局结算时发" : (have + " / " + need);
  line.appendChild(bar);
  return line;
}

/** 改完一格之后，把这条条件里**跟着失效**的几格收拾干净。
 *
 * ★ 和服务端的护栏是同一套（`_validate_card_conditions`）：留着一个
 *   「写了但不生效」的值就是 D17a 那一类 —— 保存时被拒，而人看不出为什么。
 */
function cardCondFix(cond, wasMetric) {
  var limits = cardLimits();
  // ★★ **统计范围永远改得动**（用户 2026-09-13 第五轮）：改完之后原来那个
  //   指标要是不在新范围里，就**把指标清掉**等人重选（顶上写「条件无效」）。
  //   第一版是反过来的 —— 把范围弹回指标那一档，于是那个下拉「怎么点都
  //   不动、还一声不吭」，谁都会以为它坏了。
  var scope = cond.scope || limits.scope_match;
  var wants = (cardMetricInfo(cond.metric).scopes) || [];
  if (cond.metric !== undefined && wants.length && wants.indexOf(scope) < 0) {
    delete cond.metric;
  }
  var info = cardMetricInfo(cond.metric);
  // ★★ **跨过「枚举 ↔ 数字」那条界**时，比较符和数值一起重来：
  //   枚举指标（「本局结果」）会把比较符钉成「等于」、数值钉成 0/1。
  //   从它切走之后那两格留着就成了「开枪次数 等于 1」—— 看着是个正常
  //   条件，其实是上一个指标的残渣，而且把「命中率要配开枪数下限」那道
  //   护栏顶得人莫名其妙（实测踩到）。
  var wasEnum = ((cardMetricInfo(wasMetric).values || []).length > 0);
  var nowEnum = ((info.values || []).length > 0);
  if (wasMetric !== undefined && wasMetric !== cond.metric
      && wasEnum !== nowEnum) {
    delete cond.op;
    delete cond.threshold;
  }
  // ★ 反方向（选了个只在另一档有意义的指标）不会发生：指标下拉本来就
  //   **按当前范围过滤**（`cardMetricOptions`），选不到不合适的那些。
  if (!info.weapon) { delete cond.weapon; }
  var values = info.values || [];
  if (values.length) {
    // 枚举指标：比较符固定「等于」，值必须是表里那两个之一。
    cond.op = limits.op_eq;
    if (!values.some(function (o) { return o.value === cond.threshold; })) {
      cond.threshold = values[0].value;
    }
    return;
  }
  if (cond.scope === limits.scope_total || cond.op === undefined) {
    // 累计那一档只有「每满 N」。
    // ★ 缺这一格时也**显式写回默认值**，别指望「下拉显示着大于等于」就
    //   等于存着 `ge`：`choiceNode` 在值缺失时显示的是第一项，而条件里
    //   什么都没有 ——「看到的」和「要存的」对不上是最难查的一种错。
    cond.op = limits.op_ge;
  }
  // 按武器统计时比较符只能是「大于等于」（别的方向对没用过那把枪的人恒成立）。
  if (cond.op !== limits.op_ge) { delete cond.weapon; }
  // 阈值下限跟着比较符走，别让「大于等于 0」这种恒成立的留在画面上。
  var low = (limits.op_min || {})[cond.op];
  if (low === undefined) { low = 1; }
  if (typeof cond.threshold !== "number" || cond.threshold < low) {
    cond.threshold = low;
  }
}

/* -------------------------------------------- 金币 / 经验获取：两张表格
   用户 2026-09-10 点的题（D72）：
     · 第一张 = 对战模式（生存 / 夺分 × 道具战）× 个人战 / 组队战，格子里
       填输 / 赢各给多少；
     · 第二张 = 闯关的 7 个关卡 × 简单 / 普通 / 困难，格子里填未通关 / 通关。
   顶上「金币 / 经验」一切换，同两张表换填另一对字段 —— 行列不变，
   人不用重新找位置。经验那一半下面多两个加成系数的输入框。

   ★ 行列标题**一个都不在这儿写死**：对战模式名 / 队伍名 / 关卡名 / 难度名
     全部来自 `SCHEMA.rewards` 里那几个 `options`（服务端的
     `shopcfg.PVP_MODE_ZH` / `TEAM_ZH` / `QUEST_ZH` / `DIFFICULTY_ZH`），
     输 / 赢两列的字来自 `SCHEMA.rewards.outcomes`。以后加一关、改一个译名，
     这一页跟着变，不用两头改。 */

/** 当前看的是金币还是经验。 */
function rewardView() {
  return (FILTER.rewards || {}).view || REWARD_VIEWS[0].id;
}

/** 工具条最左边那个「金币 / 经验」分段控件（D72b）。
 *
 *  ★ 复用分类标签那套 `button.cat` 的样子 —— 它在这一页里干的是同一件事
 *    （「现在看哪一档」），换个地方摆而已，没必要另造一种控件。
 */
function rewardViewSwitch() {
  var seg = el("span", "seg");
  REWARD_VIEWS.forEach(function (view) {
    var button = el("button", "cat" + (rewardView() === view.id ? " on" : ""),
                    view.label);
    button.title = "两张表的行列一样，切的是格子里填哪一对数";
    button.onclick = function () {
      FILTER.rewards.view = view.id;
      // ★ 整条工具条重画一遍 —— 选中态在按钮自己身上（`.on`），
      //   只重画列表的话左边这两个钮不会跟着变。
      renderToolbar("rewards");
      repaintList();
    };
    seg.appendChild(button);
  });
  return seg;
}

/** `SCHEMA.rewards` 里某个字段的描述（下拉选项从这儿取，不另抄一份）。 */
function rewardSpec(key) {
  var found = null;
  (((CAT.schema || {}).rewards || {}).fields || []).forEach(function (spec) {
    if (spec.key === key) { found = spec; }
  });
  return found || {key: key, label: key, type: "int", options: []};
}

function rewardOptions(key) {
  return rewardSpec(key).options || [];
}

/** 表格里的一个数字格。
 *
 * ★ 校验和红框走 `fieldNode` **同一段代码**，只是把 `.field` 外壳和标签丢掉
 *   —— 表头上已经写着这一格是什么了。`optional` 也去掉：在这张表里每一格
 *   都得有个数，留空不是「不限」，是填错了（红框 + 服务端拒收）。
 */
function rewardCell(entry, key) {
  var spec = rewardSpec(key);
  var cell = el("td", null);
  if (!entry) {
    // 理论上到不了（`fillRewards` 会把缺的补齐），但真到了要看得出来。
    cell.appendChild(el("span", "ro", "—"));
    return cell;
  }
  var node = fieldNode({key: key, label: spec.label, type: "int",
                        min: spec.min, max: spec.max}, entry, touched);
  var input = node.querySelector("input");
  input.title = spec.label;
  cell.appendChild(input);
  // ★ 服务端拒收时回的是「rules[12].win_money 不能小于 0」——
  //   `markBadCard` 照 `data-index` 找那一格并闪一下。表格里一行有好几条
  //   记录（个人战 / 组队战各一条），所以这个属性挂在**格子**上，不是行上。
  cell.setAttribute("data-index", CFG.rewards.entries.indexOf(entry));
  return cell;
}

/** 两层表头 + 一批数据行 = 一张表。
 *
 *  `groups` = [{label, key}]（上一行的分组，每组两列）；
 *  `rows`   = [{label, entryOf(groupKey)}]。
 */
function rewardTable(title, headLabel, groups, outcomes, rows) {
  var view = REWARD_VIEWS.filter(function (v) {
    return v.id === rewardView();
  })[0] || REWARD_VIEWS[0];

  var block = el("div", "reward-block");
  block.appendChild(el("h3", null, title));
  var table = el("table", "reward-table");

  var head = el("thead");
  var top = el("tr");
  var corner = el("th", "rowhead", headLabel);
  corner.rowSpan = 2;
  top.appendChild(corner);
  var sub = el("tr");
  groups.forEach(function (group) {
    var cell = el("th", "grp", group.label);
    cell.colSpan = 2;
    top.appendChild(cell);
    // 输在前、赢在后 —— 和用户写的顺序一致。
    sub.appendChild(el("th", "grp", outcomes.lose));
    sub.appendChild(el("th", null, outcomes.win));
  });
  head.appendChild(top);
  head.appendChild(sub);
  table.appendChild(head);

  var body = el("tbody");
  rows.forEach(function (row) {
    var tr = el("tr");
    tr.appendChild(el("th", "rowhead", row.label));
    groups.forEach(function (group) {
      var entry = row.entryOf(group);
      var lose = rewardCell(entry, view.lose);
      lose.className = "grp";
      tr.appendChild(lose);
      tr.appendChild(rewardCell(entry, view.win));
    });
    body.appendChild(tr);
  });
  table.appendChild(body);

  // 宽表在自己身上横滚，别把整页撑出横条（版面约束和别的页一致）。
  var scroller = el("div", "reward-scroll");
  scroller.appendChild(table);
  block.appendChild(scroller);
  return block;
}

/** 经验那一半下面的加成系数（`mode:"bonus"` 那一条记录）。 */
function rewardBonusField(bonus, key) {
  var spec = rewardSpec(key);
  var wrap = el("div", "reward-bonus");
  if (!bonus) {
    wrap.appendChild(el("span", "hint", "（配置里没有加成系数那一条）"));
    return wrap;
  }
  wrap.setAttribute("data-index", CFG.rewards.entries.indexOf(bonus));
  wrap.appendChild(fieldNode({key: key, label: spec.label, type: "int",
                              min: spec.min, max: spec.max, help: spec.help},
                             bonus, touched));
  return wrap;
}

//: 等级曲线那张参照表分几栏并排（用户 2026-09-10，D72a）。
//  60 级拉成一列要滚半天；三栏 20 行既看得全，宽度也还在 1320 里。
var LEVEL_COLUMNS = 3;

//: 「等级经验对应表」弹窗开着没有。★ Esc 要知道该关谁。
var LEVEL_MODAL = false;

/** 打开「等级与经验对应表」弹窗（D72b）。表现画现丢，页面里不留静态副本。 */
function openLevelModal() {
  var body = $("levelBody");
  body.textContent = "";
  var block = levelCurveBlock();
  body.appendChild(block || el("div", "list-empty", "服务端没有给等级曲线"));
  $("levelModal").classList.remove("hidden");
  LEVEL_MODAL = true;
}

function closeLevelModal() {
  $("levelModal").classList.add("hidden");
  $("levelBody").textContent = "";
  LEVEL_MODAL = false;
}

/** 「等级与经验」参照表 —— **只读**，曲线写在服务端的 `account_store` 里。
 *
 *  ★ 2026-09-10 第二轮起它住在**弹窗**里（D72b），不再摆在经验那一页最下面：
 *    60 级的表比上面两张要改的表还高，天天看着碍事，要看时点开就行。
 *  数全部来自 `/admin/api/catalog` 的 `level_curve`，
 *  **页面不自己套公式**（算重了迟早和服务端对不上）。
 */
function levelCurveBlock() {
  var rows = CAT.level_curve || [];
  if (!rows.length) { return null; }
  var per = Math.ceil(rows.length / LEVEL_COLUMNS);
  var block = el("div", "reward-block");
  // 标题不写在这儿 —— 弹窗自己的标题栏已经写了「等级与经验对应表」。
  block.appendChild(el(
    "p", "hint reward-note",
    "升到下一级要挣「100 × 当前等级」点经验，满 " + rows.length + " 级封顶。"
    + "这条曲线写在服务端里、管理页改不了 —— 放在这儿是为了对着上面那两张表"
    + "换算：一局给这么多经验，打几局升一级。"));

  var table = el("table", "reward-table level-table");
  var head = el("thead");
  var headRow = el("tr");
  var column;
  for (column = 0; column < LEVEL_COLUMNS; column += 1) {
    headRow.appendChild(el("th", column ? "grp" : null, "等级"));
    headRow.appendChild(el("th", null, "升到下一级"));
    headRow.appendChild(el("th", null, "累计总经验"));
  }
  head.appendChild(headRow);
  table.appendChild(head);

  var body = el("tbody");
  for (var line = 0; line < per; line += 1) {
    var tr = el("tr");
    for (column = 0; column < LEVEL_COLUMNS; column += 1) {
      var row = rows[line + column * per];
      var edge = column ? " grp" : "";
      if (!row) {
        // 级数不能被栏数整除时最后一栏会短一截，补空格子把表撑方正。
        tr.appendChild(el("td", "lv" + edge, ""));
        tr.appendChild(el("td", null, ""));
        tr.appendChild(el("td", null, ""));
        continue;
      }
      tr.appendChild(el("th", "rowhead lv" + edge, row.level));
      tr.appendChild(el("td", null,
                        row.need === null ? "满级" : String(row.need)));
      tr.appendChild(el("td", null, String(row.total)));
    }
    body.appendChild(tr);
  }
  table.appendChild(body);

  var scroller = el("div", "reward-scroll");
  scroller.appendChild(table);
  block.appendChild(scroller);
  return block;
}

function renderRewards(list, entries) {
  var byKey = {};
  entries.forEach(function (entry) { byKey[rewardKey(entry)] = entry; });
  var outcomes = ((CAT.schema.rewards || {}).outcomes) || {};
  var isExp = (rewardView() === "exp");

  // ---- 对战：行 = 模式 ×（有没有道具战），列 = 个人战 / 组队战 ----
  var teams = rewardOptions("team").map(function (option) {
    return {label: option.label, key: option.value};
  });
  var pvpRows = [];
  // ★ 行序照用户写的：生存 / 夺分 / 生存道具战 / 夺分道具战
  //   —— 先按「有没有道具」分两批，每批里按模式排。
  [false, true].forEach(function (itemMode) {
    rewardOptions("pvp_mode").forEach(function (mode) {
      pvpRows.push({
        label: mode.label + (itemMode ? "道具战" : ""),
        entryOf: function (group) {
          return byKey[["pvp", mode.value, itemMode ? 1 : 0,
                        Number(group.key) || 0].join("|")];
        }
      });
    });
  });
  list.appendChild(rewardTable("对战模式", "对战模式", teams,
                               outcomes.pvp || {win: "赢", lose: "输"},
                               pvpRows));
  if (isExp) {
    list.appendChild(rewardBonusField(byKey.bonus, "pvp_exp_per_kill"));
  }

  // ---- 闯关：行 = 关卡，列 = 难度 ----
  var difficulties = rewardOptions("difficulty").map(function (option) {
    return {label: option.label, key: option.value};
  });
  var questRows = rewardOptions("stage").map(function (stage) {
    return {
      label: stage.label,
      entryOf: function (group) {
        return byKey[["quest", stage.value, group.key].join("|")];
      }
    };
  });
  list.appendChild(rewardTable("闯关模式", "关卡", difficulties,
                               outcomes.quest || {win: "通关", lose: "未通关"},
                               questRows));
  if (isExp) {
    list.appendChild(rewardBonusField(byKey.bonus, "quest_score_per_exp"));
  } else {
    // 切到金币时那两个框会消失 —— 说一句为什么，别让人以为是画丢了。
    list.appendChild(el("p", "hint reward-note",
                        "金币不吃分数和杀敌数，所以只有「经验」那一半有加成系数。"
                        + "实际到账还要加上本局在地上捡到的金币。"));
  }
}

/* ======================================================================
   物品选择器
   ====================================================================== */

var PICKER = null;

/** 打开选择器。
 *
 *  单选（配置页「添加」）：`{kinds, selected, onPick(item)}`，点一格就选中并关闭。
 *  批量（玩家仓库弹窗「添加物品」，用户 2026-09-07）：`{multi: true, owned,
 *  onPickMany(items)}` —— 格子点了打勾、再点取消，底下「确认添加」一次全给；
 *  `owned` 里的画成「已有」、点不动。
 */
function openPicker(options) {
  PICKER = {
    kinds: options.kinds || null,
    selected: options.selected,
    onPick: options.onPick,
    multi: !!options.multi,
    onPickMany: options.onPickMany,
    owned: options.owned || {},
    chosen: {},
    // ★ 筛选每次打开都从头起（搜索串空、两个下拉「全部」、分类「全部」），
    //   不跨次记忆 —— 上一次筛剩三件，这一次打开又是空网格最难查。
    q: "",
    character: "",
    listing: "",
    big: -1,
    sub: null,
    tabs: pickerNeedsTabs(options.kinds)
  };
  $("pickSearch").value = "";
  // 角色 / 上架状态两个下拉：和配置页、「修改仓库」弹窗同一份选项（D68）。
  fillSelect($("pickCharacter"), "全部角色", characterOptions(), "");
  fillSelect($("pickListing"), "全部上架状态", LISTING_FILTER_OPTIONS, "");
  $("pickFoot").classList.toggle("hidden", !PICKER.multi);
  $("picker").classList.remove("hidden");
  paintPicker();
  $("pickSearch").focus();
}

/** 受限的选择器（`kinds`，现在只有「只挑材料」一种）要不要画分类标签：
 *  候选落在**不止一个大分类**里才画。材料全在「收集品 → 材料」一格，两行标签
 *  只剩一个能点的格子，纯占地方（而且这个弹窗常常只是给一格材料换个东西，
 *  越短越好）。不受限的照画。 */
function pickerNeedsTabs(kinds) {
  if (!kinds) { return true; }
  var groups = {};
  CAT.items.forEach(function (item) {
    if (kinds.indexOf(item.kind) >= 0) { groups[whCategory(item.id) >>> 16] = true; }
  });
  return Object.keys(groups).length > 1;
}

/** 批量模式底下那条：已选几件、「确认添加」能不能点。 */
function paintPickFoot() {
  if (!PICKER || !PICKER.multi) { return; }
  var count = Object.keys(PICKER.chosen).length;
  $("pickChosen").textContent = "已选 " + count + " 件";
  $("pickConfirm").disabled = !count;
}

function closePicker() {
  PICKER = null;
  $("picker").classList.add("hidden");
}

/** 弹窗那两行分类标签（D68）。受限的选择器不画（见 `pickerNeedsTabs`）。 */
function paintPickTabs(candidates) {
  var bigHost = $("pickCats");
  var subHost = $("pickSubCats");
  bigHost.classList.toggle("hidden", !PICKER.tabs);
  if (!PICKER.tabs) {
    subHost.classList.add("hidden");
    return;
  }
  paintCatTabs(bigHost, subHost, PICKER, function (id) {
    return candidates.filter(function (item) {
      return whMatches(id, whCategory(item.id));
    }).length;
  }, paintPicker);
}

/** 画网格。★ 换页栏没有了（D68，原来 200 格一页）：筛完剩多少画多少，
 *  只有网格自己滚（`#pickGrid`）；重画不动滚动条（D37b）。 */
function paintPicker() {
  var grid = $("pickGrid");
  var keep = grid.scrollTop;
  grid.textContent = "";
  var query = PICKER.q.toLowerCase();
  // 先过筛选条（受限的种类 / 两个下拉 / 搜索串），标签上的件数从这儿数；
  // 再按当前分类标签收一遍才是画出来的那批（和配置页 `repaintList` 一个套路）。
  var candidates = CAT.items.filter(function (item) {
    if (PICKER.kinds && PICKER.kinds.indexOf(item.kind) < 0) { return false; }
    // 角色 / 上架状态和配置页、「修改仓库」弹窗同一条判据（`dropdownsMatch`）。
    if (!dropdownsMatch(item.id, PICKER)) { return false; }
    if (!query) { return true; }
    // 中文名按**物品库**里那一份搜（D31）—— 在物品库里改过名字之后，
    // 用新名字搜不到才叫奇怪。
    return (itemName(item.id) + " " + (item.name_kr || "") + " " + item.id)
      .toLowerCase().indexOf(query) >= 0;
  });
  paintPickTabs(candidates);
  var requested = tabRequested(PICKER);
  var hits = candidates.filter(function (item) {
    return whMatches(requested, whCategory(item.id));
  });
  if (!hits.length) {
    grid.appendChild(el("div", "pick-empty", "没有匹配的物品"));
  }
  hits.forEach(function (item) {
    var owned = PICKER.multi && PICKER.owned[item.id];
    var chosen = PICKER.multi && PICKER.chosen[item.id];
    var cell = el("div", "pick" + (item.id === PICKER.selected ? " sel" : "")
                         + (owned ? " owned" : "") + (chosen ? " chosen" : ""));
    var ic = el("div", "ic");
    if (iconStyle(ic, item.cell, 44)) { cell.appendChild(ic); }
    else { cell.appendChild(el("div", "noicon", "?")); }
    cell.appendChild(el("div", "nmz", itemName(item.id)));
    if (owned) { cell.appendChild(el("span", "have", "已有")); }
    if (chosen) { cell.appendChild(el("span", "chk", "✓")); }
    // ★ 详情**只走浮窗**（`tipFor`，用户 2026-09-06）：弹窗底下原来还有一条
    //   侧栏，写的是同一批东西，而且「在不在卖」是现问服务端的 —— 管理员
    //   刚在货架上改完还没保存时，那一行说的是磁盘上那份旧的，和浮窗打架。
    tipFor(cell, item.id);
    cell.onclick = function () {
      if (PICKER.multi) {
        if (owned) { return; }             // 已经在仓库里，勾了也没意义
        // 只翻这一格，不重画整张网格 —— 滚动位置和浮窗都别动（D37b）。
        if (PICKER.chosen[item.id]) {
          delete PICKER.chosen[item.id];
          cell.classList.remove("chosen");
          var mark = cell.querySelector(".chk");
          if (mark) { mark.remove(); }
        } else {
          PICKER.chosen[item.id] = true;
          cell.classList.add("chosen");
          cell.appendChild(el("span", "chk", "✓"));
        }
        paintPickFoot();
        return;
      }
      var pick = PICKER.onPick;
      closePicker();
      pick(item);
    };
    grid.appendChild(cell);
  });
  // 标题栏只写件数（用户 2026-09-07）—— 是当前分类下筛出来的那个数。
  $("pickCount").textContent = hits.length + " 件";
  paintPickFoot();
  grid.scrollTop = keep;
}

/* ======================================================================
   管理员账号
   ====================================================================== */

var ROLE_ZH = {system: "系统管理员", operator: "运营"};

/** 密码输两遍，不一致就**一个字节都不发**（用户 2026-09-13）。
 *
 * ★ 为什么值得单独拦一道：管理员密码是**明文**存的（D3），而这一页是唯一
 *   能改它的地方 —— 添加时手滑打错，那个账号从生下来就登不进去；改密码时
 *   手滑打错，人当场被踢下线（服务端会作废他全部会话）还不知道密码是什么。
 *   两种都只能上服务器改 JSON 才救得回来。
 * ★ 这是**防手滑，不是安全边界** ⇒ 服务端接口签名一个字没动（还是只收一个
 *   `password`）。把它当门禁的话，绕过前台就等于没拦。
 * ★ 空密码不在这儿管 —— 那是服务端的密码规则（`__PASSWORD_RULE__`）的事，
 *   两边各管各的，重复一份迟早对不上。
 */
function passwordsMatch(firstId, secondId) {
  if ($(firstId).value === $(secondId).value) { return true; }
  toast("两次输入的密码不一致，请重新输入。", false);
  // 焦点丢回第二个框：要重打的是它，不是上面那个。
  $(secondId).focus();
  $(secondId).select();
  return false;
}

/* ---------------------------------------------------------------------
   「手动添加管理员」/「修改密码」两个弹窗（用户 2026-09-13 第二轮）

   ★ 两张表单原来摆在页面右栏。收进弹窗之后页面上只剩一张名单，
     三个入口并排在工具条上。
   ★ 开窗时**一律清空**上一次填的东西 —— 密码框留着上一个人的值太危险
     （下一次点开可能是给另一个人改）。
   --------------------------------------------------------------------- */
var ADD_ADMIN_OPEN = false;
var SET_PW_OPEN = false;

function clearFields(ids) {
  ids.forEach(function (id) { $(id).value = ""; });
}

function openAddAdmin() {
  ADD_ADMIN_OPEN = true;
  clearFields(["newAdminName", "newAdminPass", "newAdminPass2"]);
  $("newAdminRole").value = "operator";   // 加人先给最小权限（D34）
  $("addAdminModal").classList.remove("hidden");
  $("newAdminName").focus();
}

function closeAddAdmin() {
  ADD_ADMIN_OPEN = false;
  $("addAdminModal").classList.add("hidden");
}

/** `name` 给了就把「改谁」预填好（从名单那一行点进来时用）。 */
function openSetPw(name) {
  SET_PW_OPEN = true;
  clearFields(["pwName", "pwValue", "pwValue2"]);
  if (name) { $("pwName").value = name; }
  $("setPwModal").classList.remove("hidden");
  $(name ? "pwValue" : "pwName").focus();
}

function closeSetPw() {
  SET_PW_OPEN = false;
  $("setPwModal").classList.add("hidden");
}

//: 玩家列表上那个灰钮写什么（D40）。★ 和 `ROLE_ZH` **故意不一样**：
//  那张表用在「管理员账号」页的下拉框里，那儿上下文已经写着「权限」了；
//  玩家列表上只有一个钮，光写「运营」看不出这是管理页的权限。
var ADMIN_BADGE_ZH = {system: "系统管理员", operator: "管理员（运营）"};

//: 管理员名单的分页（用户 2026-09-13 第二轮）。★ 和玩家列表不同，这一份
//  **在前台分页**：`/admin/api/admins` 本来就一次回整张表（管理员统共几个人，
//  不值得为它加一套服务端分页），而且「加完人当场看到他」要的就是整张表。
var ADMIN_LIST = [];
var ADMIN_PAGE = {page: 0, pages: 1, total: 0, size: 10};

function renderAdmins(admins) {
  if (admins) { ADMIN_LIST = admins; }
  var size = ADMIN_PAGE.size;
  var total = ADMIN_LIST.length;
  var pages = Math.max(1, Math.ceil(total / size));
  // 删到最后一页空了（或者换了一份更短的名单）就退回最后一页，别留一张空表。
  var page = Math.min(ADMIN_PAGE.page, pages - 1);
  ADMIN_PAGE = {page: page, pages: pages, total: total, size: size};
  $("adminCount").textContent = total + " 个管理员";
  var rows = $("adminRows");
  rows.textContent = "";
  ADMIN_LIST.slice(page * size, page * size + size).forEach(function (row) {
    var name = row.name;
    var tr = document.createElement("tr");
    tr.appendChild(el("td", null, name));

    // 昵称 = 同名玩家账号的昵称（用户 2026-09-13）。没有同名号 ⇒ 空串 ⇒
    // 画一个 `-`：这一格空着和「昵称读失败了」长得一模一样，写个 `-` 才说
    // 得清「他就是没有游戏账号」。
    tr.appendChild(el("td", row.nickname ? "nick" : "nick none",
                      row.nickname || "-"));

    // 权限下拉：改了当场提交（这一格只有两个值，再加一个「保存」按钮
    // 只会让人忘了按）。服务端会拦「最后一个系统管理员降成运营」。
    var td = el("td");
    var select = document.createElement("select");
    ["system", "operator"].forEach(function (value) {
      var option = el("option", null, ROLE_ZH[value]);
      option.value = value;
      select.appendChild(option);
    });
    select.value = row.role;
    select.onchange = function () { setAdminRole(name, select.value); };
    td.appendChild(select);
    tr.appendChild(td);

    var actions = el("td");
    var acts = el("div", "acts");
    var button = el("button", "btn btn-danger btn-sm", "删除");
    button.onclick = function () { removeAdmin(name); };
    acts.appendChild(button);
    actions.appendChild(acts);
    tr.appendChild(actions);
    rows.appendChild(tr);
  });
  renderAdminPager();
}

function renderAdminPager() {
  // 换页栏和玩家列表共用一份（`pagerInto`）。前台分页 ⇒ 翻页只是重画。
  pagerInto($("adminPager"), ADMIN_PAGE, function (page) {
    ADMIN_PAGE.page = page;
    renderAdmins();
  });
}

async function loadAdmins() {
  var result = await api("/admin/api/admins");
  if (bounced(result) || !result.ok) { return false; }
  renderAdmins(result.admins);
  return true;
}

async function setAdminRole(name, role) {
  var result = await api("/admin/api/admins/role", {name: name, role: role});
  toast(result.message, result.ok);
  // ★ 失败也要重画一次 —— 下拉框已经跳到新值了，不拉回去的话画面上写着
  //   「运营」而服务端还是「系统管理员」。
  if (result.admins) { renderAdmins(result.admins); }
  else { loadAdmins(); }
  if (result.ok && result.self_demoted) {
    // 把自己降成运营 ⇒ 这一页和「玩家仓库」当场就该消失。
    // ★ 顶栏那行字走 `paintWho()`，别在这儿再拼一遍 —— 三档身份的说法
    //   （D74）和昵称怎么摆（2026-09-13）都只该有一个出处。
    ROLE = "operator";
    paintWho();
    applyRoleToTabs();
  }
}

async function removeAdmin(name) {
  if (!(await ask({title: "删除管理员",
                   lead: "确定删除管理员「" + name + "」？",
                   ok: "删除"}))) { return; }
  var result = await api("/admin/api/admins/remove", {name: name});
  if (result.ok && result.logged_out) {
    showLoggedOut("你把自己删掉了，已退出登录。");
    return;
  }
  toast(result.message, result.ok);
  if (result.ok) { renderAdmins(result.admins); }
}

/* ======================================================================
   数据备份（V0.3商店，用户 2026-09-07）

   服务端 `databackup.py` 的门面：设置区（开关 / 时刻 / 保留天数）、
   手动备份、备份列表（每页 10 行，前端分页）、回滚 / 删除。

   ★ 列表是**一次全给**、前端分页：数量被保留天数封顶（7 天也就十几份），
     不值得像玩家列表那样做服务端分页；一次 GET 把 设置 + 状态 + 列表 +
     在线情况 全带回来，正好对应「刷新 = 重取列表 + server.config 的设置」。
   ★ 每个写操作的回执里都带着最新的整份（`_backup_reply`），拿到就整页重画，
     不用再 GET 一次。
   ★ 只有系统管理员看得到这一页（`SYSTEM_ONLY_TABS`），门在服务端。
   ====================================================================== */

var BACKUP = null;            // {settings, status, backups, online, playing, edit}
var BACKUP_PAGE = 0;
//: 一页几行（用户定的 10）。★ 界面取舍，不是铁律 10 那种时序阈值。
var BACKUP_PAGE_SIZE = 10;

/** 把一份 GET / 回执塞进页面模型并整页重画。设置区的编辑值一律换成服务端那份。 */
function adoptBackups(result) {
  var settings = result.settings || {};
  BACKUP = {
    settings: settings, status: result.status || {},
    backups: result.backups || [],
    online: result.online || [], playing: result.playing || [],
    edit: {enabled: !!settings.enabled, time: settings.time || "04:00",
           keep_days: settings.keep_days},
  };
  renderBackupSettings();
  renderBackupStatus();
  renderBackupRows();
}

async function loadBackups() {
  var result = await api("/admin/api/backups");
  if (bounced(result)) { return false; }
  if (!result.ok) { toast(result.message, false); return false; }
  adoptBackups(result);
  return true;
}

function renderBackupSettings() {
  var edit = BACKUP.edit;
  $("backupEnabled").classList.toggle("on", edit.enabled);
  $("backupTime").value = edit.time;
  $("backupKeepDays").value = edit.keep_days;
  backupTouched();
}

function backupSettingsDirty() {
  if (!BACKUP) { return false; }
  var s = BACKUP.settings, e = BACKUP.edit;
  return !!s.enabled !== !!e.enabled || s.time !== e.time
    || Number(s.keep_days) !== Number(e.keep_days);
}

function backupTouched() {
  var dirty = backupSettingsDirty();
  var node = $("backupDirty");
  node.textContent = dirty ? "有未保存的改动" : "";
  node.className = "dirty" + (dirty ? "" : " clean");
  if (!BACKUP) { return; }
  // 改过的那一格套上「改过了」那一圈（用户 2026-09-14）。★ 这三样不是
  // 「一件物品」，标的是**各自那一格**，不是整条工具条。
  var s = BACKUP.settings, e = BACKUP.edit;
  $("backupEnabled").classList.toggle("edited", !!s.enabled !== !!e.enabled);
  $("backupTime").parentNode.classList.toggle("edited", s.time !== e.time);
  $("backupKeepDays").parentNode.classList.toggle(
    "edited", Number(s.keep_days) !== Number(e.keep_days));
}

function renderBackupStatus() {
  var st = BACKUP.status;
  var lines = [];
  if (st.enabled) { lines.push("下次自动备份：" + (st.next_text || "—")); }
  else { lines.push("自动备份已关闭（手动备份和按天清理照常）"); }
  if (st.last_auto) {
    lines.push("上次自动备份（本次启动以来）：" + st.last_auto.text
               + (st.last_auto.ok ? "，成功" : "，失败：" + st.last_auto.message));
  } else if (st.newest_auto) {
    lines.push("最近一份自动备份：" + st.newest_auto.text);
  } else {
    lines.push("还没有过自动备份");
  }
  if (st.thread !== "running") {
    lines.push("⚠ 调度线程没在跑（--no-backup，或这个进程不是 app.py 起的）"
               + "，到点不会自动备份；手动备份照常");
  }
  lines.push("备份目录：" + (st.dir || ""));
  $("backupStatus").textContent = lines.join("\n");
}

async function saveBackupSettings() {
  if (!BACKUP) { return; }
  var edit = BACKUP.edit;
  var days = Number(edit.keep_days);
  if (!isFinite(days) || days < 0 || days > 3650 || Math.floor(days) !== days) {
    toast("保留天数要是 0 ~ 3650 的整数（0 = 永不自动删除）", false);
    return;
  }
  if (!/^\d{1,2}:\d{2}$/.test(edit.time || "")) {
    toast("备份时刻要写成 HH:MM（比如 04:00）", false);
    return;
  }
  // 保留天数一改，服务端会**立刻**清一次 —— 先按手上这份列表算出会删几份，
  // 问过再存。用浏览器的时钟估算，和服务端差个几秒无所谓，这是确认不是判据。
  var doomed = [];
  if (days > 0) {
    var deadline = Date.now() / 1000 - days * 86400;
    doomed = BACKUP.backups.filter(function (row) { return row.created_at < deadline; });
  }
  if (doomed.length) {
    var go = await ask({
      title: "保留天数改小了",
      lead: "保存后会立刻删掉 " + doomed.length + " 份早于 " + days + " 天的备份：",
      lists: [{label: "将被删除：", bad: true,
               rows: doomed.map(function (row) {
                 return {label: row.created_text + "　" + row.label};
               })}],
      ok: "保存并删除", danger: true});
    if (!go) { return; }
  }
  toast("保存中……", true);
  var result = await api("/admin/api/backups/settings",
                         {enabled: edit.enabled, time: edit.time, keep_days: days});
  if (bounced(result)) { return; }
  if (!result.ok) { toast(result.message, false); return; }
  adoptBackups(result);
  toast(result.message, true);
}

async function createBackup() {
  var label = $("backupLabel").value.trim();
  toast("备份中……", true);
  var result = await api("/admin/api/backups/create", {label: label});
  if (bounced(result)) { return; }
  if (!result.ok) { toast(result.message, false); return; }
  $("backupLabel").value = "";
  BACKUP_PAGE = 0;              // 新的一份排最前，翻到第一页才看得见
  adoptBackups(result);
  toast(result.message, true);
}

function backupPageCount() {
  var total = BACKUP ? BACKUP.backups.length : 0;
  return Math.max(1, Math.ceil(total / BACKUP_PAGE_SIZE));
}

/** 画当前页。★ 重画一律不动滚动条（D37b）：滚动条长在 `#backupList` 上。 */
function renderBackupRows() {
  var list = $("backupList");
  var keep = list.scrollTop;
  var rows = $("backupRows");
  rows.textContent = "";
  var all = BACKUP.backups;
  var pages = backupPageCount();
  BACKUP_PAGE = Math.min(Math.max(0, BACKUP_PAGE), pages - 1);
  if (!all.length) {
    var tr = document.createElement("tr");
    var td = el("td", "own-empty",
                "还没有备份 —— 点上面的「立即备份」，或者等每天的自动备份。");
    td.colSpan = 5;
    tr.appendChild(td);
    rows.appendChild(tr);
  }
  all.slice(BACKUP_PAGE * BACKUP_PAGE_SIZE, (BACKUP_PAGE + 1) * BACKUP_PAGE_SIZE)
     .forEach(function (row) {
    var line = document.createElement("tr");
    line.appendChild(el("td", "bk-label", row.label));
    var kind = el("td");
    kind.appendChild(el("span", "kind " + row.kind, row.kind_zh));
    line.appendChild(kind);
    line.appendChild(el("td", null, row.created_text));
    var what = el("td", null, row.file_count + " 个文件 · " + row.size_text);
    what.title = (row.files || []).join("\n");
    line.appendChild(what);
    var td = el("td");
    // ★ 两个钮颜色必须不一样（用户 2026-09-07）：金 = 回滚（动作），红 = 删除。
    var acts = el("div", "acts");
    var restore = el("button", "btn btn-sm btn-primary", "回滚到此版本");
    restore.onclick = function () { confirmRestore(row); };
    acts.appendChild(restore);
    var remove = el("button", "btn btn-sm btn-danger", "删除备份");
    remove.onclick = function () { confirmRemove(row); };
    acts.appendChild(remove);
    td.appendChild(acts);
    line.appendChild(td);
    rows.appendChild(line);
  });
  $("backupCount").textContent = all.length ? all.length + " 份备份" : "";
  paintBackupPager(pages);
  list.scrollTop = keep;
}

/** 换页栏。只有列表上面这一条、只有多页才画、换页不动滚动条（D37a）。 */
function paintBackupPager(pages) {
  var host = $("backupPager");
  host.textContent = "";
  if (pages <= 1) { return; }
  function step(text, target, disabled) {
    var button = el("button", "btn btn-sm", text);
    button.disabled = disabled;
    button.onclick = function () {
      var list = $("backupList");
      var keep = list.scrollTop;
      BACKUP_PAGE = target;
      renderBackupRows();
      list.scrollTop = keep;
    };
    host.appendChild(button);
  }
  step("‹ 上一页", BACKUP_PAGE - 1, BACKUP_PAGE <= 0);
  host.appendChild(el("span", "pageno",
                      "第 " + (BACKUP_PAGE + 1) + " / " + pages + " 页　共 "
                      + BACKUP.backups.length + " 份"));
  step("下一页 ›", BACKUP_PAGE + 1, BACKUP_PAGE >= pages - 1);
}

/** 「回滚到此版本」：先重取一次（在线 / 战斗中的人数要最新的，那份备份也可能
 *  刚被别人删了），再弹带复选框的红钮确认框。
 *
 *  复选框是服务端分好的组（`databackup.groups_of`）：运营配置那几份**一个格子**
 *  （用户 2026-09-07：互相关联，不许拆开），玩家存档单独一格、默认不勾。
 */
async function confirmRestore(row) {
  var fresh = await api("/admin/api/backups");
  if (bounced(fresh)) { return; }
  if (!fresh.ok) { toast(fresh.message, false); return; }
  adoptBackups(fresh);
  var latest = null;
  BACKUP.backups.forEach(function (item) { if (item.id === row.id) { latest = item; } });
  if (!latest) {
    toast("这份备份已经不在了（刚被删掉？），列表已经刷新。", false);
    return;
  }
  row = latest;
  var lead = "用「" + row.created_text + " · " + row.label + "」覆盖现在的数据。\n"
           + "回滚前会先把现在的状态自动备份一份（类型「回滚前」），回错了还能回来。";
  var lists = [];
  var dirty = CONFIGS.filter(isDirty);
  if (dirty.length) {
    lists.push({label: "这几页还有没保存的改动，回滚后会被丢掉：", bad: true,
                rows: dirty.map(function (which) {
                  return {label: CAT.schema[which].title};
                })});
  }
  var checks = (row.groups || []).map(function (group) {
    var warn = group.warn || "";
    if (group.key === "accounts" && BACKUP.online.length) {
      warn += (warn ? "　" : "") + "现在有 " + BACKUP.online.length + " 人在线（"
            + BACKUP.online.join("、") + "），他们的进度会一起倒回去；"
            + "备份里没有的账号会被踢下线。";
    }
    return {key: group.key, label: group.label, checked: !!group.checked,
            warn: warn || null, files: group.files || []};
  });
  var picked = await ask({title: "回滚到此版本", lead: lead, lists: lists,
                          checks: checks, ok: "回滚", danger: true});
  if (!picked) { return; }
  var files = [];
  checks.forEach(function (check) {
    if (picked.indexOf(check.key) >= 0) { files = files.concat(check.files); }
  });
  toast("回滚中……", true);
  var result = await api("/admin/api/backups/restore", {id: row.id, files: files});
  if (bounced(result)) { return; }
  if (!result.ok) { toast(result.message, false); return; }
  adoptBackups(result);
  var restored = result.restored || [];
  // ★ 磁盘上的配置换了，页面上那份（连同它的 `base`）必须跟着换 —— 否则下一次
  //   保存做三方合并时，会把回滚掉的内容当成「对方的改动」（D36）。
  if (restored.some(function (name) { return name !== "accounts.json"; })) {
    await refreshConfigs(true);
  }
  if (restored.indexOf("accounts.json") >= 0) {
    await loadAdmins();
    if (PLAYER_LIST.length) { await searchPlayers(); }
    if (PLAYER) { await openPlayer(PLAYER.view.username, true); }
  }
  // 回滚的回执最后说，别被上面那几发「已刷新」盖掉；没全回滚成的留着等人点掉。
  toast(result.message, !(result.failed && result.failed.length));
}

async function confirmRemove(row) {
  var go = await ask({
    title: "删除备份",
    lead: "确定删除「" + row.created_text + " · " + row.label + "」？\n"
        + "删掉就找不回来了（" + row.file_count + " 个文件，" + row.size_text + "）。",
    ok: "删除", danger: true});
  if (!go) { return; }
  var result = await api("/admin/api/backups/remove", {id: row.id});
  if (bounced(result)) { return; }
  if (!result.ok) { toast(result.message, false); return; }
  adoptBackups(result);
  toast(result.message, true);
}

/** 标题栏那个 ↻：重取 列表 + 设置 + 状态。设置区有没保存的改动先问一句
 *  （和 `refreshAccounts` / `refreshConfigs` 同一个口径）；
 *  失败时不用「已刷新」盖掉错误。 */
async function refreshBackups() {
  if (backupSettingsDirty()
      && !(await ask({title: "还有没保存的改动",
                      lead: "备份设置还没保存，刷新会拿 server.config 里那份盖掉，确定？",
                      ok: "刷新"}))) {
    return;
  }
  toast("刷新中……", true);
  if (await loadBackups()) { toast("已刷新：备份列表和设置都是最新的。", true); }
}

/* ======================================================================
   下载日志弹窗（用户 2026-09-17，D131）

   「数据管理」页工具条上「⇩ 下载日志」点开的**只读**弹窗：两个目录（`logs/` /
   `logs_client_crash/`）各一张卡片，数字全部来自 `/admin/api/logs`，页面里不留
   静态副本；下载钮点下去就是一个 `<a href download>`，zip 由服务端**边打包边发**
   （chunked），浏览器自己的下载栏管进度。

   ★ 下载前先重取一次 overview（`loadLogs()`）：既把数字刷成最新的，也顺手当一次
     会话检查 —— 会话过期时 `bounced()` 会把人踢回登录页，而不是让 `<a download>`
     去下一个 401 的 JSON（Chromium 对 4xx 不落盘、只在下载栏里写「失败」，但那句
     「失败」没人看得懂）。
   ★ 不用 `fetch → blob → createObjectURL`：整份 zip 会先进浏览器内存，几百 MB 的
     全量日志直接把标签页撑爆；也不用 `location.href =`，出错时会整页跳成一段 JSON。
   ★ `a.download` 留空 —— 同源时 `Content-Disposition` 里的文件名优先，两头都写等于
     埋一个「以后改名只改一半」的坑。
   ★ 弹窗内所有下载钮都是米黄 `.btn.btn-sm`：下载是只读动作，没有「主钮」；
     金色留给「按下去就改东西」的那一颗（D40a / D97f 的语言）。
   ====================================================================== */

var LOGS = null;              // {data, busy} —— 弹窗开着时非 null

async function openLogsModal() {
  LOGS = {data: null, busy: false};
  var body = $("logsBody");
  body.textContent = "";
  body.appendChild(el("div", "list-empty", "读取中……"));
  $("logsModal").classList.remove("hidden");
  await loadLogs();
}

function closeLogsModal() {
  LOGS = null;
  $("logsModal").classList.add("hidden");
  $("logsBody").textContent = "";
}

/** 重取两个目录的数字。返回「拿到了没」。 */
async function loadLogs() {
  if (!LOGS) { return false; }
  var result = await api("/admin/api/logs");
  if (bounced(result)) { return false; }
  if (!LOGS) { return false; }                 // 等回包期间弹窗被关了
  if (!result.ok) {
    toast((result && result.message) || "读不到日志目录", false);
    return false;
  }
  LOGS.data = result;
  renderLogs();
  return true;
}

/** 一张卡片：标题行（名字 + 目录 + 合计）。行由 `logsRow` 往里加。 */
function logsGroup(title, dirname, dirPath, tally) {
  var group = el("section", "logs-group");
  var head = el("div", "logs-head");
  head.appendChild(el("b", null, title));
  var code = el("code", null, dirname + "/");
  code.title = dirPath;                        // 服务器上的绝对路径，鼠标指上去看
  head.appendChild(code);
  head.appendChild(el("span", "logs-meta", tally));
  group.appendChild(head);
  return group;
}

function logsRow(host, label, meta, button) {
  var row = el("div", "logs-row");
  row.appendChild(el("span", "logs-lab", label));
  row.appendChild(el("span", "logs-meta", meta));
  row.appendChild(button);
  host.appendChild(row);
  return row;
}

function logsButton(text, files, query, label) {
  var button = el("button", "btn btn-sm", text);
  if (!files) {
    button.disabled = true;                    // 没东西可打（服务端那头也会回 404）
    button.title = "没有可下载的文件";
  } else {
    button.onclick = function () { downloadLogs(query, label); };
  }
  return button;
}

function tallyText(tally) {
  return tally.files + " 个文件 · " + tally.size_text;
}

function renderLogs() {
  var data = LOGS.data;
  var body = $("logsBody");
  body.textContent = "";
  body.appendChild(el("p", "hint logs-note",
    "打成 zip 直接下载，边打包边传（下载栏里看不到总大小，传完才知道）；"
    + "服务器上不留副本。时间都是服务器本地时间。"));

  var server = data.server;
  var hours = data.recent_hours;
  var group = logsGroup("服务端日志", server.dirname, server.dir,
                        tallyText(server.total));
  logsRow(group, "全部", tallyText(server.total),
          logsButton("下载全量", server.total.files,
                     "kind=server&scope=all", "服务端日志（全量）"));
  logsRow(group, "最近 " + hours + " 小时", tallyText(server.recent),
          logsButton("下载最近 " + hours + " 小时", server.recent.files,
                     "kind=server&scope=recent",
                     "服务端日志（最近 " + hours + " 小时）"));
  body.appendChild(group);

  var crash = data.crash;
  var dirs = crash.dirs || [];
  group = logsGroup("客户端崩溃包", crash.dirname, crash.dir,
                    dirs.length + " 份 · " + crash.total.size_text);
  logsRow(group, "全部（" + dirs.length + " 份）", tallyText(crash.total),
          logsButton("下载全量", crash.total.files,
                     "kind=client_crash", "客户端崩溃包（全部）"));
  var list = el("div", "logs-list");
  if (!dirs.length) {
    list.appendChild(el("div", "list-empty", "还没有收到过崩溃包"));
  }
  dirs.forEach(function (row) {
    var line = logsRow(list, row.name,
                       row.files + " 个文件 · " + row.size_text + " · " + row.mtime_text,
                       logsButton("下载", row.files,
                                  "kind=client_crash&sub=" + encodeURIComponent(row.name),
                                  "崩溃包 " + row.name));
    line.firstChild.classList.add("logs-name");
  });
  group.appendChild(list);
  body.appendChild(group);
  body.appendChild(el("p", "hint logs-note", "统计时刻：" + data.generated_text
                      + "（关掉重开、或点任一下载钮都会重算一遍）"));
}

/** 先重取一次（刷数字 + 会话检查），再让浏览器去下载。 */
async function downloadLogs(query, label) {
  if (!LOGS || LOGS.busy) { return; }         // 连点：上一次的预检还没回来
  LOGS.busy = true;
  var fresh;
  try {
    fresh = await loadLogs();
  } finally {
    if (LOGS) { LOGS.busy = false; }
  }
  if (!fresh) { return; }
  var link = document.createElement("a");
  link.href = "/admin/api/logs/download?" + query;
  link.download = "";
  document.body.appendChild(link);
  link.click();
  link.remove();
  toast("已开始下载「" + label + "」，进度在浏览器的下载栏里。", true);
}

/* ======================================================================
   玩家仓库（V0.3商店 D22 的配套：商店按真实等级卖，改数值只能从这儿改）

   模型：`PLAYER.view` 是服务端那份快照，`PLAYER.edit` 是**要提交的补丁**
   —— 两张 `{itemId: 数量}` 表 + 等级 + 金币。删掉一件东西 = 把它的数量写成 0
   （服务端 `admin_update_account()` 就是按「数量 <= 0 删掉这一格」认的），
   所以补丁里必须留着那个 0，不能把键删掉。

   ★ 2026-09-06 之后**仓库里什么都能改**（D23a），没有「锁着的」那一类了。
   ====================================================================== */

var PLAYER = null;        // {view, edit:{level, money, materials, inventory}}
var PLAYER_LIST = [];
var PLAYER_PAGE = {page: 0, pages: 1, total: 0, size: 10, q: "", online: "all"};

//: 运营把鼠标指到那颗锁住的「修改仓库」上时说的话（用户 2026-09-13 第五轮）。
//  ★ 一处定义两处用（按钮的 `title` + 页面说明那一句），别各写各的。
var LOCKED_LOCKER_NOTE = "运营权限不能直接修改玩家仓库";

/** 说明块里那条「你改不了仓库」——**只画给运营看**。
 *
 * ★ 上面那三条讲的都是「怎么改」，而运营根本改不了 —— 不说一句的话，
 *   他只会对着那颗灰按钮猜是不是页面坏了。
 * ★ 按**身份**判（不是「有没有那颗钮」）：和 `isReadOnly()` 那一套同一个
 *   路子，一处一处地判「这个东西该不该画」迟早漏。
 */
function paintLockedLockerNote() {
  var note = $("playersLockedNote");
  var locked = !isSystemAdmin();
  note.textContent = locked
    ? LOCKED_LOCKER_NOTE + " —— 这一页你可以查玩家、看他现在在哪、"
      + "批量发送奖励，但「修改仓库」要系统管理员来。"
    : "";
  note.classList.toggle("hidden", !locked);
}

//: 在线筛选那几档的中文名（D75 三档 + 用户 2026-09-13 的两档 + 2026-09-14
//  的「挂机中」）。值和服务端 `admin.ONLINE_FILTERS` 一样，下拉本身在
//  `admin.html` 里 —— 这份表只给「N 个账号（不在线）」那句话和空列表文案用。
//  ★ 加一档就要**两边一起加**：漏了这里不会报错，只会在那句话里画出
//  `undefined`（「没有 undefined 的匹配账号」）。
var ONLINE_FILTER_ZH = {on: "在线", off: "不在线",
                        idle: "待机中", playing: "游戏中", afk: "挂机中"};

//: 位置码 -> 中文（用户 2026-09-13）。码由服务端 `gameserver.PLACE_*` 发，
//  中文只在这一处翻 —— 和 `ROLE_ZH` / `ADMIN_BADGE_ZH` 一个路子。
//  ★ **商店 / 合成 / 仓库合成一档**：它们在客户端是同一个 ShopStage，进去时
//  发的包逐字节相同，服务端分不出是哪一个（用户 2026-09-13 拍板合并）。
//  ★ **挂机那两档是「游戏中」的细分**（用户 2026-09-14）：进图了但一直
//  没有「他在玩」的证据 —— 多人局看键盘（20 秒），**单人局看「有没有
//  打中 / 捡到 / 得分」（45 秒）**，因为单人房里服务端收不到同步包。
//  鼠标和 F5 都不算（连点器会挪鼠标、会点 F5）。★ 死了等复活 / 观战、
//  结算界面、开局读图这几段**维持倒下前那一刻的状态**，不会闪回「游戏中」。
//  判据全在服务端（`gameserver.conn_is_afk`），前台只管翻译。
var PLACE_ZH = {
  lobby: "大厅",
  shop: "商店界面",
  room_quest: "待机房间·任务",
  room_battle: "待机房间·对战",
  play_quest: "游戏中·任务",
  play_battle: "游戏中·对战",
  afk_quest: "挂机中·任务",
  afk_battle: "挂机中·对战"
};

/** 画「状态」那一格：在线的画实心圆点 + 他在哪，不在线画空心圆点 + 「不在线」。
 *
 * ★ 圆点和工具条右端那个「当前在线」共用一套（`.dot`，琥珀不是绿 ——
 *   `admin.css` 里写着别改回去）。★ 位置码认不出来时**原样画出来**，
 *   不画空白：服务端加了新位置而前台忘了翻译时，空白会被当成「没在线」。
 */
function placeCell(row) {
  var td = el("td", "place" + (row.online ? "" : " off"));
  td.appendChild(el("i", "dot"));
  td.appendChild(document.createTextNode(placeText(row)));
  return td;
}

/** 「他在哪」这句话的**唯一出处**（用户 2026-09-13 第二轮）。
 *
 * 玩家列表那一列只写位置本身（那一列的表头就叫「状态」，上下文已经说清了）；
 * 别的地方（修改仓库弹窗 / 装备卖出页 / 发奖弹窗左栏）挤在一行字里，
 * 光写「待机房间·任务」看不出这是在线状态 ⇒ 用 `wrap` 包成「在线（…）」。
 *
 * ★ 位置码认不出来时**原样画出来**，不画空白：服务端加了新位置而前台忘了
 *   翻译时，空白会被当成「没在线」。
 */
function placeText(row, wrap) {
  if (!row.online) { return "不在线"; }
  var where = PLACE_ZH[row.place] || row.place || "";
  if (!wrap) { return where || "在线"; }
  return where ? "在线（" + where + "）" : "在线";
}

/** 查一页。`page` 省略 = **筛选条件没变**就停在当前页，变了就回第一页。
 *
 * ★ 「变了没有」按 `q` + 在线筛选**一起**判（D75）：只看 `q` 的话，在第 3 页
 *   把筛选从「全部」改成「在线」会停在第 3 页 —— 而筛完可能一共就一页，
 *   人看到的是一张空表。
 */
async function searchPlayers(page) {
  var q = $("playerSearch").value.trim();
  var online = $("playerOnline").value;
  if (page === undefined) {
    page = (q === PLAYER_PAGE.q && online === PLAYER_PAGE.online)
      ? PLAYER_PAGE.page : 0;
  }
  var result = await api("/admin/api/players?q=" + encodeURIComponent(q)
                         + "&page=" + page
                         + "&online=" + encodeURIComponent(online));
  if (bounced(result)) { return false; }
  if (!result.ok) {
    // 回执走右上角浮条（D39）：列表下面那条 `.msg` 2026-09-07 拿掉了 ——
    // 这一页改成「只有列表滚」的壳之后，列表下面什么都不放。
    toast((result && result.message) || "查找失败", false);
    return false;
  }
  PLAYER_LIST = result.players;
  PLAYER_PAGE = {page: result.page, pages: result.pages,
                 total: result.total, size: result.size, q: q, online: online};
  renderPlayerRows();
  // ★ 筛了在线状态就把口径写在数后面（D75）—— 不写的话「2 个账号」会被
  //   当成「全服就俩号」，而它其实是「筛出来俩」。
  $("playerCount").textContent = result.total + " 个账号"
    + (online === "all" ? "" : "（" + ONLINE_FILTER_ZH[online] + "）");
  // 工具条右端的「当前在线：N 人」（用户 2026-09-08）。★ 这个数是**全服**的，
  // 跟搜索串和页码都无关 —— 列表里那些 ● 只是这一页里在线的那几个。
  // 拿到过一次才显示：没查过就写「0 人」会被当成「现在没人在线」。
  $("playerOnlineNum").textContent = result.online_total;
  var tally = $("playerOnlineTally");
  // `zero` = 一个人都没有：圆点变空心（和弹窗那个 `● 在线 / ○ 不在线` 同口径）。
  tally.classList.toggle("zero", !result.online_total);
  tally.classList.remove("hidden");
  return true;
}

/** 「↻ 刷新」——「玩家仓库」和「管理员账号」两页**共用这一发**
 *  （用户 2026-09-06，D43）：点哪一个按钮都把两页一起刷。
 *
 * ★ 为什么两页一起：它们是同一份 `accounts.json` 的两个视图。把一个玩家
 *   设成运营（D40）之后两边都变了 —— 只刷一边就会出现「玩家列表上写着
 *   已是管理员、管理员名单里还没有他」。
 * ★ **停在当前页、保留搜索串** —— 刷新不该把人弹回第一页。
 * ★ 有没保存的改动先问一句：刷新会拿服务端那份盖掉编辑区，和 `openPlayer`
 *   同一个口径（那边已经这么问了，两处别不一致）。
 */
async function refreshAccounts() {
  if (PLAYER && playerDirty()
      && !(await ask({title: "还有没保存的改动",
                      lead: "「" + PLAYER.view.username
                            + "」还有没保存的改动，刷新会丢掉，确定？",
                      ok: "刷新"}))) {
    return;
  }
  var open = PLAYER ? PLAYER.view.username : null;
  toast("刷新中……", true);
  // 不带页码 = `searchPlayers` 自己那套：搜索串没变就停在当前页，
  // 变了（用户改了输入框但没按查找）就回第一页。
  var ok = await searchPlayers();
  // ★ `force` = 别再问一次「确定丢掉改动」—— 上面已经问过了。
  if (ok && open) { ok = await openPlayer(open, true); }
  ok = (await loadAdmins()) && ok;
  // ★ 失败时**不要**盖掉错误信息 —— 「已刷新」压在「请先登录」上面，
  //   用户看到的就是「点了刷新，然后什么都没变」。
  if (ok) { toast("已刷新：玩家仓库和管理员账号都是最新的。", true); }
}

function renderPlayerRows() {
  var rows = $("playerRows");
  rows.textContent = "";
  if (!PLAYER_LIST.length) {
    var tr = document.createElement("tr");
    // 筛了在线状态就把它写进这句话（D75）：「没有匹配的账号」会被当成
    // 「搜索串打错了」，而实际常常是「这会儿一个人都不在线」。
    var td = el("td", "own-empty",
                PLAYER_PAGE.online === "all"
                  ? "没有匹配的账号"
                  : "没有" + ONLINE_FILTER_ZH[PLAYER_PAGE.online] + "的匹配账号");
    td.colSpan = 6;
    tr.appendChild(td);
    rows.appendChild(tr);
  }
  PLAYER_LIST.forEach(function (row) {
    var line = document.createElement("tr");
    if (PLAYER && PLAYER.view.username === row.username) { line.className = "on"; }
    // ★ 用户名后面那个 ` ●` 2026-09-13 拿掉了 —— 「状态」列里已经有一个
    //   圆点，而且还写着他在哪，同一行画两个圆点只会让人以为是两件事。
    line.appendChild(el("td", null, row.username));
    line.appendChild(el("td", null, row.nickname));
    line.appendChild(placeCell(row));
    line.appendChild(el("td", null, row.level));
    line.appendChild(el("td", null, row.money));
    var td = el("td");
    // ★ 「设为管理员（运营）」2026-09-13 搬去「管理员账号」页了（D40 当初
    //   图快放在这儿，可它是账号管理，不是仓库）。这一格现在只剩这一个钮，
    //   `.acts` 那层右对齐留着 —— 表头那 110px 是按它量的。
    var acts = el("div", "acts");
    // ★ 「成就卡片进度」排在「修改仓库」左边（用户 2026-09-13 第四轮）：
    //   它是**只读**的一眼看，摆在会改东西的那颗钮前面。
    //   ★★ **运营也看得见**（用户第六轮）：它只回「这个人离下一张卡还差
    //     多少」，既不是仓库清单也改不了任何东西 —— 和「修改仓库」那道门
    //     （D97i，系统管理员专用）不是一回事。真正的门在服务端
    //     （`_admin_card_progress`：看别人要 `_require_editor()`）。
    //   ⚠ 这一页只读玩家根本进不来（`applyRoleToTabs`），所以这儿不锁。
    var progress = el("button", "btn btn-sm", "成就卡片进度");
    progress.onclick = function () { openCardProgress(row.username); };
    acts.appendChild(progress);
    var button = el("button", "btn btn-sm btn-primary", "修改仓库");
    // ★★ 运营进得了这一页、也能批量发奖，但**改不了别人的仓库**
    //   （用户 2026-09-13 第五轮）。
    //   ⚠ 锁法是 `aria-disabled` 而**不是** `disabled`：浏览器不给
    //     `disabled` 元素派发鼠标事件，那句 `title` 提示根本弹不出来 ——
    //     而这颗钮锁住之后的全部意义就是告诉他「为什么点不了」。
    //     点不动靠的是**不绑 onclick**；真正的门在服务端
    //     （`GET`/`POST /admin/api/player` 都是 `_require_system_admin()`）。
    if (isSystemAdmin()) {
      button.onclick = function () { openPlayer(row.username); };
    } else {
      button.setAttribute("aria-disabled", "true");
      button.title = LOCKED_LOCKER_NOTE;
    }
    acts.appendChild(button);
    td.appendChild(acts);
    line.appendChild(td);
    rows.appendChild(line);
  });
  renderPlayerPager();
}

/** 把一个玩家账号收进管理员表，权限给**运营**（用户 2026-09-06，D40）。
 *
 * ★ 请求里**只有用户名** —— 密码由服务端自己从那个账号里取，页面从头到尾
 *   看不见它，也就不会跑到日志、截图和浏览器历史里去（铁律 9）。
 * ★ 入口 2026-09-13 从「玩家仓库」的列表挪到了「管理员账号」页那个弹窗
 *   （`#promoteModal`）—— 这个函数本身一个字没变，只是刷新哪几处跟着换了。
 */
async function promoteToAdmin(username) {
  var go = await ask({
    title: "设为管理员（运营）",
    lead: "把玩家「" + username + "」加成「运营」权限的管理员？\n"
        + "用户名和密码原样照搬 —— 他以后就用游戏里那套账号登管理页。\n"
        // ★ 分隔符用「、」不用「 / 」—— 页名自己就带斜杠（「金币 / 经验获取」），
        //   用斜杠隔开会读成六页。
        + "运营只看得到 " + configTitles().join("、")
        + " 这 " + configCount() + " 页。",
    ok: "设为运营"});
  if (!go) { return; }
  var result = await api("/admin/api/admins/from_player", {name: username});
  if (bounced(result)) { return; }
  toast(result.message, result.ok);
  if (!result.ok) { return; }
  if (result.admins) { renderAdmins(result.admins); }
  // 弹窗里那一格要变成「已是管理员」—— 停在当前页重查一次。
  // ★ 玩家仓库那一页**不用**再刷：2026-09-13 起它已经没有这个钮了。
  searchPromote(PROMOTE_PAGE.page);
}

/* ---------------------------------------------------------------------
   「指定玩家为管理员」弹窗（用户 2026-09-13）

   ★ 数据源就是玩家仓库那一发 `/admin/api/players` —— 它早就带了分页、
     搜索、在线筛选和 `admin_role`，一个新接口都不用加。
   ★ 分页状态**和玩家仓库分开**（`PROMOTE_PAGE` vs `PLAYER_PAGE`）：
     两个列表各翻各的页，共用一份的话关掉弹窗会把背后那一页也带跑。
   --------------------------------------------------------------------- */
var PROMOTE_PAGE = {page: 0, pages: 1, total: 0, size: 10, q: "", online: "all"};
var PROMOTE_LIST = [];
//: 开着没有（Esc 那条链照 `LEVEL_MODAL` 的样子判）。
var PROMOTE_OPEN = false;

function openPromoteModal() {
  PROMOTE_OPEN = true;
  $("promoteModal").classList.remove("hidden");
  searchPromote(0);
}

function closePromoteModal() {
  PROMOTE_OPEN = false;
  $("promoteModal").classList.add("hidden");
}

/** 查一页。`page` 省略 = 筛选条件没变就停在当前页，变了就回第一页
 *  （和 `searchPlayers` 同一个道理：筛完可能只剩一页，停在第 3 页就是空表）。
 */
async function searchPromote(page) {
  var q = $("promoteSearch").value.trim();
  var online = $("promoteOnline").value;
  if (page === undefined) {
    page = (q === PROMOTE_PAGE.q && online === PROMOTE_PAGE.online)
      ? PROMOTE_PAGE.page : 0;
  }
  var result = await api("/admin/api/players?q=" + encodeURIComponent(q)
                         + "&page=" + page
                         + "&online=" + encodeURIComponent(online));
  if (bounced(result)) { return false; }
  if (!result.ok) {
    toast((result && result.message) || "查找失败", false);
    return false;
  }
  PROMOTE_LIST = result.players;
  PROMOTE_PAGE = {page: result.page, pages: result.pages,
                  total: result.total, size: result.size, q: q, online: online};
  renderPromoteRows();
  $("promoteCount").textContent = result.total + " 个账号"
    + (online === "all" ? "" : "（" + ONLINE_FILTER_ZH[online] + "）");
  return true;
}

function renderPromoteRows() {
  var rows = $("promoteRows");
  rows.textContent = "";
  if (!PROMOTE_LIST.length) {
    var tr = document.createElement("tr");
    var td = el("td", "own-empty",
                PROMOTE_PAGE.online === "all"
                  ? "没有匹配的账号"
                  : "没有" + ONLINE_FILTER_ZH[PROMOTE_PAGE.online] + "的匹配账号");
    td.colSpan = 5;
    tr.appendChild(td);
    rows.appendChild(tr);
  }
  PROMOTE_LIST.forEach(function (row) {
    var line = document.createElement("tr");
    line.appendChild(el("td", null, row.username));
    line.appendChild(el("td", null, row.nickname));
    line.appendChild(placeCell(row));
    line.appendChild(el("td", null, row.level));
    var td = el("td");
    var acts = el("div", "acts");
    // 已经是管理员的：**点不动的灰钮**，上面写他的实际权限（D40）——
    // 别给一个点下去必然报「已经存在」的按钮。
    var promote = el("button", "btn btn-sm",
                     row.admin_role ? (ADMIN_BADGE_ZH[row.admin_role]
                                       || row.admin_role)
                                    : "设为管理员（运营）");
    if (row.admin_role) {
      promote.disabled = true;
      promote.title = "这个玩家已经能登管理页了，权限在管理员列表里改";
    } else {
      promote.onclick = function () { promoteToAdmin(row.username); };
    }
    acts.appendChild(promote);
    td.appendChild(acts);
    line.appendChild(td);
    rows.appendChild(line);
  });
  renderPromotePager();
}

function renderPromotePager() {
  pagerInto($("promotePager"), PROMOTE_PAGE, searchPromote);
}

/** 把换页栏画进 `host`。`state` 是 `{page, pages}`，`go(页码)` 是重查那一发。
 *
 * ★ 「玩家仓库」和「指定玩家为管理员」弹窗**共用这一份**（用户 2026-09-13
 *   加第二个列表时抽出来的）：两条换页栏长得一样、行为也该一样，各写一份
 *   迟早会漂移成「一边能翻到最后一页、另一边差一页」。
 */
function pagerInto(host, state, go) {
  host.textContent = "";
  // ★ 只有一页时整条都不画 —— 大多数服务器就几十个号，别让翻页控件
  //   在那儿占一行说「第 1 / 1 页」。
  if (state.pages <= 1) { return; }
  function step(label, target, disabled) {
    var button = el("button", "btn btn-sm", label);
    button.disabled = disabled;
    button.onclick = function () { go(target); };
    host.appendChild(button);
  }
  step("‹ 上一页", state.page - 1, state.page <= 0);
  host.appendChild(el("span", "pageno",
                      "第 " + (state.page + 1) + " / " + state.pages + " 页"));
  step("下一页 ›", state.page + 1, state.page >= state.pages - 1);
}

function renderPlayerPager() {
  pagerInto($("playerPager"), PLAYER_PAGE, searchPlayers);
}

async function openPlayer(username, force) {
  if (!force && PLAYER && playerDirty()
      && !(await ask({title: "还有没保存的改动",
                      lead: "「" + PLAYER.view.username
                            + "」还有没保存的改动，确定丢掉？",
                      ok: "丢掉"}))) {
    return;
  }
  var result = await api("/admin/api/player?name=" + encodeURIComponent(username));
  if (bounced(result)) { return false; }
  if (!result.ok) {
    toast((result && result.message) || "读不到这个账号", false);
    return false;
  }
  adoptPlayer(result.player);
  return true;
}

/** 把服务端那份快照变成「快照 + 补丁」，并打开（或刷新）编辑弹窗。
 *  同一个人重读（保存回执 / 刷新）时停在原来的分类标签和两个下拉上，
 *  别跳回「全部」。 */
function adoptPlayer(view) {
  var edit = {level: view.level, money: view.money,
              materials: {}, inventory: {}};
  view.materials.forEach(function (row) { edit.materials[row.id] = row.count; });
  view.inventory.forEach(function (row) { edit.inventory[row.id] = row.count; });
  var same = !!(PLAYER && PLAYER.view.username === view.username);
  PLAYER = {view: view, edit: edit,
            tab: same ? PLAYER.tab : anyTab(),
            filter: same ? PLAYER.filter : {character: "", listing: ""}};
  renderPlayer();
  renderPlayerRows();
}

/** 右上角那个 ✕ —— **唯一**能关掉弹窗的地方（用户 2026-09-07：点遮罩不关）。
 *  有没保存的改动先问一句。 */
async function closePlayerModal() {
  if (!PLAYER) { return; }
  if (playerDirty()
      && !(await ask({title: "还有没保存的改动",
                      lead: "「" + PLAYER.view.username
                            + "」还有没保存的改动，关掉就丢了，确定？",
                      ok: "丢掉并关闭"}))) {
    return;
  }
  PLAYER = null;
  $("playerModal").classList.add("hidden");
  renderPlayerRows();                       // 列表里那一行的高亮收掉
}

function playerDirty() {
  if (!PLAYER) { return false; }
  var edit = PLAYER.edit;
  var view = PLAYER.view;
  if (Number(edit.level) !== view.level) { return true; }
  if (Number(edit.money) !== view.money) { return true; }
  return ["materials", "inventory"].some(function (bucket) {
    var was = {};
    view[bucket].forEach(function (row) { was[row.id] = row.count; });
    return Object.keys(edit[bucket]).some(function (id) {
      return Number(edit[bucket][id]) !== (was[id] || 0);
    }) || Object.keys(was).some(function (id) {
      return !(id in edit[bucket]);
    });
  });
}

function playerTouched() {
  var dirty = playerDirty();
  var node = $("playerDirty");
  node.textContent = dirty ? "有未保存的修改" : "没有未保存的修改";
  node.className = "dirty" + (dirty ? "" : " clean");
  paintPlayerEdited();
}

/** 这个人的这一件东西，和刚读回来那份比改过没有。
 *  ★ 服务端没给的那些 = 0 件（`adoptPlayer` 就是这么摊平的）。 */
function ownEdited(bucket, itemId) {
  var was = 0;
  (PLAYER.view[bucket] || []).forEach(function (row) {
    if (Number(row.id) === Number(itemId)) { was = row.count; }
  });
  return Number(PLAYER.edit[bucket][itemId] || 0) !== Number(was);
}

/** 改过的那几格 / 等级 / 金币各自套上「改过了」那一圈（用户 2026-09-14）。
 *
 * ★ 等级和金币标的是**那一格**：它俩不是「一件物品」，整行标过去就把
 *   旁边只读的经验也圈进来了。 */
function paintPlayerEdited() {
  if (!PLAYER) { return; }
  $("playerLevel").parentNode.classList.toggle(
    "edited", Number(PLAYER.edit.level) !== Number(PLAYER.view.level));
  $("playerMoney").parentNode.classList.toggle(
    "edited", Number(PLAYER.edit.money) !== Number(PLAYER.view.money));
  Array.prototype.forEach.call(
    $("playerOwnedGrid").querySelectorAll("[data-own]"), function (node) {
      var at = node.getAttribute("data-own").split(":");
      node.classList.toggle("edited", ownEdited(at[0], Number(at[1])));
    });
}

function renderPlayer() {
  var view = PLAYER.view;
  $("playerWho").textContent = view.nickname + "（" + view.username + "）";
  // 在线状态紧跟在名字后面（用户 2026-09-07），不再放标题栏右端。
  // ★ 是 `playerOnlineNote` 不是 `playerOnline` —— 后者是工具条上那个在线筛选
  //   下拉（D75），2026-09-10 撞过名：这一句把下拉的三个选项抹成了一行字。
  // ★ 在线时连**他现在在哪**一起写（用户 2026-09-13 第二轮）：
  //   「在线（待机房间·任务）」比光写「在线」有用 —— 改仓库之前先看一眼
  //   他是不是正在打，和玩家列表那一列同一套说法（`PLACE_ZH`）。
  $("playerOnlineNote").textContent =
    (view.online ? "● " : "○ ") + placeText(view, true)
    + (view.online ? "，改完即时生效" : "");
  $("playerLevel").value = PLAYER.edit.level;
  $("playerLevel").max = view.level_max;
  $("playerMoney").value = PLAYER.edit.money;
  $("playerExp").value = view.experience + "（本级 " + view.level_start_exp
    + " ~ 下一级 " + view.next_level_exp + "）";
  renderPlayerFilters();
  $("playerModal").classList.remove("hidden");
  repaintOwned();
}

/** 弹窗里角色 / 上架状态两个下拉（用户 2026-09-09，D68）：选项和配置页、
 *  「选择物品」弹窗同一份，值照 `PLAYER.filter`。选项每次重填 —— 弹窗里的
 *  「↻ 刷新」会重读物品表，角色表跟着它走。 */
function renderPlayerFilters() {
  fillSelect($("playerCharacter"), "全部角色", characterOptions(),
             PLAYER.filter.character);
  fillSelect($("playerListing"), "全部上架状态", LISTING_FILTER_OPTIONS,
             PLAYER.filter.listing);
}

/* ---------------------------------------------------------- 仓库分类
   分类标签那一套（`whTabs` / `whMatches` / `paintCatTabs`）2026-09-09 起
   四处共用，搬到「分类标签」那一节去了（D68）。
   材料和仓库物品**不再分两块**（用户 2026-09-07）—— 游戏里它们本来就在
   同一个仓库面板里（材料在「收集品 → 材料」）。 */

/** 两桶合成一张表：`{bucket, id, cat}`，按 id 排。数量为 0 的也在（装备类
 *  「没有」那一格要留着让人翻回来）。 */
function ownedEntries() {
  var out = [];
  ["materials", "inventory"].forEach(function (bucket) {
    Object.keys(PLAYER.edit[bucket]).forEach(function (id) {
      var itemId = Number(id);
      out.push({bucket: bucket, id: itemId, cat: whCategory(itemId)});
    });
  });
  out.sort(function (a, b) { return a.id - b.id; });
  return out;
}

/** 过了角色 / 上架状态两个下拉的那些（D68）；分类标签在 `renderOwnedList`
 *  里再收一遍，标签上的件数从这儿数（和配置页一个套路）。 */
function ownedFiltered() {
  return ownedEntries().filter(function (entry) {
    return dropdownsMatch(entry.id, PLAYER.filter);
  });
}

/** 两行标签。件数只数「有」的（数量 > 0）。 */
function renderPlayerTabs() {
  var have = ownedFiltered().filter(function (entry) {
    return Number(PLAYER.edit[entry.bucket][entry.id]) > 0;
  });
  paintCatTabs($("playerCats"), $("playerSubCats"), PLAYER.tab, function (id) {
    return have.filter(function (entry) { return whMatches(id, entry.cat); }).length;
  }, function () {
    renderPlayerTabs();
    renderOwnedList();
  });
}

/** 画当前分类下的格子。★ 重画一律不动滚动条（D37b）。 */
function renderOwnedList() {
  var box = $("playerOwned");
  var keep = box.scrollTop;
  var grid = $("playerOwnedGrid");
  grid.textContent = "";
  var requested = tabRequested(PLAYER.tab);
  var rows = ownedFiltered().filter(function (entry) {
    return whMatches(requested, entry.cat);
  });
  if (!rows.length) {
    grid.appendChild(el("div", "own-empty",
                        ownedEntries().length ? "这个分类 / 筛选条件下没有东西"
                                              : "仓库是空的 —— 点「＋ 添加物品」"));
  }
  rows.forEach(function (entry) {
    grid.appendChild(ownNode(entry.bucket, entry.id));
  });
  box.scrollTop = keep;
}

function repaintOwned() {
  renderPlayerTabs();
  renderOwnedList();
  playerTouched();
}

/** 名字：中文名翻不出来时退回 `#id`（`item_name_zh` 自己就这么退的）。 */
function ownName(itemId) {
  return itemName(itemId);
}

/** 副标题：★ 名字已经是 `#id` 时别再写一遍，换成「类别 · 角色 · 系列」。 */
function ownMeta(itemId) {
  var name = ownName(itemId);
  return name === ("#" + itemId) ? itemMeta(itemId) : ("#" + itemId);
}

/** 服务端说这件东西的数量有没有意义（装备类没有，见 admin.py 的 `stackable`）。*/
function ownStackable(bucket, itemId) {
  var rows = PLAYER.view[bucket] || [];
  for (var i = 0; i < rows.length; i += 1) {
    if (rows[i].id === itemId) { return rows[i].stackable !== false; }
  }
  // 新加进来的（服务端还没见过）：按物品表里的部位掩码判，口径和服务端一样。
  return !(BYID[itemId] && BYID[itemId].part_flag);
}

function ownEquipped(bucket, itemId) {
  var rows = PLAYER.view[bucket] || [];
  for (var i = 0; i < rows.length; i += 1) {
    if (rows[i].id === itemId) { return !!rows[i].equipped; }
  }
  return false;
}

function ownNode(bucket, itemId) {
  var stack = ownStackable(bucket, itemId);
  var have = Number(PLAYER.edit[bucket][itemId]) > 0;
  var box = el("div", "own" + (stack || have ? "" : " off"));
  // 「改过了」那一圈按它找格子（`paintPlayerEdited`）。★ 数字框改完
  // **格子本身不重画**（见下面 `oninput` 那句），所以这一圈只能靠扫属性刷。
  box.setAttribute("data-own", bucket + ":" + itemId);
  box.appendChild(slotNode(itemId, 26, false, false));

  // 名字那两行也挂上浮窗（被省略号截掉时全名在浮窗里）。★ 数字框和按钮
  // **不挂** —— 正在改数量时头上冒一张卡挡着看不见。
  var col = tipFor(el("div", "col"), itemId);
  col.appendChild(el("div", "nmz", ownName(itemId)));
  var meta = el("div", "meta", ownMeta(itemId));
  if (ownEquipped(bucket, itemId)) {
    meta.appendChild(el("span", "worn", "穿着"));
  }
  col.appendChild(meta);
  box.appendChild(col);

  if (!stack) {
    // ★ 装备类只有「有 / 没有」—— 数量那一格客户端根本不读（§28），
    //   给个数字框只会让人以为发出去了三件。
    var toggle = el("label", "toggle" + (have ? " on" : ""));
    toggle.appendChild(el("span", "track"));
    toggle.appendChild(el("span", "lab", have ? "拥有" : "没有"));
    toggle.title = "装备类只有「有」和「没有」，数量没有意义";
    toggle.onclick = function (event) {
      event.preventDefault();
      PLAYER.edit[bucket][itemId] = have ? 0 : 1;
      repaintOwned();
    };
    box.appendChild(toggle);
    return box;
  }

  var input = document.createElement("input");
  input.type = "number";
  input.min = "0";
  input.step = "1";
  input.value = PLAYER.edit[bucket][itemId];
  input.oninput = function () {
    PLAYER.edit[bucket][itemId] = Math.max(0, Number(input.value) || 0);
    playerTouched();
    renderPlayerTabs();                 // 标签上的件数跟着变；格子本身不重画
  };
  box.appendChild(input);
  var drop = el("button", "btn btn-sm btn-danger drop", "✕");
  drop.title = "清零 —— 保存后这一格就没了";
  drop.onclick = function () {
    PLAYER.edit[bucket][itemId] = 0;
    input.value = 0;
    playerTouched();
    renderPlayerTabs();
  };
  box.appendChild(drop);
  return box;
}

/** 「＋ 添加物品」：批量选择器（用户 2026-09-07），勾多件一次加进来。
 *  材料进 `materials` 桶、其余进 `inventory` 桶（存档就是这么分的，服务端接口
 *  不变）；已经有的画成「已有」点不动。加进来的东西不在当前分类里就切到「全部」，
 *  免得「加了没反应」。★ **不挡「商店在卖的」**（D23a）。 */
function addOwnedMany() {
  var owned = {};
  ownedEntries().forEach(function (entry) {
    if (Number(PLAYER.edit[entry.bucket][entry.id]) > 0) { owned[entry.id] = true; }
  });
  openPicker({
    multi: true,
    owned: owned,
    onPickMany: function (items) {
      var added = 0;
      var hidden = 0;
      var requested = tabRequested(PLAYER.tab);
      items.forEach(function (item) {
        var bucket = item.kind === "material" ? "materials" : "inventory";
        if (!Number(PLAYER.edit[bucket][item.id])) {
          PLAYER.edit[bucket][item.id] = 1;
          added += 1;
        }
        // 分类标签和两个下拉任何一样挡住它都算「看不见」。
        if (!whMatches(requested, whCategory(item.id))
            || !dropdownsMatch(item.id, PLAYER.filter)) { hidden += 1; }
      });
      if (hidden) {
        PLAYER.tab = anyTab();
        PLAYER.filter = {character: "", listing: ""};
        renderPlayerFilters();
      }
      repaintOwned();
      toast("已加入 " + added + " 件，按「保存」才真的发给玩家"
            + (hidden ? "；有的不在刚才那个分类 / 筛选里，已切回「全部」" : ""),
            true);
    }
  });
}

async function savePlayer() {
  var payload = {
    name: PLAYER.view.username,
    level: Math.max(1, Number($("playerLevel").value) || 1),
    money: Math.max(0, Number($("playerMoney").value) || 0),
    materials: PLAYER.edit.materials,
    inventory: PLAYER.edit.inventory
  };
  toast("保存中……", true);
  var result = await api("/admin/api/player", payload);
  if (bounced(result)) { return; }
  if (result.ok && result.player) {
    adoptPlayer(result.player);
    await searchPlayers();           // 列表里的等级 / 金币跟着更新
  }
  toast(result.message, result.ok);
}

/** 弹窗里的「↻ 刷新」：玩家仓库 + 物品表 + 全部运营配置（名字 / 等级门槛的出处）
 *  全部重读（用户 2026-09-07：「取得最新的用户信息和运营物品信息」）。
 *  有没保存的改动先问一句；失败时不用「已刷新」盖掉错误。 */
async function refreshPlayerPopup() {
  if (!PLAYER) { return; }
  if (playerDirty()
      && !(await ask({title: "还有没保存的改动",
                      lead: "「" + PLAYER.view.username
                            + "」还有没保存的改动，刷新会丢掉，确定？",
                      ok: "刷新"}))) {
    return;
  }
  var name = PLAYER.view.username;
  toast("刷新中……", true);
  var ok = await loadCatalog();
  ok = (await refreshConfigs(true)) && ok;
  ok = (await openPlayer(name, true)) && ok;
  ok = (await searchPlayers()) && ok;
  if (ok) { toast("已刷新：玩家仓库和物品信息都是最新的。", true); }
}

/* ======================================================================
   发送奖励（用户 2026-09-10，D76）

   「玩家仓库」工具条上「发送奖励」点开的批量发奖窗：左栏选人、右栏选奖励，
   「确认发送奖励」→ 确认框 → POST /admin/api/reward/send。服务端把每一样奖励
   各写成一份礼物塞进目标玩家的**游戏内礼物盒**，玩家自己在游戏里领 ——
   这里不改任何人的仓库。
   右栏物品区照「修改仓库」弹窗那套（分类标签 / 两个下拉 / 格子 / 批量选择器），
   只是没有等级 / 经验 / 金币三格 —— 经验金币在这儿是「每人发多少」，不是改存档。
   ====================================================================== */
var REWARD = null;   // {chosen:{username: row}, list:[row], items:{id: 数量}, tab, filter, q, online}

async function openRewardModal() {
  // ★ 每次打开都从头起（名单空、物品空、筛选「全部」、留言回默认值）——
  //   上一次发过的人和东西留在这儿最容易「再发一遍」。
  REWARD = {chosen: {}, list: [], items: {}, tab: anyTab(),
            filter: {character: "", listing: ""}, q: "", online: "all",
            truncated: false};
  $("rewardSearch").value = "";
  $("rewardOnline").value = "all";
  $("rewardExp").value = 0;
  $("rewardMoney").value = 0;
  $("rewardMessage").value = $("rewardMessage").defaultValue;
  $("rewardCount").textContent = "";
  fillSelect($("rewardCharacter"), "全部角色", characterOptions(), "");
  fillSelect($("rewardListing"), "全部上架状态", LISTING_FILTER_OPTIONS, "");
  $("rewardModal").classList.remove("hidden");
  renderRewardChosen();
  repaintRewardItems();
  await loadRewardPlayers();
  $("rewardSearch").focus();
}

function closeRewardModal() {
  REWARD = null;
  $("rewardModal").classList.add("hidden");
}

/** 还有没发出去的选择（关标签页前拦一下用）。 */
function rewardDirty() {
  return !!(REWARD && (Object.keys(REWARD.chosen).length
                       || Object.keys(REWARD.items).length));
}

/** 左栏名单：`/admin/api/reward/players`，不分页（勾人要看全的）。
 *
 * ★ 搜索框**边打边查**（用户 2026-09-10：打完字没反应，得去动一下在线下拉才刷）。
 *   每次击键发一发，不设延时（铁律 10）；回包可能乱序 —— 每发带一个递增序号，
 *   只认**最后发出去的那一发**的回包，早发晚到的直接丢掉。 */
var REWARD_QUERY_SEQ = 0;

async function loadRewardPlayers() {
  if (!REWARD) { return false; }
  var q = $("rewardSearch").value.trim();
  var online = $("rewardOnline").value;
  var seq = ++REWARD_QUERY_SEQ;
  var result = await api("/admin/api/reward/players?q=" + encodeURIComponent(q)
                         + "&online=" + encodeURIComponent(online));
  if (bounced(result)) { return false; }
  if (!REWARD) { return false; }               // 等回包期间弹窗被关了
  if (seq !== REWARD_QUERY_SEQ) { return false; }   // 后面又发过了，这一发作废
  if (!result.ok) {
    toast((result && result.message) || "读不到玩家名单", false);
    return false;
  }
  REWARD.list = result.players;
  REWARD.q = q;
  REWARD.online = online;
  REWARD.truncated = !!result.truncated;
  $("rewardCount").textContent = result.total + " 个账号"
    + (online === "all" ? "" : "（" + ONLINE_FILTER_ZH[online] + "）")
    + " · 全服在线 " + result.online_total + " 人";
  renderRewardPlayers();
  return true;
}

function renderRewardPlayers() {
  var host = $("rewardPlayers");
  var keep = host.scrollTop;
  host.textContent = "";
  if (!REWARD.list.length) {
    host.appendChild(el("div", "own-empty",
                        REWARD.online === "all"
                          ? "没有匹配的账号"
                          : "没有" + ONLINE_FILTER_ZH[REWARD.online] + "的匹配账号"));
    return;
  }
  REWARD.list.forEach(function (row) {
    var line = el("label", "reward-row" + (REWARD.chosen[row.username] ? " on" : ""));
    var box = document.createElement("input");
    box.type = "checkbox";
    box.checked = !!REWARD.chosen[row.username];
    box.onchange = function () {
      if (box.checked) { REWARD.chosen[row.username] = row; }
      else { delete REWARD.chosen[row.username]; }
      line.classList.toggle("on", box.checked);
      renderRewardChosen();
    };
    line.appendChild(box);
    line.appendChild(el("span", "who", row.nickname + "（" + row.username + "）"));
    line.appendChild(el("span", "state" + (row.online ? " on" : ""),
                        (row.online ? "● " : "○ ") + placeText(row, true)));
    line.appendChild(el("span", "lv", "Lv." + row.level));
    host.appendChild(line);
  });
  if (REWARD.truncated) {
    host.appendChild(el("div", "own-empty",
                        "账号太多，只列了前 " + REWARD.list.length
                        + " 个 —— 请缩小搜索范围"));
  }
  host.scrollTop = keep;
}

/** 「奖励玩家名单」那一排 chip：按用户名排，每个带 ✕。 */
function renderRewardChosen() {
  var host = $("rewardChosen");
  host.textContent = "";
  var names = Object.keys(REWARD.chosen).sort();
  $("rewardChosenCount").textContent = names.length ? "已选 " + names.length + " 人" : "还没选人";
  if (!names.length) {
    host.appendChild(el("div", "own-empty", "在下面的列表里勾人，或者「全选」"));
  }
  names.forEach(function (username) {
    var row = REWARD.chosen[username];
    var chip = el("span", "chip" + (row.online ? " on" : ""),
                  row.nickname + "（" + username + "）");
    var drop = el("button", null, "✕");
    drop.title = "从名单里去掉";
    drop.onclick = function () {
      delete REWARD.chosen[username];
      renderRewardChosen();
      renderRewardPlayers();              // 列表里那一行的勾也收掉
    };
    chip.appendChild(drop);
    host.appendChild(chip);
  });
  paintRewardFoot();
}

/** 把当前列表里的人整批加进 / 移出名单。 */
function rewardSelectListed(on) {
  REWARD.list.forEach(function (row) {
    if (on) { REWARD.chosen[row.username] = row; }
    else { delete REWARD.chosen[row.username]; }
  });
  renderRewardPlayers();
  renderRewardChosen();
}

/* ------------------------------------------------------------ 右栏物品 */

/** 数量有没有意义（和服务端 `shopdata.stackable` 同一条：装备类 / 角色卡没有）。 */
function rewardStackable(itemId) {
  var item = BYID[itemId];
  return !(item && (item.part_flag || item.kind === "character"));
}

function rewardEntries() {
  return Object.keys(REWARD.items).map(Number).sort(function (a, b) { return a - b; })
    .map(function (id) { return {id: id, cat: whCategory(id)}; });
}

function rewardFiltered() {
  return rewardEntries().filter(function (entry) {
    return dropdownsMatch(entry.id, REWARD.filter);
  });
}

function renderRewardTabs() {
  var rows = rewardFiltered();
  paintCatTabs($("rewardCats"), $("rewardSubCats"), REWARD.tab, function (id) {
    return rows.filter(function (entry) { return whMatches(id, entry.cat); }).length;
  }, function () {
    renderRewardTabs();
    renderRewardItemList();
  });
}

/** 一格奖励物品：图标 + 名字 + 数量框（装备类只画「×1」）+ ✕。 */
function rewardItemNode(itemId) {
  var box = el("div", "own");
  box.appendChild(slotNode(itemId, 26, false, false));
  var col = tipFor(el("div", "col"), itemId);
  col.appendChild(el("div", "nmz", itemName(itemId)));
  col.appendChild(el("div", "meta", ownMeta(itemId)));
  box.appendChild(col);
  if (!rewardStackable(itemId)) {
    // 装备类只有「有 / 没有」（§28）—— 每人一件，数量没有意义。
    var fixed = el("span", "fixed", "×1");
    fixed.title = "装备类每人一件，数量没有意义";
    box.appendChild(fixed);
  } else {
    var input = document.createElement("input");
    input.type = "number";
    input.min = "1";
    input.step = "1";
    input.value = REWARD.items[itemId];
    input.oninput = function () {
      REWARD.items[itemId] = Math.max(1, Number(input.value) || 1);
      paintRewardFoot();
    };
    box.appendChild(input);
  }
  var drop = el("button", "btn btn-sm btn-danger drop", "✕");
  drop.title = "从奖励里拿掉";
  drop.onclick = function () {
    delete REWARD.items[itemId];
    repaintRewardItems();
  };
  box.appendChild(drop);
  return box;
}

/** 画当前分类下的格子。★ 重画不动滚动条（D37b）。 */
function renderRewardItemList() {
  var box = $("rewardOwned");
  var keep = box.scrollTop;
  var grid = $("rewardOwnedGrid");
  grid.textContent = "";
  var requested = tabRequested(REWARD.tab);
  var rows = rewardFiltered().filter(function (entry) {
    return whMatches(requested, entry.cat);
  });
  if (!rows.length) {
    grid.appendChild(el("div", "own-empty",
                        rewardEntries().length ? "这个分类 / 筛选条件下没有东西"
                                               : "还没选物品 —— 点「＋ 添加物品」；只发经验 / 金币也行"));
  }
  rows.forEach(function (entry) { grid.appendChild(rewardItemNode(entry.id)); });
  box.scrollTop = keep;
}

function repaintRewardItems() {
  renderRewardTabs();
  renderRewardItemList();
  paintRewardFoot();
}

/** 「＋ 添加物品」：和「修改仓库」同一个批量选择器；已经在奖励里的画成「已有」点不动。 */
function rewardAddMany() {
  openPicker({
    multi: true,
    owned: REWARD.items,
    onPickMany: function (items) {
      if (!REWARD) { return; }
      var added = 0;
      var hidden = 0;
      var requested = tabRequested(REWARD.tab);
      items.forEach(function (item) {
        if (!REWARD.items[item.id]) {
          REWARD.items[item.id] = 1;
          added += 1;
        }
        if (!whMatches(requested, whCategory(item.id))
            || !dropdownsMatch(item.id, REWARD.filter)) { hidden += 1; }
      });
      if (hidden) {
        REWARD.tab = anyTab();
        REWARD.filter = {character: "", listing: ""};
        fillSelect($("rewardCharacter"), "全部角色", characterOptions(), "");
        fillSelect($("rewardListing"), "全部上架状态", LISTING_FILTER_OPTIONS, "");
      }
      repaintRewardItems();
      toast("已加入 " + added + " 件奖励物品，按「确认发送奖励」才真的发"
            + (hidden ? "；有的不在刚才那个分类 / 筛选里，已切回「全部」" : ""),
            true);
    }
  });
}

/* ------------------------------------------------------------ 发送 */

/** 要 POST 的东西：人 + 物品数量表 + 经验 / 金币 + 留言（留空由服务端填默认值）。 */
function rewardPayload() {
  return {
    players: Object.keys(REWARD.chosen).sort(),
    items: REWARD.items,
    exp: Math.max(0, Number($("rewardExp").value) || 0),
    money: Math.max(0, Number($("rewardMoney").value) || 0),
    message: $("rewardMessage").value.trim()
  };
}

/** 奖励清单的人话：「黑色小珠 ×5」「经验 ×500」…… */
function rewardParts(payload) {
  var parts = Object.keys(payload.items).map(Number)
    .sort(function (a, b) { return a - b; })
    .map(function (id) { return itemName(id) + " ×" + payload.items[id]; });
  if (payload.exp) { parts.push("经验 ×" + payload.exp); }
  if (payload.money) { parts.push("金币 ×" + payload.money); }
  return parts;
}

/** 底栏那句「N 名玩家 · M 样奖励」+ 发送钮能不能点。 */
function paintRewardFoot() {
  if (!REWARD) { return; }
  var payload = rewardPayload();
  var parts = rewardParts(payload);
  var people = payload.players.length;
  $("rewardSummary").textContent = (people ? people + " 名玩家" : "还没选人")
    + " · " + (parts.length ? parts.length + " 样奖励，每人各一份" : "还没选奖励");
  $("rewardSend").disabled = !(people && parts.length);
}

async function sendReward() {
  if (!REWARD) { return; }
  var payload = rewardPayload();
  var parts = rewardParts(payload);
  if (!payload.players.length || !parts.length) { return; }
  var message = payload.message || $("rewardMessage").defaultValue;
  var chosen = REWARD.chosen;
  var ok = await ask({
    title: "确认发送奖励",
    lead: "给下面 " + payload.players.length + " 名玩家各发一份下列奖励，进他们游戏里的"
          + "礼物盒，由玩家自己领取。\n留言：「" + message + "」",
    lists: [
      {label: "玩家（" + payload.players.length + "）",
       rows: payload.players.map(function (username) {
         return {label: chosen[username].nickname + "（" + username + "）"};
       })},
      {label: "奖励（每人各一份）",
       rows: parts.map(function (part) { return {label: part}; })}],
    ok: "发送"
  });
  if (!ok || !REWARD) { return; }
  toast("发送中……", true);
  var result = await api("/admin/api/reward/send", payload);
  if (bounced(result)) { return; }
  toast(result.message, result.ok);
  if (result.ok) { closeRewardModal(); }
}

/* ======================================================================
   发送记录（用户 2026-09-10 第二轮）

   「玩家仓库」工具条上「发送记录」点开的翻账窗：左栏是发过的每一次
   （发送时间 + 发送者），点一条右栏画那一次的详细；左栏上方一个「清空记录」。
   数据来自 `/admin/api/reward/history`，落在服务端的
   `server/data/gift_history.json`（`server/gifthistory.py`）。

   ★ 整窗**只读** —— 除了「清空记录」不改任何东西，所以点遮罩 / Esc 都能关。
   ★ 时间是**服务端**格式化好的 `time_text`：管理员和服务器不一定在同一个时区，
     而「那一次是几点发的」说的是服务器上的几点（数据备份页同一个口径）。
   ====================================================================== */
var HISTORY = null;   // {records: [...], picked: 记录 id}

async function openHistoryModal() {
  HISTORY = {records: [], picked: null};
  $("historyModal").classList.remove("hidden");
  $("historyCount").textContent = "";
  $("historyTally").textContent = "读取中……";
  $("historyList").textContent = "";
  renderHistoryDetail();
  await loadHistory();
}

function closeHistoryModal() {
  HISTORY = null;
  $("historyModal").classList.add("hidden");
  $("historyList").textContent = "";
  $("historyDetail").textContent = "";
}

/** 读一遍记录。`keep` = 读完还想停在哪一条（清空之后传 null）。 */
async function loadHistory(keep) {
  if (!HISTORY) { return false; }
  var result = await api("/admin/api/reward/history");
  if (bounced(result)) { return false; }
  if (!HISTORY) { return false; }              // 等回包期间弹窗被关了
  if (!result.ok) {
    toast((result && result.message) || "读不到发送记录", false);
    return false;
  }
  HISTORY.records = result.records || [];
  // 默认停在最新的那一条 —— 打开就想看的十有八九是刚发的那次。
  var want = keep === undefined ? (HISTORY.records[0] || {}).id : keep;
  HISTORY.picked = historyById(want) ? want : ((HISTORY.records[0] || {}).id || null);
  $("historyCount").textContent = HISTORY.records.length
    ? HISTORY.records.length + " 次发送" : "";
  $("historyTally").textContent = HISTORY.records.length
    ? "共 " + HISTORY.records.length + " 次（最多留 " + result.max + " 次）"
    : "还没有记录";
  renderHistoryList();
  renderHistoryDetail();
  return true;
}

/** 一条记录的「发送者」写成什么（用户 2026-09-13）。
 *
 * 有昵称（= 他有同名玩家账号）写「大炮（admin）」，没有就只写账号名。
 * ★ 昵称是服务端**读记录时现查的最新那一份**，记录文件里没存它（D96）——
 *   同一条记录今天和明天可能写着不同的昵称，那是对的：它回答的是
 *   「这是谁」，而人今天叫什么才认得出来。
 */
function historySender(row) {
  var name = row.sender || "";
  if (!name) { return "（不详）"; }
  return row.sender_nickname ? row.sender_nickname + "（" + name + "）" : name;
}

function historyById(id) {
  var found = null;
  (HISTORY.records || []).forEach(function (row) {
    if (row.id === id) { found = row; }
  });
  return found;
}

function renderHistoryList() {
  var host = $("historyList");
  var keepTop = host.scrollTop;
  host.textContent = "";
  if (!HISTORY.records.length) {
    host.appendChild(el("div", "own-empty", "还没发过奖励"));
    return;
  }
  HISTORY.records.forEach(function (row) {
    var line = el("div", "history-row" + (row.id === HISTORY.picked ? " on" : ""));
    line.appendChild(el("div", "when", row.time_text || row.id));
    line.appendChild(el("div", "who", "发送者：" + historySender(row)));
    line.appendChild(el("div", "what", historyGist(row)));
    line.onclick = function () {
      HISTORY.picked = row.id;
      renderHistoryList();
      renderHistoryDetail();
    };
    host.appendChild(line);
  });
  host.scrollTop = keepTop;
}

/** 列表里那一行的第三句：「3 名玩家 · 5 样奖励」。 */
function historyGist(row) {
  return (row.players || []).length + " 名玩家 · "
       + (row.rewards || []).length + " 样奖励";
}

/** 一样奖励的名字。物品名存的是**发送那一刻**的（服务端写死在记录里），
    不拿 `itemName()` 现查 —— 那件东西今天可能已经改名或从物品库里删了。 */
function historyRewardName(reward) {
  if (reward.kind === "exp") { return "经验"; }
  if (reward.kind === "money") { return "金币"; }
  return reward.name || ("#" + reward.id);
}

/** 「奖励物品」那一格：图标 + 名字 + ×N。 */
function historyRewardNode(reward) {
  var box = el("div", "own");
  if (reward.kind === "item") {
    box.appendChild(slotNode(reward.id, 26, false, false));
  } else {
    // 经验 / 金币在礼物里走的是**凭证**（D76）：物品表里根本不存在这两个 id，
    // 图集里自然也没有图标 —— 拿一个字顶上，别画成「?」让人以为图挂了。
    var slot = el("div", "slot");
    slot.style.width = "30px";
    slot.style.height = "30px";
    slot.appendChild(el("div", "noicon zh", reward.kind === "exp" ? "经" : "币"));
    box.appendChild(slot);
  }
  var col = el("div", "col");
  col.appendChild(el("div", "nmz", historyRewardName(reward)));
  col.appendChild(el("div", "meta", reward.kind === "item" ? "#" + reward.id : "每人一份"));
  box.appendChild(col);
  box.appendChild(el("span", "fixed", "×" + reward.count));
  return box;
}

/** 名单里的一个人。发失败 / 有跳过的，把原因写在名字后面。 */
function historyPlayerChip(player) {
  var bad = !player.ok || !player.gifts;
  var chip = el("span", "chip" + (bad ? " bad" : ""),
                (player.nickname || player.username) + "（" + player.username + "）");
  var note = "";
  if (!player.ok) {
    note = "没发出去：" + (player.error || "原因不详");
  } else if (!player.gifts) {
    note = "一份都没发（全被跳过）";
  } else if ((player.skipped || []).length) {
    note = "跳过 " + player.skipped.map(function (item) {
      return item.name || ("#" + item.id);
    }).join("、");
  }
  if (note) { chip.appendChild(el("span", "note", "· " + note)); }
  return chip;
}

function renderHistoryDetail() {
  var host = $("historyDetail");
  host.textContent = "";
  var row = HISTORY && HISTORY.picked ? historyById(HISTORY.picked) : null;
  if (!row) {
    host.appendChild(el("div", "own-empty",
                        HISTORY && HISTORY.records.length
                          ? "点左边的一条记录看详细"
                          : "还没有发送记录 —— 发过一次「批量发送奖励」就会记在这里"));
    return;
  }
  var meta = el("dl", "history-meta");
  [["发送时间", row.time_text || row.id],
   ["发送者", historySender(row)],
   ["留言", row.message || ""],
   ["结果", row.summary || ""]].forEach(function (pair) {
    if (!pair[1]) { return; }
    meta.appendChild(el("dt", null, pair[0]));
    meta.appendChild(el("dd", null, pair[1]));
  });
  host.appendChild(meta);

  var players = row.players || [];
  var block = el("div", "history-sec");
  var head = el("h3", null, "奖励玩家名单");
  head.appendChild(el("span", "n", "（" + players.length + " 人）"));
  block.appendChild(head);
  var chips = el("div", "chips");
  if (!players.length) { chips.appendChild(el("div", "own-empty", "（没有名单）")); }
  players.forEach(function (player) { chips.appendChild(historyPlayerChip(player)); });
  block.appendChild(chips);
  host.appendChild(block);

  var rewards = row.rewards || [];
  var items = el("div", "history-sec");
  var head2 = el("h3", null, "奖励物品");
  head2.appendChild(el("span", "n", "（" + rewards.length + " 样，每人各一份）"));
  items.appendChild(head2);
  var grid = el("div", "own-grid");
  if (!rewards.length) { grid.appendChild(el("div", "own-empty", "（没有奖励）")); }
  rewards.forEach(function (reward) { grid.appendChild(historyRewardNode(reward)); });
  items.appendChild(grid);
  host.appendChild(items);
  host.scrollTop = 0;
}

/** 「清空记录」：先弹确认框（红钮）。★ 只删记录，礼物盒里的东西一件都不动。 */
async function clearHistory() {
  if (!HISTORY) { return; }
  if (!HISTORY.records.length) { toast("本来就没有记录", true); return; }
  var ok = await ask({
    title: "清空发送记录",
    lead: "把保存的 " + HISTORY.records.length + " 条发送记录全部删掉，删了找不回来。\n"
          + "已经发出去的礼物不受影响 —— 它们还在玩家的礼物盒里。",
    ok: "清空", danger: true
  });
  if (!ok || !HISTORY) { return; }
  var result = await api("/admin/api/reward/history/clear", {});
  if (bounced(result)) { return; }
  toast(result.message, result.ok);
  if (result.ok) { await loadHistory(null); }
}

/** 物品表（`/admin/api/catalog`）：登录后拿一次，玩家弹窗的刷新再拿一次。 */
async function loadCatalog() {
  var result = await api("/admin/api/catalog");
  if (bounced(result)) { return false; }
  if (!result.ok) { toast(result.message, false); return false; }
  CAT = result;
  BYID = {};
  CAT.items.forEach(function (item) { BYID[item.id] = item; });
  return true;
}

/* ======================================================================
   装备卖出（用户 2026-09-12）

   左右分区：左边购物车（金额实时变），右边自己的仓库。筛选那三套
   （两行分类标签 / 角色下拉 / 上架状态下拉）和「修改仓库」弹窗**完全共用**
   —— `paintCatTabs` / `fillSelect` / `dropdownsMatch` 一个都没重写。

   ## 三件和别处不一样的事

   1. **这一页普通玩家也能写**（整个 `/admin` 里唯一一处）。前台不做任何
      「卖谁的」判断 —— 服务端从会话令牌推目标账号，接口连 `name` 都不收。
   2. **不是「补丁 + 快照」那套**（`#playerModal` 是）。购物车是个临时的
      `{itemId: 数量}`，不落盘、刷新就没；真正的账在服务端现算。
   3. **金额两行实时变**，但**合成装备退的是材料实物**，不折成金币 ——
      所以下面单独有一块「将返还的材料」（用户 2026-09-12 拍板）。
   ====================================================================== */

//: 整页状态。`null` = 还没进过这一页。
//  {view, rows, quotes, prices, canEditPrices, moneyMax, locked, reason,
//   inMatch, cart: {itemId: 数量}, tab: {big, sub}, filter: {character, listing}}
var SELL = null;

//: 价格设置弹窗。{meta, base, edit}
var SELL_PRICES = null;

//: 数量小弹窗。{id, max}
var SELL_QTY = null;

/** 这件东西的报价（服务端算好的）；没有就给一份「卖不掉」。 */
function sellQuote(itemId) {
  var found = SELL && SELL.quotes ? SELL.quotes[itemId] : null;
  return found || {sellable: false, unit: 0, materials: {}, source: "",
                   reason: "还没拿到这件东西的报价"};
}

/** 浮窗和卡片上那句「卖出单价」。 */
function sellUnitText(itemId) {
  var quote = sellQuote(itemId);
  if (!quote.sellable) { return quote.reason || "暂不可出售"; }
  var back = Object.keys(quote.materials || {});
  if (quote.source === "recipe") {
    return quote.unit + " 金币 ＋ 退回 " + back.length + " 种材料";
  }
  return quote.unit + " 金币";
}

/** 仓库里一共有几个（两桶合成一张表，和游戏仓库界面一个口径）。 */
function sellOwned(itemId) {
  var rows = (SELL && SELL.rows) || [];
  for (var i = 0; i < rows.length; i += 1) {
    if (rows[i].id === itemId) { return rows[i].count; }
  }
  return 0;
}

function sellRowOf(itemId) {
  var rows = (SELL && SELL.rows) || [];
  for (var i = 0; i < rows.length; i += 1) {
    if (rows[i].id === itemId) { return rows[i]; }
  }
  return null;
}

/** 还剩几个可以往车里放 = 拥有量 − 已经在车里的。 */
function sellLeft(itemId) {
  return sellOwned(itemId) - (Number(SELL.cart[itemId]) || 0);
}

/** 车里这一单的总账：`{money, returned, lines}`。 */
function sellTotals() {
  var money = 0;
  var returned = {};
  var lines = [];
  Object.keys(SELL.cart).forEach(function (key) {
    var itemId = Number(key);
    var count = Number(SELL.cart[key]) || 0;
    if (count <= 0) { return; }
    var quote = sellQuote(itemId);
    money += quote.unit * count;
    Object.keys(quote.materials || {}).forEach(function (mid) {
      var n = quote.materials[mid] * count;
      returned[mid] = (returned[mid] || 0) + n;
    });
    lines.push({id: itemId, count: count, money: quote.unit * count});
  });
  lines.sort(function (a, b) { return a.id - b.id; });
  return {money: money, returned: returned, lines: lines};
}

function sellFilteredRows() {
  return ((SELL && SELL.rows) || []).filter(function (row) {
    return dropdownsMatch(row.id, SELL.filter);
  });
}

function renderSellTabs() {
  var rows = sellFilteredRows();
  paintCatTabs($("sellCats"), $("sellSubCats"), SELL.tab, function (id) {
    return rows.filter(function (row) {
      return whMatches(id, whCategory(row.id));
    }).length;
  }, repaintSell);
}

/** 右栏一格：图标 + 名字 + （#id · 余 N）+ 「出售」钮。
 *
 * ★★ **卡片上不写单价**（用户 2026-09-12 第三轮）。原来右边钉着一列
 *    96 px 的单价文字，格子因此只能一行一件、名字还是被切断 ——
 *    而同一句话浮窗里本来就有（`paintTip` 最后那一行）。拿掉之后这一格
 *    就跟「修改仓库」那种小格子一样宽，一行摆得下三格。
 * ★ 余量塞进第二行小字，不单开一列：它是「还能再放几个进车」，
 *   属于这件东西的注脚，不是一个要对齐的数值列。
 */
function sellBagNode(row) {
  var left = sellLeft(row.id);
  var quote = sellQuote(row.id);
  var box = el("div", "own" + (left > 0 && quote.sellable ? "" : " off"));
  box.appendChild(slotNode(row.id, 26, false, false));

  var col = tipFor(el("div", "col"), row.id);
  col.appendChild(el("div", "nmz", itemName(row.id)));
  var meta = el("div", "meta", "#" + row.id);
  if (row.stackable) {
    meta.appendChild(el("span", "sep", "·"));
    meta.appendChild(el("span", null, "余 " + left + " / " + row.count));
  }
  if (row.equipped) {
    // 穿着的照样能卖 —— 服务端会先把它脱下来（用户 2026-09-12）。
    var worn = el("span", "worn", "穿着");
    worn.title = "卖掉时会自动脱下";
    meta.appendChild(worn);
  }
  col.appendChild(meta);
  box.appendChild(col);

  var add = el("button", "btn btn-sm btn-primary sell-add", "出售");
  if (!quote.sellable) {
    add.disabled = true;
    add.title = quote.reason || "这件东西暂时不可出售";
  } else if (left <= 0) {
    add.disabled = true;
    add.title = "已经全部放进待卖出了";
  } else {
    add.title = sellUnitText(row.id);
    add.onclick = function () {
      // 装备类只有一件，不用问数量；材料弹小窗设数量。
      if (row.stackable) { openSellQty(row.id); }
      else { addToCart(row.id, 1); }
    };
  }
  box.appendChild(add);
  return box;
}

function renderSellBag() {
  var host = $("sellOwnedGrid");
  var keep = host.parentNode.scrollTop;       // 重画不动滚动条（D37b）
  host.textContent = "";
  var want = tabRequested(SELL.tab);
  var rows = sellFilteredRows().filter(function (row) {
    return whMatches(want, whCategory(row.id));
  });
  if (!rows.length) {
    host.appendChild(el("div", "own-empty", "这一格里没有东西"));
  }
  rows.forEach(function (row) { host.appendChild(sellBagNode(row)); });
  host.parentNode.scrollTop = keep;
}

/** 左栏一格：图标 + 名字 + （×数量 · 小计）+ ✕ 退回。
 *
 * ★ 和右栏同一个道理（见 `sellBagNode`）：数量和小计进第二行小字，
 *   右边只留那颗 ✕ —— 购物车那一栏只有 380 宽，钉两列数值就没名字了。
 */
function sellCartNode(line) {
  var box = el("div", "own");
  box.appendChild(slotNode(line.id, 26, false, false));
  var col = tipFor(el("div", "col"), line.id);
  col.appendChild(el("div", "nmz", itemName(line.id)));
  var meta = el("div", "meta", "×" + line.count);
  meta.appendChild(el("span", "sep", "·"));
  meta.appendChild(el("span", "gain", line.money + " 金币"));
  col.appendChild(meta);
  box.appendChild(col);
  var drop = el("button", "btn btn-sm btn-danger drop", "✕");
  drop.title = "退回仓库";
  drop.onclick = function () {
    delete SELL.cart[line.id];
    repaintSell();
  };
  box.appendChild(drop);
  return box;
}

/** 「将返还的材料」里的一枚（只看，点不动）⇒ 画成 `.chip`，不是仓库卡片。 */
function sellReturnNode(itemId, count) {
  var chip = tipFor(el("span", "chip"), itemId);
  chip.appendChild(slotNode(itemId, 18, false, false));
  chip.appendChild(el("span", null, itemName(itemId)));
  chip.appendChild(el("span", "qty", "×" + count));
  return chip;
}

function renderSellCart() {
  var totals = sellTotals();
  var host = $("sellCartGrid");
  var keep = host.parentNode.scrollTop;       // 重画不动滚动条（D37b）
  host.textContent = "";
  if (!totals.lines.length) {
    host.appendChild(el("div", "own-empty",
                        "还没选东西 —— 在右边点「出售」放进来"));
  }
  totals.lines.forEach(function (line) {
    host.appendChild(sellCartNode(line));
  });
  host.parentNode.scrollTop = keep;

  var back = Object.keys(totals.returned);
  $("sellReturn").classList.toggle("hidden", !back.length);
  var backHost = $("sellReturnGrid");
  backHost.textContent = "";
  back.map(Number).sort(function (a, b) { return a - b; })
    .forEach(function (itemId) {
      backHost.appendChild(sellReturnNode(itemId, totals.returned[itemId]));
    });

  // 金额两行。★ 「卖出后金币」要跟着服务端那条 int32 上限钳一次，
  //   免得画面上写着一个玩家永远拿不到的数（见 account_store.MONEY_MAX）。
  var before = SELL.view ? SELL.view.money : 0;
  var after = Math.min(before + totals.money, SELL.moneyMax);
  var sum = $("sellSum");
  sum.textContent = "";
  var gain = el("div", "sell-sum-row");
  gain.appendChild(el("span", "lab", "卖出得金币"));
  gain.appendChild(el("b", null, String(totals.money)));
  sum.appendChild(gain);
  var wallet = el("div", "sell-sum-row");
  wallet.appendChild(el("span", "lab", "卖出后金币"));
  wallet.appendChild(el("b", null, before + " → " + after));
  sum.appendChild(wallet);
  if (before + totals.money > after) {
    sum.appendChild(el("div", "sell-warn",
                       "金币已经到上限，超出的 "
                       + (before + totals.money - after) + " 不会入账"));
  }
  if (back.length) {
    // 下面紧挨着就是那一排材料 chip ⇒ 这里只要说清「金额里不含它们」。
    sum.appendChild(el("div", "sell-note",
                       "合成得来的装备退回配方材料，上面的金额里不含它们"));
  }
  $("sellConfirm").disabled = !totals.lines.length || SELL.locked;
}

function repaintSell() {
  renderSellTabs();
  renderSellBag();
  renderSellCart();
}

function renderSell() {
  // 遮罩只盖左右分区那一块（它是 `.sell-split` 的孩子），工具条永远露着。
  $("sellLock").classList.toggle("hidden", !SELL.locked);
  $("sellLockText").textContent = SELL.reason || "";
  // ★★ 这颗钮**三档身份都看得见**（用户 2026-09-12：玩家只读）——
  //   玩家点开是一张锁住的价格表，正好回答他「我这东西凭什么卖这个价」。
  //   能不能改由弹窗自己按 `can_edit` 决定（`openSellPrices`），
  //   真正的门在服务端的 `_require_editor()`。
  $("sellPriceBtn").title = SELL.canEditPrices
    ? "设置各类材料的卖出单价和装备的卖出百分比"
    : "看一眼各类东西按什么价收（改价是管理员的事）";
  if (SELL.locked) {
    $("sellWho").textContent = "";
    $("sellNick").textContent = "-";
    $("sellLevel").textContent = "-";
    $("sellMoney").textContent = "-";
    // ★ 锁上也要**把两边重画一遍**（`rows` / `cart` 这时都是空的）。
    //   不画的话上一个人的仓库原封不动留在 DOM 里 —— 遮罩只有 .92 不透明，
    //   底下那些格子隐约看得见，而那可能是**另一个账号**的东西
    //   （玩家退出、管理员登进来的那一瞬间就是这条路）。
    repaintSell();
    $("sellConfirm").disabled = true;
    return;
  }
  var view = SELL.view;
  // 在线时把**他现在在哪**一起写出来（用户 2026-09-13 第二轮）。
  // ★ 不在线时这一截整个不画 —— 标题栏那一格答的是「在卖谁的东西」，
  //   「不在线」对卖东西这件事没有意义（对局中不让卖，那是另一条判据）。
  $("sellWho").textContent = view.nickname + "（" + view.username + "）"
    + (view.online ? " · " + placeText(view, true) : "");
  $("sellNick").textContent = view.nickname;
  $("sellLevel").textContent = view.level;
  $("sellMoney").textContent = view.money;
  fillSelect($("sellCharacter"), "全部角色", characterOptions(),
             SELL.filter.character);
  fillSelect($("sellListing"), "全部上架状态", LISTING_FILTER_OPTIONS,
             SELL.filter.listing);
  repaintSell();
}

/** 服务端那份 `state`（或**卖出回执**）→ 整页状态。
 *
 * ★★ 这是一次**整页赋值**：`result` 里少哪个键，页面上那一项当场变成
 *    默认值。所以服务端那两处回的是**同一组键** —— 回执由
 *    `_sell_state_payload()` 起头，再补上这一单的账
 *    （`test_the_receipt_carries_the_whole_page_state` 钉着）。
 *    别在这儿写「回执没带就留着上一份」那种补丁：那等于把判据交给
 *    「谁记得补哪个键」，而漏掉的症状是静悄悄的 —— 2026-09-12 第三轮
 *    发现的那个就是「管理员卖完一单，改价按钮自己没了」。
 */
function adoptSellState(result, keepCart) {
  var same = SELL && SELL.view && result.player
             && SELL.view.username === result.player.username;
  var cart = (keepCart && same && SELL) ? SELL.cart : {};
  SELL = {
    view: result.player || null,
    rows: [],
    quotes: result.quotes || {},
    prices: result.prices || {},
    canEditPrices: !!result.can_edit_prices,
    moneyMax: Number(result.money_max) || 2147483647,
    locked: !!result.locked,
    reason: result.reason || "",
    inMatch: !!result.in_match,
    cart: cart,
    // 同一个人重读时停在原来的分类和下拉上（照 `adoptPlayer` 的做法）。
    tab: same ? SELL.tab : anyTab(),
    filter: same ? SELL.filter : {character: "", listing: ""}
  };
  if (SELL.view) {
    // ★ 两桶合成一张表，**同一个 id 只留一行**：正常存档里一件东西只会在
    //   一个桶里，但手改过的存档两边都有过 —— 那时 `sellOwned()` 只认得
    //   第一行，画面上却摆着两格，点第二格会一直「余 0」。
    var seen = {};
    function take(row, stackable, equipped) {
      var have = seen[row.id];
      if (have) {
        have.count += row.count;
        have.stackable = have.stackable && stackable;
        have.equipped = have.equipped || equipped;
        return;
      }
      seen[row.id] = {id: row.id, count: row.count,
                      stackable: stackable, equipped: equipped};
      SELL.rows.push(seen[row.id]);
    }
    (SELL.view.materials || []).forEach(function (row) {
      take(row, true, false);
    });
    (SELL.view.inventory || []).forEach(function (row) {
      take(row, row.stackable !== false, !!row.equipped);
    });
    // ★ 不可堆叠的东西只有「有 / 没有」（§28）：存档里躺着 ×2 的老账
    //   （早先用控制通道 `give` 发过两次）也只算一件 —— 服务端 `bundle()`
    //   就是这么算钱的，画面上摆 ×2 只会让人以为能卖两份钱。
    SELL.rows.forEach(function (row) {
      if (!row.stackable) { row.count = 1; }
    });
    SELL.rows.sort(function (a, b) { return a.id - b.id; });
  }
  // 车里留着的东西可能已经不在仓库里了（他在游戏里用掉了）——
  // 按现有存量夹一下，夹到 0 的整条去掉。
  Object.keys(SELL.cart).forEach(function (key) {
    var owned = sellOwned(Number(key));
    if (owned <= 0) { delete SELL.cart[key]; }
    else if (SELL.cart[key] > owned) { SELL.cart[key] = owned; }
  });
  renderSell();
  if (!SELL.locked && SELL.inMatch) {
    toast("你现在正在游戏对局里 —— 可以先挑，但要打完这一局才卖得掉。", false);
  }
}

async function loadSellState(keepCart) {
  var result = await api("/admin/api/sell/state");
  if (bounced(result)) { return false; }
  if (!result.ok) { toast(result.message, false); return false; }
  adoptSellState(result, keepCart);
  return true;
}

/** 「↻ 刷新」—— 和别处那几个刷新钮一个手感（用户 2026-09-12）：
 *  先「刷新中……」，成功了说一句「已刷新」。
 *
 * ★ 失败时**不要**盖掉 `loadSellState` 报的那句错 —— 「已刷新」压在
 *   「请先登录」上面，人看到的就是「点了刷新，然后什么都没变」
 *   （和 `refreshConfigs` / `refreshAccounts` 同一条规矩）。
 * ★ 对局中那句提醒**并进这一句说**：`toast()` 是单条的，后来的会把
 *   `adoptSellState()` 刚弹的那句盖掉 —— 分开发的话回执反而把提醒吞了。
 */
async function refreshSell() {
  toast("刷新中……", true);
  if (!(await loadSellState(true))) { return; }
  if (SELL.locked) {
    toast("已刷新。这一页没有可操作的仓库，原因见页面上那句话。", true);
    return;
  }
  toast("已刷新：仓库和卖出价格都是最新的。"
        + (SELL.inMatch
           ? "★ 你现在正在游戏对局里，可以先挑，但要打完这一局才卖得掉。"
           : ""),
        !SELL.inMatch);
}

function addToCart(itemId, count) {
  var left = sellLeft(itemId);
  count = Math.max(0, Math.min(count, left));
  if (!count) { return; }
  SELL.cart[itemId] = (Number(SELL.cart[itemId]) || 0) + count;
  repaintSell();
}

/* ----------------------------------------------------- 数量小弹窗
   管理页原来没有这种窗：别处的数量都是卡片里的内联输入框，而这儿一行
   只有一颗「出售」钮，没地方常驻一个输入框。 */

function openSellQty(itemId) {
  var left = sellLeft(itemId);
  if (left <= 0) { return; }
  SELL_QTY = {id: itemId, max: left};
  $("sellQtyTitle").textContent = itemName(itemId);
  $("sellQtyLab").textContent = "卖出数量（最多 " + left + "）";
  var input = $("sellQtyInput");
  input.max = String(left);
  input.value = String(left);
  var range = $("sellQtyRange");
  range.max = String(left);
  range.value = String(left);
  $("sellQtyMax").textContent = String(left);
  // 只有一个可卖 ⇒ 整条藏起来：一根拖不动的滑杆看着像坏的。
  $("sellQtySlider").classList.toggle("hidden", left <= 1);
  paintSellQtySum();
  $("sellQtyModal").classList.remove("hidden");
  input.focus();
  input.select();
}

function sellQtyValue() {
  if (!SELL_QTY) { return 0; }
  var raw = Math.floor(Number($("sellQtyInput").value) || 0);
  return Math.max(1, Math.min(raw, SELL_QTY.max));
}

/** 把当前数量写回**两个入口**（数字框 / 拖动条）并重画合计。
 *
 * ★ 数字框是那个数的**唯一出处**（`sellQtyValue()` 只读它），拖动条只是
 *   另一个改法 —— 两边各存一份的话，一个改了另一个没跟上，最后按「加入
 *   待卖出」时谁说了算就成了看运气。
 * `from` = 刚才动的是谁，别把正在输入的那一格改掉（人打到一半会跳）。
 */
function syncSellQty(from) {
  if (!SELL_QTY) { return; }
  if (from === "range") {
    // ★ 拨钮是**连续**走的（`step="any"`）—— 这里取整，让数字跳、拨钮不跳。
    //   不要反过来把拨钮拉回整数位：那正是「一顿一顿」的来源。
    $("sellQtyInput").value = String(
      Math.max(1, Math.min(Math.round(Number($("sellQtyRange").value) || 1),
                           SELL_QTY.max)));
  } else {
    // 人手敲了一个确切的数 ⇒ 拨钮就该停在那个刻度上。
    $("sellQtyRange").value = String(sellQtyValue());
  }
  paintSellQtySum();
}

function paintSellQtySum() {
  if (!SELL_QTY) { return; }
  var quote = sellQuote(SELL_QTY.id);
  $("sellQtySum").textContent =
    "单价 " + quote.unit + " 金币　合计 "
    + (quote.unit * sellQtyValue()) + " 金币";
}

function closeSellQty() {
  SELL_QTY = null;
  $("sellQtyModal").classList.add("hidden");
}

function confirmSellQty() {
  if (!SELL_QTY) { return; }
  var itemId = SELL_QTY.id;
  var count = sellQtyValue();
  closeSellQty();
  addToCart(itemId, count);
}

/* ----------------------------------------------------- 确定卖出 */

async function doSell() {
  if (!SELL || SELL.locked) { return; }
  var totals = sellTotals();
  if (!totals.lines.length) { return; }
  var before = SELL.view.money;
  var after = Math.min(before + totals.money, SELL.moneyMax);
  var lists = [{
    label: "卖出的物品（" + totals.lines.length + "）",
    rows: totals.lines.map(function (line) {
      return {label: itemName(line.id) + " ×" + line.count,
              reason: line.money + " 金币"};
    })
  }];
  var back = Object.keys(totals.returned).map(Number)
    .sort(function (a, b) { return a - b; });
  if (back.length) {
    lists.push({
      label: "将返还的材料（" + back.length + " 种）",
      rows: back.map(function (itemId) {
        return {label: itemName(itemId) + " ×" + totals.returned[itemId]};
      })
    });
  }
  var ok = await ask({
    title: "确认卖出",
    lead: "一共卖出 " + totals.lines.length + " 件，得 " + totals.money
          + " 金币。\n卖出后金币：" + before + " → " + after
          + "\n★ 卖掉的东西拿不回来。",
    lists: lists,
    ok: "卖出",
    danger: true
  });
  if (!ok) { return; }
  var result = await api("/admin/api/sell", {
    // ★ 只发清单。「卖谁的」由服务端从会话令牌推 —— 这里连用户名都不该发。
    items: totals.lines.map(function (line) {
      return {id: line.id, count: line.count};
    })
  });
  if (bounced(result)) { return; }
  if (!result.ok) { toast(result.message, false); return; }
  SELL.cart = {};
  adoptSellState(result, false);
  toast(result.message, true);
}

/* ----------------------------------------------------- 卖出价格设置 */

function sellPricesDirty() {
  if (!SELL_PRICES) { return false; }
  return Object.keys(SELL_PRICES.edit).some(function (key) {
    return Number(SELL_PRICES.edit[key]) !== Number(SELL_PRICES.base[key]);
  });
}

function paintSellPriceDirty() {
  var dirty = sellPricesDirty();
  var node = $("sellPriceDirty");
  node.textContent = dirty ? "有未保存的修改" : "没有未保存的修改";
  node.className = "dirty" + (dirty ? "" : " clean");
  // 改过的那一档套上「改过了」那一圈（用户 2026-09-14）。这一窗最多十来格，
  // 但档位名字（「特殊材料」「高级材料」）差一个字，不标出来真会存错档。
  if (!SELL_PRICES) { return; }
  Array.prototype.forEach.call(
    $("sellPriceBody").querySelectorAll("[data-price-key]"), function (row) {
      var key = row.getAttribute("data-price-key");
      row.classList.toggle(
        "edited",
        Number(SELL_PRICES.edit[key]) !== Number(SELL_PRICES.base[key]));
    });
}

/** 一个价格格子：一行「标签 + 输入框 + 单位」，底下一行「这一档装着什么」。
 *
 * ★ 那行小字封成两行（CSS 的 `-webkit-line-clamp`），全文进 `title` ——
 *   卡片那一类有 17 个名字，任它长的话这一格能撑到六行，两列的高度全带歪。
 */
function sellPriceField(key, label, suffix, names, max) {
  var wrap = el("div", "sell-price-row");
  wrap.setAttribute("data-price-key", key);   // 「改过了」那一圈按它找格子
  var field = el("div", "field");
  field.appendChild(el("span", "lab", label));
  var input = document.createElement("input");
  input.type = "number";
  input.min = "0";
  input.step = "1";
  if (max) { input.max = String(max); }
  input.value = String(SELL_PRICES.edit[key]);
  // ★ 只读身份（玩家）：数就摆在原来的位置上，只是改不动 —— 和配置页
  //   `lockList()` 一个口径（用户要的是「看得见、改不了」，不是换成一行字）。
  input.disabled = !SELL_PRICES.canEdit;
  input.oninput = function () {
    // ★★ **就地钳**（而不是只在保存时报错）：百分比的上限是 100
    //   （`sellprice.PERCENT_MAX`，用户 2026-09-12 —— 卖价不许高过买价）。
    //   钳完写回输入框，所见即将要存下去的那个数；只在保存那一刻报错的话，
    //   人得先填完一屏才知道哪一格不行。服务端那一道校验一点没松。
    var value = Math.max(0, Math.floor(Number(input.value) || 0));
    if (max) { value = Math.min(value, max); }
    if (String(value) !== input.value) { input.value = String(value); }
    SELL_PRICES.edit[key] = value;
    paintSellPriceDirty();
  };
  field.appendChild(input);
  if (suffix) { field.appendChild(el("span", "suffix", suffix)); }
  wrap.appendChild(field);
  if (names) {
    var note = el("div", "sell-price-names", names);
    note.title = names;
    wrap.appendChild(note);
  }
  return wrap;
}

/** 一组（材料 / 装备 / 其他）：小标题 + 价格格子。返回那张格子网格。 */
function sellPriceGroup(host, title) {
  var group = el("div", "sell-price-group");
  group.appendChild(el("div", "sell-head", title));
  var grid = el("div", "sell-price-grid");
  group.appendChild(grid);
  host.appendChild(group);
  return grid;
}

function renderSellPrices() {
  var meta = SELL_PRICES.meta;
  var host = $("sellPriceBody");
  host.textContent = "";

  var mats = sellPriceGroup(host, "材料");
  (meta.classes || []).forEach(function (key) {
    var ids = (meta.groups && meta.groups[key]) || [];
    // ★ 把名字列出来：管理员照着一眼能看见「不死鸟之羽 / 之泪」归在特殊档里
    //   （按 id 开头猜正好会猜错）。
    var names = ids.map(itemName).join("、");
    mats.appendChild(sellPriceField(
      key, (meta.labels && meta.labels[key]) || key, "金币 / 个",
      ids.length ? (ids.length + " 种：" + names) : "（没有物品归在这一类）"));
  });

  // ★★ 「装备」和「其他」是**并列的两组、摆在同一行左右**（用户 2026-09-12）。
  //    并列是因为它们本来就是 `sellprice.quote()` 四条分支里各自独立的一条
  //    —— 「其他」是「既没上架商店、也没有配方」那 148 件（称号 / 礼包 /
  //    钥匙 / 消耗品）的兜底价，不是装备的一种；同一行是因为两组各只有
  //    一个数，上下摞着白占一屏。
  var pair = el("div", "sell-price-pair");
  host.appendChild(pair);

  sellPriceGroup(pair, "装备").appendChild(sellPriceField(
    "equip_percent", "卖出价 = 买入价 ×", "%",
    "商店在卖的按买入价算；靠合成得来的按配方的金币花费算，"
    + "并且把配方里的材料原样退回仓库。",
    meta.percent_max));

  sellPriceGroup(pair, "其他").appendChild(sellPriceField(
    "other_price", "其他物品", "金币 / 个",
    "既没上架商店、也没有合成配方的那些东西（比如称号、礼包、钥匙、消耗品）。"));

  // 「有未保存的修改 / 放弃修改 / 保存」整组：只读身份下收起来 ——
  // 一个只能看的人，这三样没有一样是有意义的（工具条左边那句话是写死的，
  // 两种身份看到的是同一句）。
  $("sellPriceActs").classList.toggle("hidden", !SELL_PRICES.canEdit);
  paintSellPriceDirty();
}

async function openSellPrices() {
  var result = await api("/admin/api/sell/prices");
  if (bounced(result)) { return; }
  if (!result.ok) { toast(result.message, false); return; }
  // ★ 「能不能改」由**这一发自己**回（`can_edit`），不去借卖出页那个
  //   `can_edit_prices` —— 借来的字段总有「那边改口这边忘了跟」的一天。
  //   两边都只是画面，真正的门是服务端的 `_require_editor()`。
  SELL_PRICES = {meta: result, base: result.prices,
                 canEdit: !!result.can_edit,
                 edit: JSON.parse(JSON.stringify(result.prices))};
  renderSellPrices();
  $("sellPriceModal").classList.remove("hidden");
}

async function closeSellPrices() {
  if (sellPricesDirty()) {
    var ok = await ask({title: "还有没保存的改动",
                        lead: "卖出价格还有没保存的改动，确定丢掉？",
                        ok: "丢掉"});
    if (!ok) { return; }
  }
  SELL_PRICES = null;
  $("sellPriceModal").classList.add("hidden");
}

async function saveSellPrices() {
  if (!SELL_PRICES) { return; }
  var result = await api("/admin/api/sell/prices",
                         {prices: SELL_PRICES.edit});
  if (bounced(result)) { return; }
  toast(result.message, result.ok);
  if (!result.ok) { return; }
  SELL_PRICES.base = result.prices;
  SELL_PRICES.edit = JSON.parse(JSON.stringify(result.prices));
  renderSellPrices();
  // 价格一改，卖出页上每一行的单价和车里的小计都过期了 —— 重新问一次。
  // ★ 车里的东西留着（他只是改了价，没打算把挑好的东西丢掉）。
  if (SELL) { await loadSellState(true); }
}

/* ======================================================================
   自定义属性弹窗（X3，用户 2026-09-19）

   从物品库里武器卡片的「自定义属性」打开。服务端 `GET /admin/api/weapon?id=`
   一发回全部：字段表（参考值 + 两套当前覆盖）、说明文、游戏里会显示的预览。
   模型 = `WEAPON.edit`（`{pve: {}, pvp: {}, desc: ""}`），`WEAPON.base` 是落盘那份
   的拷贝，脏 = 两份不相等。保存走 `POST /admin/api/weapon`，服务端存完就推给
   在线客户端，回执带新的预览。
   ★ 原版武器：`custom` 为假，两栏整体不画，只剩说明文。
   ====================================================================== */
var WEAPON = null;

function weaponSnapshot(view) {
  var out = {pve: {}, pvp: {}, desc: view.desc || ""};
  (view.fields || []).forEach(function (f) {
    ["pve", "pvp"].forEach(function (mode) {
      if (f[mode] !== null && f[mode] !== undefined) { out[mode][f.key] = f[mode]; }
    });
  });
  return out;
}

function weaponDirty() {
  if (!WEAPON) { return false; }
  return JSON.stringify(WEAPON.edit) !== JSON.stringify(WEAPON.base);
}

function paintWeaponDirty() {
  var dirty = weaponDirty();
  var node = $("weaponDirty");
  node.textContent = dirty ? "有未保存的修改" : "";
  node.classList.toggle("clean", !dirty);
  $("weaponSave").disabled = !WEAPON || !WEAPON.canEdit || !dirty;
}

/** 一格数值：标签 · 输入框 · 单位 · 「参考 N」。空 = 用参考值。 */
function weaponFieldNode(mode, spec) {
  var wrap = el("div", "weapon-field");
  wrap.setAttribute("data-key", spec.key);
  var lab = el("span", "lab", spec.label);
  wrap.appendChild(lab);
  var input = document.createElement("input");
  input.type = "number";
  input.min = String(spec.min);
  input.max = String(spec.max);
  input.step = spec.type === "float" ? "any" : "1";
  var value = WEAPON.edit[mode][spec.key];
  input.value = (value === undefined || value === null) ? "" : String(value);
  input.placeholder = spec.reference === null || spec.reference === undefined
    ? "—" : String(spec.reference);
  input.disabled = !WEAPON.canEdit;
  input.oninput = function () {
    var raw = input.value.trim();
    input.classList.remove("bad");
    if (raw === "") {
      delete WEAPON.edit[mode][spec.key];
    } else {
      var num = Number(raw);
      if (!isFinite(num) || (spec.type !== "float" && String(Math.trunc(num)) !== raw.replace(/^\+/, ""))) {
        // 不擅自改：原样存回去，让服务端那句「要是数字」来说话（同 `fieldNode`）。
        WEAPON.edit[mode][spec.key] = raw;
        input.classList.add("bad");
      } else {
        WEAPON.edit[mode][spec.key] = spec.type === "float" ? num : Math.trunc(num);
      }
    }
    wrap.classList.toggle("edited",
      JSON.stringify(WEAPON.edit[mode][spec.key]) !== JSON.stringify(WEAPON.base[mode][spec.key]));
    paintWeaponDirty();
  };
  wrap.classList.toggle("edited",
    JSON.stringify(WEAPON.edit[mode][spec.key]) !== JSON.stringify(WEAPON.base[mode][spec.key]));
  wrap.appendChild(input);
  if (spec.unit) { wrap.appendChild(el("span", "unit", spec.unit)); }
  var ref = el("span", "ref", "参考 " + (spec.reference === null || spec.reference === undefined
                                          ? "—" : spec.reference));
  ref.title = "资源包里这一把参考的爆裂 3 写的数；留空就用它";
  wrap.appendChild(ref);
  return wrap;
}

function weaponModeNode(mode) {
  var view = WEAPON.view;
  var live = mode === "pvp";
  var box = el("div", "weapon-mode" + (live ? " live" : ""));
  var head = el("div", "sell-head");
  var label = "";
  (view.modes || []).forEach(function (m) { if (m.key === mode) { label = m.label; } });
  head.appendChild(el("span", null, label));
  if (live) { head.appendChild(el("span", "note", "游戏内提示框只显示这一栏")); }
  box.appendChild(head);
  var byKey = {};
  (view.fields || []).forEach(function (f) { byKey[f.key] = f; });
  (view.groups || []).forEach(function (group) {
    var g = el("div", "weapon-group");
    g.appendChild(el("b", null, group.label));
    var rows = el("div", "rows");
    group.keys.forEach(function (key) {
      if (byKey[key]) { rows.appendChild(weaponFieldNode(mode, byKey[key])); }
    });
    g.appendChild(rows);
    box.appendChild(g);
  });
  if (WEAPON.canEdit) {
    var acts = el("div", "acts");
    var reset = el("button", "btn btn-sm", "恢复参考值");
    reset.type = "button";
    reset.title = "把这一栏全部清空 = 全部用资源包里的参考值";
    reset.onclick = function () {
      WEAPON.edit[mode] = {};
      renderWeaponModal();
    };
    acts.appendChild(reset);
    box.appendChild(acts);
  }
  return box;
}

function renderWeaponModal() {
  var view = WEAPON.view;
  var host = $("weaponBody");
  host.textContent = "";
  $("weaponTitle").textContent = "自定义属性 · " + (view.name || itemName(view.id)) + "  #" + view.id;

  if (view.custom) {
    var modes = el("div", "weapon-modes");
    modes.appendChild(weaponModeNode("pve"));
    var copy = el("div", "weapon-copy");
    if (WEAPON.canEdit) {
      var toPvp = el("button", "btn btn-sm", "复制到对战 →");
      toPvp.type = "button";
      toPvp.onclick = function () {
        WEAPON.edit.pvp = JSON.parse(JSON.stringify(WEAPON.edit.pve));
        renderWeaponModal();
      };
      var toPve = el("button", "btn btn-sm", "← 复制到任务");
      toPve.type = "button";
      toPve.onclick = function () {
        WEAPON.edit.pve = JSON.parse(JSON.stringify(WEAPON.edit.pvp));
        renderWeaponModal();
      };
      copy.appendChild(toPvp);
      copy.appendChild(toPve);
    }
    modes.appendChild(copy);
    modes.appendChild(weaponModeNode("pvp"));
    host.appendChild(modes);
  } else {
    host.appendChild(el("div", "weapon-locked",
      "原版武器的数值不可修改（客户端按资源包里的原版数值算），这里只能改说明文。"));
  }

  // ★ 生效时机得写在脸上（用户 2026-09-19）：数值下一局生效、说明文要重登。
  //   不是保守做法留下的遗憾，是有意为之 —— 局内改记录会让准星和实际弹匣对不上
  //   （弹匣容量进图时就快照进持枪器了，§42 / D32）。
  if (view.custom) {
    host.appendChild(el("div", "weapon-when",
      "⏱ 属性改动 下一局 生效，不影响正在进行的对局（保存后会立刻推给在线玩家，"
      + "他们下一局开局时套用）。"));
  }

  var desc = el("div", "field wide weapon-desc");
  desc.appendChild(el("span", "lab", "说明文（游戏提示框的下半段，最多 "
                      + view.desc_max_lines + " 行、" + view.desc_max_chars + " 个字；留空 = 不写）"
                      + "　⏱ 商店提示框即时生效，仓库提示框要重新登录客户端才更新"));
  var area = document.createElement("textarea");
  area.value = WEAPON.edit.desc || "";
  area.disabled = !WEAPON.canEdit;
  area.rows = view.desc_max_lines;
  var count = el("div", "count");
  function paintCount() {
    var text = area.value.replace(/\r/g, "");
    var lines = text ? text.split("\n").length : 0;
    count.textContent = lines + " / " + view.desc_max_lines + " 行　" + text.length + " / " + view.desc_max_chars + " 字";
    count.classList.toggle("over", lines > view.desc_max_lines || text.length > view.desc_max_chars);
  }
  area.oninput = function () {
    WEAPON.edit.desc = area.value.replace(/\r/g, "");
    paintCount();
    paintWeaponDirty();
  };
  desc.appendChild(area);
  desc.appendChild(count);
  paintCount();
  host.appendChild(desc);

  var preview = el("div", "weapon-preview");
  if (view.custom && view.lines) {
    // ★ 两套都列（用户 2026-09-19）：游戏内提示框只画 PVP 那套，这里空间够。
    preview.appendChild(el("b", null, "按已保存的内容现算（游戏内提示框只画对战那一段）："));
    ["pvp", "pve"].forEach(function (mode) {
      var label = "";
      (view.modes || []).forEach(function (m) { if (m.key === mode) { label = m.label; } });
      var block = el("pre", null, "【" + label + "】\n" + (view.lines[mode] || []).join("\n"));
      preview.appendChild(block);
    });
    var note = (view.preview || "").split("|")[1];
    if (note) { preview.appendChild(el("pre", null, note)); }
  } else {
    preview.appendChild(el("b", null, "游戏里会显示（按已保存的内容现算）："));
    var text = view.preview || "";
    if (!text) {
      preview.appendChild(el("div", "empty", "（这件东西没有说明）"));
    } else {
      text.split("|").forEach(function (segment) {
        preview.appendChild(el("pre", null, segment));
      });
    }
  }
  host.appendChild(preview);

  $("weaponActs").classList.toggle("hidden", !WEAPON.canEdit);
  $("weaponSave").classList.toggle("hidden", !WEAPON.canEdit);
  paintWeaponDirty();
}

async function openWeaponModal(itemId) {
  var result = await api("/admin/api/weapon?id=" + encodeURIComponent(itemId));
  if (bounced(result)) { return; }
  if (!result.ok) { toast(result.message, false); return; }
  var base = weaponSnapshot(result);
  WEAPON = {view: result, canEdit: !!result.can_edit,
            base: base, edit: JSON.parse(JSON.stringify(base))};
  renderWeaponModal();
  $("weaponModal").classList.remove("hidden");
}

async function closeWeaponModal() {
  if (!WEAPON) { return; }
  if (weaponDirty()) {
    var ok = await ask({title: "还有没保存的改动",
                        lead: "这把武器的自定义属性 / 说明文还有没保存的改动，确定丢掉？",
                        ok: "丢掉"});
    if (!ok) { return; }
  }
  WEAPON = null;
  $("weaponModal").classList.add("hidden");
}

async function saveWeaponModal() {
  if (!WEAPON || !WEAPON.canEdit) { return; }
  var payload = {id: WEAPON.view.id, desc: WEAPON.edit.desc || ""};
  if (WEAPON.view.custom) { payload.params = {pve: WEAPON.edit.pve, pvp: WEAPON.edit.pvp}; }
  var result = await api("/admin/api/weapon", payload);
  if (bounced(result)) { return; }
  toast(result.message, result.ok);
  if (!result.ok) { return; }
  // 回执就是一份新的视图（含新的预览）—— 直接换掉，不用再 GET 一次。
  var base = weaponSnapshot(result);
  WEAPON.view = result;
  WEAPON.base = base;
  WEAPON.edit = JSON.parse(JSON.stringify(base));
  renderWeaponModal();
  // 浮窗里那段说明文取自 catalog（服务端已经 `invalidate_catalog()`），重取一次。
  await loadCatalog();
}

/* ======================================================================
   登录 / 启动
   ====================================================================== */

/* ---------------------------------------------------------------- 权限
   三档：`system` 系统管理员 = 全部标签页；
        `operator` 运营 = 只有 `CONFIGS` 那几个配置页（D34）；
        `player` 普通玩家 = 同样那几页，但**只读**（D74）。

   ★ 这里做的**只是把标签藏起来、把控件锁上**，不是安全边界 —— 藏掉的按钮
     和锁住的输入框都拦不住直接 POST。真正的门在服务端
     `_require_system_admin()` / `_require_editor()` 里，两边都要有。
   ------------------------------------------------------------------- */
var ROLE = null;              // "system" / "operator" / "player" / null（没登录）

//: 顶栏那句「已登录：…」要写的两样东西（用户 2026-09-13）。
//  `ME.name` 是账号名，`ME.nickname` 是**同名玩家账号**的昵称 ——
//  管理员表里没有昵称，服务端每次现查（`web/admin.py` 的 `_nickname`），
//  没有同名游戏账号就是空串。
//  ★ 存下来是因为顶栏那行字**有两个出处**：登进来时画一次，把自己降成运营
//    之后还要再画一次。那一处原来只手里有个 `name`，昵称不存的话一降权就
//    掉了（同 D74 那句「三档身份的说法只该有一个出处」）。
var ME = {name: "", nickname: ""};

//: 现在停在哪个标签页。★ 和 `CURRENT` 不是一回事 —— `CURRENT` 只记那几个
//  **配置**页（渲染要用），「玩家仓库」「数据管理」「管理员账号」不在里面。
var TAB = "items";

//: 只有系统管理员能进的标签页（数据管理也是：它能回滚玩家存档、能下载整个 logs/）。
var SYSTEM_ONLY_TABS = ["backup", "admins"];

//: **系统管理员 + 运营**都能进、但只读玩家进不去的标签页（用户 2026-09-13）。
//  ★ 为什么要第四张表：「玩家仓库」2026-09-13 开给了运营，它从此
//    既不是系统管理员专页、也不是配置页（没有三方合并那一套）、
//    更不能对只读玩家开着（那一页能改**别人**的等级金币仓库）。
//  ★★ 服务端那一侧的闸必须是 `_require_editor()`，**不是**
//    `_require_admin()` —— 后者只问「登没登录」，只读玩家也过得去。
var EDITOR_TABS = ["players"];

//: **三档身份都能进**的标签页（用户 2026-09-12）。
//  ★ 为什么要第三张表：原来页面上每个标签非「配置页」即「系统管理员专页」，
//    `test_every_tab_in_the_page_is_classified` 就是照这两张表双向核对的。
//    「装备卖出」两头都不是 —— 它不是运营配置（没有三方合并那一套），
//    又必须对普通玩家开着。硬塞进 `CONFIGS` 会让配置页那条链（渲染器 /
//    脏标记 / 三方合并 / 「共几份配置」的文案）全部把它算进去。
//  ★ 这一页**玩家也能写**（卖自己的东西）—— 整个 `/admin` 里唯一一处。
//    安全不靠这张表：服务端从会话令牌推「卖谁的」，接口连 `name` 都不收。
var EVERYONE_TABS = ["sell"];

function isSystemAdmin() { return ROLE === "system"; }

//: 只读身份（D74）。★ 判据是**角色**，不是「有没有某个按钮」—— 页面上
//  凡是「能改东西」的地方都问它，一处一处地判「这个钮该不该画」迟早漏。
function isReadOnly() { return ROLE === "player"; }

function canOpenTab(tab) {
  // ★ 只读玩家看得见的就是**那几个配置页**，照 `CONFIGS` 现取（不是照
  //   `SYSTEM_ONLY_TABS` 取反）：以后加一个既不是配置页、又不属于系统管理员
  //   专档的新标签，取反那种写法会**默认放行**给玩家 —— 白名单不会。
  if (isReadOnly()) {
    return CONFIGS.indexOf(tab) >= 0 || EVERYONE_TABS.indexOf(tab) >= 0;
  }
  // 运营：系统管理员专档之外全放行（`EDITOR_TABS` 自然落在这一侧）。
  return isSystemAdmin() || SYSTEM_ONLY_TABS.indexOf(tab) < 0;
}

/** 按当前权限决定哪几个标签露出来。 */
function applyRoleToTabs() {
  Array.prototype.forEach.call($("tabs").children, function (button) {
    var tab = button.getAttribute("data-tab");
    button.classList.toggle("hidden", !canOpenTab(tab));
  });
  // 「玩家仓库」那条「你改不了仓库」的说明跟着身份走 —— 挂在这儿而不是
  // 切页那一处：把自己降成运营时它当场就该出现（同 `self_demoted` 那条路）。
  paintLockedLockerNote();
  // 权限被现场降级时，人可能正停在一个已经不该看的页上 —— 拉回第一页。
  // ★ 「`CAT` 还没到手就别切」那道保险挪进了 `switchTab`（它最后那一句
  //   `renderCurrent()` 才是要 `CAT` 的）—— 留在这儿的话，登录那一瞬间
  //   面板不会跟着切，人就停在上一个人那一页上了。
  if (!canOpenTab(TAB)) { switchTab(CONFIGS[0]); }
}

//: 顶栏那句「已登录：xxx（…）」括号里写什么。
//  ★ 和服务端 `web/admin.py` 的 `ROLE_ZH` 是同一套说法，别各写各的。
var ROLE_BADGE_ZH = {system: "系统管理员", operator: "运营",
                     player: "玩家 · 只读"};

/** 顶栏那行「已登录：…」。
 *
 * 有昵称（= 有同名玩家账号）写成「已登录：大炮（admin，系统管理员）」，
 * 没有就还是原来那句「已登录：admin（系统管理员）」（用户 2026-09-13）。
 * ★ 昵称摆在最前面、账号名留在括号里：管理员认自己靠昵称，而**能对上号的
 *   只有账号名**（昵称能改、也不保证唯一），两个都得在。
 * ★ 账号名和身份之间用的是中文逗号，不是 ` · ` —— 玩家那一档的说法本身
 *   就带一个 `·`（「玩家 · 只读」），再用它分隔就成了三段看不出层次的东西。
 */
function paintWho() {
  var badge = ROLE_BADGE_ZH[ROLE] || ROLE;
  $("who").textContent = ME.nickname
    ? "已登录：" + ME.nickname + "（" + ME.name + "，" + badge + "）"
    : "已登录：" + ME.name + "（" + badge + "）";
}

function showLoggedIn(name, role, nickname) {
  ROLE = role || null;
  ME = {name: name, nickname: nickname || ""};
  paintWho();
  $("logout").classList.remove("hidden");
  $("loginView").classList.add("hidden");
  $("mainView").classList.remove("hidden");
  // 标签行在顶栏那块壳里，不跟着 `mainView` 走 —— 自己开关一次。
  $("tabs").classList.remove("hidden");
  // 登进来停在 `TAB`（`showLoggedOut` 已经把它归到第一个配置页）：
  // 标签高亮、露哪个面板、外壳撑不撑满，一次摆正。之后换标签由 `switchTab` 管。
  paintTabChrome(TAB);
  applyRoleToTabs();
  applyReadOnly();
  boot();
}

/** 只读身份下，把「改东西」的入口从画面上收起来（D74）。
 *
 * ★ 这一发管的是**面板外壳**那几个固定按钮（添加 / 放弃修改 / 保存）；
 *   列表**里面**每次重画都会新生成一批控件，那批由 `lockList()` 收 ——
 *   两处分工：这里一次就够（DOM 不重建），那里必须跟着每一次重画走。
 * ★ `body.readonly` 只给 CSS 用（锁住的控件长什么样），**不当判据** ——
 *   判据永远是 `isReadOnly()`。
 */
function applyReadOnly() {
  var ro = isReadOnly();
  document.body.classList.toggle("readonly", ro);
  // 「添加 / ●有未保存的修改 / 放弃修改 / 保存」整组 —— 一个只能看的人，
  // 这四样没有一样是有意义的。
  var acts = document.querySelector("#cfgPanel .acts");
  if (acts) { acts.classList.toggle("hidden", ro); }
}

function showLoggedOut(message) {
  CAT = null;
  ROLE = null;
  // 下一个登进来的是另一个人 —— 名字和昵称跟他无关（顶栏那行下面会清掉，
  // 但留着脏数据的话，登录失败又重试时会一闪而过地写着上一个人的昵称）。
  ME = {name: "", nickname: ""};
  // 下一个登进来的可能是管理员 —— 把只读那身衣服脱干净（D74）。
  applyReadOnly();
  // 下一个登进来的人可能权限不同 —— 停在哪一页得跟着回到起点。
  // ★★ 光把 `TAB` 改回去**不够**：标签上那个 `.on` 和「哪个面板露出来」
  //   是 `switchTab` 顺手做的，退出登录时没人做。症状是管理员停在
  //   「玩家仓库」退出之后，下一个登进来的人看到的还是那一页 ——
  //   只读玩家连门都没有的那一页（实测过，D74）。
  TAB = CONFIGS[0];
  CURRENT = CONFIGS[0];
  paintTabChrome(TAB);
  PLAYER = null;
  PLAYER_LIST = [];
  BACKUP = null;
  BACKUP_PAGE = 0;
  // 会话过期时「下载日志」弹窗可能正开着 —— 别让它盖在登录页上。
  LOGS = null;
  // 下一个登进来的是另一个人 —— 他的仓库、报价和挑好的车都跟这个人无关。
  SELL = null;
  SELL_PRICES = null;
  SELL_QTY = null;
  $("playerModal").classList.add("hidden");
  $("sellPriceModal").classList.add("hidden");
  $("sellQtyModal").classList.add("hidden");
  $("logsModal").classList.add("hidden");
  $("who").textContent = "";
  $("logout").classList.add("hidden");
  $("mainView").classList.add("hidden");
  $("tabs").classList.add("hidden");
  // 登录页是普通页面，别让它继承配置页那套「撑满 + 内部滚动」（D39）。
  $("mainArea").classList.remove("fit");
  $("loginView").classList.remove("hidden");
  if (message) { say($("loginMsg"), message, false); }
}

async function boot() {
  toast("读取中……", true);
  if (!(await loadCatalog())) { return; }
  if (!CAT.icons) {
    toast("图标图集没生成（server/web/itemicons.png）——"
          + "先跑 tools\\update-shopicons.bat，格子里会一直是问号。", false);
  }
  var ok = true;
  for (var i = 0; i < CONFIGS.length; i += 1) {
    ok = (await loadConfig(CONFIGS[i])) && ok;
  }
  if (!CFG[CURRENT]) { return; }
  // ★ 哪几页有默认值要补 —— 算完才画工具条（那颗钮只在真有得补时才画）。
  renderCurrent();
  paintOperatorPages();
  // ★ 运营根本进不去这一页，也别去要那份名单（服务端会回 403）。
  if (isSystemAdmin()) { loadAdmins(); }
}

/** 「管理员账号」页上那句「运营能进哪几页」—— 照 `CONFIGS` 现填。
 *  和「设为运营」确认框里那句是同一份数据、同一个分隔符（页名自带斜杠，
 *  所以用「、」隔开，不用「 / 」）。 */
function paintOperatorPages() {
  var host = $("operatorPages");
  if (!host) { return; }
  host.textContent = "只能进 " + configTitles().join("、")
                     + " 这 " + configCount()
                     + " 页，看不到「玩家仓库」和「管理员账号」。";
}

/** 标签行的高亮 + 露哪个面板 + 外壳撑不撑满。返回「这是不是配置页」。
 *
 * ★ `switchTab` 和「退出登录回到起点」共用这一段（D74）。分两份写的话，
 *   加一个标签页就注定有一处会忘 —— 而忘掉的症状不是报错，是「上一个人
 *   停的那一页还留在屏幕上」。
 */
function paintTabChrome(tab) {
  Array.prototype.forEach.call($("tabs").children, function (button) {
    button.classList.toggle("on", button.getAttribute("data-tab") === tab);
  });
  var isConfig = CONFIGS.indexOf(tab) >= 0;
  $("cfgPanel").classList.toggle("hidden", !isConfig);
  $("adminsPanel").classList.toggle("hidden", tab !== "admins");
  $("playersPanel").classList.toggle("hidden", tab !== "players");
  $("backupPanel").classList.toggle("hidden", tab !== "backup");
  $("sellPanel").classList.toggle("hidden", tab !== "sell");
  // ★★ 「面板撑满、只有列表滚」（D39）—— 判据是**露出来的那个面板自己
  //    带不带 `fit-panel`**，不是一张硬编码的标签名清单。
  //    以前是 `isConfig || tab === "backup" || tab === "players" || …`：
  //    2026-09-13 把「管理员账号」也改成这套壳时，光加 `fit-panel` 那个类
  //    没用 —— 面板撑不满、列表也不滚，而且**一句报错都没有**。
  //    现在新页面只要在 HTML 上写 `fit-panel`，这里自动跟着走。
  var shown = document.querySelector("#mainView > section:not(.hidden)");
  $("mainArea").classList.toggle(
    "fit", !!(shown && shown.classList.contains("fit-panel")));
  return isConfig;
}

function switchTab(tab) {
  // ★ 第二道保险：标签已经藏起来了，但键盘 / 脚本还是点得到。
  if (!canOpenTab(tab)) { tab = CONFIGS[0]; }
  TAB = tab;
  var isConfig = paintTabChrome(tab);
  if (tab === "players") {
    // 第一次切进来先列几个，免得画面上是一片空白。
    if (!PLAYER_LIST.length) { searchPlayers(); }
    return;
  }
  if (tab === "backup") {
    // 同上：第一次切进来才去要（运营根本进不来，`boot()` 里不预取）。
    if (!BACKUP) { loadBackups(); }
    return;
  }
  if (tab === "sell") {
    // 同上。★ 每次切回来都**重读**（不是「第一次才读」）：他可能刚在游戏里
    //   花掉了金币、用掉了材料，也可能刚开了一局 —— 这一页上每个数都会过期。
    //   车里挑好的东西留着（`keepCart`），按新存量夹一下。
    loadSellState(true);
    return;
  }
  if (!isConfig) { return; }
  CURRENT = tab;
  // ★ `CAT` 还没到手（登录那一瞬间 `applyRoleToTabs` 可能就调进来了）就
  //   先别画：`renderCurrent()` 头一句读的就是 `CAT.schema`。目录到手之后
  //   `boot()` 会补画一次。
  if (CAT) { renderCurrent(); }
}

function wire() {
  $("loginBtn").onclick = async function () {
    say($("loginMsg"), "登录中……", true);
    var result = await api("/admin/api/login", {
      name: $("loginName").value, password: $("loginPass").value});
    if (!result.ok) { say($("loginMsg"), result.message, false); return; }
    $("loginPass").value = "";
    say($("loginMsg"), "");
    showLoggedIn(result.name, result.role, result.nickname);
  };
  $("loginPass").addEventListener("keydown", function (event) {
    if (event.key === "Enter") { $("loginBtn").click(); }
  });
  $("logout").onclick = async function () {
    await api("/admin/api/logout", {});
    showLoggedOut("已退出登录。");
  };

  Array.prototype.forEach.call($("tabs").children, function (button) {
    button.onclick = function () { switchTab(button.getAttribute("data-tab")); };
  });

  $("cfgSave").onclick = function () { saveConfig(CURRENT); };
  $("cfgReset").onclick = async function () {
    if (await loadConfig(CURRENT)) { renderCurrent(); }
  };
  $("cfgAdd").onclick = function () { addEntry(CURRENT); };

  // ★ 不能直接把 `searchPlayers` 当 onclick —— 那会把 Event 当页码传进去。
  //   手动查一次一律回第一页。
  $("playerSearchBtn").onclick = function () { searchPlayers(0); };
  $("playerSearch").addEventListener("keydown", function (event) {
    if (event.key === "Enter") { searchPlayers(0); }
  });
  // 在线筛选（D75）：下拉一改当场重查，回第一页 —— 和配置页那几个筛选
  // 下拉一个手感，不用再点一次「查找」。
  $("playerOnline").onchange = function () { searchPlayers(0); };
  // 同上：不能直接挂 `refreshAccounts`（Event 会被当第一个参数）。
  // ★ 两页共用一发（D43）—— 点哪个都刷两边。
  $("playerRefreshBtn").onclick = function () { refreshAccounts(); };
  $("adminRefreshBtn").onclick = function () { refreshAccounts(); };

  // 「指定玩家为管理员」弹窗（用户 2026-09-13）。同上：都包一层。
  $("promoteOpenBtn").onclick = function () { openPromoteModal(); };
  $("promoteClose").onclick = closePromoteModal;
  // ★ 只读窗 ⇒ 点遮罩也能关（照「等级经验对应表」那一个）。
  $("promoteModal").onclick = function (event) {
    if (event.target === $("promoteModal")) { closePromoteModal(); }
  };
  $("promoteSearchBtn").onclick = function () { searchPromote(0); };
  $("promoteSearch").addEventListener("keydown", function (event) {
    if (event.key === "Enter") { searchPromote(0); }
  });
  $("promoteOnline").onchange = function () { searchPromote(0); };
  $("promoteRefreshBtn").onclick = function () { searchPromote(); };

  // 数据备份页。同上：包一层，别把 Event 当参数传进去。
  $("backupRefreshBtn").onclick = function () { refreshBackups(); };
  $("backupCreate").onclick = function () { createBackup(); };
  $("backupLabel").addEventListener("keydown", function (event) {
    if (event.key === "Enter") { createBackup(); }
  });
  $("backupSettingsSave").onclick = function () { saveBackupSettings(); };
  $("backupEnabled").onclick = function (event) {
    event.preventDefault();
    if (!BACKUP) { return; }
    BACKUP.edit.enabled = !BACKUP.edit.enabled;
    $("backupEnabled").classList.toggle("on", BACKUP.edit.enabled);
    backupTouched();
  };
  $("backupTime").oninput = function () {
    if (BACKUP) { BACKUP.edit.time = $("backupTime").value; backupTouched(); }
  };
  $("backupKeepDays").oninput = function () {
    if (BACKUP) { BACKUP.edit.keep_days = $("backupKeepDays").value; backupTouched(); }
  };
  // 「⇩ 下载日志」弹窗（用户 2026-09-17）：只读 ⇒ ✕ / Esc / 点遮罩都能关。
  $("logsOpenBtn").onclick = function () { openLogsModal(); };
  $("logsClose").onclick = closeLogsModal;
  $("logsModal").onclick = function (event) {
    if (event.target === $("logsModal")) { closeLogsModal(); }
  };
  $("playerLevel").oninput = function () {
    PLAYER.edit.level = Math.max(1, Number($("playerLevel").value) || 1);
    playerTouched();
  };
  $("playerMoney").oninput = function () {
    PLAYER.edit.money = Math.max(0, Number($("playerMoney").value) || 0);
    playerTouched();
  };
  // 玩家仓库弹窗（用户 2026-09-07）：只有 ✕ 能关，点遮罩不关 —— 所以
  // `#playerModal` 故意**没有** onclick。
  $("playerAddItem").onclick = function () { addOwnedMany(); };
  // 角色 / 上架状态两个下拉（D68）：改了只重画标签行和格子区（D47 的口径）。
  $("playerCharacter").onchange = function () {
    if (!PLAYER) { return; }
    PLAYER.filter.character = $("playerCharacter").value;
    repaintOwned();
  };
  $("playerListing").onchange = function () {
    if (!PLAYER) { return; }
    PLAYER.filter.listing = $("playerListing").value;
    repaintOwned();
  };
  $("playerPopupRefresh").onclick = function () { refreshPlayerPopup(); };
  $("playerClose").onclick = function () { closePlayerModal(); };
  $("playerSave").onclick = savePlayer;
  $("playerReset").onclick = function () {
    if (PLAYER) { openPlayer(PLAYER.view.username, true); }
  };

  // 发送奖励弹窗（D76）。和「修改仓库」一样只有 ✕ 能关：`#rewardModal`
  // 故意**没有** onclick，也不认 Esc —— 里面是勾了一半的人和东西。
  $("rewardOpenBtn").onclick = function () { openRewardModal(); };
  $("rewardClose").onclick = closeRewardModal;
  // 搜索框边打边查（回车也行）；在线下拉一改当场重查。
  $("rewardSearch").oninput = function () { loadRewardPlayers(); };
  $("rewardSearch").addEventListener("keydown", function (event) {
    if (event.key === "Enter") { loadRewardPlayers(); }
  });
  $("rewardOnline").onchange = function () { loadRewardPlayers(); };
  $("rewardSelectAll").onclick = function () { if (REWARD) { rewardSelectListed(true); } };
  $("rewardSelectNone").onclick = function () { if (REWARD) { rewardSelectListed(false); } };
  $("rewardChosenClear").onclick = function () {
    if (!REWARD) { return; }
    REWARD.chosen = {};
    renderRewardChosen();
    renderRewardPlayers();
  };
  $("rewardAddItem").onclick = function () { if (REWARD) { rewardAddMany(); } };
  $("rewardItemsClear").onclick = function () {
    if (!REWARD) { return; }
    REWARD.items = {};
    repaintRewardItems();
  };
  $("rewardCharacter").onchange = function () {
    if (!REWARD) { return; }
    REWARD.filter.character = $("rewardCharacter").value;
    repaintRewardItems();
  };
  $("rewardListing").onchange = function () {
    if (!REWARD) { return; }
    REWARD.filter.listing = $("rewardListing").value;
    repaintRewardItems();
  };
  ["rewardExp", "rewardMoney", "rewardMessage"].forEach(function (id) {
    $(id).oninput = paintRewardFoot;
  });
  $("rewardSend").onclick = function () { sendReward(); };

  // 发送记录弹窗（用户 2026-09-10 第二轮）。整窗只读 ⇒ 点遮罩就能关，
  // Esc 也认（下面那个 keydown 里）—— 和 `#rewardModal` 相反，那里面
  // 是勾了一半的人和东西，误关一次代价太大。
  $("rewardHistoryBtn").onclick = function () { openHistoryModal(); };
  $("historyClose").onclick = closeHistoryModal;
  $("historyModal").onclick = function (event) {
    if (event.target === $("historyModal")) { closeHistoryModal(); }
  };
  $("historyClear").onclick = function () { clearHistory(); };

  // ---------------------------------------------- 装备卖出（2026-09-12）
  $("sellRefresh").onclick = function () { refreshSell(); };
  $("sellConfirm").onclick = function () { doSell(); };
  $("sellCharacter").onchange = function () {
    if (!SELL) { return; }
    SELL.filter.character = $("sellCharacter").value;
    repaintSell();
  };
  $("sellListing").onchange = function () {
    if (!SELL) { return; }
    SELL.filter.listing = $("sellListing").value;
    repaintSell();
  };

  // 价格设置窗：有没保存的改动 ⇒ **点遮罩不关**（和「修改仓库」一个规矩）。
  $("sellPriceBtn").onclick = function () { openSellPrices(); };
  $("sellPriceClose").onclick = function () { closeSellPrices(); };
  $("sellPriceReset").onclick = function () {
    if (!SELL_PRICES) { return; }
    SELL_PRICES.edit = JSON.parse(JSON.stringify(SELL_PRICES.base));
    renderSellPrices();
  };
  $("sellPriceSave").onclick = function () { saveSellPrices(); };
  // 自定义属性弹窗（X3）：有未保存改动的窗 ⇒ 不认遮罩，只认 ✕ / 取消 / Esc。
  $("weaponClose").onclick = function () { closeWeaponModal(); };
  $("weaponCancel").onclick = function () { closeWeaponModal(); };
  $("weaponSave").onclick = function () { saveWeaponModal(); };

  // 数量窗：还没提交，取消不心疼 ⇒ ✕ / 取消 / Esc 都能走，遮罩不认
  // （输入框就在正中间，点歪一下把数字丢了没道理）。
  $("sellQtyClose").onclick = closeSellQty;
  $("sellQtyCancel").onclick = closeSellQty;
  $("sellQtyOk").onclick = confirmSellQty;
  $("sellQtyAll").onclick = function () {
    if (!SELL_QTY) { return; }
    $("sellQtyInput").value = String(SELL_QTY.max);
    syncSellQty("input");
  };
  $("sellQtyInput").oninput = function () { syncSellQty("input"); };
  $("sellQtyRange").oninput = function () { syncSellQty("range"); };
  $("sellQtyInput").onkeydown = function (event) {
    if (event.key === "Enter") { event.preventDefault(); confirmSellQty(); }
  };

  $("pickSearch").oninput = function () {
    PICKER.q = $("pickSearch").value.trim();
    paintPicker();
  };
  $("pickCharacter").onchange = function () {
    PICKER.character = $("pickCharacter").value;
    paintPicker();
  };
  $("pickListing").onchange = function () {
    PICKER.listing = $("pickListing").value;
    paintPicker();
  };
  $("pickClose").onclick = closePicker;
  $("picker").onclick = function (event) {
    // 批量模式点遮罩不关：勾了二十件被一次误点全丢掉太亏；单选照旧。
    if (event.target === $("picker") && !(PICKER && PICKER.multi)) { closePicker(); }
  };
  $("pickClear").onclick = function () {
    if (!PICKER) { return; }
    PICKER.chosen = {};
    paintPicker();
  };
  $("pickConfirm").onclick = function () {
    if (!PICKER || !PICKER.multi) { return; }
    var items = Object.keys(PICKER.chosen)
      .map(function (id) { return BYID[id]; })
      .filter(Boolean);
    var done = PICKER.onPickMany;
    closePicker();
    if (items.length && done) { done(items); }
  };
  // 点遮罩 = 取消（和选择器一个手感）。
  $("dialog").onclick = function (event) {
    if (event.target === $("dialog")) { closeDialog(false); }
  };

  // 「等级经验对应表」（D72b）：✕ 和点旁边的空白都能关。
  // ★ 它是**只读**的，关掉不丢东西 ⇒ 认遮罩；「修改仓库」那个窗故意不认，
  //   因为里面有没保存的改动，点歪一下就白改了。
  $("levelClose").onclick = closeLevelModal;
  $("levelModal").onclick = function (event) {
    if (event.target === $("levelModal")) { closeLevelModal(); }
  };
  document.addEventListener("keydown", function (event) {
    // ★ 对话框排在选择器前面：它是**盖在**选择器上面的那一层
    //   （「加一种材料」的弹窗上再弹确认框时，Esc 该先关掉上面那个）。
    if (DIALOG) {
      if (event.key === "Escape") { closeDialog(false); }
      // 危险操作（红钮）的框不认回车 —— 焦点在「取消」上，让它自己走。
      else if (event.key === "Enter" && !DIALOG.danger) { closeDialog(true); }
      return;
    }
    if (event.key === "Escape" && PICKER) { closePicker(); return; }
    // 数量窗排在价格窗前面：它是从卖出页弹的，两个不会同时开，但万一
    // 有人先开价格窗再点出售，上面那个该先关。
    if (event.key === "Escape" && SELL_QTY) { closeSellQty(); return; }
    // ★ 价格窗有没保存的改动 ⇒ `closeSellPrices()` 会先问一句再关。
    if (event.key === "Escape" && SELL_PRICES) { closeSellPrices(); return; }
    // ★ 自定义属性窗有没保存的改动 ⇒ `closeWeaponModal()` 会先问一句再关。
    if (event.key === "Escape" && WEAPON) { closeWeaponModal(); return; }
    // 管理员账号页那两张表单窗：有没提交的输入，但 Esc 是明确的「我不填了」。
    if (event.key === "Escape" && ADD_ADMIN_OPEN) { closeAddAdmin(); return; }
    if (event.key === "Escape" && SET_PW_OPEN) { closeSetPw(); return; }
    // 称号卡片那两个设置窗：改的是一份拷贝，Esc = 丢掉这次的改动。
    if (event.key === "Escape" && CARD_EDIT) { closeCardEdit(); return; }
    // 达成进度是**只读**的一眼看，关掉不丢东西 ⇒ 排在只读那几张里。
    if (event.key === "Escape" && CARD_PROG) { closeCardProgress(); return; }
    // 只读那几张排最后：它们不会盖在选择器上面。
    if (event.key === "Escape" && LOGS) { closeLogsModal(); return; }
    if (event.key === "Escape" && LEVEL_MODAL) { closeLevelModal(); return; }
    if (event.key === "Escape" && PROMOTE_OPEN) { closePromoteModal(); return; }
    if (event.key === "Escape" && HISTORY) { closeHistoryModal(); }
  });

  // 「手动添加管理员」/「修改密码」两个弹窗（用户 2026-09-13 第二轮）。
  // ★ 两张都有没提交的输入 ⇒ **只认 ✕ / 取消 / Esc，不认遮罩**（本页约定）。
  $("addAdminOpenBtn").onclick = function () { openAddAdmin(); };
  $("addAdminClose").onclick = closeAddAdmin;
  $("addAdminCancel").onclick = closeAddAdmin;
  $("setPwOpenBtn").onclick = function () { openSetPw(); };
  $("setPwClose").onclick = closeSetPw;
  $("setPwCancel").onclick = closeSetPw;

  // 称号卡片那两个设置窗（用户 2026-09-13 第三轮）。
  // ★ 两张都有没保存的改动 ⇒ **只认 ✕ / 取消 / Esc，不认遮罩**（本页约定）。
  $("cardModeClose").onclick = closeCardEdit;
  $("cardModeCancel").onclick = closeCardEdit;
  $("cardModeSave").onclick = function () { saveCardEdit(); };
  $("cardCondClose").onclick = closeCardEdit;
  $("cardCondCancel").onclick = closeCardEdit;
  $("cardCondAdd").onclick = function () {
    if (!CARD_EDIT || CARD_EDIT.which !== "cond") { return; }
    var list = cardConditions(CARD_EDIT.draft);
    list.push(cardNewCondition(list.length));
    renderCardCond();
  };
  // 达成进度弹窗：**只读** ⇒ ✕ / Esc / 点遮罩都能关（同「等级经验对应表」）。
  $("cardProgressClose").onclick = closeCardProgress;
  $("cardProgressModal").onclick = function (event) {
    if (event.target === $("cardProgressModal")) { closeCardProgress(); }
  };
  $("cardCondSave").onclick = function () {
    // 「条件无效」时这颗钮是灰的（`paintCardSay`），这儿再兜一道 ——
    // 键盘也能按到它。
    if (!CARD_EDIT || cardRuleProblem(CARD_EDIT.draft)) { return; }
    saveCardEdit();
  };

  $("addAdmin").onclick = async function () {
    if (!passwordsMatch("newAdminPass", "newAdminPass2")) { return; }
    // ★ 权限**永远明确传**，不指望服务端的默认值 —— 弹窗里那个下拉框
    //   每次打开都被重置成「运营」（加人先给最小权限，要全权得自己点一下）。
    var result = await api("/admin/api/admins/add", {
      name: $("newAdminName").value, password: $("newAdminPass").value,
      role: $("newAdminRole").value});
    toast(result.message, result.ok);
    if (result.ok) {
      closeAddAdmin();
      renderAdmins(result.admins);
    }
  };
  $("setPw").onclick = async function () {
    if (!passwordsMatch("pwValue", "pwValue2")) { return; }
    var result = await api("/admin/api/admins/password", {
      name: $("pwName").value, password: $("pwValue").value});
    if (result.ok && result.logged_out) {
      // 改的是自己 ⇒ 整页退到登录界面，弹窗跟着收掉。
      closeSetPw();
      showLoggedOut(result.message);
      return;
    }
    toast(result.message, result.ok);
    // ★ 失败时**把窗留着**（名字打错了改一个字就能再试），只清密码那两格。
    if (result.ok) { closeSetPw(); }
    else { clearFields(["pwValue", "pwValue2"]); }
  };

  // 浮条的位置跟着标题栏走：窗口变窄标题栏可能换行变高，重新量一次。
  window.addEventListener("resize", placeToasts);

  // 关标签页前拦一下 —— 表单页最容易「改了半天忘了按保存」。
  window.addEventListener("beforeunload", function (event) {
    if (CAT && (CONFIGS.some(isDirty) || playerDirty() || backupSettingsDirty()
                || rewardDirty())) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
}

/** 「+ 添加」。新条目按字段表铺一份默认值，物品让用户当场选。 */
function addEntry(which) {
  openPicker({
    kinds: (which === "drops") ? ["material"] : null,
    onPick: function (item) {
      var entry = {};
      (CAT.schema[which].fields || []).forEach(function (spec) {
        if (spec.optional) { return; }          // 可选的一律先不写
        if (spec.type === "item") { entry[spec.key] = item.id; }
        else if (spec.type === "bool") { entry[spec.key] = true; }
        else if (spec.type === "materials") { entry[spec.key] = []; }
        else if (spec.type === "int") { entry[spec.key] = spec.min || 0; }
        else if (spec.type === "choice") {
          entry[spec.key] = (spec.options && spec.options[0])
            ? spec.options[0].value : null;
        } else { entry[spec.key] = ""; }
      });
      if ("kind" in entry) { entry.kind = item.kind; }
      if ("id" in entry && which === "recipe") { entry.id = nextRecipeId(); }
      CFG[which].entries.push(entry);
      // 新加的那条一定要看得见 —— 否则筛选开着的时候「加了没反应」。
      // ★ 它追加在末尾 ⇒ 清掉筛选（连分类标签一起）再滚过去。
      FILTER[which] = emptyFilter();
      renderCurrent();
      var card = $("cfgList").querySelector(
        '[data-index="' + (CFG[which].entries.length - 1) + '"]');
      if (card) { revealIfHidden(card); }
    }
  });
}

function nextRecipeId() {
  var top = 0;
  CFG.recipe.entries.forEach(function (entry) {
    var value = Number(entry.id);
    if (isFinite(value) && value > top) { top = value; }
  });
  return top + 1;
}

(async function () {
  wire();
  wireTips();
  var session = await api("/admin/api/session");
  if (session.logged_in) {
    showLoggedIn(session.name, session.role, session.nickname);
  }
  else { showLoggedOut(""); }
}());
