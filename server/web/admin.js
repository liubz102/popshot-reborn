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

var DIALOG = null;            // {resolve} —— 正开着的那一个

/** 弹一个对话框。返回 `Promise<boolean>`（确定 = true）。
 *
 *  options = {title, lead, lists: [{label, bad, rows: [{label, reason}]}],
 *             ok, cancel}
 *  `cancel` 传 `null` = 只有一个「知道了」，一定 resolve(false)。
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
  if (options.cancel !== null) {
    var no = el("button", "btn", options.cancel || "取消");
    no.onclick = function () { closeDialog(false); };
    buttons.appendChild(no);
  }
  var yes = el("button", "btn btn-primary", options.ok || "确定");
  yes.onclick = function () { closeDialog(true); };
  buttons.appendChild(yes);

  $("dialog").classList.remove("hidden");
  yes.focus();
  return new Promise(function (resolve) {
    DIALOG = {resolve: resolve, single: options.cancel === null};
  });
}

function closeDialog(answer) {
  if (!DIALOG) { return; }
  var open = DIALOG;
  DIALOG = null;
  $("dialog").classList.add("hidden");
  open.resolve(open.single ? false : !!answer);
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

/** 这件东西现在在商店里是什么状态。★ 取**当前标签页模型**，和
    `listedIds()` 一个口径：刚改完还没保存也照着新值说。 */
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
  var lab = el("span", "lab", spec.label || spec.key);
  if (spec.help) { lab.title = spec.help; }
  wrap.appendChild(lab);

  if (spec.readonly) {
    wrap.appendChild(el("span", "ro", format(entry[spec.key])));
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
  if (!result.ok) { say($("cfgMsg"), result.message, false); return false; }
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
    say($("cfgMsg"), "服务端上那份 " + which + ".json 不是合法 JSON："
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
    say($("cfgMsg"), CAT.schema[which].title + "：" + result.message, false);
    if (which === CURRENT) { markBadCard(result.message); }
    return false;
  }
  // ★ 换成**落盘后的那一份** —— 别人改的东西这一刻就出现在画面上
  //   （用户 2026-09-06：「保存后画面直接更新显示 merge 后的最新状态」）。
  adoptConfig(which, result.text, []);
  if (which === CURRENT) { renderCurrent(); }
  say($("cfgMsg"), result.message, true);
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
    say($("cfgMsg"), "没有保存 —— 另一个人刚改了同样的东西，"
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
    say($("cfgMsg"), "已取消，什么都没保存。", false);
    return false;
  }
  // ★ **重新发一次，服务端会重新判一遍** —— 从我按下确定到这一发落地之间，
  //   第三个人可能刚好也改了同一条（用户 2026-09-06 明确要求）。
  var ok = await postConfig(which, mergeable.map(function (row) {
    return row.key;
  }));
  if (ok) {
    say($("cfgMsg"), "已单独提交 " + mergeable.length + " 件未冲突的；冲突的 "
        + conflicts.length + " 件已换成服务端上最新的内容，请重新修改。", false);
  }
  return ok;
}

async function saveConfig(which, skipClashCheck) {
  var clash = [];
  if (!skipClashCheck && OTHER_LISTING[which]) {
    clash = listingClash(which);
    if (clash.length && !(await confirmClash(which, clash))) {
      say($("cfgMsg"), "已取消，什么都没保存。", false);
      return false;
    }
  }
  say($("cfgMsg"), "保存中……", true);
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
      say($("cfgMsg"), CAT.schema[which].title + "已保存，但「"
          + CAT.schema[other].title + "」的自动下架没能存进去 —— "
          + "这件东西现在两边都上着架。切到那一页按一次保存。", false);
      return false;
    }
    say($("cfgMsg"), "已保存，并把 " + clash.length + " 件东西从「"
        + CAT.schema[other].title + "」下架了。", true);
  }
  return true;
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
  card.scrollIntoView({block: "center", behavior: "smooth"});
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
  say($("cfgMsg"), notes.join("\n"), false);

  // ★ 物品库没有「＋ 添加一条」：条目由 `shop_items.json` 定死，加不出新物品。
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
    bar.appendChild(selectFilter(filter, "character", "全部角色",
      Object.keys(CAT.characters).map(function (k) {
        return {value: k, label: CAT.characters[k]}; })));
  }
  if (which === "drops") {
    bar.appendChild(selectFilter(filter, "mode", "全部模式",
      [{value: "quest", label: "闯关"}, {value: "pvp", label: "对战"}]));
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
  return {q: "", kind: "", character: "", listedOnly: false, page: 0};
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
  list.textContent = "";
  var view = pageRows(CURRENT);
  // ★ 换页栏只有**列表上面这一条**（D37b）。只有一页时 `pagerNode` 回 null
  //   —— 空的 `.pager` 也占一截外边距，短列表上多出一条空白很显眼。
  var pager = pagerNode(CURRENT, view);
  if (pager) { list.appendChild(pager); }
  RENDERERS[CURRENT](list, view.rows);
  touched();
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
  var itemId = entry.id || entry.result || entry.material;
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
  return rows;
}

/** 一页画几条（D37）。★ 这是**界面取舍**（一次铺 800 张卡 DOM 会卡手），
 *  不是铁律 10 说的那种时序阈值 —— 超出的翻页，不再是「剩下的不画了」。 */
var PAGE_SIZE = 120;

function pageCount(total) {
  return Math.max(1, Math.ceil(total / PAGE_SIZE));
}

/** 这一页要画的那些记录，顺带把「筛出 x / y　第 m / n 页」写上。 */
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
    var parts = [];
    if (all.length !== total) {
      parts.push("筛出 " + all.length + " / " + total);
    }
    if (pages > 1) { parts.push("第 " + (page + 1) + " / " + pages + " 页"); }
    label.textContent = parts.join("　");
  }
  return {rows: all.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE),
          pages: pages, page: page, total: all.length};
}

/** 换页栏，**只放在列表上面这一条**（D37b）。只有一页时整条不画。
 *
 *  ★ 换页**一律不动滚动条**：点哪个按钮都只换内容，画面停在原处。
 *    列表下面原来还有一条，删掉了 —— 在底下换到末页（末页是半页，列表
 *    真的变短）时，原位置越过新的底，浏览器一夹画面就是一跳，怎么写都
 *    躲不掉。栏子只留在顶上，点它的时候人本来就在顶上，没得可夹。 */
function pagerNode(which, view) {
  if (view.pages <= 1) { return null; }
  var host = el("div", "pager");
  function step(text, target, disabled) {
    var button = el("button", "btn btn-sm", text);
    button.disabled = disabled;
    button.onclick = function () {
      var keep = window.scrollY;
      FILTER[which].page = target;
      repaintList();
      // 清空再填是同一个任务里做完的，浏览器本来就不会动滚动条；这一发
      // 是把「不许动」写死，免得日后谁往重画里插一句读版面的代码，位置
      // 就被夹没了。末页比整页短、原位置越界时浏览器自己会夹回来。
      window.scrollTo(0, keep);
    };
    host.appendChild(button);
  }
  step("‹ 上一页", view.page - 1, view.page <= 0);
  host.appendChild(el("span", "pageno",
                      "第 " + (view.page + 1) + " / " + view.pages + " 页　共 "
                      + view.total + " 条"));
  step("下一页 ›", view.page + 1, view.page >= view.pages - 1);
  return host;
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
 * ★ 取的是**当前页面模型**（还没保存的改动也算）—— 和 `listedIds()`
 *   一个口径：管理员刚把一件东西勾上，浮窗和筛选就该跟着说。
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
      cell.appendChild(el("div", "nmz", (BYID[material.id] || {}).name
                                        || ("#" + material.id)));
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
    var entry = line.entry, index = line.index;
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
    who.appendChild(el("div", "nmz", (BYID[entry.material] || {}).name
                                     || ("#" + entry.material)));

    var placed = false;
    restFields("drops", entry, ["material"], touched).forEach(function (node) {
      // 材料格子插在「难度」后面，和原来 json 里的字段顺序一致。
      row.appendChild(node);
      if (!placed && node.querySelector
          && node.firstChild && node.firstChild.textContent === "难度") {
        row.appendChild(who);
        placed = true;
      }
    });
    if (!placed) { row.appendChild(who); }
    list.appendChild(row);
  });
}

/* ======================================================================
   物品选择器
   ====================================================================== */

var PICKER = null;

function openPicker(options) {
  PICKER = {
    kinds: options.kinds || null,
    selected: options.selected,
    onPick: options.onPick,
    // 额外的一道过滤（玩家资料页拿它挡掉「商店在卖的」）。
    filter: options.filter || null,
    q: "",
    page: 0,
    kind: (options.kinds && options.kinds.length === 1) ? options.kinds[0] : ""
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
  kindSelect.disabled = !!(PICKER.kinds && PICKER.kinds.length === 1);
  $("picker").classList.remove("hidden");
  $("pickDetail").textContent = "";
  paintPicker();
  $("pickSearch").focus();
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
      var body = $("picker").querySelector(".panel-body");
      var keep = body ? body.scrollTop : 0;
      PICKER.page = target;
      paintPicker();
      if (body) { body.scrollTop = keep; }
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
    if (PICKER.filter && !PICKER.filter(item)) { return false; }
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
    var cell = el("div", "pick" + (item.id === PICKER.selected ? " sel" : ""));
    var ic = el("div", "ic");
    if (iconStyle(ic, item.cell, 44)) { cell.appendChild(ic); }
    else { cell.appendChild(el("div", "noicon", "?")); }
    cell.appendChild(el("div", "nmz", itemName(item.id)));
    tipFor(cell, item.id);
    cell.onmouseenter = function () { describe(item); };
    cell.onclick = function () {
      var pick = PICKER.onPick;
      closePicker();
      pick(item);
    };
    grid.appendChild(cell);
  });
  paintPickPager($("pickPagerTop"), pages);
  $("pickCount").textContent = pages > 1
    ? ("共 " + hits.length + " 件 · 第 " + (PICKER.page + 1) + " / "
       + pages + " 页")
    : (hits.length + " 件");
}

/** 侧栏：这件东西是什么、**现在在商店里卖不卖**。 */
async function describe(item) {
  var box = $("pickDetail");
  box.textContent = "";
  var line = el("div");
  line.appendChild(el("b", null, itemName(item.id)));
  line.appendChild(document.createTextNode(
    "  #" + item.id + "  " + itemMeta(item.id)));
  box.appendChild(line);
  if (item.name_kr) { box.appendChild(el("div", null, "原名：" + item.name_kr)); }
  if (item.bonus) {
    box.appendChild(el("div", null, "加成：" + JSON.stringify(item.bonus)));
  }
  if (item.weapon) {
    var w = item.weapon;
    box.appendChild(el("div", null, "伤害 " + w.damage + " / 爆头 "
      + w.head_damage + " / 换弹 " + w.reload_ms + "ms"));
  }
  var result = await api("/admin/api/item?id=" + encodeURIComponent(item.id));
  if (!PICKER || !result.ok) { return; }
  box.appendChild(el("div", null, result.listed
    ? ("★ 商店里在卖，" + result.price + " 金币")
    : "商店里没上架"));
}

/* ======================================================================
   管理员账号
   ====================================================================== */

var ROLE_ZH = {system: "系统管理员", operator: "运营"};

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
  if (bounced(result) || !result.ok) { return; }
  renderAdmins(result.admins);
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
   玩家资料（V0.3商店 D22 的配套：商店按真实等级卖，改数值只能从这儿改）

   模型：`PLAYER.view` 是服务端那份快照，`PLAYER.edit` 是**要提交的补丁**
   —— 两张 `{itemId: 数量}` 表 + 等级 + 金币。删掉一件东西 = 把它的数量写成 0
   （服务端 `admin_update_account()` 就是按「数量 <= 0 删掉这一格」认的），
   所以补丁里必须留着那个 0，不能把键删掉。

   ★ 商店在卖的（`listed`）一律不进补丁 —— 服务端也会再拦一次。
   ====================================================================== */

var PLAYER = null;        // {view, edit:{level, money, materials, inventory}}
var PLAYER_LIST = [];
var PLAYER_PAGE = {page: 0, pages: 1, total: 0, size: 10, q: ""};

/** 现在商店里在卖哪些 id。★ 取的是**当前标签页模型**里的 shop 条目 ——
    管理员刚在「商店货架」里改了上架状态还没保存时，这边跟着一起变，
    免得画面上说「能改」、点了保存服务端说「不能改」。 */
function listedIds() {
  var set = {};
  ((CFG.shop && CFG.shop.entries) || []).forEach(function (entry) {
    if (entry && entry.listed) { set[Number(entry.id)] = true; }
  });
  return set;
}

/** 查一页。`page` 省略 = 回第一页（换了查询串就该从头看）。 */
async function searchPlayers(page) {
  var q = $("playerSearch").value.trim();
  if (page === undefined) { page = (q === PLAYER_PAGE.q) ? PLAYER_PAGE.page : 0; }
  var result = await api("/admin/api/players?q=" + encodeURIComponent(q)
                         + "&page=" + page);
  if (bounced(result) || !result.ok) {
    say($("playerMsg"), (result && result.message) || "查找失败", false);
    return false;
  }
  PLAYER_LIST = result.players;
  PLAYER_PAGE = {page: result.page, pages: result.pages,
                 total: result.total, size: result.size, q: q};
  renderPlayerRows();
  $("playerCount").textContent = result.total + " 个账号";
  say($("playerMsg"), "");
  return true;
}

/** 「↻ 刷新」：重读列表 + 重读正在改的那个人（用户 2026-09-05 要的）。
 *
 * ★ **停在当前页、保留搜索串** —— 刷新不该把人弹回第一页。
 * ★ 有没保存的改动先问一句：刷新会拿服务端那份盖掉编辑区，和 `openPlayer`
 *   同一个口径（那边已经这么问了，两处别不一致）。
 */
async function refreshPlayers() {
  if (PLAYER && playerDirty()
      && !(await ask({title: "还有没保存的改动",
                      lead: "「" + PLAYER.view.username
                            + "」还有没保存的改动，刷新会丢掉，确定？",
                      ok: "刷新"}))) {
    return;
  }
  var open = PLAYER ? PLAYER.view.username : null;
  say($("playerMsg"), "刷新中……", true);
  // 不带页码 = `searchPlayers` 自己那套：搜索串没变就停在当前页，
  // 变了（用户改了输入框但没按查找）就回第一页。
  var ok = await searchPlayers();
  // ★ `force` = 别再问一次「确定丢掉改动」—— 上面已经问过了。
  if (ok && open) { ok = await openPlayer(open, true); }
  // ★ 失败时**不要**盖掉错误信息 —— 「已刷新」压在「请先登录」上面，
  //   用户看到的就是「点了刷新，然后什么都没变」。
  if (ok) { say($("playerMsg"), "已刷新", true); }
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
    var button = el("button", "btn btn-sm", "修改");
    button.onclick = function () { openPlayer(row.username); };
    td.appendChild(button);
    line.appendChild(td);
    rows.appendChild(line);
  });
  renderPlayerPager();
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
  if (bounced(result) || !result.ok) {
    say($("playerMsg"), (result && result.message) || "读不到这个账号", false);
    return false;
  }
  adoptPlayer(result.player);
  return true;
}

/** 把服务端那份快照变成「快照 + 补丁」。 */
function adoptPlayer(view) {
  var edit = {level: view.level, money: view.money,
              materials: {}, inventory: {}};
  view.materials.forEach(function (row) {
    if (!row.locked) { edit.materials[row.id] = row.count; }
  });
  view.inventory.forEach(function (row) {
    if (!row.locked) { edit.inventory[row.id] = row.count; }
  });
  PLAYER = {view: view, edit: edit};
  say($("playerMsg"), "");
  renderPlayer();
  renderPlayerRows();
}

function playerDirty() {
  if (!PLAYER) { return false; }
  var edit = PLAYER.edit;
  var view = PLAYER.view;
  if (Number(edit.level) !== view.level) { return true; }
  if (Number(edit.money) !== view.money) { return true; }
  return ["materials", "inventory"].some(function (bucket) {
    var was = {};
    view[bucket].forEach(function (row) {
      if (!row.locked) { was[row.id] = row.count; }
    });
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
  $("playerEdit").classList.remove("hidden");
  $("playerFoot").classList.remove("hidden");
  $("playerWho").textContent = view.nickname + "（" + view.username + "）";
  $("playerOnline").textContent = view.online ? "● 在线，改完即时生效" : "不在线";
  $("playerLevel").value = PLAYER.edit.level;
  $("playerLevel").max = view.level_max;
  $("playerMoney").value = PLAYER.edit.money;
  $("playerExp").value = view.experience + "（本级 " + view.level_start_exp
    + " ~ 下一级 " + view.next_level_exp + "）";
  paintOwned("materials", $("playerMaterials"), "还没有任何材料");
  paintOwned("inventory", $("playerInventory"), "仓库是空的");
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

/** 画一整格 —— 锁着的（商店在卖）只显示，不给改。 */
function paintOwned(bucket, host, emptyText) {
  host.textContent = "";
  var view = PLAYER.view;
  var lockedRows = view[bucket].filter(function (row) { return row.locked; });
  var ids = Object.keys(PLAYER.edit[bucket]).map(Number)
    .sort(function (a, b) { return a - b; });
  if (!ids.length && !lockedRows.length) {
    host.appendChild(el("div", "own-empty", emptyText));
    return;
  }
  ids.forEach(function (itemId) { host.appendChild(ownNode(bucket, itemId)); });
  lockedRows.forEach(function (row) { host.appendChild(lockedNode(row)); });
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
      renderPlayer();
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
  };
  box.appendChild(input);
  var drop = el("button", "btn btn-sm btn-danger drop", "✕");
  drop.title = "清零 —— 保存后这一格就没了";
  drop.onclick = function () {
    PLAYER.edit[bucket][itemId] = 0;
    input.value = 0;
    playerTouched();
  };
  box.appendChild(drop);
  return box;
}

function lockedNode(row) {
  var box = el("div", "own locked");
  box.appendChild(slotNode(row.id, 26, true, false));
  var col = tipFor(el("div", "col"), row.id);
  col.appendChild(el("div", "nmz", ownName(row.id)));
  var meta = el("div", "meta",
                row.stackable === false ? ownMeta(row.id)
                                        : (ownMeta(row.id) + " ×" + row.count));
  if (row.equipped) { meta.appendChild(el("span", "worn", "穿着")); }
  col.appendChild(meta);
  box.appendChild(col);
  var lock = el("span", "lock", "🔒");
  lock.title = "商店在卖的东西不能直接改 —— 改金币和等级，让玩家自己去买";
  box.appendChild(lock);
  return box;
}

/** 「＋ 加一种」。已经有的就把它加回来，别加出两张一样的格子。 */
function addOwned(bucket, kinds) {
  var listed = listedIds();
  openPicker({
    kinds: kinds,
    filter: function (item) { return !listed[item.id]; },
    onPick: function (item) {
      if (!Number(PLAYER.edit[bucket][item.id])) {
        PLAYER.edit[bucket][item.id] = 1;
      }
      renderPlayer();
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
  say($("playerMsg"), "保存中……", true);
  var result = await api("/admin/api/player", payload);
  if (bounced(result)) { return; }
  if (result.ok && result.player) {
    adoptPlayer(result.player);
    await searchPlayers();           // 列表里的等级 / 金币跟着更新
  }
  // ★ 这一句要排在最后：上面两个都会把消息条清空（它们各自也是入口），
  //   先说再刷的话「已保存：…」会当场被擦掉。
  say($("playerMsg"), result.message, result.ok);
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

//: 只有系统管理员能进的标签页。
var SYSTEM_ONLY_TABS = ["players", "admins"];

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
  // 标签行在顶栏那块 sticky 容器里，不跟着 `mainView` 走 —— 自己开关一次。
  $("tabs").classList.remove("hidden");
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
  $("playerEdit").classList.add("hidden");
  $("playerFoot").classList.add("hidden");
  $("who").textContent = "";
  $("logout").classList.add("hidden");
  $("mainView").classList.add("hidden");
  $("tabs").classList.add("hidden");
  $("loginView").classList.remove("hidden");
  if (message) { say($("loginMsg"), message, false); }
}

async function boot() {
  say($("cfgMsg"), "读取中……", true);
  var result = await api("/admin/api/catalog");
  if (bounced(result)) { return; }
  if (!result.ok) { say($("cfgMsg"), result.message, false); return; }
  CAT = result;
  BYID = {};
  CAT.items.forEach(function (item) { BYID[item.id] = item; });
  if (!CAT.icons) {
    say($("cfgMsg"), "图标图集没生成（server/web/itemicons.png）——"
                     + "先跑 tools\\update-shopicons.bat，格子里会一直是问号。",
        false);
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
  $("cfgPanel").classList.toggle("hidden", !isConfig);
  $("adminsPanel").classList.toggle("hidden", tab !== "admins");
  $("playersPanel").classList.toggle("hidden", tab !== "players");
  if (tab === "players") {
    // 第一次切进来先列几个，免得画面上是一片空白。
    if (!PLAYER_LIST.length) { searchPlayers(); }
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
  // 同上：不能直接挂 `refreshPlayers`（Event 会被当第一个参数）。
  $("playerRefreshBtn").onclick = function () { refreshPlayers(); };
  $("playerLevel").oninput = function () {
    PLAYER.edit.level = Math.max(1, Number($("playerLevel").value) || 1);
    playerTouched();
  };
  $("playerMoney").oninput = function () {
    PLAYER.edit.money = Math.max(0, Number($("playerMoney").value) || 0);
    playerTouched();
  };
  $("playerAddMaterial").onclick = function () {
    addOwned("materials", ["material"]);
  };
  $("playerAddItem").onclick = function () { addOwned("inventory", null); };
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
  $("pickClose").onclick = closePicker;
  $("picker").onclick = function (event) {
    if (event.target === $("picker")) { closePicker(); }
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
      else if (event.key === "Enter") { closeDialog(true); }
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

  // 关标签页前拦一下 —— 表单页最容易「改了半天忘了按保存」。
  window.addEventListener("beforeunload", function (event) {
    if (CAT && (CONFIGS.some(isDirty) || playerDirty())) {
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
      if (card) { card.scrollIntoView({block: "center", behavior: "smooth"}); }
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
