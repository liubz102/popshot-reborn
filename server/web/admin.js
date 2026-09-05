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
var CURRENT = "shop";       // 当前标签页
var FILTER = {};            // 每个标签页各自的筛选条件

var CONFIGS = ["shop", "recipe", "drops"];

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
  box.title = itemLabel(itemId);
  return box;
}

function itemLabel(itemId) {
  var item = BYID[itemId];
  if (!item) { return "#" + itemId + "（物品表里没有这个 id）"; }
  var bits = [item.name];
  if (item.name_kr && item.name_kr !== item.name) { bits.push(item.name_kr); }
  return bits.join(" / ") + "  #" + item.id;
}

function itemMeta(itemId) {
  var item = BYID[itemId];
  if (!item) { return "★ 物品表里没有这个 id"; }
  var bits = [CAT.kinds[item.kind] || item.kind];
  if (item.character !== undefined) {
    bits.push(CAT.characters[String(item.character)] || ("角色" + item.character));
  }
  if (item.series) {
    bits.push((CAT.series[item.series] || item.series) + (item.tier || ""));
  }
  return bits.join(" · ");
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
  var raw;
  try {
    raw = JSON.parse(result.text);
  } catch (error) {
    say($("cfgMsg"), "服务端上那份 " + which + ".json 不是合法 JSON："
                     + error.message, false);
    return false;
  }
  var entries = raw[listKey(which)];
  CFG[which] = {
    format: raw.format,
    entries: Array.isArray(entries) ? entries : [],
    warnings: result.warnings || [],
    path: result.path || "",
    // 老文件里可能还留着 `_说明`。保存后它会消失（D16），先说一声。
    hadNotes: Object.keys(raw).some(function (k) { return k.charAt(0) === "_"; })
  };
  CFG[which].snapshot = snapshot(which);
  return true;
}

function collect(which) {
  var payload = {};
  payload.format = (CFG[which].format === undefined) ? 1 : CFG[which].format;
  payload[listKey(which)] = CFG[which].entries;
  return payload;
}

async function saveConfig(which) {
  say($("cfgMsg"), "保存中……", true);
  clearBadCards();
  var result = await api("/admin/api/config/" + which,
                         {text: JSON.stringify(collect(which), null, 2)});
  if (bounced(result)) { return; }
  if (result.ok) {
    CFG[which].snapshot = snapshot(which);
    CFG[which].hadNotes = false;
    CFG[which].warnings = [];
    renderCurrent();
    say($("cfgMsg"), result.message, true);
    return;
  }
  say($("cfgMsg"), result.message, false);
  markBadCard(result.message);
}

/** 服务端的错误里带着下标（`recipes[3].materials[1].id：…`），定位过去。 */
function markBadCard(message) {
  var match = /\[(\d+)\]/.exec(message || "");
  if (!match) { return; }
  var card = $("cfgList").querySelector('[data-index="' + match[1] + '"]');
  if (!card) { return; }
  card.classList.add("bad", "flash");
  card.scrollIntoView({block: "center", behavior: "smooth"});
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

  renderToolbar(which);
  var list = $("cfgList");
  list.textContent = "";
  var render = {shop: renderShop, recipe: renderRecipe, drops: renderDrops}[which];
  render(list);
  touched();
}

/* ------------------------------------------------------------ 工具条 */
function renderToolbar(which) {
  var bar = $("cfgToolbar");
  bar.textContent = "";
  if (!FILTER[which]) { FILTER[which] = {q: "", kind: "", character: "", listedOnly: false}; }
  var filter = FILTER[which];

  var search = document.createElement("input");
  search.type = "text";
  search.placeholder = "搜 中文名 / 韩文名 / id";
  search.value = filter.q;
  search.oninput = function () { filter.q = search.value.trim(); repaintList(); };
  bar.appendChild(search);

  if (which === "shop") {
    bar.appendChild(selectFilter(filter, "kind", "全部类别",
      uniq(CFG.shop.entries.map(function (e) { return e.kind; }))
        .map(function (k) { return {value: k, label: CAT.kinds[k] || k}; })));
    bar.appendChild(selectFilter(filter, "character", "全部角色",
      Object.keys(CAT.characters).map(function (k) {
        return {value: k, label: CAT.characters[k]}; })));
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
  if (which !== "drops") {
    var only = el("label", "toggle" + (filter.listedOnly ? " on" : ""));
    only.appendChild(el("span", "track"));
    only.appendChild(el("span", null, "只看上架"));
    only.onclick = function (event) {
      event.preventDefault();
      filter.listedOnly = !filter.listedOnly;
      only.classList.toggle("on", filter.listedOnly);
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
  select.onchange = function () { filter[key] = select.value; repaintList(); };
  return select;
}

function uniq(values) {
  var seen = {}, out = [];
  values.forEach(function (v) { if (!seen[v]) { seen[v] = 1; out.push(v); } });
  return out.sort();
}

function repaintList() {
  var list = $("cfgList");
  list.textContent = "";
  ({shop: renderShop, recipe: renderRecipe, drops: renderDrops})[CURRENT](list);
  touched();
}

/** 一条记录过不过筛选。**下标一律用原数组的**，服务端报错才对得上。 */
function matches(which, entry) {
  var filter = FILTER[which] || {};
  if (filter.listedOnly && !entry.listed) { return false; }
  if (filter.mode && (entry.mode || "quest") !== filter.mode) { return false; }
  var itemId = entry.id || entry.result || entry.material;
  var item = BYID[itemId];
  if (filter.kind && entry.kind !== filter.kind
      && (!item || item.kind !== filter.kind)) { return false; }
  if (filter.character !== undefined && filter.character !== "") {
    var who = (entry.character !== undefined && entry.character !== null)
      ? entry.character : (item ? item.character : undefined);
    if (String(who) !== filter.character) { return false; }
  }
  if (filter.q) {
    var hay = [entry.name, entry.note, String(itemId),
               item ? item.name_kr : "", item ? item.name : ""]
      .join(" ").toLowerCase();
    if (hay.indexOf(filter.q.toLowerCase()) < 0) { return false; }
  }
  return true;
}

function eachVisible(which, run) {
  var shown = 0;
  CFG[which].entries.forEach(function (entry, index) {
    if (!matches(which, entry)) { return; }
    shown += 1;
    run(entry, index);
  });
  var label = $("cfgShown");
  if (label) {
    var total = CFG[which].entries.length;
    label.textContent = shown === total ? ""
      : ("筛出 " + shown + " / " + total);
  }
  return shown;
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

/* -------------------------------------------------- 商店目录：卡片网格 */
function renderShop(list) {
  var grid = el("div", "grid");
  eachVisible("shop", function (entry, index) {
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
    var nm = el("div", "nm");
    nm.appendChild(fieldNode({key: "name", label: "中文名", type: "text"},
                             entry, touched).lastChild);
    col.appendChild(nm);
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
  var meta = el("div", "meta");
  meta.title = itemLabel(itemId);      // 一行放不下时被省略号截掉，鼠标停上去看全
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

/** 换了物品之后，跟着它走的那几个字段一起更新。 */
function adoptItem(entry, key, item) {
  var old = BYID[entry[key]];
  entry[key] = item.id;
  if ("kind" in entry) { entry.kind = item.kind; }
  // 名字只在「还是上一件的默认名 / 空」时才跟着换 —— 用户自己改过的不动。
  if ("name" in entry && (!entry.name || (old && entry.name === old.name))) {
    entry.name = item.name;
  }
  touched();
}

/* -------------------------------------------------- 合成配方：配方卡 */
function renderRecipe(list) {
  eachVisible("recipe", function (entry, index) {
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
    col.appendChild(fieldNode({key: "name", label: "中文名", type: "text"},
                              entry, touched).lastChild);
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
function renderDrops(list) {
  eachVisible("drops", function (entry, index) {
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

/** 一次最多画这么多格。808 件全铺出来是几千像素高的一张网，搜索框会卡手。 */
var PICK_LIMIT = 200;

function paintPicker() {
  var grid = $("pickGrid");
  grid.textContent = "";
  var query = PICKER.q.toLowerCase();
  var hits = CAT.items.filter(function (item) {
    if (PICKER.kinds && PICKER.kinds.indexOf(item.kind) < 0) { return false; }
    if (PICKER.kind && item.kind !== PICKER.kind) { return false; }
    if (PICKER.filter && !PICKER.filter(item)) { return false; }
    if (!query) { return true; }
    return (item.name + " " + (item.name_kr || "") + " " + item.id)
      .toLowerCase().indexOf(query) >= 0;
  });
  if (!hits.length) {
    grid.appendChild(el("div", "pick-empty", "没有匹配的物品"));
  }
  hits.slice(0, PICK_LIMIT).forEach(function (item) {
    var cell = el("div", "pick" + (item.id === PICKER.selected ? " sel" : ""));
    var ic = el("div", "ic");
    if (iconStyle(ic, item.cell, 44)) { cell.appendChild(ic); }
    else { cell.appendChild(el("div", "noicon", "?")); }
    cell.appendChild(el("div", "nmz", item.name));
    cell.title = itemLabel(item.id);
    cell.onmouseenter = function () { describe(item); };
    cell.onclick = function () {
      var pick = PICKER.onPick;
      closePicker();
      pick(item);
    };
    grid.appendChild(cell);
  });
  $("pickCount").textContent = hits.length > PICK_LIMIT
    ? ("共 " + hits.length + " 件，先画 " + PICK_LIMIT + " 件，搜一下缩小范围")
    : (hits.length + " 件");
}

/** 侧栏：这件东西是什么、**现在在商店里卖不卖**。 */
async function describe(item) {
  var box = $("pickDetail");
  box.textContent = "";
  var line = el("div");
  line.appendChild(el("b", null, item.name));
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

function renderAdmins(names) {
  var rows = $("adminRows");
  rows.textContent = "";
  (names || []).forEach(function (name) {
    var tr = document.createElement("tr");
    tr.appendChild(el("td", null, name));
    var td = el("td");
    var button = el("button", "btn btn-danger btn-sm", "删除");
    button.onclick = function () { removeAdmin(name); };
    td.appendChild(button);
    tr.appendChild(td);
    rows.appendChild(tr);
  });
}

async function loadAdmins() {
  var result = await api("/admin/api/admins");
  if (bounced(result) || !result.ok) { return; }
  renderAdmins(result.names);
}

async function removeAdmin(name) {
  if (!window.confirm("确定删除管理员「" + name + "」？")) { return; }
  var result = await api("/admin/api/admins/remove", {name: name});
  if (result.ok && result.logged_out) {
    showLoggedOut("你把自己删掉了，已退出登录。");
    return;
  }
  say($("adminMsg"), result.message, result.ok);
  if (result.ok) { renderAdmins(result.names); }
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
    管理员刚在「商店目录」里改了上架状态还没保存时，这边跟着一起变，
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
    return;
  }
  PLAYER_LIST = result.players;
  PLAYER_PAGE = {page: result.page, pages: result.pages,
                 total: result.total, size: result.size, q: q};
  renderPlayerRows();
  $("playerCount").textContent = result.total + " 个账号";
  say($("playerMsg"), "");
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
      && !window.confirm("「" + PLAYER.view.username
                         + "」还有没保存的改动，确定丢掉？")) {
    return;
  }
  var result = await api("/admin/api/player?name=" + encodeURIComponent(username));
  if (bounced(result) || !result.ok) {
    say($("playerMsg"), (result && result.message) || "读不到这个账号", false);
    return;
  }
  adoptPlayer(result.player);
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
  var item = BYID[itemId];
  return (item && item.name) || ("#" + itemId);
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
  // 名字长的会被省略号截掉 —— 全名放 title 里，鼠标停一下就看得到。
  box.title = itemLabel(itemId);
  box.appendChild(slotNode(itemId, 26, false, false));

  var col = el("div", "col");
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
  box.title = itemLabel(row.id);
  box.appendChild(slotNode(row.id, 26, true, false));
  var col = el("div", "col");
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

function showLoggedIn(name) {
  $("who").textContent = "已登录：" + name;
  $("logout").classList.remove("hidden");
  $("loginView").classList.add("hidden");
  $("mainView").classList.remove("hidden");
  // 标签行在顶栏那块 sticky 容器里，不跟着 `mainView` 走 —— 自己开关一次。
  $("tabs").classList.remove("hidden");
  boot();
}

function showLoggedOut(message) {
  CAT = null;
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
  loadAdmins();
}

function switchTab(tab) {
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
    showLoggedIn(result.name);
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
    paintPicker();
  };
  $("pickKind").onchange = function () {
    PICKER.kind = $("pickKind").value;
    paintPicker();
  };
  $("pickClose").onclick = closePicker;
  $("picker").onclick = function (event) {
    if (event.target === $("picker")) { closePicker(); }
  };
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && PICKER) { closePicker(); }
  });

  $("addAdmin").onclick = async function () {
    var result = await api("/admin/api/admins/add", {
      name: $("newAdminName").value, password: $("newAdminPass").value});
    say($("adminMsg"), result.message, result.ok);
    if (result.ok) {
      $("newAdminName").value = "";
      $("newAdminPass").value = "";
      renderAdmins(result.names);
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
      if ("name" in entry) { entry.name = item.name; }
      if ("id" in entry && which === "recipe") { entry.id = nextRecipeId(); }
      CFG[which].entries.push(entry);
      // 新加的那条一定要看得见 —— 否则筛选开着的时候「加了没反应」。
      FILTER[which] = {q: "", kind: "", character: "", listedOnly: false};
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
  var session = await api("/admin/api/session");
  if (session.logged_in) { showLoggedIn(session.name); }
  else { showLoggedOut(""); }
}());
