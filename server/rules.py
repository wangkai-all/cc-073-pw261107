"""指标规则注册表。

规则按版本固化，历史结果版本永远记录当时使用的 rule_version；
切换当前规则只影响之后的重算，不回写历史版本。
"""

RULES = {
    "RULE-V1": {
        "description": "成交口径 v1：GMV=有效订单金额合计（退款单不扣减退款）",
        "include_status": ("normal", "refunded"),
        "net_refund": False,
    },
    "RULE-V2": {
        "description": "净成交口径 v2：GMV=金额-退款，剔除作废单",
        "include_status": ("normal", "refunded"),
        "net_refund": True,
    },
}

METRICS = ("gmv", "orders")
ALL_DIM = "ALL"


def seed_rules(conn, now):
    for ver, spec in RULES.items():
        conn.execute(
            "INSERT OR IGNORE INTO rule_versions(rule_version, description, is_current, created_at)"
            " VALUES (?,?,?,?)",
            (ver, spec["description"], 1 if ver == "RULE-V1" else 0, now),
        )
    conn.commit()


def get_current_rule(conn):
    row = conn.execute(
        "SELECT rule_version FROM rule_versions WHERE is_current=1"
    ).fetchone()
    return row["rule_version"] if row else "RULE-V1"


def set_current_rule(conn, rule_version):
    if rule_version not in RULES:
        raise ValueError(f"未知规则版本: {rule_version}")
    conn.execute("UPDATE rule_versions SET is_current=0")
    conn.execute(
        "UPDATE rule_versions SET is_current=1 WHERE rule_version=?", (rule_version,)
    )
    conn.commit()


def compute(records, rule_version, metric):
    """按规则版本聚合一组有效记录，返回指标值。"""
    spec = RULES[rule_version]
    inc = [r for r in records if r["status"] in spec["include_status"]]
    if metric == "orders":
        return len(inc)
    if metric == "gmv":
        if spec["net_refund"]:
            return round(sum(r["amount"] - r["refund_amount"] for r in inc), 2)
        return round(sum(r["amount"] for r in inc), 2)
    raise ValueError(f"未知指标: {metric}")
