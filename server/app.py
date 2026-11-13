"""HTTP 服务：JSON API + 静态前端。仅用标准库。"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import core, db, rules, samples

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


def _rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def overview(conn):
    c = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    return {
        "snapshot_days": c("SELECT COUNT(DISTINCT biz_date) FROM snapshots"),
        "source_records": c("SELECT COUNT(*) FROM source_records"),
        "corrections": {
            "pending": c("SELECT COUNT(*) FROM corrections WHERE status='pending'"),
            "active": c("SELECT COUNT(*) FROM corrections WHERE status='active'"),
            "withdrawn": c("SELECT COUNT(*) FROM corrections WHERE status='withdrawn'"),
            "manual": c("SELECT COUNT(*) FROM corrections WHERE status='manual_pending'"),
        },
        "preview_runs": c("SELECT COUNT(*) FROM recalc_runs WHERE status='preview'"),
        "confirmed_runs": c("SELECT COUNT(*) FROM recalc_runs WHERE status='confirmed'"),
        "published_reports": c("SELECT COUNT(*) FROM report_versions WHERE status='published'"),
        "stale_reports": c("SELECT COUNT(*) FROM report_versions WHERE status='published' AND stale=1"),
        "manual_pending": c("SELECT COUNT(*) FROM manual_queue WHERE status='pending'"),
        "current_rule": rules.get_current_rule(conn),
        "backfill_window_days": core.BACKFILL_WINDOW_DAYS,
        "recent_audit": _rows(conn, "SELECT * FROM audit_log ORDER BY id DESC LIMIT 12"),
    }


def list_cells(conn):
    return _rows(conn, """
        SELECT cv.* FROM cell_versions cv
        JOIN (SELECT cell_key, MAX(version_no) v FROM cell_versions GROUP BY cell_key) cur
          ON cv.cell_key=cur.cell_key AND cv.version_no=cur.v
        ORDER BY cv.biz_date DESC, cv.metric, cv.dim""")


def list_reports(conn):
    defs = _rows(conn, "SELECT * FROM report_defs ORDER BY report_key")
    for d in defs:
        d["versions"] = _rows(
            conn, "SELECT * FROM report_versions WHERE report_key=? ORDER BY version_no DESC",
            (d["report_key"],))
        for v in d["versions"]:
            v["blockers"] = core.publish_blockers(conn, v["id"]) if v["status"] == "draft" else []
    return defs


def make_handler(db_path):
    class Handler(BaseHTTPRequestHandler):
        def _conn(self):
            return db.connect(db_path)

        def _send(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, msg, status=400):
            self._send({"error": str(msg)}, status)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))

        def log_message(self, *a):  # 静音访问日志
            pass

        # ---------------------------------------------------------- GET
        def do_GET(self):
            u = urlparse(self.path)
            path, qs = u.path, parse_qs(u.query)
            try:
                conn = self._conn()
                if path == "/" or path == "/index.html":
                    return self._static("index.html", "text/html; charset=utf-8")
                if path.startswith("/static/"):
                    name = path[len("/static/"):]
                    ctype = ("text/javascript; charset=utf-8" if name.endswith(".js")
                             else "text/css; charset=utf-8")
                    return self._static(name, ctype)
                if path == "/api/overview":
                    return self._send(overview(conn))
                if path == "/api/rules":
                    return self._send({
                        "current": rules.get_current_rule(conn),
                        "rules": _rows(conn, "SELECT * FROM rule_versions"),
                    })
                if path == "/api/cells":
                    return self._send({"cells": list_cells(conn)})
                if path == "/api/cell-versions":
                    return self._send({"versions": _rows(conn, """
                        SELECT * FROM cell_versions WHERE biz_date=? AND metric=? AND dim=?
                        ORDER BY version_no DESC""",
                        (qs["date"][0], qs["metric"][0], qs["dim"][0]))})
                if path == "/api/corrections":
                    return self._send({"corrections": _rows(
                        conn, "SELECT * FROM corrections ORDER BY received_at DESC, correction_id")})
                if path == "/api/runs":
                    runs = _rows(conn, "SELECT * FROM recalc_runs ORDER BY created_at DESC, run_id DESC")
                    for r in runs:
                        r["trigger_refs"] = json.loads(r["trigger_refs"])
                        r["affected_dates"] = json.loads(r["affected_dates"])
                        r["affected_reports"] = json.loads(r["affected_reports"])
                    return self._send({"runs": runs})
                if path.startswith("/api/runs/"):
                    run_id = path.rsplit("/", 1)[1]
                    run = conn.execute(
                        "SELECT * FROM recalc_runs WHERE run_id=?", (run_id,)).fetchone()
                    if not run:
                        return self._error("运行不存在", 404)
                    run = dict(run)
                    for k in ("trigger_refs", "affected_dates", "affected_reports"):
                        run[k] = json.loads(run[k])
                    items = _rows(conn,
                        "SELECT * FROM recalc_run_items WHERE run_id=? ORDER BY biz_date, metric, dim",
                        (run_id,))
                    for it in items:
                        it["corrections_applied"] = json.loads(it["corrections_applied"])
                    run["items"] = items
                    return self._send(run)
                if path == "/api/reports":
                    return self._send({"reports": list_reports(conn)})
                if path.startswith("/api/report-versions/"):
                    rvid = int(path.rsplit("/", 1)[1])
                    v = conn.execute("SELECT * FROM report_versions WHERE id=?", (rvid,)).fetchone()
                    if not v:
                        return self._error("报表版本不存在", 404)
                    items = _rows(conn,
                        "SELECT * FROM report_items WHERE report_version_id=? ORDER BY metric, dim",
                        (rvid,))
                    return self._send({"version": dict(v), "items": items})
                if path == "/api/compare":
                    return self._send(core.compare_versions(
                        conn, int(qs["a"][0]), int(qs["b"][0])))
                if path == "/api/trace":
                    return self._send(core.trace_item(
                        conn, int(qs["report_version_id"][0]), qs["metric"][0], qs["dim"][0]))
                if path == "/api/manual-queue":
                    rows = _rows(conn, "SELECT * FROM manual_queue ORDER BY id DESC")
                    for r in rows:
                        c = conn.execute("SELECT * FROM corrections WHERE correction_id=?",
                                         (r["correction_id"],)).fetchone()
                        r["correction"] = dict(c) if c else None
                        if c:
                            r["correction"]["payload"] = json.loads(c["payload"])
                    return self._send({"queue": rows})
                if path == "/api/audit":
                    return self._send({"audit": _rows(
                        conn, "SELECT * FROM audit_log ORDER BY id DESC LIMIT 200")})
                return self._error("not found", 404)
            except Exception as e:  # noqa: BLE001
                return self._error(e, 500)

        # ---------------------------------------------------------- POST
        def do_POST(self):
            u = urlparse(self.path)
            path = u.path
            try:
                body = self._body()
                conn = self._conn()
                if path == "/api/import/snapshot":
                    return self._send(core.import_snapshot(
                        conn, body["biz_date"], body["records"], body.get("snapshot_id")))
                if path == "/api/import/corrections":
                    return self._send(core.import_corrections(conn, body["corrections"]))
                if path == "/api/corrections/withdraw":
                    return self._send(core.withdraw_correction(conn, body["correction_id"]))
                if path == "/api/runs/confirm":
                    return self._send(core.confirm_run(conn, body["run_id"]))
                if path == "/api/runs/cancel":
                    return self._send(core.cancel_run(conn, body["run_id"]))
                if path == "/api/reports/generate":
                    return self._send({"generated": core.generate_all_reports(conn)})
                if path == "/api/reports/publish":
                    return self._send(core.publish_report(conn, int(body["report_version_id"])))
                if path == "/api/manual-queue/resolve":
                    return self._send(core.resolve_manual(
                        conn, int(body["id"]), body["action"], body.get("note", "")))
                if path == "/api/rule/set-current":
                    rules.set_current_rule(conn, body["rule_version"])
                    core.audit(conn, "set_rule", f"当前规则版本切换为 {body['rule_version']}")
                    return self._send({"ok": True, "current": body["rule_version"]})
                if path == "/api/demo/seed":
                    return self._send(samples.seed_demo(conn))
                if path == "/api/reset":
                    db.reset(conn)
                    core.audit(conn, "reset", "业务数据已清空")
                    return self._send({"ok": True})
                return self._error("not found", 404)
            except KeyError as e:
                return self._error(f"缺少字段: {e}", 400)
            except ValueError as e:
                return self._error(e, 400)
            except Exception as e:  # noqa: BLE001
                return self._error(e, 500)

        def _static(self, name, ctype):
            safe = os.path.normpath(name).lstrip("/")
            full = os.path.join(WEB_DIR, safe)
            if not full.startswith(WEB_DIR) or not os.path.isfile(full):
                return self._error("not found", 404)
            with open(full, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(db_path, port=8000):
    conn = db.connect(db_path)
    db.init(conn)
    rules.seed_rules(conn, core.now_str())
    conn.close()
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(db_path))
    print(f"迟到更正处理台已启动: http://localhost:{port}  (数据库: {db_path})")
    server.serve_forever()
