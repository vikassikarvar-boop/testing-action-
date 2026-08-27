# RUN INSTRUCTIONS — 2-3 Day Pattern Survival Test

> **Goal**: Run all 6 accounts for 2-3 days and measure **which pattern survives longest** and **what daily volume is safe** per account. No patterns = no accounts. Accounts first, profit second.

---

## 1. What Was Fixed (15 fixes applied)

| # | Fix | File |
|---|-----|------|
| 1 | Per-session connection UUID (removed pool-wide leak) | ig_actions.py |
| 3 | Random connection type (WIFI/4G/LTE/3G) per session | ig_actions.py |
| 4 | timezone_offset from proxy country (UK = 0, not 19800) | ig_actions.py |
| 5 | ig-u-rur read from real cookie (not hardcoded "ODN") | ig_actions.py |
| 10 | container_module rotation (stories/likes/comments) | ig_actions.py |
| 19 | Per-account seeded RNG | ig_actions.py + matrix_runner.py |
| 2 | Per-account target pools (each account touches only its slice) | matrix_runner.py |
| 6 | Month-safe daily volume caps (12C / 25L / 40S max) | matrix_runner.py |
| 7 | Volume ramp-up (0.2 → 1.0 over 22 days) | matrix_runner.py |
| 8 | Exponential backoff by severity (2h / 6h / 24h × 2^n) | matrix_runner.py |
| 9 | Mid-session health check every 3rd action | matrix_runner.py |
| 11 | Realistic dwell (seen 4-12s, like 8-20s, comment 25-60s) | matrix_runner.py |
| 12 | Minimum action gaps (seen ≥90s, like/comment ≥120s) | matrix_runner.py |
| 13 | Passive browsing capped at 2 calls/session (saves data) | matrix_runner.py |
| 15 | Per-account comment vocab + question-form comments | unique_comment_generator.py |
| 16 | Story fetch cache TTL 10-15 min (saves data) | matrix_runner.py |
| 17 | Account-staggered activity windows | matrix_runner.py |
| 18 | Dead-account dedupe + status tracking | matrix_runner.py |

---

## 2. How To Run

```bash
# From the ig_multi_matrix_engine folder:
python matrix_runner.py --status        # check scorecard anytime
python matrix_runner.py --all           # run all 6 accounts (day mode)
python matrix_runner.py --account 1     # run single account only
```

**Important**: `matrix_state.json` still contains OLD counters (e.g. bat shows C:22 L:35 S:33 from the old aggressive run) and dead-account entries for accounts that died BEFORE these fixes. For a **clean measurement**, reset the state file before the 2-3 day test:

```bash
# Delete old state so counters start fresh:
del matrix_state.json
```

> If you delete state, the engine re-creates it. This gives you a clean baseline to measure which pattern survives longest.

---

## 3. The 6 Patterns Being Tested

| ID | Account | Pattern | Daily Volume | Total/day |
|----|---------|---------|--------------|-----------|
| 1 | bat.74447119 | **Mixed 3:3:2** (12C/18L/18S) — high, 3 sessions | 12 + 18 + 18 | **48** |
| 2 | badger.4951992 | **Mixed 3:3:2** (10C/15L/15S) — medium, 3 sessions | 10 + 15 + 15 | **40** |
| 3 | crab.53731619 | **Comment-only** 12/day | 12 | **12** |
| 4 | camel.58963911 | **Story-only** 40/day | 40 | **40** |
| 5 | bear.82362367 | **Story-only** 30/day | 30 | **30** |
| 6 | camel.68524392 | **Comment-only** 10/day | 10 | **10** |

**What this answers**:
- Does *mixed* (C+L+S) trigger detection faster than *single-type*?
- Do comments (higher risk) or story views (lower risk) survive longer?
- Which daily volume is safe: 48/day or 12/day?

---

## 4. Daily Checklist (run this every morning)

1. Open terminal: `python matrix_runner.py --status`
2. Read the scorecard:
   - **ACTIVE** = survived the night
   - **COOLDOWN** = got a block/feedback → check `matrix_state.json` → `cooldowns` for how long
   - **DEAD** = banned/checkpointed → note which pattern it was
3. Check `matrix_log.csv` for response status codes (rows with 4xx/5xx = trouble)
4. Check which account type died first → that's the risky pattern

**Log file columns**: timestamp, account, action, target, media_id, status, note.

---

## 5. Decision Rules (after 2-3 days)

| Outcome | Meaning | Action |
|---------|---------|--------|
| All 6 ACTIVE after 3 days | Volumes are safe | Test next volume tier (+30% per account) |
| Story-only accounts alive, mixed died | Comments/likes are the risk | Reduce comments, keep story volume |
| All accounts got COOLDOWN (soft/medium) | Volume too high | Cut all targets by 50% |
| Any account got **severe** block (24h+) | Pattern too aggressive | Stop that pattern entirely |
| 2+ accounts DEAD in 3 days | Volume way too high | Drop to 50% of current, or only story-only |

**Safe daily limit formula** (for scaling later):
```
Safe actions/day/account = your current daily volume × (survival days / target days)
Example: 48/day, 2 of 3 days survived = 48 × 2/3 = 32 actions/day safe
```

---

## 6. Cost Economics (₹)

### Account cost
- ₹10/account, one-time. 6 accounts = ₹60 total.
- If an account dies, you lose ₹10. **Your daily profit on ONE story-only account easily covers 10+ dead accounts.**

### Data cost (₹450/GB)
- Story view ≈ 30-60 KB each (stories are small, cached responses gzipped)
- **Story fetch cache (Fix #16)** now prevents re-fetching the same user within 10-15 min → biggest data saver
- **Passive browsing cap (Fix #13)** cut 6+ calls/session to max 2 → second biggest saver
- Estimate per account per day: ~2-4 MB → all 6 accounts: ~15-25 MB/day ≈ **₹7-12/day data cost**

### Revenue at market rates
| Service | Market price | Unit | Daily from 6 accounts |
|---------|-------------|------|----------------------|
| Story views | ₹5-10 | /1000 | bat 18 + badger 15 + camel 40 + bear 30 = **103 views/day** |
| Likes | ₹5-10 | /1000 | bat 18 + badger 15 = **33 likes/day** |
| Comments | ₹100-400 | /1000 | bat 12 + badger 10 + crab 12 + camel 10 = **44 comments/day** |

### Daily P&L (worst case — 6 accounts, all active)
```
Revenue:  103/1000 × ₹5  = ₹0.52 (views)
          33/1000 × ₹5   = ₹0.17 (likes)
          44/1000 × ₹100 = ₹4.40 (comments)
Total revenue/day        = ₹5.09
Data cost/day            = ₹7-12 (!!!!)
Account amortization     = ₹60/30 = ₹2/day
Net/day                  = -₹4 to -₹9  ← LOSING at this volume
```

### ⚠️ The honest math — this is why volume matters
At **safe** volumes (12C/25L/40S max), 6 accounts CANNOT be profitable yet. That's **by design**: this 2-3 day test is to **find the safe ceiling**, not to profit.

**To become profitable you need either:**
1. **More accounts** (30-50 accounts × safe volume each), OR
2. **Higher safe volume** (proven by this test), OR
3. **Higher-margin services** (comments at ₹100-400/1k are 20-80× more profitable than views)

**Scaling roadmap (after the test proves safe limits):**
```
Phase 1 (now):  6 accounts, safe volume → find ceiling, cost ₹60, 2-3 days
Phase 2:        30 accounts × safe volume → ~500 views/day, ~₹25/day revenue
Phase 3:        100 accounts → ~1,700 views/day, ~₹85/day revenue
Phase 4:        500 accounts (2-5M views goal) → needs proven per-account ceiling
```
Each phase requires the **previous phase's survival data** — never scale without it.

---

## 7. Anti-Patterns To Avoid During The Test

- ❌ Don't increase volume mid-test (ruins the measurement)
- ❌ Don't run the same account on a different proxy/IP mid-test
- ❌ Don't delete `matrix_state.json` mid-test (only BEFORE starting)
- ❌ Don't run more sessions per day than config allows (code enforces it)
- ✅ Keep logs; they are your evidence for what's safe
