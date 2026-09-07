#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_listing.py —— 把 `server/shopdefaults.py` 的设计表算一遍：试算 / 自洽检查 / 写回 `server/data/`。

设计口径全在 `shopdefaults.py` 的文件头（D44 / D49 / D50），这里只是命令行壳：

    python tools/gen_listing.py               试算 + 自洽检查，不写文件
    python tools/gen_listing.py --apply       写 recipe.json / drops.json（写前留 `.bak-<时刻>`）；
                                              items.json / shop.json 和磁盘一样就不动，
                                              不一样就跳过并提示（可能被管理页改过）
    python tools/gen_listing.py --apply --all 四份全写

★ 改数值改 `shopdefaults.py` 的表再跑这里，别手改四份 json 再回头对不上 —— 模板
就是 `shopdefaults`，第一次开服生成的和这里写出来的是同一份。
"""
import collections
import json
import os
import shutil
import sys
import time

SERVER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server")
sys.path.insert(0, SERVER)
import shopcfg          # noqa: E402
import shopdata         # noqa: E402
import shopdefaults     # noqa: E402
import shop             # noqa: E402

DATA = os.path.join(SERVER, "data")


def load(name):
    with open(os.path.join(DATA, name), "r", encoding="utf-8") as fp:
        return json.load(fp)


def main(argv):
    apply = "--apply" in argv
    write_all = "--all" in argv
    built = shopdefaults.build_all()
    report = built["report"]
    zh = shopdefaults.zh

    problems = shopdefaults.problems(built)
    recipes_ok = shopcfg.validate_recipes(built["recipes"])
    drops_ok = shopcfg.validate_drops(built["drops"])
    shop_ok = shopcfg.validate_shop(built["shop"])
    for recipe in recipes_ok:
        cat = shop.category_of(recipe["result"])
        if cat not in shop.COMPOSITION_CATEGORIES:
            problems.append("配方 #%d 的产物 %s 归在 %#x，合成面板点不到"
                            % (recipe["id"], zh(recipe["result"]), cat))

    # ---- 汇总 ----
    listed_shop = [e for e in shop_ok.values() if e["listed"]]
    print("上架统计：商店 %d 件、合成 %d 条、掉落规则 %d 条、物品库 %d 件"
          % (len(listed_shop), len(recipes_ok), len(drops_ok), len(built["items"]["items"])))
    by_kind = collections.Counter((e["kind"], "商店") for e in listed_shop)
    for recipe in recipes_ok:
        by_kind[(shopdata.kind(recipe["result"]), "合成")] += 1
    for (kind, w_), n in sorted(by_kind.items()):
        print("  %-11s %-4s %d" % (kind, w_, n))
    print("跳过（不上架）：", dict(report["skipped"]))
    print("去重去掉的 %d 件：%s" % (len(report["duplicates"]), report["duplicates"]))
    print("掉落表（每关每档，全部 100%）：")
    for stage in sorted(shopdefaults.SPECIAL):
        for diff in shopdefaults.DIFFICULTIES:
            mats = [r["material"] for r in drops_ok
                    if r.get("stage") == stage and r.get("difficulty") == diff]
            print("  第 %d 关 %-7s %s  %s" % (stage, shopdefaults.SPECIAL[stage][2],
                                            shopcfg.DIFFICULTY_ZH[diff], " + ".join(zh(m) for m in mats)))
    used = collections.Counter()
    for recipe in recipes_ok:
        for m in recipe["materials"]:
            used[m["id"]] += 1
    print("每种材料被几条配方用到：")
    for mid, n in sorted(used.items()):
        probs = ["%s%s%s%d%%" % (r["mode"], "/s%d" % r["stage"] if r.get("stage") else "",
                                 "/d%d" % r["difficulty"] if r.get("difficulty") else "", r["prob"])
                 for r in drops_ok if r["material"] == mid]
        print("  %-8d %-12s 用于 %2d 条配方  掉落：%s" % (mid, zh(mid), n, " ".join(probs)))
    droppable = {r["material"] for r in drops_ok}
    idle = [zh(m) for m in sorted(droppable) if m not in used]
    if idle:
        print("掉了但没有配方用的材料：", "、".join(idle))
    multi = []
    for recipe in recipes_ok:
        common = None
        for m in recipe["materials"]:
            mine = shopdefaults.stages_of(drops_ok, m["id"])
            common = mine if common is None else common & mine
        if not common:
            multi.append(zh(recipe["result"]))
    print("材料要跑不止一关的配方（%d 条）：%s" % (len(multi), "、".join(multi) or "无"))
    prices = sorted(e["price"] for e in shop_ok.values())
    if prices:
        print("商店价格区间：%d ~ %d，中位 %d" % (prices[0], prices[-1], prices[len(prices) // 2]))
    print("等级分布（上架/合成的）：", sorted(collections.Counter(report["levels"].values()).items()))
    if problems:
        print("\n★ 自洽检查有问题：")
        for p in problems:
            print("  -", p)
        return 1
    print("自洽检查全部通过。")

    if not apply:
        print("\n（试算模式，没有写文件。加 --apply 才写。）")
        return 0
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for name, doc in (("items.json", built["items"]), ("shop.json", built["shop"]),
                      ("recipe.json", built["recipes"]), ("drops.json", built["drops"])):
        path = os.path.join(DATA, name)
        if os.path.exists(path):
            # ★ 和磁盘一样就不用重写（省一份 .bak）；items / shop 不一样时默认不碰
            #   —— 那两份可能被管理页改过（价格 / 名字 / 等级），加 --all 才覆盖。
            same = json.dumps(load(name), sort_keys=True) == json.dumps(doc, sort_keys=True)
            if same:
                print("不变", path)
                continue
            if name in ("items.json", "shop.json") and not write_all:
                print("跳过", path, "—— 磁盘上那份和生成的不一样（可能管理页改过），加 --all 才覆盖")
                continue
        if os.path.exists(path):
            shutil.copyfile(path, "%s.bak-%s" % (path, stamp))
        shopcfg.write_json(path, doc)
        print("已写", path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
