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

var CONFIGS = ["items", "shop", "recipe", "drops"];

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
    var body = el("div", "t-body");
    // ★ `desc` 是用 `\n` 分行的（服务端就是这么发给游戏客户端的），
    //   一行一个 div，别指望 white-space 去还原。
    item.desc.split("\n").forEach(function (line) {
      body.appendChild(el("div", null, line));
    });
    box.appendChild(body);
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

/** 「↻ 刷新」：把**四份配置全部**重新读一遍（用户 2026-09-06）。
 *
 * ★ 为什么不只读当前这一页：这四份**不是各管各的**。
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
  if (ok) { toast("已刷新：四份配置都换成服务端上最新的了。", true); }
  return ok;
}

/** 服务端的错误里带着下标（`recipes[3].materials[1].id：…`），定位过去。
 *
 * ★ 那张卡可能在**别的页上**、甚至被筛选挡住了 —— 分页之后不翻过去的话
 *   「报了个错但画面上什么都没高亮」，比不报还难查。
 */
function markBadCard(message) {
  var match = /\[(\d+)\]/.exec(message || "");
  if (!match) { return; }
  var index = Number(match[1]);
  var at = positionOf(CURRENT, index);
  if (at < 0) {
    FILTER[CURRENT] = emptyFilter();     // 被筛掉了，先把筛选清掉
    renderToolbar(CURRENT);
    at = positionOf(CURRENT, index);
  }
  if (at < 0) { return; }
  FILTER[CURRENT].page = Math.floor(at / PAGE_SIZE);
  repaintList();
  var card = $("cfgList").querySelector('[data-index="' + index + '"]');
  if (!card) { return; }
  card.classList.add("bad", "flash");
  revealIfHidden(card);
}

/** 原数组下标 `index` 的那一条，排在**当前筛选结果**的第几位；筛没了就 -1。 */
function positionOf(which, index) {
  var at = -1;
  visibleEntries(which).forEach(function (row, position) {
    if (row.index === index) { at = position; }
  });
  return at;
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
   渲染
   ====================================================================== */

function renderCurrent() {
  var which = CURRENT;
  var schema = CAT.schema[which];
  $("cfgTitle").textContent = schema.title;

  var help = $("cfgHelp");
  help.textContent = "";
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
  $("cfgAdd").classList.toggle("hidden", which === "items");

  renderToolbar(which);
  repaintList();
}

/* ------------------------------------------------------------ 工具条 */
function renderToolbar(which) {
  var bar = $("cfgToolbar");
  bar.textContent = "";
  if (!FILTER[which]) { FILTER[which] = emptyFilter(); }
  var filter = FILTER[which];

  var search = document.createElement("input");
  search.type = "text";
  search.placeholder = "搜 中文名 / 韩文名 / id";
  search.value = filter.q;
  search.oninput = function () {
    filter.q = search.value.trim();
    resetPage(which);
    repaintList();
  };
  bar.appendChild(search);

  if (which === "items" || which === "shop") {
    // ★ 物品库的类别下拉列**全部类别**（和「选择物品」弹窗一个口径），
    //   不是「这份文件里出现过的类别」—— 筛出空列表也是有用的信息
    //   （「原来一件宠物都没登记」）。
    var kinds = (which === "items")
      ? Object.keys(CAT.kinds).sort()
      : uniq(CFG[which].entries.map(function (e) { return e.kind; }));
    bar.appendChild(selectFilter(filter, "kind", "全部类别",
      kinds.map(function (k) { return {value: k, label: CAT.kinds[k] || k}; })));
    bar.appendChild(selectFilter(filter, "character", "全部角色",
      Object.keys(CAT.characters).map(function (k) {
        return {value: k, label: CAT.characters[k]}; })));
  }
  if (which === "items") {
    // 上架状态：一件东西要么在商店卖、要么靠合成拿、要么都不是（互斥）。
    bar.appendChild(selectFilter(filter, "listing", "全部", [
      {value: "shop", label: "只看上架商店"},
      {value: "recipe", label: "只看上架合成"},
      {value: "any", label: "只看已上架"},
      {value: "none", label: "只看未上架"}]));
  }
  if (which === "recipe") {
    // ★ 类别取的是**产物**的类别（配方条目自己没有 `kind` 这一栏）——
    //   和「商店货架」一个口径：只列这份文件里真出现过的那几类。
    bar.appendChild(selectFilter(filter, "kind", "全部类别",
      uniq(CFG.recipe.entries.map(function (entry) {
        return (BYID[entry && entry.result] || {}).kind;
      }).filter(Boolean)).map(function (k) {
        return {value: k, label: CAT.kinds[k] || k}; })));
    bar.appendChild(selectFilter(filter, "character", "全部角色",
      Object.keys(CAT.characters).map(function (k) {
        return {value: k, label: CAT.characters[k]}; })));
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
  if (which === "shop" || which === "recipe") {
    var only = el("label", "toggle" + (filter.listedOnly ? " on" : ""));
    only.appendChild(el("span", "track"));
    only.appendChild(el("span", null, "只看上架"));
    only.onclick = function (event) {
      event.preventDefault();
      filter.listedOnly = !filter.listedOnly;
      only.classList.toggle("on", filter.listedOnly);
      resetPage(which);
      repaintList();
    };
    bar.appendChild(only);
  }

  // ★ 「↻ 刷新」排在筛选控件**后面**，**四个配置页都有**（用户 2026-09-06）
  //   —— 点哪一页的都是把四份一起重读，见 `refreshConfigs()`。
  var refresh = el("button", "btn btn-sm", "↻ 刷新");
  refresh.title = "重新读一遍服务端上的四份配置（不只是这一页）";
  refresh.onclick = function () { refreshConfigs(); };
  bar.appendChild(refresh);

  var shown = el("span", "grow");
  shown.id = "cfgShown";
  bar.appendChild(shown);
}

function selectFilter(filter, key, allLabel, options) {
  var select = document.createElement("select");
  var first = el("option", null, allLabel);
  first.value = "";
  select.appendChild(first);
  options.forEach(function (option) {
    var node = el("option", null, option.label);
    node.value = String(option.value);
    select.appendChild(node);
  });
  select.value = filter[key] || "";
  select.onchange = function () {
    filter[key] = select.value;
    filter.page = 0;               // 换了筛选条件就回第一页
    repaintList();
  };
  return select;
}

/** 一份空的筛选条件。★ 加字段时只改这一处 —— 页面上有三个地方要「清筛选」。 */
function emptyFilter() {
  return {q: "", kind: "", character: "", listedOnly: false,
          mode: "", stage: "", difficulty: "", page: 0};
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

function uniq(values) {
  var seen = {}, out = [];
  values.forEach(function (v) { if (!seen[v]) { seen[v] = 1; out.push(v); } });
  return out.sort();
}

//: 每个配置标签页画成什么样。★ 加一份配置时只要在这儿登记一行。
//  （函数声明会被提升，所以写在它们前面没问题。）
var RENDERERS = {items: renderItems, shop: renderShop,
                 recipe: renderRecipe, drops: renderDrops};

function repaintList() {
  var list = $("cfgList");
  // ★ 重画**一律不动滚动条**（用户 2026-09-07）：清空再填是同一个任务里做完的，
  //   浏览器本来就不会动它；这一发是把「不许动」写死，免得日后谁往重画里插
  //   一句读版面的代码，位置就被夹没了。
  var keep = list.scrollTop;
  list.textContent = "";
  var view = pageRows(CURRENT);
  // ★ 换页栏画在 `#cfgPager` 里，**在滚动区外面**（D39）—— 它得跟筛选条
  //   一起钉住不动。只有一页时整条不画（`.pager:empty` 连外边距一起收掉）。
  paintPager(CURRENT, view);
  RENDERERS[CURRENT](list, view.rows);
  list.scrollTop = keep;
  touched();
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

/** 一条记录过不过筛选。**下标一律用原数组的**，服务端报错才对得上。 */
function matches(which, entry) {
  var filter = FILTER[which] || {};
  if (filter.listedOnly && !entry.listed) { return false; }
  if (filter.listing) {
    var where = listingOf(entry.id);          // "shop" / "recipe" / ""
    if (filter.listing === "any" && !where) { return false; }
    if (filter.listing === "none" && where) { return false; }
    if ((filter.listing === "shop" || filter.listing === "recipe")
        && where !== filter.listing) { return false; }
  }
  if (filter.mode && (entry.mode || "quest") !== filter.mode) { return false; }
  if (which === "drops") {
    if (!dropFieldMatches(entry.stage, filter.stage)) { return false; }
    if (!dropFieldMatches(entry.difficulty, filter.difficulty)) { return false; }
  }
  var itemId = entryItemId(which, entry);
  var item = BYID[itemId];
  if (filter.kind && entry.kind !== filter.kind
      && (!item || item.kind !== filter.kind)) { return false; }
  if (filter.character !== undefined && filter.character !== "") {
    // ★ 按**物品库里那份角色限定**筛（D31），不看条目自己带的键 ——
    //   在物品库里把一件东西改成「不限」之后，它就不该再出现在
    //   「泰尔」这一档里（`character` 那个键是**删掉**表示不限的，
    //   拿 `undefined` 退回原版数据会让「改成不限」看上去没生效）。
    if (String(itemRuleOf(itemId).character) !== filter.character) {
      return false;
    }
  }
  if (filter.q) {
    var hay = [itemName(itemId), entry.note, String(itemId),
               item ? item.name_kr : "", item ? item.name : ""]
      .join(" ").toLowerCase();
    if (hay.indexOf(filter.q.toLowerCase()) < 0) { return false; }
  }
  return true;
}

/** 过了筛选的那些记录，`[{entry, index}]`。**下标一律用原数组的**。 */
function visibleEntries(which) {
  var rows = [];
  CFG[which].entries.forEach(function (entry, index) {
    if (matches(which, entry)) { rows.push({entry: entry, index: index}); }
  });
  if (which === "drops") {
    rows.sort(function (a, b) {
      return (dropRank(a.entry) - dropRank(b.entry)) || (a.index - b.index);
    });
  }
  return rows;
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

/** 一页画几条（D37）。★ 这是**界面取舍**（一次铺 800 张卡 DOM 会卡手），
 *  不是铁律 10 说的那种时序阈值 —— 超出的翻页，不再是「剩下的不画了」。 */
var PAGE_SIZE = 120;

function pageCount(total) {
  return Math.max(1, Math.ceil(total / PAGE_SIZE));
}

/** 这一页要画的那些记录，顺带把「筛出 x / y　第 m / n 页」写上。
 *  （掉落页的显示顺序在 `visibleEntries` 里定，这里不再另排 —— 翻页、
 *  报错定位用的 `positionOf` 才和画面一致。） */
function pageRows(which) {
  var all = visibleEntries(which);
  var pages = pageCount(all.length);
  var filter = FILTER[which] || (FILTER[which] = {});
  // 筛完变短了、或者删掉了最后一条 ⇒ 当前页可能已经不存在了，夹回来。
  var page = Math.min(Math.max(0, filter.page || 0), pages - 1);
  filter.page = page;
  var label = $("cfgShown");
  if (label) {
    var total = CFG[which].entries.length;
    // ★ 只写「筛出 x / y」。页码**只在换页栏上写一次**（用户 2026-09-07：
    //   筛选条和换页栏各写一遍「第 m / n 页」是重复的）。
    label.textContent = (all.length !== total)
      ? ("筛出 " + all.length + " / " + total) : "";
  }
  return {rows: all.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE),
          pages: pages, page: page, total: all.length};
}

/** 换页栏。**只有列表上面这一条**（D37b），而且画在滚动区**外面**
 *  （D39：`#cfgPager` 和筛选条一起钉住，滚列表的时候它不动）。
 *  只有一页时整条不画。
 *
 *  ★ 换页**一律不动滚动条**：点哪个按钮都只换内容，画面停在原处。
 *    列表下面原来还有一条，删掉了 —— 在底下换到末页（末页是半页，列表
 *    真的变短）时，原位置越过新的底，浏览器一夹画面就是一跳，怎么写都
 *    躲不掉。栏子只留在顶上，点它的时候人本来就在顶上，没得可夹。 */
function paintPager(which, view) {
  var host = $("cfgPager");
  host.textContent = "";
  if (view.pages <= 1) { return; }
  function step(text, target, disabled) {
    var button = el("button", "btn btn-sm", text);
    button.disabled = disabled;
    button.onclick = function () {
      // ★ 滚动条现在长在 `#cfgList` 上（D39），不是窗口上。
      var list = $("cfgList");
      var keep = list.scrollTop;
      FILTER[which].page = target;
      repaintList();
      // 清空再填是同一个任务里做完的，浏览器本来就不会动滚动条；这一发
      // 是把「不许动」写死，免得日后谁往重画里插一句读版面的代码，位置
      // 就被夹没了。末页比整页短、原位置越界时浏览器自己会夹回来。
      list.scrollTop = keep;
    };
    host.appendChild(button);
  }
  step("‹ 上一页", view.page - 1, view.page <= 0);
  host.appendChild(el("span", "pageno",
                      "第 " + (view.page + 1) + " / " + view.pages + " 页　共 "
                      + view.total + " 条"));
  step("下一页 ›", view.page + 1, view.page >= view.pages - 1);
}

/** 筛选条件一变就回第一页 —— 停在第 5 页而新结果只有 2 页会变成一片空白。 */
function resetPage(which) {
  if (FILTER[which]) { FILTER[which].page = 0; }
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
function renderRecipe(list, rows) {
  rows.forEach(function (row) {
    var entry = row.entry, index = row.index;
    var card = el("div", "recipe-card" + (entry.listed ? " listed" : ""));
    card.setAttribute("data-index", index);
    card.appendChild(killButton("recipe", index));
    card.appendChild(el("span", "rid", "配方 #" + (entry.id === undefined ? "?" : entry.id)));

    var head = el("div", "recipe-head");
    head.appendChild(materialSlots(entry, card));
    head.appendChild(el("span", "arrow", "➜"));

    var out = el("div", "recipe-out");
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
    card.appendChild(head);

    var foot = el("div", "recipe-foot");
    restFields("recipe", entry, ["id", "result", "name", "materials"],
      function () {
        card.classList.toggle("listed", !!entry.listed);
        slot.classList.toggle("on", !!entry.listed);
        touched();
      }).forEach(function (node) { foot.appendChild(node); });
    card.appendChild(foot);
    list.appendChild(card);
  });
}

/** 固定画 `max_materials` 格 —— 原版合成界面只有 4 个槽，第 5 种玩家看不见。 */
function materialSlots(entry, card) {
  var box = el("div", "mat-slots");
  if (!Array.isArray(entry.materials)) { entry.materials = []; }
  var spec = null;
  (CAT.schema.recipe.fields || []).forEach(function (field) {
    if (field.key === "materials") { spec = field; }
  });
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

  var who = el("div", "who");
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

/* ======================================================================
   物品选择器
   ====================================================================== */

var PICKER = null;

/** 打开选择器。
 *
 *  单选（配置页「添加」）：`{kinds, selected, onPick(item)}`，点一格就选中并关闭。
 *  批量（玩家背包弹窗「添加物品」，用户 2026-09-07）：`{multi: true, owned,
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
    q: "",
    page: 0,
    kind: (options.kinds && options.kinds.length === 1) ? options.kinds[0] : "",
    character: ""
  };
  $("pickSearch").value = "";
  var kindSelect = $("pickKind");
  kindSelect.textContent = "";
  var all = el("option", null, "全部类别");
  all.value = "";
  kindSelect.appendChild(all);
  var kinds = PICKER.kinds || uniq(CAT.items.map(function (i) { return i.kind; }));
  kinds.forEach(function (kind) {
    var node = el("option", null, CAT.kinds[kind] || kind);
    node.value = kind;
    kindSelect.appendChild(node);
  });
  kindSelect.value = PICKER.kind;
  // 角色下拉（用户 2026-09-06）。★ 列**全部角色**而不是「这批候选里出现过的」
  //   —— 筛出空网格也是有用的信息（「原来泰尔一件鞋都没有」）。
  var whoSelect = $("pickCharacter");
  whoSelect.textContent = "";
  var anyone = el("option", null, "全部角色");
  anyone.value = "";
  whoSelect.appendChild(anyone);
  Object.keys(CAT.characters).forEach(function (cid) {
    var node = el("option", null, CAT.characters[cid]);
    node.value = cid;
    whoSelect.appendChild(node);
  });
  whoSelect.value = "";
  kindSelect.disabled = !!(PICKER.kinds && PICKER.kinds.length === 1);
  $("pickFoot").classList.toggle("hidden", !PICKER.multi);
  $("picker").classList.remove("hidden");
  paintPicker();
  $("pickSearch").focus();
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

/** 弹窗里一页画这么多格（D37）。808 件全铺出来是几千像素高的一张网，搜索框会卡手
 *  —— 超出的**翻页**（用户 2026-09-06），不再是「剩下的不画了」。 */
var PICK_PAGE_SIZE = 200;

/** 弹窗的换页栏，也**只有网格上面这一条**（D37b）。只有一页时整条不画。
 *  规矩和列表那边一模一样（见 `pagerNode`），只是这里滚的是弹窗自己那个
 *  `.panel-body`，不是整页。 */
function paintPickPager(host, pages) {
  host.textContent = "";
  if (pages <= 1) { return; }
  function step(text, target, disabled) {
    var button = el("button", "btn btn-sm", text);
    button.disabled = disabled;
    button.onclick = function () {
      // ★ 滚的是网格自己（D39），不再是整个 `.panel-body`。
      var grid = $("pickGrid");
      var keep = grid.scrollTop;
      PICKER.page = target;
      paintPicker();
      grid.scrollTop = keep;
    };
    host.appendChild(button);
  }
  step("‹ 上一页", PICKER.page - 1, PICKER.page <= 0);
  host.appendChild(el("span", "pageno",
                      "第 " + (PICKER.page + 1) + " / " + pages + " 页"));
  step("下一页 ›", PICKER.page + 1, PICKER.page >= pages - 1);
}

function paintPicker() {
  var grid = $("pickGrid");
  grid.textContent = "";
  var query = PICKER.q.toLowerCase();
  var hits = CAT.items.filter(function (item) {
    if (PICKER.kinds && PICKER.kinds.indexOf(item.kind) < 0) { return false; }
    if (PICKER.kind && item.kind !== PICKER.kind) { return false; }
    // ★ 角色按**物品库里那份角色限定**筛（D31），和 `matches()` 同一条判据
    //   —— 在物品库里改成「不限」之后，这儿也不该再把它算进那个角色。
    if (PICKER.character
        && String(itemRuleOf(item.id).character) !== PICKER.character) {
      return false;
    }
    if (!query) { return true; }
    // 中文名按**物品库**里那一份搜（D31）—— 在物品库里改过名字之后，
    // 用新名字搜不到才叫奇怪。
    return (itemName(item.id) + " " + (item.name_kr || "") + " " + item.id)
      .toLowerCase().indexOf(query) >= 0;
  });
  if (!hits.length) {
    grid.appendChild(el("div", "pick-empty", "没有匹配的物品"));
  }
  var pages = Math.max(1, Math.ceil(hits.length / PICK_PAGE_SIZE));
  // 搜索串一变结果就短了 —— 当前页可能已经不存在，夹回来。
  PICKER.page = Math.min(Math.max(0, PICKER.page || 0), pages - 1);
  hits.slice(PICKER.page * PICK_PAGE_SIZE,
             (PICKER.page + 1) * PICK_PAGE_SIZE).forEach(function (item) {
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
  paintPickPager($("pickPagerTop"), pages);
  // 标题栏只写件数，页码归换页栏（用户 2026-09-07，和配置页一个口径：
  // 「第 m / n 页」只写一处）。
  $("pickCount").textContent = hits.length + " 件";
  paintPickFoot();
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
  say($("adminMsg"), result.message, result.ok);
  // ★ 失败也要重画一次 —— 下拉框已经跳到新值了，不拉回去的话画面上写着
  //   「运营」而服务端还是「系统管理员」。
  if (result.admins) { renderAdmins(result.admins); }
  else { loadAdmins(); }
  if (result.ok && result.self_demoted) {
    // 把自己降成运营 ⇒ 这一页和「玩家资料」当场就该消失。
    ROLE = "operator";
    $("who").textContent = "已登录：" + name + "（运营）";
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
  say($("adminMsg"), result.message, result.ok);
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
 *  复选框是服务端分好的组（`databackup.groups_of`）：四份运营配置**一个格子**
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
   玩家资料（V0.3商店 D22 的配套：商店按真实等级卖，改数值只能从这儿改）

   模型：`PLAYER.view` 是服务端那份快照，`PLAYER.edit` 是**要提交的补丁**
   —— 两张 `{itemId: 数量}` 表 + 等级 + 金币。删掉一件东西 = 把它的数量写成 0
   （服务端 `admin_update_account()` 就是按「数量 <= 0 删掉这一格」认的），
   所以补丁里必须留着那个 0，不能把键删掉。

   ★ 2026-09-06 之后**仓库里什么都能改**（D23a），没有「锁着的」那一类了。
   ====================================================================== */

var PLAYER = null;        // {view, edit:{level, money, materials, inventory}}
var PLAYER_LIST = [];
var PLAYER_PAGE = {page: 0, pages: 1, total: 0, size: 10, q: ""};

/** 查一页。`page` 省略 = 回第一页（换了查询串就该从头看）。 */
async function searchPlayers(page) {
  var q = $("playerSearch").value.trim();
  if (page === undefined) { page = (q === PLAYER_PAGE.q) ? PLAYER_PAGE.page : 0; }
  var result = await api("/admin/api/players?q=" + encodeURIComponent(q)
                         + "&page=" + page);
  if (bounced(result)) { return false; }
  if (!result.ok) {
    // 回执走右上角浮条（D39）：列表下面那条 `.msg` 2026-09-07 拿掉了 ——
    // 这一页改成「只有列表滚」的壳之后，列表下面什么都不放。
    toast((result && result.message) || "查找失败", false);
    return false;
  }
  PLAYER_LIST = result.players;
  PLAYER_PAGE = {page: result.page, pages: result.pages,
                 total: result.total, size: result.size, q: q};
  renderPlayerRows();
  $("playerCount").textContent = result.total + " 个账号";
  return true;
}

/** 「↻ 刷新」——「玩家资料」和「管理员账号」两页**共用这一发**
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
  if (ok) { toast("已刷新：玩家资料和管理员账号都是最新的。", true); }
}

function renderPlayerRows() {
  var rows = $("playerRows");
  rows.textContent = "";
  if (!PLAYER_LIST.length) {
    var tr = document.createElement("tr");
    var td = el("td", "own-empty", "没有匹配的账号");
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
    // ★ 权限那个钮在**左**、「修改背包」在**右**，整组**右对齐**
    //   （用户 2026-09-06）。两个钮**长得不一样**（D40）：一个改这个人的背包、
    //   一个给他管理页的权限，不能像同一个东西 —— 但靠的是**轻重**不是色相
    //   （D40a，用户 2026-09-07）：「修改背包」是这一行的主动作，金色主钮；
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
    var button = el("button", "btn btn-sm btn-primary", "修改背包");
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
        + "运营只看得到 物品库 / 商店货架 / 合成配方 / 材料掉落 四页。",
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
 *  同一个人重读（保存回执 / 刷新）时停在原来的分类标签上，别跳回「全部」。 */
function adoptPlayer(view) {
  var edit = {level: view.level, money: view.money,
              materials: {}, inventory: {}};
  view.materials.forEach(function (row) { edit.materials[row.id] = row.count; });
  view.inventory.forEach(function (row) { edit.inventory[row.id] = row.count; });
  var tab = (PLAYER && PLAYER.view.username === view.username)
    ? PLAYER.tab : {big: -1, sub: null};
  PLAYER = {view: view, edit: edit, tab: tab};
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
  $("playerOnline").textContent = view.online ? "● 在线，改完即时生效" : "○ 不在线";
  $("playerLevel").value = PLAYER.edit.level;
  $("playerLevel").max = view.level_max;
  $("playerMoney").value = PLAYER.edit.money;
  $("playerExp").value = view.experience + "（本级 " + view.level_start_exp
    + " ~ 下一级 " + view.next_level_exp + "）";
  $("playerModal").classList.remove("hidden");
  repaintOwned();
}

/* ---------------------------------------------------------- 仓库分类
   照**游戏仓库界面**那棵标签树（§41，服务端 `shop.WAREHOUSE_TABS` 随物品表
   一起发）：7 个大分类，每个下面几个小分类；前面多一个「全部」。
   每件物品落在哪一格由服务端算好放在 `item.wh` 里（客户端自己的两个分类
   函数翻过来的），这儿只做匹配：精确相等，或大分类按高半字收。
   材料和仓库物品**不再分两块**（用户 2026-09-07）—— 游戏里它们本来就在
   同一个仓库面板里（材料在「收集品 → 材料」）。 */

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

/** 现在选中的是哪一格：小分类优先，没选小分类就是大分类（或「全部」= -1）。 */
function playerRequested() {
  return PLAYER.tab.sub !== null ? PLAYER.tab.sub : PLAYER.tab.big;
}

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

function switchOwnedTab(big, sub) {
  PLAYER.tab = {big: big, sub: sub};
  renderPlayerTabs();
  renderOwnedList();
}

/** 两行标签。件数只数「有」的（数量 > 0）。「人物 → 英雄」是 `0`，客户端遇到
 *  0 直接返回空 —— 永远是空格子，不画。 */
function renderPlayerTabs() {
  var have = ownedEntries().filter(function (entry) {
    return Number(PLAYER.edit[entry.bucket][entry.id]) > 0;
  });
  function count(id) {
    return have.filter(function (entry) { return whMatches(id, entry.cat); }).length;
  }
  function tabButton(id, label, on, onclick) {
    var button = el("button", "cat" + (on ? " on" : ""), label);
    var n = count(id);
    if (n) { button.appendChild(el("span", "n", String(n))); }
    button.onclick = onclick;
    return button;
  }
  var host = $("playerCats");
  host.textContent = "";
  host.appendChild(tabButton(-1, "全部", PLAYER.tab.big === -1, function () {
    switchOwnedTab(-1, null);
  }));
  whTabs().forEach(function (tab) {
    host.appendChild(tabButton(tab.id, tab.label, PLAYER.tab.big === tab.id,
                               function () { switchOwnedTab(tab.id, null); }));
  });

  var sub = $("playerSubCats");
  sub.textContent = "";
  var big = whTabs().filter(function (tab) { return tab.id === PLAYER.tab.big; })[0];
  var children = big ? (big.children || []).filter(function (child) {
    return child.id !== 0;
  }) : [];
  sub.classList.toggle("hidden", !children.length);
  if (!children.length) { return; }
  sub.appendChild(tabButton(big.id, "全部" + big.label, PLAYER.tab.sub === null,
                            function () { switchOwnedTab(big.id, null); }));
  children.forEach(function (child) {
    sub.appendChild(tabButton(child.id, child.label, PLAYER.tab.sub === child.id,
                              function () { switchOwnedTab(big.id, child.id); }));
  });
}

/** 画当前分类下的格子。★ 重画一律不动滚动条（D37b）。 */
function renderOwnedList() {
  var box = $("playerOwned");
  var keep = box.scrollTop;
  var grid = $("playerOwnedGrid");
  grid.textContent = "";
  var requested = playerRequested();
  var rows = ownedEntries().filter(function (entry) {
    return whMatches(requested, entry.cat);
  });
  if (!rows.length) {
    grid.appendChild(el("div", "own-empty",
                        requested === -1 ? "仓库是空的 —— 点「＋ 添加物品」"
                                         : "这个分类下没有东西"));
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
      var requested = playerRequested();
      items.forEach(function (item) {
        var bucket = item.kind === "material" ? "materials" : "inventory";
        if (!Number(PLAYER.edit[bucket][item.id])) {
          PLAYER.edit[bucket][item.id] = 1;
          added += 1;
        }
        if (!whMatches(requested, whCategory(item.id))) { hidden += 1; }
      });
      if (hidden) { PLAYER.tab = {big: -1, sub: null}; }
      repaintOwned();
      toast("已加入 " + added + " 件，按「保存」才真的发给玩家"
            + (hidden ? "；有的不在刚才那个分类里，已切到「全部」" : ""), true);
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

/** 弹窗里的「↻ 刷新」：玩家资料 + 物品表 + 四份运营配置（名字 / 等级门槛的出处）
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
  if (ok) { toast("已刷新：玩家资料和物品信息都是最新的。", true); }
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
   两档（D34）：`system` 系统管理员 = 全部标签页；
                `operator` 运营 = 只有那四个配置页。

   ★ 这里做的**只是把标签藏起来**，不是安全边界 —— 藏掉的按钮拦不住直接
     POST。真正的门在服务端 `_require_system_admin()` 里，两边都要有。
   ------------------------------------------------------------------- */
var ROLE = null;                       // "system" / "operator" / null（没登录）

//: 现在停在哪个标签页。★ 和 `CURRENT` 不是一回事 —— `CURRENT` 只记那四个
//  **配置**页（渲染要用），「玩家资料」和「管理员账号」不在里面。
var TAB = "items";

//: 只有系统管理员能进的标签页（数据备份也是：它能回滚玩家存档）。
var SYSTEM_ONLY_TABS = ["players", "backup", "admins"];

function isSystemAdmin() { return ROLE === "system"; }

function canOpenTab(tab) {
  return isSystemAdmin() || SYSTEM_ONLY_TABS.indexOf(tab) < 0;
}

/** 按当前权限决定哪几个标签露出来。 */
function applyRoleToTabs() {
  Array.prototype.forEach.call($("tabs").children, function (button) {
    var tab = button.getAttribute("data-tab");
    button.classList.toggle("hidden", !canOpenTab(tab));
  });
  // 权限被现场降级时，人可能正停在一个已经不该看的页上 —— 拉回物品库。
  // ★ `CAT` 还没到手就别切：`switchTab` 会去 `renderCurrent()`，
  //   那一步读 `CAT.schema`（登录的那一瞬间它还是 null）。
  if (CAT && !canOpenTab(TAB)) { switchTab("items"); }
}

function showLoggedIn(name, role) {
  ROLE = role || null;
  $("who").textContent = "已登录：" + name
    + (isSystemAdmin() ? "（系统管理员）" : "（运营）");
  $("logout").classList.remove("hidden");
  $("loginView").classList.add("hidden");
  $("mainView").classList.remove("hidden");
  // 标签行在顶栏那块壳里，不跟着 `mainView` 走 —— 自己开关一次。
  $("tabs").classList.remove("hidden");
  // 登进来默认停在物品库 ⇒ 直接进「撑满 + 列表自己滚」那套（D39）。
  // 之后换标签由 `switchTab` 管。
  $("mainArea").classList.toggle("fit", CONFIGS.indexOf(TAB) >= 0);
  applyRoleToTabs();
  boot();
}

function showLoggedOut(message) {
  CAT = null;
  ROLE = null;
  // 下一个登进来的人可能权限不同 —— 停在哪一页得跟着回到起点。
  TAB = "items";
  CURRENT = "items";
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
  // ★ 运营根本进不去这一页，也别去要那份名单（服务端会回 403）。
  if (isSystemAdmin()) { loadAdmins(); }
}

function switchTab(tab) {
  // ★ 第二道保险：标签已经藏起来了，但键盘 / 脚本还是点得到。
  if (!canOpenTab(tab)) { tab = "items"; }
  TAB = tab;
  Array.prototype.forEach.call($("tabs").children, function (button) {
    button.classList.toggle("on", button.getAttribute("data-tab") === tab);
  });
  var isConfig = CONFIGS.indexOf(tab) >= 0;
  // ★ 四个配置页、数据备份页和玩家资料页是「面板撑满、列表自己滚」（D39；
  //   玩家资料页 2026-09-07 加进来）；管理员账号是普通长页面，整块跟着
  //   `main` 滚。
  $("mainArea").classList.toggle("fit",
                                 isConfig || tab === "backup" || tab === "players");
  $("cfgPanel").classList.toggle("hidden", !isConfig);
  $("adminsPanel").classList.toggle("hidden", tab !== "admins");
  $("playersPanel").classList.toggle("hidden", tab !== "players");
  $("backupPanel").classList.toggle("hidden", tab !== "backup");
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
  renderCurrent();
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
  // 玩家背包弹窗（用户 2026-09-07）：只有 ✕ 能关，点遮罩不关 —— 所以
  // `#playerModal` 故意**没有** onclick。
  $("playerAddItem").onclick = function () { addOwnedMany(); };
  $("playerPopupRefresh").onclick = function () { refreshPlayerPopup(); };
  $("playerClose").onclick = function () { closePlayerModal(); };
  $("playerSave").onclick = savePlayer;
  $("playerReset").onclick = function () {
    if (PLAYER) { openPlayer(PLAYER.view.username, true); }
  };

  $("pickSearch").oninput = function () {
    PICKER.q = $("pickSearch").value.trim();
    PICKER.page = 0;
    paintPicker();
  };
  $("pickKind").onchange = function () {
    PICKER.kind = $("pickKind").value;
    PICKER.page = 0;
    paintPicker();
  };
  $("pickCharacter").onchange = function () {
    PICKER.character = $("pickCharacter").value;
    PICKER.page = 0;
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
  document.addEventListener("keydown", function (event) {
    // ★ 对话框排在选择器前面：它是**盖在**选择器上面的那一层
    //   （「加一种材料」的弹窗上再弹确认框时，Esc 该先关掉上面那个）。
    if (DIALOG) {
      if (event.key === "Escape") { closeDialog(false); }
      // 危险操作（红钮）的框不认回车 —— 焦点在「取消」上，让它自己走。
      else if (event.key === "Enter" && !DIALOG.danger) { closeDialog(true); }
      return;
    }
    if (event.key === "Escape" && PICKER) { closePicker(); }
  });

  $("addAdmin").onclick = async function () {
    // ★ 权限**永远明确传**，不指望服务端的默认值 —— 页面上那个下拉框
    //   默认是「运营」（加人先给最小权限，要全权得自己点一下）。
    var result = await api("/admin/api/admins/add", {
      name: $("newAdminName").value, password: $("newAdminPass").value,
      role: $("newAdminRole").value});
    say($("adminMsg"), result.message, result.ok);
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
    say($("adminMsg"), result.message, result.ok);
  };

  // 浮条的位置跟着标题栏走：窗口变窄标题栏可能换行变高，重新量一次。
  window.addEventListener("resize", placeToasts);

  // 关标签页前拦一下 —— 表单页最容易「改了半天忘了按保存」。
  window.addEventListener("beforeunload", function (event) {
    if (CAT && (CONFIGS.some(isDirty) || playerDirty() || backupSettingsDirty())) {
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
      // ★ 它追加在末尾 ⇒ 清掉筛选之后还得**翻到最后一页**。
      FILTER[which] = emptyFilter();
      FILTER[which].page = pageCount(CFG[which].entries.length) - 1;
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
