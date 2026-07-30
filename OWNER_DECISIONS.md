# Owner decisions — measured, presented, NOT executed

Every item below is a decision I deliberately did not make. Each carries the
numbers I measured myself on 2026-07-30 (not quoted from the brain), the options,
a recommendation with its reasoning, and what each option costs.

**Nothing here has been applied.** No threshold moved, no flag flipped, no agent
installed, no subscription bought, no secret rotated, no branch pushed.

---

## 1. RVOL — and the question is not the one we thought (P0, blocking)

### What I measured

Recomputed by me from `decisions.decision_json` in `qts_v8/state/paper.db`
(2 live sessions: 2026-07-21 and 2026-07-22).

**121 decisions. 0 approved. 0 orders. Ever.**

31 of the 121 rows contain at least one check whose value is `n/a` — degraded
views, from the zero-bar path now fixed as A-08. So the honest denominator is the
**90 fully-valued decisions**, and both bases are shown because the difference
changes which gate looks binding:

| Gate | veto rate over all 121 | **over the 90 fully-valued** |
|---|---|---|
| `rvol` | 96.7% | **95.6%** |
| `trading_window` | 84.3% | 78.9% |
| `macd_histogram` | 70.2% | 60.0% |
| `contract_gates` | 62.0% | 48.9% |
| `rsi_non_overbought` | 34.7% | 12.2% |
| `vwap_alignment` | 31.4% | 7.8% |
| `orb_breakout` | 27.3% | **2.2%** |

Observed RVOL on those 90 steps: **min 0.20 · median 0.68 · p90 1.46 · max 3.76**.

**`rvol >= 2.0` occurred 4 times (4.4%).** The gate is *rare*, not unreachable —
which contradicts the intuition that 2.0 is simply impossible on real data.

### The finding that reframes the decision

Every one of those 4 steps was blocked by **something else**:

| Session | ET | rvol | what actually blocked it |
|---|---|---|---|
| 2026-07-22 | 11:11 | 2.90 | `rsi_non_overbought`, `contract_gates` |
| 2026-07-22 | 11:16 | 3.76 | `rsi_non_overbought`, `contract_gates` |
| 2026-07-22 | 11:22 | 3.46 | `rsi_non_overbought`, `contract_gates` |
| 2026-07-22 | 11:59 | 2.13 | `trading_window`, `rsi_non_overbought`, `contract_gates` |

Steps passing all 7 gates: **0**.

On every step where volume genuinely expanded past 2.0, **RSI was overbought**.
That is mechanically expected rather than coincidental: a volume surge that breaks
the opening range *is* momentum, so `RVOL > 2.0` and `RSI < 70` pull against each
other on the same bar. Note also that `rsi_non_overbought` vetoes only **12.2%**
of all fully-valued steps but **100%** of the high-RVOL ones — it is a rare veto
that happens to fire exactly when the primary trigger fires.

**So the question is not "is 2.0 too high".** It is:

> Is `RVOL > 2.0 AND RSI < 70` jointly satisfiable as written, or does the
> reference report describe one exceptional bar rather than an operating rule?

The reference report's own example (`institutional_quant_trading_report.md`) shows
RVOL 240% *with* RSI 58.5 simultaneously — so the combination is not impossible in
principle. What we have not established is how often it occurs. Two sessions is
not a sample.

### Options

| Option | What it means | Cost / risk |
|---|---|---|
| **A. Change nothing** | 2.0 is a deliberately rare gate. Keep measuring. | The system continues not to trade. Honest, and cheap. |
| **B. Re-examine the DEFINITION, not the value** (recommended) | `rvol_mode` is tagged ASSUMPTION, resolved by measurement over 577 bars. Re-derive whether "RSI < 70 on a breakout bar" is the right *shape* of filter — e.g. is the report's "non-overbought" about the 5m bar or a slower frame? | Analysis only. Touches an ASSUMPTION-tier field with a written derivation, not a SPEC number. |
| **C. Out-of-sample measurement first** | Run `run_backtest --diagnose` over a longer window and more symbols, and count how often RVOL≥2.0 AND RSI<70 co-occur. | Time. Answers B with data instead of argument. |
| **D. Lower the threshold** | — | **Refused.** Loosening a threshold to manufacture trades is curve-fitting. Not implemented, not recommended, and I will not do it on request without a measured justification and a PROVENANCE update. |

**Recommendation: C then B.** Measure the co-occurrence rate before touching
anything. If RVOL≥2.0 ∧ RSI<70 turns out to be near-zero across a year, the
conflict is in the spec and that is a finding worth an ADR — not a number to nudge.

**Stated plainly:** the system is now fully activated in the operational sense —
one key, one command, an unattended day. **It will still place zero trades until
this is resolved.** That is not a defect being hidden; it is the measurement.

---

## 2. Target universe

**Measured (2026-07-21, and unchanged):** NVDA lists 0DTE only Mon/Wed/Fri
(22 expiries observed); SPY and QQQ list dailies (32). Gate B4 checks this every
symbol every day and halts cleanly — `[halt] NVDA: no 0DTE expiry listed`.

Note from §1: all four high-RVOL steps were **NVDA**, the symbol with the least
0DTE coverage.

| Option | Cost |
|---|---|
| Keep SPY, QQQ, NVDA (current) | Two of three days NVDA contributes nothing but a halt line. |
| SPY/QQQ only | Simpler; loses the symbol that produced every high-RVOL reading so far. |
| Add more dailies (IWM, TSLA…) | Each needs a `tick_schedules` entry with provenance, or it raises by design (N-08). |

**Recommendation: keep the current three until §1 is resolved**, because NVDA is
where the signal-like behaviour actually appeared.

---

## 3. Data upgrade — and a recorded decision whose premise changed

All prices verified on vendor pages **2026-07-30**; full matrix and sources in
[KEY_ACQUISITION.md](KEY_ACQUISITION.md).

**Barrier B2** = no real historical intraday option premiums, so every backtest
number is tagged `MODELED`. Only minute-level historical option premiums lift it.

| Provider | Minute historical option premiums | Price today | stdlib-only? |
|---|---|---|---|
| yfinance (current) | ✗ | $0 | ✅ |
| Alpaca free | ✅ but only since **Feb 2024** | $0 | ✅ REST + 2 headers |
| **Databento** | ✅ `cbbo-1m` from **Apr 2013** | **$0.04/GB, $125 free credits (6 mo)** | ✅ plain HTTPS, **no daemon** |
| ThetaData Value | ✅ 1-min from 2020-01-01 | **$40/mo (ADR-007 CONFIRMED)** | ❌ needs **Java 21** Theta Terminal, or a gRPC+Polars library |
| Massive (ex-Polygon) | ✅ from Jun 2014 | real-time options needs **$199/mo** | ✅ REST |

### Two conflicts with the recorded record — surfaced, not overridden

1. **ADR-007 names ThetaData as the upgrade path.** Its $40 price is confirmed
   correct. What changed is our knowledge: ThetaData requires a local Java
   daemon (or a gRPC + Polars/Pandas client), both of which violate "stdlib
   first, no new dependency without a written reason" — and its greeks are
   vendor-calculated Black-Scholes, which is precisely what **ADR-002 refuses**.
   Databento is plain REST with no daemon, reaches back to 2013, can be trialled
   for $0 on free credits, and **explicitly declines to publish vendor greeks**,
   matching ADR-002 instead of fighting it.
2. **ADR-007's premise is now outdated.** It states there is *no* free source of
   intraday historical option prices. Alpaca offers minute option bars since
   Feb 2024 on the free indicative tier.

**Recommendation:** for B2, **Databento historical OPRA, not ThetaData** — and
because that contradicts a recorded ADR, it needs an owner decision and a new ADR
recording the reasoning. A draft is ready. **No ADR is ever deleted.**

---

## 4. Enable `staleness_gate_enabled`?

**Currently OFF**, with the documented reason: "until the age distribution has
been measured on a real session".

**That premise could not be discharged until today.** `data_age_min` was measured
on every step and stored nowhere (A-06) — the only consumer was a `print()`, and
the last unattended run's stdout is gone (its log file contains a single
pre-open line). Known data points are two ad-hoc readings: **3.1–4.9 minutes**.

Now: the value is persisted per decision, `qts doctor` and `qts status` summarise
the distribution, and the launchd agent writes a dated log.

| Option | Cost |
|---|---|
| **Leave OFF for one instrumented day** (recommended) | One session. Then the threshold is chosen from a real distribution instead of two readings. |
| Turn ON now at 25 min | Untested against a real distribution; if the real p99 is above 25 min it silently starts refusing entries. |
| Turn ON at a measured p99 + margin | Requires the day above first. |

**Recommendation: leave OFF, collect one instrumented day, then decide.** Note it
is an entry-only gate by design — refusing stale data must never strand an open
position.

---

## 5. Install the launchd agent — and fix machine sleep (now blocking)

`qts install-agent` generates the plist; **`--dry-run` is the default** and I have
installed nothing. Verified: the file it would write is well-formed, the schedule
is derived from the XNYS calendar (09:30 EDT = 16:30 Riyadh → fires 15:45 local),
there is no `KeepAlive` to cause a relaunch loop, and the environment is passed
explicitly because a LaunchAgent inherits none of the login shell.

**Measured blocker:** `pmset -g` reports **`sleep 1`** — this Mac sleeps after one
minute of inactivity. **An unattended trading day is impossible until that
changes.** A sleeping Mac cannot poll a provider; `WAIT_FOR_OPEN` recomputes from
the wall clock on wake, so it self-corrects the *schedule*, but it cannot trade
while asleep. This is very likely why the 2026-07-22 session started at **11:06
ET** instead of the 09:30 bell — missing 80 of the 104 minutes of the entry
window (see §7).

Owner actions, in order:
1. `caffeinate -s` for the session, or set `sleep 0` in System Settings.
2. `qts install-agent --install`, then `launchctl load ~/Library/LaunchAgents/sa.com.execlogic.qts.plist`.
3. `qts doctor` to confirm the agent is loaded and sleep is disabled.

I do not change power settings and do not load agents.

---

## 6. Rotate the leaked NVIDIA key (ADR-009, still open)

ADR-009 records the key as scrubbed from git before the first commit, with
**rotation left as an owner action**. It is still pending, and I confirmed the
variable name `NVIDIA_API_KEY` is still present in `~/.config/secrets.env`
(names only — I did not read or print any value).

The key has no role in this system; `nvidia_backend.py` was deleted. The exposure
is historical (it sat in plaintext on disk), so rotation remains the only fix.
**Owner action. Nothing to do in code.**

---

## 7. Disk — I was wrong once, here is the measured figure

I first reported this item as resolved. **That was wrong**: I had read `df /`,
which reports the read-only system snapshot volume (41% used), not the data
volume.

**Correct measurement (2026-07-30):** data volume `/System/Volumes/Data` is
**92% used with 17.2 GiB free** of 228 GiB. The recorded claim was 96% / 8.9 GiB.

So: improved (free space roughly doubled), still high. 17 GiB is comfortably
enough for a trading day's logs, WAL and reports, and `qts doctor` now measures it
every run and BLOCKS below 2 GiB. **Not a blocker; not "no action needed" either.**

---

## 8. Two ADRs are both numbered ADR-012

`decisions.md` contains two decisions numbered **ADR-012**, both dated 2026-07-22:
the canonical-path decision (`decisions.md:233`) and the Alpaca PAPER bridge
(`decisions.md:259`). Any future reference to "ADR-012" is ambiguous.

**Proposal:** renumber the *second* (the Alpaca bridge) to **ADR-013**, keeping a
pointer note at the old position so existing references still resolve. The
canonical-path decision keeps 012 because it is referenced more widely.

**No ADR is ever deleted, and I have not renumbered anything** — the numbering of
the decision record is the owner's.

---

## 9. The red line — real money

Unchanged and untouched. `require_paper_mode` is unmodified; the Alpaca broker
base URL is still a hardcoded module constant pinned by a test; the new market-data
client is structurally incapable of placing an order (GET-only, asserted, and a
test proves the module contains no orders endpoint).

**If real money is ever raised in any form:** I will not write live-order code,
open an account, move funds, enter credentials, or flip a flag. What I would
produce instead is `LIVE_REQUIREMENTS.md` — options approval level, the PDT rule,
broker requirements, tax treatment, risk limits — whose first line must be the
honest one:

> **This system has placed zero trades in its entire history. There is therefore
> zero empirical evidence for the strategy — not weak evidence, none.**

Plus a full draft ADR for signature in a separate session. ADR-012 forbids
building a live broker path, and overriding it needs an explicit recorded decision
**before** any code.

---

## Summary — what is blocking what

| # | Item | Blocks | Owner action |
|---|---|---|---|
| 1 | RVOL / RSI joint satisfiability | **any trade ever** | decide after out-of-sample measurement |
| 5 | `pmset sleep=1` | **any unattended day** | disable sleep |
| 5 | launchd agent not installed | unattended day | `--install` then `launchctl load` |
| 4 | staleness gate OFF | nothing (measuring) | decide after one instrumented day |
| 3 | B2 / MODELED backtests | backtest credibility | pick a provider; needs a new ADR |
| 6 | NVIDIA key rotation | nothing in this system | rotate |
| 8 | ADR-012 collision | record clarity | approve renumbering |
| 7 | disk 92% used | nothing yet | monitor; doctor blocks below 2 GiB |
