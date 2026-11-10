"""Per-Market Isolated SQLite Vault & Ledger (`data/pm_markets/<market_slug>.sqlite3`).

Implements Section 5 and Section 6.3.1 of the Master Blueprint (`AGENTS.md`):
- Each market `m` has its own dedicated SQLite file.
- Strictly separates `TotalUnspentCash_m`, `OpenBuyLocks_m`, and `AvailableCash_m`:
    TotalUnspentCash_m(t) = ApprovedDeposits_m(t) - NetBuyCost_m(t) + RealizedSellProceeds_m(t)
    AvailableCash_m(t)    = TotalUnspentCash_m(t) - OpenBuyLocks_m(t)
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Dict, List, Optional


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class VaultCashState:
    market_slug: str
    approved_deposits: float
    net_buy_cost: float
    realized_sell_proceeds: float
    total_unspent_cash: float
    open_buy_locks: float
    available_cash: float


@dataclass(frozen=True)
class TargetLegPlan:
    token_id: str
    outcome_name: str
    target_shares: float
    max_entry_price: float
    tick_size: float
    min_order_size: float
    role_set: str = "primary"


class MarketVault:
    """Isolated SQLite vault for a single Polymarket campaign `m`."""

    def __init__(self, market_slug: str, data_dir: Path | str = "data/pm_markets") -> None:
        if not market_slug or "/" in market_slug or ".." in market_slug:
            raise ValueError(f"Invalid market_slug: {market_slug!r}")
        self.market_slug = market_slug
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / f"{market_slug}.sqlite3"
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA busy_timeout = 30000;")
        return conn

    def _init_schema(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS market_metadata (
                    market_slug TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS vault_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_type TEXT NOT NULL CHECK (entry_type IN ('DEPOSIT', 'BUY_FILL', 'SELL_PROCEEDS', 'WITHDRAWAL')),
                    amount_usd REAL NOT NULL CHECK (amount_usd >= 0.0),
                    reference_id TEXT,
                    note TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS target_positions (
                    token_id TEXT PRIMARY KEY,
                    outcome_name TEXT NOT NULL,
                    role_set TEXT NOT NULL,
                    target_shares REAL NOT NULL CHECK (target_shares >= 0.0),
                    max_entry_price REAL NOT NULL CHECK (max_entry_price > 0.0 AND max_entry_price < 1.0),
                    tick_size REAL NOT NULL CHECK (tick_size > 0.0),
                    min_order_size REAL NOT NULL CHECK (min_order_size >= 0.0),
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS clob_orders (
                    order_id TEXT PRIMARY KEY,
                    client_order_id TEXT UNIQUE NOT NULL,
                    token_id TEXT NOT NULL,
                    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
                    order_type TEXT NOT NULL,
                    limit_price REAL NOT NULL CHECK (limit_price > 0.0 AND limit_price < 1.0),
                    size_shares REAL NOT NULL CHECK (size_shares > 0.0),
                    matched_shares REAL NOT NULL DEFAULT 0.0 CHECK (matched_shares >= 0.0),
                    locked_usdc REAL NOT NULL DEFAULT 0.0 CHECK (locked_usdc >= 0.0),
                    status TEXT NOT NULL CHECK (status IN ('OPEN', 'FILLED', 'CANCELED', 'EXPIRED')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS verified_inventory (
                    token_id TEXT PRIMARY KEY,
                    wallet_shares REAL NOT NULL CHECK (wallet_shares >= 0.0),
                    vwap_price REAL NOT NULL DEFAULT 0.0 CHECK (vwap_price >= 0.0),
                    last_verified_at TEXT NOT NULL
                );
                """
            )
            now = utcnow_iso()
            conn.execute(
                """
                INSERT INTO market_metadata (market_slug, created_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(market_slug) DO UPDATE SET updated_at = excluded.updated_at;
                """,
                (self.market_slug, now, now),
            )
            conn.commit()

    def get_cash_state(self) -> VaultCashState:
        """Computes exact isolated cash state per Section 6.3.1."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT entry_type, COALESCE(SUM(amount_usd), 0.0) AS total FROM vault_ledger GROUP BY entry_type"
            ).fetchall()
            totals = {r["entry_type"]: float(r["total"]) for r in rows}

            approved_deposits = totals.get("DEPOSIT", 0.0) - totals.get("WITHDRAWAL", 0.0)
            net_buy_cost = totals.get("BUY_FILL", 0.0)
            realized_sell_proceeds = totals.get("SELL_PROCEEDS", 0.0)
            total_unspent_cash = round(approved_deposits - net_buy_cost + realized_sell_proceeds, 6)

            lock_row = conn.execute(
                "SELECT COALESCE(SUM(locked_usdc), 0.0) AS open_locks FROM clob_orders WHERE status = 'OPEN' AND side = 'BUY'"
            ).fetchone()
            open_buy_locks = round(float(lock_row["open_locks"]) if lock_row else 0.0, 6)
            available_cash = round(total_unspent_cash - open_buy_locks, 6)

            return VaultCashState(
                market_slug=self.market_slug,
                approved_deposits=round(approved_deposits, 6),
                net_buy_cost=round(net_buy_cost, 6),
                realized_sell_proceeds=round(realized_sell_proceeds, 6),
                total_unspent_cash=total_unspent_cash,
                open_buy_locks=open_buy_locks,
                available_cash=available_cash,
            )

    def record_deposit(self, amount_usd: float, note: str = "") -> VaultCashState:
        if amount_usd <= 0.0:
            raise ValueError("Deposit amount must be strictly positive.")
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO vault_ledger (entry_type, amount_usd, note, created_at) VALUES ('DEPOSIT', ?, ?, ?)",
                (round(amount_usd, 6), note, utcnow_iso()),
            )
            conn.commit()
        return self.get_cash_state()

    def upsert_target_leg(self, leg: TargetLegPlan) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO target_positions (
                    token_id, outcome_name, role_set, target_shares, max_entry_price, tick_size, min_order_size, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(token_id) DO UPDATE SET
                    outcome_name = excluded.outcome_name,
                    role_set = excluded.role_set,
                    target_shares = excluded.target_shares,
                    max_entry_price = excluded.max_entry_price,
                    tick_size = excluded.tick_size,
                    min_order_size = excluded.min_order_size,
                    updated_at = excluded.updated_at;
                """,
                (
                    leg.token_id,
                    leg.outcome_name,
                    leg.role_set,
                    round(leg.target_shares, 6),
                    round(leg.max_entry_price, 6),
                    round(leg.tick_size, 6),
                    round(leg.min_order_size, 6),
                    utcnow_iso(),
                ),
            )
            conn.commit()

    def get_target_positions(self) -> Dict[str, TargetLegPlan]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM target_positions ORDER BY token_id").fetchall()
            return {
                r["token_id"]: TargetLegPlan(
                    token_id=r["token_id"],
                    outcome_name=r["outcome_name"],
                    role_set=r["role_set"],
                    target_shares=float(r["target_shares"]),
                    max_entry_price=float(r["max_entry_price"]),
                    tick_size=float(r["tick_size"]),
                    min_order_size=float(r["min_order_size"]),
                )
                for r in rows
            }

    def register_open_order(
        self,
        order_id: str,
        client_order_id: str,
        token_id: str,
        side: str,
        order_type: str,
        limit_price: float,
        size_shares: float,
    ) -> None:
        side_u = side.upper()
        locked_usdc = round(limit_price * size_shares, 6) if side_u == "BUY" else 0.0
        state = self.get_cash_state()
        if side_u == "BUY" and locked_usdc > state.available_cash + 1e-6:
            raise ValueError(
                f"Insufficient AvailableCash_m in {self.market_slug}: required {locked_usdc:.4f}, available {state.available_cash:.4f}"
            )
        now = utcnow_iso()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO clob_orders (
                    order_id, client_order_id, token_id, side, order_type,
                    limit_price, size_shares, matched_shares, locked_usdc, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0.0, ?, 'OPEN', ?, ?)
                """,
                (order_id, client_order_id, token_id, side_u, order_type.upper(), limit_price, size_shares, locked_usdc, now, now),
            )
            conn.commit()

    def record_order_fill(self, order_id: str, newly_matched_shares: float, fill_price: Optional[float] = None) -> None:
        if newly_matched_shares <= 0.0:
            return
        now = utcnow_iso()
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM clob_orders WHERE order_id = ?", (order_id,)).fetchone()
            if not row:
                raise KeyError(f"Order {order_id} not found in vault {self.market_slug}")
            eff_price = float(fill_price if fill_price is not None else row["limit_price"])
            side = row["side"]
            new_matched = min(float(row["size_shares"]), float(row["matched_shares"]) + newly_matched_shares)
            rem_shares = max(0.0, float(row["size_shares"]) - new_matched)
            new_locked = round(rem_shares * float(row["limit_price"]), 6) if side == "BUY" else 0.0
            new_status = "FILLED" if rem_shares <= 1e-6 else "OPEN"

            conn.execute(
                "UPDATE clob_orders SET matched_shares = ?, locked_usdc = ?, status = ?, updated_at = ? WHERE order_id = ?",
                (round(new_matched, 6), new_locked, new_status, now, order_id),
            )
            notional = round(newly_matched_shares * eff_price, 6)
            entry_type = "BUY_FILL" if side == "BUY" else "SELL_PROCEEDS"
            conn.execute(
                "INSERT INTO vault_ledger (entry_type, amount_usd, reference_id, note, created_at) VALUES (?, ?, ?, ?, ?)",
                (entry_type, notional, order_id, f"{side} {newly_matched_shares:.4f} sh @ {eff_price:.4f}", now),
            )
            token_id = str(row["token_id"])
            inv_row = conn.execute(
                "SELECT wallet_shares, vwap_price FROM verified_inventory WHERE token_id = ?",
                (token_id,),
            ).fetchone()
            prev_sh = float(inv_row["wallet_shares"]) if inv_row else 0.0
            prev_vwap = float(inv_row["vwap_price"]) if inv_row else 0.0
            if side == "BUY":
                new_sh = round(prev_sh + newly_matched_shares, 6)
                new_vwap = round(((prev_sh * prev_vwap) + notional) / new_sh, 6) if new_sh > 0.0 else 0.0
            else:
                new_sh = round(max(0.0, prev_sh - newly_matched_shares), 6)
                new_vwap = prev_vwap if new_sh > 0.0 else 0.0
            conn.execute(
                """
                INSERT INTO verified_inventory (token_id, wallet_shares, vwap_price, last_verified_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(token_id) DO UPDATE SET
                    wallet_shares = excluded.wallet_shares,
                    vwap_price = excluded.vwap_price,
                    last_verified_at = excluded.last_verified_at;
                """,
                (token_id, new_sh, new_vwap, now),
            )
            conn.commit()

    def mark_order_closed(self, order_id: str, new_status: str = "CANCELED") -> None:
        if new_status not in ("CANCELED", "EXPIRED", "FILLED"):
            raise ValueError(f"Invalid terminal status: {new_status}")
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE clob_orders SET locked_usdc = 0.0, status = ?, updated_at = ? WHERE order_id = ?",
                (new_status, utcnow_iso(), order_id),
            )
            conn.commit()

    def owns_order(self, order_id: str) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT 1 FROM clob_orders WHERE order_id = ?", (order_id,)).fetchone()
            return row is not None

    def get_open_orders(self, token_id: Optional[str] = None) -> List[Dict[str, object]]:
        with closing(self._connect()) as conn:
            if token_id:
                rows = conn.execute(
                    "SELECT * FROM clob_orders WHERE status = 'OPEN' AND token_id = ? ORDER BY created_at",
                    (token_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM clob_orders WHERE status = 'OPEN' ORDER BY created_at").fetchall()
            return [dict(r) for r in rows]

    def update_verified_inventory(self, token_id: str, wallet_shares: float, vwap_price: float = 0.0) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO verified_inventory (token_id, wallet_shares, vwap_price, last_verified_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(token_id) DO UPDATE SET
                    wallet_shares = excluded.wallet_shares,
                    vwap_price = CASE WHEN excluded.vwap_price > 0 THEN excluded.vwap_price ELSE verified_inventory.vwap_price END,
                    last_verified_at = excluded.last_verified_at;
                """,
                (token_id, round(wallet_shares, 6), round(vwap_price, 6), utcnow_iso()),
            )
            conn.commit()

    def get_verified_inventory(self) -> Dict[str, Dict[str, float]]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM verified_inventory").fetchall()
            return {
                r["token_id"]: {
                    "wallet_shares": float(r["wallet_shares"]),
                    "vwap_price": float(r["vwap_price"]),
                }
                for r in rows
            }
