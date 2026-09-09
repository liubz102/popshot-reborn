#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_items.py -- resource audit for every listed item (shop / craft / drop).

For each item that is on the shelves (server/data/shop.json listed), a craft
result (recipe.json listed) or a drop material (drops.json), check the client
side resources the Chinese client will load for it:

  * ShopItem-Chn.ini  Tag=  -> Models/Characters/chNN/....msh must exist
                              (+ the .dds next to it)
  * ShopItem-Chn.ini  Image= -> Images/Shop/*.png must exist
  * the item's character (id prefix, what 0x0501 sends) must equal the chNN
    of its mesh, and every bone name inside the mesh must exist in that
    character's skeleton (bone names live in Models/Characters/chNN/*.mtn)

A ch02 mesh attached to ch00/ch01 has no matching bones -> NULL bone pointer
-> crash in the CPU skinning loop (V0.3 shop FINDINGS 50 / D31a / D56).

Usage:  python tools/audit_items.py [out.json]
Needs Pack_decrypt/ (only present in the main worktree).
"""
import collections, json, os, re, struct, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACK = os.path.join(ROOT, "Pack_decrypt")
DATA = os.path.join(ROOT, "server", "data")


def load_ini():
    raw = open(os.path.join(PACK, "Data", "ShopItem-Chn.ini"), "rb").read()
    txt = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("cp949", "replace")
    out = {}
    for m in re.finditer(r"^\[(Item|Stock)-(\d+)\]\s*\n((?:(?!\[).*\n?)*)", txt, re.M):
        out.setdefault(int(m.group(2)), {})[m.group(1)] = dict(re.findall(r"^(\w+)=(.*?)\s*$", m.group(3), re.M))
    return out


def bones_of(path):
    b = open(path, "rb").read()
    out = []
    for m in re.finditer(rb"B\x00o\x00n\x00e\x00_\x00", b):
        o = m.start()
        ln = struct.unpack_from("<I", b, o - 4)[0]
        out.append(b[o:o + ln * 2].decode("utf-16le", "replace"))
    return out


def skeleton(ch):
    names = set()
    d = os.path.join(PACK, "Models", "Characters", ch)
    for fn in os.listdir(d):
        if not fn.lower().endswith(".mtn"):
            continue
        b = open(os.path.join(d, fn), "rb").read()
        for m in re.finditer(rb"B\x00o\x00n\x00e\x00_\x00(?:[\x20-\x7e]\x00){1,30}", b):
            names.add(m.group().decode("utf-16le"))
    return names


def main():
    if not os.path.isdir(PACK):
        print("Pack_decrypt/ not found (only the main worktree has it)")
        return 2
    ini = load_ini()
    si = json.load(open(os.path.join(ROOT, "server", "shop_items.json"), encoding="utf-8"))["items"]
    si = si if isinstance(si, list) else list(si.values())
    by = {int(i["id"]): i for i in si}
    lib = json.load(open(os.path.join(DATA, "items.json"), encoding="utf-8"))
    lib = lib["items"] if isinstance(lib, dict) and "items" in lib else lib
    lib = {int(i["id"]): i for i in (lib.values() if isinstance(lib, dict) else lib)}
    listed = collections.OrderedDict()
    for it in json.load(open(os.path.join(DATA, "shop.json"), encoding="utf-8"))["items"]:
        if it.get("listed"):
            listed.setdefault(int(it["id"]), set()).add("shop")
    for r in json.load(open(os.path.join(DATA, "recipe.json"), encoding="utf-8"))["recipes"]:
        if r.get("listed"):
            listed.setdefault(int(r["result"]), set()).add("craft")
    for r in json.load(open(os.path.join(DATA, "drops.json"), encoding="utf-8"))["rules"]:
        listed.setdefault(int(r["material"]), set()).add("drop")
    skel = {}
    rows, problems = [], []
    for iid, where in listed.items():
        d = by.get(iid, {})
        sec = ini.get(iid, {}).get("Item", {})
        tag, img = sec.get("Tag", ""), sec.get("Image", "")
        row = dict(id=iid, kind=d.get("kind"), name=lib.get(iid, {}).get("name"),
                   character=lib.get(iid, {}).get("character"), kr=bool(d.get("name_kr")),
                   where=",".join(sorted(where)), tag=tag, bones=[], issues=[])
        m = re.search(r"Characters/(ch(\d\d))/", tag or "")
        if img and not os.path.isfile(os.path.join(PACK, img)):
            row["issues"].append("icon missing: " + img)
        if m:
            ch, n = m.group(1), int(m.group(2))
            msh = os.path.join(PACK, tag)
            if not os.path.isfile(msh):
                row["issues"].append("msh missing: " + tag)
            else:
                if not os.path.isfile(re.sub(r"\.msh$", ".dds", msh, flags=re.I)):
                    row["issues"].append("dds missing")
                row["bones"] = bones_of(msh)
                if ch not in skel:
                    skel[ch] = skeleton(ch)
                missing = [b for b in row["bones"] if b not in skel[ch]]
                if missing:
                    row["issues"].append("bones not in %s skeleton: %s" % (ch, missing))
            if row["character"] != n:
                row["issues"].append("character %r != mesh %s" % (row["character"], ch))
        rows.append(row)
        if row["issues"]:
            problems.append(row)
    print("listed items:", len(rows), dict(collections.Counter(r["kind"] for r in rows)))
    print("character meshes:", sum(1 for r in rows if re.search(r"Characters/ch\d\d/", r["tag"] or "")))
    print("never named in Chinese (untested on this client):", sum(1 for r in rows if r["kr"]))
    print("problems:", len(problems))
    for r in problems:
        print("  %s %s [%s] %s" % (r["id"], r["name"], r["where"], "; ".join(r["issues"])))
    if len(sys.argv) > 1:
        json.dump(rows, open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
