"""联展结算引擎的规则测试（基于 data/ 合成业务包）。

覆盖：
- 资料装载口径（``-`` 不当零、外键、权重一致性）
- 截至日证据：共享团确认、预热污染、对照资格随确认翻转
- 凭证可行归属与退款随附
- 反事实按同类档分组、面积/客流两口径、场次视图与合作方视图对平
- 保底独立有效、奖金池非负、三道约束、摇摆取稳健交集、无对照不发奖
- 托管未归属不进可签、确认后释放；历史不改、本方结转不跨方冲抵
- 调账单能回查正式单并反映对照/归属变动
"""
from __future__ import annotations

import unittest
from fractions import Fraction
from pathlib import Path

from jointfair.evidence import Snapshot
from jointfair.increment import (BASIS_AREA, BASIS_TRAFFIC, all_scenarios,
                                 build_benchmarks, compute_scenario)
from jointfair.records import load_packet
from jointfair.settle import (_attribution_flows, build_adjustment,
                              round_half_up, settle_carry, settle_period)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.packet = load_packet(DATA)

    def test_dash_and_question_marks_are_not_zero(self):
        # claims 里的 ticket=- 解析为 None，而不是字符串或零
        no_ticket = [c for c in self.packet.claims if c.proof == "R3"]
        self.assertEqual(no_ticket[0].ticket, None)
        # controls 的普通营收缺项为 None
        self.assertIsNone(self.packet.controls["S1"].ordinary_revenue)
        self.assertEqual(self.packet.controls["S3"].ordinary_revenue, 8900)

    def test_weights_match_beneficiaries(self):
        for cost in self.packet.shared:
            self.assertEqual(set(cost.allocation), set(cost.studios))

    def test_reversal_keeps_integer_cents(self):
        reversal = next(c for c in self.packet.claims if c.kind == "reversal")
        self.assertEqual(reversal.amount, -1100)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.packet = load_packet(DATA)

    def test_exposure_confirmation_levels(self):
        # d13：EX1 已由 L1 在 S1 的真实入场确认到 S1（AM1 d15 才改判到 S2）；
        # EX3 由 L3 真实入场确认到 S2
        snap13 = Snapshot(self.packet, "M1", 13)
        states = snap13.exposure_states()
        self.assertTrue(states["EX1"].confirmed)
        self.assertEqual(states["EX1"].confirmed_session, "S1")
        self.assertEqual(states["EX3"].confirmed_session, "S2")
        self.assertTrue(states["EX3"].basis.startswith("ticket"))
        # d15：AM1 把 EX1 从 S1 改判确认到 S2
        snap15 = Snapshot(self.packet, "M1", 15)
        self.assertEqual(snap15.confirmed_exposures()["EX1"], "S2")

    def test_transfer_ticket_does_not_confirm(self):
        # L1 在 S2 是 transfer，不能凭它把 EX1 确认到 S2；AM1 前 EX1 只能确认在 S1
        snap = Snapshot(self.packet, "M1", 13)
        self.assertEqual(snap.confirmed_exposures()["EX1"], "S1")
        snap15 = Snapshot(self.packet, "M1", 15)
        self.assertEqual(snap15.confirmed_exposures()["EX1"], "S2")

    def test_control_eligibility_flips_with_confirmation(self):
        snap13 = Snapshot(self.packet, "M1", 13)
        eligible13 = {s.session for s in snap13.eligible_controls()}
        # S4 被已确认的 EX3 预热污染；S3 此时仍合格；S5 合格
        self.assertEqual(eligible13, {"S3", "S5"})
        snap15 = Snapshot(self.packet, "M1", 15)
        eligible15 = {s.session for s in snap15.eligible_controls()}
        # AM1 确认 EX1→S2 后，S3 被预热链剔除
        self.assertEqual(eligible15, {"S5"})

    def test_partial_observation_and_companion_excluded(self):
        snap = Snapshot(self.packet, "M2", 19)
        eligible = {s.session for s in snap.eligible_controls()}
        # S8 partial 观测不合格；S9 被 EX5 预热污染；S10 合格
        self.assertEqual(eligible, {"S10"})

    def test_tender_attribution_scenarios(self):
        snap13 = Snapshot(self.packet, "M1", 13)
        states = snap13.tender_states()
        self.assertFalse(states["X12"].determined)
        self.assertEqual(set(states["X12"].candidates), {("S1", "A"), ("S2", "B")})
        snap15 = Snapshot(self.packet, "M1", 15)
        self.assertTrue(snap15.tender_states()["X12"].determined)
        self.assertEqual(snap15.tender_states()["X12"].candidates, (("S2", "B"),))

    def test_reversal_follows_same_tender_attribution(self):
        # R8 退款观测日为 16，取 d16 快照；X16 = 6100 payment - 1100 reversal，同归 S2/B
        snap = Snapshot(self.packet, "M1", 16)
        assignment = snap.scenarios()[0]
        nets = snap.tender_net(assignment)
        self.assertEqual(nets["X16"], 5000)

    def test_reversal_not_available_before_its_observed_day(self):
        snap = Snapshot(self.packet, "M1", 15)
        nets = snap.tender_net(snap.scenarios()[0])
        self.assertEqual(nets["X16"], 6100)


class IncrementTests(unittest.TestCase):
    def setUp(self):
        self.packet = load_packet(DATA)

    def test_benchmark_grouped_by_match_class(self):
        snap = Snapshot(self.packet, "M2", 19)
        bench = build_benchmarks(snap, BASIS_AREA)
        self.assertFalse(bench["peak-B"].identifiable)       # S6 无同类对照
        self.assertTrue(bench["wk-C"].identifiable)
        self.assertEqual(bench["wk-C"].control_sessions, ("S10",))
        self.assertEqual(bench["wk-C"].rate, Fraction(8100, 20))

    def test_unidentifiable_window_excluded_from_bonus_but_kept_in_actual(self):
        snap = Snapshot(self.packet, "M2", 23)
        result = compute_scenario(snap, BASIS_AREA, snap.scenarios()[0])
        self.assertEqual(result.unidentifiable_sessions, ("S6",))
        self.assertEqual(result.unidentifiable_actual, 9000)
        self.assertIn(9000, [s.actual_net for s in result.studios if s.studio == "C"])
        s6 = next(show for show in result.shows if show.session == "S6")
        self.assertIsNone(s6.increment)

    def test_view_invariance_session_vs_studio(self):
        for period, cutoff in (("M1", 13), ("M1", 15), ("M2", 19), ("M2", 23)):
            snap = Snapshot(self.packet, period, cutoff)
            for basis in (BASIS_AREA, BASIS_TRAFFIC):
                for result in all_scenarios(snap, basis):
                    session_actual = sum(s.actual_net for s in result.shows)
                    studio_actual = sum(s.actual_net for s in result.studios)
                    self.assertEqual(session_actual, studio_actual)
                    session_cf = sum((s.counterfactual for s in result.shows
                                      if s.counterfactual is not None), Fraction(0))
                    studio_cf = sum((s.counterfactual for s in result.studios), Fraction(0))
                    self.assertEqual(session_cf, studio_cf)

    def test_hand_computed_m1_area_numbers(self):
        snap13 = Snapshot(self.packet, "M1", 13)
        result = compute_scenario(snap13, BASIS_AREA, snap13.scenarios()[0])
        # 合并率 (8900+9500)/(30+27) = 18400/57；两个展售窗口各 28 面积
        self.assertEqual(result.benchmarks["wk-A"].rate, Fraction(18400, 57))
        self.assertEqual(result.joint_actual, 15100)
        self.assertEqual(round_half_up(result.joint_counterfactual), 18077)
        self.assertEqual(round_half_up(result.joint_net_increment), -4377)

        snap15 = Snapshot(self.packet, "M1", 15)
        result15 = compute_scenario(snap15, BASIS_AREA, snap15.scenarios()[0])
        self.assertEqual(result15.benchmarks["wk-A"].control_sessions, ("S5",))
        self.assertEqual(round_half_up(result15.joint_counterfactual), 19704)

    def test_direct_vs_shared_cost_treatment(self):
        # M1 direct extra 1400 均摊 A/B 各 700；shared guide/booth 不进边际开支
        snap = Snapshot(self.packet, "M1", 15)
        result = compute_scenario(snap, BASIS_AREA, snap.scenarios()[0])
        for row in result.studios:
            self.assertEqual(row.direct_cost, 700)
        self.assertEqual(result.joint_direct_cost, 1400)

    def test_basis_switch_keeps_scope_totals_consistent_within_basis(self):
        snap = Snapshot(self.packet, "M2", 23)
        area = compute_scenario(snap, BASIS_AREA, snap.scenarios()[0])
        traffic = compute_scenario(snap, BASIS_TRAFFIC, snap.scenarios()[0])
        # 同一业务范围的实收总额不随口径/维度改变
        self.assertEqual(area.joint_actual, traffic.joint_actual)
        self.assertEqual(area.unidentifiable_actual, traffic.unidentifiable_actual)


class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.packet = load_packet(DATA)

    def _settle(self, period, cutoff, basis=BASIS_AREA):
        snap = Snapshot(self.packet, period, cutoff)
        return snap, settle_period(snap, basis, all_scenarios(snap, basis))

    def test_guarantee_survives_missing_counterfactual(self):
        _, m2 = self._settle("M2", 23)
        by_studio = {row.studio: row for row in m2.studios}
        # C 的 S6 无对照、无奖金，但 1600 保底独立有效且未签过，应全额可付
        self.assertTrue(by_studio["C"].unidentifiable)
        self.assertEqual(by_studio["C"].guarantee_cash_payable, 1600)
        self.assertEqual(by_studio["C"].bonus_signable, 0)

    def test_pool_is_non_negative_and_bonus_zero_when_joint_increment_negative(self):
        for period, cutoff in (("M1", 13), ("M1", 15), ("M2", 19), ("M2", 23)):
            _, settlement = self._settle(period, cutoff)
            self.assertTrue(all(amount >= 0 for amount in settlement.pool.pool_amounts))
            self.assertEqual(settlement.total_bonus_signable, 0)

    def test_robust_intersection_holds_ambiguous_bonus(self):
        # 构造一个合办增量为正的场景，验证摇摆时只发各场景最小值
        # 这里直接校验数据本身：d13 X12 摇摆，两场景奖金候选一致取稳健值
        _, m1 = self._settle("M1", 13)
        for row in m1.studios:
            self.assertEqual(row.bonus_signable, min(row.bonus_candidates))

    def test_overpaid_bonus_becomes_same_studio_carry(self):
        _, m1 = self._settle("M1", 13)
        a = next(row for row in m1.studios if row.studio == "A")
        # BP3 已发 1900，当前证据支撑 0 → A 本方待抵扣 1900
        self.assertEqual(a.bonus_overpaid, 1900)
        self.assertEqual(a.carry_out, 1900)

    def test_reserve_is_kept_not_silently_released(self):
        _, m1 = self._settle("M1", 15)
        b = next(row for row in m1.studios if row.studio == "B")
        self.assertEqual(b.bonus_reserved, 3800)
        self.assertEqual(m1.total_reserved_kept, 3800)
        self.assertNotIn(3800, [line.get("amount") for line in b.signable_lines])

    def test_attribution_flip_creates_receivable_and_payable(self):
        snap15 = Snapshot(self.packet, "M1", 15)
        flows = _attribution_flows(
            self.packet, "M1", 15, snap15.scenarios()[0], set())
        # X12 原由 A 签收 3500，AM1 改归 B
        self.assertEqual(flows["A"]["receivable_carry"], 3500)
        self.assertEqual(flows["B"]["attribution_payable"], 3500)
        _, m1 = self._settle("M1", 15)
        a = next(row for row in m1.studios if row.studio == "A")
        b = next(row for row in m1.studios if row.studio == "B")
        self.assertEqual(a.carry_out, 1900 + 3500)      # 奖金超发 + 归属应收
        self.assertEqual(b.attribution_payable, 3500)

    def test_unassigned_custody_excluded_then_released(self):
        snap19, m19 = self._settle("M2", 19)
        # d19 X20 在 A/B 摇摆，4500 托管不得进任何一方可签
        self.assertEqual(sum(row.attribution_payable for row in m19.studios), 0)
        self.assertTrue(any("未归属托管款" in note for note in m19.pool.notes))

        snap23, m23 = self._settle("M2", 23)
        b = next(row for row in m23.studios if row.studio == "B")
        self.assertEqual(b.attribution_payable, 4500)
        self.assertEqual(b.custodial_held, 4500)

    def test_carry_injection_never_crosses_studios(self):
        _, m1 = self._settle("M1", 15)
        snap23 = Snapshot(self.packet, "M2", 23)
        m2 = settle_period(snap23, BASIS_AREA, all_scenarios(snap23, BASIS_AREA))
        m2c = settle_carry(m2, m1, self.packet)
        injected = {row.studio: row.carry_in for row in m2c.studios}
        self.assertEqual(injected, {"A": 5400, "B": 0, "C": 0})

    def test_pool_allocation_caps_and_shared_constraint(self):
        from jointfair.settle import _allocate_pool
        caps = {"A": 100, "B": 100}
        # 共同池 100，但各方非负增量合计候选 300 → 按比例缩减，总和不超池
        alloc = _allocate_pool({"A": Fraction(200), "B": Fraction(100)},
                               Fraction(100), caps)
        self.assertEqual(sum(alloc.values(), Fraction(0)), Fraction(100))
        # 单方上限生效
        alloc_capped = _allocate_pool({"A": Fraction(500), "B": Fraction(0)},
                                      Fraction(1000), caps)
        self.assertEqual(alloc_capped["A"], Fraction(100))


class AdjustmentTests(unittest.TestCase):
    def setUp(self):
        self.packet = load_packet(DATA)

    def _chain(self, period, cutoffs):
        previous = None
        previous_snap = None
        bills = []
        for cutoff in cutoffs:
            snap = Snapshot(self.packet, period, cutoff)
            settlement = settle_period(snap, BASIS_AREA,
                                       all_scenarios(snap, BASIS_AREA))
            if previous is not None:
                bills.append(build_adjustment(previous, settlement,
                                              previous_snap, snap))
            previous, previous_snap = settlement, snap
        return bills

    def test_m1_bill_records_control_loss_and_attribution(self):
        bill = self._chain("M1", (13, 15))[0]
        lost = [c["session"] for c in bill.control_changes if c["change"] == "lost"]
        self.assertEqual(lost, ["S3"])
        kinds = {(line.studio, line.kind): line.amount for line in bill.lines}
        self.assertEqual(kinds[("A", "carry")], 3500)
        self.assertEqual(kinds[("B", "attribution_payable")], 3500)
        # 必须能回查正式单
        self.assertIn("K1", bill.formal_refs)
        self.assertIn("AM1", bill.formal_refs)

    def test_m2_bill_releases_custody_after_amend(self):
        bill = self._chain("M2", (19, 23))[0]
        release = [line for line in bill.lines if line.kind == "custody_release"]
        self.assertEqual(len(release), 1)
        self.assertEqual(release[0].studio, "B")
        self.assertEqual(release[0].amount, 4500)
        self.assertIn("K2", bill.formal_refs)

    def test_history_not_rewritten(self):
        # 调账单只描述新单影响，不修改任何 paid/bonus_payments 原始金额
        before = [(p.closure, p.got) for p in self.packet.paid]
        self._chain("M1", (13, 15))
        self.assertEqual(before, [(p.closure, p.got) for p in self.packet.paid])


class RoundingTests(unittest.TestCase):
    def test_half_up(self):
        self.assertEqual(round_half_up(Fraction(1, 2)), 1)
        self.assertEqual(round_half_up(Fraction(3, 2)), 2)
        self.assertEqual(round_half_up(Fraction(1, 3)), 0)
        self.assertEqual(round_half_up(Fraction(-1, 2)), 0)


if __name__ == "__main__":
    unittest.main()
