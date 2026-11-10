# Autonomous Polymarket Multi-Option, Futures & NegRisk Quant Engine

Önálló, 100%-ban izolált autonóm kvantitatív kereskedési és pozíciómenedzselő rendszer többopciós (`NegRisk`), határidős (`Futures`), korlát- (`Barrier`) és egyedi Polymarket CLOB piacokra.

## Alapelvek és Alkotmány
- **Mesterterv és Rendszeralkotmány:** [`docs/POLYMARKET_AUTONOMOUS_FUTURES_MASTER_PLAN.md`](docs/POLYMARKET_AUTONOMOUS_FUTURES_MASTER_PLAN.md) és [`AGENTS.md`](AGENTS.md)
- **100% Önálló Architektúra:** Saját végrehajtó motorral (`src/pm_markets_engine/`), piaconként különálló SQLite adatbázisokkal (`data/pm_markets/<market_slug>.sqlite3`) és központi tárca-zsilippel (`data/pm_markets/_wallet_guard.sqlite3`) működik.
- **Zero-Anchoring & Reality-First Evaluation:** Minden új piac elemzése a nyers piaci tényadatokból, a valós könyvmélységből, a hátralévő időtartamból ($T - t_0$) és a megvalósítás szigorú realitásából indul ki.
