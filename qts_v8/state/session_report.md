# 📊 QTS PAPER TRADING REPORT

> **PAPER TRADING — MODELED FILLS.** No real order was placed. Fills are
> simulated at quote +/- 100bp slippage with 65c/contract commission
> (config provenance: ASSUMPTION). Nothing here is investment advice or a
> promise of returns.

## 🌐 TELEMETRY & ROUTING METADATA
* **Session Date:** `2026-07-21`
* **Engine:** `qts-core v0.1.0 / paper`
* **Decisions Evaluated:** `18`
* **Orders (journaled/filled):** `0`

## 🔍 ENTRY CHECKLIST AUDIT

| Time (ET) | Symbol | Verdict | Failed checks |
| :--- | :--- | :--- | :--- |
| 10:45 | SPY | 🔴 VETOED | orb_breakout, rvol |
| 10:45 | SPY | 🔴 VETOED | orb_breakout, rvol |
| 11:13 | SPY | 🔴 VETOED | rvol |
| 11:13 | QQQ | 🔴 VETOED | rvol |
| 15:08 | SPY | 🔴 VETOED | trading_window, rvol, macd_histogram, contract_gates |
| 15:08 | QQQ | 🔴 VETOED | trading_window, rvol, macd_histogram |
| 15:23 | SPY | 🔴 VETOED | trading_window, rvol, macd_histogram, contract_gates |
| 15:23 | QQQ | 🔴 VETOED | trading_window, rvol, macd_histogram |
| 15:23 | SPY | 🔴 VETOED | trading_window, rvol, macd_histogram, contract_gates |
| 15:23 | QQQ | 🔴 VETOED | trading_window, rvol, macd_histogram |
| 15:24 | SPY | 🔴 VETOED | trading_window, rvol, macd_histogram, contract_gates |
| 15:24 | QQQ | 🔴 VETOED | trading_window, rvol, macd_histogram |
| 15:25 | SPY | 🔴 VETOED | trading_window, rvol, macd_histogram, contract_gates |
| 15:25 | QQQ | 🔴 VETOED | trading_window, rvol, macd_histogram, contract_gates |
| 15:26 | SPY | 🔴 VETOED | trading_window, rvol, macd_histogram, contract_gates |
| 15:26 | QQQ | 🔴 VETOED | trading_window, rvol, macd_histogram, contract_gates |
| 15:27 | SPY | 🔴 VETOED | trading_window, rvol, macd_histogram, contract_gates |
| 15:27 | QQQ | 🔴 VETOED | trading_window, rvol, macd_histogram, contract_gates |

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
* **Cash:** $850.00 | **Open position value (mark):** $0.00
* **Equity snapshots:** 7

---
*Assumptions: paper fills at quote±100bp, commission 65c/contract/side, marks at option mid. Cherry-picking of sessions is prohibited; this report covers one full session as recorded in the state store.*
