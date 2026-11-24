"""出账：人读 Markdown 报告与机读 JSON（含按场次/按合作方对平视图）。"""
from __future__ import annotations

import json
from fractions import Fraction

from .evidence import Snapshot
from .increment import ScenarioResult
from .settle import AdjustmentBill, Settlement
from .settle import round_half_up


def _money(value: Fraction | int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    return round_half_up(value)


def _fraction_str(value: Fraction | None) -> str | None:
    if value is None:
        return None
    return f"{float(value):.4f}"


def scenario_to_dict(result: ScenarioResult) -> dict:
    return {
        "basis": result.basis,
        "assignment": {tender: {"session": s, "studio": st}
                       for tender, (s, st) in result.assignment.items()},
        "benchmarks": [
            {
                "match_class": benchmark.match_class,
                "control_sessions": list(benchmark.control_sessions),
                "pooled_revenue": benchmark.pooled_revenue,
                "pooled_scale": benchmark.pooled_scale,
                "rate_per_unit": _fraction_str(benchmark.rate),
                "note": benchmark.note,
                "identifiable": benchmark.identifiable,
            }
            for benchmark in result.benchmarks.values()
        ],
        "by_session": [
            {**row,
             "counterfactual": _money(row["counterfactual"]),
             "increment": _money(row["increment"])}
            for row in result.by_session_table()
        ],
        "by_studio": [
            {**row,
             "counterfactual": _money(row["counterfactual"]),
             "increment": _money(row["increment"]),
             "net_increment": _money(row["net_increment"])}
            for row in result.by_studio_table()
        ],
        "joint": {
            "actual_net": result.joint_actual,
            "counterfactual": _money(result.joint_counterfactual),
            "increment": _money(result.joint_increment),
            "direct_cost": result.joint_direct_cost,
            "net_increment": _money(result.joint_net_increment),
        },
        "unidentifiable_sessions": list(result.unidentifiable_sessions),
        "unidentifiable_actual": result.unidentifiable_actual,
    }


def settlement_to_dict(settlement: Settlement) -> dict:
    return {
        "period": settlement.period,
        "cutoff_day": settlement.cutoff,
        "basis": settlement.basis,
        "pool": {
            "pool_key": settlement.pool.pool_key,
            "pool_rate": float(settlement.pool.pool_rate),
            "pool_amount_by_scenario": list(settlement.pool.pool_amounts),
            "joint_net_increment_by_scenario": [
                _money(v) for v in settlement.pool.joint_net_increments],
            "pool_signable_total": settlement.pool.pool_signable,
            "notes": list(settlement.pool.notes),
        },
        "studios": [
            {
                "studio": row.studio,
                "guaranteed": row.guaranteed,
                "max_bonus": row.max_bonus,
                "bonus_candidates_by_scenario": list(row.bonus_candidates),
                "bonus_signable": row.bonus_signable,
                "bonus_pending": row.bonus_pending,
                "unidentifiable_window": row.unidentifiable,
                "guarantee_paid": row.guarantee_paid,
                "bonus_paid": row.bonus_paid,
                "bonus_reserved": row.bonus_reserved,
                "custodial_held": row.custodial_held,
                "guarantee_cash_payable": row.guarantee_cash_payable,
                "attribution_payable": row.attribution_payable,
                "bonus_overpaid": row.bonus_overpaid,
                "receivable_carry": row.receivable_carry,
                "carry_in": row.carry_in,
                "carry_out": row.carry_out,
                "signable_lines": list(row.signable_lines),
                "pending_lines": list(row.pending_lines),
            }
            for row in settlement.studios
        ],
        "totals": {
            "signable": settlement.total_signable,
            "guarantee_payable": settlement.total_guarantee_payable,
            "bonus_signable": settlement.total_bonus_signable,
            "reserved_kept": settlement.total_reserved_kept,
            "carry_out": settlement.total_carry_out,
        },
        "unidentifiable_sessions": list(settlement.unidentifiable_sessions),
        "unknown_notes": list(settlement.unknown_notes),
        "scenarios": [scenario_to_dict(result) for result in settlement.scenarios],
    }


def adjustment_to_dict(bill: AdjustmentBill) -> dict:
    return {
        "period": bill.period,
        "prior_cutoff": bill.prior_cutoff,
        "new_cutoff": bill.new_cutoff,
        "basis": bill.basis,
        "control_changes": list(bill.control_changes),
        "lines": [line.__dict__ for line in bill.lines],
        "formal_refs": list(bill.formal_refs),
    }


def render_markdown(settlement: Settlement, snapshot: Snapshot,
                    bill: AdjustmentBill | None = None) -> str:
    out: list[str] = []
    out.append(f"# 联展结算报告 · {settlement.period} · 截至第 {settlement.cutoff} 日"
               f" · 规模口径 {settlement.basis}")
    out.append("")
    states = snapshot.exposure_states()
    out.append("## 1. 证据与共享团确认")
    for exposure_id, state in sorted(states.items()):
        out.append(f"- {exposure_id}: {'已确认→' + state.confirmed_session if state.confirmed else '未确认'}"
                   f"（依据 {state.basis}）")
    out.append("")
    out.append("## 2. 对照窗口资格")
    for session_id, reasons in sorted(snapshot.eligibility_reasons().items()):
        for reason in reasons:
            out.append(f"- {session_id}: {reason}")
    out.append("")
    out.append("## 3. 可行归属场景")
    for index, result in enumerate(settlement.scenarios, start=1):
        assigns = ", ".join(f"{t}→{s}/{st}" for t, (s, st) in sorted(result.assignment.items()))
        out.append(f"- 场景 {index}: {assigns}")
    out.append("")
    out.append("## 4. 新增净收益（按场次视图，逐可行场景）")
    for index, result in enumerate(settlement.scenarios, start=1):
        assigns = ", ".join(f"{t}→{s}/{st}" for t, (s, st) in sorted(result.assignment.items()))
        out.append(f"### 场景 {index}: {assigns}")
        out.append("| 场次 | 同类档 | 合作方 | 实收净额 | 反事实 | 新增收益 | 可识别 | 说明 |")
        out.append("|---|---|---|---:|---:|---:|---|---|")
        for show in result.shows:
            out.append(f"| {show.session} | {show.match_class} | {','.join(show.studios) or '-'} "
                       f"| {show.actual_net} "
                       f"| {_money(show.counterfactual) if show.counterfactual is not None else '—'} "
                       f"| {_money(show.increment) if show.increment is not None else '不可识别'} "
                       f"| {'是' if show.identifiable else '否'} | {show.note} |")
        out.append("")
    out.append("## 5. 新增净收益（按合作方视图，逐场景与场次视图同范围对平）")
    for index, result in enumerate(settlement.scenarios, start=1):
        out.append(f"### 场景 {index}")
        out.append("| 合作方 | 实收净额 | 反事实 | 新增收益 | 边际开支 | 新增净收益 | 不可识别实收 |")
        out.append("|---|---:|---:|---:|---:|---:|---:|")
        for row in result.studios:
            out.append(f"| {row.studio} | {row.actual_net} | {_money(row.counterfactual)} "
                       f"| {_money(row.increment)} | {row.direct_cost} "
                       f"| {_money(row.net_increment)} | {row.unidentifiable_actual} |")
        out.append("")
    out.append("## 6. 保底与共同奖金池")
    out.append(f"- 池 {settlement.pool.pool_key}，rate={float(settlement.pool.pool_rate):g}，"
               f"各场景奖金池 {list(settlement.pool.pool_amounts)}，"
               f"实际可签合计 {settlement.pool.pool_signable}")
    for note in settlement.pool.notes:
        out.append(f"- {note}")
    out.append("")
    out.append("| 合作方 | 保底 | 保底净应付 | 归属补付 | 奖金候选(各场景) | 可签奖金 | 已发奖金 | 留置 | 托管 | 待抵扣总额 | 未决说明 |")
    out.append("|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---|")
    for row in settlement.studios:
        pending = "；".join(line.get("basis", line.get("kind", "")) for line in row.pending_lines)
        out.append(f"| {row.studio} | {row.guaranteed} | {row.guarantee_cash_payable} "
                   f"| {row.attribution_payable} | {list(row.bonus_candidates)} "
                   f"| {row.bonus_signable} | {row.bonus_paid} | {row.bonus_reserved} "
                   f"| {row.custodial_held} | {row.carry_out} | {pending} |")
    out.append("")
    out.append("## 7. 合计")
    for key, value in settlement_to_dict(settlement)["totals"].items():
        out.append(f"- {key}: {value}")
    if settlement.unidentifiable_sessions:
        out.append(f"- 无同类对照、不提供奖金依据的窗口: {', '.join(settlement.unidentifiable_sessions)}"
                   "（保底仍独立有效）")
    out.append("")
    if bill is not None:
        out.append("## 8. 调账单（历史出账不回改）")
        for change in bill.control_changes:
            out.append(f"- 对照变动 {change['session']} {change['change']}: "
                       f"{'；'.join(change['reasons'])}")
        out.append("")
        out.append("| 编号 | 合作方 | 类型 | 金额(分) | 方向 | 证据 | 说明 |")
        out.append("|---|---|---|---:|---|---|---|")
        for line in bill.lines:
            out.append(f"| {line.code} | {line.studio or '-'} | {line.kind} "
                       f"| {line.amount if line.amount is not None else '—'} | {line.direction} "
                       f"| {line.evidence} | {line.explanation} |")
        out.append("")
        out.append(f"可回查正式单: {', '.join(bill.formal_refs)}")
    return "\n".join(out) + "\n"


def write_json(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as target:
        json.dump(payload, target, ensure_ascii=False, indent=2, sort_keys=True)
