"""GOLDEN REGRESSION GATE.

Runs a deterministic paper session (entry -> tranche-1 -> trail exit -> report)
and byte-compares outputs against committed golden files. Any silent change to
trading logic, money arithmetic, decision serialization, or report rendering
breaks this test — which is the point.

Regenerate (ONLY on an intentional, reviewed logic change):
    .venv/bin/python -m tests.test_golden --regen
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

from qts_core.broker import PaperBroker
from qts_core.clock import NY
from qts_core.config import StrategyConfig
from qts_core.paper import PaperSession
from qts_core.report import render_session_report
from qts_core.store import StateStore
from tests.test_checklist import OPEN, SESSION, make_view

GOLDEN_DIR = Path(__file__).parent / "golden"
CFG = StrategyConfig(commission_per_contract_cents=0)  # report-arithmetic mode
FLAT_AT = dt.datetime(2026, 6, 17, 15, 30, tzinfo=NY)


def _mark_view(mark_cents: int, hh: int, mm: int):  # type: ignore[no-untyped-def]
    import dataclasses as dc

    now = dt.datetime(2026, 6, 17, hh, mm, tzinfo=NY)
    view = make_view(now)
    q = view.chain[0]
    return dc.replace(
        view,
        chain=(
            dc.replace(q, bid_cents=mark_cents - 1, ask_cents=mark_cents + 1, received_at=now),
            *view.chain[1:],
        ),
    )


def run_golden_session(db_path: Path) -> tuple[str, str]:
    """The canonical scripted session. Returns (report_md, ledger_json)."""
    store = StateStore(db_path)
    session = PaperSession(
        store,
        PaperBroker(CFG, CFG.tick_schedule_for("NVDA")),
        CFG,
        session_date=SESSION,
        session_open_et=OPEN,
        force_flat_at=FLAT_AT,
    )
    r1 = session.step(make_view())  # 10:15 — approved entry
    assert r1.position is not None, "golden scenario must enter"
    entry_quote = r1.position.entry_quote_cents
    target = r1.position.tranche1_level(CFG)
    session.step(_mark_view(target + 5, 10, 45))  # tranche-1 -> RUNNER
    pos = session.any_open_position()
    assert pos is not None
    trail = pos.trail_level(CFG)
    session.step(_mark_view(trail - 10, 11, 5))  # trail exit -> CLOSED

    report = render_session_report(store, CFG, SESSION)
    ledger = [
        {
            "seq": o["seq"],
            "side": o["side"],
            "reason": o["reason"],
            "contracts": o["contracts"],
            "limit_cents": o["limit_cents"],
            "fill_premium_cents": o["fill_premium_cents"],
            "cash_or_realized_cents": o["fill_cost_cents"],
            "status": o["status"],
            "client_order_id": o["client_order_id"],
        }
        for o in store.orders_for_session(SESSION)
    ]
    decisions = [
        {"symbol": s, "approved": bool(a), "decision": json.loads(blob)}
        for _, s, a, blob in store.decisions_for_session(SESSION)
    ]
    payload = json.dumps(
        {"entry_quote_cents": entry_quote, "orders": ledger, "decisions": decisions},
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    store.close()
    return report, payload


class TestGolden:
    def test_report_byte_identical(self, tmp_path: Path) -> None:
        report, _ = run_golden_session(tmp_path / "g.db")
        golden = (GOLDEN_DIR / "session_report.md").read_text()
        assert report == golden, (
            "Golden report drifted. If the change is INTENTIONAL and reviewed, "
            "regenerate: .venv/bin/python -m tests.test_golden --regen"
        )

    def test_ledger_byte_identical(self, tmp_path: Path) -> None:
        _, ledger = run_golden_session(tmp_path / "g.db")
        golden = (GOLDEN_DIR / "session_ledger.json").read_text()
        assert ledger == golden

    def test_two_runs_are_byte_identical(self, tmp_path: Path) -> None:
        # Determinism proof independent of the committed files.
        r1, l1 = run_golden_session(tmp_path / "a.db")
        r2, l2 = run_golden_session(tmp_path / "b.db")
        assert r1 == r2
        assert l1 == l2


if __name__ == "__main__":
    if "--regen" in sys.argv:
        import tempfile

        GOLDEN_DIR.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory() as td:
            report, ledger = run_golden_session(Path(td) / "g.db")
        (GOLDEN_DIR / "session_report.md").write_text(report)
        (GOLDEN_DIR / "session_ledger.json").write_text(ledger)
        print(f"regenerated golden files in {GOLDEN_DIR}")
    else:
        print("use --regen to regenerate golden files (intentional changes only)")
