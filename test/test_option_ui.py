#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设定界面 `Option2.ui`（X22 / §150 / D108）：「全屏」拆成「全屏(保持比例)」「全屏(拉伸)」。

原版「窗口模式设定」一组三个单选按钮，X22 在全屏下面插了 `FullScreenStretchBtn`，
它下面的两个窗口模式按钮、「其它设定」整节、灰底、对话框都往下挪了 30。
bshook 按名字找它（`FSO_STRETCH_BTN`），原版代码按名字找另外三个 —— 名字一个字都不能错。

★ 文件是原版格式：UTF-16LE + BOM + CRLF。改它用脚本按字节写，别用会转成 UTF-8 的编辑器。
"""
import os
import re
import unittest
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
UI = os.path.join(ROOT, "game_patched", "Pack_develop", "Data", "Ui", "Option2.ui")
BSHOOK = os.path.join(ROOT, "hook", "bshook.c")

# 原版初始化 0x41de2f 按名字取的那几个 + 它按 Title%dLb / Label%d 循环取的
ORIGINAL_NAMES = (
    "MuteBgmBtn", "MuteEffBtn", "WindowedBtn", "WindowedTo800600Btn", "FullScreenBtn",
    "ReqFriendBtn", "AcceptWhisperBtn", "DoNotUseKillEffectBtn",
    "DoNotUseRecognizeEnemyEffectBtn", "DoNotUseRecognizeMySelfEffectBtn",
    "Title0Lb", "Title1Lb", "Title2Lb", "Title3Lb", "Label0", "Label1", "Label2",
)
MODE_BUTTONS = ("FullScreenBtn", "FullScreenStretchBtn", "WindowedBtn", "WindowedTo800600Btn")


def load():
    if not os.path.isfile(UI):
        raise unittest.SkipTest("不在源码仓库里（缺 %s），跳过" % UI)
    with open(UI, "rb") as f:
        raw = f.read()
    return raw


def controls(raw):
    root = ET.fromstring(raw[2:].decode("utf-16-le").replace('encoding="UTF-16"', ""))
    props = root.find("Properties")
    dialog = dict((k, int(props.find(k).text)) for k in ("PosX", "PosY", "Width", "Height"))
    out = {}
    for child in root.find("Children"):
        p = child.find("Properties")
        out[child.get("name")] = {
            "tag": child.tag,
            "x": int(p.find("PosX").text), "y": int(p.find("PosY").text),
            "w": int(p.find("Width").text), "h": int(p.find("Height").text),
            "text": p.find("Text").text or "",
        }
    return dialog, out


class OptionUiTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.raw = load()
        cls.dialog, cls.ctl = controls(cls.raw)

    def test_original_encoding_is_kept(self):
        self.assertEqual(b"\xff\xfe", self.raw[:2], "UTF-16LE 的 BOM 没了")
        text = self.raw[2:].decode("utf-16-le")
        self.assertIn("\r\n", text)
        self.assertNotIn("\n", text.replace("\r\n", ""), "混进了光 LF 的行")

    def test_every_name_the_game_looks_up_is_still_there(self):
        for name in ORIGINAL_NAMES:
            self.assertIn(name, self.ctl, name)

    def test_the_stretch_button_is_the_one_bshook_looks_for(self):
        with open(BSHOOK, encoding="utf-8") as f:
            src = f.read()
        m = re.search(r'#define FSO_STRETCH_BTN\s+L"([^"]+)"', src)
        self.assertTrue(m, "bshook.c 里找不到 FSO_STRETCH_BTN")
        self.assertEqual("FullScreenStretchBtn", m.group(1))
        self.assertEqual("UiButton", self.ctl["FullScreenStretchBtn"]["tag"])

    def test_four_mode_buttons_in_order_with_the_new_texts(self):
        ys = [self.ctl[n]["y"] for n in MODE_BUTTONS]
        self.assertEqual(sorted(ys), ys, "四个模式按钮不是从上到下排的")
        for a, b in zip(MODE_BUTTONS, MODE_BUTTONS[1:]):
            self.assertGreaterEqual(self.ctl[b]["y"], self.ctl[a]["y"] + self.ctl[a]["h"], "%s / %s 重叠" % (a, b))
        self.assertEqual("全屏(保持比例)", self.ctl["FullScreenBtn"]["text"])
        self.assertEqual("全屏(拉伸)", self.ctl["FullScreenStretchBtn"]["text"])
        full, new = self.ctl["FullScreenBtn"], self.ctl["FullScreenStretchBtn"]
        self.assertEqual((full["x"], full["w"], full["h"]), (new["x"], new["w"], new["h"]), "新按钮尺寸要和全屏按钮一样")

    def test_buttons_do_not_overlap_and_stay_inside_the_dialog(self):
        buttons = [(n, c) for n, c in self.ctl.items() if c["tag"] == "UiButton"]
        for name, c in self.ctl.items():
            self.assertGreaterEqual(c["x"], 0, name)
            self.assertGreaterEqual(c["y"], 0, name)
            self.assertLessEqual(c["x"] + c["w"], self.dialog["Width"], name)
            self.assertLessEqual(c["y"] + c["h"], self.dialog["Height"], name)
        for i, (a, ca) in enumerate(buttons):
            for b, cb in buttons[:i]:
                apart = (ca["x"] + ca["w"] <= cb["x"] or cb["x"] + cb["w"] <= ca["x"]
                         or ca["y"] + ca["h"] <= cb["y"] or cb["y"] + cb["h"] <= ca["y"])
                self.assertTrue(apart, "%s 和 %s 重叠" % (a, b))

    def test_window_mode_section_holds_all_four_and_the_rest_moved_down(self):
        grey = self.ctl["GreyBack2"]
        for n in MODE_BUTTONS + ("ReqFriendBtn", "DoNotUseRecognizeMySelfEffectBtn"):
            c = self.ctl[n]
            self.assertTrue(grey["y"] <= c["y"] and c["y"] + c["h"] <= grey["y"] + grey["h"], n + " 出了灰底")
        last_mode = self.ctl["WindowedTo800600Btn"]
        self.assertGreater(self.ctl["Title3Lb"]["y"], last_mode["y"] + last_mode["h"] - 1,
                           "「其它设定」标题压到了窗口模式按钮上")
        self.assertEqual(self.ctl["ImgBar4"]["y"], self.ctl["Title3Lb"]["y"])
        # 对话框还在 1024×768 的界面里
        self.assertLessEqual(self.dialog["PosY"] + self.dialog["Height"], 768)


if __name__ == "__main__":
    unittest.main()
