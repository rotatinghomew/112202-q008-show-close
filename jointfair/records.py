"""TSV 资料装载与口径解析。

资料约定（见 README）：
- UTF-8、Tab 分隔、首行列名、逗号分隔集合；
- ``-`` 表示不适用，``?`` 表示未取得，二者都不能当作 0；
- 金额为整数分；标识符只在各表声明的归属范围内有效。
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

NA = "-"          # 不适用
MISSING = "?"     # 未取得（不是零）


def is_na(value: str) -> bool:
    return value == NA


def is_missing(value: str) -> bool:
    return value == MISSING


def parse_int(value: str) -> int | None:
    """整数列；``-``/``?`` 返回 None，调用方须区分不适用与未取得。"""
    if value in (NA, MISSING):
        return None
    return int(value)


def parse_set(value: str) -> tuple[str, ...]:
    """逗号分隔集合；``-`` 表示空集合（不适用），``?`` 向上抛未取得。"""
    if value == NA:
        return ()
    if value == MISSING:
        raise ValueError("未取得的集合列不能被当作空集合")
    return tuple(part.strip() for part in value.split(",") if part.strip())


def parse_weights(value: str) -> dict[str, int]:
    """``A:2,B:1`` 形式的分担权重。"""
    if value == NA:
        return {}
    weights: dict[str, int] = {}
    for part in value.split(","):
        key, _, weight = part.strip().partition(":")
        if not key or not weight:
            raise ValueError(f"分担权重格式错误: {value!r}")
        weights[key] = int(weight)
    return weights


@dataclass(frozen=True)
class Session:
    session: str
    period: str
    hall: str
    band: str
    format: str          # show / ordinary
    traffic: int
    observed: str        # full / partial


@dataclass(frozen=True)
class Claim:
    proof: str
    tender: str
    ticket: str | None
    session: str
    studio: str | None       # 归属未定时为 None（paid.tsv 里的托管行）
    kind: str                # payment / reversal / reported
    amount: int
    observed_day: int


@dataclass(frozen=True)
class Ticket:
    ticket: str
    session: str
    holder: str
    admitted: str            # yes / no / transfer
    day: int
    evidence: str


@dataclass(frozen=True)
class SharedCost:
    period: str
    service: str
    amount: int
    studios: tuple[str, ...]
    allocation: dict[str, int]
    unit: str                # direct / shared


@dataclass(frozen=True)
class Paid:
    closure: str
    period: str
    tender: str
    studio: str | None
    got: int
    paid_at: int
    state: str               # closed / reserve


@dataclass(frozen=True)
class Amend:
    amend: str
    tender: str
    session: str
    studio: str
    received: int
    evidence: str


@dataclass(frozen=True)
class Exposure:
    exposure: str
    period: str
    group: str
    candidates: tuple[str, ...]
    origin: str | None
    may_preheat: str | None
    source: str
    received: int


@dataclass(frozen=True)
class ExposureCorrection:
    evidence: str
    exposure: str
    confirmed_session: str
    tender: str | None
    received: int


@dataclass(frozen=True)
class Control:
    session: str
    companion: str | None
    open_area: int
    unaffected_base: str     # yes / no，仅为当时标注，不是终局资格
    match_class: str
    ordinary_revenue: int | None  # None 即 ``-`` 或 ``?``，调用方区分


@dataclass(frozen=True)
class Right:
    period: str
    studio: str
    guaranteed: int
    max_bonus: int
    pool_key: str
    pool_rate: float
    carry: str | None        # 可抵扣的下一周期，``-`` 表示无


@dataclass(frozen=True)
class BonusPayment:
    payment: str
    period: str
    studio: str
    day: int
    amount: int
    kind: str                # guarantee / bonus / reserve
    pool_key: str


@dataclass(frozen=True)
class PreheatLink:
    exposure: str
    show: str
    control: str
    starts_before: int


@dataclass
class Packet:
    sessions: dict[str, Session]
    claims: list[Claim]
    tickets: list[Ticket]
    shared: list[SharedCost]
    paid: list[Paid]
    amends: list[Amend]
    exposures: dict[str, Exposure]
    corrections: list[ExposureCorrection]
    controls: dict[str, Control]
    rights: dict[tuple[str, str], Right]
    bonus_payments: list[BonusPayment]
    preheat_links: list[PreheatLink]
    folder: Path | None = None
    unknown_marks: list[str] = field(default_factory=list)

    def sessions_of_period(self, period: str) -> list[Session]:
        return [row for row in self.sessions.values() if row.period == period]

    def claims_of_period(self, period: str) -> list[Claim]:
        periods = {row.session: row.period for row in self.sessions.values()}
        return [row for row in self.claims if periods.get(row.session) == period]


def _read_table(folder: Path, name: str, fields: list[str]) -> list[dict[str, str]]:
    with (folder / name).open(newline="", encoding="utf-8") as source:
        reader = csv.reader(source, delimiter="\t", strict=True)
        header = next(reader, None)
        if header != fields:
            raise ValueError(f"{name}: 表头不符，得到 {header}")
        rows: list[dict[str, str]] = []
        for line, values in enumerate(reader, start=2):
            if len(values) != len(fields) or any(not value.strip() for value in values):
                raise ValueError(f"{name}:{line}: 列数不符或有未标记的空值")
            rows.append(dict(zip(fields, values)))
        return rows


def load_packet(folder: str | Path) -> Packet:
    folder = Path(folder)
    with (folder / "layout.json").open(encoding="utf-8") as source:
        layout: dict[str, list[str]] = json.load(source)

    tables = {name: _read_table(folder, name, fields) for name, fields in layout.items()}
    unknown: list[str] = []

    sessions = {
        row["session"]: Session(
            session=row["session"], period=row["period"], hall=row["hall"],
            band=row["band"], format=row["format"],
            traffic=int(row["traffic"]), observed=row["observed"],
        )
        for row in tables["sessions.tsv"]
    }

    claims: list[Claim] = []
    for row in tables["claims.tsv"]:
        claims.append(Claim(
            proof=row["proof"], tender=row["tender"],
            ticket=None if is_na(row["ticket"]) else row["ticket"],
            session=row["session"],
            studio=None if is_na(row["studio"]) else row["studio"],
            kind=row["kind"], amount=int(row["amount"]),
            observed_day=int(row["observed"]),
        ))

    tickets = [
        Ticket(ticket=row["ticket"], session=row["session"], holder=row["holder"],
               admitted=row["admitted"], day=int(row["day"]), evidence=row["evidence"])
        for row in tables["tickets.tsv"]
    ]

    shared: list[SharedCost] = []
    for row in tables["shared.tsv"]:
        cost = SharedCost(
            period=row["period"], service=row["service"], amount=int(row["amount"]),
            studios=parse_set(row["studios"]), allocation=parse_weights(row["allocation"]),
            unit=row["unit"],
        )
        if set(cost.allocation) != set(cost.studios):
            raise ValueError(f"shared.tsv:{cost.service}: 分担方与受益方不一致")
        shared.append(cost)

    paid: list[Paid] = []
    for row in tables["paid.tsv"]:
        if is_missing(row["studio"]):
            unknown.append(f"paid.tsv:{row['closure']}: studio 未取得")
        paid.append(Paid(
            closure=row["closure"], period=row["period"], tender=row["tender"],
            studio=None if is_na(row["studio"]) or is_missing(row["studio"]) else row["studio"],
            got=int(row["got"]), paid_at=int(row["paid_at"]), state=row["state"],
        ))

    amends = [
        Amend(amend=row["amend"], tender=row["tender"], session=row["session"],
              studio=row["studio"], received=int(row["received"]), evidence=row["evidence"])
        for row in tables["amend.tsv"]
    ]

    exposures = {}
    for row in tables["exposure.tsv"]:
        exp = Exposure(
            exposure=row["exposure"], period=row["period"], group=row["group"],
            candidates=parse_set(row["candidates"]),
            origin=None if is_na(row["origin"]) else row["origin"],
            may_preheat=None if is_na(row["may_preheat"]) else row["may_preheat"],
            source=row["source"], received=int(row["received"]),
        )
        if not exp.candidates:
            raise ValueError(f"exposure.tsv:{exp.exposure}: 候选场次为空")
        exposures[exp.exposure] = exp

    corrections = [
        ExposureCorrection(
            evidence=row["evidence"], exposure=row["exposure"],
            confirmed_session=row["confirmed_session"],
            tender=None if is_na(row["tender"]) else row["tender"],
            received=int(row["received"]),
        )
        for row in tables["exposure_corrections.tsv"]
    ]

    controls: dict[str, Control] = {}
    for row in tables["controls.tsv"]:
        rev_raw = row["ordinary_revenue"]
        if is_missing(rev_raw):
            unknown.append(f"controls.tsv:{row['session']}: ordinary_revenue 未取得")
        controls[row["session"]] = Control(
            session=row["session"],
            companion=None if is_na(row["companion"]) else row["companion"],
            open_area=int(row["open_area"]),
            unaffected_base=row["unaffected_base"],
            match_class=row["match_class"],
            ordinary_revenue=parse_int(rev_raw),
        )

    rights = {}
    for row in tables["rights.tsv"]:
        right = Right(
            period=row["period"], studio=row["studio"],
            guaranteed=int(row["guaranteed"]), max_bonus=int(row["max_bonus"]),
            pool_key=row["pool_key"], pool_rate=float(row["pool_rate"]),
            carry=None if is_na(row["carry"]) else row["carry"],
        )
        rights[(right.period, right.studio)] = right

    bonus_payments = [
        BonusPayment(payment=row["payment"], period=row["period"], studio=row["studio"],
                     day=int(row["day"]), amount=int(row["amount"]), kind=row["kind"],
                     pool_key=row["pool_key"])
        for row in tables["bonus_payments.tsv"]
    ]

    preheat_links = [
        PreheatLink(exposure=row["exposure"], show=row["show"], control=row["control"],
                    starts_before=int(row["starts_before"]))
        for row in tables["preheat_link.tsv"]
    ]

    packet = Packet(
        sessions=sessions, claims=claims, tickets=tickets, shared=shared, paid=paid,
        amends=amends, exposures=exposures, corrections=corrections, controls=controls,
        rights=rights, bonus_payments=bonus_payments, preheat_links=preheat_links,
        folder=folder, unknown_marks=unknown,
    )
    _validate_references(packet)
    return packet


def _validate_references(packet: Packet) -> None:
    """外键与基础一致性校验；标识符只在各表声明范围内有效，越界即拒。"""
    for claim in packet.claims:
        if claim.session not in packet.sessions:
            raise ValueError(f"claims.tsv:{claim.proof}: 未知场次 {claim.session}")
        if claim.ticket is not None and not any(t.ticket == claim.ticket for t in packet.tickets):
            raise ValueError(f"claims.tsv:{claim.proof}: 未知联票 {claim.ticket}")
    for ticket in packet.tickets:
        if ticket.session not in packet.sessions:
            raise ValueError(f"tickets.tsv:{ticket.ticket}: 未知场次 {ticket.session}")
    for cost in packet.shared:
        for studio in cost.studios:
            if (cost.period, studio) not in packet.rights:
                raise ValueError(f"shared.tsv:{cost.service}: 未知合作方 {studio}")
    for paid in packet.paid:
        if not any(c.tender == paid.tender for c in packet.claims):
            raise ValueError(f"paid.tsv:{paid.closure}: 未知凭证 {paid.tender}")
    for amend in packet.amends:
        if amend.session not in packet.sessions:
            raise ValueError(f"amend.tsv:{amend.amend}: 未知场次 {amend.session}")
    for correction in packet.corrections:
        if correction.exposure not in packet.exposures:
            raise ValueError(f"exposure_corrections.tsv:{correction.evidence}: 未知曝光 {correction.exposure}")
        exp = packet.exposures[correction.exposure]
        if correction.confirmed_session not in exp.candidates:
            raise ValueError(
                f"exposure_corrections.tsv:{correction.evidence}: 确认场次不在候选内")
    for link in packet.preheat_links:
        if link.exposure not in packet.exposures:
            raise ValueError(f"preheat_link.tsv: 未知曝光 {link.exposure}")
        if link.show not in packet.sessions or link.control not in packet.sessions:
            raise ValueError(f"preheat_link.tsv:{link.exposure}: 未知场次")
    for session_id, control in packet.controls.items():
        if session_id not in packet.sessions:
            raise ValueError(f"controls.tsv: 未知场次 {session_id}")
    # 同一 (tender, session, studio, kind) 不允许出现重复凭证行；
    # 同一 tender 的竞争归属（不同 session/studio）由证据层枚举处理，不在此拒入
    seen: dict[tuple[str, str, str | None, str], int] = {}
    for claim in packet.claims:
        key = (claim.tender, claim.session, claim.studio, claim.kind)
        if key in seen:
            raise ValueError(f"claims.tsv:{claim.proof}: 同一凭证同归属出现重复 {key}")
        seen[key] = claim.amount
