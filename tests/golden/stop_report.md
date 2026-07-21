# 📊 QTS PAPER TRADING REPORT

> **PAPER TRADING — MODELED FILLS.** No real order was placed. Fills are
> simulated at quote +/- 100bp slippage with 0c/contract commission
> (config provenance: ASSUMPTION). Nothing here is investment advice or a
> promise of returns.

## 🌐 TELEMETRY & ROUTING METADATA
* **Session Date:** `2026-06-17`
* **Engine:** `qts-core v0.1.0 / paper`
* **Decisions Evaluated:** `1`
* **Orders (journaled/filled):** `2`

## 🔍 ENTRY CHECKLIST AUDIT

| Time (ET) | Symbol | Verdict | Failed checks |
| :--- | :--- | :--- | :--- |
| 10:15 | NVDA | 🟢 APPROVED | — |

### Last approved decision — full checklist

| Check | Value | Threshold | Status |
| :--- | :--- | :--- | :--- |
| **trading_window** | 10:15 ET | 09:46-11:30 ET | 🟢 PASS |
| **orb_breakout** | close 204.22 vs ORB high 204.10 | close > ORB high | 🟢 PASS |
| **vwap_alignment** | close 204.22 vs VWAP 203.86 | close > session VWAP | 🟢 PASS |
| **rvol** | 3.00 | >= 2.0 (per_bar, N=20 of 20d) | 🟢 PASS |
| **rsi_non_overbought** | 67.0 | < 70 (Wilder-14, ASSUMPTION) | 🟢 PASS |
| **macd_histogram** | 0.0544 | > 0 (12/26/9, ASSUMPTION) | 🟢 PASS |
| **contract_gates** | NVDA260617C00205000 | 0DTE 0.45<=delta<=0.55, IV<95%, spread<=5c & <=350bp | 🟢 PASS |

Selected: `NVDA260617C00205000` — delta 0.4806 (IV 67.79%, source: computed), mid $1.32, spread $0.03

## 🧮 ORDER LEDGER (MODELED FILLS)

| Seq | Side | Reason | Contracts | Limit | Fill | Cash Δ / Realized | Status |
| :-- | :-- | :-- | :-- | :-- | :-- | :-- | :-- |
| 0 | BUY | ENTRY | 6 | $1.34 | $1.36 | -$816.00 | FILLED |
| 1 | SELL | STOP | 6 | $1.00 | $0.96 | -$240.00 | FILLED |

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

* **Realized P&L today:** **-$240.00**
* **Cash:** $610.00 | **Open position value (mark):** $0.00
* **Equity snapshots:** 2

---
*Assumptions: paper fills at quote±100bp, commission 0c/contract/side, marks at option mid. Cherry-picking of sessions is prohibited; this report covers one full session as recorded in the state store.*
