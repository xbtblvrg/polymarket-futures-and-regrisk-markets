# Autonomous Polymarket Multi-Option, Futures & NegRisk Quant Engine

Önálló, 100%-ban izolált autonóm kvantitatív kereskedési és pozíciómenedzselő rendszer bármely Polymarket CLOB esemény és piac első-elvű, bizonyítás-alapú elemzésére és végrehajtására.

## Alapelvek és Alkotmány
- **Mesterterv és Rendszeralkotmány:** [`docs/POLYMARKET_AUTONOMOUS_FUTURES_MASTER_PLAN.md`](docs/POLYMARKET_AUTONOMOUS_FUTURES_MASTER_PLAN.md), [`AGENTS.md`](AGENTS.md) és [`GEMINI.md`](GEMINI.md)
- **100% Önálló Architektúra:** Saját végrehajtó motorral (`src/pm_markets_engine/`), piaconként különálló SQLite adatbázisokkal (`data/pm_markets/<market_slug>.sqlite3`) és központi tárca-zsilippel (`data/pm_markets/_wallet_guard.sqlite3`) működik.
- **Zero-Anchoring, Zero-Memory & Proof-First Evaluation:** Minden új piac elemzése tiszta lappal (`Tabula Rasa`), kizárólag élő piaci tényadatokból, a valós könyvmélység-integrálokból, a hátralévő időtartamból ($T - t_0$) és a megvalósítás szigorú matematikai bizonyításából indul ki.
