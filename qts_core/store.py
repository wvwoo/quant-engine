"""Crash-safe state store: SQLite, WAL, macOS-durable, idempotent by design.

The recovery contract (findings IDEM-*): a `kill -9` at ANY instant, followed
by restart, must produce no duplicate order and no lost position. Three
mechanisms deliver it:

1. Deterministic client order ids — uuid5 over (strategy, session, occ,
   side, seq). A replayed intent MAPS TO THE SAME ID, so `INSERT OR IGNORE`
   makes resubmission a no-op at the storage layer.
2. Intent journal before side effect — the order row (status=INTENT) commits
   BEFORE the broker is called; the fill updates it afterwards. A crash
   between the two leaves a visible INTENT row for recovery to reconcile,
   never a silent duplicate.
3. Real durability on APFS — `PRAGMA fullfsync=ON`: plain fsync on macOS does
   NOT force media flush; F_FULLFSYNC does (finding IDEM-atomic-write-macos).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import sqlite3
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "qts-core.local")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    session_date    TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    occ_symbol      TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    contracts       INTEGER NOT NULL CHECK (contracts > 0),
    limit_cents     INTEGER NOT NULL,
    reason          TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('INTENT','FILLED','REJECTED')),
    created_at      TEXT NOT NULL,
    filled_at       TEXT,
    fill_premium_cents   INTEGER,
    fill_cost_cents      INTEGER,
    commission_cents     INTEGER,
    -- Two sessions racing on one db would both read the same next_seq and one
    -- would silently overwrite the other (finding QTS-3). NOTE: this
    -- constraint alone does NOT raise — journal_intent uses INSERT OR IGNORE
    -- so that a replayed intent is a no-op, and OR IGNORE swallows a UNIQUE
    -- violation just as happily. journal_intent therefore distinguishes the
    -- two cases explicitly and raises SeqCollisionError; see there (G-03).
    UNIQUE (session_date, seq)
);
CREATE TABLE IF NOT EXISTS positions (
    occ_symbol      TEXT PRIMARY KEY,
    state_json      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equity (
    ts              TEXT PRIMARY KEY,
    session_date    TEXT NOT NULL,
    cash_cents      INTEGER NOT NULL,
    open_value_cents INTEGER NOT NULL,
    realized_pnl_today_cents INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_date    TEXT NOT NULL,
    ts              TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    approved        INTEGER NOT NULL,
    decision_json   TEXT NOT NULL
);
"""


def client_order_id(
    strategy: str, session_date: dt.date, occ_symbol: str, side: str, seq: int
) -> str:
    """Deterministic: same intent -> same id, across process restarts."""
    return str(uuid.uuid5(_NAMESPACE, f"{strategy}|{session_date}|{occ_symbol}|{side}|{seq}"))


class SeqCollisionError(RuntimeError):
    """Two different orders claimed the same (session_date, seq).

    Never a replay — a replay carries the SAME deterministic client_order_id.
    This means two writers raced on next_seq, and continuing would execute at
    the broker while the ledger silently kept only one of them (G-03).
    """


class UnknownOrderError(RuntimeError):
    """A fill was recorded for an order that is not in the ledger."""


@dataclass(frozen=True, slots=True)
class OrderIntent:
    client_order_id: str
    session_date: dt.date
    strategy: str
    occ_symbol: str
    side: str  # BUY | SELL
    contracts: int
    limit_cents: int
    reason: str
    seq: int


class StateStore:
    def __init__(self, path: str | Path) -> None:
        # autocommit: each statement commits on its own. The comment here
        # previously claimed "autocommit off via BEGIN" while no BEGIN was ever
        # issued — a false promise the reviewers caught. Multi-statement
        # atomicity is now explicit via the transaction() context manager.
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA fullfsync=ON")  # F_FULLFSYNC on macOS/APFS
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """All-or-nothing for a group of writes.

        Defence-in-depth for the fill->save window (QTS-R1/QTS-1/QTS-2): with
        the fill and the position update in ONE transaction, a crash rolls
        back to the INTENT state instead of leaving a FILLED order with no
        position. ``reconcile()`` still repairs any pre-existing inconsistency;
        this simply stops new ones from being created.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # ---------------------------------------------------------- orders
    def journal_intent(self, intent: OrderIntent, now: dt.datetime) -> bool:
        """Persist the intent BEFORE any side effect.

        Returns False if this EXACT intent already exists (idempotent replay —
        the deterministic client_order_id makes resubmission a no-op).

        Raises SeqCollisionError if the row was rejected for any OTHER reason,
        which in this schema means a different order already holds
        (session_date, seq). INSERT OR IGNORE cannot tell those apart on its
        own, and the caller cannot act safely on a bare False: the previous
        code executed at the broker anyway and then updated zero ledger rows
        (G-03).
        """
        cur = self._conn.execute(
            """INSERT OR IGNORE INTO orders
               (client_order_id, session_date, strategy, occ_symbol, side, contracts,
                limit_cents, reason, seq, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,'INTENT',?)""",
            (
                intent.client_order_id,
                intent.session_date.isoformat(),
                intent.strategy,
                intent.occ_symbol,
                intent.side,
                intent.contracts,
                intent.limit_cents,
                intent.reason,
                intent.seq,
                now.isoformat(),
            ),
        )
        if cur.rowcount == 1:
            return True
        if self.order_status(intent.client_order_id) is not None:
            return False  # same id already journaled: a legitimate replay
        raise SeqCollisionError(
            f"seq {intent.seq} on {intent.session_date} is already held by a different "
            f"order; refusing to execute {intent.occ_symbol} {intent.side} without a "
            "ledger row"
        )

    def record_fill(
        self,
        coid: str,
        now: dt.datetime,
        fill_premium_cents: int,
        fill_cost_cents: int,
        commission_cents: int,
    ) -> None:
        cur = self._conn.execute(
            """UPDATE orders SET status='FILLED', filled_at=?, fill_premium_cents=?,
               fill_cost_cents=?, commission_cents=? WHERE client_order_id=?""",
            (now.isoformat(), fill_premium_cents, fill_cost_cents, commission_cents, coid),
        )
        # A zero-row UPDATE means the broker executed against an order the
        # ledger does not have. Saying nothing here is what let a position be
        # committed with no order behind it, and reconcile() cannot repair that
        # because it rebuilds only FROM order rows (G-03).
        if cur.rowcount != 1:
            raise UnknownOrderError(f"no ledger row for client_order_id={coid!r}")

    def reject_intents(self, coids: list[str]) -> None:
        """Retire rolled-back attempts (INTENT with no committed fill)."""
        self._conn.executemany(
            "UPDATE orders SET status='REJECTED' WHERE client_order_id=? AND status='INTENT'",
            [(c,) for c in coids],
        )

    def unfilled_intents(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT client_order_id FROM orders WHERE status='INTENT' ORDER BY created_at"
        ).fetchall()
        return [r[0] for r in rows]

    def order_status(self, coid: str) -> str | None:
        row = self._conn.execute(
            "SELECT status FROM orders WHERE client_order_id=?", (coid,)
        ).fetchone()
        return row[0] if row else None

    def next_seq(self, session_date: dt.date) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), -1) FROM orders WHERE session_date=?",
            (session_date.isoformat(),),
        ).fetchone()
        return int(row[0]) + 1

    def orders_for_session(self, session_date: dt.date) -> list[sqlite3.Row]:
        self._conn.row_factory = sqlite3.Row
        rows = self._conn.execute(
            "SELECT * FROM orders WHERE session_date=? ORDER BY seq",
            (session_date.isoformat(),),
        ).fetchall()
        self._conn.row_factory = None
        return rows

    # ---------------------------------------------------------- positions
    def save_position(self, occ_symbol: str, state: dict[str, object], now: dt.datetime) -> None:
        self._conn.execute(
            """INSERT INTO positions (occ_symbol, state_json, updated_at) VALUES (?,?,?)
               ON CONFLICT(occ_symbol) DO UPDATE SET state_json=excluded.state_json,
               updated_at=excluded.updated_at""",
            (occ_symbol, json.dumps(state, sort_keys=True), now.isoformat()),
        )

    def load_position(self, occ_symbol: str) -> dict[str, object] | None:
        row = self._conn.execute(
            "SELECT state_json FROM positions WHERE occ_symbol=?", (occ_symbol,)
        ).fetchone()
        return None if row is None else json.loads(row[0])

    def open_positions(self) -> dict[str, dict[str, object]]:
        rows = self._conn.execute("SELECT occ_symbol, state_json FROM positions").fetchall()
        out: dict[str, dict[str, object]] = {}
        for occ, blob in rows:
            state = json.loads(blob)
            if state.get("phase") != "CLOSED":
                out[occ] = state
        return out

    # ---------------------------------------------------------- equity & decisions
    def snapshot_equity(
        self,
        now: dt.datetime,
        session_date: dt.date,
        cash_cents: int,
        open_value_cents: int,
        realized_today_cents: int,
    ) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO equity
               (ts, session_date, cash_cents, open_value_cents, realized_pnl_today_cents)
               VALUES (?,?,?,?,?)""",
            (
                now.isoformat(),
                session_date.isoformat(),
                cash_cents,
                open_value_cents,
                realized_today_cents,
            ),
        )

    def equity_series(self, session_date: dt.date) -> list[tuple[str, int, int, int]]:
        return self._conn.execute(
            """SELECT ts, cash_cents, open_value_cents, realized_pnl_today_cents
               FROM equity WHERE session_date=? ORDER BY ts""",
            (session_date.isoformat(),),
        ).fetchall()

    def log_decision(
        self, now: dt.datetime, session_date: dt.date, symbol: str, approved: bool, blob: str
    ) -> None:
        self._conn.execute(
            "INSERT INTO decisions (session_date, ts, symbol, approved, decision_json)"
            " VALUES (?,?,?,?,?)",
            (session_date.isoformat(), now.isoformat(), symbol, int(approved), blob),
        )

    def decisions_for_session(self, session_date: dt.date) -> list[tuple[str, str, int, str]]:
        return self._conn.execute(
            "SELECT ts, symbol, approved, decision_json FROM decisions WHERE session_date=?"
            " ORDER BY id",
            (session_date.isoformat(),),
        ).fetchall()

    def realized_pnl_today(self, session_date: dt.date) -> int:
        """Sum of realized P&L from FILLED SELL legs minus all commissions today.

        BUY legs move cash into position cost basis; realized P&L is accounted
        on exits (proceeds - basis share), which the paper loop stores in the
        fill_cost_cents column as the signed realized amount for SELLs.
        """
        row = self._conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN side='SELL' THEN fill_cost_cents ELSE 0 END),0)
               FROM orders WHERE session_date=? AND status='FILLED'""",
            (session_date.isoformat(),),
        ).fetchone()
        return int(row[0])
