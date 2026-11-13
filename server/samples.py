"""演示样例：每日快照 + 跨期更正 + 撤回 + 超窗口 + 重复投递。

场景（T = 今天）：
1. 导入上月末 3 天 + 最近 10 天的每日业务快照（自动形成基线指标版本，RULE-V1）。
2. 生成并发布上月/本月月报与本周周报。
3. 规则升级到 RULE-V2（净成交口径）。
4. C-3001 调整 D-4 订单金额 -> 预览 -> 确认（预览里能看到规则版本 V1->V2）。
5. C-3001 原样重复投递 -> 判重忽略，不重复叠加。
6. C-3002 作废上月末订单（跨期更正）-> 预览 -> 确认 -> 上月月报出新版并发布。
7. 撤回 C-3002 -> 恢复重算 -> 确认 -> 数值回到撤回前版本（restored_from 链接）。
8. C-3003 补录 D-60 的漏单 -> 超出回补窗口 -> 人工队列（留待页面处理）。
9. C-3004 调整 D-2 订单 -> 生成预览运行，**故意不确认**，供页面走确认流程。
10. C-3001 同 ID 不同内容再次投递 -> 冲突 -> 人工队列。
"""
from datetime import date, timedelta

from . import core, rules


def _orders_for(d: date):
    """确定性生成某日订单：电商 3 单 + 门店 2 单。"""
    recs = []
    for i in range(3):
        amount = 100 + (d.day * 53 + i * 77 + d.month * 31) % 400
        recs.append({
            "order_id": f"O{d:%m%d}-E{i + 1}", "channel": "电商",
            "amount": float(amount), "qty": 1 + (i % 3),
        })
    for i in range(2):
        amount = 80 + (d.day * 41 + i * 59 + d.month * 17) % 300
        recs.append({
            "order_id": f"O{d:%m%d}-S{i + 1}", "channel": "门店",
            "amount": float(amount), "qty": 1 + (i % 2),
        })
    return recs


def seed_demo(conn):
    today_ = core.today()
    prev_month_end = today_.replace(day=1) - timedelta(days=1)
    dates = sorted({
        prev_month_end - timedelta(days=k) for k in range(3)
    } | {today_ - timedelta(days=k) for k in range(1, 11)})

    # 1. 快照基线
    for d in dates:
        recs = _orders_for(d)
        if d == today_ - timedelta(days=5):  # 一单退款，体现 V1/V2 口径差异
            recs[0]["status"] = "refunded"
            recs[0]["refund_amount"] = round(recs[0]["amount"] * 0.3, 2)
        core.import_snapshot(conn, d.isoformat(), recs)

    # 2. 报表初版并发布
    for info in core.generate_all_reports(conn):
        core.publish_report(conn, info["version_id"])

    # 3. 规则升级
    rules.set_current_rule(conn, "RULE-V2")
    core.audit(conn, "set_rule", "当前规则版本切换为 RULE-V2（净成交口径）")

    # 4. C-3001 调整金额 -> 确认
    d4 = (today_ - timedelta(days=4)).isoformat()
    r = core.import_corrections(conn, [{
        "correction_id": "C-3001", "type": "adjust",
        "target_order_id": f"O{d4[5:7]}{d4[8:10]}-E2",
        "amount": 888.0, "reason": "客户补差价，金额更正",
    }])
    if r["run_id"]:
        core.confirm_run(conn, r["run_id"])

    # 5. C-3001 重复投递（同 payload）-> 判重
    core.import_corrections(conn, [{
        "correction_id": "C-3001", "type": "adjust",
        "target_order_id": f"O{d4[5:7]}{d4[8:10]}-E2",
        "amount": 888.0, "reason": "客户补差价，金额更正",
    }])

    # 6. C-3002 跨期作废上月末订单 -> 确认 -> 发布上月月报新版
    pme = prev_month_end.isoformat()
    r = core.import_corrections(conn, [{
        "correction_id": "C-3002", "type": "void",
        "target_order_id": f"O{pme[5:7]}{pme[8:10]}-S1",
        "reason": "跨期退货，作废订单",
    }])
    if r["run_id"]:
        core.confirm_run(conn, r["run_id"])
    key = f"monthly:{pme[:7]}"
    rv = conn.execute(
        "SELECT id FROM report_versions WHERE report_key=? AND status='draft'"
        " ORDER BY version_no DESC LIMIT 1", (key,)).fetchone()
    if rv:
        core.publish_report(conn, rv["id"])

    # 7. 撤回 C-3002 -> 恢复重算 -> 确认 -> 发布恢复后的月报
    w = core.withdraw_correction(conn, "C-3002")
    if w["run_id"]:
        core.confirm_run(conn, w["run_id"])
    rv = conn.execute(
        "SELECT id FROM report_versions WHERE report_key=? AND status='draft'"
        " ORDER BY version_no DESC LIMIT 1", (key,)).fetchone()
    if rv:
        core.publish_report(conn, rv["id"])

    # 8. C-3003 超窗口补录 -> 人工队列（不处理，留给页面）
    old = (today_ - timedelta(days=60)).isoformat()
    core.import_corrections(conn, [{
        "correction_id": "C-3003", "type": "insert",
        "biz_date": old, "order_id": f"O{old[5:7]}{old[8:10]}-E9",
        "channel": "电商", "amount": 666.0, "qty": 2,
        "reason": "两个月前的漏单补录",
    }])

    # 9. C-3004 调整 D-2 -> 只生成预览，留给页面确认
    d2 = (today_ - timedelta(days=2)).isoformat()
    core.import_corrections(conn, [{
        "correction_id": "C-3004", "type": "adjust",
        "target_order_id": f"O{d2[5:7]}{d2[8:10]}-E1",
        "amount": 555.0, "reason": "价格录入错误",
    }])

    # 10. C-3001 同 ID 不同 payload -> 冲突进人工队列
    core.import_corrections(conn, [{
        "correction_id": "C-3001", "type": "adjust",
        "target_order_id": f"O{d4[5:7]}{d4[8:10]}-E2",
        "amount": 999.0, "reason": "冲突示例：同 ID 不同内容",
    }])

    core.audit(conn, "seed_demo", "演示数据加载完成")
    return {"days": len(dates), "range": [dates[0].isoformat(), dates[-1].isoformat()]}
