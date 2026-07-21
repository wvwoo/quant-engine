# 📊 QTS PAPER TRADING REPORT

> **PAPER TRADING — MODELED FILLS.** No real order was placed. Fills are
> simulated at quote +/- 100bp slippage with 65c/contract commission
> (config provenance: ASSUMPTION). Nothing here is investment advice or a
> promise of returns.

## 🌐 TELEMETRY & ROUTING METADATA
* **Session Date:** `2026-07-21`
* **Engine:** `qts-core v0.1.0 / paper`
* **Decisions Evaluated:** `1`
* **Orders (journaled/filled):** `0`

## 🔍 ENTRY CHECKLIST AUDIT

| Time | Symbol | Verdict | Failed checks |
| :--- | :--- | :--- | :--- |
| 14:14 | SPY | 🔴 VETOED | orb_breakout, vwap_alignment, rvol |

## 🛡️ RISK PARAMETERS IN FORCE

| Parameter | Value | Provenance |
| :--- | :--- | :--- |
| **sub_portfolio** | $850.00 | SPEC |
| **stop_loss** | -25% of entry quote | SPEC |
| **tranche1** | +100% (sell 1) | SPEC |
| **trailing** | 12% from peak | SPEC |
| **runner_floor** | +10% of entry quote | SPEC |
| **force_flat** | 15:30 ET | SAFETY |
| **daily_loss_limit** | $212.50 | SAFETY |

## 📈 SESSION P&L (PAPER, MODELED)

* **Realized P&L today:** **$0.00**

---
*Assumptions: paper fills at quote±100bp, commission 65c/contract/side, marks at option mid. Cherry-picking of sessions is prohibited; this report covers one full session as recorded in the state store.*
