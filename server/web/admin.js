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
var CONFIGS = ["items", "shop", "recipe", "drops", "rewards"];

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
  var bits = [CAT.kinds[item.kind] || item.kind];
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
  var rule = itemRuleOf(itemId);
  var line = el("div", "t-meta");
  line.appendChild(document.createTextNode(
    (rule.character === null ? "不限角色"
      : (CAT.characters[String(rule.character)] || ("角色" + rule.character)))
    + "　" + (rule.level > 1 ? ("需 " + rule.level + " 级") : "不限等级")
    + (rule.known ? "" : "（物品库里没登记）")));
  box.appendChild(line);

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
  CFG[which].snapshot = snapshot(which);
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
  if (which === CURRENT) { renderCurrent(); }
  toast(result.message, true);
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
  var ok = true;
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
  $("cfgCount").textContent =
    CFG[CURRENT].entries.length + " " + CAT.schema[CURRENT].unit;
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
    help.appendChild(el("li", null, line));
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
  $("cfgAdd").classList.toggle("hidden",
                               which === "items" || which === "rewards");

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
  if (which !== "rewards") {
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

  if (which !== "drops" && which !== "rewards") {
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
                 rewards: renderRewards};

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
    // ★ 「筛出 x / y」**只有掉落页写**（用户 2026-09-09，D68a）：另外三页的
    //   分类标签上已经带着件数，再写一遍是重复的。一样多就什么都不写。
    label.textContent = (CURRENT === "drops" && rows.length !== total)
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
  var hide = (which === "drops" || which === "rewards");
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
  return (entry.id === undefined) ? entry.material : entry.id;
}

/** 一条记录过不过**筛选条**（搜索串 / 下拉；分类标签另算，见 `narrowToTab`）。 */
function matches(which, entry) {
  var filter = FILTER[which] || {};
  var itemId = entryItemId(which, entry);
  var item = BYID[itemId];
  // ★ 拿 `entryItemId` 不拿 `entry.id`：合成配方的 `id` 是配方号。以前这一句
  //   写的是 `entry.id`，那时只有物品库有「上架状态」筛选，没暴露出来。
  if (!dropdownsMatch(itemId, filter)) { return false; }
  if (filter.mode && (entry.mode || "quest") !== filter.mode) { return false; }
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

//: 玩家列表上那个灰钮写什么（D40）。★ 和 `ROLE_ZH` **故意不一样**：
//  那张表用在「管理员账号」页的下拉框里，那儿上下文已经写着「权限」了；
//  玩家列表上只有一个钮，光写「运营」看不出这是管理页的权限。
var ADMIN_BADGE_ZH = {system: "系统管理员", operator: "管理员（运营）"};

function renderAdmins(admins) {
  var rows = $("adminRows");
  rows.textContent = "";
  (admins || []).forEach(function (row) {
    var name = row.name;
    var tr = document.createElement("tr");
    tr.appendChild(el("td", null, name));

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
    var button = el("button", "btn btn-danger btn-sm", "删除");
    button.onclick = function () { removeAdmin(name); };
    actions.appendChild(button);
    tr.appendChild(actions);
    rows.appendChild(tr);
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
    // ★ 括号里那句话走 `ROLE_BADGE_ZH`，别在这儿再写一遍「（运营）」——
    //   三档身份的说法只该有一个出处（D74）。
    ROLE = "operator";
    $("who").textContent = "已登录：" + name + "（" + ROLE_BADGE_ZH[ROLE] + "）";
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

//: 在线筛选那三档的中文名（D75）。值和服务端 `admin.ONLINE_FILTERS` 一样，
//  下拉本身在 `admin.html` 里 —— 这份表只给「N 个账号（不在线）」那句话用。
var ONLINE_FILTER_ZH = {on: "在线", off: "不在线"};

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
    td.colSpan = 5;
    tr.appendChild(td);
    rows.appendChild(tr);
  }
  PLAYER_LIST.forEach(function (row) {
    var line = document.createElement("tr");
    if (PLAYER && PLAYER.view.username === row.username) { line.className = "on"; }
    line.appendChild(el("td", null, row.username + (row.online ? " ●" : "")));
    line.appendChild(el("td", null, row.nickname));
    line.appendChild(el("td", null, row.level));
    line.appendChild(el("td", null, row.money));
    var td = el("td");
    // ★ 权限那个钮在**左**、「修改仓库」在**右**，整组**右对齐**
    //   （用户 2026-09-06）。两个钮**长得不一样**（D40）：一个改这个人的仓库、
    //   一个给他管理页的权限，不能像同一个东西 —— 但靠的是**轻重**不是色相
    //   （D40a，用户 2026-09-07）：「修改仓库」是这一行的主动作，金色主钮；
    //   「设为管理员」少用、米黄默认钮。原来的青色是整页唯一的冷色，太突兀。
    var acts = el("div", "acts");
    // 已经是管理员的：同一个位置换成**点不动的灰钮**，上面写他的实际权限，
    // 别给一个点下去必然报「已经存在」的按钮。
    var promote = el("button", "btn btn-sm",
                     row.admin_role ? (ADMIN_BADGE_ZH[row.admin_role]
                                       || row.admin_role)
                                    : "设为管理员（运营）");
    if (row.admin_role) {
      promote.disabled = true;
      promote.title = "这个玩家已经能登管理页了，权限在「管理员账号」页改";
    } else {
      promote.onclick = function () { promoteToAdmin(row.username); };
    }
    acts.appendChild(promote);
    var button = el("button", "btn btn-sm btn-primary", "修改仓库");
    button.onclick = function () { openPlayer(row.username); };
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
  // 那一格要变成「已是管理员」—— 停在当前页重查一次。
  searchPlayers(PLAYER_PAGE.page);
}

function renderPlayerPager() {
  var host = $("playerPager");
  host.textContent = "";
  // ★ 只有一页时整条都不画 —— 大多数服务器就几十个号，别让翻页控件
  //   在那儿占一行说「第 1 / 1 页」。
  if (PLAYER_PAGE.pages <= 1) { return; }
  function step(label, target, disabled) {
    var button = el("button", "btn btn-sm", label);
    button.disabled = disabled;
    button.onclick = function () { searchPlayers(target); };
    host.appendChild(button);
  }
  step("‹ 上一页", PLAYER_PAGE.page - 1, PLAYER_PAGE.page <= 0);
  host.appendChild(el("span", "pageno",
                      "第 " + (PLAYER_PAGE.page + 1) + " / "
                      + PLAYER_PAGE.pages + " 页"));
  step("下一页 ›", PLAYER_PAGE.page + 1,
       PLAYER_PAGE.page >= PLAYER_PAGE.pages - 1);
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
}

function renderPlayer() {
  var view = PLAYER.view;
  $("playerWho").textContent = view.nickname + "（" + view.username + "）";
  // 在线状态紧跟在名字后面（用户 2026-09-07），不再放标题栏右端。
  // ★ 是 `playerOnlineNote` 不是 `playerOnline` —— 后者是工具条上那个在线筛选
  //   下拉（D75），2026-09-10 撞过名：这一句把下拉的三个选项抹成了一行字。
  $("playerOnlineNote").textContent = view.online ? "● 在线，改完即时生效" : "○ 不在线";
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
                        row.online ? "● 在线" : "○ 不在线"));
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
    line.appendChild(el("div", "who", "发送者：" + (row.sender || "（不详）")));
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
   ["发送者", row.sender || "（不详）"],
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

//: 现在停在哪个标签页。★ 和 `CURRENT` 不是一回事 —— `CURRENT` 只记那几个
//  **配置**页（渲染要用），「玩家仓库」「数据备份」「管理员账号」不在里面。
var TAB = "items";

//: 只有系统管理员能进的标签页（数据备份也是：它能回滚玩家存档）。
var SYSTEM_ONLY_TABS = ["players", "backup", "admins"];

function isSystemAdmin() { return ROLE === "system"; }

//: 只读身份（D74）。★ 判据是**角色**，不是「有没有某个按钮」—— 页面上
//  凡是「能改东西」的地方都问它，一处一处地判「这个钮该不该画」迟早漏。
function isReadOnly() { return ROLE === "player"; }

function canOpenTab(tab) {
  // ★ 只读玩家看得见的就是**那几个配置页**，照 `CONFIGS` 现取（不是照
  //   `SYSTEM_ONLY_TABS` 取反）：以后加一个既不是配置页、又不属于系统管理员
  //   专档的新标签，取反那种写法会**默认放行**给玩家 —— 白名单不会。
  if (isReadOnly()) { return CONFIGS.indexOf(tab) >= 0; }
  return isSystemAdmin() || SYSTEM_ONLY_TABS.indexOf(tab) < 0;
}

/** 按当前权限决定哪几个标签露出来。 */
function applyRoleToTabs() {
  Array.prototype.forEach.call($("tabs").children, function (button) {
    var tab = button.getAttribute("data-tab");
    button.classList.toggle("hidden", !canOpenTab(tab));
  });
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

function showLoggedIn(name, role) {
  ROLE = role || null;
  $("who").textContent = "已登录：" + name
    + "（" + (ROLE_BADGE_ZH[ROLE] || ROLE) + "）";
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
  $("playerModal").classList.add("hidden");
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
  // ★ 配置页、数据备份页和玩家仓库页是「面板撑满、列表自己滚」（D39；
  //   玩家仓库页 2026-09-07 加进来）；管理员账号是普通长页面，整块跟着
  //   `main` 滚。
  $("mainArea").classList.toggle("fit",
                                 isConfig || tab === "backup" || tab === "players");
  $("cfgPanel").classList.toggle("hidden", !isConfig);
  $("adminsPanel").classList.toggle("hidden", tab !== "admins");
  $("playersPanel").classList.toggle("hidden", tab !== "players");
  $("backupPanel").classList.toggle("hidden", tab !== "backup");
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
    showLoggedIn(result.name, result.role);
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
    // 只读那两张排最后：它们不会盖在选择器上面。
    if (event.key === "Escape" && LEVEL_MODAL) { closeLevelModal(); return; }
    if (event.key === "Escape" && HISTORY) { closeHistoryModal(); }
  });

  $("addAdmin").onclick = async function () {
    // ★ 权限**永远明确传**，不指望服务端的默认值 —— 页面上那个下拉框
    //   默认是「运营」（加人先给最小权限，要全权得自己点一下）。
    var result = await api("/admin/api/admins/add", {
      name: $("newAdminName").value, password: $("newAdminPass").value,
      role: $("newAdminRole").value});
    toast(result.message, result.ok);
    if (result.ok) {
      $("newAdminName").value = "";
      $("newAdminPass").value = "";
      renderAdmins(result.admins);
    }
  };
  $("setPw").onclick = async function () {
    var result = await api("/admin/api/admins/password", {
      name: $("pwName").value, password: $("pwValue").value});
    $("pwValue").value = "";
    if (result.ok && result.logged_out) {
      showLoggedOut(result.message);
      return;
    }
    toast(result.message, result.ok);
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
  if (session.logged_in) { showLoggedIn(session.name, session.role); }
  else { showLoggedOut(""); }
}());
