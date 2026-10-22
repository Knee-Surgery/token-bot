"""
Evolutionary paper-trading engine: launch sniping + momentum, rug-risk reasoning, trailing exits.

ENTRY: no fixed strategy filters. A token is only bought if it passes the RUG-RISK REASONING below
(holders >= 50, top-10 holders < 80%, liquidity floor, no RugCheck danger flags, sells actually happening).
Everything else (token age, momentum, volume, score, stricter holder limits) is an OPTIONAL gene: off by default,
switched on or tuned by evolution only if it improves results.

EXIT (per variant genes): stop-loss, take-profit (45%+, aiming for 100%+), a trailing stop measured from the
token's peak price since entry (armed once the trade is past break-even), a time cap (2 to 30 minutes), and
emergency exits for liquidity pulls / crashes that fire even inside the minimum hold time.

EVOLUTION: variants run side by side on the same market data (paper only). A variant is retired if, after
EVO_EVAL_AFTER trades, win rate < target or net PnL < 0 (or drawdown, or it never trades). Each retirement spawns a
child bred from the best performer, nudged by how the parent lost, plus what winning trades had in common across
all variants, plus jitter. Survivors graduate after EVO_GRAD_TRADES trades and must pass a fresh validation window.

Paper only. Paper fills are optimistic, especially at launch: results will be worse live.
"""
import json, math, os, random, time, logging
from collections import Counter
from pathlib import Path

import auto

log = logging.getLogger("evo")
STATE = Path("evo_state.json")

POP = int(os.getenv("EVO_POP", "6"))
START_USD = float(os.getenv("EVO_START_USD", "10"))
TARGET_WR = float(os.getenv("EVO_TARGET_WINRATE", "0.9"))
EVAL_AFTER = int(os.getenv("EVO_EVAL_AFTER", "10"))
GRAD_TRADES = int(os.getenv("EVO_GRAD_TRADES", "30"))
VAL_TRADES = int(os.getenv("EVO_VAL_TRADES", "30"))
MAX_DD_PCT = float(os.getenv("EVO_MAX_DRAWDOWN_PCT", "30"))
COST_PCT = float(os.getenv("EVO_COST_PCT", "2.5"))               # fees + slippage, round trip
TARGET_USD = float(os.getenv("SCALP_TARGET_USD", "0.20"))        # min expected net profit per winning trade
STALE_HOURS = int(os.getenv("EVO_STALE_HOURS", "48"))
NOTIFY_TRADES = os.getenv("EVO_NOTIFY_TRADES", "false").lower() == "true"
ANALYZE_LIMIT = int(os.getenv("EVO_ANALYZE_LIMIT", "4"))         # fresh analyses per scan
CANDIDATE_LIMIT = int(os.getenv("EVO_CANDIDATE_LIMIT", "40"))
COOLDOWN_MIN = 30
ACACHE_TTL_MIN = 30

# hold window: never shorter than MIN_HOLD (except emergency exits), never longer than HARD_HOLD
MIN_HOLD = float(os.getenv("EVO_MIN_HOLD_MIN", "2"))
HARD_HOLD = float(os.getenv("EVO_HARD_HOLD_MIN", "30"))

# rug-risk reasoning (hard rules; evolution can only make them stricter)
HARD_MIN_HOLDERS = int(os.getenv("EVO_MIN_HOLDERS", "50"))
HARD_MAX_TOP10 = float(os.getenv("EVO_MAX_TOP10", "80"))         # top 10 must be strictly below this
HARD_MIN_LIQ = float(os.getenv("EVO_MIN_LIQ", "5000"))
RUG_LIQ_DROP = float(os.getenv("EVO_RUG_LIQ_DROP_PCT", "30")) / 100   # liquidity down this much vs entry = rug exit
CRASH_TICK_PCT = float(os.getenv("EVO_CRASH_TICK_PCT", "15"))         # price down this much between two checks
RUG_EXIT_SLIP = float(os.getenv("EVO_RUG_EXIT_SLIP_PCT", "10"))       # paper penalty: you can't sell at the pre-rug price
LAUNCH_SLIP = float(os.getenv("EVO_LAUNCH_SLIP_PCT", "3"))            # paper penalty on entries into pairs < 15 min old

SCHEMA = 4  # bump to restart all variants from fresh seeds when the rules change
BOUNDS = {"tp": (45, 300), "sl": (1.5, 8), "hold": (2, 15), "trail": (3, 30), "size_frac": (0.1, 0.5),
          "target_usd": (0.2, 1.5), "max_age_min": (3, 10080),
          "min_holders": (HARD_MIN_HOLDERS, 10000), "max_top10": (10, HARD_MAX_TOP10 - 1),
          "min_liq": (HARD_MIN_LIQ, 200000),
          "min_score": (0, 90), "min_m5": (0, 3), "max_m5": (3, 1000), "min_txns": (0, 200),
          "min_vol_h1": (0, 500000)}
INTS = {"hold", "min_score", "min_txns", "min_holders", "max_age_min"}
LOG_KEYS = {"tp", "target_usd", "max_age_min", "min_holders", "min_liq"}
OPTIONAL = {"min_m5": (0.5, 3.0), "min_txns": (10, 100), "min_vol_h1": (5000, 100000), "min_score": (50, 80)}  # 0 = off
ANY_AGE = 10080  # max_age_min at this value = no age limit
M5_OFF = 1000    # max_m5 at this value = no ceiling

_BASE = {"tp": 100, "sl": 3, "hold": 15, "trail": 5, "size_frac": 0.25, "target_usd": 0.2, "max_age_min": 30,
         "min_holders": HARD_MIN_HOLDERS, "max_top10": HARD_MAX_TOP10 - 1, "min_liq": HARD_MIN_LIQ,
         "min_score": 0, "min_m5": 0, "max_m5": M5_OFF, "min_txns": 0, "min_vol_h1": 0}
SEEDS = [
    dict(_BASE),                                                                       # pure launch sniper, 5% trail from peak
    dict(_BASE, max_age_min=120, tp=60, hold=12, trail=8),                             # slightly later entries
    dict(_BASE, max_age_min=ANY_AGE, min_m5=1, min_txns=20, tp=80, trail=8),           # any age, rising only
    dict(_BASE, max_age_min=ANY_AGE, trail=12),                                        # any age, rug checks only
    dict(_BASE, max_age_min=15, min_holders=200, max_top10=50, min_liq=10000, tp=150, trail=15),  # careful launch
]


# ---------- state ----------
def load() -> dict:
    if STATE.exists():
        S = json.loads(STATE.read_text())
        if S.get("schema") == SCHEMA:
            return S
        # rules changed: start variants fresh, keep pause flag and Telegram offset
        return {"schema": SCHEMA, "variants": {}, "next_id": 1, "acache": {},
                "halted": S.get("halted", False), "tg_offset": S.get("tg_offset")}
    return {"schema": SCHEMA, "variants": {}, "next_id": 1, "halted": False, "acache": {}, "tg_offset": None}


def save(S: dict):
    S["acache"] = dict(sorted(S["acache"].items(), key=lambda kv: kv[1]["ts"])[-150:])
    for v in S["variants"].values():
        if v["status"] == "retired" and len(v["closed"]) > 60:
            v["closed"] = v["closed"][-60:]
        v["cooldown"] = dict(sorted(v["cooldown"].items(), key=lambda kv: kv[1])[-50:])
    STATE.write_text(json.dumps(S, indent=1))


# ---------- genome ----------
def clamp(gn: dict) -> dict:
    out = {}
    for k, (lo, hi) in BOUNDS.items():
        x = min(hi, max(lo, float(gn.get(k, lo if k not in ("max_m5", "max_age_min") else hi))))
        out[k] = int(round(x)) if k in INTS else round(x, 2)
    out["sl"] = min(out["sl"], round(1.5 * out["tp"], 2))
    if out["max_m5"] < out["min_m5"] + 1:
        out["max_m5"] = round(out["min_m5"] + 1, 2)
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
    return clamp(gn)


def fitness(v: dict) -> float:
    c = v["closed"]
    if not c:
        return -999
    wr = sum(1 for t in c if t["win"]) / len(c)
    return sum(t["pnl_pct"] for t in c) / len(c) + 5 * wr


def learn_targets(S: dict, gn: dict) -> dict:
    """What did winning trades look like across every variant? Suggested thresholds for filters that are ON."""
    trades = [t for v in S["variants"].values() for t in v["closed"] if t.get("feat")]
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
    if random.random() < 0.3:  # explore a bigger target; stop-loss stays, hold scales with it (clamped to 2-15)
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


def new_variant(S, genome, gen, parent, notes):
    vid = f"v{S['next_id']}"
    S["next_id"] += 1
    v = {"id": vid, "gen": gen, "parent": parent, "genome": genome, "notes": notes, "status": "testing",
         "reason": "", "paper_usd": START_USD, "start_usd": START_USD, "positions": {}, "closed": [],
         "cooldown": {}, "created": time.time(), "val_start": 0}
    S["variants"][vid] = v
    return v


def spawn(S) -> tuple[dict, str]:
    seeds_used = S["next_id"] - 1
    if seeds_used < len(SEEDS):
        v = new_variant(S, clamp(SEEDS[seeds_used]), 0, None, ["seed"])
        return v, f"{v['id']} (seed)"
    pool = [x for x in S["variants"].values() if len(x["closed"]) >= 5]
    if pool and random.random() > 0.25:
        parent = max(pool, key=fitness)
        gn, notes = mutate(parent, S)
        v = new_variant(S, gn, parent["gen"] + 1, parent["id"], notes)
        return v, f"{v['id']} gen{v['gen']} from {parent['id']}: " + "; ".join(notes)
    v = new_variant(S, random_genome(), 0, None, ["random exploration"])
    return v, f"{v['id']} (random exploration)"


def active(S):
    return [v for v in S["variants"].values() if v["status"] in ("testing", "validating")]


def ensure_population(S, events):
    while len(active(S)) < POP:
        v, desc = spawn(S)
        events.append(f"SPAWNED {desc}\n  genome: {v['genome']}")


# ---------- judging ----------
def judge(v: dict):
    if v["status"] not in ("testing", "validating"):
        return None
    validating = v["status"] == "validating"
    w = v["closed"][v["val_start"]:] if validating else v["closed"]
    n, wins = len(w), sum(1 for t in w if t["win"])
    pnl = sum(t["pnl_usd"] for t in w)
    total = sum(t["pnl_usd"] for t in v["closed"])
    if total < -v["start_usd"] * MAX_DD_PCT / 100:
        return ("retire", f"drawdown {total:+.2f} USD")
    if n >= EVAL_AFTER:
        wr = wins / n
        tag = " in validation (likely overfit)" if validating else ""
        if wr < TARGET_WR:
            return ("retire", f"win rate {wr:.0%} after {n} trades{tag}")
        if pnl < 0:
            return ("retire", f"win rate {wr:.0%} but net {pnl:+.2f} USD after {n} trades{tag}")
    if not validating and n >= GRAD_TRADES:
        return ("graduate", "")
    if validating and n >= VAL_TRADES:
        return ("validated", "")
    return None


def apply_judgement(S, v, events):
    res = judge(v)
    if not res:
        return
    kind, why = res
    n, wins = len(v["closed"]), sum(1 for t in v["closed"] if t["win"])
    pnl = sum(t["pnl_usd"] for t in v["closed"])
    if kind == "retire":
        v["status"], v["reason"] = "retired", why
        events.append(f"RETIRED {v['id']} gen{v['gen']}: {why}")
        ensure_population(S, events)
    elif kind == "graduate":
        v["status"], v["val_start"] = "validating", len(v["closed"])
        events.append(f"GRADUATED {v['id']}: {wins}/{n} wins, net {pnl:+.2f} USD. Now needs {VAL_TRADES} fresh "
                      f"trades to validate (guards against luck).")
    elif kind == "validated":
        v["status"] = "validated"
        events.append(f"VALIDATED CANDIDATE {v['id']} gen{v['gen']}: {wins}/{n} wins ({wins/n:.0%}), net {pnl:+.2f} USD, "
                      f"paper only.\nGenome: {v['genome']}\nPaper fills are optimistic; do not assume live results match.")


# ---------- market ----------
async def get_pair(g, mint, pc, ttl):
    hit = pc.get(mint)
    if hit and time.time() - hit[0] < ttl:
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
    """Can we reason that this is NOT a rug? Every check must pass. Missing data = can't reason = no trade."""
    if a.get("danger"):
        return False, "RugCheck danger-level risk"
    h, t = a.get("holders"), a.get("top10")
    if h is None or t is None:
        return False, "holder data unavailable"
    if h < HARD_MIN_HOLDERS:
        return False, f"only {h} holders"
    if t >= HARD_MAX_TOP10:
        return False, f"top 10 hold {t}%"
    liq = (p.get("liquidity") or {}).get("usd", 0) or 0
    if liq < HARD_MIN_LIQ:
        return False, f"liquidity ${liq:,.0f}"
    tx = (p.get("txns") or {}).get("h1") or {}
    if tx.get("buys", 0) >= 8 and tx.get("sells", 0) == 0:
        return False, "buys but no sells (honeypot pattern)"
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
    if age_min < MIN_HOLD:
        return None
    if gross >= pos["tp"]:
        return "take-profit"
    if pos.get("armed") and gross <= max(floor, trail_gross):
        return "trailing-stop"
    cap = HARD_HOLD if in_profit else min(pos["hold"], HARD_HOLD)
    if age_min >= cap:
        return "max hold time"
    return None


# ---------- jobs ----------
async def monitor(g, S, pc, events, notes_out):
    for v in list(S["variants"].values()):
        if v["status"] == "retired":
            continue
        for mint, pos in list(v["positions"].items()):
            try:
                p = await get_pair(g, mint, pc, 8)
                if not p or not p.get("priceUsd"):
                    continue
                price = float(p["priceUsd"])
                liq = float((p.get("liquidity") or {}).get("usd", 0))
                now = time.time()
                gross = (price / pos["entry_price"] - 1) * 100
                why = decide_exit(pos, price, liq, now)

                if not why:
                    if now - pos.get("last_patch", 0) >= 60:  # keep Notion calls light
                        pos["last_patch"] = now
                        val = pos["size_tokens"] * price
                        await auto.n_patch(g, pos.get("page_id"), {"properties": {
                            "Current Price (USD)": auto._num(price), "Current Value (USD)": auto._num(val),
                            "Unrealised PnL (USD)": auto._num(val - pos["cost_usd"]),
                            "Unrealised PnL %": auto._num(gross / 100)}})
                    continue

                slip = RUG_EXIT_SLIP if why in ("liquidity collapse", "crash exit") else 0.0
                net = gross - COST_PCT - slip
                pnl_usd = pos["cost_usd"] * net / 100
                v["paper_usd"] += pnl_usd
                v["closed"].append({"mint": mint, "pnl_pct": net, "pnl_usd": pnl_usd, "win": net > 0,
                                    "why": why, "ts": now, "feat": pos.get("feat")})
                del v["positions"][mint]
                cum = sum(t["pnl_usd"] for t in v["closed"])
                result = "Win" if net > 0 else "Loss" if net < 0 else "Breakeven"
                await auto.n_patch(g, pos.get("page_id"), {"archived": True})
                await auto.n_create(g, auto.DB_CLOSED, {
                    "Token": auto._title(pos["name"]), "Mint": auto._text(mint),
                    "Entry Time": auto._date(pos["ts"]), "Exit Time": auto._date(now),
                    "Entry Price (USD)": auto._num(pos["entry_price"]), "Exit Price (USD)": auto._num(price),
                    "Size (Tokens)": auto._num(pos["size_tokens"]), "Hold Time (hrs)": auto._num((now - pos["ts"]) / 3600),
                    "Realised PnL (USD)": auto._num(pnl_usd), "PnL %": auto._num(net / 100),
                    "Result": auto._sel(result), "Exit Reason": auto._sel(why),
                    "Score": auto._num(pos.get("score")), "Holders": auto._num(pos.get("holders")),
                    "Top 10 %": auto._num(pos.get("top10")), "Mode": auto._sel("Paper"),
                    "Notes": auto._text(f"Variant {v['id']} gen{v['gen']} | peak "
                                        f"{(pos['peak_price'] / pos['entry_price'] - 1) * 100:+.0f}% | trail {pos['trail']}%")})
                await auto.n_create(g, auto.DB_EVENTS, {
                    "Event": auto._title(f"Exit {pos['name']} [{v['id']}]"), "Type": auto._sel("Exit"),
                    "Token": auto._text(pos["name"]), "Time": auto._date(now), "Price (USD)": auto._num(price),
                    "Realised PnL (USD)": auto._num(pnl_usd), "Cumulative PnL (USD)": auto._num(cum),
                    "Mode": auto._sel("Paper")})
                if NOTIFY_TRADES or why in ("liquidity collapse", "crash exit"):
                    events.append(f"[{v['id']}] closed {pos['name']} ({why}) {net:+.1f}% = {pnl_usd:+.2f} USD")
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
        if not v["closed"] and now - v["created"] > STALE_HOURS * 3600 and v["status"] == "testing":
            v["status"], v["reason"] = "retired", f"no trades in {STALE_HOURS}h (entry rules too tight)"
            events.append(f"RETIRED {v['id']} gen{v['gen']}: {v['reason']}")
            free.remove(v)
            ensure_population(S, events)
    random.shuffle(free)

    cands = list(dict.fromkeys((await auto.new_launches(g)) + (await auto.candidates(g))))[:CANDIDATE_LIMIT]
    analyses = 0
    for mint in cands:
        if not free:
            break
        p = await get_pair(g, mint, pc, 60)
        if not p or not p.get("priceUsd"):
            continue
        age = pair_age_min(p, now)
        wanting = [v for v in free if now - v["cooldown"].get(mint, 0) > COOLDOWN_MIN * 60
                   and entry_ok(p, v["genome"], age)]
        if not wanting:
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
            ts_cache = now
            if a["holders"] is None or a["top10"] is None:
                ts_cache = now - (ACACHE_TTL_MIN * 60 - 120)  # brand-new token, data not ready: retry in ~2 min
            S["acache"][mint] = {"ts": ts_cache, "a": a}

        ok, why = rug_ok(a, p)
        if not ok:
            log.info("skip %s: %s", mint, why)
            continue

        price = float(p["priceUsd"])
        liq = float((p.get("liquidity") or {}).get("usd", 0))
        slip = LAUNCH_SLIP if (age is not None and age < 15) else 0.0
        fill = price * (1 + slip / 100)
        for v in wanting:
            gn = v["genome"]
            if a["holders"] < gn["min_holders"] or a["top10"] > gn["max_top10"] or (a["score"] or 0) < gn["min_score"]:
                continue
            size_usd = v["paper_usd"] * gn["size_frac"]
            exp_net = size_usd * (gn["tp"] - COST_PCT) / 100
            target = max(TARGET_USD, float(gn.get("target_usd", TARGET_USD)))
            if exp_net < target * 0.9:
                continue  # position too small to net the variant's dollar target at its TP
            name = auto.token_name(a, mint)
            size_tokens = size_usd / fill
            ts = time.time()
            page_id = await auto.n_create(g, auto.DB_OPEN, {
                "Token": auto._title(name), "Mint": auto._text(mint), "Entry Time": auto._date(ts),
                "Entry Price (USD)": auto._num(fill), "Current Price (USD)": auto._num(price),
                "Size (Tokens)": auto._num(size_tokens), "Cost Basis (USD)": auto._num(size_usd),
                "Current Value (USD)": auto._num(size_usd), "Unrealised PnL (USD)": auto._num(0),
                "Unrealised PnL %": auto._num(0), "Score": auto._num(a["score"]),
                "Holders": auto._num(a["holders"]), "Top 10 %": auto._num(a["top10"]),
                "Mode": auto._sel("Paper"),
                "Notes": auto._text(f"Variant {v['id']} gen{v['gen']} | age "
                                    f"{'?' if age is None else f'{age:.0f}m'} | TP {gn['tp']}% SL {gn['sl']}% "
                                    f"trail {gn['trail']}% hold {gn['hold']}m")})
            await auto.n_create(g, auto.DB_EVENTS, {
                "Event": auto._title(f"Entry {name} [{v['id']}]"), "Type": auto._sel("Entry"),
                "Token": auto._text(name), "Time": auto._date(ts), "Price (USD)": auto._num(fill),
                "Mode": auto._sel("Paper")})
            v["positions"][mint] = {"name": name, "entry_price": fill, "entry_liq": liq, "ts": ts,
                                    "cost_usd": size_usd, "size_tokens": size_tokens, "score": a["score"],
                                    "holders": a["holders"], "top10": a["top10"], "tp": gn["tp"], "sl": gn["sl"],
                                    "hold": gn["hold"], "trail": gn["trail"], "page_id": page_id,
                                    "feat": features(p, a, age), "peak_price": fill, "last_price": price}
            v["cooldown"][mint] = ts
            free.remove(v)
            if NOTIFY_TRADES:
                events.append(f"[{v['id']}] opened {name} ~${size_usd:.2f}")


# ---------- reporting ----------
def leaderboard(S) -> str:
    rows = []
    for v in sorted(S["variants"].values(), key=lambda x: (x["status"] == "retired", -fitness(x))):
        if v["status"] == "retired":
            continue
        c = v["closed"]
        n, w = len(c), sum(1 for t in c if t["win"])
        wr = f"{w/n:.0%}" if n else "-"
        rows.append(f"{v['id']} g{v['gen']} {v['status']}: {w}/{n} ({wr}), {sum(t['pnl_usd'] for t in c):+.2f} USD"
                    f"{', OPEN' if v['positions'] else ''}")
    retired = sum(1 for v in S["variants"].values() if v["status"] == "retired")
    validated = [v["id"] for v in S["variants"].values() if v["status"] == "validated"]
    return ("EVO " + ("HALTED" if S["halted"] else "running") + " (paper)\n" + "\n".join(rows or ["no active variants"])
            + f"\nRetired so far: {retired}\nValidated candidates: {', '.join(validated) or 'none yet'}")


async def flush(tb, g, events):
    for e in events:
        try:
            await tb.send_message(g["OWNER_ID"], e[:3500])
        except Exception as ex:
            log.warning("notify failed: %s", ex)
    events.clear()


async def run(tb, g, loop_seconds: int):
    import asyncio
    import menu  # lazy import: menu.py imports this module
    S = load()
    events: list[str] = []
    ensure_population(S, events)
    pc: dict = {}
    start = time.time()
    last_scan = last_cmd = last_mon = 0.0
    while True:
        now = time.time()
        try:
            if now - last_cmd >= 10:
                await menu.handle(tb, g, S); last_cmd = now
            if now - last_mon >= 10:
                await monitor(g, S, pc, events, None); last_mon = now
            if now - last_scan >= 20:
                await scan(g, S, pc, events); last_scan = now
            save(S)
            await flush(tb, g, events)
        except Exception as e:
            log.exception("cycle error: %s", e)
        if time.time() - start + 10 >= loop_seconds:
            break
        await asyncio.sleep(10)
    save(S)
