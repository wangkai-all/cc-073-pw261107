"""核心域逻辑：快照导入、更正接收（幂等）、影响分析、重算预览/确认、
撤回恢复、下游报表生成与发布门禁、版本比较与血缘追溯。

关键不变量：
1. cell_versions / report_versions 只增不改，"当前值"= 每个 cell_key 最大 version_no。
2. 更正是快照之上的叠加层；撤回 = 重算时排除该更正并追加新版本，
   若新值与某个历史版本一致，用 restored_from_version_id 显式链接，而不是覆盖文件。
3. 同一 correction_id 重复到达：payload 相同 -> 记 duplicate，不重复叠加；
   payload 不同 -> 记 conflict，进人工队列。
4. 超出回补窗口的更正进人工队列，由人决定强制回补或关闭。
5. 报表版本只有在没有覆盖其区间的未确认重算时才能发布。
"""
import hashlib
import json
from datetime import date, datetime, timedelta

from . import rules

BACKFILL_WINDOW_DAYS = 45  # 可回补窗口：更正业务日期距今超过该天数则进人工队列


# ---------------------------------------------------------------- 基础助手

def now_str():
    return datetime.now().isoformat(timespec="seconds")


def today():
    return date.today()


def audit(conn, action, detail="", actor="system"):
    conn.execute(
        "INSERT INTO audit_log(at, actor, action, detail) VALUES (?,?,?,?)",
        (now_str(), actor, action, detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)),
    )
    conn.commit()


def _payload_hash(payload):
    return hashlib.sha1(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _next_run_id(conn):
    n = conn.execute("SELECT COUNT(*) c FROM recalc_runs").fetchone()["c"]
    return f"RUN-{n + 1:04d}"


def cell_key(biz_date, metric, dim):
    return f"{biz_date}|{metric}|{dim}"


# ---------------------------------------------------------------- 报表期间

def month_range(period):
    y, m = int(period[:4]), int(period[5:7])
    first = date(y, m, 1)
    last = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    return first, last


def week_range(period):
    y, w = int(period[:4]), int(period[6:8])
    monday = date.fromisocalendar(y, w, 1)
    return monday, monday + timedelta(days=6)


def report_keys_for_date(d):
    iso = d.isocalendar()
    return [f"monthly:{d:%Y-%m}", f"weekly:{iso[0]}-W{iso[1]:02d}"]


def period_range(report_type, period):
    return month_range(period) if report_type == "monthly" else week_range(period)


def upsert_report_def(conn, report_key):
    rtype, period = report_key.split(":", 1)
    f, t = period_range(rtype, period)
    conn.execute(
        "INSERT OR IGNORE INTO report_defs(report_key, report_type, period, date_from, date_to)"
        " VALUES (?,?,?,?,?)",
        (report_key, rtype, period, f.isoformat(), t.isoformat()),
    )


# ---------------------------------------------------------------- 有效记录（快照 + 活跃更正叠加）

def _latest_snapshot_id(conn, biz_date):
    row = conn.execute(
        "SELECT snapshot_id FROM snapshots WHERE biz_date=? ORDER BY seq DESC LIMIT 1",
        (biz_date,),
    ).fetchone()
    return row["snapshot_id"] if row else None


def effective_records(conn, biz_date, include=(), exclude=()):
    """返回 (records, applied) ：
    records = 该日期最新快照记录 + 活跃 insert 更正合成记录，并应用 adjust/void 叠加；
    applied = [(correction_id, channel)] 实际影响到记录的更正及其作用渠道。
    include: 额外视为生效的更正（预览时把 pending 更正算进来）；
    exclude: 强制排除的更正。
    """
    recs = []
    snap = _latest_snapshot_id(conn, biz_date)
    if snap:
        recs = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM source_records WHERE snapshot_id=?", (snap,)
            ).fetchall()
        ]
    by_order = {r["order_id"]: r for r in recs}
    applied = []

    placeholders = ",".join("?" for _ in include) or "NULL"
    rows = conn.execute(
        f"""SELECT * FROM corrections
            WHERE biz_date=? AND (status='active' OR correction_id IN ({placeholders}))
            ORDER BY rowid""",
        (biz_date, *include),
    ).fetchall()
    exclude = set(exclude)
    for c in rows:
        if c["correction_id"] in exclude:
            continue
        p = json.loads(c["payload"])
        cid = c["correction_id"]
        if c["type"] == "adjust":
            t = by_order.get(c["target_order_id"])
            if t is not None:
                for field in ("amount", "qty", "status", "refund_amount"):
                    if field in p:
                        t[field] = p[field]
                applied.append((cid, t["channel"]))
        elif c["type"] == "void":
            t = by_order.get(c["target_order_id"])
            if t is not None and t["status"] != "void":
                t["status"] = "void"
                applied.append((cid, t["channel"]))
        elif c["type"] == "insert":
            oid = p["order_id"]
            if oid not in by_order:
                by_order[oid] = {
                    "id": None,
                    "order_id": oid,
                    "biz_date": biz_date,
                    "channel": p["channel"],
                    "amount": float(p["amount"]),
                    "qty": int(p.get("qty", 1)),
                    "status": p.get("status", "normal"),
                    "refund_amount": float(p.get("refund_amount", 0)),
                    "snapshot_id": None,
                    "_origin_correction": cid,
                }
                applied.append((cid, p["channel"]))
    return list(by_order.values()), applied


def compute_cell(conn, biz_date, metric, dim, rule_version, include=(), exclude=()):
    """计算单个指标单元格，返回 (value, input_record_ids, correction_ids)。"""
    recs, applied = effective_records(conn, biz_date, include, exclude)
    if dim != rules.ALL_DIM:
        recs = [r for r in recs if r["channel"] == dim]
        applied = [(cid, ch) for cid, ch in applied if ch == dim]
    value = rules.compute(recs, rule_version, metric)
    ids = sorted(r["id"] for r in recs if r["id"] is not None)
    return value, ids, sorted({cid for cid, _ in applied})


def dims_for_date(conn, biz_date, include=()):
    recs, _ = effective_records(conn, biz_date, include=include)
    channels = sorted({r["channel"] for r in recs})
    return channels + [rules.ALL_DIM]


def current_cell_version(conn, biz_date, metric, dim):
    return conn.execute(
        "SELECT * FROM cell_versions WHERE cell_key=? ORDER BY version_no DESC LIMIT 1",
        (cell_key(biz_date, metric, dim),),
    ).fetchone()


# ---------------------------------------------------------------- 快照导入

def import_snapshot(conn, biz_date, records, snapshot_id=None):
    date.fromisoformat(biz_date)  # 校验
    if not records:
        raise ValueError("快照记录不能为空")
    seq_row = conn.execute(
        "SELECT COALESCE(MAX(seq),0) s FROM snapshots WHERE biz_date=?", (biz_date,)
    ).fetchone()
    seq = seq_row["s"] + 1
    snapshot_id = snapshot_id or f"SNAP-{biz_date}-{seq}"
    conn.execute(
        "INSERT INTO snapshots(snapshot_id, biz_date, seq, imported_at, record_count)"
        " VALUES (?,?,?,?,?)",
        (snapshot_id, biz_date, seq, now_str(), len(records)),
    )
    for r in records:
        conn.execute(
            "INSERT INTO source_records(order_id, biz_date, channel, amount, qty, status,"
            " refund_amount, snapshot_id) VALUES (?,?,?,?,?,?,?,?)",
            (
                r["order_id"], biz_date, r["channel"], float(r["amount"]),
                int(r.get("qty", 1)), r.get("status", "normal"),
                float(r.get("refund_amount", 0)), snapshot_id,
            ),
        )
    conn.commit()

    has_cells = conn.execute(
        "SELECT 1 FROM cell_versions WHERE biz_date=? LIMIT 1", (biz_date,)
    ).fetchone()
    run_id = create_preview_run(
        conn, "snapshot", {"snapshot_id": snapshot_id}, [biz_date]
    )
    result = {"snapshot_id": snapshot_id, "seq": seq, "records": len(records)}
    if run_id is None:
        result["impact"] = "no_change"
    elif not has_cells:
        confirm_run(conn, run_id)  # 首日基线直接确认
        result["impact"] = "baseline_confirmed"
        result["run_id"] = run_id
    else:
        result["impact"] = "preview"  # 重导入已有日期 -> 走预览确认，不直接覆盖
        result["run_id"] = run_id
    audit(conn, "import_snapshot", {"snapshot_id": snapshot_id, "biz_date": biz_date,
                                    "records": len(records), "impact": result["impact"]})
    return result


# ---------------------------------------------------------------- 重算运行

def create_preview_run(conn, trigger_type, trigger_refs, dates):
    """对受影响日期做影响分析并生成预览运行；无任何值变化时返回 None。"""
    rule = rules.get_current_rule(conn)
    include = [
        cid for cid in trigger_refs.get("correction_ids", [])
        if conn.execute("SELECT 1 FROM corrections WHERE correction_id=? AND status='pending'",
                        (cid,)).fetchone()
    ]
    items = []
    for d in sorted(dates):
        for metric in rules.METRICS:
            for dim in dims_for_date(conn, d, include=include):
                value, rec_ids, corr_ids = compute_cell(
                    conn, d, metric, dim, rule, include=include
                )
                cur = current_cell_version(conn, d, metric, dim)
                if cur is None or abs(cur["value"] - value) > 1e-9:
                    items.append({
                        "biz_date": d, "metric": metric, "dim": dim,
                        "old_value": cur["value"] if cur else None,
                        "old_version_no": cur["version_no"] if cur else None,
                        "old_rule_version": cur["rule_version"] if cur else None,
                        "new_value": value,
                        "corrections_applied": corr_ids,
                    })
    if not items:
        return None

    affected_reports = sorted({rk for d in dates for rk in report_keys_for_date(date.fromisoformat(d))})
    for rk in affected_reports:
        upsert_report_def(conn, rk)
    run_id = _next_run_id(conn)
    conn.execute(
        "INSERT INTO recalc_runs(run_id, trigger_type, trigger_refs, rule_version, status,"
        " affected_dates, affected_reports, created_at) VALUES (?,?,?,?, 'preview', ?,?,?)",
        (run_id, trigger_type, json.dumps(trigger_refs, ensure_ascii=False), rule,
         json.dumps(sorted(dates)), json.dumps(affected_reports), now_str()),
    )
    for it in items:
        conn.execute(
            "INSERT INTO recalc_run_items(run_id, cell_key, biz_date, metric, dim, old_value,"
            " old_version_no, old_rule_version, new_value, corrections_applied)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, cell_key(it["biz_date"], it["metric"], it["dim"]), it["biz_date"],
             it["metric"], it["dim"], it["old_value"], it["old_version_no"],
             it["old_rule_version"], it["new_value"],
             json.dumps(it["corrections_applied"], ensure_ascii=False)),
        )
    # 覆盖到的已发布报表先打"已过期"标记：有未确认重算，不得当作最新事实
    for rk in affected_reports:
        conn.execute(
            "UPDATE report_versions SET stale=1 WHERE report_key=? AND status='published'", (rk,)
        )
    conn.commit()
    audit(conn, "preview_run", {"run_id": run_id, "trigger": trigger_type,
                                "dates": sorted(dates), "items": len(items),
                                "reports": affected_reports})
    return run_id


def confirm_run(conn, run_id):
    run = conn.execute("SELECT * FROM recalc_runs WHERE run_id=?", (run_id,)).fetchone()
    if not run:
        raise ValueError(f"重算运行不存在: {run_id}")
    if run["status"] != "preview":
        raise ValueError(f"重算运行 {run_id} 状态为 {run['status']}，不能确认")

    refs = json.loads(run["trigger_refs"])
    for cid in refs.get("correction_ids", []):
        conn.execute(
            "UPDATE corrections SET status='active' WHERE correction_id=? AND status='pending'",
            (cid,),
        )
        conn.execute(
            "INSERT INTO correction_events(correction_id, event, detail, at) VALUES (?,?,?,?)",
            (cid, "activated", f"由 {run_id} 确认生效", now_str()),
        )

    rule = run["rule_version"]
    withdrawn = set(refs.get("correction_ids", [])) if run["trigger_type"] == "withdrawal" else set()
    items = conn.execute(
        "SELECT * FROM recalc_run_items WHERE run_id=?", (run_id,)
    ).fetchall()
    for it in items:
        value, rec_ids, corr_ids = compute_cell(conn, it["biz_date"], it["metric"], it["dim"], rule)
        ver_no_row = conn.execute(
            "SELECT COALESCE(MAX(version_no),0) v FROM cell_versions WHERE cell_key=?",
            (it["cell_key"],),
        ).fetchone()
        ver_no = ver_no_row["v"] + 1
        restored_from = None
        if withdrawn:
            # 找回撤回前最后一个不含被撤回更正、且数值一致的历史版本，显式建立"恢复"链接
            for v in conn.execute(
                "SELECT * FROM cell_versions WHERE cell_key=? ORDER BY version_no DESC",
                (it["cell_key"],),
            ).fetchall():
                corr_of_v = set(json.loads(v["corrections_applied"]))
                if not (corr_of_v & withdrawn) and abs(v["value"] - value) < 1e-9:
                    restored_from = v["id"]
                    break
        conn.execute(
            "INSERT INTO cell_versions(cell_key, biz_date, metric, dim, version_no, value,"
            " rule_version, run_id, corrections_applied, input_record_ids,"
            " restored_from_version_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (it["cell_key"], it["biz_date"], it["metric"], it["dim"], ver_no, value, rule,
             run_id, json.dumps(corr_ids, ensure_ascii=False), json.dumps(rec_ids),
             restored_from, now_str()),
        )
        conn.execute(
            "UPDATE recalc_run_items SET new_value=?, new_version_no=? WHERE id=?",
            (value, ver_no, it["id"]),
        )
    conn.execute(
        "UPDATE recalc_runs SET status='confirmed', confirmed_at=? WHERE run_id=?",
        (now_str(), run_id),
    )
    # 下游报表：已为每个受影响且「已存在版本」的报表生成新草稿
    # （从未生成过的报表不在此自动建版，由用户在报表中心显式生成）
    for rk in json.loads(run["affected_reports"]):
        has_versions = conn.execute(
            "SELECT 1 FROM report_versions WHERE report_key=? LIMIT 1", (rk,)).fetchone()
        if has_versions:
            generate_report_version(conn, rk, run_id=run_id)
    conn.commit()
    audit(conn, "confirm_run", {"run_id": run_id, "items": len(items)})
    return {"run_id": run_id, "confirmed_items": len(items)}


def cancel_run(conn, run_id):
    run = conn.execute("SELECT * FROM recalc_runs WHERE run_id=?", (run_id,)).fetchone()
    if not run:
        raise ValueError(f"重算运行不存在: {run_id}")
    if run["status"] != "preview":
        raise ValueError(f"仅预览状态的运行可取消（当前 {run['status']}）")
    if run["trigger_type"] == "withdrawal":
        raise ValueError("撤回触发的重算必须确认，以保证结果回到正确版本；如需反悔请重新导入该更正")
    refs = json.loads(run["trigger_refs"])
    for cid in refs.get("correction_ids", []):
        conn.execute(
            "UPDATE corrections SET status='manual_pending' WHERE correction_id=? AND status='pending'",
            (cid,),
        )
        exists = conn.execute(
            "SELECT 1 FROM manual_queue WHERE correction_id=? AND status='pending'", (cid,)
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO manual_queue(correction_id, reason, status, created_at)"
                " VALUES (?,?, 'pending', ?)",
                (cid, "run_cancelled", now_str()),
            )
    conn.execute("UPDATE recalc_runs SET status='cancelled' WHERE run_id=?", (run_id,))
    conn.commit()
    audit(conn, "cancel_run", {"run_id": run_id})
    return {"run_id": run_id, "status": "cancelled"}


# ---------------------------------------------------------------- 更正接收（幂等）与撤回

def _resolve_target(conn, order_id):
    """在最新快照中查找目标记录（用于 adjust/void 定位影响日期）。"""
    return conn.execute(
        """SELECT r.* FROM source_records r
           JOIN snapshots s ON r.snapshot_id = s.snapshot_id
           WHERE r.order_id=?
           ORDER BY s.seq DESC, r.id DESC LIMIT 1""",
        (order_id,),
    ).fetchone()


def _to_manual(conn, cid, ctype, target, biz_date, payload, reason, results):
    conn.execute(
        "INSERT OR IGNORE INTO corrections(correction_id, type, target_order_id, biz_date,"
        " payload, payload_hash, status, received_at) VALUES (?,?,?,?,?,?, 'manual_pending', ?)",
        (cid, ctype, target, biz_date, json.dumps(payload, ensure_ascii=False),
         _payload_hash(payload), now_str()),
    )
    conn.execute(
        "INSERT INTO manual_queue(correction_id, reason, status, created_at)"
        " VALUES (?,?, 'pending', ?)",
        (cid, reason, now_str()),
    )
    conn.execute(
        "INSERT INTO correction_events(correction_id, event, detail, at) VALUES (?,?,?,?)",
        (cid, "received", f"进入人工队列: {reason}", now_str()),
    )
    conn.commit()
    results.append({"correction_id": cid, "result": "manual_queue", "reason": reason})


def import_corrections(conn, items, today_override=None, window_days=None):
    """批量接收更正。返回每条的处理结果与（如有）生成的预览运行。

    幂等：correction_id 已存在时——payload 相同记 duplicate（arrival_count+1，不叠加）；
    payload 不同记 conflict 并进人工队列。
    """
    today_ = today_override or today()
    window = window_days if window_days is not None else BACKFILL_WINDOW_DAYS
    results, pending_ids, pending_dates = [], [], set()

    for raw in items:
        cid = raw.get("correction_id")
        ctype = raw.get("type")
        if not cid or ctype not in ("adjust", "void", "insert"):
            results.append({"correction_id": cid, "result": "error",
                            "reason": "correction_id 缺失或 type 非法"})
            continue
        payload = {k: v for k, v in raw.items() if k not in ("correction_id", "type")}
        phash = _payload_hash(payload)

        existing = conn.execute(
            "SELECT * FROM corrections WHERE correction_id=?", (cid,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE corrections SET arrival_count = arrival_count + 1 WHERE correction_id=?",
                (cid,),
            )
            if existing["payload_hash"] == phash:
                conn.execute(
                    "INSERT INTO correction_events(correction_id, event, detail, at)"
                    " VALUES (?,?,?,?)",
                    (cid, "duplicate", "重复到达，payload 一致，已忽略（不重复叠加）", now_str()),
                )
                results.append({"correction_id": cid, "result": "duplicate_ignored"})
            else:
                conn.execute(
                    "INSERT INTO correction_events(correction_id, event, detail, at)"
                    " VALUES (?,?,?,?)",
                    (cid, "conflict", "同 ID 不同 payload，进人工队列", now_str()),
                )
                conn.execute(
                    "INSERT INTO manual_queue(correction_id, reason, status, created_at)"
                    " VALUES (?,?, 'pending', ?)",
                    (cid, "payload_conflict", now_str()),
                )
                results.append({"correction_id": cid, "result": "manual_queue",
                                "reason": "payload_conflict"})
            conn.commit()
            continue

        if ctype == "insert":
            biz_date = payload.get("biz_date")
            if not biz_date:
                results.append({"correction_id": cid, "result": "error",
                                "reason": "insert 缺少 biz_date"})
                continue
            dup = _resolve_target(conn, payload.get("order_id", ""))
            if dup:
                _to_manual(conn, cid, ctype, None, biz_date, payload,
                           "insert_order_exists", results)
                continue
            target = None
        else:
            target = raw.get("target_order_id")
            rec = _resolve_target(conn, target) if target else None
            if rec is None:
                _to_manual(conn, cid, ctype, target, None, payload,
                           "target_not_found", results)
                continue
            biz_date = rec["biz_date"]

        age = (today_ - date.fromisoformat(biz_date)).days
        if age > window:
            _to_manual(conn, cid, ctype, target, biz_date, payload,
                       f"out_of_window(>{window}d)", results)
            continue

        conn.execute(
            "INSERT INTO corrections(correction_id, type, target_order_id, biz_date, payload,"
            " payload_hash, received_at, status) VALUES (?,?,?,?,?,?,?, 'pending')",
            (cid, ctype, target, biz_date, json.dumps(payload, ensure_ascii=False),
             phash, now_str()),
        )
        conn.execute(
            "INSERT INTO correction_events(correction_id, event, detail, at) VALUES (?,?,?,?)",
            (cid, "received", "已接收，待重算确认", now_str()),
        )
        pending_ids.append(cid)
        pending_dates.add(biz_date)
        results.append({"correction_id": cid, "result": "accepted", "biz_date": biz_date})
        conn.commit()

    run_id = None
    if pending_dates:
        run_id = create_preview_run(
            conn, "correction", {"correction_ids": pending_ids}, sorted(pending_dates)
        )
        if run_id is None:  # 值无变化：直接激活，不留运行
            for cid in pending_ids:
                conn.execute(
                    "UPDATE corrections SET status='active' WHERE correction_id=?", (cid,))
            conn.commit()
    audit(conn, "import_corrections", {"count": len(items), "run_id": run_id,
                                       "results": results})
    return {"results": results, "run_id": run_id}


def withdraw_correction(conn, cid):
    c = conn.execute("SELECT * FROM corrections WHERE correction_id=?", (cid,)).fetchone()
    if not c:
        raise ValueError(f"更正不存在: {cid}")
    if c["status"] == "pending":
        # 尚未确认生效：取消其预览运行即可
        cancelled = []
        for run in conn.execute(
            "SELECT * FROM recalc_runs WHERE status='preview' AND trigger_type='correction'"
        ).fetchall():
            if cid in json.loads(run["trigger_refs"]).get("correction_ids", []):
                conn.execute(
                    "UPDATE recalc_runs SET status='cancelled' WHERE run_id=?", (run["run_id"],))
                cancelled.append(run["run_id"])
        conn.execute(
            "UPDATE corrections SET status='withdrawn', withdrawn_at=? WHERE correction_id=?",
            (now_str(), cid),
        )
        conn.execute(
            "INSERT INTO correction_events(correction_id, event, detail, at) VALUES (?,?,?,?)",
            (cid, "withdrawn", "生效前撤回，预览运行已取消", now_str()),
        )
        conn.commit()
        audit(conn, "withdraw_correction", {"correction_id": cid, "cancelled": cancelled})
        return {"correction_id": cid, "run_id": None, "cancelled_runs": cancelled}
    if c["status"] != "active":
        raise ValueError(f"更正 {cid} 当前状态为 {c['status']}，仅生效中的更正可撤回")

    conn.execute(
        "UPDATE corrections SET status='withdrawn', withdrawn_at=? WHERE correction_id=?",
        (now_str(), cid),
    )
    conn.execute(
        "INSERT INTO correction_events(correction_id, event, detail, at) VALUES (?,?,?,?)",
        (cid, "withdrawn", "已撤回，触发恢复重算", now_str()),
    )
    conn.commit()
    run_id = create_preview_run(
        conn, "withdrawal", {"correction_ids": [cid]}, [c["biz_date"]]
    )
    audit(conn, "withdraw_correction", {"correction_id": cid, "run_id": run_id})
    return {"correction_id": cid, "run_id": run_id}


# ---------------------------------------------------------------- 人工队列

def resolve_manual(conn, queue_id, action, note=""):
    q = conn.execute("SELECT * FROM manual_queue WHERE id=?", (queue_id,)).fetchone()
    if not q:
        raise ValueError(f"人工队列项不存在: {queue_id}")
    if q["status"] != "pending":
        raise ValueError("该队列项已处理")
    cid = q["correction_id"]
    run_id = None
    if action == "force":
        c = conn.execute(
            "SELECT * FROM corrections WHERE correction_id=?", (cid,)).fetchone()
        if not c or not c["biz_date"]:
            raise ValueError("无法强制回补：更正缺少可定位的业务日期")
        conn.execute(
            "UPDATE corrections SET status='pending' WHERE correction_id=?", (cid,))
        run_id = create_preview_run(
            conn, "manual_force", {"correction_ids": [cid]}, [c["biz_date"]])
        if run_id is None:
            conn.execute(
                "UPDATE corrections SET status='active' WHERE correction_id=?", (cid,))
        resolution = f"强制回补{('，运行 ' + run_id) if run_id else '（无值变化）'}。{note}"
    elif action == "close":
        conn.execute(
            "UPDATE corrections SET status='manual_closed' WHERE correction_id=?", (cid,))
        resolution = f"线下处理，关闭。{note}"
    else:
        raise ValueError("action 必须是 force 或 close")
    conn.execute(
        "UPDATE manual_queue SET status='resolved', resolution=?, resolved_at=? WHERE id=?",
        (resolution, now_str(), queue_id),
    )
    conn.commit()
    audit(conn, "resolve_manual", {"queue_id": queue_id, "action": action,
                                   "correction_id": cid, "run_id": run_id})
    return {"queue_id": queue_id, "action": action, "run_id": run_id}


# ---------------------------------------------------------------- 报表

def generate_report_version(conn, report_key, run_id=None):
    """基于当前指标单元格版本生成一个新的报表草稿版本。"""
    upsert_report_def(conn, report_key)
    d = conn.execute("SELECT * FROM report_defs WHERE report_key=?", (report_key,)).fetchone()
    cells = conn.execute(
        """SELECT cv.* FROM cell_versions cv
           JOIN (SELECT cell_key, MAX(version_no) v FROM cell_versions GROUP BY cell_key) cur
             ON cv.cell_key=cur.cell_key AND cv.version_no=cur.v
           WHERE cv.biz_date BETWEEN ? AND ?""",
        (d["date_from"], d["date_to"]),
    ).fetchall()
    agg = {}
    for c in cells:
        k = (c["metric"], c["dim"])
        if k not in agg:
            agg[k] = {"value": 0.0, "cell_version_ids": []}
        agg[k]["value"] += c["value"]
        agg[k]["cell_version_ids"].append(c["id"])
    ver_no = conn.execute(
        "SELECT COALESCE(MAX(version_no),0) v FROM report_versions WHERE report_key=?",
        (report_key,),
    ).fetchone()["v"] + 1
    cur = conn.execute(
        "INSERT INTO report_versions(report_key, version_no, status, stale, run_id,"
        " rule_version, created_at) VALUES (?,?, 'draft', 0, ?,?,?)",
        (report_key, ver_no, run_id, rules.get_current_rule(conn), now_str()),
    )
    rvid = cur.lastrowid
    for (metric, dim), a in sorted(agg.items()):
        conn.execute(
            "INSERT INTO report_items(report_version_id, metric, dim, value, cell_version_ids)"
            " VALUES (?,?,?,?,?)",
            (rvid, metric, dim, round(a["value"], 2), json.dumps(a["cell_version_ids"])),
        )
    # 已有发布版本 -> 标记过期（有新草稿待发布）
    conn.execute(
        "UPDATE report_versions SET stale=1 WHERE report_key=? AND status='published'",
        (report_key,),
    )
    conn.commit()
    return rvid


def generate_all_reports(conn):
    rows = conn.execute("SELECT DISTINCT biz_date FROM cell_versions").fetchall()
    keys = sorted({rk for r in rows for rk in report_keys_for_date(date.fromisoformat(r["biz_date"]))})
    return [{"report_key": rk, "version_id": generate_report_version(conn, rk)} for rk in keys]


def publish_blockers(conn, rvid):
    v = conn.execute("SELECT * FROM report_versions WHERE id=?", (rvid,)).fetchone()
    if not v:
        return ["报表版本不存在"]
    blockers = []
    if v["status"] != "draft":
        blockers.append(f"仅草稿版本可发布（当前 {v['status']}）")
    latest = conn.execute(
        "SELECT MAX(version_no) v FROM report_versions WHERE report_key=?",
        (v["report_key"],),
    ).fetchone()["v"]
    if v["version_no"] != latest:
        blockers.append("存在更新的版本，请发布最新草稿")
    d = conn.execute(
        "SELECT * FROM report_defs WHERE report_key=?", (v["report_key"],)).fetchone()
    for run in conn.execute(
        "SELECT run_id, affected_dates FROM recalc_runs WHERE status='preview'"
    ).fetchall():
        dates = set(json.loads(run["affected_dates"]))
        if any(d["date_from"] <= x <= d["date_to"] for x in dates):
            blockers.append(f"重算 {run['run_id']} 尚未确认，覆盖该报表区间，不得发布")
    return blockers


def publish_report(conn, rvid):
    blockers = publish_blockers(conn, rvid)
    if blockers:
        return {"ok": False, "blockers": blockers}
    v = conn.execute("SELECT * FROM report_versions WHERE id=?", (rvid,)).fetchone()
    conn.execute(
        "UPDATE report_versions SET status='superseded' WHERE report_key=? AND status='published'",
        (v["report_key"],),
    )
    conn.execute(
        "UPDATE report_versions SET status='published', published_at=?, stale=0 WHERE id=?",
        (now_str(), rvid),
    )
    conn.execute(
        "UPDATE report_versions SET stale=0 WHERE report_key=?", (v["report_key"],))
    conn.commit()
    audit(conn, "publish_report", {"report_version_id": rvid,
                                   "report_key": v["report_key"],
                                   "version_no": v["version_no"]})
    return {"ok": True, "report_version_id": rvid}


def compare_versions(conn, a_id, b_id):
    def items_of(rvid):
        return {
            (r["metric"], r["dim"]): r
            for r in conn.execute(
                "SELECT * FROM report_items WHERE report_version_id=?", (rvid,)).fetchall()
        }

    ia, ib = items_of(a_id), items_of(b_id)
    rows = []
    for key in sorted(set(ia) | set(ib)):
        va = ia[key]["value"] if key in ia else None
        vb = ib[key]["value"] if key in ib else None
        rows.append({
            "metric": key[0], "dim": key[1],
            "a": va, "b": vb,
            "delta": round(vb - va, 2) if va is not None and vb is not None else None,
            "changed": va != vb,
        })
    va_ = conn.execute("SELECT * FROM report_versions WHERE id=?", (a_id,)).fetchone()
    vb_ = conn.execute("SELECT * FROM report_versions WHERE id=?", (b_id,)).fetchone()
    return {
        "a": {"id": a_id, "report_key": va_["report_key"], "version_no": va_["version_no"],
              "rule_version": va_["rule_version"], "status": va_["status"]},
        "b": {"id": b_id, "report_key": vb_["report_key"], "version_no": vb_["version_no"],
              "rule_version": vb_["rule_version"], "status": vb_["status"]},
        "rows": rows,
    }


def trace_item(conn, report_version_id, metric, dim):
    """从报表单元格一路追到指标版本、触发更正与源记录。"""
    item = conn.execute(
        "SELECT * FROM report_items WHERE report_version_id=? AND metric=? AND dim=?",
        (report_version_id, metric, dim),
    ).fetchone()
    if not item:
        raise ValueError("报表项不存在")
    cells = []
    for cvid in json.loads(item["cell_version_ids"]):
        cv = conn.execute("SELECT * FROM cell_versions WHERE id=?", (cvid,)).fetchone()
        corrections = []
        for cid in json.loads(cv["corrections_applied"]):
            c = conn.execute(
                "SELECT * FROM corrections WHERE correction_id=?", (cid,)).fetchone()
            if c:
                corrections.append({
                    "correction_id": c["correction_id"], "type": c["type"],
                    "status": c["status"], "payload": json.loads(c["payload"]),
                    "target_order_id": c["target_order_id"],
                })
        records = []
        ids = json.loads(cv["input_record_ids"])
        if ids:
            ph = ",".join("?" for _ in ids)
            for r in conn.execute(
                f"SELECT * FROM source_records WHERE id IN ({ph}) ORDER BY order_id", ids
            ).fetchall():
                records.append(dict(r))
        cells.append({
            "cell_version_id": cv["id"], "biz_date": cv["biz_date"],
            "metric": cv["metric"], "dim": cv["dim"], "value": cv["value"],
            "version_no": cv["version_no"], "rule_version": cv["rule_version"],
            "run_id": cv["run_id"],
            "restored_from_version_id": cv["restored_from_version_id"],
            "corrections": corrections, "records": records,
        })
    return {"metric": metric, "dim": dim, "value": item["value"], "cells": cells}
