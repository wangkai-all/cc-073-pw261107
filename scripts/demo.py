#!/usr/bin/env python3
"""端到端演示：在临时数据库里完整跑一遍迟到更正场景并打印每一步。

用法: python3 scripts/demo.py
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import core, db, rules  # noqa: E402

TODAY = date.today()


def show_cell(conn, d, metric="gmv", dim="ALL"):
    v = core.current_cell_version(conn, d, metric, dim)
    if not v:
        print(f"  {d} {metric}/{dim}: （无数据）")
        return
    print(f"  {d} {metric}/{dim} = {v['value']}  (v{v['version_no']}, 规则 {v['rule_version']},"
          f" 更正 {v['corrections_applied']})")


def show_run(conn, run_id):
    run = conn.execute("SELECT * FROM recalc_runs WHERE run_id=?", (run_id,)).fetchone()
    print(f"  预览 {run_id}（触发={run['trigger_type']}，规则={run['rule_version']}，"
          f"下游报表={run['affected_reports']}）")
    for it in conn.execute(
            "SELECT * FROM recalc_run_items WHERE run_id=? ORDER BY biz_date, metric, dim",
            (run_id,)).fetchall():
        old = "∅" if it["old_value"] is None else it["old_value"]
        print(f"    {it['biz_date']} {it['metric']:6s} {it['dim']:4s}  {old} -> {it['new_value']}"
              f"   更正: {it['corrections_applied']}")


def main():
    path = os.path.join(tempfile_dir(), "demo.db")
    conn = db.connect(path)
    db.init(conn)
    rules.seed_rules(conn, core.now_str())

    d2 = (TODAY - timedelta(days=2)).isoformat()
    d1 = (TODAY - timedelta(days=1)).isoformat()

    print("== 1. 导入两日业务快照（自动形成基线版本，RULE-V1） ==")
    core.import_snapshot(conn, d2, [
        {"order_id": "A1", "channel": "电商", "amount": 100.0},
        {"order_id": "A2", "channel": "门店", "amount": 200.0},
    ])
    core.import_snapshot(conn, d1, [
        {"order_id": "B1", "channel": "电商", "amount": 300.0,
         "status": "refunded", "refund_amount": 30.0},
    ])
    show_cell(conn, d2)
    key = f"monthly:{d2[:7]}"
    rv1 = core.generate_report_version(conn, key)
    core.publish_report(conn, rv1)
    print(f"  已发布 {key} v1")

    print("\n== 2. 迟到更正 C-9 到达（A1 金额 100 -> 160），先生成预览 ==")
    r = core.import_corrections(conn, [{
        "correction_id": "C-9", "type": "adjust",
        "target_order_id": "A1", "amount": 160.0}])
    show_run(conn, r["run_id"])
    print("  -- 确认前当前值不变：", end="")
    show_cell(conn, d2)
    core.confirm_run(conn, r["run_id"])
    print("  -- 确认后：", end="")
    show_cell(conn, d2)

    print("\n== 3. 同一更正重复到达：判重，不重复叠加 ==")
    r = core.import_corrections(conn, [{
        "correction_id": "C-9", "type": "adjust",
        "target_order_id": "A1", "amount": 160.0}])
    print(f"  结果: {r['results'][0]['result']}，run_id={r['run_id']}")
    show_cell(conn, d2)

    print("\n== 4. 新快照重导入（A1=999, A2=250）：更正必须继续生效，不能被覆盖 ==")
    r = core.import_snapshot(conn, d2, [
        {"order_id": "A1", "channel": "电商", "amount": 999.0},
        {"order_id": "A2", "channel": "门店", "amount": 250.0},
    ])
    print(f"  快照导入: {r['impact']} -> {r.get('run_id')}")
    show_run(conn, r["run_id"])
    core.confirm_run(conn, r["run_id"])
    show_cell(conn, d2)  # ALL=160+250=410：A1 仍按更正 160 计，而非快照的 999

    print("\n== 5. 撤回 C-9：恢复到正确版本（追加新版本，非覆盖） ==")
    w = core.withdraw_correction(conn, "C-9")
    show_run(conn, w["run_id"])
    core.confirm_run(conn, w["run_id"])
    show_cell(conn, d2)
    v = core.current_cell_version(conn, d2, "gmv", "ALL")
    print(f"  restored_from_version_id = {v['restored_from_version_id']}")

    print("\n== 6. 超窗口补录 -> 人工队列 -> 强制回补 ==")
    old = (TODAY - timedelta(days=60)).isoformat()
    r = core.import_corrections(conn, [{
        "correction_id": "C-OLD", "type": "insert", "biz_date": old,
        "order_id": "X1", "channel": "电商", "amount": 500.0}])
    print(f"  结果: {r['results'][0]}")
    q = conn.execute("SELECT * FROM manual_queue WHERE correction_id='C-OLD'").fetchone()
    res = core.resolve_manual(conn, q["id"], "force", note="财务特批")
    core.confirm_run(conn, res["run_id"])
    show_cell(conn, old)

    print("\n== 7. 发布门禁：未确认重算期间报表不得发布 ==")
    r = core.import_corrections(conn, [{
        "correction_id": "C-10", "type": "void", "target_order_id": "B1"}])
    draft = conn.execute(
        "SELECT id FROM report_versions WHERE report_key=? ORDER BY version_no DESC LIMIT 1",
        (key,)).fetchone()
    if draft:
        print(f"  门禁拦截原因: {core.publish_blockers(conn, draft['id'])}")
    core.confirm_run(conn, r["run_id"])
    draft = conn.execute(
        "SELECT id FROM report_versions WHERE report_key=? AND status='draft'"
        " ORDER BY version_no DESC LIMIT 1", (key,)).fetchone()
    print(f"  确认后门禁: {core.publish_blockers(conn, draft['id']) or '通过'}")
    core.publish_report(conn, draft["id"])

    print("\n== 8. 版本对比与溯源 ==")
    latest = conn.execute(
        "SELECT id FROM report_versions WHERE report_key=? ORDER BY version_no DESC LIMIT 1",
        (key,)).fetchone()["id"]
    cmp_ = core.compare_versions(conn, rv1, latest)
    for row in cmp_["rows"]:
        if row["changed"]:
            print(f"  {row['metric']}/{row['dim']}: {row['a']} -> {row['b']} (Δ{row['delta']})")
    trace = core.trace_item(conn, latest, "gmv", "ALL")
    for c in trace["cells"]:
        corr = ",".join(x["correction_id"] for x in c["corrections"]) or "-"
        print(f"  血缘: {c['biz_date']} v{c['version_no']} 值={c['value']} 规则={c['rule_version']}"
              f" 更正={corr} 源记录={len(c['records'])}条")
    print("\n演示完成。")


def tempfile_dir():
    import tempfile
    return tempfile.mkdtemp(prefix="workbench-demo-")


if __name__ == "__main__":
    main()
