"""Unconstrained First-Principles Strategy & Proof Evaluator (`MarketStrategyOptimizer`).

Implements Sections 1, 3, and 4 of the Master Blueprint (`AGENTS.md`):
- Zero hardcoded constants, zero forced basket templates, zero structural bias toward mutually exclusive (`NegRisk`) markets.
- Supports both mutually exclusive outcome partitions AND general joint state-space payoff structures (`JointStateSpec`),
  where multiple held legs can simultaneously resolve to `$1.00`.
- Evaluates any first-principles position vector `q` strictly from live Ask/Bid orderbook integrals
  and verified probability distributions, proving:
    1. MaxLoss_guarded (purely from Bid-ladder integral and step shock, or full cost if unabsorbable),
    2. MaxProfit,
    3. Realistic Expected Profit (E[PnL_real]),
    4. Effective Risk/Reward Ratio (R/R).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .scanner_evaluator import OrderbookSnapshot, integrate_ask_ladder, integrate_bid_ladder


@dataclass(frozen=True)
class OutcomeSpec:
    token_id: str
    name: str
    true_prob: float
    target_entry_price: float
    orderbook: OrderbookSnapshot
    max_step_adverse_drop: float = 0.0  # Empirical max single-window price drop (\Delta p_i)
    target_exit_price: float = 1.0  # Supports both hold-to-maturity ($1.00) and pre-maturity convergence exits (< $1.00)
    use_live_ask_integral: bool = False  # When True (Taker entry), computes exact entry cost from live Ask ladder


@dataclass(frozen=True)
class JointStateSpec:
    r"""General world-state specification `\omega \in \Omega` for non-mutually-exclusive or intermediate-exit markets."""

    state_id: str
    probability: float
    winning_token_ids: Tuple[str, ...] = ()
    token_valuations: Optional[Dict[str, float]] = None  # Optional exact per-share value V_i(\omega) \in [0, 1] in state \omega


@dataclass(frozen=True)
class PositionProofReport:
    total_cost_usd: float
    max_profit_usd: float
    min_positive_state_profit_usd: float
    realistic_expected_profit_usd: float
    endogenous_bid_liquidation_usd: float
    max_loss_guarded_usd: float
    naive_maturity_max_loss_usd: float
    effective_rr: float
    leg_shares: Dict[str, float]
    is_profitable_go: bool
    proof_summary: str

    @property
    def min_covered_profit_usd(self) -> float:
        return self.min_positive_state_profit_usd


class MarketStrategyOptimizer:
    """Pure first-principles mathematical evaluator with zero template anchoring or magic constants."""

    @staticmethod
    def prove_position_vector(
        outcomes: List[OutcomeSpec],
        leg_shares: Dict[str, float],
        joint_states: Optional[List[JointStateSpec]] = None,
        has_discontinuous_gap_risk: bool = False,
        min_acceptable_rr: float = 0.0,
    ) -> PositionProofReport:
        """Proves the 4 mandatory metrics (MaxLoss_guarded, MaxProfit, Realistic E[PnL], R/R) for any position vector `q`."""
        active_outcomes = [o for o in outcomes if leg_shares.get(o.token_id, 0.0) > 0.0]
        if not active_outcomes:
            raise ValueError("Position vector must contain at least one positive leg.")

        for o in active_outcomes:
            if not (0.0 <= o.true_prob <= 1.0):
                raise ValueError(f"Invalid probability {o.true_prob} for outcome {o.token_id!r}.")
            if not (0.0 < o.target_entry_price < 1.0):
                raise ValueError(f"Invalid target_entry_price {o.target_entry_price} for outcome {o.token_id!r}.")
            if not (0.0 < o.target_exit_price <= 1.0):
                raise ValueError(f"Invalid target_exit_price {o.target_exit_price} for outcome {o.token_id!r}.")

        total_cost = 0.0
        for o in active_outcomes:
            sh = leg_shares[o.token_id]
            if o.use_live_ask_integral:
                ask_int = integrate_ask_ladder(o.orderbook.asks, sh)
                if not ask_int.fully_absorbable:
                    raise ValueError(f"Ask ladder for {o.token_id!r} cannot absorb {sh} shares as Taker.")
                total_cost += ask_int.total_usdc
            else:
                total_cost += sh * o.target_entry_price
        total_cost = round(total_cost, 6)

        # Endogenous Bid-Ladder Liquidation Integral: \sum_i \int_0^{q_i} Bid_i(u) du
        total_bid_liq = 0.0
        step_shock_usd = 0.0
        all_absorbable = True

        for o in active_outcomes:
            sh = leg_shares[o.token_id]
            bid_int = integrate_bid_ladder(o.orderbook.bids, sh)
            total_bid_liq += bid_int.total_usdc
            step_shock_usd += sh * max(0.0, o.max_step_adverse_drop)
            if not bid_int.fully_absorbable:
                all_absorbable = False

        total_bid_liq = round(total_bid_liq, 6)
        if has_discontinuous_gap_risk or not all_absorbable or total_bid_liq <= 0.0:
            max_loss_guarded = total_cost
        else:
            immediate_liquidation_gap = max(0.0, total_cost - total_bid_liq)
            max_loss_guarded = round(min(total_cost, immediate_liquidation_gap + step_shock_usd), 6)
            if max_loss_guarded <= 0.0:
                min_tick_friction = sum(leg_shares[o.token_id] * o.orderbook.tick_size for o in active_outcomes)
                max_loss_guarded = round(max(1e-6, min_tick_friction), 6)

        # Evaluate state-space payoffs (either general joint states \Omega or mutually exclusive partition)
        if joint_states is not None:
            prob_sum = sum(s.probability for s in joint_states)
            if prob_sum > 1.0 + 1e-6 or any(s.probability < 0.0 for s in joint_states):
                raise ValueError(f"Invalid joint_states probability distribution (sum={prob_sum:.6f}).")
            active_map = {o.token_id: o for o in active_outcomes}
            covered_states_profits: List[Tuple[float, float]] = []
            uncovered_prob = max(0.0, 1.0 - prob_sum)
            for s in joint_states:
                if s.token_valuations is not None:
                    gross_payout = sum(
                        leg_shares[tid] * float(val)
                        for tid, val in s.token_valuations.items()
                        if tid in active_map
                    )
                else:
                    gross_payout = sum(
                        leg_shares[tid] * active_map[tid].target_exit_price
                        for tid in s.winning_token_ids
                        if tid in active_map
                    )
                if gross_payout > 0.0:
                    covered_states_profits.append((s.probability, round(gross_payout - total_cost, 6)))
                else:
                    uncovered_prob += s.probability
            if not covered_states_profits:
                max_profit = -total_cost
                min_pos_profit = -total_cost
                realistic_expected_profit = round(-max_loss_guarded, 6)
            else:
                max_profit = max(p for _, p in covered_states_profits)
                min_pos_profit = min(p for _, p in covered_states_profits)
                realistic_expected_profit = round(
                    sum(prob * net_p for prob, net_p in covered_states_profits)
                    - uncovered_prob * max_loss_guarded,
                    6,
                )
        else:
            covered_prob_sum = sum(o.true_prob for o in active_outcomes)
            if covered_prob_sum > 1.0 + 1e-6:
                raise ValueError(
                    f"Sum of mutually exclusive probabilities ({covered_prob_sum:.6f}) exceeds 1.0; "
                    "pass `joint_states` for non-mutually-exclusive markets."
                )
            net_profits_if_win = {
                o.token_id: round((leg_shares[o.token_id] * o.target_exit_price) - total_cost, 6)
                for o in active_outcomes
            }
            max_profit = max(net_profits_if_win.values())
            min_pos_profit = min(net_profits_if_win.values())
            uncovered_prob = max(0.0, 1.0 - covered_prob_sum)
            realistic_expected_profit = round(
                sum(o.true_prob * net_profits_if_win[o.token_id] for o in active_outcomes)
                - uncovered_prob * max_loss_guarded,
                6,
            )

        effective_rr = round(realistic_expected_profit / max_loss_guarded, 4) if max_loss_guarded > 0 else 0.0
        is_go = (
            realistic_expected_profit > 0.0
            and max_profit > 0.0
            and effective_rr > 0.0
            and effective_rr >= min_acceptable_rr
        )

        summary = (
            f"{'GO' if is_go else 'NO_GO'} | Cost=${total_cost:.2f} | "
            f"MaxProfit=+${max_profit:.2f} | RealisticE[PnL]=${realistic_expected_profit:+.2f} | "
            f"GuardedMaxLoss=-${max_loss_guarded:.2f} (Naive=-${total_cost:.2f}) | R/R={effective_rr:.2f}x"
        )

        return PositionProofReport(
            total_cost_usd=total_cost,
            max_profit_usd=max_profit,
            min_positive_state_profit_usd=min_pos_profit,
            realistic_expected_profit_usd=realistic_expected_profit,
            endogenous_bid_liquidation_usd=total_bid_liq,
            max_loss_guarded_usd=max_loss_guarded,
            naive_maturity_max_loss_usd=total_cost,
            effective_rr=effective_rr,
            leg_shares={k: round(v, 6) for k, v in leg_shares.items() if v > 0.0},
            is_profitable_go=is_go,
            proof_summary=summary,
        )

    @staticmethod
    def rank_candidate_vectors(
        outcomes: List[OutcomeSpec],
        candidate_vectors: List[Dict[str, float]],
        joint_states: Optional[List[JointStateSpec]] = None,
        has_discontinuous_gap_risk: bool = False,
        min_acceptable_rr: float = 0.0,
    ) -> List[PositionProofReport]:
        """Evaluates and ranks any set of candidate vectors purely by Effective R/R and Realistic Expected Profit."""
        reports = [
            MarketStrategyOptimizer.prove_position_vector(
                outcomes=outcomes,
                leg_shares=vec,
                joint_states=joint_states,
                has_discontinuous_gap_risk=has_discontinuous_gap_risk,
                min_acceptable_rr=min_acceptable_rr,
            )
            for vec in candidate_vectors
        ]
        return sorted(
            reports,
            key=lambda r: (r.is_profitable_go, r.effective_rr, r.realistic_expected_profit_usd, -r.max_loss_guarded_usd),
            reverse=True,
        )

    @staticmethod
    def verify_monotonic_value_guard(
        value_before: float,
        value_after_net_of_spread: float,
    ) -> bool:
        """Section 4.1: Rebalance / Swing is permitted ONLY if net value strictly improves after spread."""
        return value_after_net_of_spread > value_before + 1e-6
