"""Standalone Polymarket Multi-Option & Futures CLOB Executor (`AutonomousMarketsExecutor`).

Implements Section 7.1, 7.2, and 7.3 of the Master Blueprint:
- Connects directly to `py_clob_client_v2` / `py_clob_client` L1/L2 protocol (`signature_type=1`).
- Code-level ban on `cancel_all()` and `cancel_market_orders()`; only single `order_id` cancellation verified
  against `MarketVault` and `CentralWalletGuard` is permitted.
- Three-Way Truth Reconciliation Loop (`Target Plan` vs `On-Chain Wallet Inventory` vs `Live CLOB Open Orders`).
- Generates the 3-Column Verified Execution Receipt for chat verification.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .market_vault import MarketVault
from .wallet_guard import CentralWalletGuard

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LegReconciliationRow:
    token_id: str
    outcome_name: str
    role_set: str
    target_shares: float
    max_entry_price: float
    wallet_shares: float
    wallet_vwap: float
    open_order_shares: float
    unfilled_discrepancy: float
    is_verified_on_chain: bool
    status_action: str


class AutonomousMarketsExecutor:
    """100% standalone CLOB executor with 3-way on-chain reconciliation and per-market isolation."""

    def __init__(
        self,
        wallet_guard: CentralWalletGuard,
        env_path: Optional[Path | str] = None,
        client: Optional[Any] = None,
        dry_run: Optional[bool] = None,
    ) -> None:
        self.wallet_guard = wallet_guard
        if env_path:
            self._load_dotenv(Path(env_path))
        else:
            default_env = Path(__file__).resolve().parents[2] / ".env"
            if default_env.exists():
                self._load_dotenv(default_env)

        env_dry = os.getenv("POLYMARKET_DRY_RUN", "").lower() in ("true", "1", "yes")
        self.dry_run = dry_run if dry_run is not None else env_dry
        self.private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
        self.proxy_address = os.getenv("POLYMARKET_PROXY", "")
        self.client = client
        self.is_v2 = False
        if self.client is None and not self.dry_run and self.private_key:
            self._init_clob_client()

    @staticmethod
    def _load_dotenv(path: Path) -> None:
        if not path.exists():
            return
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'").strip('"')
            if k and k not in os.environ:
                os.environ[k] = v

    def _init_clob_client(self) -> None:
        try:
            try:
                from py_clob_client_v2.client import ClobClient  # type: ignore
                self.is_v2 = True
            except ImportError:
                from py_clob_client.client import ClobClient  # type: ignore
                self.is_v2 = False

            c = ClobClient(
                host="https://clob.polymarket.com",
                chain_id=137,
                key=self.private_key,
                funder=self.proxy_address,
                signature_type=1,
            )
            if hasattr(c, "create_or_derive_api_creds"):
                creds = c.create_or_derive_api_creds()
                c.set_api_creds(creds)
            self.client = c
        except Exception as exc:
            log.warning("Could not initialize live ClobClient (%s); falling back to dry_run=True", exc)
            self.dry_run = True

    def cancel_all(self, *_args: Any, **_kwargs: Any) -> None:
        """Code-level ban on global order cancellation (Section 7.3.2.B)."""
        raise RuntimeError(
            "FORBIDDEN_OPERATION: Global cancel_all() is strictly prohibited by Section 7.3.2.B. "
            "Use cancel_market_order_by_id(vault, order_id) instead."
        )

    def cancel_market_orders(self, *_args: Any, **_kwargs: Any) -> None:
        """Code-level ban on blanket market order cancellation (Section 7.3.2.B)."""
        raise RuntimeError(
            "FORBIDDEN_OPERATION: Blanket cancel_market_orders() is strictly prohibited by Section 7.3.2.B. "
            "Use cancel_market_order_by_id(vault, order_id) instead."
        )

    def cancel_market_order_by_id(self, vault: MarketVault, order_id: str) -> bool:
        """Cancels a single `order_id` ONLY if owned by `vault.market_slug`."""
        if not vault.owns_order(order_id) or not self.wallet_guard.verify_order_ownership(order_id, vault.market_slug):
            raise PermissionError(
                f"ORDER_OWNERSHIP_VIOLATION: Market {vault.market_slug!r} attempted to cancel unowned order_id {order_id!r}"
            )
        if not self.dry_run and self.client is not None:
            if hasattr(self.client, "cancel"):
                self.client.cancel(order_id)
            elif hasattr(self.client, "cancel_orders"):
                self.client.cancel_orders([order_id])
        vault.mark_order_closed(order_id, new_status="CANCELED")
        return True

    def get_on_chain_collateral_usdc(self) -> float:
        """Fetches physical USDC.e balance on the proxy wallet (`AssetType.COLLATERAL, signature_type=1`)."""
        if self.dry_run or self.client is None:
            return 0.0
        try:
            from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams  # type: ignore
        except ImportError:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams  # type: ignore
        res = self.client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=1)
        )
        raw_bal = float(res.get("balance", 0.0)) if isinstance(res, dict) else 0.0
        return round(raw_bal / 1e6 if raw_bal > 1e4 else raw_bal, 6)

    def get_on_chain_conditional_shares(self, token_id: str) -> float:
        """Fetches physical ERC-1155 conditional token shares (`AssetType.CONDITIONAL, signature_type=1`)."""
        if self.dry_run or self.client is None:
            return 0.0
        try:
            from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams  # type: ignore
        except ImportError:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams  # type: ignore
        res = self.client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id, signature_type=1)
        )
        raw_sh = float(res.get("balance", 0.0)) if isinstance(res, dict) else 0.0
        return round(raw_sh / 1e6 if raw_sh > 1e4 else raw_sh, 6)

    def submit_isolated_limit_order(
        self,
        vault: MarketVault,
        token_id: str,
        side: str,
        limit_price: float,
        size_shares: float,
        order_type: str = "GTC",
        on_chain_wallet_usdc: Optional[float] = None,
        simulated_order_id: Optional[str] = None,
    ) -> str:
        """Submits a limit order after enforcing all 4 CentralWalletGuard invariants."""
        side_u = side.upper()
        order_cost = round(limit_price * size_shares, 6) if side_u == "BUY" else 0.0

        with self.wallet_guard.execution_lock():
            # 1. Claim token exclusivity (Guard B)
            self.wallet_guard.claim_token(token_id, vault.market_slug)

            # 2. Pre-order dual solvency check (Guard A.2)
            if side_u == "BUY":
                live_usdc = (
                    on_chain_wallet_usdc
                    if on_chain_wallet_usdc is not None
                    else self.get_on_chain_collateral_usdc()
                )
                self.wallet_guard.verify_pre_order_solvency(
                    vault=vault,
                    order_cost_usd=order_cost,
                    on_chain_wallet_usdc=live_usdc,
                )

            # 3. Submit to CLOB (or register deterministic order_id in dry_run / test)
            order_id = simulated_order_id or f"ord_{vault.market_slug}_{token_id[:8]}_{int(size_shares*100)}"
            if not self.dry_run and self.client is not None and simulated_order_id is None:
                try:
                    from py_clob_client_v2.clob_types import OrderArgs, OrderType  # type: ignore
                except ImportError:
                    from py_clob_client.clob_types import OrderArgs, OrderType  # type: ignore
                from py_clob_client.order_builder.constants import BUY, SELL  # type: ignore

                clob_side = BUY if side_u == "BUY" else SELL
                signed = self.client.create_order(
                    OrderArgs(price=limit_price, size=size_shares, side=clob_side, token_id=token_id)
                )
                ot = getattr(OrderType, order_type.upper(), OrderType.GTC)
                resp = self.client.post_order(signed, ot)
                if isinstance(resp, dict) and resp.get("orderID"):
                    order_id = str(resp["orderID"])

            client_order_id = f"cid_{order_id}"
            vault.register_open_order(
                order_id=order_id,
                client_order_id=client_order_id,
                token_id=token_id,
                side=side_u,
                order_type=order_type,
                limit_price=limit_price,
                size_shares=size_shares,
            )
            self.wallet_guard.register_order_ownership(
                order_id=order_id,
                market_slug=vault.market_slug,
                token_id=token_id,
            )
            return order_id

    def reconcile_market_state(
        self,
        vault: MarketVault,
        wallet_shares_override: Optional[Dict[str, float]] = None,
        dust_tolerance: float = 0.05,
    ) -> List[LegReconciliationRow]:
        """Executes the Three-Way Truth Reconciliation Loop (Section 7.1)."""
        targets = vault.get_target_positions()
        inv = vault.get_verified_inventory()
        rows: List[LegReconciliationRow] = []

        for token_id, target in targets.items():
            if wallet_shares_override is not None and token_id in wallet_shares_override:
                w_sh = float(wallet_shares_override[token_id])
                vwap = inv.get(token_id, {}).get("vwap_price", target.max_entry_price)
                vault.update_verified_inventory(token_id, w_sh, vwap)
            elif not self.dry_run and self.client is not None:
                w_sh = self.get_on_chain_conditional_shares(token_id)
                vwap = inv.get(token_id, {}).get("vwap_price", target.max_entry_price)
                vault.update_verified_inventory(token_id, w_sh, vwap)
            else:
                w_sh = inv.get(token_id, {}).get("wallet_shares", 0.0)
                vwap = inv.get(token_id, {}).get("vwap_price", 0.0)

            open_orders = vault.get_open_orders(token_id=token_id)
            open_rem_shares = sum(
                max(0.0, float(o["size_shares"]) - float(o["matched_shares"]))
                for o in open_orders
                if o["side"] == "BUY"
            )
            discrepancy = round(target.target_shares - w_sh, 6)
            is_verified = (abs(discrepancy) <= dust_tolerance and open_rem_shares <= 1e-6)

            if is_verified:
                action = "VERIFIED_ON_CHAIN"
            elif open_rem_shares > 0.0:
                action = f"WORKING_MAKER_GTC ({open_rem_shares:.2f} sh open)"
            else:
                action = f"NEEDS_REOPTIMIZATION_OR_FILL ({discrepancy:.2f} sh missing)"

            rows.append(
                LegReconciliationRow(
                    token_id=token_id,
                    outcome_name=target.outcome_name,
                    role_set=target.role_set,
                    target_shares=target.target_shares,
                    max_entry_price=target.max_entry_price,
                    wallet_shares=round(w_sh, 4),
                    wallet_vwap=round(vwap, 4),
                    open_order_shares=round(open_rem_shares, 4),
                    unfilled_discrepancy=round(discrepancy, 4),
                    is_verified_on_chain=is_verified,
                    status_action=action,
                )
            )
        return rows

    @staticmethod
    def format_three_column_receipt(rows: List[LegReconciliationRow]) -> str:
        """Formats the 3-Column Verified Execution Receipt (Section 7.2.4) as Markdown."""
        lines = [
            "| Outcome (`Role`) | 1. Target Plan (`Target Shares @ Max Price`) | 2. On-Chain Wallet Verified (`Settled Shares @ VWAP`) | 3. Open Orders & Reconciliation Action |",
            "| :--- | :---: | :---: | :--- |",
        ]
        for r in rows:
            lines.append(
                f"| **{r.outcome_name}** (`{r.role_set}`) | `{r.target_shares:.2f} sh @ <= {r.max_entry_price:.3f}` | "
                f"`{r.wallet_shares:.2f} sh @ {r.wallet_vwap:.3f}` | `{r.status_action}` |"
            )
        return "\n".join(lines)
