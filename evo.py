"""
Evolutionary paper-trading engine: early momentum, rug-risk reasoning, trailing exits.

ENTRY: no fixed strategy filters. A token is only bought if it passes the RUG-RISK REASONING below
(at least EVO_MIN_AGE_MIN old, holders >= 50, top-10 holders < 80%, liquidity floor, no RugCheck danger flags,
sells actually happening). Brand-new launches are skipped: in paper trading they produced most of the losses
(rugs and liquidity pulls in the first minutes). Everything else (max token age, momentum, volume, score, stricter
holder limits) is an OPTIONAL gene: off by default, switched on or tuned by evolution only if it improves results.

Rules (2026-10-24, owner: 'whatever helps the bots profit'): the bf2874b strategy lost -$94 over 746 trades in its
first 75 minutes (about -$75/h vs -$2 to -$6/h before). Brought back: 4 h minimum age, quiet-token rule, 8% testing
rule, 40% stakes for unproven bots, 15-min flat hold and the matching gene bounds (replayed on those 746 trades:
about -$11 instead of -$94). Still off: recovery trades, partial sells, Momentum v2, Sprint.

EXIT (per variant genes): stop-loss, take-profit (45%+, aiming for 100%+), a trailing stop measured from the
token's peak price since entry (armed once the trade is past break-even), a time cap (2 to 30 minutes), and
emergency exits for liquidity pulls / crashes that fire even inside the minimum hold time.

EVOLUTION: variants run side by side on the same market data (paper only), at most EVO_MAX_PER_TOKEN of them in
one token, so one rug can't sink half the population and each variant is judged on its own picks. A variant is
retired if, after EVO_EVAL_AFTER trades, win rate < target or net PnL < 0 (or drawdown, or it never trades). The
win-rate target is low by default: with take-profits of 45%+ against stop-losses under 8%, a strategy can be
profitable winning one trade in three, and a high target only rewards luck. Each retirement spawns a
child bred from the best performer, nudged by how the parent lost, plus what winning trades had in common across
all variants, plus jitter. Survivors graduate after EVO_GRAD_TRADES trades and must pass a fresh validation window.

Paper only. Paper fills are optimistic, especially at launch: results will be worse live.
"""
import asyncio, json, math, os, random, re, time, logging
from collections import Counter
from pathlib import Path

import auto
import paths

log = logging.getLogger("evo")
STATE = Path("evo_state.json")

POP = int(os.getenv("EVO_POP", "100"))
PROVEN_WR = float(os.getenv("EVO_PROVEN_WR", "0.35"))  # win rate that keeps a bot alive and lets it breed
# breeding may push the population above POP, never above this (at least 30% headroom, or proven bots can't breed)
MAX_POP = max(int(os.getenv("EVO_MAX_POP", "0")), int(POP * 1.3))
START_USD = float(os.getenv("EVO_START_USD", "10"))
TARGET_WR = float(os.getenv("EVO_TARGET_WINRATE", "0.35"))
REQ_WR = min(TARGET_WR, PROVEN_WR)  # win rate every check demands (lower either setting to relax it)
CHECK_1 = int(os.getenv("EVO_CHECK_1", "5"))        # first profitability check (closed trades)
EVAL_AFTER = CHECK_2 = int(os.getenv("EVO_CHECK_2", "10"))  # second check, same requirements; passing it graduates to validation
# max bots in one token at once (0 = no limit). 10 (owner, 2026-10-24): with no limit the whole population piled into
# 2 coins in one hour and a single stop-out (DOPE, ~360 bots) cost $50.96
# 3 (2026-10-25): at 10, the 2,141 v3.1 trades were only ~228 separate decisions (median 7 bots in the same entry, up
# to 125), so one coin's luck decided many bots' records at once and evolution was judging noise
MAX_PER_TOKEN = int(os.getenv("EVO_MAX_PER_TOKEN", "3")) or 10 ** 9
KEEP_RETIRED = max(150, POP // 2)
VAL_TRADES = int(os.getenv("EVO_VAL_TRADES", "30"))
VAL_CHECK = int(os.getenv("EVO_VAL_CHECK", "5"))     # early check inside validation (same requirements)
MAX_DD_PCT = float(os.getenv("EVO_MAX_DRAWDOWN_PCT", "30"))
COST_PCT = float(os.getenv("EVO_COST_PCT", "2.5"))               # fees + slippage, round trip
TARGET_USD = float(os.getenv("SCALP_TARGET_USD", "0.20"))        # min expected net profit per winning trade
STALE_HOURS = int(os.getenv("EVO_STALE_HOURS", "48"))
NOTIFY_TRADES = os.getenv("EVO_NOTIFY_TRADES", "false").lower() == "true"
ANALYZE_LIMIT = int(os.getenv("EVO_ANALYZE_LIMIT", "4"))         # fresh analyses per scan
CANDIDATE_LIMIT = int(os.getenv("EVO_CANDIDATE_LIMIT", "80"))
COOLDOWN_MIN = 30
ACACHE_TTL_MIN = 30
# open positions are checked this often, in their own task: inside the main loop a slow scan (discovery, RugCheck)
# held exits up for 20-40 s, so 6-8% stop-losses filled at a median -8.9% gross (10% of them past -14.7%)
MONITOR_S = float(os.getenv("EVO_MONITOR_S", "5"))

# hold window: no minimum hold; out by MAX_HOLD_FLAT minutes unless in profit, then never longer than HARD_HOLD
HARD_HOLD = float(os.getenv("EVO_HARD_HOLD_MIN", "30"))      # only for trades that are in profit
MAX_HOLD_FLAT = float(os.getenv("EVO_MAX_HOLD_MIN", "15"))   # everything not in profit is out by here at the latest
# active tokens only: 1h volume at least this multiple of liquidity. On tokens 4h+ old, entries below 0.5x won
# 1 of 72 trades across 12 tokens (paper data to 2026-10-23); on 2026-10-24 they lost $18.54 of $45.27
HARD_MIN_VOL_LIQ = float(os.getenv("EVO_MIN_VOL_LIQ", "0.5"))

# the 8% rule (small wins left out of testing) was dropped (owner, 2026-10-25): every closed trade counts. Under
# v3.3 a trade only takes profit once the whole trade nets MIN_TP_NET%, so small wins come only from time exits

# recovery trades: an idle bot takes a small quick trade to win back funds; never counted in testing (off at bf2874b)
RECOVERY_ON = os.getenv("EVO_RECOVERY", "false").lower() == "true"
RECOVERY_IDLE_MIN = float(os.getenv("EVO_RECOVERY_IDLE_MIN", "30"))  # a bot must trade at least this often
RECOVERY_NET_PCT = float(os.getenv("EVO_RECOVERY_NET_PCT", "1.0"))   # net profit target (TP = this + costs)
RECOVERY_SL = float(os.getenv("EVO_RECOVERY_SL_PCT", "3"))
RECOVERY_HOLD = float(os.getenv("EVO_RECOVERY_HOLD_MIN", "15"))
RECOVERY_CHECK_N = int(os.getenv("EVO_RECOVERY_CHECK_N", "40"))       # pause them if net negative after this many


# bots that have not (yet) proven themselves trade this fraction of their normal size. Walk-forward replay of 1,421
# paper trades: unproven bots lost $0.232/trade, already-proven ones $0.017/trade; at 40% the same trades lost
# $127.63 instead of $317.71. Entry decisions are unchanged, only the stake.
UNPROVEN_SIZE = float(os.getenv("EVO_UNPROVEN_SIZE", "0.4"))


def proven_now(v) -> bool:
    """Meets the profit checks on its counted trades right now (5+ counted trades, win rate, net profit)."""
    T = test_trades(v)
    return len(T) >= CHECK_1 and meets(T)[0]


def test_trades(v) -> list:
    """The trades a bot is tested on: all of its closed trades (recovery trades are not in v["closed"] at all)."""
    return list(v["closed"])

# rug-risk reasoning (hard rules; evolution can only make them stricter)
# pair must be at least this old (minutes). 4 h: across 61 tokens, entries into pairs under 4 h lost $237 of the
# $235 total (to 2026-10-23); on 2026-10-24 under 4 h lost $48.71 over 234 trades in 75 minutes
HARD_MIN_AGE = float(os.getenv("EVO_MIN_AGE_MIN", "240"))
HARD_MIN_HOLDERS = int(os.getenv("EVO_MIN_HOLDERS", "50"))
HARD_MAX_TOP10 = float(os.getenv("EVO_MAX_TOP10", "80"))         # top 10 must be strictly below this
HARD_MIN_LIQ = float(os.getenv("EVO_MIN_LIQ", "5000"))
RUG_LIQ_DROP = float(os.getenv("EVO_RUG_LIQ_DROP_PCT", "30")) / 100   # liquidity down this much vs entry = rug exit
CRASH_TICK_PCT = float(os.getenv("EVO_CRASH_TICK_PCT", "15"))         # price down this much between two checks
PRICE_CHECK_PCT = float(os.getenv("EVO_PRICE_CHECK_PCT", "50"))       # a jump this big between two checks is confirmed
JUP_PRICE = "https://lite-api.jup.ag/price/v3"                          # second price source for those checks
RUG_EXIT_SLIP = float(os.getenv("EVO_RUG_EXIT_SLIP_PCT", "10"))       # paper penalty: you can't sell at the pre-rug price
DISCRETION_PCT = float(os.getenv("EVO_DISCRETION_PCT", "10"))         # above this gross profit a bot may exit whenever it decides
HARD_MAX_TOP1 = float(os.getenv("EVO_MAX_TOP1", "25"))              # biggest single (non-pool) holder
HARD_MAX_INSIDER = float(os.getenv("EVO_MAX_INSIDER", "20"))        # insider-flagged wallets combined
HARD_MAX_CREATOR = float(os.getenv("EVO_MAX_CREATOR", "15"))        # creator wallet
MIN_LP_LOCKED = float(os.getenv("EVO_MIN_LP_LOCKED", "50"))         # % of LP locked/burned, when the data exists
LAUNCH_SLIP = float(os.getenv("EVO_LAUNCH_SLIP_PCT", "3"))            # paper penalty on entries into pairs < 15 min old

SCHEMA = 4  # bump to restart all variants from fresh seeds when the rules change
BOUNDS = {"tp": (45, 300), "sl": (1.5, 8), "hold": (1, 15), "trail": (3, 30), "size_frac": (0.1, 0.5),
          # max token age: at least 4 h above the hard minimum, so every bot has a real window of coins to buy
          "target_usd": (0.2, 1.5), "max_age_min": (HARD_MIN_AGE + 240, 10080),
          "min_holders": (HARD_MIN_HOLDERS, 10000), "max_top10": (10, HARD_MAX_TOP10 - 1),
          "min_liq": (HARD_MIN_LIQ, 200000),
          "min_score": (0, 90), "min_m5": (0, 15), "max_m5": (3, 1000), "min_txns": (0, 200),
          "min_vol_h1": (0, 500000),
          "lock_pull": (0.5, 15), "stall": (2, 30),  # discretionary exit genes (used once profit > DISCRETION_PCT)
          "ps_frac": (0, 1), "ps_at": (5, 80),  # partial sell: sell ps_frac of the position at +ps_at% gross
          "ps_step": (5, 50)}  # v3.3: and again every ps_step% higher (as many partial sells as the price allows)
GENE_DEFAULT = {"lock_pull": 4.0, "stall": 8, "ps_frac": 0, "ps_at": 20, "ps_step": 15}
PS_CHOICES = (0, 0.10, 0.25, 0.50, 0.75, 1.0)  # partial-sell sizes a bot can use (0 = never sells part)
INTS = {"stall", "hold", "min_score", "min_txns", "min_holders", "max_age_min"}
LOG_KEYS = {"tp", "target_usd", "max_age_min", "min_holders", "min_liq"}
OPTIONAL = {"min_m5": (0.5, 3.0), "min_txns": (10, 100), "min_vol_h1": (5000, 100000), "min_score": (50, 80)}  # 0 = off
ANY_AGE = 10080  # max_age_min at this value = no age limit
M5_OFF = 1000    # max_m5 at this value = no ceiling

_BASE = {"tp": 100, "sl": 3, "hold": 5, "trail": 5, "size_frac": 0.25, "target_usd": 0.2, "max_age_min": 60,
         "min_holders": HARD_MIN_HOLDERS, "max_top10": HARD_MAX_TOP10 - 1, "min_liq": HARD_MIN_LIQ,
         "min_score": 0, "min_m5": 0, "max_m5": M5_OFF, "min_txns": 0, "min_vol_h1": 0,
         "lock_pull": 4.0, "stall": 8}
SEEDS = [
    dict(_BASE),                                                                       # early (first hour) runner, 5% trail
    dict(_BASE, max_age_min=120, tp=60, hold=5, trail=8),                             # slightly later entries
    dict(_BASE, max_age_min=ANY_AGE, min_m5=1, min_txns=20, tp=80, trail=8),           # any age, rising only
    dict(_BASE, max_age_min=ANY_AGE, trail=12),                                        # any age, rug checks only
    dict(_BASE, max_age_min=45, min_holders=200, max_top10=50, min_liq=10000, tp=150, trail=15),  # careful early
]

# Momentum v2 (2026-10-23): breakouts on established, actively traded tokens, given time to run. Built from the
# paper data on tokens 4h+ old: strong 5m momentum (+10%) was the only entry bucket in profit, 1-3% lost; liquidity
# under $30k averaged -5.4%; flat exits at the 5-minute cap averaged -2.1% (costs, no time to move); profit-lock
# exits on pullbacks made all the money.
MOMENTUM_V2 = dict(_BASE, max_age_min=ANY_AGE, min_m5=7, max_m5=60, min_liq=30000, min_holders=500, max_top10=40,
                   hold=12, sl=4, trail=10, tp=80, lock_pull=5.0, stall=10, size_frac=0.25)
STRATEGY_VERSION = 2
STRATEGY_SWAP = int(os.getenv("EVO_STRATEGY_SWAP", "100"))  # bots switched to a new strategy family when it ships


def momentum_genome() -> dict:
    """Momentum v2 with jitter, so the new family explores around the design instead of 100 identical bots."""
    gn = {k: (float(x) * random.uniform(0.8, 1.25) if k in ("min_m5", "hold", "sl", "trail", "tp", "lock_pull", "stall")
              else x) for k, x in MOMENTUM_V2.items()}
    return clamp(gn)


def introduce_strategy(S, events):
    """Ship a new strategy family once: the STRATEGY_SWAP weakest unproven bots (no trades or a loss) are replaced
    by Momentum v2 bots. Proven and validating bots are never touched."""
    if S.get("strategy_v", 1) >= STRATEGY_VERSION:
        return
    S["strategy_v"] = STRATEGY_VERSION
    pool = [v for v in active(S) if v["status"] == "testing" and not v.get("proven") and not v["positions"]
            and sum(t["pnl_usd"] for t in v["closed"]) <= 0]
    pool.sort(key=lambda v: (sum(t["pnl_usd"] for t in v["closed"]), -len(v["closed"])))  # biggest losers first
    swap = pool[:STRATEGY_SWAP]
    for v in swap:
        v["status"], v["reason"] = "retired", "replaced by the Momentum v2 strategy"
    for _ in swap:
        new_variant(S, momentum_genome(), 0, None, ["Momentum v2 strategy"])
    if swap:
        events.append(f"🆕 New strategy live: {len(swap)} bots switched to Momentum v2.")


# Momentum v3.1 for all 300 bots (owner, 2026-10-24: 'implement the best working version of momentum'). Best of the
# local live benchmarks: about -1% per trade (24 trades in 2 h) vs v3 -3.6%, v4 -7% and the bots' own rules about
# -3.5%. Still not profitable in testing: it is the least bad. Each bot keeps its own stop-loss (6-8%) and
# partial-sell level (20-30%), which evolution keeps tuning, and its own entry window inside v3.1's (m31_window:
# min_m5, max_m5, min_liq); the other genes are not used while this is on.
MOMENTUM = os.getenv("EVO_MOMENTUM", "v3.1").strip().lower().replace("off", "")  # "off": back to the evolved genes
# v3.2 exits (2026-10-25, "fewer, bigger, lopsided bets"): under v3.1 the median profit-lock exit netted +0.33% (62%
# under +1%) while stop-losses averaged -12.3%, so the bots needed ~80% wins to break even and won 33%. The 43 trades
# that reached the partial sell were all winners (+28.6% average). So: no lock until a trade has been up M31_LOCK_START,
# the lock never keeps less than M31_LOCK_MIN_NET after costs, and the half left after the partial sell trails
# M31_RUNNER_TRAIL below its peak
M31_LOCK_START = float(os.getenv("EVO_M31_LOCK_START", "12"))      # gross %
M31_LOCK_MIN_NET = float(os.getenv("EVO_M31_LOCK_MIN_NET", "5"))   # net % the lock keeps at the least
M31_RUNNER_TRAIL = float(os.getenv("EVO_M31_RUNNER_TRAIL", "10"))  # % below the peak, after the partial sell
# 5 m momentum ceiling: 15 again (2026-10-25). Each bot's own window (m31_window) decides; v3.2 splits the bots into
# a +3-5% half and a +8-15% half (the only v3.1 entry bucket in profit: 248 trades, +0.67%/trade) to settle it on data
M31_M5_MAX = float(os.getenv("EVO_M31_M5_MAX", "15"))
# liquidity ceiling: v3.1 entries above $250k won 7 of 189 trades (-10.3%/trade)
M31_MAX_LIQ = float(os.getenv("EVO_M31_MAX_LIQ", "250000"))
# coins the bots keep losing on are skipped: 10+ trades in the last 24 h and net negative (TRUMP and DOPE: 324
# trades, 0 wins, -$55.55 under v3.1)
BAD_COIN_TRADES, BAD_COIN_H = 10, 24
# and for LAST_LOSS_H hours after the bots' last decision on a coin lost (2026-10-25): with max 3 bots per coin the
# 10-trade rule needs 4+ losing decisions to fire (BTC: 5 losing decisions in a row, -$1.29 of v3.2's first -$1.98).
# Replayed on 213 decisions: the ones right after a losing decision on the same coin made -6.1% each (25 of 108 won),
# the rest -5.2%; skipping them would have saved $96.86 of $135.51
LAST_LOSS_H = float(os.getenv("EVO_LAST_LOSS_H", "6"))
DECISION_S = 180  # entries into one coin within this many seconds are one decision (the bots that went in together)


def m31_pair_ok(p: dict, age) -> bool:
    """v3.1 entry, the market part: 4 h+, liquidity $30k-M31_MAX_LIQ, the hour and the 6 h trend rising, 5 m momentum
    +3% to M31_M5_MAX, buyers outnumber sellers in the last 5 m, 1 h volume 0.5-2x liquidity."""
    ch, tx = p.get("priceChange") or {}, (p.get("txns") or {}).get("m5") or {}
    liq = (p.get("liquidity") or {}).get("usd", 0) or 0
    vl = ((p.get("volume") or {}).get("h1", 0) or 0) / liq if liq else 0
    return (age is not None and age >= HARD_MIN_AGE and 30000 <= liq <= M31_MAX_LIQ and (ch.get("h1") or 0) > 0
            and (ch.get("h6") or 0) > 0 and 3 <= (ch.get("m5") or 0) <= M31_M5_MAX and tx.get("buys", 0) > tx.get("sells", 0)
            and 0.5 <= vl <= 2)


# the v3.2 entry test's two groups: 5 m momentum window per label. Children keep their parent's label and window
M32_ARMS = {"+3-5%": (3.0, 5.0), "+8-15%": (8.0, 15.0)}


def inherit_arm(S, v):
    """A child of a v3.2 test bot joins its parent's group: same m5_arm label, and its 5 m window kept inside that
    group's range (mutation may move it within the range, never across), so the A/B comparison stays clean."""
    par = S["variants"].get(v.get("parent") or "")
    arm = par.get("m5_arm") if par else None
    if arm not in M32_ARMS or v.get("line"):  # breeding lines (X1) keep their own windows, outside the test
        return
    lo, hi = M32_ARMS[arm]
    gn = v["genome"]
    mn = min(hi - 1, max(lo, float(gn.get("min_m5") or 0)))
    gn["min_m5"], gn["max_m5"] = mn, max(mn + 1, min(hi, float(gn.get("max_m5") or hi)))
    v["m5_arm"] = arm


def introduce_v32(S, events):
    """One-off (2026-10-25): v3.2 exits for everyone (see M31_LOCK_START) and an entry A/B test: half the active bots
    take +3-5% 5 m momentum, the other half +8-15%. Their children inherit the window, so evolution keeps the side
    that makes money. Open trades keep their lock settings from entry."""
    if S.get("m32") or not MOMENTUM:
        return
    S["m32"] = time.time()
    bots = active(S)
    random.shuffle(bots)
    for i, v in enumerate(bots):
        arm = "+3-5%" if i % 2 else "+8-15%"
        lo, hi = M32_ARMS[arm]
        v["genome"] = clamp(dict(v["genome"], min_m5=lo, max_m5=hi, ps_at=min(25.0, max(20.0, v["genome"]["ps_at"]))))
        v["m5_arm"] = arm
        v["notes"].append(f"v3.2 entry test: 5 m momentum {arm}")
    events.append(f"🆕 Momentum v3.2 live for {len(bots)} bots: no profit lock until +{M31_LOCK_START:g}% (then at "
                  f"least +{M31_LOCK_MIN_NET:g}% kept), half sold at +20-25%, the rest trails {M31_RUNNER_TRAIL:g}% "
                  f"below its peak; max {MAX_PER_TOKEN} bots per coin; no coins over ${M31_MAX_LIQ / 1000:.0f}k "
                  f"liquidity or that keep losing. Entry test: half the bots +3-5%, half +8-15%.")


def introduce_v33(S, events):
    """One-off (owner, 2026-10-25): v3.3 for every active bot. Each gets a partial-sell ladder (first sale at its
    ps_at, 10-30% gross, then every 10-30% higher, a quarter or half of what is left each time); the X1 founders get
    the ladder from X1_DESIGN. Open trades finish under the rules they were opened with."""
    if S.get("m33") or not (MOMENTUM and M33):
        return
    S["m33"] = time.time()
    for v in active(S):
        gn = v["genome"]
        if v.get("founder") and v.get("line") == LINE:
            _, _, _, ps, step, frac = X1_DESIGN[int(v["id"].split("-")[1]) - 1]
            gn.update(ps_at=ps, ps_step=step, ps_frac=frac)
        else:
            gn.update(ps_at=round(random.uniform(10, 30), 1), ps_step=round(random.uniform(10, 30), 1),
                      ps_frac=random.choice((0.25, 0.5)))
        v["genome"] = clamp(gn)
        v["notes"].append("v3.3: partial-sell ladder, take profit only at +25% whole trade, protective floors")
    events.append(f"🆕 v3.3 live for {len(active(S))} bots: profit is only taken once the whole trade nets "
                  f"+{MIN_TP_NET:g}% (partial sells allowed from any profit, as many as the price allows); protective "
                  f"floors at +{FLOOR_HI:g}% and 0%; every trade now counts in testing (8% rule dropped).")


def founder_records_v33(S):
    """One-off: the founders are ranked for the owner's pick, so their records start when v3.3 went live (S["m33"])
    and only hold trades made with their founder genes and the v3.3 exits. Trades opened before (some even before the
    redesign, under the v2171 x v1844 cross) move to closed_pre, and positions still open from before are marked so
    they close into closed_pre too; their money still counts (paper funds, totals, ledger). Separate from
    introduce_v33 so it also runs when v3.3 went live on code without it (run #197); trades opened after S["m33"]
    stay in the record either way."""
    if S.get("x1_rec33") or not S.get("m33"):
        return
    S["x1_rec33"] = time.time()
    t0 = S["m33"]
    for v in founders(S):
        pre = [t for t in v["closed"] if (t.get("entry_ts") or t["ts"]) < t0]
        if pre:
            v["closed_pre"] = v.get("closed_pre", []) + pre
            v["closed"] = [t for t in v["closed"] if (t.get("entry_ts") or t["ts"]) >= t0]
        for pos in v["positions"].values():
            if pos.get("ts", 0) < t0:
                pos["pre"] = True


def coin_trades(S, mint, now, hours) -> list:
    """Closed trades on one coin in the last `hours`, oldest first. From the append-only ledger (it keeps the trades of
    bots retired and dropped from the state, and the founders' earlier trades), plus the bots' own lists for the time
    before the ledger existed. Recovery trades are left out."""
    cut = now - hours * 3600
    L = S.get("ledger") or []
    out = []
    for t in reversed(L):  # the ledger is in closing order: stop at the first row older than the window
        if t["ts"] < cut:
            break
        if t["mint"] == mint and not t.get("rec"):
            out.append(t)
    start = L[0]["ts"] if L else float("inf")
    if cut < start:
        out += [t for v in S["variants"].values() for t in v["closed"] if t["mint"] == mint and cut <= t["ts"] < start]
    return sorted(out, key=lambda t: t["ts"])


def coin_losing(S, mint, now) -> str | None:
    """Why the bots should stay out of this coin: BAD_COIN_TRADES+ closed trades on it in the last BAD_COIN_H hours
    and net negative, or their last decision on it (within LAST_LOSS_H hours) lost money. None if neither. A decision
    is the bots that entered the coin together (entry times within DECISION_S): under v3.3 they leave at different
    times (partial sells, locks, floors), so grouping by exit time could split one decision."""
    T = coin_trades(S, mint, now, BAD_COIN_H)
    if len(T) >= BAD_COIN_TRADES and sum(t["pnl_usd"] for t in T) < 0:
        return "bots keep losing on it (24h)"
    if T and now - T[-1]["ts"] <= LAST_LOSS_H * 3600:
        e = T[-1].get("entry_ts") or T[-1]["ts"]
        last = [t for t in T if abs((t.get("entry_ts") or t["ts"]) - e) <= DECISION_S]
        if sum(t["pnl_usd"] for t in last) < 0:
            return f"last decision on it lost ({LAST_LOSS_H:g}h)"
    return None


def m31_window(gn: dict) -> tuple:
    """Each bot's own entry window inside v3.1's (2026-10-24): 5 m momentum from min_m5 (3% up to 1 below the
    ceiling) to max_m5 (up to M31_M5_MAX), liquidity at least min_liq ($30k-150k). Only ever tighter than v3.1, so
    evolution can tune entries again (all bots were near-copies of v1092 and only their stop-loss and partial-sell
    level could evolve)."""
    lo = min(M31_M5_MAX - 1, max(3.0, float(gn.get("min_m5") or 0)))
    hi = min(M31_M5_MAX, max(lo + 1, float(gn.get("max_m5") or M5_OFF)))
    return lo, hi, min(150000.0, max(30000.0, float(gn.get("min_liq") or 0)))


def m31_gene_ok(p: dict, gn: dict) -> bool:
    lo, hi, liq = m31_window(gn)
    return lo <= ((p.get("priceChange") or {}).get("m5") or 0) <= hi and ((p.get("liquidity") or {}).get("usd", 0) or 0) >= liq


def m31_holders_ok(a: dict) -> bool:
    return (a.get("holders") or 0) >= 500 and (a.get("top10") if a.get("top10") is not None else 100) <= 40


def m31_lock(peak_gain) -> float:
    # +12 -> +7.5 (the floor: 5% net), +20 -> +12, +30 -> +21, +60 -> +48
    return max(COST_PCT + M31_LOCK_MIN_NET, peak_gain * min(0.8, 0.4 + peak_gain / 100))


def m31_exits(gn: dict) -> dict:
    return dict(gn, tp=1000, hold=30, sl=min(8.0, max(6.0, float(gn["sl"]))), ps_frac=0.5,
                ps_at=min(25.0, max(20.0, float(gn["ps_at"]))), m31=True)


# v3.3 (owner, 2026-10-25): minimum take-profit 25%. A bot may sell part of a trade at any level in profit, as many
# times as it likes (ladder: ps_frac of what is left at +ps_at% gross, then every +ps_step% higher), but it only takes
# its profit on the rest once the WHOLE trade (partial sells + the rest at the current price) nets MIN_TP_NET% after
# costs. From then a lock on the whole trade ratchets up (never below MIN_TP_NET%) and closes it if it falls back.
# Losses are cut at any level: protective floors (below), stop-loss, crash and liquidity exits, 30 min time limit.
M33 = os.getenv("EVO_V33", "true").lower() == "true"
MIN_TP_NET = float(os.getenv("EVO_MIN_TP_PCT", "25"))          # whole-trade net % before profit is taken
M33_LOCK_START = float(os.getenv("EVO_M33_LOCK_START", "30"))  # whole-trade net % at which the lock switches on
PS_MAX_FRAC = 0.5  # one partial sell never sells more than half of what is left, so there is a rest to take profit on
# protective floors (owner, 2026-10-25: "almost no losses"): once the whole trade has netted FLOOR_HI%, it is sold if it
# falls back to FLOOR_HI%; once it has netted FLOOR_ARM%, it is sold if it falls back to break-even (0% after costs).
# They sell early, before a slide turns into a crash. A crash or rug that jumps past the floor between two checks
# still sells at the real price (no paper fill better than the market), and trades never in profit keep the stop-loss.
FLOOR_HI = float(os.getenv("EVO_FLOOR_HI_PCT", "5"))
FLOOR_ARM = float(os.getenv("EVO_FLOOR_ARM_PCT", "0.5"))


def m33_exits(gn: dict) -> dict:
    fr = float(gn.get("ps_frac") or 0)
    return dict(gn, tp=1000, hold=30, sl=min(8.0, max(6.0, float(gn["sl"]))), ps_frac=min(PS_MAX_FRAC, fr),
                ps_at=min(80.0, max(COST_PCT + 2.5, float(gn["ps_at"]))),
                ps_step=min(50.0, max(5.0, float(gn.get("ps_step") or GENE_DEFAULT["ps_step"]))), m31=True, m33=True)


def m33_lock(peak_whole) -> float:
    """Whole-trade net % the lock keeps: MIN_TP_NET at the least, a growing share of the best whole-trade result above
    that (+30 -> +25, +50 -> +40, +100 -> +80)."""
    return max(MIN_TP_NET, peak_whole * min(0.8, 0.4 + peak_whole / 100))


def m33_step(pos: dict, price: float, liq: float, now: float):
    """One price check of a v3.3 trade: ("partial", fraction of what is left), ("exit", reason) or None. The caller
    books a partial sell and updates pos["part"] (net % of the original stake booked so far) and pos["left"] (share
    of the stake still held). Used by the live monitor and by the replay (bench.py), so both behave the same."""
    gross = (price / pos["entry_price"] - 1) * 100
    prev, pos["last_price"] = pos.get("last_price", price), price
    pos["peak_price"] = max(pos.get("peak_price", pos["entry_price"]), price)
    part, left = pos.get("part", 0.0), pos.get("left", 1.0)
    whole = part + left * (gross - COST_PCT)  # the whole trade's net % if the rest were sold now
    pos["peak_whole"] = max(pos.get("peak_whole", -1e9), whole)
    if pos["peak_whole"] >= M33_LOCK_START:
        pos["lock"] = max(pos.get("lock") if pos.get("lock") is not None else -1e9, m33_lock(pos["peak_whole"]))
    if liq < pos["entry_liq"] * (1 - RUG_LIQ_DROP):
        return ("exit", "liquidity collapse")
    if prev > 0 and price < prev * (1 - CRASH_TICK_PCT / 100):
        return ("exit", "crash exit")
    if pos.get("lock") is not None and whole <= pos["lock"]:
        return ("exit", "rising profit lock")
    if pos["peak_whole"] >= FLOOR_HI and whole <= FLOOR_HI:
        return ("exit", f"protective floor +{FLOOR_HI:g}%")
    if pos["peak_whole"] >= FLOOR_ARM and whole <= 0:
        return ("exit", "protective floor 0%")
    if gross <= -pos["sl"]:
        return ("exit", "stop-loss")
    if pos.get("ps_frac") and gross >= pos.get("ps_next", pos["ps_at"]) and gross > COST_PCT:
        pos["ps_next"] = pos.get("ps_next", pos["ps_at"]) + pos["ps_step"]
        return ("partial", pos["ps_frac"])
    if (now - pos["ts"]) / 60 >= pos["hold"]:
        return ("exit", "max hold time")
    return None


def m31_exit(pos: dict, price: float, liq: float, now: float) -> str | None:
    """v3.2 exits: rising profit lock from +M31_LOCK_START% (never moves down, keeps at least M31_LOCK_MIN_NET% after
    costs), stop-loss, out by 30 min, crash and liquidity exits. Half the position is sold at ps_at by the monitor's
    partial sell; the rest rides here behind a trailing stop M31_RUNNER_TRAIL% below its peak (or the lock, if higher)."""
    gross = (price / pos["entry_price"] - 1) * 100
    prev, pos["last_price"] = pos.get("last_price", price), price
    pos["peak_price"] = max(pos.get("peak_price", pos["entry_price"]), price)
    peak = (pos["peak_price"] / pos["entry_price"] - 1) * 100
    if peak >= M31_LOCK_START:
        if pos.get("ps_done"):  # the runner: a looser trail than the lock, so the half left can keep going
            lock = max(COST_PCT + M31_LOCK_MIN_NET,
                       (pos["peak_price"] * (1 - M31_RUNNER_TRAIL / 100) / pos["entry_price"] - 1) * 100)
        else:
            lock = m31_lock(peak)
        pos["lock"] = max(pos.get("lock") if pos.get("lock") is not None else -1e9, lock)  # never moves down
    if liq < pos["entry_liq"] * (1 - RUG_LIQ_DROP):
        return "liquidity collapse"
    if prev > 0 and price < prev * (1 - CRASH_TICK_PCT / 100):
        return "crash exit"
    if pos.get("lock") is not None and gross <= pos["lock"]:
        return "rising profit lock"
    if gross <= -pos["sl"]:
        return "stop-loss"
    if (now - pos["ts"]) / 60 >= pos["hold"]:
        return "max hold time"
    return None


def introduce_momentum31(S, events):
    """One-off: every active bot gets a v3.1 stop-loss (6-8%) and partial-sell level (about 25%) to start from."""
    if not MOMENTUM or S.get("momentum31"):
        return
    S["momentum31"] = time.time()
    for v in active(S):
        v["genome"]["sl"] = round(random.uniform(6, 8), 2)
        v["genome"]["ps_at"] = round(25 * random.uniform(0.85, 1.15), 1)
        v["genome"] = clamp(v["genome"])
    events.append("🆕 All 300 bots now trade Momentum v3.1 (best of the benchmarks so far, still not profitable in "
                  "testing): rising coins only, rising profit lock, half sold at about +25%.")


# Sprint group (owner request, 2026-10-24): 100 bots with the genes of the proven bots that are really in profit, no
# entry filters (only the hard safety rules), no per-coin cooldown, a +25% net target and out within 10 minutes.
SPRINT_N = int(os.getenv("EVO_SPRINT_N", "100"))
SPRINT_NET_PCT = float(os.getenv("EVO_SPRINT_NET_PCT", "25"))     # profit goal after costs (TP = this + COST_PCT)
SPRINT_MAX_HOLD = float(os.getenv("EVO_SPRINT_MAX_HOLD_MIN", "10"))  # short trades: out by here even in profit
SPRINT_PER_TOKEN = int(os.getenv("EVO_SPRINT_PER_TOKEN", "3"))   # own per-coin cap, so they don't crowd out the rest


def sprint_exits(gn: dict) -> dict:
    return dict(gn, tp=SPRINT_NET_PCT + COST_PCT, hold=min(float(gn["hold"]), SPRINT_MAX_HOLD), max_hold=SPRINT_MAX_HOLD)


def introduce_sprint(S, events):
    """Ship the Sprint group once: the SPRINT_N weakest unproven, idle bots are replaced by copies of the proven bots
    whose counted trades are net positive (the best gets 60%, the rest share 40%). Exit genes get a small jitter so
    copies on the same coin don't all exit at once. Proven and validating bots are never touched."""
    if S.get("sprint_v"):
        return
    S["sprint_v"] = 1
    src = [v for v in S["variants"].values() if v.get("proven") and sum(t["pnl_usd"] for t in test_trades(v)) > 0]
    src.sort(key=lambda v: -sum(t["pnl_usd"] for t in test_trades(v)))
    if not src:
        return
    pool = [v for v in active(S) if v["status"] == "testing" and not v.get("proven") and not v["positions"]
            and not v.get("sprint")]
    pool.sort(key=lambda v: (sum(t["pnl_usd"] for t in v["closed"]), -len(v["closed"])))  # biggest losers first
    swap = pool[:SPRINT_N]
    for v in swap:
        v["status"], v["reason"] = "retired", "replaced by the Sprint group"
    n_best = len(swap) if len(src) == 1 else round(len(swap) * 0.6)
    for i in range(len(swap)):
        par = src[0] if i < n_best else src[1 + (i - n_best) % (len(src) - 1)]
        gn = {k: (float(x) * random.uniform(0.85, 1.15) if k in ("sl", "trail", "lock_pull", "stall") else x)
              for k, x in par["genome"].items()}
        ch = new_variant(S, clamp(gn), par["gen"] + 1, par["id"], [f"Sprint group: genes of proven {par['id']}"])
        ch["sprint"] = True
    if swap:
        events.append(f"🆕 Sprint group live: {len(swap)} bots with the genes of the best proven bots "
                      f"({', '.join(v['id'] for v in src)}). No entry filters, +{SPRINT_NET_PCT:g}% target, "
                      f"out within {SPRINT_MAX_HOLD:g} min.")


def restart_population(S, events):
    """One-off (owner, 2026-10-24): back to the bf2874b strategy with 300 fresh bots. Every current bot is retired
    (one with a trade still open finishes that trade first, then stops); the five seeds and fresh seed variations /
    random genomes take their place, and breeding and learning only use bots created from now on. The overall P&L
    history is kept."""
    if S.get("restart_ts"):
        return
    now = S["restart_ts"] = time.time()
    old = [v for v in S["variants"].values() if v["status"] in ("testing", "validating", "validated")]
    for v in old:
        v["status"], v["reason"] = "retired", "fresh start on the bf2874b strategy"
    for sd in SEEDS:
        new_variant(S, clamp(sd), 0, None, ["seed (fresh start, bf2874b strategy)"])
    S["milestone"] = {"stage": "searching"}  # consistency is counted on the new bots only
    ensure_population(S, [])
    events.append(f"🆕 Fresh start: {len(old)} bots retired, {len(active(S))} new bots on the bf2874b strategy "
                  f"(15-min minimum age, full-size trades, every trade counts). Trainees unchanged.")


def fix_age_windows(S, events):
    """One-off repair: raising the hard minimum age to 4 h clamped 204 bots' max token age to 255 min, a 15-minute
    window (240-255 min) that almost no coin passes through, so they hardly traded (25 trades vs 160 for the rest).
    Give each bot whose window is under 4 h a fresh max age between 8 h and any age (log-uniform), so evolution can
    find what works. Run again (age_fix2) when the 4 h minimum came back after the fresh start on 15-min rules."""
    if S.get("age_fix2"):
        return
    S["age_fix2"] = S["age_fix"] = time.time()
    fixed = 0
    for v in S["variants"].values():
        if v["status"] != "retired" and v["genome"]["max_age_min"] <= HARD_MIN_AGE + 240:  # load() already clamped 255 up to here
            v["genome"]["max_age_min"] = round(math.exp(random.uniform(math.log(HARD_MIN_AGE + 240), math.log(ANY_AGE))))
            v["genome"] = clamp(v["genome"])
            fixed += 1
    if fixed:
        events.append(f"🆕 Rules updated for the 300 bots: coins 4 h+ old, quiet coins skipped, 40% stakes until a bot "
                      f"proves itself (the bf2874b rules lost $94 in 75 min). {fixed} bots got a fresh coin-age window.")


def introduce_partial_test(S, events):
    """A/B test of partial selling, once: half of the unproven, idle bots get a partial-sell setting (sell 10, 25, 50,
    75 or 100% of the position once it is up ps_at%, 5-40% gross); the other half keep selling all at once. Evolution
    and the reviews then show which side makes more. Proven and validating bots are never touched."""
    if S.get("partial_test"):
        return
    S["partial_test"] = time.time()
    pool = [v for v in active(S) if v["status"] == "testing" and not v.get("proven") and not v["positions"]]
    random.shuffle(pool)
    picked = pool[:len(pool) // 2]
    for v in picked:
        v["genome"]["ps_frac"] = random.choice(PS_CHOICES[1:])
        v["genome"]["ps_at"] = round(math.exp(random.uniform(math.log(5), math.log(40))), 1)
        v["genome"] = clamp(v["genome"])
        v.setdefault("notes", []).append("partial-sell test")
    if picked:
        events.append(f"🆕 New test live: {len(picked)} bots now sell part of a trade (10-100%) when it's in profit.")


# ---------- state ----------
def load() -> dict:
    if STATE.exists():
        S = json.loads(STATE.read_text())
        if S.get("schema") == SCHEMA:
            for v in S["variants"].values():  # bring old genomes inside today's bounds; missing genes get defaults
                v["genome"] = clamp(v["genome"])
            return S
        # rules changed: start variants fresh, keep pause flag and Telegram offset
        return {"schema": SCHEMA, "variants": {}, "next_id": 1, "acache": {},
                "halted": S.get("halted", False), "tg_offset": S.get("tg_offset")}
    return {"schema": SCHEMA, "variants": {}, "next_id": 1, "halted": False, "acache": {}, "tg_offset": None}


def save(S: dict):
    S["acache"] = dict(sorted(S["acache"].items(), key=lambda kv: kv[1]["ts"])[-150:])
    retired = sorted((v for v in S["variants"].values() if v["status"] == "retired"), key=lambda x: x["created"])
    for v in retired[:-KEEP_RETIRED]:  # keep the file small; remember how many were dropped
        if v["positions"]:
            continue  # retired with a trade still open: kept until the trade closes
        del S["variants"][v["id"]]
        S["retired_dropped"] = S.get("retired_dropped", 0) + 1
    for v in S["variants"].values():
        if v["status"] == "retired" and len(v["closed"]) > 60:
            v["closed"] = v["closed"][-60:]
        if len(v.get("recovery") or []) > 20:  # recovery trades only matter for the money, already counted
            v["recovery"] = v["recovery"][-20:]
        v["cooldown"] = dict(sorted(v["cooldown"].items(), key=lambda kv: kv[1])[-50:])
    STATE.write_text(json.dumps(S, separators=(",", ":")))  # compact: a third smaller to write and to commit


# ---------- genome ----------
def clamp(gn: dict) -> dict:
    out = {}
    for k, (lo, hi) in BOUNDS.items():
        dflt = GENE_DEFAULT.get(k, lo if k not in ("max_m5", "max_age_min") else hi)
        x = min(hi, max(lo, float(gn.get(k, dflt))))
        out[k] = int(round(x)) if k in INTS else round(x, 2)
    out["sl"] = min(out["sl"], round(1.5 * out["tp"], 2))
    if out["max_m5"] < out["min_m5"] + 1:
        out["max_m5"] = round(out["min_m5"] + 1, 2)
    out["ps_frac"] = min(PS_CHOICES, key=lambda c: abs(c - out["ps_frac"]))  # snap to 10/25/50/75/100% (or off)
    return out


def random_genome() -> dict:
    gn = {}
    for k, (lo, hi) in BOUNDS.items():
        if k in OPTIONAL:
            lo2, hi2 = OPTIONAL[k]
            gn[k] = 0 if random.random() < 0.7 else random.uniform(lo2, hi2)
        elif k == "max_m5":
            gn[k] = M5_OFF if random.random() < 0.7 else math.exp(random.uniform(math.log(10), math.log(500)))
        elif k == "min_vol_h1":
            gn[k] = 0
        elif k in LOG_KEYS:
            gn[k] = math.exp(random.uniform(math.log(lo), math.log(hi)))
        else:
            gn[k] = random.uniform(lo, hi)
    gn["ps_frac"] = 0  # no partial sells for the 300 bots (bf2874b strategy)
    return clamp(gn)


def fitness(v: dict) -> float:
    c = test_trades(v)
    if not c:
        return -999
    wr = sum(1 for t in c if t["win"]) / len(c)
    return sum(t["pnl_pct"] for t in c) / len(c) + 5 * wr


def learn_targets(S: dict, gn: dict) -> dict:
    """What did winning trades look like across every variant? Suggested thresholds for filters that are ON."""
    trades = [t for v in S["variants"].values() if v["created"] >= S.get("restart_ts", 0)  # this population only
              for t in test_trades(v) if t.get("feat")]
    wins = [t["feat"] for t in trades if t["win"]]
    if len(trades) < 20 or len(wins) < 5:
        return {}

    def pct(vals, q):
        vals = sorted(vals)
        return vals[int(q * (len(vals) - 1))]

    out = {}
    for gene, feat, q in (("min_m5", "m5", 0.2), ("min_txns", "txns", 0.2), ("min_vol_h1", "vol_h1", 0.2),
                          ("min_liq", "liq", 0.2), ("min_score", "score", 0.2), ("min_holders", "holders", 0.2),
                          ("max_m5", "m5", 0.8), ("max_top10", "top10", 0.8), ("max_age_min", "age", 0.8)):
        if gene in OPTIONAL and float(gn.get(gene, 0)) == 0:
            continue  # filter is off; the toggle mutation decides whether to turn it on
        if gene == "max_m5" and float(gn.get(gene, M5_OFF)) >= M5_OFF:
            continue
        vals = [w[feat] for w in wins if w.get(feat) is not None]
        if len(vals) >= 5:
            out[gene] = pct(vals, q)
    return out


def mutate(parent: dict, S: dict):
    gn, notes = dict(parent["genome"]), []
    for gene, target in learn_targets(S, gn).items():
        if random.random() < 0.5:
            gn[gene] = 0.5 * float(gn[gene]) + 0.5 * float(target)
            notes.append(f"{gene} moved toward what winning trades had")
    losses = [t for t in parent["closed"] if not t["win"]]
    if losses:
        top, _ = Counter(t["why"] for t in losses).most_common(1)[0]
        if top in ("stop-loss", "crash exit"):
            gn["min_holders"] = float(gn["min_holders"]) * 1.5
            gn["max_top10"] = float(gn["max_top10"]) * 0.85
            notes.append(f"losses were {top}s: stricter rug filter (more holders, lower top-10 share)")
        elif top == "liquidity collapse":
            gn["min_liq"] = float(gn["min_liq"]) * 1.5
            gn["min_holders"] = float(gn["min_holders"]) * 1.3
            notes.append("losses were liquidity pulls: higher liquidity and holder minimums")
        elif top == "max hold time":
            gn["hold"] = float(gn["hold"]) * 0.8
            gn["tp"] = float(gn["tp"]) * 0.9
            notes.append("losses were timeouts: shorter hold, slightly lower TP")
        elif top == "trailing-stop":
            gn["trail"] = float(gn["trail"]) * 1.25
            notes.append("losses were trailing stops: looser trail")
    if random.random() < 0.3:  # explore a bigger target; stop-loss stays, hold scales with it (clamped to 1-5)
        f = random.uniform(1.3, 2.0)
        gn["tp"] = float(gn["tp"]) * f
        gn["hold"] = float(gn["hold"]) * f ** 0.5
        notes.append(f"trying a higher take-profit (x{f:.1f}), same stop-loss")
    if random.random() < 0.25:  # toggle one optional filter on or off
        k = random.choice(list(OPTIONAL))
        if float(gn.get(k, 0)) == 0:
            gn[k] = random.uniform(*OPTIONAL[k])
            notes.append(f"switched on optional filter {k}")
        else:
            gn[k] = 0
            notes.append(f"dropped optional filter {k}")
    for k in random.sample(list(BOUNDS), random.randint(1, 3)):
        if k in OPTIONAL and float(gn.get(k, 0)) == 0:
            continue
        gn[k] = float(gn.get(k, BOUNDS[k][0])) * random.uniform(0.8, 1.2)
        notes.append(f"jitter {k}")
    return clamp(gn), notes


def new_variant(S, genome, gen, parent, notes, line=None):
    par = S["variants"].get(parent or "")
    line = line or (par.get("line") if par else None)  # children of a breeding line stay in it
    if line:
        S.setdefault("line_next", {})
        S["line_next"][line] = S["line_next"].get(line, 0) + 1
        vid = f"{line}-{S['line_next'][line]:02d}"
    else:
        vid = f"v{S['next_id']}"
        S["next_id"] += 1
    v = {"id": vid, "gen": gen, "parent": parent, "genome": genome, "notes": notes, "status": "testing",
         "reason": "", "paper_usd": START_USD, "start_usd": START_USD, "positions": {}, "closed": [],
         "cooldown": {}, "created": time.time(), "val_start": 0}
    if line:
        v["line"] = line
    S["variants"][vid] = v
    inherit_arm(S, v)  # children stay in their parent's v3.2 entry-test group
    return v


def spawn(S) -> tuple[dict, str]:
    if S.get("cross_bred"):  # selective breeding: new bots only from the X1 line
        parent = line_parent(S)
        if parent:
            gn, notes = mutate(parent, S)
            v = new_variant(S, gn, parent["gen"] + 1, parent["id"], notes)
            return v, f"{v['id']} gen{v['gen']} from {parent['id']}: " + "; ".join(notes)
    seeds_used = S["next_id"] - 1
    if seeds_used < len(SEEDS):
        v = new_variant(S, clamp(SEEDS[seeds_used]), 0, None, ["seed"])
        return v, f"{v['id']} (seed)"
    pool = [x for x in S["variants"].values() if len(test_trades(x)) >= 5 and x["created"] >= S.get("restart_ts", 0)]
    if not pool and random.random() < 0.6:
        base = clamp(random.choice(SEEDS))
        gn = {k: (float(x) * random.uniform(0.75, 1.3) if isinstance(x, (int, float)) and not isinstance(x, bool) and x else x)
              for k, x in base.items()}
        v = new_variant(S, clamp(gn), 0, None, ["seed jitter"])
        return v, f"{v['id']} (seed variation)"
    if pool and random.random() > 0.25:
        proven = [x for x in pool if x.get("proven") and x["status"] != "retired"]
        parent = max(proven or pool, key=fitness)
        gn, notes = mutate(parent, S)
        v = new_variant(S, gn, parent["gen"] + 1, parent["id"], notes)
        return v, f"{v['id']} gen{v['gen']} from {parent['id']}: " + "; ".join(notes)
    v = new_variant(S, random_genome(), 0, None, ["random exploration"])
    return v, f"{v['id']} (random exploration)"


def active(S):
    return [v for v in S["variants"].values() if v["status"] in ("testing", "validating")]


def ensure_population(S, events):
    if S.get("breed_hold"):  # line X1 founders under test: no new bots until the owner picks the next parents
        return
    made = []
    while len(active(S)) < pop_target(S):
        v, desc = spawn(S)
        made.append((v, desc))
    if len(made) > 4:
        events.append(f"SPAWNED {len(made)} new bots ({made[0][0]['id']}-{made[-1][0]['id']}); population now {pop_target(S)}")
    else:
        for v, desc in made:
            events.append(f"SPAWNED {desc}\n  genome: {v['genome']}")


# ---------- judging ----------
def judge(v: dict):
    if v["status"] not in ("testing", "validating"):
        return None
    validating = v["status"] == "validating"
    T = test_trades(v)
    if validating:
        if not v.get("val_ts"):  # bots that began validating before this was stored by time
            c = v["closed"]
            v["val_ts"] = c[v["val_start"]]["ts"] if len(c) > v.get("val_start", 0) else time.time()
        w = [t for t in T if t["ts"] >= v["val_ts"]]
    else:
        w = T
    n = len(w)
    total = sum(t["pnl_usd"] for t in v["closed"])  # drawdown is about money: every real trade counts
    if total < -v["start_usd"] * MAX_DD_PCT / 100:
        return ("retire", f"drawdown {total:+.2f} USD")
    if validating:
        if n >= VAL_CHECK and not v.get("vchk"):
            ok, why = meets(w)
            if not ok:
                return ("retire", f"{why} in validation (likely overfit)")
            return ("vcheck", "")
        if n >= VAL_TRADES:
            ok, why = meets(w)
            return ("validated", "") if ok else ("retire", f"{why} in validation (likely overfit)")
        return None
    chk = v.get("chk", 0)
    if chk == 0 and n >= CHECK_1:           # check 1: after 5 trades
        ok, why = meets(w)
        if not ok:
            return ("retire", f"failed {CHECK_1}-trade check: {why}")
        if n < CHECK_2:
            return ("check1", "")
    if n >= CHECK_2:                         # check 2: after 10 trades, same requirements
        ok, why = meets(w)
        if not ok:
            return ("retire", f"failed {CHECK_2}-trade check: {why}")
        return ("graduate", "")
    return None


# Growth (owner, 2026-10-24): every hour GROW_PER_HOUR new bots bred from the strongest bots join the population,
# up to GROW_MAX. Under Momentum v3.1 the genes that matter are the stop-loss, the partial-sell level and the stake.
GROW_PER_HOUR = int(os.getenv("EVO_GROW_PER_HOUR", "20"))
GROW_MAX = int(os.getenv("EVO_GROW_MAX", "600"))     # population ceiling (state size, Notion and API limits)
GROW_PARENTS = 5


def pop_target(S) -> int:
    if S.get("pop_cap"):  # set by the owner (cut_population): a fixed size
        return S["pop_cap"]
    return min(GROW_MAX, POP + S.get("grown", 0))


def strongest(S, n=GROW_PARENTS) -> list:
    """The best current bots: fitness on their counted trades, using only trades since Momentum v3.1 went live once a
    bot has 3+ of them (they reflect today's rules), otherwise all its counted trades (5+ needed)."""
    since = S.get("momentum31") or 0
    scored = []
    for v in S["variants"].values():
        if v["status"] == "retired":
            continue
        T = test_trades(v)
        recent = [t for t in T if t["ts"] >= since]
        use = recent if since and len(recent) >= 3 else T if len(T) >= 5 else None
        if use:
            wr = sum(1 for t in use if t["win"]) / len(use)
            scored.append((sum(t["pnl_pct"] for t in use) / len(use) + 5 * wr, sum(t["pnl_usd"] for t in use), v))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [v for _, _, v in scored[:n]]


def grow(S, events):
    """Once an hour: GROW_PER_HOUR children of the strongest bots (mutated, so they explore around the best genes)."""
    now = time.time()
    if GROW_PER_HOUR <= 0 or S.get("pop_cap") or now - S.get("last_grow", 0) < 3600 or pop_target(S) >= GROW_MAX:
        return
    parents = strongest(S)
    if not parents:
        return
    S["last_grow"] = now
    add = min(GROW_PER_HOUR, GROW_MAX - pop_target(S))
    S["grown"] = S.get("grown", 0) + add
    for i in range(add):
        par = parents[i % len(parents)]
        gn, notes = mutate(par, S)
        new_variant(S, gn, par["gen"] + 1, par["id"], [f"strongest-genes batch: child of {par['id']}"] + notes)
    events.append(f"🆕 +{add} bots bred from the strongest genes ({', '.join(p['id'] for p in parents)}). "
                  f"Population now {pop_target(S)}" + (f" (ceiling {GROW_MAX})." if pop_target(S) >= GROW_MAX else "."))


# selective breeding (owner, 2026-10-25): line X1 starts from 10 founders, generation 1, with deliberately different
# genes, so their trades show which settings work. (A first plan crossed v2171 x v1844, but under Momentum v3.2 those
# two are near-copies in every gene that is used, so it would have made 10 copies of one strategy.) The genes that
# decide trades are spread over their whole usable range: 5 m momentum window (3-15%), minimum liquidity
# ($30k-120k), stop-loss (6-8%) and, since v3.3, the partial-sell ladder (first sale +10/20/30%, every +10/20%, a
# quarter or half); each level is used about equally often and every founder has its own window. The stake is the same for all (31%), so their dollar results compare directly. The
# genes v3.2 ignores are spread at random within their bounds, for when the momentum rules are switched off.
# Until the owner picks the 2 parents of the next generation (after about 15-30 trades each), the founders are never
# retired by the checks, they get first pick of the per-coin slots, and no new bots are bred (breed_hold).
CROSS_PARENTS = ("v2171", "v1844")  # the two kept through the population cut
LINE = "X1"
POP_CAP = int(os.getenv("EVO_POP_CAP", "60"))  # owner: 50 main bots + the 10 of the line
# v3.3 partial-sell ladder per founder: first sale at +ps_at% gross, then every +step% higher, frac of what is left
#            5 m window   min liq  stop-loss  ps_at  step  frac
X1_DESIGN = [((3, 5), 30000, 6.0, 10.0, 10.0, 0.25),
             ((3, 8), 80000, 7.0, 20.0, 20.0, 0.5),
             ((4, 7), 50000, 8.0, 30.0, 10.0, 0.25),
             ((5, 8), 120000, 6.0, 20.0, 10.0, 0.5),
             ((6, 10), 30000, 8.0, 10.0, 20.0, 0.5),
             ((8, 12), 80000, 6.0, 30.0, 20.0, 0.25),
             ((8, 15), 50000, 7.0, 10.0, 10.0, 0.5),
             ((10, 15), 120000, 8.0, 20.0, 20.0, 0.25),
             ((3, 15), 50000, 7.0, 30.0, 10.0, 0.5),
             ((12, 15), 80000, 7.0, 20.0, 20.0, 0.25)]
X1_STAKE = 0.31
X1_READY = (15, 30)  # trades each founder needs before the owner is told the ranking (15 first, again at 30)


def founders(S) -> list:
    return sorted((v for v in S["variants"].values() if v.get("founder") and v.get("line") == LINE), key=lambda v: v["id"])


def line_parent(S):
    """Best bot of the line by fitness (5+ counted trades), else a random live one, else the best retired one."""
    L = [v for v in S["variants"].values() if v.get("line") == LINE]
    live = [v for v in L if v["status"] != "retired"]
    scored = [v for v in live if len(test_trades(v)) >= 5]
    if scored:
        return max(scored, key=fitness)
    if live:
        return random.choice(live)
    return max(L, key=fitness) if L else None


def x1_genome(i: int) -> dict:
    (lo, hi), liq, sl, ps, step, frac = X1_DESIGN[i]
    rnd = random.Random(f"X1-{i + 1}")  # the same founder every time this runs
    gn = {k: (rnd.uniform(*b) if k not in OPTIONAL or rnd.random() < 0.5 else 0) for k, b in BOUNDS.items()}
    gn.update(min_m5=float(lo), max_m5=float(hi), min_liq=float(liq), sl=sl, ps_at=ps, ps_step=step, ps_frac=frac,
              size_frac=X1_STAKE)
    return clamp(gn)


def crossbreed(S, events):
    """One-off: the 10 founders of line X1 (X1_DESIGN). If an earlier version already made X1 bots, the first 10
    get the founder genes in place (same ids) and any later X1 bots retire. Their records start over when v3.3
    goes live (introduce_v33 moves earlier trades to closed_pre)."""
    if S.get("x1_founders"):
        return
    S["x1_founders"] = time.time()
    S["cross_bred"] = S.get("cross_bred") or S["x1_founders"]
    S["breed_hold"] = True
    old = sorted((v for v in S["variants"].values() if v.get("line") == LINE), key=lambda v: v["id"])
    made = []
    for i in range(len(X1_DESIGN)):
        (lo, hi), liq, sl, ps, step, frac = X1_DESIGN[i]
        note = (f"X1 founder (gen 1): 5 m +{lo}-{hi}%, liquidity ${liq / 1000:.0f}k+, stop-loss {sl:g}%, sells "
                f"{frac:.0%} of the rest at +{ps:g}% and every +{step:g}% higher")
        if i < len(old):
            v = old[i]
            v.update(genome=x1_genome(i), gen=1, parent=None, notes=[note], status="testing", reason="")
            v.pop("parent2", None)
            v["val_start"] = len(v["closed"])
        else:
            v = new_variant(S, x1_genome(i), 1, None, [note], line=LINE)
        v["founder"] = True
        v.pop("m5_arm", None)  # not part of the +3-5% / +8-15% test
        made.append(v["id"])
    for v in old[len(X1_DESIGN):]:
        v["status"], v["reason"] = "retired", "made before the X1 founders were redesigned"
    events.append(f"🧬 Line {LINE}: {len(made)} founder bots ({made[0]} to {made[-1]}), generation 1, each with "
                  f"different genes (5 m windows from +3-5% to +12-15%, liquidity $30k-120k, stop-loss 6-8%, partial "
                  f"sells from +10/20/30%). They get first pick of trades and are not retired; no new bots are bred until you "
                  f"pick the next 2 parents (after {X1_READY[0]}-{X1_READY[1]} trades each).")


def founder_report(S, events):
    """When every founder reaches X1_READY[0] closed trades (and again at X1_READY[1]): their ranking, once each."""
    F = founders(S)
    if not F or S.get("x1_picked"):
        return
    n_min = min(len(v["closed"]) for v in F)
    for level in X1_READY:
        key = f"x1_report_{level}"
        if n_min < level or S.get(key):
            continue
        S[key] = time.time()
        rows = []
        for v in sorted(F, key=lambda v: -sum(t["pnl_usd"] for t in v["closed"])):
            c = v["closed"]
            w = sum(1 for t in c if t["pnl_usd"] > 0)
            (lo, hi), liq, sl, ps, step, frac = X1_DESIGN[int(v["id"].split("-")[1]) - 1]
            rows.append(f"{v['id']}: {len(c)} trades, {w} won, {sum(t['pnl_usd'] for t in c):+.2f} USD, "
                        f"{sum(t['pnl_pct'] for t in c) / len(c):+.1f}%/trade, {len({t['mint'] for t in c})} coins"
                        f"  (+{lo}-{hi}%, ${liq / 1000:.0f}k, SL {sl:g}%, sells {frac:.0%} from +{ps:g}% every +{step:g}%)")
        events.append(f"🧬 Line {LINE}: every founder has {level}+ trades. Ranking by profit:\n" + "\n".join(rows)
                      + "\nTell me which 2 to breed the next generation from.")


def cut_population(S, events):
    """One-off (owner): keep the best main bots (v2171 and v1844 always) so that, with the line, the population is
    POP_CAP; retire the rest (open trades finish first)."""
    if S.get("pop_cut") or not S.get("cross_bred"):
        return
    S["pop_cut"] = time.time()
    S["pop_cap"] = POP_CAP
    main = [v for v in active(S) if not v.get("line")]
    keep_n = max(0, POP_CAP - sum(1 for v in active(S) if v.get("line")))
    rank = sorted(main, key=lambda v: (v["id"] in CROSS_PARENTS, len(test_trades(v)) >= 5, fitness(v),
                                       sum(t["pnl_usd"] for t in v["closed"]), random.random()), reverse=True)
    for v in rank[keep_n:]:
        v["status"], v["reason"] = "retired", f"population cut to {keep_n} main bots + line {LINE} (owner)"
    events.append(f"🧬 Population cut: {min(keep_n, len(rank))} main bots kept, {max(0, len(rank) - keep_n)} retired "
                  f"(open trades finish first). Cap {POP_CAP}: replacements are bred from line {LINE}.")


CLONE_ID = os.getenv("EVO_CLONE_ID", "v1092")  # the only one of the strongest bots in profit (2026-10-24 12:15 UTC)


def clone_best(S, events):
    """One-off (owner, 2026-10-24): every bot gets CLONE_ID's genes, each slightly different (every numeric gene
    nudged by up to 8%), so they explore just around them. CLONE_ID itself stays; every other active bot is retired
    (open trades are finished first) and replaced. Replaces the strongest-genes rebuild, which is skipped."""
    if S.get("cloned_best"):
        return
    best = S["variants"].get(CLONE_ID)
    if not best or best["status"] == "retired":
        best = (strongest(S) or [None])[0]
    if not best:
        return
    now = S["cloned_best"] = time.time()
    S.setdefault("replaced_strong", now)
    old = [v for v in active(S) if v is not best]
    for v in old:
        v["status"], v["reason"] = "retired", f"replaced by a copy of {best['id']}"
    for _ in old:
        gn = {k: (float(x) * random.uniform(0.92, 1.08) if isinstance(x, (int, float)) and not isinstance(x, bool) and x
                  else x) for k, x in best["genome"].items()}
        new_variant(S, clamp(gn), best["gen"] + 1, best["id"], [f"copy of {best['id']} (slightly different)"])
    events.append(f"🆕 All bots now carry {best['id']}'s genes, each slightly different ({len(old)} replaced). "
                  f"No limit on bots per coin. Still Momentum v3.1.")


def fix_overloss(S, events):
    """One-off (2026-10-25): a rug exit (price near 0, minus costs and the 10% rug-exit penalty) booked 10 CHUNKEE
    trades at -112%, more than the stake. Losses are now capped at -100%; this corrects those records, the bots'
    funds and the overall total to -100%."""
    if S.get("overloss_fixed"):
        return
    S["overloss_fixed"] = time.time()
    n, back = 0, 0.0
    for v in S["variants"].values():
        for t in v["closed"]:
            if t["pnl_pct"] < -100:
                diff = t["pnl_usd"] * (100 / -t["pnl_pct"] - 1)  # > 0: the part of the loss beyond the stake
                t["pnl_usd"] += diff
                t["pnl_pct"] = -100.0
                v["paper_usd"] += diff
                n, back = n + 1, back + diff
    if n:
        S["total_pnl"] = (S.get("total_pnl") or 0.0) + back
        events.append(f"⚠️ Correction: {n} trades were booked at a loss bigger than their stake. Losses are now "
                      f"capped at -100%; the records and the total were corrected (+${back:.2f}).")


def spread_entry_genes(S, events):
    """One-off (2026-10-24): give every active bot except CLONE_ID its own v3.1 entry window (see m31_window), so
    bots differ in which coins they buy and the 10 slots per coin go to bots that want it. CLONE_ID keeps v3.1's."""
    if S.get("spread_entry") or not MOMENTUM:
        return
    S["spread_entry"] = time.time()
    bots = [v for v in active(S) if v["id"] != CLONE_ID]
    for v in bots:
        gn = dict(v["genome"], min_m5=random.uniform(3, 6), max_m5=random.uniform(8, 15),
                  min_liq=random.uniform(30000, 90000))
        v["genome"] = clamp(gn)
        v["notes"].append("own v3.1 entry window: m5 %.1f-%.1f%%, liquidity $%.0fk+" % (
            v["genome"]["min_m5"], v["genome"]["max_m5"], v["genome"]["min_liq"] / 1000))
    events.append(f"🧬 Evolution tunes entries again: {len(bots)} bots each got their own entry window inside "
                  f"Momentum v3.1 (5 m momentum 3-15%, liquidity $30k+, never looser). Max 10 bots per coin stays.")


def replace_with_strongest(S, events):
    """One-off (owner, 2026-10-24): the whole population becomes the strongest genes. The parents of the first
    strongest-genes batch stay, the batch stays, and every other active bot is retired (an open trade is finished
    first) and replaced by a mutated child of those parents, so all bots are the same as or close to the best.
    Momentum v3.1 stays on for everyone."""
    if S.get("replaced_strong") or not S.get("grown"):
        return
    batch = [v for v in active(S) if v["notes"] and v["notes"][0].startswith("strongest-genes batch")]
    pids = list(dict.fromkeys(v["parent"] for v in batch if v.get("parent") in S["variants"]))
    parents = [S["variants"][p] for p in pids] or strongest(S)
    if not parents:
        return
    S["replaced_strong"] = time.time()
    keep = {v["id"] for v in batch} | {p["id"] for p in parents}
    old = [v for v in active(S) if v["id"] not in keep]
    for v in old:
        v["status"], v["reason"] = "retired", "replaced by a child of the strongest genes"
    for i in range(len(old)):
        par = parents[i % len(parents)]
        gn, notes = mutate(par, S)
        new_variant(S, gn, par["gen"] + 1, par["id"], [f"strongest-genes population: child of {par['id']}"] + notes)
    events.append(f"🆕 Whole population rebuilt from the strongest genes: {len(old)} bots replaced by children of "
                  f"{', '.join(p['id'] for p in parents)}. Still Momentum v3.1. Population {len(active(S))}.")


def meets(w: list) -> tuple[bool, str]:
    """The profitability requirements: win rate >= REQ_WR and net PnL positive."""
    n, wins = len(w), sum(1 for t in w if t["win"])
    pnl = sum(t["pnl_usd"] for t in w)
    wr = wins / n if n else 0
    if wr < REQ_WR:
        return False, f"win rate {wr:.0%} ({wins}/{n}), needs {REQ_WR:.0%}"
    if pnl <= 0:
        return False, f"win rate {wr:.0%} but net {pnl:+.2f} USD"
    return True, ""


def breed_proven(S, v, events):
    """Meets the requirements (win rate >= REQ_WR, net positive) after CHECK_1 trades: stays alive and breeds a
    mutated child; again at CHECK_2 and then every further 10 closed trades while it keeps meeting them."""
    if S.get("breed_hold"):
        return
    c = test_trades(v)
    n = len(c)
    if n < CHECK_1 or not meets(c)[0]:
        return
    wins = sum(1 for t in c if t["win"])
    total = sum(t["pnl_usd"] for t in c)
    level = (n >= CHECK_1) + (n >= CHECK_2) + max(0, (n - CHECK_2) // 10)
    v["proven"] = True
    while (v.get("bred_lvl", 0) < level and v["status"] != "retired"
           and (not S.get("cross_bred") or v.get("line"))  # only the X1 line breeds now
           and len(active(S)) < (int(S["pop_cap"] * 1.3) if S.get("pop_cap") else max(MAX_POP, int(pop_target(S) * 1.3)))):
        v["bred_lvl"] = v.get("bred_lvl", 0) + 1
        gn, notes = mutate(v, S)
        ch = new_variant(S, gn, v["gen"] + 1, v["id"], ["bred from proven " + v["id"]] + notes)
        if v.get("sprint"):
            ch["sprint"] = True  # children of Sprint bots trade the Sprint way too
        events.append(f"PROVEN {v['id']}: {wins}/{n} wins ({wins / n:.0%}), net {total:+.2f} USD. Stays alive and bred "
                      f"{ch['id']} gen{ch['gen']}\n  {'; '.join(notes)}")


def apply_judgement(S, v, events):
    if v.get("founder") and not S.get("x1_picked"):
        return  # X1 founders trade on until the owner picks; the checks don't retire them
    breed_proven(S, v, events)
    res = judge(v)
    if not res:
        return
    kind, why = res
    T = test_trades(v)
    n, wins = len(T), sum(1 for t in T if t["win"])
    pnl = sum(t["pnl_usd"] for t in v["closed"])
    if kind == "retire":
        v["status"], v["reason"] = "retired", why
        events.append(f"RETIRED {v['id']} gen{v['gen']}: {why}")
        ensure_population(S, events)
    elif kind == "check1":
        v["chk"] = 1
    elif kind == "vcheck":
        v["vchk"] = True
    elif kind == "graduate":
        v["chk"] = 2
        v["status"], v["val_start"], v["val_ts"] = "validating", len(v["closed"]), time.time()
        events.append(f"GRADUATED {v['id']}: passed the {CHECK_1}- and {CHECK_2}-trade checks ({wins}/{n} wins, "
                      f"net {pnl:+.2f} USD). Now validating on {VAL_TRADES} fresh trades.")
    elif kind == "validated":
        v["status"] = "validated"
        events.append(f"⭐ Bot {v['id']} passed its final test: {wins}/{n} wins, "
                      f"{'-' if pnl < 0 else '+'}${abs(pnl):.2f} (paper).")


# ---------- milestone: enough consistently profitable bots -> spread their strategies -> prove it again ----------
GOAL_BOTS = int(os.getenv("EVO_GOAL_BOTS", "75"))           # consistent bots needed to trigger the milestone
CONS_TRADES = int(os.getenv("EVO_CONS_TRADES", "20"))       # closed trades a bot needs to count as consistent
CONS_TOKENS = int(os.getenv("EVO_CONS_TOKENS", "10"))       # ...spread over at least this many different tokens
PROOF_HOURS = float(os.getenv("EVO_PROOF_HOURS", "48"))     # length of the re-test after strategies are spread
PROOF_TRADES = int(os.getenv("EVO_PROOF_TRADES", "10"))     # per-bot trades inside the re-test window
PROOF_TOKENS = int(os.getenv("EVO_PROOF_TOKENS", "5"))


def consistent(trades: list, min_trades=CONS_TRADES, min_tokens=CONS_TOKENS) -> bool:
    """Reliably profitable, not lucky: enough trades over enough different tokens, win rate >= REQ_WR, net profit,
    and still in profit after removing its single best token."""
    if len(trades) < min_trades:
        return False
    by_token: dict = {}
    for t in trades:
        by_token[t["mint"]] = by_token.get(t["mint"], 0.0) + t["pnl_usd"]
    if len(by_token) < min_tokens or not meets(trades)[0]:
        return False
    return sum(by_token.values()) - max(by_token.values()) > 0


def consistent_bots(S) -> list:
    return [v for v in S["variants"].values() if v["status"] != "retired" and consistent(test_trades(v))]


def milestone(S, events):
    """Stage 1: GOAL_BOTS bots are consistent -> Telegram alert, every other bot is replaced by a mutated child of a
    consistent one, and a PROOF_HOURS re-test starts on fresh trades only. Stage 2: the re-test passes (population
    in profit on the new trades and GOAL_BOTS bots consistent on them) -> 'ready for a live test' alert. A failed
    re-test resets to stage 1 and evolution carries on. Paper only either way: nothing here trades live."""
    M = S.setdefault("milestone", {"stage": "searching"})
    now = time.time()
    if M["stage"] == "searching":
        good = consistent_bots(S)
        M["count"] = len(good)
        if len(good) < GOAL_BOTS:
            return
        good.sort(key=lambda v: -sum(t["pnl_usd"] for t in v["closed"]))
        replaced = 0
        for v in [x for x in active(S) if x not in good]:
            v["status"], v["reason"] = "retired", "replaced by a consistent strategy (milestone)"
            replaced += 1
        for i in range(replaced):  # refill with children of the consistent bots, best first
            parent = good[i % len(good)]
            gn, notes = mutate(parent, S)
            new_variant(S, gn, parent["gen"] + 1, parent["id"], ["milestone: bred from consistent " + parent["id"]] + notes)
        M.update(stage="proving", since=now, hit_count=len(good), pnl_at_start=S.get("total_pnl") or 0.0,
                 trades_at_start=S.get("total_trades", 0))
        events.append(f"🎯 Goal reached: {len(good)} bots are reliably profitable.\n"
                      f"Their strategies were copied to the rest. {PROOF_HOURS:.0f}h proof test started.")
    elif M["stage"] == "proving" and now - M["since"] >= PROOF_HOURS * 3600:
        # every trade since the test began counts, including bots retired during it (overall running total)
        pnl = (S.get("total_pnl") or 0.0) - M["pnl_at_start"]
        n = S.get("total_trades", 0) - M["trades_at_start"]
        ok_bots = sum(1 for v in active(S) if consistent([t for t in test_trades(v) if t["ts"] >= M["since"]],
                                                         PROOF_TRADES, PROOF_TOKENS))
        if pnl > 0 and ok_bots >= GOAL_BOTS:
            M.update(stage="ready", ready_at=now, proof_pnl=pnl, proof_bots=ok_bots)
            events.append(f"✅ Proof test passed: +${pnl:,.2f} over {n} trades.\n"
                          f"Ready for a small live test, your call. Still paper only.")
        else:
            M.update(stage="searching", last_fail=now,
                     last_fail_why=f"{n} fresh trades, net {pnl:+.2f} USD, {ok_bots}/{GOAL_BOTS} bots consistent")
            events.append(f"❌ Proof test failed: P&L {'-' if pnl < 0 else '+'}${abs(pnl):,.2f}, "
                          f"{ok_bots}/{GOAL_BOTS} reliable bots. Back to improving.")


# ---------- market ----------
async def get_pair(g, mint, pc, ttl):
    hit = pc.get(mint)
    if hit and time.time() - hit[0] < (ttl if hit[1] else min(ttl, 15)):  # retry "no pair yet" sooner (new tokens)
        return hit[1]
    p = await auto.best_pair(g, mint)
    pc[mint] = (time.time(), p)
    return p


def pair_age_min(p: dict, now: float | None = None):
    created = p.get("pairCreatedAt")
    if not created:
        return None
    return max(0.0, ((now or time.time()) * 1000 - float(created)) / 60000)


def rug_ok(a: dict, p: dict) -> tuple[bool, str]:
    """Can we reason that this is NOT a rug? Every check must pass. Missing holder data = can't reason = no trade.
    Checks (1) on-chain control (mint/freeze authority, transfer fee, LP lock), (2) who holds it (single wallet,
    creator, insiders, top 10, holder count), (3) what the market is doing (sell pressure, falling knife, wash volume,
    liquidity vs valuation, honeypot pattern). Extra RugCheck fields are only used when present."""
    if a.get("rugged"):
        return False, "RugCheck marks it rugged"
    if a.get("danger"):
        return False, "RugCheck danger-level risk"
    age = pair_age_min(p)
    if age is None:
        return False, "pair age unknown"
    if age < HARD_MIN_AGE:
        return False, f"pair only {age:.0f} min old (min {HARD_MIN_AGE:.0f})"
    h, t = a.get("holders"), a.get("top10")
    if h is None:
        return False, "holder data unavailable"
    if t is None or a.get("mint_auth") is None or a.get("freeze_auth") is None:
        return False, "safety data unavailable"  # unknown is not safe: no top-10 list or no authority fields
    # --- on-chain control
    if a.get("mint_auth"):
        return False, "mint authority not revoked (supply can be inflated)"
    if a.get("freeze_auth"):
        return False, "freeze authority not revoked (wallets can be frozen)"
    if (a.get("fee_pct") or 0) > 2:
        return False, f"transfer fee {a['fee_pct']}%"
    for r in a.get("risks") or []:
        if any(k in r for k in ("honeypot", "rugged", "copycat", "freeze", "mint authority", "creator history")):
            return False, f"RugCheck risk: {r}"
    dex = (p.get("dexId") or "").lower()
    lp = a.get("lp_locked")
    if lp is not None and dex not in ("pumpfun",) and lp < MIN_LP_LOCKED:
        return False, f"only {lp:.0f}% of LP locked/burned"
    # --- holders
    if h < HARD_MIN_HOLDERS:
        return False, f"only {h} holders"
    if t >= HARD_MAX_TOP10:
        return False, f"top 10 hold {t}%"
    if a.get("top1") is not None and a["top1"] > HARD_MAX_TOP1:
        return False, f"one wallet holds {a['top1']}%"
    if (a.get("insider_pct") or 0) > HARD_MAX_INSIDER:
        return False, f"insider wallets hold {a['insider_pct']}%"
    if (a.get("creator_pct") or 0) > HARD_MAX_CREATOR:
        return False, f"creator holds {a['creator_pct']}%"
    # --- market behaviour
    liq = (p.get("liquidity") or {}).get("usd", 0) or 0
    if liq < HARD_MIN_LIQ:
        return False, f"liquidity ${liq:,.0f}"
    fdv = p.get("fdv") or p.get("marketCap") or 0
    if fdv and liq and fdv / liq > 300:
        return False, f"valuation {fdv / liq:.0f}x liquidity (cannot exit size)"
    tx = p.get("txns") or {}
    h1, m5 = tx.get("h1") or {}, tx.get("m5") or {}
    if h1.get("buys", 0) >= 8 and h1.get("sells", 0) == 0:
        return False, "buys but no sells (honeypot pattern)"
    if m5.get("sells", 0) >= 10 and m5["sells"] > 3 * max(m5.get("buys", 0), 1):
        return False, "heavy sell pressure (5m)"
    if h1.get("sells", 0) >= 30 and h1["sells"] > 2 * max(h1.get("buys", 0), 1):
        return False, "heavy sell pressure (1h)"
    ch = p.get("priceChange") or {}
    if (ch.get("m5") or 0) <= -12:
        return False, f"price already falling ({ch['m5']}% in 5m)"
    if (ch.get("h1") or 0) <= -40:
        return False, f"price already dumped ({ch['h1']}% in 1h)"
    vol_h1 = (p.get("volume") or {}).get("h1", 0) or 0
    if liq and vol_h1 / liq > 100:
        return False, "volume/liquidity absurdly high (wash trading)"
    return True, ""


def entry_ok(p: dict, gn: dict, age_min) -> bool:
    """Optional, evolved filters. At seed settings these are all off except token age."""
    ch = p.get("priceChange") or {}
    m5 = ch.get("m5") or 0
    tx = (p.get("txns") or {}).get("m5") or {}
    n_tx = tx.get("buys", 0) + tx.get("sells", 0)
    vol = (p.get("volume") or {}).get("h1", 0) or 0
    liq = (p.get("liquidity") or {}).get("usd", 0) or 0
    if age_min is None:
        if gn["max_age_min"] < ANY_AGE:
            return False  # can't verify it's a fresh launch
    elif age_min > gn["max_age_min"]:
        return False
    return (gn["min_m5"] <= m5 <= gn["max_m5"] and n_tx >= gn["min_txns"]
            and vol >= gn["min_vol_h1"] and liq >= gn["min_liq"])


def features(p: dict, a: dict, age_min) -> dict:
    ch = p.get("priceChange") or {}
    tx = (p.get("txns") or {}).get("m5") or {}
    return {"m5": ch.get("m5") or 0, "h1": ch.get("h1") or 0,
            "liq": (p.get("liquidity") or {}).get("usd", 0) or 0,
            "vol_h1": (p.get("volume") or {}).get("h1", 0) or 0,
            "txns": tx.get("buys", 0) + tx.get("sells", 0),
            "holders": a.get("holders"), "top10": a.get("top10"), "score": a.get("score"), "age": age_min}


# ---------- exits ----------
def decide_exit(pos: dict, price: float, liq: float, now: float) -> str | None:
    """Update the position's running state and return an exit reason, or None. Pure logic, no I/O."""
    age_min = (now - pos["ts"]) / 60
    gross = (price / pos["entry_price"] - 1) * 100
    prev = pos.get("last_price", price)
    pos["last_price"] = price
    pos["peak_price"] = max(pos.get("peak_price", pos["entry_price"]), price)
    peak_gross = (pos["peak_price"] / pos["entry_price"] - 1) * 100
    if peak_gross >= COST_PCT + 1.0:
        pos["armed"] = True                          # trailing stop only switches on once past break-even
    floor = COST_PCT + 0.5                           # never let an armed trade fall back below ~break-even
    trail_gross = (pos["peak_price"] * (1 - pos["trail"] / 100) / pos["entry_price"] - 1) * 100
    in_profit = pos.get("armed") and gross > floor

    # emergency exits: allowed at any time, even inside the minimum hold
    if liq < pos["entry_liq"] * (1 - RUG_LIQ_DROP):
        return "liquidity collapse"
    if prev > 0 and price < prev * (1 - CRASH_TICK_PCT / 100):
        return "crash exit"
    if gross <= -pos["sl"]:
        return "stop-loss"
    # discretion: once clearly in profit the bot decides for itself when to leave
    if prev > 0:
        pos["down_ticks"] = pos.get("down_ticks", 0) + 1 if price < prev else (0 if price > prev else pos.get("down_ticks", 0))
    if gross >= DISCRETION_PCT:
        if (1 - price / pos["peak_price"]) * 100 >= pos.get("lock_pull", GENE_DEFAULT["lock_pull"]):
            return "profit lock (pullback)"
        if pos.get("down_ticks", 0) >= pos.get("stall", GENE_DEFAULT["stall"]):
            return "profit lock (momentum stall)"
    if gross >= pos["tp"]:
        return "take-profit"
    if pos.get("armed") and gross <= max(floor, trail_gross):
        return "trailing-stop"
    cap = HARD_HOLD if in_profit else min(pos["hold"], MAX_HOLD_FLAT)  # 5 min unless in profit (then up to 30)
    if pos.get("max_hold"):
        cap = min(cap, pos["max_hold"])  # Sprint trades: short, even in profit
    if age_min >= cap:
        return "max hold time"
    return None


# ---------- jobs ----------
async def prefetch(g, pc, mints, ttl):
    """Refresh the price cache for many mints with batch requests (30 per call) instead of one call each."""
    now = time.time()
    stale = list({m for m in mints if not (pc.get(m) and now - pc[m][0] < ttl)})
    if stale:
        for m, p in (await auto.best_pairs(g, stale)).items():
            pc[m] = (now, p)


async def partial_sell(g, S, v, mint, pos, price, now, frac=None):
    """Sell a fraction of what is left of an open position at the current price (ps_frac, once, before v3.3; under
    v3.3 `frac` each time the ladder says so). The profit on that part is booked at once (bot funds, overall total,
    Notion rows labelled 'Partial sell N%'); the rest stays open with the bot's normal exits."""
    f = pos["ps_frac"] if frac is None else frac
    label = f"Partial sell {f:.0%}"
    net = (price / pos["entry_price"] - 1) * 100 - COST_PCT
    part_cost, part_tokens = pos["cost_usd"] * f, pos["size_tokens"] * f
    pnl = part_cost * net / 100
    pos["ps_done"] = True
    pos["ps_n"] = pos.get("ps_n", 0) + 1
    pos["realized_usd"] = pos.get("realized_usd", 0.0) + pnl
    orig = pos.get("orig_cost") or pos["cost_usd"]
    pos["part"] = pos["realized_usd"] / orig * 100  # v3.3: whole-trade bookkeeping for m33_step
    pos["left"] = (pos["cost_usd"] - part_cost) / orig
    pos["cost_usd"] -= part_cost
    pos["size_tokens"] -= part_tokens
    v["paper_usd"] += pnl
    S["total_pnl"] = cum = (S.get("total_pnl") or 0.0) + pnl
    # journalled in the background like entries and exits: under v3.3 bots sell parts often, and waiting for Notion
    # here held up every other bot's exit checks. Values are taken now; the page id when the job runs (the entry row
    # may still be on its way)
    left = {"properties": {"Size (Tokens)": auto._num(pos["size_tokens"]), "Cost Basis (USD)": auto._num(pos["cost_usd"])}}
    closed_row = {
            "Token": auto._title(pos["name"]), "Mint": auto._text(mint),
            "Entry Time": auto._date(pos["ts"]), "Exit Time": auto._date(now),
            "Entry Price (USD)": auto._num(pos["entry_price"]), "Exit Price (USD)": auto._num(price),
            "Size (Tokens)": auto._num(part_tokens), "Hold Time (hrs)": auto._num((now - pos["ts"]) / 3600),
            "Realised PnL (USD)": auto._num(pnl), "PnL %": auto._num(net / 100),
            "Result": auto._sel("Win" if pnl > 0 else "Loss"), "Exit Reason": auto._sel(label),
            "Score": auto._num(pos.get("score")), "Holders": auto._num(pos.get("holders")),
            "Top 10 %": auto._num(pos.get("top10")), "Mode": auto._sel("Paper"),
            "Notes": auto._text(f"Variant {v['id']} gen{v['gen']} | {label} of the position, the rest stays open")}
    event_row = {
            "Event": auto._title(f"{label} {pos['name']} [{v['id']}]"), "Type": auto._sel(label),
            "Token": auto._text(pos["name"]), "Time": auto._date(now), "Price (USD)": auto._num(price),
            "Realised PnL (USD)": auto._num(pnl), "Cumulative PnL (USD)": auto._num(cum), "Mode": auto._sel("Paper")}
    _journal(lambda pos=pos, b=left: auto.n_patch(g, pos.get("page_id"), b),
             lambda r=closed_row: auto.n_create(g, auto.DB_CLOSED, r),
             lambda r=event_row: auto.n_create(g, auto.DB_EVENTS, r))


_jup: dict = {}


async def checked_price(g, mint, price, ref):
    """DexScreener sometimes serves a wrong price for a coin (24 Oct: TRUTH stuck at $0.001077 for over half an hour
    while it really traded at $0.00014-0.00019, so bots 'sold' at +500-650% that never existed). A price that moved
    PRICE_CHECK_PCT% or more since the last check is confirmed with Jupiter first; if the two disagree by more than
    25% Jupiter's price is used. No answer from Jupiter -> None (skip this check, try again next tick)."""
    if not ref or abs(price / ref - 1) * 100 < PRICE_CHECK_PCT:
        return price
    hit = _jup.get(mint)
    if not hit or time.time() - hit[0] > 8:
        js = await auto._get_json(g, "jup_price", f"{JUP_PRICE}?ids={mint}", cool=30)
        jp = ((js or {}).get(mint) or {}).get("usdPrice")
        hit = _jup[mint] = (time.time(), float(jp) if jp else None)
    jp = hit[1]
    if not jp:
        return None
    if abs(price / jp - 1) > 0.25:
        log.warning("price check %s: DexScreener %.3g vs Jupiter %.3g, using Jupiter", mint[:6], price, jp)
        return jp
    return price


async def fix_bad_prices(g, S, events):
    """One-off repair: the TRUTH trades closed at DexScreener's wrong $0.001077 (see checked_price) are re-booked at
    what the coin really traded at, about the entry price (minute candles: within a few % of entry when they 'sold'),
    so each counts as a loss of the trading costs. Corrects bot funds, the overall total, wins, the proven flag, and
    adds one Correction row to Trade Events so the journal's P&L chart drops by the same amount."""
    if S.get("price_fix"):
        return
    S["price_fix"] = time.time()
    since = 1792800000  # 2026-10-24 00:00 UTC
    delta, n = 0.0, 0
    for v in S["variants"].values():
        hit = False
        for t in v["closed"]:
            if t["mint"] == BAD_PRICE_MINT and t["ts"] >= since and t["pnl_pct"] >= 100:
                cost = t["pnl_usd"] / (t["pnl_pct"] / 100)
                new = -cost * COST_PCT / 100
                delta += new - t["pnl_usd"]
                v["paper_usd"] += new - t["pnl_usd"]
                S["total_wins"] = max(0, S.get("total_wins", 0) - t["win"])
                t.update(pnl_pct=-COST_PCT, pnl_usd=new, win=False, why=t["why"] + " (bad price, corrected)")
                n += 1
                hit = True
        if hit and v.get("proven") and not proven_now(v):
            v["proven"] = False
    if not n:
        return
    S["total_pnl"] = cum = (S.get("total_pnl") or 0.0) + delta
    events.append(f"⚠️ Fix: {n} TRUTH trades were booked at a wrong price from DexScreener. Corrected by "
                  f"-${-delta:,.2f} (total now {'-' if cum < 0 else '+'}${abs(cum):,.2f}).")
    now = time.time()
    await auto.n_create(g, auto.DB_EVENTS, {
        "Event": auto._title(f"Correction TRUTH bad price ({n} trades)"), "Type": auto._sel("Correction"),
        "Token": auto._text("TRUTH"), "Time": auto._date(now), "Realised PnL (USD)": auto._num(delta),
        "Cumulative PnL (USD)": auto._num(cum), "Mode": auto._sel("Paper")})


BAD_PRICE_MINT = "6bW6xsZxYqcvqabk9rRgQwgjPNwe8SACPusx5o1wpump"
BAD_PRICE_MIN = 0.0009  # TRUTH really traded at $0.00014-0.00024 that night; every row at or above this used the fake price


async def _n_query(g, db, flt) -> list:
    rows, cursor = [], None
    while True:
        r = await g["http"].post(f"https://api.notion.com/v1/databases/{db}/query", headers=auto._h(),
                                 json={"page_size": 100, "filter": flt, **({"start_cursor": cursor} if cursor else {})})
        r.raise_for_status()
        j = r.json()
        rows += j.get("results", [])
        cursor = j.get("next_cursor")
        if not j.get("has_more"):
            break
    return rows


async def purge_bad_prices(g, S, events):
    """One-off, after fix_bad_prices (owner: only true data anywhere). The corrected TRUTH trades were estimates, so
    they are removed entirely (bot records, funds, counts); bots created during the glitch whose genes were moved
    toward 'what winning trades had' (a set that included the fake wins) get their genes re-derived from their clean
    parent; in Notion the fake rows (Closed Trades and Trade Events at the fake price) and the Correction row are
    archived (Notion trash, restorable) and every running total is recalculated from the remaining rows."""
    if S.get("purge_done") or not S.get("price_fix"):
        return
    S["purge_done"] = time.time()
    removed, regened = 0, []
    for v in S["variants"].values():
        bad = [t for t in v["closed"] if "(bad price, corrected)" in t["why"]]
        if bad:
            v["closed"] = [t for t in v["closed"] if t not in bad]
            v["paper_usd"] -= sum(t["pnl_usd"] for t in bad)
            S["total_pnl"] = (S.get("total_pnl") or 0.0) - sum(t["pnl_usd"] for t in bad)
            S["total_trades"] = max(0, S.get("total_trades", 0) - len(bad))
            removed += len(bad)
            if v.get("proven") and not proven_now(v):
                v["proven"] = False
        par = S["variants"].get(v.get("parent") or "")
        if (par and v["status"] != "retired" and 1792805000 <= v["created"] <= S["price_fix"]
                and any("moved toward what winning trades had" in n for n in v["notes"])):
            gn = {k: (float(x) * random.uniform(0.85, 1.15) if k in ("sl", "trail", "lock_pull", "stall") else x)
                  for k, x in par["genome"].items()}
            v["genome"] = clamp(gn)
            v["notes"] = [f"genes re-derived from {par['id']} (old ones were learned partly from fake TRUTH wins)"]
            regened.append(v["id"])
    archived = 0
    if auto.NOTION_TOKEN:
        try:
            rows = (await _n_query(g, auto.DB_CLOSED, {"and": [
                        {"property": "Token", "title": {"equals": "TRUTH"}},
                        {"property": "Exit Price (USD)", "number": {"greater_than_or_equal_to": BAD_PRICE_MIN}}]})
                    + await _n_query(g, auto.DB_EVENTS, {"or": [
                        {"and": [{"property": "Token", "rich_text": {"equals": "TRUTH"}},
                                 {"property": "Price (USD)", "number": {"greater_than_or_equal_to": BAD_PRICE_MIN}}]},
                        {"property": "Type", "select": {"equals": "Correction"}}]}))
            for row in rows:
                await auto.n_patch(g, row["id"], {"archived": True})
                archived += 1
                await asyncio.sleep(0.35)  # Notion rate limit
            S["cum_fixed"] = False  # run() recalculates every running total from the rows that remain
        except Exception as e:
            log.warning("purge of fake rows in Notion: %s", e)
            S["purge_done"] = None  # try again next run (the state part is already done and won't repeat)
    if removed or archived or regened:
        events.append(f"⚠️ Removed every trace of the fake TRUTH wins: {removed} trades, {archived} Notion rows, "
                      f"genes of {len(regened)} bots rebuilt from clean parents. Only real data left.")


async def monitor(g, S, pc, events, notes_out):
    n_open = sum(len(v["positions"]) for v in S["variants"].values())
    patch_every = max(60, n_open * 0.8)  # keep Notion under its rate limit when many bots are open
    # every open position, retired bots' included (they still exit), so none falls back to one request each
    await prefetch(g, pc, [m for v in S["variants"].values() for m in v["positions"]], MONITOR_S - 1)
    for v in list(S["variants"].values()):
        if v["status"] == "retired" and not v["positions"]:
            continue  # retired bots still see their open trades through to the exit, then stop
        for mint, pos in list(v["positions"].items()):
            try:
                p = await get_pair(g, mint, pc, MONITOR_S - 1)
                if not p or not p.get("priceUsd"):
                    continue
                price = await checked_price(g, mint, float(p["priceUsd"]), pos.get("last_price") or pos["entry_price"])
                if price is None:
                    continue
                liq = float((p.get("liquidity") or {}).get("usd", 0))
                now = time.time()
                gross = (price / pos["entry_price"] - 1) * 100
                why = None
                if pos.get("m33"):
                    act = m33_step(pos, price, liq, now)
                    if act and act[0] == "partial":
                        await partial_sell(g, S, v, mint, pos, price, now, frac=act[1])
                        act = None
                    why = act[1] if act else None
                elif pos.get("ps_frac") and not pos.get("ps_done") and gross >= pos["ps_at"]:
                    if pos["ps_frac"] >= 1:
                        why = "Partial sell 100%"  # the whole position goes at the partial-sell level
                    else:
                        await partial_sell(g, S, v, mint, pos, price, now)
                if not pos.get("m33"):
                    why = why or (m31_exit(pos, price, liq, now) if pos.get("m31") else decide_exit(pos, price, liq, now))

                if not why:
                    if now - pos.get("last_patch", 0) >= patch_every:  # keep Notion calls light
                        pos["last_patch"] = now
                        val = pos["size_tokens"] * price
                        _journal(lambda pos=pos, body={"properties": {
                            "Current Price (USD)": auto._num(price), "Current Value (USD)": auto._num(val),
                            "Unrealised PnL (USD)": auto._num(val - pos["cost_usd"]),
                            "Unrealised PnL %": auto._num(gross / 100)}}: auto.n_patch(g, pos.get("page_id"), body))
                    continue

                slip = RUG_EXIT_SLIP if why in ("liquidity collapse", "crash exit") else 0.0
                net = max(-100.0, gross - COST_PCT - slip)  # a position can't lose more than it cost
                pnl_usd = pos["cost_usd"] * net / 100  # what is left of the position (all of it without a partial)
                v["paper_usd"] += pnl_usd
                # the trade as a whole (partial sell + the rest) is what the bot is tested on
                whole_usd = pnl_usd + pos.get("realized_usd", 0.0)
                whole_pct = whole_usd / (pos.get("orig_cost") or pos["cost_usd"]) * 100
                rec = {"mint": mint, "pnl_pct": whole_pct, "pnl_usd": whole_usd, "win": whole_usd > 0,
                       "why": why, "ts": now, "entry_ts": pos.get("ts"), "feat": pos.get("feat"), "name": pos["name"],
                       "partial": pos["ps_frac"] if pos.get("ps_done") else None, "partials": pos.get("ps_n", 0)}
                # append-only ledger of every closed trade (never pruned, unlike the per-bot lists), so the totals
                # can be reconciled; pnl_usd is the whole trade (partial sell included), as in the bot's record
                S.setdefault("ledger", []).append(
                    {"mint": mint, "name": pos["name"], "bot": v["id"], "entry_ts": pos.get("ts"), "ts": now,
                     "pnl_usd": whole_usd, "pnl_pct": whole_pct, "why": why, **({"rec": True} if pos.get("recovery") else {})})
                if pos.get("recovery"):  # funds only: kept out of the bot's testing record
                    v.setdefault("recovery", []).append(rec)
                    R = S.setdefault("recovery_stats", {"n": 0, "wins": 0, "pnl": 0.0, "paused": False})
                    R["n"] += 1; R["wins"] += net > 0; R["pnl"] += pnl_usd
                    if not R["paused"] and R["n"] >= RECOVERY_CHECK_N and R["pnl"] < 0:
                        R["paused"] = True
                        events.append(f"⚠️ Recovery trades paused: {R['n']} trades lost "
                                      f"${-R['pnl']:,.2f}. Small targets can't beat trading costs here.")
                elif pos.get("pre"):  # opened before the founder's record started (introduce_v33): money only
                    v.setdefault("closed_pre", []).append(rec)
                else:
                    v["closed"].append(rec)
                S["total_wins"] = S.get("total_wins", 0) + (whole_usd > 0)
                del v["positions"][mint]
                # overall running total across every bot (what the journal's P&L chart plots)
                S["total_pnl"] = cum = (S.get("total_pnl") or 0.0) + pnl_usd
                S["total_trades"] = S.get("total_trades", 0) + 1
                # the whole trade decides the label: after a partial sell the rest can lose while the trade won
                result = "Win" if whole_usd > 0 else "Loss" if whole_usd < 0 else "Breakeven"
                # journalled in the background (like entries), so a crowd of bots exiting one coin never delays
                # the exits of the others; pos["page_id"] is read when the job runs, after its entry row exists
                closed_row = {
                    "Token": auto._title(pos["name"]), "Mint": auto._text(mint),
                    "Entry Time": auto._date(pos["ts"]), "Exit Time": auto._date(now),
                    "Entry Price (USD)": auto._num(pos["entry_price"]), "Exit Price (USD)": auto._num(price),
                    "Size (Tokens)": auto._num(pos["size_tokens"]), "Hold Time (hrs)": auto._num((now - pos["ts"]) / 3600),
                    "Realised PnL (USD)": auto._num(pnl_usd), "PnL %": auto._num(net / 100),
                    "Result": auto._sel(result), "Exit Reason": auto._sel(why),
                    "Score": auto._num(pos.get("score")), "Holders": auto._num(pos.get("holders")),
                    "Top 10 %": auto._num(pos.get("top10")), "Mode": auto._sel("Paper"),
                    "Notes": auto._text(f"Variant {v['id']} gen{v['gen']} | peak "
                                        f"{(pos['peak_price'] / pos['entry_price'] - 1) * 100:+.0f}% | trail {pos['trail']}%"
                                        + (f" | rest after partial sell {pos['ps_frac']:.0%}; whole trade "
                                           f"{whole_usd:+.2f} USD ({whole_pct:+.1f}%)" if pos.get("ps_done") else ""))}
                event_row = {
                    "Event": auto._title(f"{'Exit' if why != 'Partial sell 100%' else why} {pos['name']} [{v['id']}]"),
                    "Type": auto._sel("Exit" if why != "Partial sell 100%" else why),
                    "Token": auto._text(pos["name"]), "Time": auto._date(now), "Price (USD)": auto._num(price),
                    "Realised PnL (USD)": auto._num(pnl_usd), "Cumulative PnL (USD)": auto._num(cum),
                    "Mode": auto._sel("Paper")}
                _journal(lambda pos=pos: auto.n_patch(g, pos.get("page_id"), {"archived": True}),
                         lambda r=closed_row: auto.n_create(g, auto.DB_CLOSED, r),
                         lambda r=event_row: auto.n_create(g, auto.DB_EVENTS, r))
                if NOTIFY_TRADES or why in ("liquidity collapse", "crash exit"):
                    events.append(f"[{v['id']}] closed {pos['name']} ({why}) {net:+.1f}% = {pnl_usd:+.2f} USD")
                if not pos.get("recovery") and v["status"] != "retired":
                    apply_judgement(S, v, events)
            except Exception as e:
                log.warning("monitor %s %s: %s", v["id"], mint, e)


async def scan(g, S, pc, events):
    if S["halted"]:
        return
    free = [v for v in active(S) + [x for x in S["variants"].values() if x["status"] == "validated"]
            if not v["positions"]]
    if not free:
        return
    now = time.time()
    for v in list(free):  # retire variants that never trade
        if (not v["closed"] and now - v["created"] > STALE_HOURS * 3600 and v["status"] == "testing"
                and not v.get("founder")):
            v["status"], v["reason"] = "retired", f"no trades in {STALE_HOURS}h (entry rules too tight)"
            events.append(f"RETIRED {v['id']} gen{v['gen']}: {v['reason']}")
            free.remove(v)
            ensure_population(S, events)
    random.shuffle(free)

    cands = (await auto.discover(g, HARD_MIN_AGE))[:CANDIDATE_LIMIT]
    g["cands"] = cands  # the trainee researches from the same list
    await prefetch(g, pc, cands, 15)  # one request per 30 coins instead of one each
    analyses = 0
    ok_tokens = []  # coins that passed every safety rule this scan (recovery trades pick from these)
    for mint in cands:
        if not free:
            break
        p = await get_pair(g, mint, pc, 60)
        if not p or not p.get("priceUsd"):
            continue
        age = pair_age_min(p, now)
        if age is None or age < HARD_MIN_AGE:
            continue  # too new for the rug rules; don't spend one of the few analyses per scan on it
        p_liq = (p.get("liquidity") or {}).get("usd", 0) or 0
        # quiet token: on established tokens these almost never moved enough to pay the costs
        quiet = p_liq and ((p.get("volume") or {}).get("h1", 0) or 0) / p_liq < HARD_MIN_VOL_LIQ
        if MOMENTUM:  # Momentum v3.1: one rule set for every bot
            wanting = ([v for v in free if now - v["cooldown"].get(mint, 0) > COOLDOWN_MIN * 60
                        and m31_gene_ok(p, v["genome"])] if not quiet and m31_pair_ok(p, age) else [])
        else:
            wanting = [v for v in free if v.get("sprint")  # Sprint bots: no entry filters or cooldown, safety rules only
                       or (not quiet and now - v["cooldown"].get(mint, 0) > COOLDOWN_MIN * 60
                           and entry_ok(p, v["genome"], age))]
        if not wanting:
            continue
        bad = coin_losing(S, mint, now) if MOMENTUM else None
        if bad:
            rs = S.setdefault("rejects", {})
            rs[bad] = rs.get(bad, 0) + 1
            continue
        ac = S["acache"].get(mint)
        if ac and now - ac["ts"] < ACACHE_TTL_MIN * 60:
            a = ac["a"]
        else:
            if analyses >= ANALYZE_LIMIT:
                continue
            analyses += 1
            try:
                full = await g["analyze"](mint)
            except Exception as e:
                log.warning("analyze %s: %s", mint, e)
                continue
            a = {k: full.get(k) for k in ("score", "liq", "holders", "top10", "symbol")}
            a["danger"] = any("[danger]" in f for f in (full.get("flags") or []))
            rc = full.get("rc") or {}
            for k in ("mint_auth", "freeze_auth", "rugged", "top1", "insider_pct", "creator_pct", "lp_locked", "fee_pct", "risks"):
                if rc.get(k) is not None:
                    a[k] = rc[k]
            ts_cache = now
            if a["holders"] is None or a["top10"] is None or a.get("mint_auth") is None or a.get("freeze_auth") is None:
                ts_cache = now - (ACACHE_TTL_MIN * 60 - 120)  # safety data not ready: retry in ~2 min
            S["acache"][mint] = {"ts": ts_cache, "a": a}

        ok, why = rug_ok(a, p)
        if not ok:
            log.info("skip %s: %s", mint, why)
            rs = S.setdefault("rejects", {})
            key = re.sub(r"[\d.]+", "#", why)[:48]
            rs[key] = rs.get(key, 0) + 1
            if len(rs) > 40:
                for k in sorted(rs, key=rs.get)[:10]:
                    del rs[k]
            continue
        ok_tokens.append((mint, p, a, age))
        paths.mark(S, mint, p, a, "scan")  # follow its price for the benchmarks, bought or not
        if MOMENTUM and not m31_holders_ok(a):
            continue
        holders = [vv for vv in S["variants"].values() if mint in vv["positions"]]
        slots = {False: MAX_PER_TOKEN - sum(1 for vv in holders if not vv.get("sprint")),
                 True: SPRINT_PER_TOKEN - sum(1 for vv in holders if vv.get("sprint"))}
        if max(slots.values()) <= 0:
            continue

        random.shuffle(wanting)
        wanting.sort(key=lambda v: not v.get("founder"))  # X1 founders first, so they reach 15-30 trades sooner
        entered = []
        for v in wanting:
            sprint = bool(v.get("sprint"))
            if slots[sprint] <= 0:
                continue
            gn = v["genome"]
            size_usd = v["paper_usd"] * gn["size_frac"]
            if MOMENTUM:
                ex = m33_exits(gn) if M33 else m31_exits(gn)
            elif sprint:
                ex = sprint_exits(gn)
            else:
                if a["holders"] < gn["min_holders"] or a["top10"] > gn["max_top10"] or (a["score"] or 0) < gn["min_score"]:
                    continue
                exp_net = size_usd * (gn["tp"] - COST_PCT) / 100
                target = max(TARGET_USD, float(gn.get("target_usd", TARGET_USD)))
                if exp_net < target * 0.9:
                    continue  # position too small to net the variant's dollar target at its TP
                ex = gn
            if not proven_now(v):  # same trade, smaller stake until the bot has proven itself
                size_usd *= UNPROVEN_SIZE
            await open_position(g, v, mint, p, a, age, size_usd, ex)
            free.remove(v)
            slots[sprint] -= 1
            entered.append(v)
        if entered:
            coin_alert(S, events, mint, p, a, entered)

    await recovery_trades(g, S, pc, free, ok_tokens)


COIN_REPORT_KEEP = int(os.getenv("EVO_COIN_REPORT_KEEP", "150"))  # coins whose latest report is kept in the state


def coin_alert(S, events, mint, p, a, entered):
    """Fact-based report on a coin the bots just entered (coin_report.py), stored in S["coin_reports"] (not sent to Telegram).
    Its evidence score is saved on every new trade, so reviews can check whether higher scores actually win."""
    import coin_report
    now = time.time()
    name = auto.token_name(a, mint)
    try:
        sc, text = coin_report.report(name, mint, p, a, coin_report.track_record(S, mint, now), now)
    except Exception as e:  # a report must never stop trading
        log.warning("coin report %s: %s", mint, e)
        return
    for v in entered:
        v["positions"][mint]["feat"]["evidence"] = sc
    # stored only, not sent to Telegram (owner, 2026-10-25): the latest report per coin, the newest COIN_REPORT_KEEP
    R = S.setdefault("coin_reports", {})
    R[mint] = {"ts": now, "score": sc, "name": name, "text": text}
    for k in sorted(R, key=lambda m: R[m]["ts"])[:max(0, len(R) - COIN_REPORT_KEEP)]:
        del R[k]


async def open_position(g, v, mint, p, a, age, size_usd, ex, recovery=False):
    """Open a paper position for bot v with exit settings ex (tp, sl, hold, trail, lock_pull, stall)."""
    price = float(p["priceUsd"])
    liq = float((p.get("liquidity") or {}).get("usd", 0))
    slip = LAUNCH_SLIP if (age is not None and age < 15) else 0.0
    fill = price * (1 + slip / 100)
    name = auto.token_name(a, mint)
    size_tokens = size_usd / fill
    ts = time.time()
    kind = " | recovery trade" if recovery else ""
    open_row = {
        "Token": auto._title(name), "Mint": auto._text(mint), "Entry Time": auto._date(ts),
        "Entry Price (USD)": auto._num(fill), "Current Price (USD)": auto._num(price),
        "Size (Tokens)": auto._num(size_tokens), "Cost Basis (USD)": auto._num(size_usd),
        "Current Value (USD)": auto._num(size_usd), "Unrealised PnL (USD)": auto._num(0),
        "Unrealised PnL %": auto._num(0), "Score": auto._num(a["score"]),
        "Holders": auto._num(a["holders"]), "Top 10 %": auto._num(a["top10"]),
        "Mode": auto._sel("Paper"),
        "Notes": auto._text(f"Variant {v['id']} gen{v['gen']} | age "
                            f"{'?' if age is None else f'{age:.0f}m'} | TP {ex['tp']:.1f}% SL {ex['sl']}% "
                            f"trail {ex['trail']}% hold {ex['hold']}m{kind}")}
    event_row = {"Event": auto._title(f"Entry {name} [{v['id']}]"), "Type": auto._sel("Entry"),
                 "Token": auto._text(name), "Time": auto._date(ts), "Price (USD)": auto._num(fill),
                 "Mode": auto._sel("Paper")}
    v["positions"][mint] = {"name": name, "entry_price": fill, "entry_liq": liq, "ts": ts,
                            "cost_usd": size_usd, "size_tokens": size_tokens, "score": a["score"],
                            "holders": a["holders"], "top10": a["top10"], "tp": ex["tp"], "sl": ex["sl"],
                            "hold": ex["hold"], "trail": ex["trail"], "page_id": None,
                            "lock_pull": ex["lock_pull"], "stall": ex["stall"],
                            "feat": features(p, a, age), "peak_price": fill, "last_price": price,
                            "recovery": recovery, "ps_frac": ex.get("ps_frac", 0), "ps_at": ex.get("ps_at", 0),
                            "orig_cost": size_usd, "realized_usd": 0.0, "max_hold": ex.get("max_hold"),
                            "m31": bool(ex.get("m31")), "lock": None,
                            "m33": bool(ex.get("m33")), "ps_step": ex.get("ps_step"), "ps_next": ex.get("ps_at", 0),
                            "part": 0.0, "left": 1.0}
    v["cooldown"][mint] = ts
    v["last_entry"] = ts
    pos = v["positions"][mint]
    # Notion rows are written in the background at a steady pace: with no limit on bots per coin, dozens of entries
    # can arrive in one scan, and waiting for Notion there would delay every bot's exits
    task = asyncio.create_task(_journal_entry(g, pos, open_row, event_row))
    _BG.add(task)
    task.add_done_callback(_BG.discard)


_BG: set = set()
_NOTION_SLOTS: list = []  # one asyncio.Semaphore, made inside the running loop


def _slot() -> asyncio.Semaphore:
    if not _NOTION_SLOTS:
        _NOTION_SLOTS.append(asyncio.Semaphore(2))
    return _NOTION_SLOTS[0]


def _journal(*calls):
    """Run Notion writes (functions returning coroutines) in the background, sharing the entry rows' pace."""
    async def job():
        async with _slot():
            await asyncio.gather(*(c() for c in calls))
            await asyncio.sleep(0.35)  # Notion allows about 3 requests a second
    task = asyncio.create_task(job())
    _BG.add(task)
    task.add_done_callback(_BG.discard)


async def _journal_entry(g, pos, open_row, event_row):
    async with _slot():
        page_id, _ = await asyncio.gather(auto.n_create(g, auto.DB_OPEN, open_row), auto.n_create(g, auto.DB_EVENTS, event_row))
        pos["page_id"] = page_id
        await asyncio.sleep(0.35)  # Notion allows about 3 requests a second


def last_entry(v) -> float:
    return v.get("last_entry") or max(list(v["cooldown"].values()) + [v["created"]])


async def recovery_trades(g, S, pc, free, ok_tokens):
    """Recovery trades: a bot that has not opened a trade for RECOVERY_IDLE_MIN minutes takes a small, quick one
    (+RECOVERY_NET_PCT% net target) on the most active coin that passed every safety rule this scan. They only exist
    to win back paper funds a little at a time: they are kept apart from the bot's real trades and never count in
    its testing (checks, proven, validation, reliable bots). Paused automatically if they lose money overall."""
    R = S.setdefault("recovery_stats", {"n": 0, "wins": 0, "pnl": 0.0, "paused": False})
    if not RECOVERY_ON or R["paused"] or S["halted"]:
        return
    now = time.time()
    idle = sorted((v for v in free if not v["positions"] and v["status"] != "retired" and v["paper_usd"] > 1
                   and now - last_entry(v) >= RECOVERY_IDLE_MIN * 60), key=last_entry)  # longest idle first
    if not idle:
        return
    seen = {m for m, *_ in ok_tokens}
    for mint, ac in S["acache"].items():  # coins checked in earlier scans are fine too, if still safe
        hit = pc.get(mint)
        if mint in seen or not hit or not hit[1] or now - hit[0] > 60 or now - ac["ts"] > ACACHE_TTL_MIN * 60:
            continue
        p, age = hit[1], pair_age_min(hit[1], now)
        liq = (p.get("liquidity") or {}).get("usd", 0) or 0
        if (p.get("priceUsd") and age is not None and age >= HARD_MIN_AGE and liq
                and ((p.get("volume") or {}).get("h1", 0) or 0) / liq >= HARD_MIN_VOL_LIQ and rug_ok(ac["a"], p)[0]):
            ok_tokens.append((mint, p, ac["a"], age))

    def activity(item):  # rising coins first, then the most traded relative to liquidity
        p = item[1]
        ch = p.get("priceChange") or {}
        liq = (p.get("liquidity") or {}).get("usd", 0) or 1
        return ((ch.get("m5") or 0) > 0 and (ch.get("h1") or 0) > 0, ((p.get("volume") or {}).get("h1", 0) or 0) / liq)

    coins = sorted(ok_tokens, key=activity, reverse=True)
    ex = {"tp": RECOVERY_NET_PCT + COST_PCT, "sl": RECOVERY_SL, "hold": RECOVERY_HOLD, "trail": 30,
          "lock_pull": 15, "stall": 30}  # take the small profit at once; no trailing or discretionary exits
    for mint, p, a, age in coins:
        held = sum(1 for vv in S["variants"].values() if mint in vv["positions"])
        while idle and held < MAX_PER_TOKEN:
            v = idle.pop(0)
            if now - v["cooldown"].get(mint, 0) <= COOLDOWN_MIN * 60:
                continue
            await open_position(g, v, mint, p, a, age, v["paper_usd"] * v["genome"]["size_frac"], ex, recovery=True)
            if v in free:
                free.remove(v)
            held += 1
        if not idle:
            break


# ---------- reporting ----------
def _lb_row(v) -> str:
    c = v["closed"]
    n, w = len(c), sum(1 for t in c if t["win"])
    wr = f"{w/n:.0%}" if n else "-"
    vt = v.get("virt") or {}
    return (f"{v['id']} g{v['gen']} {v['status'][:4]}: {w}/{n} ({wr}), {sum(t['pnl_usd'] for t in c):+.2f} USD"
            f"{', OPEN' if v['positions'] else ''}"
            + (f" | replay {vt['n']} trades {vt['avg']:+.1f}%" if v.get("line") and vt.get("n") else ""))


def line_summary(S) -> str | None:
    """One line for the menu and the hourly summary: the X1 line's size, trades and P&L so far."""
    L = [v for v in S["variants"].values() if v.get("line") == LINE]
    if not L:
        return None
    T = [t for v in L for t in v["closed"]]
    return (f"🧬 Line {LINE}: {sum(1 for v in L if v['status'] != 'retired')} bots, {len(T)} trades, "
            f"{sum(1 for t in T if t['pnl_usd'] > 0)} won, {sum(t['pnl_usd'] for t in T):+.2f} USD")


def leaderboard(S) -> str:
    live = [v for v in S["variants"].values() if v["status"] != "retired"]
    live.sort(key=lambda x: -fitness(x))
    line = [v for v in live if v.get("line")]
    rows = ([f"🧬 LINE {LINE} (gen-1 founders, different genes):"] + [_lb_row(v) for v in line] + ["", "Main bots:"]
            if line else [])
    rows += [_lb_row(v) for v in [v for v in live if not v.get("line")][:25 - min(len(line), 15)]]
    by = Counter(v["status"] for v in live)
    retired = sum(1 for v in S["variants"].values() if v["status"] == "retired") + S.get("retired_dropped", 0)
    validated = [v["id"] for v in S["variants"].values() if v["status"] == "validated"]
    traded = sum(1 for v in live if v["closed"])
    head = (f"EVO {'HALTED' if S['halted'] else 'running'} (paper)  {len(live)} bots: {by.get('testing', 0)} testing, "
            f"{by.get('validating', 0)} validating, {by.get('validated', 0)} validated; {traded} have traded\n"
            f"{'Line ' + LINE + ' and top main bots' if line else 'Top 25'} of {len(live)}:\n")
    rj = sorted(S.get("rejects", {}).items(), key=lambda kv: -kv[1])[:5]
    why = ("\nTop reasons tokens were rejected: " + "; ".join(f"{k} x{n}" for k, n in rj)) if rj else ""
    return (head + "\n".join(rows or ["no active variants"]) + f"\nRetired so far: {retired}"
            f"\nValidated candidates: {', '.join(validated) or 'none yet'}" + why)


def ab_lines(S, usd) -> list:
    """v3.2 entry test, judged per decision: bots entering one coin within the same 2 minutes are one decision, so a
    crowd can't make one lucky (or unlucky) call look like many. Bots are grouped by their m5_arm label."""
    since = S.get("m32")
    if not since:
        return []
    out = []
    for arm in M32_ARMS:
        T = [t for v in S["variants"].values() if v.get("m5_arm") == arm for t in v["closed"] if t["ts"] >= since]
        if not T:
            continue
        dec: dict = {}
        for t in T:  # by entry time; trades from before entry_ts was saved fall back to their close time
            k = (t["mint"], round((t.get("entry_ts") or t["ts"]) / 120))
            dec[k] = dec.get(k, 0.0) + t["pnl_usd"]
        pnl = sum(dec.values())
        out.append(f"🧪 {arm} entries: {len(dec)} decisions ({len(T)} trades), {usd(pnl)} = "
                   f"{usd(pnl / len(dec))}/decision, {sum(1 for x in dec.values() if x > 0)} won")
    return out


ALERTS = ("🎯", "✅", "❌", "🆕", "⭐", "⚠️", "🔎", "🧬")  # sent straight away; everything else only feeds the hourly summary
DIGEST_MIN = float(os.getenv("TG_DIGEST_MIN", "60"))


async def flush(tb, g, S, events):
    """Keep Telegram readable at a glance: important alerts go out at once, routine bot activity (spawned, retired,
    proven, rug exits...) is only counted, and one short summary is sent every DIGEST_MIN minutes. Then the menu
    is moved back under the new messages."""
    import menu, trainee, bench
    now = time.time()
    D = S.get("digest")
    if not D:
        R = S.get("recovery_stats") or {}
        D = S["digest"] = {"since": now, "pnl": S.get("total_pnl") or 0.0, "trades": S.get("total_trades", 0),
                           "wins": S.get("total_wins", 0), "new": 0, "retired": 0,
                           "rec_n": R.get("n", 0), "rec_pnl": R.get("pnl", 0.0),
                           "tr": trainee.snapshot(S)}
    msgs = []
    for e in events:
        if e.startswith(ALERTS):
            msgs.append(e[:1500])
        elif e.startswith("SPAWNED"):
            m = re.match(r"SPAWNED (\d+) new bots", e)
            D["new"] += int(m.group(1)) if m else 1
        elif e.startswith("RETIRED"):
            D["retired"] += 1
    events.clear()
    if now - D["since"] >= DIGEST_MIN * 60:
        pnl = (S.get("total_pnl") or 0.0) - D["pnl"]
        n = S.get("total_trades", 0) - D["trades"]
        w = S.get("total_wins", 0) - D["wins"]
        if n or D["new"] or D["retired"]:
            lines = [f"📊 Last {DIGEST_MIN / 60:g}h" if DIGEST_MIN >= 60 else f"📊 Last {DIGEST_MIN:g} min"]
            if n:
                lines.append(f"Trades: {n} ({w} won)")
                lines.append(f"P&L: {menu.usd(pnl)}")
                R = S.get("recovery_stats") or {}
                rn = R.get("n", 0) - D.get("rec_n", 0)
                if rn:
                    lines.append(f"🔁 Recovery: {rn} trades, {menu.usd(R.get('pnl', 0.0) - D.get('rec_pnl', 0.0))}")
            lines.append(f"Total: {menu.usd(S.get('total_pnl') or 0.0)}")
            lines += ab_lines(S, menu.usd)
            if D["new"] or D["retired"]:
                lines.append(f"Bots: {D['new']} new, {D['retired']} retired")
            lines += trainee.digest_lines(S, D)
            lines += bench.digest_lines(S)
            if line_summary(S):
                lines.append(line_summary(S))
            msgs.append("\n".join(lines))
        S["digest"] = None
    chunks = msgs
    for c in chunks[:6]:
        try:
            await menu.say(tb, g, S, c)
        except Exception as ex:
            log.warning("notify failed: %s", ex)
    if chunks:
        try:
            await menu.keep_menu_last(tb, g, S)
        except Exception as ex:
            log.warning("menu repost failed: %s", ex)


async def sweep_orphans(g, S) -> int:
    """Archive Open Trades rows that no live position owns (left behind by schema resets, retirements, crashes)."""
    if not (auto.NOTION_TOKEN and auto.DB_OPEN):
        return 0
    live = {(p.get("page_id") or "").replace("-", "") for v in S["variants"].values() for p in v["positions"].values()}
    pages, cursor = [], None
    try:
        for _ in range(10):
            body = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
            r = await g["http"].post(f"https://api.notion.com/v1/databases/{auto.DB_OPEN}/query", headers=auto._h(), json=body)
            r.raise_for_status()
            j = r.json()
            pages += [x["id"] for x in j.get("results", [])]
            cursor = j.get("next_cursor")
            if not j.get("has_more"):
                break
    except Exception as e:
        log.warning("orphan sweep failed: %s", e)
        return 0
    n = 0
    for pid in pages:
        if pid.replace("-", "") not in live:
            await auto.n_patch(g, pid, {"archived": True})
            n += 1
    return n


async def pnl_history(g, S) -> list:
    """Read every Trade Events row that realised money, in time order, set S["total_pnl"] to the overall realised PnL, and return
    the rows whose 'Cumulative PnL (USD)' is not yet the overall running total (older rows held each bot's own
    total, which made the journal chart meaningless). Fixing them is left to fix_cumulative() in the background."""
    rows, cursor = [], None
    # every row that realised money: exits, partial sells (corrections too, if any are left)
    body = {"page_size": 100, "filter": {"property": "Realised PnL (USD)", "number": {"is_not_empty": True}},
            "sorts": [{"property": "Time", "direction": "ascending"},
                      {"timestamp": "created_time", "direction": "ascending"}]}
    while True:
        r = await g["http"].post(f"https://api.notion.com/v1/databases/{auto.DB_EVENTS}/query", headers=auto._h(),
                                 json={**body, **({"start_cursor": cursor} if cursor else {})})
        r.raise_for_status()
        j = r.json()
        rows += j.get("results", [])
        cursor = j.get("next_cursor")
        if not j.get("has_more"):
            break
    total, todo = 0.0, []
    for row in rows:
        p = row["properties"]
        total += (p.get("Realised PnL (USD)") or {}).get("number") or 0
        if abs(((p.get("Cumulative PnL (USD)") or {}).get("number") or 0) - total) > 0.005:
            todo.append((row["id"], total))
    S["total_pnl"] = total
    return todo


async def fix_cumulative(g, S, todo):
    for pid, total in todo:
        await auto.n_patch(g, pid, {"properties": {"Cumulative PnL (USD)": auto._num(total)}})
        await asyncio.sleep(0.35)  # Notion rate limit, shared with the trading loop
    S["cum_fixed"] = True
    log.info("journal: corrected the running total on %d past exits", len(todo))


async def run(tb, g, loop_seconds: int):
    import menu  # lazy import: menu.py imports this module
    S = load()
    events: list[str] = []
    try:
        await fix_bad_prices(g, S, events)
    except Exception as e:  # the state is already corrected; only the Notion row can fail
        log.warning("bad-price correction row: %s", e)
    await purge_bad_prices(g, S, events)
    restart_population(S, events)  # fresh population (done once); Momentum v2, partial-sell test and Sprint stay off
    fix_age_windows(S, events)     # 4 h minimum back: give the fresh bots real max-age windows again
    introduce_momentum31(S, events)
    clone_best(S, events)              # one-off: every bot a slightly different copy of v1092
    replace_with_strongest(S, events)  # one-off: every bot from the strongest genes (skipped once cloned)
    spread_entry_genes(S, events)      # one-off: each bot its own entry window inside v3.1
    fix_overloss(S, events)            # one-off: trades booked below -100% back to -100%
    introduce_v32(S, events)           # one-off: v3.2 exits, max 3 per coin, +3-5% vs +8-15% entry test
    for v in active(S):                # bots bred before children inherited the label: their parent's group
        if not v.get("m5_arm"):
            inherit_arm(S, v)
    crossbreed(S, events)              # one-off: line X1, 10 founders with different genes
    cut_population(S, events)          # one-off: 50 main bots + the line
    introduce_v33(S, events)           # one-off: 25% minimum take-profit, partial-sell ladder, protective floors
    founder_records_v33(S)             # one-off: founders' records start with v3.3 (earlier trades -> closed_pre)
    grow(S, events)  # 20 children of the strongest bots, once an hour (off once the population is capped)
    ensure_population(S, events)
    pc: dict = {}
    start = time.time()
    last_scan = last_clean = last_goal = last_save = 0.0
    # Telegram runs in its own task with long polling, so taps and messages are answered within about a second
    # instead of waiting for the next 10 s tick and for slow scans / Notion writes to finish.
    tg = asyncio.create_task(menu.poll(tb, g, S, pc))
    import trainee, bench
    tr = asyncio.create_task(trainee.loop(g, S, pc))  # the intensely trained bot, beside the main loop
    rec = asyncio.create_task(paths.loop(g, S, pc))   # price paths of every coin that passes the safety rules
    vs = asyncio.create_task(bench.score_in_background(S))  # every bot replayed on the recorded coins (measurement only)

    async def _exits():  # exits never wait for a scan
        while True:
            try:
                await monitor(g, S, pc, events, None)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("monitor error: %s", e)
            await asyncio.sleep(MONITOR_S)
    mon = asyncio.create_task(_exits())
    family = cumfix = None
    try:
        if auto.NOTION_TOKEN and not S.get("cum_fixed"):
            try:  # one-off: start the overall running total from the journal and correct past rows
                cumfix = asyncio.create_task(fix_cumulative(g, S, await pnl_history(g, S)))
            except Exception as e:
                log.warning("journal running total: %s", e)
        if S.get("total_pnl") is None:
            S["total_pnl"] = sum(t["pnl_usd"] for v in S["variants"].values() for t in v["closed"])
        try:  # before any new entries, so rows being created can't be mistaken for orphans
            n = await sweep_orphans(g, S)
            if n:
                events.append(f"Cleaned {n} stale row(s) from Notion Open Trades")
        except Exception as e:
            log.warning("sweep error: %s", e)

        async def _family():
            import tree
            try:
                await tree.refresh(g, S)
            except Exception as e:
                log.warning("family tree update failed: %s", e)
        family = asyncio.create_task(_family())  # once per run, in the background (it takes a minute or two)
        while True:
            now = time.time()
            try:
                if now - last_scan >= 20:
                    await scan(g, S, pc, events); last_scan = now
                if now - last_goal >= 300:  # consistent-bot milestone and proof test
                    milestone(S, events); grow(S, events); founder_report(S, events); last_goal = now
                if now - last_save >= 60:  # the file is only committed at the end of the run; once a minute is plenty
                    save(S); last_save = now
                await flush(tb, g, S, events)
                if now - last_clean >= 60:  # delete chat messages past their age, keep a menu at the bottom
                    last_clean = now
                    await menu.cleanup(tb, g, S)
                    await menu.keep_menu_last(tb, g, S)
                    await menu.refresh_menu(tb, g, S)  # P&L and last-6-h figures stay current
            except Exception as e:
                log.exception("cycle error: %s", e)
            if time.time() - start + 10 >= loop_seconds:
                break
            await asyncio.sleep(10)
    finally:
        for t in (mon, tg, tr, rec, vs, family, cumfix):  # an unfinished correction simply continues next run
            if t:
                t.cancel()
        await asyncio.gather(*(t for t in (mon, tg, tr, rec, vs, family, cumfix) if t), return_exceptions=True)
        if _BG:  # let queued Notion rows (entries, exits) finish, bounded well inside the job's 32-min timeout
            await asyncio.wait(list(_BG), timeout=180)
        save(S)
