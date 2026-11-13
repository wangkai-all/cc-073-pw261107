# 迟到更正处理台

导入每日业务快照与更正记录，按更正影响范围找出需要重算的日期、指标和下游报表；
每次重算先生成预览（旧值 / 新值 / 触发更正 / 规则版本），确认后才创建新的结果版本。
纯 Python 标准库实现（SQLite + http.server + 原生前端），零外部依赖。

## 运行

```bash
python3 run.py --port 8000        # 打开 http://localhost:8000
```

首次启动后点页面右上角 **「加载演示数据」**，即可得到一套完整样例：
上月末 3 天 + 最近 10 天快照、跨期更正与撤回、重复投递判重、超窗口人工队列、
规则版本升级（RULE-V1 成交口径 → RULE-V2 净成交口径），并故意留下
**1 个未确认的重算预览** 和 **2 条人工队列待办** 供在页面上操作。

```bash
python3 -m unittest discover -s tests   # 12 个域逻辑测试
python3 scripts/demo.py                 # 命令行端到端演示（打印每步旧值->新值）
```

## 核心机制

| 需求 | 实现 |
| --- | --- |
| 影响范围分析 | 更正 → 业务日期 → 该日期的指标单元格（指标 × 渠道维度）→ 覆盖该区间的月报/周报 |
| 先预览后确认 | `recalc_runs`（preview）逐项列出旧值/旧版本/旧规则、新值、差异、该单元格吃到的更正；确认才写 `cell_versions` 新版本 |
| 重复到达不叠加 | `correction_id` 唯一 + payload 哈希：相同→记 duplicate（到达次数+1，不入计算）；不同→冲突进人工队列 |
| 撤回恢复正确版本 | 更正是快照上的叠加层而非就地修改；撤回 = 重算时排除该更正并**追加新版本**，与历史某版本数值一致时以 `restored_from_version_id` 显式链接。新快照重导入也只生成预览，已生效更正继续叠加在新快照之上，绝不简单覆盖 |
| 可回补窗口 | 默认 45 天（`core.BACKFILL_WINDOW_DAYS`），超窗更正进人工队列，可「强制回补」（仍走预览确认）或「线下关闭」 |
| 发布门禁 | 报表版本仅在「无覆盖其区间的未确认重算」时可发布；预览生成时已发布版本自动打「已过期」标记 |
| 版本比较与溯源 | 任意两个报表版本逐行 diff；每行可追到指标版本 → 触发更正 → 源记录（含所属快照） |
| 规则版本 | 规则按版本固化（RULE-V1/V2），每个结果版本记录所用规则版本；切换只影响之后的重算 |

## 页面

总览 · 数据导入 · 更正记录（含撤回）· 重算中心（预览/确认/取消）· 指标版本（逐单元格版本史）·
报表中心（生成/发布/过期标记）· 版本对比（diff + 一键溯源）· 人工队列 · 审计日志

## 数据流

```
快照导入 ─┐
          ├─> 影响分析 ─> 重算预览(preview) ─确认─> cell_versions 新版本
更正到达 ─┤        │                                │
  (判重/窗口/冲突)  └─> 受影响报表打「已过期」          └─> 已有报表再生成草稿 ─门禁─> 发布
更正撤回 ─┘
超窗/冲突 ─> 人工队列 ─强制回补--> 回到重算预览
```

## 目录

```
run.py              入口
server/             db.py(存储) rules.py(规则版本) core.py(域逻辑) samples.py(演示样例) app.py(HTTP)
web/                单页前端（原生 JS）
tests/test_core.py  域逻辑测试（幂等/撤回/窗口/门禁/覆盖防护/血缘）
scripts/demo.py     命令行端到端演示
```

## 主要 API

```
POST /api/import/snapshot        {biz_date, records:[...]}
POST /api/import/corrections     {corrections:[{correction_id,type,...}]}
POST /api/corrections/withdraw   {correction_id}
GET  /api/runs  GET /api/runs/{id}
POST /api/runs/confirm | /api/runs/cancel
GET  /api/cells  GET /api/cell-versions?date&metric&dim
GET  /api/reports  POST /api/reports/generate  POST /api/reports/publish
GET  /api/compare?a=&b=   GET /api/trace?report_version_id&metric&dim
GET  /api/manual-queue    POST /api/manual-queue/resolve {id, action: force|close}
POST /api/rule/set-current  POST /api/demo/seed  POST /api/reset
```

更正类型：`adjust`（改金额/数量/状态）、`void`（作废）、`insert`（漏单补录，需带 biz_date/channel/amount）。
