"""关账结算：保底、共同奖金池、单方上限、预留、已付、结转与调账单。

规则
====
1. 保底是独立条款：哪怕反事实证据不足（增量不可识别），保底责任也不消失。
2. 共同奖金池 = max(合办新增净收益, 0) × pool_rate；pool_rate 以同一 pool_key 的合同为准。
3. 各方奖金同时受三道约束：本方新增净收益的非负部分、单方 max_bonus、同一共同池总额；
   候选合计超出共同池时按比例缩减，任何一方都不能在联合总计里拿到“最有利分派”。
4. 归属摇摆时：
   - 各场景都能兑现的部分才可签发（取场景最小值）；
   - 结论摇摆的部分留未决（reserve），不静默释放；
   - 无同类对照的部分不产生奖金依据，但保底照常。
5. 已付不改历史；应减金额作为本方差额待抵扣（carry）或标争议，禁止用另一合作方的争议款冲抵。
6. 托管款（paid.tsv 中 state=reserve）不是已付现金，凭证确认归属后再对该方处理。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .evidence import Snapshot, period_studios
from .increment import ScenarioResult
from .records import Packet, Right


def round_half_up(value: Fraction) -> int:
    """金额落回整数分（半数向上），仅在出账边界取整，内部保持精确分数。"""
    whole = value.numerator // value.denominator
    remainder = value.numerator % value.denominator
    if remainder * 2 >= value.denominator:
        whole += 1
    return whole


@dataclass(frozen=True)
class StudioSettlement:
    studio: str
    guaranteed: int
    max_bonus: int
    bonus_candidates: tuple[int, ...]       # 每个可行场景下的可发奖金（整数分）
    bonus_signable: int                     # 各场景同时可兑现部分
    bonus_pending: bool                     # 是否存在摇摆未决
    unidentifiable: bool                    # 是否含无对照窗口
    guarantee_paid: int                     # 已签保底
    bonus_paid: int                         # 已发奖金（历史，不改）
    bonus_reserved: int                     # 已留置奖金（非已付）
    custodial_held: int                     # 已确认归本方、仍托管未付的款
    guarantee_cash_payable: int             # 保底净现金（保底 − 已签保底），不为负
    attribution_payable: int                # 凭证确认归属后应补付本方的历史托管/关账款
    bonus_overpaid: int                     # 历史奖金超发形成的本方待抵扣
    receivable_carry: int                   # 已付款被改归他方形成的本方应收/争议
    carry_in: int                           # 上期结转来的待抵扣
    carry_out: int                          # 结转下期的待抵扣总额（仅本方）
    signable_lines: tuple[dict, ...]
    pending_lines: tuple[dict, ...]


@dataclass(frozen=True)
class PoolSettlement:
    pool_key: str
    pool_rate: Fraction
    pool_amounts: tuple[int, ...]           # 各场景的奖金池
    joint_net_increments: tuple[Fraction, ...]
    pool_signable: int
    notes: tuple[str, ...]


@dataclass(frozen=True)
class Settlement:
    period: str
    cutoff: int
    basis: str
    scenarios: tuple[ScenarioResult, ...]
    studios: tuple[StudioSettlement, ...]
    pool: PoolSettlement
    total_signable: int
    total_guarantee_payable: int
    total_bonus_signable: int
    total_reserved_kept: int
    total_carry_out: int
    unidentifiable_sessions: tuple[str, ...]
    unknown_notes: tuple[str, ...]


def _pool_rights(packet: Packet, period: str) -> tuple[str, Fraction, dict[str, Right]]:
    rights = {studio: right for (per, studio), right in packet.rights.items() if per == period}
    keys = {right.pool_key for right in rights.values()}
    if len(keys) != 1:
        raise ValueError(f"{period}: 合同池不唯一 {keys}")
    rates = {right.pool_rate for right in rights.values()}
    if len(rates) != 1:
        raise ValueError(f"{period}: 同池 pool_rate 不一致 {rates}")
    return next(iter(keys)), _rate(next(iter(rates))), rights


def _rate(value: float) -> Fraction:
    return Fraction(str(value))


def _allocate_pool(net_increments: dict[str, Fraction], pool: Fraction,
                   caps: dict[str, int]) -> dict[str, Fraction]:
    """三道约束下的奖金分派：本方非负净增量、单方上限、共同池（按比例缩减）。"""
    raw = {studio: max(Fraction(0), inc) for studio, inc in net_increments.items()}
    capped = {studio: min(raw[studio], Fraction(caps[studio])) for studio in raw}
    total_capped = sum(capped.values(), Fraction(0))
    if total_capped <= pool:
        return capped
    if total_capped == 0:
        return {studio: Fraction(0) for studio in raw}
    return {studio: capped[studio] * pool / total_capped for studio in raw}


def _historical(packet: Packet, period: str, cutoff: int,
                 assignment: dict[str, tuple[str, str]],
                 ambiguous_tenders: set[str] | None = None) -> dict[str, dict[str, int]]:
    """按合作方汇总历史出账（仅 cutoff 前已发生），归属按当前可行事实重述。"""
    ambiguous_tenders = ambiguous_tenders or set()
    buckets = {studio: {"guarantee_paid": 0, "bonus_paid": 0,
                        "bonus_reserved": 0, "custodial_held": 0}
               for studio in period_studios(packet, period)}
    for payment in packet.bonus_payments:
        if payment.period != period or payment.day > cutoff:
            continue
        if payment.studio not in buckets:
            raise ValueError(f"{payment.payment}: 未知合作方 {payment.studio}")
        if payment.kind == "guarantee":
            buckets[payment.studio]["guarantee_paid"] += payment.amount
        elif payment.kind == "bonus":
            buckets[payment.studio]["bonus_paid"] += payment.amount
        elif payment.kind == "reserve":
            buckets[payment.studio]["bonus_reserved"] += payment.amount
        else:
            raise ValueError(f"{payment.payment}: 未知付款类型 {payment.kind}")
    # 凭证级托管/关账款：按当前确认归属登记；未确定的托管单列 __unassigned__
    for paid in packet.paid:
        if paid.period != period or paid.paid_at > cutoff:
            continue
        if paid.state == "reserve":
            target = assignment.get(paid.tender)
            if target is None or paid.tender in ambiguous_tenders:
                buckets.setdefault("__unassigned__",
                                   {"guarantee_paid": 0, "bonus_paid": 0,
                                    "bonus_reserved": 0, "custodial_held": 0})
                buckets["__unassigned__"]["custodial_held"] += paid.got
            else:
                studio = paid.studio or target[1]
                buckets.setdefault(studio,
                                   {"guarantee_paid": 0, "bonus_paid": 0,
                                    "bonus_reserved": 0, "custodial_held": 0})
                buckets[studio]["custodial_held"] += paid.got
    return buckets


def _attribution_flows(packet: Packet, period: str, cutoff: int,
                       assignment: dict[str, tuple[str, str]],
                       ambiguous_tenders: set[str]) -> dict[str, dict[str, int]]:
    """凭证级关账/托管款按当前确认归属重述，分出补付与应收两类。

    返回每个合作方：
    - attribution_payable：托管款经确认归本方、尚未现金支付，应在新单调账补付；
    - receivable_carry：本方已现金签收、但后到确认改归他方的金额，列本方应收/争议。
    未确定的托管款进 ``__unassigned__``，不进任何一方。
    """
    flows: dict[str, dict[str, int]] = {}
    for paid in packet.paid:
        if paid.period != period or paid.paid_at > cutoff:
            continue
        target = assignment.get(paid.tender)
        determined = paid.tender not in ambiguous_tenders
        if paid.state == "reserve":
            if target is None or not determined:
                # 凭证归属未定（多场/多方竞争）：托管款不得挂到任一候选方
                flows.setdefault("__unassigned__",
                                 {"attribution_payable": 0, "receivable_carry": 0,
                                  "unassigned_held": 0})
                flows["__unassigned__"]["unassigned_held"] += paid.got
            else:
                studio = target[1]
                flows.setdefault(studio,
                                 {"attribution_payable": 0, "receivable_carry": 0,
                                  "unassigned_held": 0})
                flows[studio]["attribution_payable"] += paid.got
        elif paid.state == "closed":
            if (target is not None and determined and paid.studio is not None
                    and paid.studio != target[1]):
                flows.setdefault(paid.studio,
                                 {"attribution_payable": 0, "receivable_carry": 0,
                                  "unassigned_held": 0})
                flows[paid.studio]["receivable_carry"] += paid.got
                flows.setdefault(target[1],
                                 {"attribution_payable": 0, "receivable_carry": 0,
                                  "unassigned_held": 0})
                flows[target[1]]["attribution_payable"] += paid.got
    return flows


def settle_period(snapshot: Snapshot, basis: str,
                  scenarios: list[ScenarioResult]) -> Settlement:
    packet = snapshot.packet
    period = snapshot.period
    cutoff = snapshot.cutoff
    pool_key, pool_rate, rights = _pool_rights(packet, period)
    studios_all = period_studios(packet, period)

    # 历史出账按“各场景都成立”的稳健口径汇总：托管归属逐场景可能不同，
    # 可签托管只取各场景最小值；应收同理取稳健交集。
    ambiguous = {tender for tender, state in snapshot.tender_states().items()
                 if not state.determined}
    history_by_scenario = [
        _historical(packet, period, cutoff, result.assignment, ambiguous)
        for result in scenarios]
    flows_by_scenario = [
        _attribution_flows(packet, period, cutoff, result.assignment, ambiguous)
        for result in scenarios]

    def robust(key: str, studio: str, sub: str) -> int:
        values = []
        for buckets in key:
            row = buckets.get(studio)
            values.append(row[sub] if row else 0)
        return min(values)

    pool_amounts: list[int] = []
    per_scenario_bonus: list[dict[str, int]] = []
    joint_nets: list[Fraction] = []
    for result in scenarios:
        pool_exact = max(Fraction(0), result.joint_net_increment) * pool_rate
        pool_amounts.append(round_half_up(pool_exact))
        joint_nets.append(result.joint_net_increment)
        net_by_studio = {row.studio: row.net_increment for row in result.studios}
        caps = {studio: rights[studio].max_bonus for studio in studios_all}
        allocated = _allocate_pool(net_by_studio, pool_exact, caps)
        per_scenario_bonus.append(
            {studio: round_half_up(allocated.get(studio, Fraction(0)))
             for studio in studios_all})

    unidentified_set: set[str] = set()
    for result in scenarios:
        unidentified_set.update(result.unidentifiable_sessions)

    # 上期结转由 settle_carry 显式注入（同一合作方的待抵扣，禁止跨方冲抵）；
    # 单期快照默认 carry_in=0。
    carry_in = {studio: 0 for studio in studios_all}

    studio_rows: list[StudioSettlement] = []
    total_signable = 0
    total_guarantee = 0
    total_bonus = 0
    total_reserved = 0
    total_carry = 0
    total_attribution_payable = 0
    for studio in studios_all:
        right = rights[studio]
        candidates = tuple(per[studio] for per in per_scenario_bonus)
        signable = min(candidates)
        pending = len(set(candidates)) > 1 or any(c > signable for c in candidates)

        guarantee_paid = robust(history_by_scenario, studio, "guarantee_paid")
        bonus_paid = robust(history_by_scenario, studio, "bonus_paid")
        bonus_reserved = robust(history_by_scenario, studio, "bonus_reserved")
        custodial_held = robust(history_by_scenario, studio, "custodial_held")
        attribution_payable = robust(flows_by_scenario, studio, "attribution_payable")
        receivable_carry = robust(flows_by_scenario, studio, "receivable_carry")

        guarantee_cash = max(0, right.guaranteed - guarantee_paid)
        unidentified_sessions = {sid for result in scenarios
                                 for sid in result.unidentifiable_sessions}
        studio_show_ids = {sid for result in scenarios
                           for row in result.studios if row.studio == studio
                           for sid in row.show_sessions}
        unidentified = bool(studio_show_ids & unidentified_sessions)

        # 已发奖金超出当前证据可支撑部分：本方差额待抵扣，禁止跨方冲抵
        bonus_overpaid = max(0, bonus_paid - signable)
        carry_out = carry_in[studio] + bonus_overpaid + receivable_carry

        signable_lines: list[dict] = []
        pending_lines: list[dict] = []
        if guarantee_cash:
            signable_lines.append({
                "studio": studio, "kind": "guarantee",
                "amount": guarantee_cash,
                "basis": f"保底 {right.guaranteed} − 已签保底 {guarantee_paid}；"
                         "保底为独立条款，不因对照不足消失",
            })
        if signable:
            signable_lines.append({
                "studio": studio, "kind": "bonus", "amount": signable,
                "basis": f"各可行场景同时可兑现的奖金（候选 {list(candidates)}）",
            })
        if attribution_payable:
            signable_lines.append({
                "studio": studio, "kind": "attribution_payable",
                "amount": attribution_payable,
                "basis": "凭证确认归属后的托管/关账补付，历史出账不回改",
            })
        if pending:
            pending_lines.append({
                "studio": studio, "kind": "bonus",
                "amount_undecided": max(candidates) - signable,
                "basis": "归属或对照摇摆，留置未决，不静默释放",
            })
        if bonus_reserved:
            pending_lines.append({
                "studio": studio, "kind": "reserve_kept",
                "amount": bonus_reserved,
                "basis": "已预留金额仍未取得奖金依据，继续留置",
            })
        if custodial_held and not attribution_payable:
            pending_lines.append({
                "studio": studio, "kind": "custodial_pending",
                "amount": custodial_held,
                "basis": "托管款归属仍摇摆，确认前不进可签金额",
            })
        if unidentified:
            pending_lines.append({
                "studio": studio, "kind": "unidentifiable",
                "basis": "无同类对照窗口，不产生奖金依据；保底独立有效",
            })
        if bonus_overpaid:
            pending_lines.append({
                "studio": studio, "kind": "bonus_overpaid",
                "amount": bonus_overpaid,
                "basis": "历史奖金超发，作本方差额待抵扣，不得用他方争议款冲抵",
            })
        if receivable_carry:
            pending_lines.append({
                "studio": studio, "kind": "receivable_carry",
                "amount": receivable_carry,
                "basis": "已付款被后到确认改归他方，列本方应收/争议",
            })
        if carry_out:
            pending_lines.append({
                "studio": studio, "kind": "carry",
                "amount": carry_out,
                "basis": "本方待抵扣总额，仅限抵扣本方未来应付款",
            })

        total_signable += guarantee_cash + signable + attribution_payable
        total_guarantee += guarantee_cash
        total_bonus += signable
        total_attribution_payable += attribution_payable
        total_reserved += bonus_reserved
        total_carry += carry_out
        studio_rows.append(StudioSettlement(
            studio=studio, guaranteed=right.guaranteed, max_bonus=right.max_bonus,
            bonus_candidates=candidates, bonus_signable=signable, bonus_pending=pending,
            unidentifiable=unidentified,
            guarantee_paid=guarantee_paid, bonus_paid=bonus_paid,
            bonus_reserved=bonus_reserved, custodial_held=custodial_held,
            guarantee_cash_payable=guarantee_cash,
            attribution_payable=attribution_payable,
            bonus_overpaid=bonus_overpaid, receivable_carry=receivable_carry,
            carry_in=carry_in[studio], carry_out=carry_out,
            signable_lines=tuple(signable_lines), pending_lines=tuple(pending_lines)))

    notes: list[str] = []
    for flows in flows_by_scenario:
        unassigned = flows.get("__unassigned__")
        if unassigned and unassigned.get("unassigned_held"):
            notes.append(
                f"存在未归属托管款 {unassigned['unassigned_held']} 分，"
                "凭证确认前不进任何一方可签金额")
            break
    notes.append(f"奖金池按合办增量非负部分 × {float(pool_rate):g} 计入")
    pool = PoolSettlement(
        pool_key=pool_key, pool_rate=pool_rate,
        pool_amounts=tuple(pool_amounts), joint_net_increments=tuple(joint_nets),
        pool_signable=sum(row.bonus_signable for row in studio_rows),
        notes=tuple(notes))

    return Settlement(
        period=period, cutoff=cutoff, basis=basis,
        scenarios=tuple(scenarios), studios=tuple(studio_rows), pool=pool,
        total_signable=total_signable, total_guarantee_payable=total_guarantee,
        total_bonus_signable=total_bonus, total_reserved_kept=total_reserved,
        total_carry_out=total_carry,
        unidentifiable_sessions=tuple(sorted(unidentified_set)),
        unknown_notes=tuple(snapshot.packet.unknown_marks))


def settle_carry(settlement: Settlement, prior_settlement: Settlement | None,
                 packet: Packet) -> Settlement:
    """把上期结算确认的本方超发注入本期 carry_in（同方抵扣，不跨合作方）。"""
    if prior_settlement is None:
        return settlement
    prior_by_studio = {row.studio: row for row in prior_settlement.studios}
    new_rows: list[StudioSettlement] = []
    for row in settlement.studios:
        prior = prior_by_studio.get(row.studio)
        injected = prior.carry_out if prior is not None else 0
        if injected == 0 and row.carry_in == 0:
            new_rows.append(row)
            continue
        carry_in = row.carry_in + injected
        carry_out = carry_in + row.bonus_overpaid + row.receivable_carry
        lines = list(row.pending_lines)
        if injected:
            lines.append({
                "studio": row.studio, "kind": "carry_from_prior",
                "amount": injected,
                "basis": f"承接 {prior_settlement.period} 本方待抵扣，不与其他方争议款互抵",
            })
        new_rows.append(StudioSettlement(
            studio=row.studio, guaranteed=row.guaranteed, max_bonus=row.max_bonus,
            bonus_candidates=row.bonus_candidates, bonus_signable=row.bonus_signable,
            bonus_pending=row.bonus_pending, unidentifiable=row.unidentifiable,
            guarantee_paid=row.guarantee_paid, bonus_paid=row.bonus_paid,
            bonus_reserved=row.bonus_reserved, custodial_held=row.custodial_held,
            guarantee_cash_payable=row.guarantee_cash_payable,
            attribution_payable=row.attribution_payable,
            bonus_overpaid=row.bonus_overpaid, receivable_carry=row.receivable_carry,
            carry_in=carry_in, carry_out=carry_out,
            signable_lines=row.signable_lines, pending_lines=tuple(lines)))
    total_carry = sum(row.carry_out for row in new_rows)
    return Settlement(
        period=settlement.period, cutoff=settlement.cutoff, basis=settlement.basis,
        scenarios=settlement.scenarios, studios=tuple(new_rows), pool=settlement.pool,
        total_signable=settlement.total_signable,
        total_guarantee_payable=settlement.total_guarantee_payable,
        total_bonus_signable=settlement.total_bonus_signable,
        total_reserved_kept=settlement.total_reserved_kept,
        total_carry_out=total_carry,
        unidentifiable_sessions=settlement.unidentifiable_sessions,
        unknown_notes=settlement.unknown_notes)


@dataclass(frozen=True)
class AdjustmentLine:
    code: str
    studio: str | None
    kind: str                 # attribution / control / bonus / guarantee / reserve / carry
    amount: int | None        # 对新一张可签收调账单的影响（分）；None 表示仅披露
    direction: str            # payable / deductible / disclose
    evidence: str
    explanation: str


@dataclass(frozen=True)
class AdjustmentBill:
    period: str
    prior_cutoff: int
    new_cutoff: int
    basis: str
    lines: tuple[AdjustmentLine, ...]
    formal_refs: tuple[str, ...]             # 可回查的正式单（付款/关账/预留编号）
    control_changes: tuple[dict, ...]


def build_adjustment(prior: Settlement, new: Settlement,
                     prior_snapshot: Snapshot, new_snapshot: Snapshot) -> AdjustmentBill:
    """相邻截至日之间生成调账单：历史出账不动，只描述对新一张可签收单的影响。"""
    lines: list[AdjustmentLine] = []
    packet = new_snapshot.packet

    # 1) 对照资格变动
    prior_controls = {s.session for s in prior_snapshot.eligible_controls()}
    new_controls = {s.session for s in new_snapshot.eligible_controls()}
    control_changes: list[dict] = []
    for lost in sorted(prior_controls - new_controls):
        control_changes.append({"session": lost, "change": "lost",
                                "reasons": new_snapshot.eligibility_reasons().get(lost, [])})
    for gained in sorted(new_controls - prior_controls):
        control_changes.append({"session": gained, "change": "gained",
                                "reasons": new_snapshot.eligibility_reasons().get(gained, [])})

    # 2) 凭证确认/归属翻转（amend 在新区间内到达）
    prior_amends = {a.amend for a in prior_snapshot.available_amends()}
    for amend in new_snapshot.available_amends():
        if amend.amend in prior_amends:
            continue
        lines.append(AdjustmentLine(
            code=amend.amend, studio=amend.studio, kind="attribution",
            amount=None, direction="disclose", evidence=amend.evidence,
            explanation=(f"凭证 {amend.tender} 后到确认归 {amend.session}/{amend.studio}；"
                         "原历史出账不回改，按新归属另开调账")))
        # 原收款方的已付现金变成本方应收（待抵扣/争议），不跨方冲抵
        for paid in packet.paid:
            if paid.tender != amend.tender or paid.state != "closed":
                continue
            if paid.studio and paid.studio != amend.studio:
                lines.append(AdjustmentLine(
                    code=paid.closure, studio=paid.studio, kind="carry",
                    amount=paid.got, direction="deductible",
                    evidence=f"closure {paid.closure} / {amend.amend}",
                    explanation=(f"原由 {paid.studio} 签收的 {paid.got} 分经 {amend.amend} "
                                 f"确认应归 {amend.studio}，列为 {paid.studio} 本方待抵扣/争议，"
                                 "不得抵其他方款项")))
                lines.append(AdjustmentLine(
                    code=paid.closure, studio=amend.studio, kind="attribution_payable",
                    amount=paid.got, direction="payable",
                    evidence=f"closure {paid.closure} / {amend.amend}",
                    explanation=f"确认归属后对 {amend.studio} 补付，资金来源不依赖原收款方退款"))

    # 3) 托管款状态变化：未归属 → 确认归属后可对该方签付（历史托管记录不回改）
    prior_ambiguous = {tender for tender, state in prior_snapshot.tender_states().items()
                       if not state.determined}
    new_ambiguous = {tender for tender, state in new_snapshot.tender_states().items()
                     if not state.determined}
    prior_flows = [_attribution_flows(packet, new.period, prior.cutoff, pick, prior_ambiguous)
                   for pick in prior_snapshot.scenarios()]
    new_flows = [_attribution_flows(packet, new.period, new.cutoff, pick, new_ambiguous)
                 for pick in new_snapshot.scenarios()]

    def _flow(flow_list: list[dict], studio: str, key: str) -> int:
        return min(buckets.get(studio, {}).get(key, 0) for buckets in flow_list)

    def _unassigned(flow_list: list[dict]) -> int:
        return sum(buckets.get("__unassigned__", {}).get("unassigned_held", 0)
                   for buckets in flow_list) // max(len(flow_list), 1)

    if _unassigned(prior_flows) and not _unassigned(new_flows):
        for row in new.studios:
            released = _flow(new_flows, row.studio, "attribution_payable")
            before = _flow(prior_flows, row.studio, "attribution_payable")
            if released > before:
                closures = sorted({paid.closure for paid in packet.paid
                                   if paid.period == new.period and paid.state == "reserve"})
                lines.append(AdjustmentLine(
                    code=",".join(closures) or "K?", studio=row.studio,
                    kind="custody_release",
                    amount=released - before, direction="payable",
                    evidence="paid.reserve / amend",
                    explanation=("托管凭证经后到确认归本方，原托管记录不回改，"
                                 "在新单调账为可签收补付")))

    # 4) 可签奖金/保底变化（对照或归属变动的传导）
    prior_by_studio = {row.studio: row for row in prior.studios}
    for row in new.studios:
        old = prior_by_studio.get(row.studio)
        if old is None:
            continue
        if row.bonus_signable != old.bonus_signable:
            delta = row.bonus_signable - old.bonus_signable
            lines.append(AdjustmentLine(
                code=f"BONUS-{row.studio}", studio=row.studio, kind="bonus",
                amount=delta,
                direction="payable" if delta > 0 else "deductible",
                evidence="; ".join(c["session"] for c in control_changes) or "amend",
                explanation=(f"可签奖金由 {old.bonus_signable} 变为 {row.bonus_signable}；"
                             "减少部分作本方差额，不静默释放留置款")))
        if row.guarantee_cash_payable != old.guarantee_cash_payable:
            lines.append(AdjustmentLine(
                code=f"GUAR-{row.studio}", studio=row.studio, kind="guarantee",
                amount=row.guarantee_cash_payable - old.guarantee_cash_payable,
                direction="payable", evidence="rights",
                explanation="保底为独立条款，按已签保底滚动计算"))

    # 5) 仍留置的预留（显式披露，不释放）
    for row in new.studios:
        if row.bonus_reserved:
            lines.append(AdjustmentLine(
                code=f"RES-{row.studio}", studio=row.studio, kind="reserve",
                amount=row.bonus_reserved, direction="disclose",
                evidence="bonus_payments.reserve",
                explanation="预留仍无奖金依据，继续留置，未决金额不得静默释放"))

    formal_refs = tuple(sorted(
        {p.payment for p in packet.bonus_payments if p.period == new.period}
        | {p.closure for p in packet.paid if p.period == new.period}
        | {a.amend for a in new_snapshot.available_amends()}))

    return AdjustmentBill(
        period=new.period, prior_cutoff=prior.cutoff, new_cutoff=new.cutoff,
        basis=new.basis, lines=tuple(lines), formal_refs=formal_refs,
        control_changes=tuple(control_changes))
