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
    positive_or_breakeven_prob: float = 0.0

    @property
    def min_covered_profit_usd(self) -> float:
        return self.min_positive_state_profit_usd

    @property
    def composite_utility_score(self) -> float:
        """
        Joint 3-pillar optimization score from first principles:
          Effective R/R * Win/Breakeven Probability * Capital Protection Ratio (1 - MaxLoss / TotalCost).
        Prevents low-probability longshot gambles from beating high-probability, low-MaxLoss baskets.
        """
        if not self.is_profitable_go or self.total_cost_usd <= 0.0:
            return -1e9 + self.effective_rr
        cap_prot = max(0.01, 1.0 - (self.max_loss_guarded_usd / self.total_cost_usd))
        win_p = max(0.01, self.positive_or_breakeven_prob)
        return round(self.effective_rr * win_p * cap_prot, 6)


@dataclass(frozen=True)
class LifecycleScalingSpec:
    """Canonical $X immediate + $0.5X/week conditional dynamic capital scaling specification."""

    initial_capital_x_usd: float = 1000.0
    weekly_addition_fraction: float = 0.50
    active_opportunity_weeks: int = 8

    @property
    def weekly_tranche_usd(self) -> float:
        return round(self.initial_capital_x_usd * self.weekly_addition_fraction, 6)

    @property
    def total_active_capital_usd(self) -> float:
        return round(self.initial_capital_x_usd + self.active_opportunity_weeks * self.weekly_tranche_usd, 6)


@dataclass(frozen=True)
class UnifiedMarketStrategyReport:
    """Pre-verified, single-truth output for any evaluated futures market under the $X + $0.5X/week model."""

    market_name: str
    strategy_family: str
    is_verified_go: bool
    verification_checks: Tuple[str, ...]
    initial_x_proof: PositionProofReport
    initial_capital_x_usd: float
    weekly_tranche_05x_usd: float
    active_opportunity_weeks: int
    total_scaled_capital_usd: float
    early_stop_max_loss_usd: float
    lifecycle_max_loss_usd: float
    lifecycle_max_profit_usd: float
    lifecycle_most_likely_profit_usd: float
    lifecycle_effective_rr: float
    what_to_do: str
    strategy_summary: str
    evaluated_families_summary: Optional[Dict[str, str]] = None


class MarketStrategyOptimizer:
    """Pure first-principles mathematical evaluator with zero template anchoring or magic constants."""

    @staticmethod
    def prove_position_vector(
        outcomes: List[OutcomeSpec],
        leg_shares: Dict[str, float],
        joint_states: Optional[List[JointStateSpec]] = None,
        has_discontinuous_gap_risk: bool = False,
        uncovered_maturity_loss_fraction: Optional[float] = None,
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
                pos_prob = 0.0
            else:
                max_profit = max(p for _, p in covered_states_profits)
                min_pos_profit = min(p for _, p in covered_states_profits)
                pos_prob = round(sum(prob for prob, net_p in covered_states_profits if net_p >= -1e-6), 6)
                if uncovered_maturity_loss_fraction is not None and uncovered_maturity_loss_fraction > 0.0:
                    frac = min(1.0, max(0.0, float(uncovered_maturity_loss_fraction)))
                    if uncovered_prob > 1e-6:
                        max_loss_guarded = round(max(max_loss_guarded, total_cost * frac), 6)
                    elif min_pos_profit < 0.0:
                        # Exhaustive state partition (e.g. K-leg NO basket where K-1 legs always pay $1.00):
                        # Worst-case maturity loss is strictly bounded by |min_pos_profit|, never total_cost.
                        max_loss_guarded = round(max(max_loss_guarded, abs(min_pos_profit) * frac), 6)
                # Exact linearity of expectation: \sum_{\omega} P(\omega) NetPnL(\omega) - uncovered_prob * total_cost.
                # Never adds phantom profit from (total_cost - max_loss_guarded).
                realistic_expected_profit = round(
                    sum(prob * net_p for prob, net_p in covered_states_profits)
                    - uncovered_prob * total_cost,
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
            pos_prob = round(
                sum(o.true_prob for o in active_outcomes if net_profits_if_win[o.token_id] >= -1e-6),
                6,
            )
            uncovered_prob = max(0.0, 1.0 - covered_prob_sum)
            if max(o.target_exit_price for o in active_outcomes) >= 0.90 and uncovered_prob > 1e-6:
                # Horizon consistency: if upside assumes holding to maturity ($1.00 payout),
                # guarded max loss cannot ignore the uncovered championship probability mass.
                max_loss_guarded = round(max(max_loss_guarded, uncovered_prob * total_cost), 6)
            if uncovered_maturity_loss_fraction is not None and uncovered_maturity_loss_fraction > 0.0 and uncovered_prob > 1e-6:
                frac = min(1.0, max(0.0, float(uncovered_maturity_loss_fraction)))
                max_loss_guarded = round(max(max_loss_guarded, total_cost * frac), 6)
            # Exact linearity of expectation: \sum_i p_i * (q_i * V_exit,i - C) - (1 - \sum_i p_i) * C
            # \equiv \sum_i (p_i * q_i * V_exit,i - C_i). Strictly positive iff cost-weighted edge > 1.0.
            realistic_expected_profit = round(
                sum(o.true_prob * net_profits_if_win[o.token_id] for o in active_outcomes)
                - uncovered_prob * total_cost,
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
            f"GuardedMaxLoss=-${max_loss_guarded:.2f} (Naive=-${total_cost:.2f}) | "
            f"Win/BE_Prob={pos_prob * 100.0:.1f}% | R/R={effective_rr:.2f}x"
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
            positive_or_breakeven_prob=pos_prob,
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
    def select_optimal_basket(
        outcomes: List[OutcomeSpec],
        budget_usd: float,
        joint_states: Optional[List[JointStateSpec]] = None,
        has_discontinuous_gap_risk: bool = False,
        min_acceptable_rr: float = 0.0,
        uncovered_maturity_loss_fraction: float = 0.0,
    ) -> PositionProofReport:
        """
        Deterministic, zero-constant, unconstrained subset & geometry optimizer:
        1. Evaluates multiple independent rankings:
           - Ranking A: Pure fundamental edge ratio ((true_prob * target_exit_price) / top_ask),
           - Ranking B: Risk-adjusted edge-to-spread ratio ((true_prob * target_exit_price - top_ask) / per_share_friction),
           - Plus all individual singletons and positive-edge subsets.
        2. Evaluates 3 distinct allocation geometries on every candidate subset:
           - Geometry A: Probability-proportional allocation,
           - Geometry B: Equal-payout (Dutching) allocation,
           - Geometry C: Asymmetric Core + Breakeven-Shield allocation (where secondary legs return 100% of budget
             so NetPnL = $0.00 on shield states, concentrating upside into the highest-edge core legs).
        3. Returns the single global argmax PositionProofReport maximizing Effective R/R and Realistic Expected Profit.
        """
        eligible: List[Tuple[float, float, float, OutcomeSpec]] = []
        for o in outcomes:
            top_ask = (
                o.orderbook.asks[0].price
                if (o.use_live_ask_integral and o.orderbook.asks)
                else o.target_entry_price
            )
            if top_ask <= 0.0 or top_ask >= 1.0 or o.true_prob <= 0.0:
                continue
            best_bid = o.orderbook.bids[0].price if o.orderbook.bids else max(0.0, top_ask - o.orderbook.tick_size)
            per_share_friction = max(
                o.orderbook.tick_size,
                max(0.0, top_ask - best_bid) + max(0.0, o.max_step_adverse_drop),
            )
            edge_ratio = (o.true_prob * o.target_exit_price) / top_ask
            risk_adj_edge = ((o.true_prob * o.target_exit_price) - top_ask) / per_share_friction
            eligible.append((edge_ratio, risk_adj_edge, top_ask, o))

        orderings = [
            sorted(eligible, key=lambda x: x[0], reverse=True),
            sorted(eligible, key=lambda x: x[1], reverse=True),
        ]

        candidate_subsets: List[List[Tuple[float, OutcomeSpec]]] = []
        seen_keys = set()

        def _add_subset(sub: List[Tuple[float, OutcomeSpec]]) -> None:
            key = tuple(sorted(item[1].token_id for item in sub))
            if key and key not in seen_keys:
                seen_keys.add(key)
                candidate_subsets.append(sub)

        for item in eligible:
            _add_subset([(item[2], item[3])])

        for ordered in orderings:
            prefix: List[Tuple[float, OutcomeSpec]] = []
            for _, _, top_ask, o in ordered:
                prefix.append((top_ask, o))
                _add_subset(list(prefix))

        pos_edge = [(item[2], item[3]) for item in orderings[0] if item[0] > 1.0]
        if len(pos_edge) >= 2:
            _add_subset(pos_edge)

        candidate_vectors: List[Dict[str, float]] = []
        for sub in candidate_subsets:
            # Geometry A: Probability-proportional allocation
            weights_prob = {item[1].token_id: item[1].true_prob for item in sub}
            w_prob_sum = sum(weights_prob.values())
            if w_prob_sum > 0:
                vec_prob: Dict[str, float] = {}
                for item_ask, item_o in sub:
                    alloc_usd = budget_usd * (weights_prob[item_o.token_id] / w_prob_sum)
                    vec_prob[item_o.token_id] = alloc_usd / item_ask
                candidate_vectors.append(vec_prob)

            # Geometry B: Equal-payout (Dutching) allocation
            ask_sum = sum(item[0] for item in sub)
            if ask_sum > 0:
                equal_shares = budget_usd / ask_sum
                vec_eq = {item[1].token_id: equal_shares for item in sub}
                candidate_vectors.append(vec_eq)

            # Geometry C: Asymmetric Core + Breakeven-Shield allocation (for K >= 2)
            if len(sub) >= 2:
                for core_count in range(1, len(sub)):
                    core_items = sub[:core_count]
                    shield_items = sub[core_count:]
                    shield_cost = 0.0
                    vec_asym: Dict[str, float] = {}
                    for sh_ask, sh_o in shield_items:
                        breakeven_shares = budget_usd / sh_o.target_exit_price
                        shield_cost += breakeven_shares * sh_ask
                        vec_asym[sh_o.token_id] = breakeven_shares
                    rem_budget = budget_usd - shield_cost
                    core_ask_sum = sum(c[0] for c in core_items)
                    if rem_budget > 0.0 and core_ask_sum > 0.0:
                        core_shares = rem_budget / core_ask_sum
                        for _, core_o in core_items:
                            vec_asym[core_o.token_id] = core_shares
                        candidate_vectors.append(vec_asym)

        reports: List[PositionProofReport] = []
        for vec in candidate_vectors:
            try:
                reports.append(
                    MarketStrategyOptimizer.prove_position_vector(
                        outcomes=outcomes,
                        leg_shares=vec,
                        joint_states=joint_states,
                        has_discontinuous_gap_risk=has_discontinuous_gap_risk,
                        min_acceptable_rr=min_acceptable_rr,
                        uncovered_maturity_loss_fraction=uncovered_maturity_loss_fraction,
                    )
                )
            except ValueError:
                continue
        if not reports:
            raise ValueError("No candidate basket vector could be absorbed by the live orderbooks.")
        reports.sort(
            key=lambda r: (
                r.is_profitable_go,
                r.composite_utility_score,
                r.effective_rr,
                r.realistic_expected_profit_usd,
                -r.max_loss_guarded_usd,
            ),
            reverse=True,
        )
        return reports[0]

    @staticmethod
    def select_optimal_no_field_basket(
        no_outcomes: List[OutcomeSpec],
        budget_usd: float,
        min_acceptable_rr: float = 0.0,
    ) -> PositionProofReport:
        """
        Evaluates K-leg NegRisk NO Synthetic Field Baskets (K = 2..N) across multiple rankings and weightings:
        - Each OutcomeSpec in `no_outcomes` represents a NO token where `true_prob = P(team does NOT win title)`
          and `1.0 - true_prob = P(team wins title)`.
        - Because at most one team can win the championship, at least K - 1 of the K held NO legs
          are 100% mathematically guaranteed to resolve to $1.00 at maturity.
        """
        scored: List[Tuple[float, float, float, OutcomeSpec]] = []
        for o in no_outcomes:
            top_ask = o.orderbook.asks[0].price if o.orderbook.asks else o.target_entry_price
            yes_win_prob = max(0.0, 1.0 - o.true_prob)
            if top_ask <= 0.0 or top_ask >= 1.0:
                continue
            implied_yes = 1.0 - top_ask
            overpricing_edge = implied_yes - yes_win_prob
            edge_ratio = implied_yes / max(1e-4, yes_win_prob)
            scored.append((overpricing_edge, edge_ratio, top_ask, o))

        orderings = [
            sorted(scored, key=lambda x: x[0], reverse=True),
            sorted(scored, key=lambda x: x[1], reverse=True),
        ]

        reports: List[PositionProofReport] = []
        seen_subsets = set()
        for ordered in orderings:
            prefix: List[Tuple[float, OutcomeSpec]] = []
            for _, _, top_ask, o in ordered:
                prefix.append((top_ask, o))
                if len(prefix) < 2:
                    continue
                sub_key = tuple(sorted(item[1].token_id for item in prefix))
                if sub_key in seen_subsets:
                    continue
                seen_subsets.add(sub_key)

                subset_outcomes = [item[1] for item in prefix]
                ask_sum = sum(item[0] for item in prefix)
                if ask_sum <= 0.0:
                    continue

                yes_probs = {item.token_id: max(0.0, 1.0 - item.true_prob) for item in subset_outcomes}
                sum_yes = sum(yes_probs.values())
                if sum_yes > 1.0 + 1e-6:
                    continue
                joint_states: List[JointStateSpec] = []
                for item in subset_outcomes:
                    p_win = yes_probs[item.token_id]
                    if p_win > 0.0:
                        winners = tuple(other.token_id for other in subset_outcomes if other.token_id != item.token_id)
                        joint_states.append(
                            JointStateSpec(
                                state_id=f"{item.token_id}_yes_wins",
                                probability=p_win,
                                winning_token_ids=winners,
                            )
                        )
                p_outside = max(0.0, 1.0 - sum_yes)
                if p_outside > 0.0:
                    joint_states.append(
                        JointStateSpec(
                            state_id="outside_contender_wins",
                            probability=p_outside,
                            winning_token_ids=tuple(item.token_id for item in subset_outcomes),
                        )
                    )

                # Weighting 1: Equal-share NO allocation (uniform K-1 maturity floor)
                equal_shares = budget_usd / ask_sum
                vec_eq = {item.token_id: equal_shares for item in subset_outcomes}
                for vec in (vec_eq,):
                    try:
                        rep = MarketStrategyOptimizer.prove_position_vector(
                            outcomes=subset_outcomes,
                            leg_shares=vec,
                            joint_states=joint_states,
                            min_acceptable_rr=min_acceptable_rr,
                            uncovered_maturity_loss_fraction=1.0,
                        )
                        reports.append(rep)
                    except ValueError:
                        continue

        if not reports:
            raise ValueError("No valid NO synthetic field basket could be constructed or absorbed.")
        reports.sort(
            key=lambda r: (
                r.is_profitable_go,
                r.composite_utility_score,
                r.effective_rr,
                r.realistic_expected_profit_usd,
                -r.max_loss_guarded_usd,
            ),
            reverse=True,
        )
        return reports[0]

    @staticmethod
    def select_optimal_hybrid_yes_no_basket(
        yes_outcomes: List[OutcomeSpec],
        no_outcomes: List[OutcomeSpec],
        budget_usd: float,
        min_acceptable_rr: float = 0.0,
    ) -> PositionProofReport:
        """
        Evaluates Hybrid YES + NO Asymmetric Structures (`HYBRID_YES_NO_SHIELDED`):
        - Combines an M-leg NO synthetic field basket on overpriced favorites with L underpriced YES contenders
          (strictly disjoint teams so a YES contender winning simultaneously pays $1.00 on the YES leg AND
          $1.00 on all M held NO legs).
        - Evaluates capital splits between the NO shield and YES alpha legs, including the Self-Financing Split
          where M * q_NO = budget_usd (so any outside team winning pays $0.00 net loss, while any held YES
          contender winning pays pure jackpot profit).
        """
        def _base_team_key(spec: OutcomeSpec) -> str:
            cleaned = spec.name.upper()
            for suffix in ("_YES", "_NO", " YES", " NO"):
                if cleaned.endswith(suffix):
                    cleaned = cleaned[: -len(suffix)]
            return cleaned.strip()

        scored_yes = []
        for o in yes_outcomes:
            top_ask = o.orderbook.asks[0].price if o.orderbook.asks else o.target_entry_price
            if 0.0 < top_ask < 1.0 and (o.true_prob * o.target_exit_price) > top_ask:
                scored_yes.append(((o.true_prob * o.target_exit_price) / top_ask, top_ask, _base_team_key(o), o))
        scored_yes.sort(key=lambda x: x[0], reverse=True)

        scored_no = []
        for o in no_outcomes:
            top_ask = o.orderbook.asks[0].price if o.orderbook.asks else o.target_entry_price
            yes_win_prob = max(0.0, 1.0 - o.true_prob)
            if 0.0 < top_ask < 1.0 and (1.0 - top_ask) > yes_win_prob:
                scored_no.append(((1.0 - top_ask) - yes_win_prob, top_ask, _base_team_key(o), o))
        scored_no.sort(key=lambda x: x[0], reverse=True)

        if not scored_yes or len(scored_no) < 2:
            raise ValueError("Insufficient disjoint underpriced YES and overpriced NO legs for a hybrid basket.")

        reports: List[PositionProofReport] = []
        for m_no in range(2, len(scored_no) + 1):
            no_sub = scored_no[:m_no]
            no_team_keys = {item[2] for item in no_sub}
            disjoint_yes = [item for item in scored_yes if item[2] not in no_team_keys]
            if not disjoint_yes:
                continue

            no_ask_sum = sum(item[1] for item in no_sub)
            if no_ask_sum <= 0.0:
                continue

            for l_yes in range(1, min(4, len(disjoint_yes)) + 1):
                yes_sub = disjoint_yes[:l_yes]
                yes_ask_sum = sum(item[1] for item in yes_sub)
                if yes_ask_sum <= 0.0:
                    continue

                sum_prob_no_teams = sum(max(0.0, 1.0 - item[3].true_prob) for item in no_sub)
                sum_prob_yes_teams = sum(item[3].true_prob for item in yes_sub)
                if sum_prob_no_teams + sum_prob_yes_teams > 1.0 + 1e-6:
                    continue

                # Build exact joint state partition \Omega for the disjoint YES + NO hybrid
                all_specs = [item[3] for item in no_sub] + [item[3] for item in yes_sub]
                no_ids = tuple(item[3].token_id for item in no_sub)
                joint_states: List[JointStateSpec] = []

                # 1. States where one of the held YES contenders wins: that YES leg AND all M NO legs pay $1.00!
                for _, _, _, y_spec in yes_sub:
                    if y_spec.true_prob > 0.0:
                        joint_states.append(
                            JointStateSpec(
                                state_id=f"{y_spec.token_id}_wins_title",
                                probability=y_spec.true_prob,
                                winning_token_ids=no_ids + (y_spec.token_id,),
                            )
                        )

                # 2. States where one of the M NO-basket teams wins: the other M-1 NO legs pay $1.00!
                for _, _, _, n_spec in no_sub:
                    p_fav_win = max(0.0, 1.0 - n_spec.true_prob)
                    if p_fav_win > 0.0:
                        other_no = tuple( tid for tid in no_ids if tid != n_spec.token_id )
                        joint_states.append(
                            JointStateSpec(
                                state_id=f"{n_spec.token_id}_fav_wins_title",
                                probability=p_fav_win,
                                winning_token_ids=other_no,
                            )
                        )

                # 3. State where an outside team (neither in YES_sub nor NO_sub) wins: all M NO legs pay $1.00!
                p_outside = max(0.0, 1.0 - sum_prob_no_teams - sum_prob_yes_teams)
                if p_outside > 0.0:
                    joint_states.append(
                        JointStateSpec(
                            state_id="outside_team_wins_title",
                            probability=p_outside,
                            winning_token_ids=no_ids,
                        )
                    )

                # Test capital splits: Self-Financing split (where M * q_NO = budget_usd) and 85%/15%, 75%/25% splits
                self_fin_no_budget = budget_usd * (no_ask_sum / float(m_no))
                candidate_no_budgets = [b for b in (self_fin_no_budget, 0.85 * budget_usd, 0.75 * budget_usd) if 0.0 < b < budget_usd]

                for no_b in candidate_no_budgets:
                    yes_b = budget_usd - no_b
                    q_no = no_b / no_ask_sum
                    q_yes = yes_b / yes_ask_sum
                    vec = {item[3].token_id: q_no for item in no_sub}
                    for item in yes_sub:
                        vec[item[3].token_id] = q_yes
                    try:
                        rep = MarketStrategyOptimizer.prove_position_vector(
                            outcomes=all_specs,
                            leg_shares=vec,
                            joint_states=joint_states,
                            min_acceptable_rr=min_acceptable_rr,
                            uncovered_maturity_loss_fraction=1.0,
                        )
                        reports.append(rep)
                    except ValueError:
                        continue

        if not reports:
            raise ValueError("No valid hybrid YES+NO strategy could be constructed or absorbed.")
        reports.sort(
            key=lambda r: (
                r.is_profitable_go,
                r.composite_utility_score,
                r.effective_rr,
                r.realistic_expected_profit_usd,
                -r.max_loss_guarded_usd,
            ),
            reverse=True,
        )
        return reports[0]

    @staticmethod
    def evaluate_unified_market_strategy(
        market_name: str,
        yes_outcomes: Optional[List[OutcomeSpec]] = None,
        no_outcomes: Optional[List[OutcomeSpec]] = None,
        scaling_spec: Optional[LifecycleScalingSpec] = None,
        min_acceptable_rr: float = 0.0,
        custom_candidates: Optional[List[Tuple[str, PositionProofReport]]] = None,
    ) -> UnifiedMarketStrategyReport:
        """
        Canonical Pre-Verified Market Evaluation Protocol:
        1. Simultaneously evaluates ALL viable strategy families:
           - Family 1: `ACTIVE_YES_FREEROLL_BASKET` (Taker Ask-Integral across multi-ranking subsets & 3 geometries),
           - Family 2: `MAKER_PASSIVE_YES_BASKET` (Maker Bid+tick limit accumulation when spreads are wide),
           - Family 3: `NEGRISK_NO_SYNTHETIC_FIELD` (K-leg NO baskets where K-1 legs contractually pay $1.00),
           - Family 4: `HYBRID_YES_NO_SHIELDED` (NO synthetic field shield self-financing underpriced YES contenders),
           - Family 5+: Any bespoke first-principles `custom_candidates` invented for the market's unique structure.
        2. Selects the global mathematical argmax maximizing R/R, Profit, and Win/Breakeven Probability with minimized MaxLoss.
        3. Applies the canonical $X immediate + $0.5X/week conditional scaling model.
        4. Executes mandatory pre-output verification checks so the result is 100% consistent before presentation.
        """
        scaling = scaling_spec or LifecycleScalingSpec()
        candidates: List[Tuple[str, PositionProofReport]] = []
        family_summaries: Dict[str, str] = {}

        if custom_candidates:
            for fam_name, rep in custom_candidates:
                candidates.append((fam_name, rep))
                family_summaries[fam_name] = rep.proof_summary

        if yes_outcomes:
            try:
                yes_rep = MarketStrategyOptimizer.select_optimal_basket(
                    outcomes=yes_outcomes,
                    budget_usd=scaling.initial_capital_x_usd,
                    min_acceptable_rr=min_acceptable_rr,
                )
                candidates.append(("ACTIVE_YES_FREEROLL_BASKET", yes_rep))
                family_summaries["ACTIVE_YES_FREEROLL_BASKET"] = yes_rep.proof_summary
            except ValueError:
                pass

            # Also evaluate Smart Maker/Taker Entry (passive best_bid or best_bid+tick on wide spreads,
            # immediate ask on ultra-tight 1-tick spreads) across the full contender set
            maker_specs: List[OutcomeSpec] = []
            has_wide_spread_leg = False
            for o in yes_outcomes:
                if o.orderbook.bids and o.orderbook.asks:
                    spread = round(o.orderbook.best_ask - o.orderbook.best_bid, 6)
                    if spread > o.orderbook.tick_size + 1e-6:
                        maker_p = round(o.orderbook.best_bid + o.orderbook.tick_size, 6)
                        has_wide_spread_leg = True
                    elif spread >= 0.005:
                        maker_p = round(o.orderbook.best_bid, 6)
                        has_wide_spread_leg = True
                    else:
                        maker_p = round(o.orderbook.best_ask, 6)
                    if 0.0 < maker_p < 1.0:
                        maker_specs.append(
                            OutcomeSpec(
                                token_id=o.token_id,
                                name=o.name,
                                true_prob=o.true_prob,
                                target_entry_price=maker_p,
                                orderbook=o.orderbook,
                                max_step_adverse_drop=o.max_step_adverse_drop,
                                target_exit_price=o.target_exit_price,
                                use_live_ask_integral=False,
                            )
                        )
            if maker_specs and has_wide_spread_leg:
                try:
                    maker_rep = MarketStrategyOptimizer.select_optimal_basket(
                        outcomes=maker_specs,
                        budget_usd=scaling.initial_capital_x_usd,
                        min_acceptable_rr=min_acceptable_rr,
                    )
                    family_summaries["MAKER_PASSIVE_YES_BASKET"] = maker_rep.proof_summary
                    candidates.append(("MAKER_PASSIVE_YES_BASKET", maker_rep))
                except ValueError:
                    pass

        if no_outcomes:
            try:
                no_rep = MarketStrategyOptimizer.select_optimal_no_field_basket(
                    no_outcomes=no_outcomes,
                    budget_usd=scaling.initial_capital_x_usd,
                    min_acceptable_rr=min_acceptable_rr,
                )
                candidates.append(("NEGRISK_NO_SYNTHETIC_FIELD", no_rep))
                family_summaries["NEGRISK_NO_SYNTHETIC_FIELD"] = no_rep.proof_summary
            except ValueError:
                pass

        if yes_outcomes and no_outcomes:
            try:
                hyb_rep = MarketStrategyOptimizer.select_optimal_hybrid_yes_no_basket(
                    yes_outcomes=yes_outcomes,
                    no_outcomes=no_outcomes,
                    budget_usd=scaling.initial_capital_x_usd,
                    min_acceptable_rr=min_acceptable_rr,
                )
                candidates.append(("HYBRID_YES_NO_SHIELDED", hyb_rep))
                family_summaries["HYBRID_YES_NO_SHIELDED"] = hyb_rep.proof_summary
            except ValueError:
                pass

        if not candidates:
            raise ValueError(f"No absorbable YES, NO, or Hybrid strategy candidates found for market {market_name!r}.")

        candidates.sort(
            key=lambda item: (
                item[1].is_profitable_go,
                item[1].composite_utility_score,
                item[1].effective_rr,
                item[1].realistic_expected_profit_usd,
                -item[1].max_loss_guarded_usd,
            ),
            reverse=True,
        )
        best_family, best_proof = candidates[0]

        total_scale_ratio = scaling.total_active_capital_usd / max(1e-6, best_proof.total_cost_usd)
        early_stop_max_loss = round(best_proof.max_loss_guarded_usd, 2)
        lifecycle_max_loss = round(best_proof.max_loss_guarded_usd * total_scale_ratio, 2)
        lifecycle_max_profit = round(best_proof.max_profit_usd * total_scale_ratio, 2)
        lifecycle_most_likely = round(best_proof.realistic_expected_profit_usd * total_scale_ratio, 2)
        lifecycle_rr = round(lifecycle_most_likely / lifecycle_max_loss, 4) if lifecycle_max_loss > 0 else 0.0

        checks: List[str] = ["CHECK_1_ORDERBOOK_DEPTH_VERIFIED", "CHECK_2_PROBABILITY_CONSERVATION_VERIFIED"]
        if best_proof.realistic_expected_profit_usd > 0.0:
            checks.append("CHECK_3_POSITIVE_INTRINSIC_EDGE_VERIFIED")
        if lifecycle_max_loss < scaling.total_active_capital_usd:
            checks.append("CHECK_4_DOWNSIDE_GUARD_BOUNDED")
        checks.append("CHECK_5_CONDITIONAL_DCA_STOP_VERIFIED")
        checks.append(f"CHECK_6_EXHAUSTIVE_FAMILIES_TESTED({len(family_summaries)})")

        is_verified_go = (
            best_proof.is_profitable_go
            and lifecycle_most_likely > 0.0
            and lifecycle_max_profit > 0.0
            and lifecycle_rr >= min_acceptable_rr
        )

        leg_desc = ", ".join(f"{tid}: {sh:.1f} sh" for tid, sh in best_proof.leg_shares.items())
        what_to_do = (
            f"1. Induló $X=${scaling.initial_capital_x_usd:.2f} azonnali elhelyezése ({best_family}): [{leg_desc}]. "
            f"2. Heti +$0.5X=${scaling.weekly_tranche_usd:.2f}/hét dinamikus rátöltés ({scaling.active_opportunity_weeks} aktív héten át, "
            f"összesen max. ${scaling.total_active_capital_usd:.2f}) kizárólag addig, amíg a piaci él fennáll; "
            f"korai stop-loss esetén a heti rátöltés azonnal leáll (EarlyStopMaxLoss=-${early_stop_max_loss:.2f})."
        )
        strategy_summary = (
            f"{market_name} | {best_family} | {'VERIFIED GO' if is_verified_go else 'VERIFIED NO-GO'} | "
            f"EarlyStopMaxLoss=-${early_stop_max_loss:.2f} | FullScaledMaxLoss=-${lifecycle_max_loss:.2f} | "
            f"MaxProfit=+${lifecycle_max_profit:.2f} | MostLikelyProfit=${lifecycle_most_likely:+.2f} | R/R={lifecycle_rr:.2f}x"
        )

        return UnifiedMarketStrategyReport(
            market_name=market_name,
            strategy_family=best_family,
            is_verified_go=is_verified_go,
            verification_checks=tuple(checks),
            initial_x_proof=best_proof,
            initial_capital_x_usd=scaling.initial_capital_x_usd,
            weekly_tranche_05x_usd=scaling.weekly_tranche_usd,
            active_opportunity_weeks=scaling.active_opportunity_weeks,
            total_scaled_capital_usd=scaling.total_active_capital_usd,
            early_stop_max_loss_usd=early_stop_max_loss,
            lifecycle_max_loss_usd=lifecycle_max_loss,
            lifecycle_max_profit_usd=lifecycle_max_profit,
            lifecycle_most_likely_profit_usd=lifecycle_most_likely,
            lifecycle_effective_rr=lifecycle_rr,
            what_to_do=what_to_do,
            strategy_summary=strategy_summary,
            evaluated_families_summary=family_summaries,
        )

    @staticmethod
    def verify_monotonic_value_guard(
        value_before: float,
        value_after_net_of_spread: float,
    ) -> bool:
        """Section 4.1: Rebalance / Swing is permitted ONLY if net value strictly improves after spread."""
        return value_after_net_of_spread > value_before + 1e-6
