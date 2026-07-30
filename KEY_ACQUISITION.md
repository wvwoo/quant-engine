# KEY_ACQUISITION — market-data and broker credentials for the 0DTE paper system

**Verification date for every fact on this page: 2026-07-30.** Every quantitative claim carries its
source URL inline. Claims that could not be verified from the documentation consulted on that date
are tagged with the literal word `UNVERIFIED` and are collected in
[Appendix A — UNVERIFIED register](#appendix-a--unverified-register). Nothing on this page is an
extrapolation: if a number is not sourced, it is not here.

Canonical repo: `~/qts`. This document lives in the activation worktree
`~/qts-activation-wt`.

---

## 0. Rules of engagement (read once, then do not deviate)

1. **The owner creates the accounts.** No agent, script, or assistant signs up, accepts terms, or
   generates credentials on the owner's behalf. Every section below is written as a checklist for
   the owner to execute personally in a browser.
2. **A key value never enters a chat window.** Do not paste a key id or secret into this or any
   other conversation, into a commit message, into an issue, into a screenshot, or into a log.
   The only destination for a key value is the owner's own local file `~/.config/secrets.env`,
   opened in the owner's own editor.
3. **Nobody is asked to share a key.** No section here requests a key value, a redacted key, a
   prefix, a length, or a "just the last four" — those are all key material.
4. **If a key value is ever exposed** (pasted into a chat, committed, screenshotted), the owner
   revokes and regenerates it in the provider dashboard. Regeneration is always cheaper than
   reasoning about blast radius.
5. **`~/.config/secrets.env` must be `chmod 600`.** The loader refuses to read the file if the mode
   is looser. This is a hard precondition, not a recommendation.
6. **Verification is done by `qts doctor`**, which prints verdicts only (present / absent /
   reachable / rejected) and never prints key values.

### Two project constraints that shape every recommendation below

| Constraint | What it says | Consequence for provider selection |
|---|---|---|
| **ADR-002** | Greeks are **not** part of the provider interface. Delta and IV are computed locally by our own `PricingEngine`. (Recorded in `~/qts/README.md` line 50; enforced at `~/qts/qts_core/pricing.py` line 3.) | "Provider returns greeks" is **IRRELEVANT** to this system. It is not a plus, it is not a tiebreaker, and paying for it buys nothing. A provider that *refuses* to publish vendor greeks is *aligned*, not deficient. |
| **Barrier B2** | We have no real historical intraday option premiums, so **every backtest number is tagged `MODELED`** (recorded against ADR-007, `~/qts/README.md` line 51). | **Only minute-level historical option premium data lifts B2.** Real-time quotes, greeks, chains, and equity bars do not lift B2 — however useful they are for other reasons. |

### One safety fact about the broker bridge

The Alpaca broker bridge is already built and is waiting only for a key. Its base URL is
**hardcoded to `https://paper-api.alpaca.markets` and is not configurable**. The system therefore
cannot be pointed at a live trading endpoint by editing configuration.

---

## 1. Alpaca paper trading keys — owner runbook (target: ≤ 10 minutes)

### 1.1 Which dashboard am I on?

| Environment | Dashboard URL | Source (verified 2026-07-30) |
|---|---|---|
| **PAPER** (the one we want) | `https://app.alpaca.markets/paper/dashboard/overview` | https://docs.alpaca.markets/us/docs/alpaca-mcp-server.md (page updated 2026-06-08) |
| LIVE (do not use) | `https://app.alpaca.markets/brokerage/dashboard/overview` | https://docs.alpaca.markets/us/docs/getting-started-with-alpaca-market-data.md (page updated 2025-09-24) |

The **only documented way to tell the two apart** is (a) the path segment — `/paper/` versus
`/brokerage/` — and (b) the account number shown in the upper-left corner of the dashboard
(same source, verified 2026-07-30).

> `UNVERIFIED` — there is **no documented coloured "PAPER" badge**. Do not rely on remembering a
> colour or a banner. Read the URL bar and the upper-left account number.

### 1.2 Generating the keys

This is the only click path the documentation gives
(https://docs.alpaca.markets/us/docs/getting-started-with-alpaca-market-data.md, page updated
2025-09-24, verified 2026-07-30):

1. Open `https://app.alpaca.markets/paper/dashboard/overview`.
2. Confirm the URL contains `/paper/` and note the account number in the upper-left corner.
3. Find the **"API Keys"** section in the **right sidebar**.
4. Click **"Generate New Keys"**.
5. **Save the credentials immediately** — into `~/.config/secrets.env` (section 1.6), not into a
   chat, not into a note-taking app, not into a screenshot.

### 1.3 Options permission on a paper account: nothing to do

Verbatim from https://docs.alpaca.markets/us/docs/options-trading.md (page updated 2026-04-02,
verified 2026-07-30):

> "In the Paper environment, options trading capability will be enabled by default - there's nothing
> you need to do!"

Options levels, same source, verified 2026-07-30:

| Level | What it permits |
|---|---|
| 0 | Options disabled |
| 1 | Sell covered call / sell cash-secured put |
| 2 | Level 1 **plus buy a call, buy a put** |
| 3 | Levels 1–2 **plus** call and put spreads |

**Our strategy buys calls, so it requires LEVEL 2 or higher.** On paper this is expected to be
satisfied by default per the quote above.

To confirm programmatically, `GET /v2/account` exposes
(https://docs.alpaca.markets/us/docs/options-trading-overview.md, page updated 2026-07-09, verified
2026-07-30):

- `options_approved_level` — documented as `"0=disabled, 1=Covered Call/Cash-Secured Put, 2=Long
  Call/Put, 3=Spreads/Straddles"`.
- `options_trading_level` — the **effective** level, always `<=` approved, capped by the
  account-configurations field `max_options_trading_level`.

To **disable** options on paper: Trading Dashboard → **"Account"** → **"Configure"**
(https://docs.alpaca.markets/us/docs/options-trading.md, verified 2026-07-30).

> `UNVERIFIED` — the LIVE options-request click path. The docs only say that "in production there
> will be a more robust experience to request options trading". Since our base URL is hardcoded to
> the paper endpoint, this is out of scope anyway.

### 1.4 What a paper-only account is entitled to

| Item | Value | Source (verified 2026-07-30) |
|---|---|---|
| Account type | "Paper Only Account", email signup, available globally | https://docs.alpaca.markets/us/docs/getting-started-with-alpaca-market-data.md |
| Market data entitlement | IEX only — verbatim: "As an Alpaca Paper Only Account holder, you are only entitled to receive and make use of IEX market data." | same |
| Default paper balance | $100,000 | same |

Free / Basic vs. paid, per the docs (verified 2026-07-30):

| | Free / Basic | Algo Trader Plus |
|---|---|---|
| Price | $0 | **$99 / month** |
| Historical API calls | **200 / min** | **10,000 / min** |
| Equity real-time | IEX only | All US exchanges (SIP) |
| Options quote websocket subscriptions | 200 | — |
| Options feed | Indicative Pricing Feed | OPRA |

> **Vendor inconsistency to plan around.** The `/data` marketing page states free = "200 API
> calls/min" and Algo Trader Plus = "Unlimited", while the documentation states 200 and **10,000**
> respectively (both verified 2026-07-30). **Plan for 10,000, not unlimited.** Which figure is
> authoritative is `UNVERIFIED`.

### 1.5 Two honest warnings before you paste anything

1. **`UNVERIFIED` — Alpaca documents no key format anywhere.** No prefix (not `PK`, not `AK`, not
   `CK`), no length, no character set (verified 2026-07-30). Therefore **our local validator is
   heuristic only**. It cannot tell you your key "looks wrong", and a validator complaint is not
   evidence of a bad key — nor is validator silence evidence of a good one. The authoritative test
   is an authenticated request, which is what `qts doctor` performs.
2. **`UNVERIFIED` — key expiry and rotation.** The documentation consulted on 2026-07-30 did not
   state when paper keys expire, nor what resets a paper account. No answer is invented here. If
   authentication starts failing without a code change, regenerate per section 1.2 as the first
   diagnostic step.

### 1.6 Append to `~/.config/secrets.env`

Open the file in your own editor and append exactly these two lines, replacing `<PASTE_HERE>` with
the values shown once in the dashboard:

```
APCA_API_KEY_ID=<PASTE_HERE>
APCA_API_SECRET_KEY=<PASTE_HERE>
```

Then: `chmod 600 ~/.config/secrets.env` (the loader refuses to read the file otherwise), and verify
with `qts doctor` — it prints verdicts only, never key values.

---

## 2. Provider comparison matrix

Legend: **B2** = the barrier "every backtest number is tagged `MODELED`", liftable **only** by
minute-level historical option premiums. **Greeks column is marked IRRELEVANT throughout** because
ADR-002 forbids taking greeks from any data provider.

| Provider | 0DTE option chain? | Real delay | Minute-level HISTORICAL option premiums (lifts B2) | Greeks (IRRELEVANT — ADR-002) | Price verified 2026-07-30 | Rate limits | stdlib-only integration |
|---|---|---|---|---|---|---|---|
| **Alpaca Market Data** (free tier) | **Yes** — `GET https://data.alpaca.markets/v1beta1/options/snapshots/{underlying_symbol}`, accepts `expiration_date` (exact `YYYY-MM-DD`), `expiration_date_gte/_lte`, `strike_price_gte/_lte`, `feed` = `opra` or `indicative` (https://docs.alpaca.markets/reference/optionchain) | Options on free = **Indicative Pricing Feed**, "a free derivative of the original OPRA feed" — derivative quotes rather than actual OPRA quotes, trades **"delayed by 15 minutes"** (https://docs.alpaca.markets/us/docs/historical-option-data). Equities on free = **real-time IEX** | **Yes, but shallow** — `GET https://data.alpaca.markets/v1beta1/options/bars`, `timeframe` accepts `"[1-59]Min"` (https://docs.alpaca.markets/us/reference/optionbars). Hard limit: "Currently we only offer historical option data since February 2024" — ≈2.5 years as of 2026-07-30 | **None for 0DTE** — Alpaca: 0DTE contracts "won't have Greeks. Why? The Black-Scholes model includes a factor with 'days to expiry' in the denominator. If that is 0, the result is division by 0 and is undefined." **ALIGNS with ADR-002** | **$0** (Algo Trader Plus $99/mo) | 200 historical calls/min free; 10,000/min on Algo Trader Plus (docs) — see the "Unlimited" inconsistency in §1.4 | **Trivial** — two custom headers `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY`, plain JSON over HTTPS, no SDK. **Blocker:** real-time option *streaming* is msgpack-over-websocket only (`wss://stream.data.alpaca.markets/v1beta1/{feed}`), which a urllib-only client cannot consume → we poll REST, which suits a 5-minute cadence |
| **Databento** | OPRA coverage, **1,600,000+ symbols**, history from **2013** (https://databento.com/options) | Live OPRA available on subscription tiers only (see price column) | **Yes, deepest** — schema `cbbo-1m` (NBBO + last sale at 1-minute intervals), `OPRA.PILLAR` history from **2013-04-01** (https://databento.com/blog/opra-improvements-coming-soon) | **Explicitly none** — "Our API design prioritizes transparency by avoiding vendor-calculated or derived data". **ALIGNS PERFECTLY with ADR-002** | Historical: pay-as-you-go **$0.04/GB** of uncompressed binary; new users get **$125 free credits, valid 6 months**. Live OPRA pay-as-you-go **DISCONTINUED** — live now requires **Standard $199/mo** or higher (https://databento.com/blog/introducing-new-opra-pricing-plans, June 2025). Ladder (https://databento.com/pricing): usage-based historical-only; Standard $199/mo; Plus $1,750/mo; Unlimited $4,500/mo | Not stated in the sources consulted 2026-07-30 → `UNVERIFIED` | **Feasible** — plain HTTPS REST, no local daemon, no JVM; stdlib-feasible **if** you request CSV or JSON rather than the binary encoding |
| **ThetaData** | Not separately verified for same-day expirations on 2026-07-30 → `UNVERIFIED` | Not separately verified on 2026-07-30 → `UNVERIFIED` | **Yes** — minute intervals are a **Value**-tier bullet. History depth is self-contradictory across ThetaData's own pages (see note below) | Included at Value (IV + 1st-order), **vendor-calculated Black-Scholes computed tick-by-tick** — this is exactly the vendor delta ADR-002 refuses (https://docs.thetadata.us/Articles/Data-And-Requests/Option-Greeks.html). Buying it **does not remove** the local `PricingEngine` | Options **Value $40/mo**, Standard $80/mo, Pro $160/mo (https://www.thetadata.net/pricing). **ADR-007 recorded "$40/month" — CONFIRMED, still correct** | Self-contradictory: Concurrent-Requests page says VALUE = **1** thread, Subscriptions page says **2** → `UNVERIFIED` | **No.** Either the **Theta Terminal**, a local **Java** process — "Java 21 or higher is required to use the Theta Terminal" — hosting a local HTTP server on port **25510** (https://docs.thetadata.us/Articles/Getting-Started/Getting-Started.html); or their Python library, which "authenticates over HTTPS, makes requests over gRPC, and returns Polars or Pandas dataframes" (https://docs.thetadata.us/Python-Library/Getting-Started.html). **Neither path is stdlib-only** |
| **Massive** (formerly Polygon.io) | Snapshot endpoint `GET /v3/snapshot/options/{underlyingAsset}` exists; same-day-expiration behaviour not separately verified 2026-07-30 → `UNVERIFIED` | **Real-time options requires the $199 Advanced tier**; Starter and Developer are **15-minute delayed**. Free tier: **End-of-Day** per the pricing page — see inconsistency note below | **Yes** — `GET /v2/aggs/ticker/{optionsTicker}/range/{multiplier}/{timespan}/{from}/{to}`, records back to **2014-06-02**, "all history" on Advanced and business plans | Returns greeks **and** IV per contract via `/v3/snapshot/options/{underlyingAsset}` — **IRRELEVANT to us per ADR-002** | Options **Basic $0/mo, Starter $29/mo, Developer $79/mo, Advanced $199/mo** (https://massive.com/pricing?product=options) | Free tier **5 API calls/minute**, End-of-Day data, **2 years** history → unusable for 0DTE | Plain REST |
| **Tradier** | `GET https://api.tradier.com/v1/markets/options/chains`, with a `greeks` boolean defaulting to `false`. **Whether same-day expirations are returned is `UNVERIFIED`** | Real-time data is gated on being a brokerage customer — verbatim: "Real-time data is available to all Tradier Brokerage account holders... If you are not a Tradier Brokerage account holder, we are unable to provide you with any real-time data". **Sandbox is not viable**: delayed by "the industry-standard 15 minutes", and greeks are unavailable in Sandbox entirely | Not established from the sources consulted 2026-07-30 → `UNVERIFIED` | Greeks come from **ORATS** and refresh only **HOURLY** in the Brokerage API — useless for intraday 0DTE even if we wanted them, and we do not (ADR-002) | Not established from the sources consulted 2026-07-30 → `UNVERIFIED` | Not established from the sources consulted 2026-07-30 → `UNVERIFIED` | Plain REST, but blocked upstream by the brokerage-customer gate |
| **yfinance** (what the system uses today) | Yes — this is the current production path | Option chains documented as **15-minute delayed**, but **the documentation is Yahoo's, not yfinance's**: Yahoo's exchanges/data-providers table lists OPRA options as "15 min" delayed via ICE Data Services (https://help.yahoo.com/kb/finance-for-web/exchanges-data-providers-yahoo-finance-sln2310.html). The yfinance README makes **no delay claim of its own** → the delay actually served is `UNVERIFIED`. **Our own live measurement: newest-bar age 3.1–4.9 minutes in real sessions** — materially fresher than the nominal 15 minutes | **No** — this is precisely why B2 exists | **None** — confirmed **in the installed package**, not just from docs: the option-chain DataFrame column list is hard-coded and contains only `contractSymbol, lastTradeDate, strike, lastPrice, bid, ask, change, percentChange, volume, openInterest, impliedVolatility, inTheMoney, contractSize, currency`. Consistent with ADR-002 | **$0** | Not established from the sources consulted 2026-07-30 → `UNVERIFIED` | Already integrated; it is a third-party dependency, not stdlib |

### 2.1 ThetaData history-depth contradiction (vendor-side, unresolved)

ThetaData's own pages disagree, both verified 2026-07-30:

| Source | Claim for the Value tier |
|---|---|
| Pricing page (https://www.thetadata.net/pricing) | "4 Years of data" |
| Subscriptions doc (https://docs.thetadata.us/Articles/Getting-Started/Subscriptions.html) | Explicit per-tier starts: FREE **2023-06-01**, VALUE **2020-01-01**, STANDARD **2016-01-01**, PRO **2012-06-01** |

**ADR-007's "since 2020" matches the docs, not the marketing page.** Which is authoritative is
`UNVERIFIED`.

### 2.2 Massive delay contradiction (vendor-side, unresolved)

The pricing page says Basic is **"End of Day"**, while the `/options` page says "Basic and Starter
plans feature 15-minute delayed data" (both verified 2026-07-30). **Treat free as end-of-day until
confirmed** — the safer of the two readings for planning purposes. Which is correct is `UNVERIFIED`.

### 2.3 No key line for ThetaData, Tradier, or yfinance

None of these three is being adopted, and **no environment-variable name exists for them in this
project**. No variable name is invented here. If one of them is ever adopted, the variable name gets
chosen at that time, in writing, before any key is created.

---

## 3. Recommendation

### 3.1 IMPLEMENT NOW — Alpaca Market Data

| Reason | Detail |
|---|---|
| One key, literally | It reuses the **same key the owner is already creating for the paper broker** (§1). "One key" stops being a slogan and becomes true. |
| stdlib-trivial | Two custom headers, plain JSON over HTTPS, no SDK (§2). |
| 0DTE chains available | Snapshot endpoint with exact `expiration_date` filtering (§2). |
| No greeks — which is what we want | 0DTE contracts return no greeks by construction (division by zero on days-to-expiry). This **aligns with ADR-002** rather than fighting it. |
| Fresher equity bars | Free-tier **real-time IEX** equity bars should sharply reduce the measured bar age that currently feeds RVOL / ORB / VWAP. |

**Be honest about what this does *not* buy.** It does **not** improve the *options* quote delay:
free options data is the Indicative Pricing Feed with trades delayed 15 minutes — the same ballpark
as yfinance's nominal figure. The gain is (a) on the **equity-bar** side and (b) a **single-vendor,
single-key** setup.

Note the tension worth watching after cutover: our own yfinance measurement is **3.1–4.9 minutes**
of newest-bar age (§2), which is already far better than the nominal 15 minutes. The expected
improvement is on equity bars specifically; the options side should be measured, not assumed.

### 3.2 RECOMMEND TO THE OWNER FOR B2 — Databento historical OPRA, **not** ThetaData

This is a **decision to be made by the owner**, not something to install today.

| Criterion | Databento | ThetaData |
|---|---|---|
| Integration | Plain HTTPS REST, no JVM, no local daemon; stdlib-feasible with CSV/JSON | **Java 21** Theta Terminal on port 25510, **or** a gRPC + Polars/Pandas library — both break "stdlib first, no new dependency without a written reason" |
| Minute OPRA history depth | `cbbo-1m` back to **2013-04-01** | Value tier **2020-01-01** per docs (marketing says "4 Years"; unresolved, §2.1) |
| Cost of a first backfill | **$0.04/GB** pay-as-you-go with **$125 free credits** (6 months) — a first backfill can cost **nothing** | **$40/month** subscription (ADR-007's price, confirmed) |
| ADR-002 posture | Explicitly refuses vendor-calculated/derived data — **matches** ADR-002 | Vendor Black-Scholes greeks computed tick-by-tick — exactly what ADR-002 refuses; buying it does not remove the local `PricingEngine` |

For reference, Alpaca's own minute option bars reach back only to **February 2024**, so Alpaca is
the shallowest of the three on B2 depth.

### 3.3 FREE FALLBACK — stay on yfinance

It works, it is **measured** (3.1–4.9 minute newest-bar age), and it costs **$0**. There is no
obligation to leave it.

### 3.4 This recommendation CONTRADICTS ADR-007 — stated plainly, not applied

ADR-007 named **ThetaData** as the upgrade path. Section 3.2 recommends **Databento** instead.

- **ADR-007's $40/month price is confirmed correct** (verified 2026-07-30). The price is not the
  problem.
- **What changed** is (a) our knowledge of ThetaData's integration requirements — Java 21 terminal
  or gRPC+Polars, neither stdlib-only — and (b) the appearance of a cheaper, dependency-free
  alternative with deeper history.
- **ADR-007's premise is now outdated.** ADR-007 assumed "there is no free source of intraday
  historical option prices". Alpaca offers **minute option bars since February 2024 on the free
  indicative tier** (verified 2026-07-30). The premise no longer holds — though note the depth is
  ≈2.5 years, which constrains how much of B2 a free backfill can actually lift.

**Per this project's rules a recorded decision is never silently overridden.** Nothing in §3.2 is
implemented. This is presented **for the owner to decide**. If the owner accepts it, the correct
next step is a *new* recorded decision that supersedes ADR-007 and states why — not an edit to
ADR-007 and not a quiet code change.

---

## 4. Owner runbooks — numbered steps, ≤ 10 minutes each

Reminder of the invariants from §0: **the owner performs these steps**; **no key value is ever
pasted into a chat or shared with anyone**; the file must be **`chmod 600`**; verification is
**`qts doctor`**, which prints verdicts only, never key values.

### 4.1 Alpaca (the one to do now)

1. Open `https://app.alpaca.markets/paper/dashboard/overview`
   (source: https://docs.alpaca.markets/us/docs/alpaca-mcp-server.md, verified 2026-07-30).
2. Confirm the URL contains **`/paper/`**, not `/brokerage/`, and note the account number in the
   **upper-left corner**. These two signals are the only documented way to distinguish paper from
   live; a coloured "PAPER" badge is `UNVERIFIED` and must not be relied on.
3. In the **right sidebar**, find the **"API Keys"** section.
4. Click **"Generate New Keys"**.
5. Leave the browser tab open — the secret is shown once.
6. In your own editor, open `~/.config/secrets.env` and append the two lines in step 8, replacing
   each `<PASTE_HERE>` with the corresponding value from the dashboard.
7. Close the browser tab. Do not copy the values anywhere else.
8. The lines to append:

```
APCA_API_KEY_ID=<PASTE_HERE>
APCA_API_SECRET_KEY=<PASTE_HERE>
```

9. `chmod 600 ~/.config/secrets.env` — the loader refuses to read the file if the mode is looser.
10. Run `qts doctor`. It prints verdicts only, never key values. Expect it to confirm credentials
    are present and that the paper endpoint authenticates. If it reports a format concern, remember
    that **Alpaca documents no key format at all** (`UNVERIFIED`, §1.5) — our validator is heuristic
    and cannot judge your key. The authoritative signal is whether the authenticated request
    succeeds.

Optional confirmation of options entitlement: `GET /v2/account` and read `options_trading_level`;
our strategy buys calls, so it needs **level 2 or higher**
(https://docs.alpaca.markets/us/docs/options-trading-overview.md, verified 2026-07-30). Paper is
documented to enable options by default (§1.3).

### 4.2 Databento (only if the owner accepts §3.2 — otherwise skip entirely)

Do **not** perform these steps unless the owner has decided to supersede ADR-007. Section 3.2 is a
recommendation awaiting a decision.

1. Open `https://databento.com/pricing` and confirm which tier you need. For B2, **historical-only,
   usage-based** is sufficient: **$0.04/GB** uncompressed, with **$125 free credits valid 6 months**
   for new users (verified 2026-07-30). Live OPRA pay-as-you-go was **discontinued** and now
   requires **Standard $199/mo** or higher
   (https://databento.com/blog/introducing-new-opra-pricing-plans, verified 2026-07-30) — **we do
   not need live**.
2. Create the account yourself and complete signup.
3. Locate the API key section of the Databento portal and create a key. `UNVERIFIED` — the exact
   in-portal click path for key creation was not part of the verified source set for 2026-07-30; do
   not follow a guessed path, follow what the portal shows you.
4. In your own editor, open `~/.config/secrets.env` and append the line in step 5.
5. The line to append — **variable name proposed, not yet read by any code**:

```
DATABENTO_API_KEY=<PASTE_HERE>
```

6. `chmod 600 ~/.config/secrets.env`.
7. Run `qts doctor` (verdicts only, never key values). Expect it to have **no opinion** on this
   variable yet: nothing in the codebase reads `DATABENTO_API_KEY` today. Its presence is inert
   until a written decision and an implementation exist.
8. When you do backfill, request **CSV or JSON**, not the binary encoding — that is what keeps the
   integration stdlib-feasible. The schema you want for B2 is **`cbbo-1m`** (`OPRA.PILLAR` history
   from **2013-04-01**, verified 2026-07-30).
9. `UNVERIFIED` — the GB volume, and therefore the dollar cost, of a first 0DTE backfill. $0.04/GB
   is the rate; the quantity was not established on 2026-07-30. Size a small trial against the $125
   credit before committing to a full pull.

### 4.3 Massive (formerly Polygon.io) — steps only, **not recommended**

No section of this document recommends Massive. These steps exist for completeness if the owner
chooses it anyway. Note before starting: the **free tier is unusable for 0DTE** (5 API calls/minute,
End-of-Day data, 2 years history), and **real-time options requires the $199/mo Advanced tier**
(https://massive.com/pricing?product=options, verified 2026-07-30).

1. Note the rename: **polygon.io now 301-redirects to massive.com**, effective **2025-10-30
   16:00 ET**, and the vendor says **`api.polygon.io` will be phased out**. **Any new integration
   must target `api.massive.com`** (verified 2026-07-30).
2. Open `https://massive.com/pricing?product=options` and choose a tier: **Basic $0/mo, Starter
   $29/mo, Developer $79/mo, Advanced $199/mo** (verified 2026-07-30). For 0DTE, only **Advanced**
   provides real-time options.
3. Create the account yourself and complete signup.
4. Locate the API key in the dashboard. `UNVERIFIED` — the exact in-dashboard click path was not
   part of the verified source set for 2026-07-30; follow what the dashboard shows you rather than a
   guessed path.
5. In your own editor, open `~/.config/secrets.env` and append the line in step 6.
6. The line to append — **variable name proposed, not yet read by any code**:

```
MASSIVE_API_KEY=<PASTE_HERE>
```

7. `chmod 600 ~/.config/secrets.env`.
8. Run `qts doctor` (verdicts only, never key values). As with Databento, expect **no verdict** on
   this variable: nothing in the codebase reads `MASSIVE_API_KEY` today.

---

## Appendix A — `UNVERIFIED` register

Every item the documentation consulted on 2026-07-30 did **not** settle. No answer has been invented
for any of them.

| # | Item | Why it is unverified |
|---|---|---|
| 1 | A coloured "PAPER" badge on the Alpaca dashboard | Not documented. The `/paper/` path segment plus the upper-left account number are the only documented discriminators. |
| 2 | LIVE options-trading request click path | Docs only say "in production there will be a more robust experience to request options trading". Out of scope: base URL is hardcoded to paper. |
| 3 | Alpaca paper key expiry / rotation policy | Docs consulted do not state when paper keys expire. |
| 4 | What resets an Alpaca paper account | Docs consulted do not state it. |
| 5 | Alpaca key id / secret **format** | No prefix, length, or character set is documented anywhere. Consequence: our validator is heuristic and cannot flag a "wrong-looking" key. |
| 6 | Alpaca Algo Trader Plus call ceiling: docs say **10,000/min**, `/data` marketing says **"Unlimited"** | Vendor pages disagree; which is authoritative is unresolved. **Plan for 10,000.** |
| 7 | Tradier: whether same-day (0DTE) expirations are returned by `/v1/markets/options/chains` | Not stated in the sources consulted. |
| 8 | Tradier: minute-level historical option premiums, price, rate limits | Not established from the sources consulted. |
| 9 | Massive free tier: **End-of-Day** (pricing page) vs **15-minute delayed** (`/options` page) | Vendor pages disagree. Treat free as end-of-day until confirmed. |
| 10 | Massive: whether the snapshot endpoint returns same-day expirations | Not separately verified. |
| 11 | ThetaData Value history start: **2020-01-01** (docs) vs **"4 Years"** (pricing page) | Vendor pages disagree. ADR-007's "since 2020" matches the docs. |
| 12 | ThetaData Value concurrency: **1 thread** (Concurrent-Requests page) vs **2** (Subscriptions page) | Vendor pages disagree. |
| 13 | ThetaData: 0DTE chain availability and real delay | Not separately verified. |
| 14 | Databento rate limits | Not stated in the sources consulted. |
| 15 | Databento / Massive **in-portal key-creation click path** | Not part of the verified source set; follow the portal, not a guess. |
| 16 | GB volume — hence dollar cost — of a first Databento 0DTE backfill | Only the $0.04/GB rate is verified, not the quantity. |
| 17 | `DATABENTO_API_KEY` and `MASSIVE_API_KEY` | **Proposed names, not yet read by any code.** No loader or module consumes them today. |
| 18 | The delay yfinance's option-chain endpoint actually serves | The 15-minute figure is **Yahoo's** documentation (OPRA via ICE Data Services); the yfinance README makes no delay claim of its own. Our own measurement is 3.1–4.9 minutes newest-bar age. |
| 19 | yfinance rate limits | Not established from the sources consulted. |

**Total: 19 items tagged `UNVERIFIED`.**
