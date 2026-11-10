"""Central Wallet Guard (`data/pm_markets/_wallet_guard.sqlite3`).

Implements Section 7.3.2 of the Master Blueprint:
- Guard A: Maker-Collateral Reservation & Dual Solvency Check across all market vaults.
- Guard B: Global `token_id` Ownership Lock & Order Ownership Registry (preventing cross-market cancellation).
- Guard C: Sequential Execution Mutex (`flock`) to prevent concurrent API race conditions.
- Guard D: Zero interference with any external wallet activity outside registered market vaults.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
from pathlib import Path
import sqlite3
from typing import Dict, Iterator, List

from .market_vault import MarketVault


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CentralWalletGuard:
    """Enforces cross-market collateral solvency, token ownership, and sequential flock mutex."""

    def __init__(self, data_dir: Path | str = "data/pm_markets") -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "_wallet_guard.sqlite3"
        self.lock_path = self.data_dir / "_wallet_guard.lock"
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 30000;")
        return conn

    def _init_schema(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS registered_markets (
                    market_slug TEXT PRIMARY KEY,
                    db_path TEXT NOT NULL,
                    registered_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS token_ownership (
                    token_id TEXT PRIMARY KEY,
                    market_slug TEXT NOT NULL,
                    claimed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS order_ownership (
                    order_id TEXT PRIMARY KEY,
                    market_slug TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            conn.commit()

    @contextmanager
    def execution_lock(self) -> Iterator[None]:
        """Sequential file lock (`flock`) across all market operations (Guard C)."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def register_market(self, vault: MarketVault) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO registered_markets (market_slug, db_path, registered_at)
                VALUES (?, ?, ?)
                ON CONFLICT(market_slug) DO UPDATE SET db_path = excluded.db_path;
                """,
                (vault.market_slug, str(vault.db_path.resolve()), utcnow_iso()),
            )
            conn.commit()

    def list_registered_vaults(self) -> List[MarketVault]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT market_slug FROM registered_markets ORDER BY market_slug").fetchall()
            return [MarketVault(r["market_slug"], data_dir=self.data_dir) for r in rows]

    def claim_token(self, token_id: str, market_slug: str) -> None:
        """Enforces global `token_id` exclusivity (Guard B)."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT market_slug FROM token_ownership WHERE token_id = ?", (token_id,)
            ).fetchone()
            if row and row["market_slug"] != market_slug:
                raise PermissionError(
                    f"TOKEN_OWNERSHIP_CONFLICT: token_id {token_id} is already owned by market {row['market_slug']!r}, cannot be claimed by {market_slug!r}"
                )
            conn.execute(
                """
                INSERT INTO token_ownership (token_id, market_slug, claimed_at)
                VALUES (?, ?, ?)
                ON CONFLICT(token_id) DO NOTHING;
                """,
                (token_id, market_slug, utcnow_iso()),
            )
            conn.commit()

    def register_order_ownership(self, order_id: str, market_slug: str, token_id: str) -> None:
        self.claim_token(token_id, market_slug)
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO order_ownership (order_id, market_slug, token_id, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(order_id) DO NOTHING;
                """,
                (order_id, market_slug, token_id, utcnow_iso()),
            )
            conn.commit()

    def verify_order_ownership(self, order_id: str, market_slug: str) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT market_slug FROM order_ownership WHERE order_id = ?", (order_id,)
            ).fetchone()
            return bool(row and row["market_slug"] == market_slug)

    def aggregate_cash_reservations(self) -> Dict[str, float]:
        """Sums `TotalUnspentCash_k` and `OpenBuyLocks_k` across all registered market DBs."""
        total_unspent_all = 0.0
        open_buy_locks_all = 0.0
        for vault in self.list_registered_vaults():
            st = vault.get_cash_state()
            total_unspent_all += st.total_unspent_cash
            open_buy_locks_all += st.open_buy_locks
        return {
            "total_unspent_cash_all": round(total_unspent_all, 6),
            "open_buy_locks_all": round(open_buy_locks_all, 6),
        }

    def approve_deposit(self, vault: MarketVault, deposit_amount_usd: float, on_chain_wallet_usdc: float, note: str = "") -> float:
        """Guard A.1: Verifies `TrulyUnallocatedWalletUSDC >= deposit_amount_usd` before crediting `vault`."""
        self.register_market(vault)
        agg = self.aggregate_cash_reservations()
        truly_unallocated = round(on_chain_wallet_usdc - agg["total_unspent_cash_all"], 6)
        if deposit_amount_usd > truly_unallocated + 1e-6:
            raise ValueError(
                f"INSUFFICIENT_UNALLOCATED_WALLET_USDC: requested deposit {deposit_amount_usd:.2f}, "
                f"but TrulyUnallocatedWalletUSDC is {truly_unallocated:.2f} "
                f"(on-chain={on_chain_wallet_usdc:.2f}, reserved_across_markets={agg['total_unspent_cash_all']:.2f})"
            )
        vault.record_deposit(deposit_amount_usd, note=note)
        return round(truly_unallocated - deposit_amount_usd, 6)

    def verify_pre_order_solvency(
        self,
        vault: MarketVault,
        order_cost_usd: float,
        on_chain_wallet_usdc: float,
    ) -> None:
        """Guard A.2: Verifies both `OrderCost <= AvailableCash_m` and `OrderCost + sum(OpenBuyLocks_k) <= WalletUSDC_on_chain`."""
        self.register_market(vault)
        m_state = vault.get_cash_state()
        if order_cost_usd > m_state.available_cash + 1e-6:
            raise ValueError(
                f"INSUFFICIENT_MARKET_AVAILABLE_CASH: market {vault.market_slug!r} needs {order_cost_usd:.4f}, "
                f"has AvailableCash_m={m_state.available_cash:.4f}"
            )
        agg = self.aggregate_cash_reservations()
        required_on_chain = round(order_cost_usd + agg["open_buy_locks_all"], 6)
        if required_on_chain > on_chain_wallet_usdc + 1e-6:
            raise ValueError(
                f"MAKER_COLLATERAL_BREACH_PREVENTED: order_cost ({order_cost_usd:.4f}) + "
                f"all_open_buy_locks ({agg['open_buy_locks_all']:.4f}) = {required_on_chain:.4f} > "
                f"on_chain_wallet_usdc ({on_chain_wallet_usdc:.4f})"
            )
