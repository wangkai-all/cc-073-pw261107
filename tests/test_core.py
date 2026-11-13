"""核心域逻辑测试：幂等、撤回恢复、窗口、发布门禁、快照重导不覆盖更正、血缘。"""
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import core, db, rules, samples  # noqa: E402

TODAY = date.today()
D1 = (TODAY - timedelta(days=2)).isoformat()
D2 = (TODAY - timedelta(days=1)).isoformat()


def fresh_conn():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = db.connect(path)
    db.init(conn)
    rules.seed_rules(conn, core.now_str())
    return conn


def seed_base(conn):
    core.import_snapshot(conn, D1, [
        {"order_id": "A1", "channel": "电商", "amount": 100.0},
        {"order_id": "A2", "channel": "门店", "amount": 200.0},
    ])
    core.import_snapshot(conn, D2, [
        {"order_id": "B1", "channel": "电商", "amount": 300.0,
         "status": "refunded", "refund_amount": 30.0},
        {"order_id": "B2", "channel": "门店", "amount": 50.0},
    ])


def cell(conn, d, metric, dim):
    v = core.current_cell_version(conn, d, metric, dim)
    return v["value"] if v else None


class TestBaseline(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_conn()
        seed_base(self.conn)

    def test_baseline_values_v1(self):
        self.assertEqual(cell(self.conn, D1, "gmv", "ALL"), 300.0)
        self.assertEqual(cell(self.conn, D1, "gmv", "电商"), 100.0)
        self.assertEqual(cell(self.conn, D1, "orders", "ALL"), 2)
        # V1 不扣退款
        self.assertEqual(cell(self.conn, D2, "gmv", "ALL"), 350.0)

    def test_rule_v2_nets_refund(self):
        rules.set_current_rule(self.conn, "RULE-V2")
        value, _, _ = core.compute_cell(self.conn, D2, "gmv", "ALL", "RULE-V2")
        self.assertEqual(value, 320.0)


class TestCorrectionFlow(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_conn()
        seed_base(self.conn)

    def _adjust_a1(self):
        return core.import_corrections(self.conn, [{
            "correction_id": "C-1", "type": "adjust",
            "target_order_id": "A1", "amount": 150.0,
        }], today_override=TODAY)

    def test_preview_then_confirm(self):
        r = self._adjust_a1()
        self.assertIsNotNone(r["run_id"])
        # 预览阶段：当前值不变
        self.assertEqual(cell(self.conn, D1, "gmv", "电商"), 100.0)
        items = self.conn.execute(
            "SELECT * FROM recalc_run_items WHERE run_id=?", (r["run_id"],)).fetchall()
        changed = {(i["metric"], i["dim"]): (i["old_value"], i["new_value"]) for i in items}
        self.assertEqual(changed[("gmv", "电商")], (100.0, 150.0))
        self.assertEqual(changed[("gmv", "ALL")], (300.0, 350.0))
        self.assertNotIn(("orders", "电商"), changed)  # 无变化的单元格不进预览
        core.confirm_run(self.conn, r["run_id"])
        self.assertEqual(cell(self.conn, D1, "gmv", "电商"), 150.0)
        self.assertEqual(cell(self.conn, D1, "gmv", "ALL"), 350.0)
        # 旧版本保留，未被覆盖
        versions = self.conn.execute(
            "SELECT version_no, value FROM cell_versions WHERE cell_key=? ORDER BY version_no",
            (f"{D1}|gmv|电商",)).fetchall()
        self.assertEqual([(v["version_no"], v["value"]) for v in versions],
                         [(1, 100.0), (2, 150.0)])

    def test_duplicate_not_double_applied(self):
        r1 = self._adjust_a1()
        core.confirm_run(self.conn, r1["run_id"])
        payload = {"correction_id": "C-1", "type": "adjust",
                   "target_order_id": "A1", "amount": 150.0}
        r2 = core.import_corrections(self.conn, [payload], today_override=TODAY)
        self.assertEqual(r2["results"][0]["result"], "duplicate_ignored")
        self.assertIsNone(r2["run_id"])
        self.assertEqual(cell(self.conn, D1, "gmv", "电商"), 150.0)  # 没有叠加成 200
        c = self.conn.execute(
            "SELECT arrival_count FROM corrections WHERE correction_id='C-1'").fetchone()
        self.assertEqual(c["arrival_count"], 2)

    def test_conflicting_payload_goes_manual(self):
        r1 = self._adjust_a1()
        core.confirm_run(self.conn, r1["run_id"])
        r2 = core.import_corrections(self.conn, [{
            "correction_id": "C-1", "type": "adjust",
            "target_order_id": "A1", "amount": 999.0,
        }], today_override=TODAY)
        self.assertEqual(r2["results"][0]["reason"], "payload_conflict")
        q = self.conn.execute(
            "SELECT * FROM manual_queue WHERE correction_id='C-1'").fetchone()
        self.assertEqual(q["reason"], "payload_conflict")
        self.assertEqual(cell(self.conn, D1, "gmv", "电商"), 150.0)

    def test_withdraw_restores_correct_version(self):
        r1 = self._adjust_a1()
        core.confirm_run(self.conn, r1["run_id"])
        w = core.withdraw_correction(self.conn, "C-1")
        self.assertIsNotNone(w["run_id"])
        # 撤回预览里应显示回到 100
        items = self.conn.execute(
            "SELECT * FROM recalc_run_items WHERE run_id=? AND metric='gmv' AND dim='电商'",
            (w["run_id"],)).fetchall()
        self.assertEqual(items[0]["new_value"], 100.0)
        core.confirm_run(self.conn, w["run_id"])
        self.assertEqual(cell(self.conn, D1, "gmv", "电商"), 100.0)
        # 是追加的新版本且带 restored_from 链接，而不是覆盖
        versions = self.conn.execute(
            "SELECT * FROM cell_versions WHERE cell_key=? ORDER BY version_no",
            (f"{D1}|gmv|电商",)).fetchall()
        self.assertEqual([v["version_no"] for v in versions], [1, 2, 3])
        self.assertEqual(versions[2]["restored_from_version_id"], versions[0]["id"])
        self.assertEqual(versions[1]["value"], 150.0)  # 历史版本原样保留

    def test_snapshot_reimport_keeps_correction(self):
        """新快照到达不能简单覆盖：已生效更正必须继续叠加在新快照之上。"""
        r1 = self._adjust_a1()
        core.confirm_run(self.conn, r1["run_id"])
        r = core.import_snapshot(self.conn, D1, [
            {"order_id": "A1", "channel": "电商", "amount": 120.0},  # 快照新值 120
            {"order_id": "A2", "channel": "门店", "amount": 200.0},
            {"order_id": "A3", "channel": "电商", "amount": 10.0},
        ])
        self.assertEqual(r["impact"], "preview")
        core.confirm_run(self.conn, r["run_id"])
        # A1 仍按更正后 150 计（不是快照的 120），A3 新增
        self.assertEqual(cell(self.conn, D1, "gmv", "电商"), 160.0)
        self.assertEqual(cell(self.conn, D1, "gmv", "ALL"), 360.0)


class TestWindowAndManual(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_conn()
        seed_base(self.conn)

    def test_out_of_window_goes_manual_then_forced(self):
        old = (TODAY - timedelta(days=60)).isoformat()
        r = core.import_corrections(self.conn, [{
            "correction_id": "C-OLD", "type": "insert", "biz_date": old,
            "order_id": "X1", "channel": "电商", "amount": 500.0,
        }], today_override=TODAY)
        self.assertEqual(r["results"][0]["result"], "manual_queue")
        self.assertIn("out_of_window", r["results"][0]["reason"])
        self.assertIsNone(cell(self.conn, old, "gmv", "ALL"))
        q = self.conn.execute(
            "SELECT * FROM manual_queue WHERE correction_id='C-OLD'").fetchone()
        res = core.resolve_manual(self.conn, q["id"], "force")
        self.assertIsNotNone(res["run_id"])
        core.confirm_run(self.conn, res["run_id"])
        self.assertEqual(cell(self.conn, old, "gmv", "ALL"), 500.0)

    def test_late_insert_affects_monthly_report(self):
        r = core.import_corrections(self.conn, [{
            "correction_id": "C-INS", "type": "insert", "biz_date": D1,
            "order_id": "A9", "channel": "电商", "amount": 40.0,
        }], today_override=TODAY)
        core.confirm_run(self.conn, r["run_id"])
        self.assertEqual(cell(self.conn, D1, "gmv", "电商"), 140.0)
        self.assertEqual(cell(self.conn, D1, "orders", "ALL"), 3)
        key = f"monthly:{D1[:7]}"
        rvid = core.generate_report_version(self.conn, key)
        item = self.conn.execute(
            "SELECT value FROM report_items WHERE report_version_id=? AND metric='gmv' AND dim='电商'",
            (rvid,)).fetchone()
        self.assertEqual(item["value"], 440.0)  # 100+40 (D1) + 300 (D2)


class TestPublishGate(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_conn()
        seed_base(self.conn)
        self.key = f"monthly:{D1[:7]}"
        self.rv1 = core.generate_report_version(self.conn, self.key)
        core.publish_report(self.conn, self.rv1)

    def test_unfinished_recalc_blocks_publish(self):
        r = core.import_corrections(self.conn, [{
            "correction_id": "C-2", "type": "adjust",
            "target_order_id": "A1", "amount": 150.0,
        }], today_override=TODAY)
        # 预览未确认时：旧发布版本被标记过期，且任何发布都被门禁拦下
        stale = self.conn.execute(
            "SELECT stale FROM report_versions WHERE id=?", (self.rv1,)).fetchone()
        self.assertEqual(stale["stale"], 1)
        blockers = core.publish_blockers(self.conn, self.rv1)
        self.assertTrue(any("尚未确认" in b or "仅草稿" in b for b in blockers))
        # 确认重算 -> 自动出新草稿 -> 草稿可发布
        core.confirm_run(self.conn, r["run_id"])
        draft = self.conn.execute(
            "SELECT id FROM report_versions WHERE report_key=? AND status='draft'",
            (self.key,)).fetchone()
        self.assertIsNotNone(draft)
        self.assertEqual(core.publish_blockers(self.conn, draft["id"]), [])
        res = core.publish_report(self.conn, draft["id"])
        self.assertTrue(res["ok"])
        old = self.conn.execute(
            "SELECT status FROM report_versions WHERE id=?", (self.rv1,)).fetchone()
        self.assertEqual(old["status"], "superseded")


class TestCompareTrace(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_conn()
        seed_base(self.conn)
        self.key = f"monthly:{D1[:7]}"

    def test_compare_and_trace(self):
        rv1 = core.generate_report_version(self.conn, self.key)
        core.publish_report(self.conn, rv1)
        r = core.import_corrections(self.conn, [{
            "correction_id": "C-3", "type": "void", "target_order_id": "A2",
        }], today_override=TODAY)
        core.confirm_run(self.conn, r["run_id"])
        rv2 = self.conn.execute(
            "SELECT id FROM report_versions WHERE report_key=? AND status='draft'",
            (self.key,)).fetchone()["id"]
        cmp_ = core.compare_versions(self.conn, rv1, rv2)
        row = [x for x in cmp_["rows"] if x["metric"] == "gmv" and x["dim"] == "门店"][0]
        # 月报含 D1(门店 A2=200，被作废) 与 D2(门店 B2=50)
        self.assertEqual((row["a"], row["b"], row["delta"]), (250.0, 50.0, -200.0))
        trace = core.trace_item(self.conn, rv2, "gmv", "门店")
        d1_cell = [c for c in trace["cells"] if c["biz_date"] == D1][0]
        self.assertEqual(d1_cell["corrections"][0]["correction_id"], "C-3")
        order_ids = {rec["order_id"] for rec in d1_cell["records"]}
        self.assertIn("A2", order_ids)  # 能追到源记录（含被作废的单）


class TestDemoSeed(unittest.TestCase):
    def test_seed_scenario_end_to_end(self):
        conn = fresh_conn()
        info = samples.seed_demo(conn)
        self.assertGreaterEqual(info["days"], 10)
        # 留有 1 个未确认预览（C-3004）供页面操作
        previews = conn.execute(
            "SELECT COUNT(*) c FROM recalc_runs WHERE status='preview'").fetchone()["c"]
        self.assertEqual(previews, 1)
        # 人工队列：超窗口补录 + 同 ID 冲突
        manual = conn.execute(
            "SELECT COUNT(*) c FROM manual_queue WHERE status='pending'").fetchone()["c"]
        self.assertEqual(manual, 2)
        # C-3001 到达 3 次（首次 + 重复 + 冲突），只生效一次
        c = conn.execute(
            "SELECT arrival_count, status FROM corrections WHERE correction_id='C-3001'"
        ).fetchone()
        self.assertEqual((c["arrival_count"], c["status"]), (3, "active"))
        # 撤回恢复产生了 restored_from 链接
        restored = conn.execute(
            "SELECT COUNT(*) c FROM cell_versions WHERE restored_from_version_id IS NOT NULL"
        ).fetchone()["c"]
        self.assertGreater(restored, 0)
        # C-3002 撤回后，上月末门店订单金额恢复
        w = conn.execute(
            "SELECT status FROM corrections WHERE correction_id='C-3002'").fetchone()
        self.assertEqual(w["status"], "withdrawn")


if __name__ == "__main__":
    unittest.main()
