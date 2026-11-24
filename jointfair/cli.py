"""命令行入口：按周期/截至日生成结算快照、调账单与机读/人读输出。

用法：
    python3 -m jointfair.cli run --data data --out out --cutoffs M1=13,15 M2=19,23
    python3 -m jointfair.cli snapshot --data data --period M1 --cutoff 15
"""
from __future__ import annotations

import argparse
from pathlib import Path

from .evidence import Snapshot
from .increment import BASIS_CHOICES, all_scenarios
from .records import load_packet
from .report import adjustment_to_dict, render_markdown, settlement_to_dict, write_json
from .settle import build_adjustment, settle_carry, settle_period

DEFAULT_CUTOFFS = {"M1": (13, 15), "M2": (19, 23)}


def _periods(packet) -> list[str]:
    return sorted({session.period for session in packet.sessions.values()})


def run_chain(packet, cutoffs: dict[str, tuple[int, ...]], out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    index: dict = {"periods": {}}
    prior_final: dict[str, dict[str, object]] = {}

    for period in _periods(packet):
        days = cutoffs.get(period, ())
        period_block: dict = {"cutoffs": []}
        previous: dict[str, object] | None = None
        previous_snapshot = None
        period_final: dict[str, object] = {}
        prior_periods = [name for name in prior_final if name != period]
        prior_settlements = prior_final[prior_periods[-1]] if prior_periods else None
        for day in days:
            snapshot = Snapshot(packet=packet, period=period, cutoff=day)
            basis_block: dict = {}
            for basis in BASIS_CHOICES:
                scenarios = all_scenarios(snapshot, basis)
                settlement = settle_period(snapshot, basis, scenarios)
                # 上一周期期末的本方待抵扣注入本期（同方抵扣，不跨合作方）
                if prior_settlements is not None:
                    settlement = settle_carry(
                        settlement, prior_settlements[basis], packet)
                bill = None
                if previous is not None:
                    bill = build_adjustment(
                        previous[basis], settlement, previous_snapshot, snapshot)
                basis_block[basis] = (settlement, snapshot, bill)

                stem = f"{period}_d{day}_{basis}"
                write_json(out_dir / f"{stem}_settlement.json",
                           settlement_to_dict(settlement))
                (out_dir / f"{stem}_report.md").write_text(
                    render_markdown(settlement, snapshot, bill), encoding="utf-8")
                if bill is not None:
                    write_json(out_dir / f"{stem}_adjustment.json",
                               adjustment_to_dict(bill))
            period_block["cutoffs"].append(
                {"day": day, "bases": list(BASIS_CHOICES)})
            previous = {basis: value[0] for basis, value in basis_block.items()}
            previous_snapshot = snapshot
        if previous is not None:
            period_final = previous
            prior_final[period] = period_final
        index["periods"][period] = period_block

    write_json(out_dir / "index.json", index)
    return index


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="联展经营结算引擎")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="按多个截至日跑完整周期链")
    run.add_argument("--data", type=Path, default=Path("data"))
    run.add_argument("--out", type=Path, default=Path("out"))
    run.add_argument("--cutoffs", nargs="*", default=[],
                     help="形如 M1=13,15 M2=19,23")

    snap = sub.add_parser("snapshot", help="单一截至日快照")
    snap.add_argument("--data", type=Path, default=Path("data"))
    snap.add_argument("--period", required=True)
    snap.add_argument("--cutoff", type=int, required=True)
    snap.add_argument("--out", type=Path, default=Path("out"))

    args = parser.parse_args(argv)
    packet = load_packet(args.data)

    if args.command == "snapshot":
        cutoffs = {args.period: (args.cutoff,)}
        run_chain(packet, cutoffs, args.out)
        return

    cutoffs = dict(DEFAULT_CUTOFFS)
    for item in args.cutoffs:
        key, _, values = item.partition("=")
        cutoffs[key] = tuple(int(v) for v in values.split(","))
    run_chain(packet, cutoffs, args.out)


if __name__ == "__main__":
    main()
