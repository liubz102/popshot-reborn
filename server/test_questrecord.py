#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`server/questrecord.py` —— 任务通关记录的存储层与破纪录判定。

夹具照 `test_web_admin.GiftHistoryStoreTests`：每个用例自己一份临时 `DATA_DIR`。
★ 这是并行跑测试的前提（`run_tests._prepare` 给每个进程指的是同一个空目录，
不隔离的话用例之间会互相污染、随机红）。
"""
import json
import os
import tempfile
import unittest

import questrecord
import shopcfg


class QuestRecordStoreTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = shopcfg.DATA_DIR
        shopcfg.DATA_DIR = self.tmp.name
        self.addCleanup(setattr, shopcfg, "DATA_DIR", saved)
        self.target = os.path.join(self.tmp.name, questrecord.FILENAME)

    def write_raw(self, payload):
        with open(self.target, "w", encoding="utf-8") as fp:
            json.dump(payload, fp)

    def read_raw(self):
        with open(self.target, "r", encoding="utf-8") as fp:
            return json.load(fp)

    # ---- 落脚点 / 不生成默认值 ---------------------------------------------

    def test_the_file_follows_the_data_dir(self):
        """★ 打包自检的 `--data-dir` 靠这一条把它挪出包外（`Assert-PackageDataClean`）。"""
        self.assertEqual(self.target, questrecord.path())
        questrecord.note_clear(3, 1, [("alice", "Alice")], 214)
        self.assertTrue(os.path.exists(self.target))

    def test_a_missing_file_is_simply_no_records(self):
        """★★ 缺文件 = 谁都没打通过。**读一次不许留下任何东西** ——
        它一旦被启动路径生成出来，打包自检就会把它带进包里（D7 / 铁律 11）。"""
        self.assertEqual({}, questrecord.load())
        self.assertEqual(({}, []), questrecord.board(3, 1, ["alice"]))
        self.assertEqual([], os.listdir(self.tmp.name))

    # ---- 写入与覆盖 ---------------------------------------------------------

    def test_only_a_better_time_overwrites(self):
        questrecord.note_clear(3, 1, [("alice", "Alice")], 214, now=100.0)
        questrecord.note_clear(3, 1, [("alice", "Alice")], 250, now=200.0)
        row = self.read_raw()["records"]["3:1"]["alice"]
        self.assertEqual(214, row["seconds"])
        self.assertEqual(100.0, row["at"])        # 连时间戳都不许动
        questrecord.note_clear(3, 1, [("alice", "Alice")], 200, now=300.0)
        row = self.read_raw()["records"]["3:1"]["alice"]
        self.assertEqual(200, row["seconds"])
        self.assertEqual(300.0, row["at"])

    def test_each_player_keeps_exactly_one_row(self):
        """合作局四个人各更各的 —— 全体榜天然去重，不靠写入时的过程性约定。"""
        team = [("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")]
        questrecord.note_clear(3, 1, team, 214)
        questrecord.note_clear(3, 1, team, 200)
        self.assertEqual(4, len(self.read_raw()["records"]["3:1"]))

    def test_each_quest_and_difficulty_is_its_own_bucket(self):
        """★ 用户点名的需求：记录要区分不同的地图、不同的难度。"""
        questrecord.note_clear(3, 1, [("alice", "Alice")], 214)
        self.assertEqual({"alice": 214}, questrecord.board(3, 1, ["alice"])[0])
        # 反向：同图别的难度、同难度别的图，都必须是空的
        self.assertEqual(({}, []), questrecord.board(3, 2, ["alice"]))
        self.assertEqual(({}, []), questrecord.board(2, 1, ["alice"]))

    # ---- 榜单 ---------------------------------------------------------------

    def test_the_top_is_capped_and_sorted_ascending(self):
        for n in range(30):
            questrecord.note_clear(3, 1, [("p%02d" % n, "P%02d" % n)], 300 - n, now=float(n))
        _, top = questrecord.board(3, 1)
        self.assertEqual(questrecord.TOP_N, len(top))
        self.assertEqual([seconds for seconds, _ in top],
                         sorted(seconds for seconds, _ in top))
        self.assertEqual((271, "P29"), top[0])

    def test_a_tie_is_broken_by_who_got_there_first(self):
        questrecord.note_clear(3, 1, [("late", "Late")], 214, now=200.0)
        questrecord.note_clear(3, 1, [("early", "Early")], 214, now=100.0)
        _, top = questrecord.board(3, 1)
        self.assertEqual(["Early", "Late"], [name for _, name in top])
        # 确定性：并行跑测试要求两次读出来逐字节一样
        self.assertEqual(questrecord.board(3, 1), questrecord.board(3, 1))

    def test_the_slowest_falls_off_when_the_bucket_is_full(self):
        saved = questrecord.PLAYERS_MAX
        questrecord.PLAYERS_MAX = 3
        self.addCleanup(setattr, questrecord, "PLAYERS_MAX", saved)
        for n in range(5):
            questrecord.note_clear(3, 1, [("p%d" % n, "P%d" % n)], 300 - n, now=float(n))
        rows = self.read_raw()["records"]["3:1"]
        self.assertEqual({"p2", "p3", "p4"}, set(rows))

    def test_the_board_answers_both_questions_from_one_read(self):
        questrecord.note_clear(3, 1, [("alice", "Alice"), ("bob", "Bob")], 214)
        questrecord.note_clear(3, 1, [("bob", "Bob")], 190)
        mine, top = questrecord.board(3, 1, ["alice", "carol"])
        self.assertEqual({"alice": 214}, mine)          # carol 没成绩就不在里面
        self.assertEqual([(190, "Bob"), (214, "Alice")], top)

    def test_an_empty_board_answers_with_no_rows(self):
        """★ 用户拍板：无记录时照原版回空列表，**不造占位条目**。

        代价是客户端那个下拉框的兜底文本恒为「正在查询资料」（`0x466594` 里
        `ebx` 在 99999 分支没被改写）—— 已知且接受，别「好心」去塞一条。
        """
        self.assertEqual([], questrecord.board(7, 3)[1])

    # ---- 破纪录判定 ---------------------------------------------------------

    def test_the_first_clear_ever_is_a_global_record(self):
        verdicts = questrecord.note_clear(3, 1, [("alice", "Alice")], 214)
        self.assertEqual({"alice": (questrecord.RECORD_GLOBAL, None)}, verdicts)

    def test_everyone_who_cleared_together_gets_the_global_verdict(self):
        """★★ 写前快照只取一次的守门人。

        边判边写的话，第一个人写进去就成了新的全服最好成绩，后三个人一比
        就变成「没破」—— 可四个人明明是一起打出来的。
        """
        team = [("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")]
        verdicts = questrecord.note_clear(3, 1, team, 214)
        self.assertEqual([questrecord.RECORD_GLOBAL] * 4,
                         [kind for kind, _ in verdicts.values()])

    def test_beating_only_your_own_time_is_a_personal_record(self):
        questrecord.note_clear(3, 1, [("fast", "Fast")], 180)
        questrecord.note_clear(3, 1, [("alice", "Alice")], 200)
        verdicts = questrecord.note_clear(3, 1, [("alice", "Alice")], 190)
        self.assertEqual((questrecord.RECORD_PERSONAL, 200), verdicts["alice"])

    def test_a_slower_run_is_no_record_at_all(self):
        questrecord.note_clear(3, 1, [("alice", "Alice")], 200)
        verdicts = questrecord.note_clear(3, 1, [("alice", "Alice")], 250)
        self.assertEqual((questrecord.RECORD_NONE, 200), verdicts["alice"])

    def test_matching_your_own_time_is_not_a_record(self):
        """并列不覆盖 —— 保住更早的那个 `at`，也避免「播报说破了纪录、
        榜上数字纹丝不动」。"""
        questrecord.note_clear(3, 1, [("alice", "Alice")], 214, now=100.0)
        verdicts = questrecord.note_clear(3, 1, [("alice", "Alice")], 214, now=200.0)
        self.assertEqual(questrecord.RECORD_NONE, verdicts["alice"][0])
        self.assertEqual(100.0, self.read_raw()["records"]["3:1"]["alice"]["at"])

    def test_kind_zero_means_nothing_was_written(self):
        """不变量：`类型 0` ⇔ 不写盘。正反两面都验。"""
        questrecord.note_clear(3, 1, [("alice", "Alice")], 200)
        before = self.read_raw()
        verdicts = questrecord.note_clear(3, 1, [("alice", "Alice")], 300)
        self.assertEqual(questrecord.RECORD_NONE, verdicts["alice"][0])
        self.assertEqual(before, self.read_raw())

    def test_a_global_record_always_gets_written(self):
        """不变量：`own_before >= best_before` 恒成立 ⇒ 类型 2 蕴含会写盘。"""
        questrecord.note_clear(3, 1, [("other", "Other")], 200)
        verdicts = questrecord.note_clear(3, 1, [("alice", "Alice")], 150)
        self.assertEqual(questrecord.RECORD_GLOBAL, verdicts["alice"][0])
        self.assertEqual(150, self.read_raw()["records"]["3:1"]["alice"]["seconds"])

    # ---- 哨兵与夹取 ---------------------------------------------------------

    def test_no_record_is_the_sentinel_never_zero(self):
        """★★ `0` 在客户端是「正在查询资料」，`99999` 才是「无历史战绩」。"""
        self.assertEqual(99999, questrecord.NO_RECORD)
        self.assertEqual(1, questrecord.SECONDS_MIN)
        self.assertEqual(99998, questrecord.SECONDS_MAX)

    def test_a_sub_second_clear_is_stored_as_one_not_zero(self):
        questrecord.note_clear(3, 1, [("alice", "Alice")], 0)
        self.assertEqual({"alice": 1}, questrecord.board(3, 1, ["alice"])[0])

    def test_an_absurdly_long_run_never_reaches_the_sentinel(self):
        questrecord.note_clear(3, 1, [("alice", "Alice")], 10 ** 9)
        self.assertEqual({"alice": 99998}, questrecord.board(3, 1, ["alice"])[0])

    # ---- 坏文件 / 脏数据 ----------------------------------------------------

    def test_a_file_that_cannot_be_read_is_set_aside_not_overwritten(self):
        with open(self.target, "w", encoding="utf-8") as fp:
            fp.write("{ 这不是 json")
        said = []
        self.assertEqual({}, questrecord.load(log=said.append))
        spare = [name for name in os.listdir(self.tmp.name)
                 if name.startswith(questrecord.FILENAME + ".bad-")]
        self.assertEqual(1, len(spare), os.listdir(self.tmp.name))
        self.assertTrue(said and "读不了" in said[0], said)
        questrecord.note_clear(3, 1, [("alice", "Alice")], 214)
        self.assertEqual({"alice": 214}, questrecord.board(3, 1, ["alice"])[0])

    def test_garbage_rows_are_dropped_not_crashed(self):
        self.write_raw({"format": 1, "records": {
            "3:1": {
                "ok": {"name": "OK", "seconds": 214, "at": 1.0},
                "text": {"seconds": "214"},
                "negative": {"seconds": -5},
                "zero": {"seconds": 0},              # 0 在客户端是「正在查询资料」
                "sentinel": {"seconds": 99999},      # 撞上哨兵的也不收
                "yes": {"seconds": True},            # bool 是 int 的子类，得挡掉
                "notadict": 214,
            },
            "abc": {"x": {"seconds": 100}},          # 桶键解不动 → 整桶丢
            "3": {"x": {"seconds": 100}},            # 少一半 → 整桶丢
            "9:9": [],                               # 桶不是 dict → 丢
        }})
        buckets = questrecord.load()
        self.assertEqual(["3:1"], list(buckets))
        self.assertEqual(["ok"], list(buckets["3:1"]))

    def test_unknown_fields_survive_a_rewrite(self):
        """★ 铁律 11：这一版不认识的字段，不许被一次改写抹掉。"""
        self.write_raw({"format": 1, "records": {
            "3:1": {"alice": {"name": "Alice", "seconds": 300, "at": 1.0,
                              "future_field": 42}}}})
        questrecord.note_clear(3, 1, [("alice", "Alice")], 200)
        row = self.read_raw()["records"]["3:1"]["alice"]
        self.assertEqual(200, row["seconds"])
        self.assertEqual(42, row["future_field"])

    def test_an_older_file_without_name_or_at_still_reads(self):
        self.write_raw({"format": 1, "records": {"3:1": {"alice": {"seconds": 214}}}})
        mine, top = questrecord.board(3, 1, ["alice"])
        self.assertEqual({"alice": 214}, mine)
        self.assertEqual([(214, "alice")], top)      # 没昵称就退回账号名

    def test_a_handedited_name_is_sanitised_before_it_reaches_the_wire(self):
        """★ 盘上的名字是可以被人手改的。补充平面字符会让 `w_wstr` 的 u16
        长度字段少算，把整个包的后半截错位。"""
        self.write_raw({"format": 1, "records": {"3:1": {"alice": {
            "name": "A\U0001F600B\x01C" + "x" * 40, "seconds": 214, "at": 1.0}}}})
        name = questrecord.board(3, 1)[1][0][1]
        self.assertTrue(len(name) <= questrecord.NAME_MAX_LENGTH)
        self.assertTrue(all(0x20 <= ord(ch) <= 0xffff for ch in name), name)
        self.assertTrue(name.startswith("ABC"), name)

    # ---- 清榜 ---------------------------------------------------------------

    def test_clearing_removes_the_file_itself(self):
        questrecord.note_clear(3, 1, [("alice", "Alice"), ("bob", "Bob")], 214)
        self.assertEqual(2, questrecord.clear())
        self.assertFalse(os.path.exists(self.target))
        self.assertEqual(0, questrecord.clear())


class GitRulesTests(unittest.TestCase):
    """新的 `server/data/*.json` 必须同时进 `.gitignore`（本体 / tmp / bad）。"""

    def test_gitignore_covers_the_new_data_file(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, ".gitignore"), "r", encoding="utf-8") as fp:
            text = fp.read()
        for line in ("/server/data/quest_record.json",
                     "/server/data/quest_record.json.*.tmp",
                     "/server/data/quest_record.json.bad-*"):
            self.assertIn(line, text)


if __name__ == "__main__":
    unittest.main()
