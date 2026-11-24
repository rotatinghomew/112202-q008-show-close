"""营业对照、反事实基准与新增净收益。

规模口径
========
- ``area``：以 ``controls.open_area`` 为规模，反事实 = 展售窗口面积 × 单位面积普通营收；
- ``traffic``：以 ``sessions.traffic`` 为规模，反事实 = 展售窗口客流 × 单位客流普通营收。

对照必须同类：基准按 ``controls.match_class`` 分组建率，展售窗口只与同档窗口比较。
基准率用「合格对照营收合计 ÷ 合格对照规模合计」的合并率，而不是各窗口比率的简单平均，
避免小面积窗口放大权重。两种口径各自出账，切换查询维度（场次/合作方）不改变同范围总额。

不可识别
========
所属档没有任何合格同类对照的展售窗口（如 S6 处于 peak-B 档），增量标记 unidentifiable：
不计入可发奖金的增量，也不静默当零；其实收单列，保底条款独立于该判定。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .evidence import Snapshot, period_studios, show_sessions
from .records import Packet, Session

BASIS_AREA = "area"
BASIS_TRAFFIC = "traffic"
BASIS_CHOICES = (BASIS_AREA, BASIS_TRAFFIC)


@dataclass(frozen=True)
class Benchmark:
    basis: str
    match_class: str | None                 # None 表示无同类对照
    control_sessions: tuple[str, ...]
    pooled_revenue: int | None
    pooled_scale: int | None
    rate: Fraction | None
    note: str

    @property
    def identifiable(self) -> bool:
        return self.rate is not None


@dataclass(frozen=True)
class ShowResult:
    session: str
    match_class: str
    studios: tuple[str, ...]
    actual_net: int
    counterfactual: Fraction | None
    increment: Fraction | None
    identifiable: bool
    note: str


@dataclass(frozen=True)
class StudioIncrement:
    studio: str
    actual_net: int
    counterfactual: Fraction
    increment: Fraction
    direct_cost: int
    net_increment: Fraction
    unidentifiable_actual: int
    show_sessions: tuple[str, ...]


@dataclass(frozen=True)
class ScenarioResult:
    basis: str
    assignment: dict[str, tuple[str, str]]
    benchmarks: dict[str, Benchmark]        # match_class -> 基准
    shows: tuple[ShowResult, ...]
    studios: tuple[StudioIncrement, ...]
    joint_actual: int
    joint_counterfactual: Fraction
    joint_increment: Fraction
    joint_direct_cost: int
    joint_net_increment: Fraction
    unidentifiable_sessions: tuple[str, ...]
    unidentifiable_actual: int

    @property
    def benchmark(self) -> Benchmark | None:
        """单基准兼容入口：数据存在多个同类档时返回 None，调用方应使用 benchmarks。"""
        identified = [b for b in self.benchmarks.values() if b.identifiable]
        return identified[0] if len(identified) == 1 else None

    def by_session_table(self) -> list[dict]:
        return [
            {
                "session": show.session,
                "match_class": show.match_class,
                "studios": list(show.studios),
                "actual_net": show.actual_net,
                "counterfactual": show.counterfactual,
                "increment": show.increment,
                "identifiable": show.identifiable,
                "note": show.note,
            }
            for show in self.shows
        ]

    def by_studio_table(self) -> list[dict]:
        return [
            {
                "studio": row.studio,
                "actual_net": row.actual_net,
                "counterfactual": row.counterfactual,
                "increment": row.increment,
                "direct_cost": row.direct_cost,
                "net_increment": row.net_increment,
                "unidentifiable_actual": row.unidentifiable_actual,
                "show_sessions": list(row.show_sessions),
            }
            for row in self.studios
        ]


def build_benchmarks(snapshot: Snapshot, basis: str) -> dict[str, Benchmark]:
    """按 match_class 分组建率；某档无合格窗口时保留不可识别基准占位。"""
    if basis not in BASIS_CHOICES:
        raise ValueError(f"未知规模口径 {basis}")
    packet = snapshot.packet
    eligible = set(snapshot.eligible_controls())
    classes = sorted({packet.controls[s.session].match_class
                      for s in packet.sessions_of_period(snapshot.period)
                      if s.session in packet.controls})
    benchmarks: dict[str, Benchmark] = {}
    for match_class in classes:
        controls = [s for s in eligible
                    if packet.controls[s.session].match_class == match_class
                    and packet.controls[s.session].ordinary_revenue is not None]
        if not controls:
            benchmarks[match_class] = Benchmark(
                basis, match_class, (), None, None, None,
                f"档 {match_class} 无合格同类对照窗口，反事实不可识别")
            continue
        revenue = sum(packet.controls[s.session].ordinary_revenue for s in controls)
        if basis == BASIS_AREA:
            scale = sum(packet.controls[s.session].open_area for s in controls)
            note = f"档 {match_class} 单位面积普通营收，对照 {','.join(s.session for s in controls)}"
        else:
            scale = sum(s.traffic for s in controls)
            note = f"档 {match_class} 单位客流普通营收，对照 {','.join(s.session for s in controls)}"
        benchmarks[match_class] = Benchmark(
            basis, match_class, tuple(s.session for s in controls),
            revenue, scale, Fraction(revenue, scale), note)
    return benchmarks


def build_benchmark(snapshot: Snapshot, basis: str) -> Benchmark:
    """兼容旧入口：仅当全期只有一个同类档时有效。"""
    benchmarks = build_benchmarks(snapshot, basis)
    if len(benchmarks) == 1:
        return next(iter(benchmarks.values()))
    raise ValueError("存在多个 match_class，应使用 build_benchmarks 分组取率")


def _show_scale(session: Session, packet: Packet, basis: str) -> int:
    if basis == BASIS_AREA:
        return packet.controls[session.session].open_area
    return session.traffic


def _direct_costs(packet: Packet, period: str) -> dict[str, int]:
    """边际开支（unit=direct）按会计约定权重分担；shared 费用不等同边际，不入增量。"""
    out: dict[str, int] = {}
    for cost in packet.shared:
        if cost.period != period or cost.unit != "direct":
            continue
        weight_sum = sum(cost.allocation.values())
        for studio, weight in cost.allocation.items():
            share = Fraction(cost.amount * weight, weight_sum)
            if share.denominator != 1:
                raise ValueError(
                    f"边际开支 {cost.service} 对 {studio} 的分担出现非整数分，需先约定取整")
            out[studio] = out.get(studio, 0) + int(share)
    return out


def compute_scenario(snapshot: Snapshot, basis: str,
                     assignment: dict[str, tuple[str, str]]) -> ScenarioResult:
    packet = snapshot.packet
    period = snapshot.period
    benchmarks = build_benchmarks(snapshot, basis)

    tender_rows: dict[str, list] = {}
    for claim in snapshot.available_claims():
        if claim.tender in assignment:
            tender_rows.setdefault(claim.tender, []).append(claim)

    # 场次 -> studio -> 净额；退款随同一 tender 归属
    session_studio_actual: dict[str, dict[str, int]] = {}
    for tender, target in assignment.items():
        session_id, studio = target
        for claim in tender_rows.get(tender, []):
            if claim.kind == "reversal":
                if (claim.session, claim.studio) != target:
                    raise ValueError(f"退款 {claim.proof} 与确认归属冲突")
            elif (claim.session, claim.studio) != target:
                continue
            session_studio_actual.setdefault(session_id, {}).setdefault(studio, 0)
            session_studio_actual[session_id][studio] += claim.amount

    shows: list[ShowResult] = []
    unidentifiable: list[str] = []
    unidentifiable_actual = 0
    studios_all = period_studios(packet, period)
    studio_actual: dict[str, int] = {s: 0 for s in studios_all}
    studio_unidentified: dict[str, int] = {s: 0 for s in studios_all}
    studio_shows: dict[str, list[str]] = {s: [] for s in studios_all}
    joint_counterfactual = Fraction(0)

    for session in show_sessions(packet, period):
        match_class = packet.controls[session.session].match_class
        benchmark = benchmarks[match_class]
        per_studio = session_studio_actual.get(session.session, {})
        actual = sum(per_studio.values())
        for studio, value in per_studio.items():
            studio_actual[studio] = studio_actual.get(studio, 0) + value
            studio_shows.setdefault(studio, []).append(session.session)
        scale = _show_scale(session, packet, basis)
        if benchmark.identifiable:
            counterfactual = benchmark.rate * scale
            increment = Fraction(actual) - counterfactual
            note = f"反事实 = {benchmark.note} × 规模 {scale}"
            identifiable = True
            joint_counterfactual += counterfactual
        else:
            counterfactual = None
            increment = None
            note = "所属档无同类对照窗口，增量不可识别，不提供奖金依据"
            identifiable = False
            unidentifiable.append(session.session)
            unidentifiable_actual += actual
            for studio, value in per_studio.items():
                studio_unidentified[studio] = studio_unidentified.get(studio, 0) + value
        shows.append(ShowResult(
            session.session, match_class, tuple(sorted(per_studio)), actual,
            counterfactual, increment, identifiable, note))

    # 反事实按合作方拆分：按可识别窗口的实收占比，全为零/负时在参与方间均分
    identifiable_actual = {s: studio_actual[s] - studio_unidentified[s] for s in studios_all}
    active = [s for s in studios_all
              if any(sid not in unidentifiable for sid in studio_shows[s])]
    positive_total = sum(max(0, identifiable_actual[s]) for s in active)
    studio_counterfactual: dict[str, Fraction] = {s: Fraction(0) for s in studios_all}
    if active and joint_counterfactual > 0:
        if positive_total > 0:
            weights = {s: Fraction(max(0, identifiable_actual[s]), positive_total) for s in active}
        else:
            weights = {s: Fraction(1, len(active)) for s in active}
        for studio in active:
            studio_counterfactual[studio] = joint_counterfactual * weights[studio]

    direct = _direct_costs(packet, period)
    studio_rows: list[StudioIncrement] = []
    for studio in studios_all:
        cf = studio_counterfactual[studio]
        inc = Fraction(identifiable_actual[studio]) - cf
        cost = direct.get(studio, 0)
        studio_rows.append(StudioIncrement(
            studio=studio,
            actual_net=studio_actual[studio],
            counterfactual=cf,
            increment=inc,
            direct_cost=cost,
            net_increment=inc - cost,
            unidentifiable_actual=studio_unidentified[studio],
            show_sessions=tuple(studio_shows[studio]),
        ))

    joint_actual = sum(studio_actual.values())
    joint_increment = Fraction(joint_actual - unidentifiable_actual) - joint_counterfactual
    joint_direct = sum(direct.values())
    joint_net = joint_increment - joint_direct

    # 视图对平校验：场次视图与合作方视图的同范围总额必须一致
    view_actual = sum(show.actual_net for show in shows)
    view_cf = sum((show.counterfactual for show in shows if show.counterfactual is not None),
                  Fraction(0))
    if view_actual != joint_actual or view_cf != joint_counterfactual:
        raise AssertionError("按场次与按合作方视图未能对平")

    return ScenarioResult(
        basis=basis, assignment=assignment, benchmarks=benchmarks,
        shows=tuple(shows), studios=tuple(studio_rows),
        joint_actual=joint_actual, joint_counterfactual=joint_counterfactual,
        joint_increment=joint_increment, joint_direct_cost=joint_direct,
        joint_net_increment=joint_net,
        unidentifiable_sessions=tuple(unidentifiable),
        unidentifiable_actual=unidentifiable_actual,
    )


def all_scenarios(snapshot: Snapshot, basis: str) -> list[ScenarioResult]:
    return [compute_scenario(snapshot, basis, pick) for pick in snapshot.scenarios()]
