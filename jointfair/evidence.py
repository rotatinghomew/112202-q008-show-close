"""截至日证据快照、共享团确认、对照窗口资格与凭证可行归属。

口径要点
--------
- 所有事实都带收到/观测日；快照只接受 ``day <= cutoff`` 的资料，后到证据不回溯改写旧快照。
- 共享团（曝光）确认级别：
  1. ``exposure_corrections`` 的后到确认（引用 amend 编号）级别最高；
  2. 其次，联票在候选场次 ``admitted=yes`` 的真实入场（``transfer`` 不算确认）；
  3. 都不满足则未确认，候选场次都不能当作正式到场。
- 预热链 ``preheat_link`` 只有在其曝光被确认、且确认场次恰为 ``show`` 时才生效，
  生效后对应 ``control`` 窗口失去对照资格——确认翻转时相邻窗口要重新判定。
- 同一 tender 的多条凭证构成可行经办事实：后到 amend 唯一确定归属；否则枚举全部
  候选 (场次, 合作方) 组合。退款（reversal）随其 tender 同一归属，不另立分派。
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import product

from .records import Packet, Session


@dataclass(frozen=True)
class ExposureState:
    exposure: str
    confirmed: bool
    confirmed_session: str | None
    basis: str            # correction / ticket / none
    known: bool           # 曝光事件本身是否已收到


@dataclass(frozen=True)
class TenderState:
    tender: str
    candidates: tuple[tuple[str, str], ...]   # (session, studio)
    determined: bool
    amend: str | None


@dataclass(frozen=True)
class Snapshot:
    packet: Packet
    period: str
    cutoff: int

    # ---- 基本过滤 ----
    def session_period(self, session_id: str) -> str:
        return self.packet.sessions[session_id].period

    def available_claims(self) -> list:
        return [c for c in self.packet.claims
                if self.session_period(c.session) == self.period and c.observed_day <= self.cutoff]

    def available_tickets(self) -> list:
        return [t for t in self.packet.tickets
                if self.session_period(t.session) == self.period and t.day <= self.cutoff]

    def available_amends(self) -> list:
        result = []
        for amend in self.packet.amends:
            if self.session_period(amend.session) == self.period and amend.received <= self.cutoff:
                result.append(amend)
        return result

    def available_corrections(self) -> list:
        return [c for c in self.packet.corrections
                if self.packet.exposures[c.exposure].period == self.period
                and c.received <= self.cutoff]

    def available_exposures(self) -> list:
        return [e for e in self.packet.exposures.values()
                if e.period == self.period and e.received <= self.cutoff]

    # ---- 共享团确认 ----
    def exposure_states(self) -> dict[str, ExposureState]:
        states: dict[str, ExposureState] = {}
        tickets = self.available_tickets()
        corrections = {c.exposure: c for c in self.available_corrections()}
        for exposure in self.available_exposures():
            known = True
            if exposure.exposure in corrections:
                correction = corrections[exposure.exposure]
                states[exposure.exposure] = ExposureState(
                    exposure.exposure, True, correction.confirmed_session, "correction", known)
                continue
            # 联票真实入场：holder 为团号，在候选场次 admitted=yes
            hit = next((t for t in tickets
                        if t.holder == exposure.group and t.admitted == "yes"
                        and t.session in exposure.candidates), None)
            if hit is not None:
                states[exposure.exposure] = ExposureState(
                    exposure.exposure, True, hit.session, f"ticket:{hit.evidence}", known)
            else:
                states[exposure.exposure] = ExposureState(
                    exposure.exposure, False, None, "none", known)
        return states

    def confirmed_exposures(self) -> dict[str, str]:
        """曝光号 -> 唯一确认场次。"""
        return {key: state.confirmed_session for key, state in self.exposure_states().items()
                if state.confirmed}

    # ---- 对照窗口资格 ----
    def active_preheat_controls(self) -> set[str]:
        """因预热链生效而失去资格的对照窗口集合。"""
        confirmed = self.confirmed_exposures()
        blocked: set[str] = set()
        for link in self.packet.preheat_links:
            if self.session_period(link.show) != self.period:
                continue
            if confirmed.get(link.exposure) == link.show:
                blocked.add(link.control)
        return blocked

    def eligible_controls(self) -> list[Session]:
        """终局合格的普通营业窗口。

        条件全部满足：ordinary、观测完整、当时标注可作通常营业（unaffected_base=yes）、
        未被生效预热链污染。标注为联合/预热关联（companion 非空且 unaffected_base=no）
        的窗口不加区别地排除。
        """
        blocked = self.active_preheat_controls()
        result: list[Session] = []
        for session in self.packet.sessions_of_period(self.period):
            control = self.packet.controls.get(session.session)
            if control is None:
                continue
            if session.format != "ordinary":
                continue
            if session.observed != "full":
                continue
            if control.unaffected_base != "yes":
                continue
            if session.session in blocked:
                continue
            result.append(session)
        return result

    def eligibility_reasons(self) -> dict[str, list[str]]:
        """每个 ordinary 窗口的资格判定理由，供报告披露口径。"""
        blocked = self.active_preheat_controls()
        reasons: dict[str, list[str]] = {}
        for session in self.packet.sessions_of_period(self.period):
            if session.format != "ordinary":
                continue
            control = self.packet.controls[session.session]
            notes: list[str] = []
            if session.observed != "full":
                notes.append(f"观测不完整({session.observed})，不能作完整对照")
            if control.unaffected_base != "yes":
                notes.append("标注为联合/预热关联或不可作通常营业窗口")
            if session.session in blocked:
                link = next(link for link in self.packet.preheat_links
                            if link.control == session.session
                            and self.confirmed_exposures().get(link.exposure) == link.show)
                notes.append(f"被预热链 {link.exposure} 污染（确认场次 {link.show}）")
            if not notes:
                notes.append(
                    f"合格：同类档 {control.match_class}，面积 {control.open_area}，"
                    f"普通营收 {control.ordinary_revenue}")
            reasons[session.session] = notes
        return reasons

    # ---- 凭证可行归属 ----
    def tender_states(self) -> dict[str, TenderState]:
        amends = {a.tender: a for a in self.available_amends()}
        grouped: dict[str, set[tuple[str, str]]] = {}
        for claim in self.available_claims():
            if claim.kind not in ("payment", "reported"):
                continue
            if claim.studio is None:
                continue
            grouped.setdefault(claim.tender, set()).add((claim.session, claim.studio))

        result: dict[str, TenderState] = {}
        for tender, candidates in sorted(grouped.items()):
            if tender in amends:
                amend = amends[tender]
                chosen = (amend.session, amend.studio)
                if chosen not in candidates:
                    # amend 可以改变事实：新归属即便没有旧凭证行也成立，但要登记
                    pass
                result[tender] = TenderState(tender, (chosen,), True, amend.amend)
            else:
                ordered = tuple(sorted(candidates))
                result[tender] = TenderState(
                    tender, ordered, len(ordered) == 1, None)
        return result

    def scenarios(self) -> list[dict[str, tuple[str, str]]]:
        """枚举当期所有可行归属组合；确定项固定，摇摆项做笛卡尔积。"""
        states = self.tender_states()
        ambiguous = [t for t in states.values() if not t.determined]
        if not ambiguous:
            return [{t.tender: t.candidates[0] for t in states.values()}]
        combo: list[dict[str, tuple[str, str]]] = []
        for picks in product(*(t.candidates for t in ambiguous)):
            assignment = {t.tender: t.candidates[0] for t in states.values()
                          if t.determined}
            for state, pick in zip(ambiguous, picks):
                assignment[state.tender] = pick
            combo.append(assignment)
        return combo

    def tender_net(self, assignment: dict[str, tuple[str, str]]) -> dict[str, int]:
        """按某一可行归属计算每个 tender 的净额（含 reversal 退款）。

        退款随其 tender 的同一归属：reversal 行本身带 (session, studio)，
        必须与该场景选定归属一致，否则属经办事实冲突，直接报错而不是静默丢弃。
        """
        nets: dict[str, int] = {}
        for claim in self.available_claims():
            target = assignment.get(claim.tender)
            if target is None:
                continue
            if claim.kind == "reversal":
                if (claim.session, claim.studio) != target:
                    raise ValueError(
                        f"退款 {claim.proof} 与凭证 {claim.tender} 归属冲突，"
                        "不能在同一组经办事实内分派")
                nets[claim.tender] = nets.get(claim.tender, 0) + claim.amount
            elif (claim.session, claim.studio) == target:
                nets[claim.tender] = nets.get(claim.tender, 0) + claim.amount
        return nets


def show_sessions(packet: Packet, period: str) -> list[Session]:
    return [s for s in packet.sessions_of_period(period) if s.format == "show"]


def period_studios(packet: Packet, period: str) -> list[str]:
    return sorted(studio for (per, studio) in packet.rights if per == period)
