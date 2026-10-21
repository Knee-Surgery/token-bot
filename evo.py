"""
Evolutionary paper-trading engine.

Runs POP variants side by side on the same market data (paper only, USD bankroll each).
  - A variant is RETIRED if, after EVO_EVAL_AFTER trades, win rate < target or net PnL < 0 (or drawdown, or no trades).
  - Every retirement spawns a child: mutated from the best-performing variant so far, nudged by what the
    parent's losses looked like (stop-loss / timeout / liquidity-collapse), plus random jitter.
    1 in 4 children is fully random to keep exploring.
  - A variant that survives EVO_GRAD_TRADES trades is GRADUATED, then must pass a fresh EVO_VAL_TRADES-trade
    VALIDATION window it was never tuned on. Only then is it VALIDATED (a candidate, never auto-live).

Hard rules that mutation can never cross: holders >= 1000, top-10 <= 20%, TP >= 5% (cost-adjusted $0.20 target),
SL <= 1.5x TP (so the win rate can't be inflated with huge stops), per-trade net target >= $0.20.
Paper only. Paper fills are optimistic: live results will be worse.
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
COST_PCT = float(os.getenv("EVO_COST_PCT", "2.5"))        # pessimistic paper-fill cost, round trip
TARGET_USD = float(os.getenv("SCALP_TARGET_USD", "0.20"))  # min net profit per winning trade
BANKROLL_FRAC = float(os.getenv("EVO_BANKROLL_FRAC", "0.85"))
STALE_HOURS = int(os.getenv("EVO_STALE_HOURS", "48"))
NOTIFY_TRADES = os.getenv("EVO_NOTIFY_TRADES", "false").lower() == "true"
ANALYZE_LIMIT = int(os.getenv("EVO_ANALYZE_LIMIT", "6"))   # fresh analyses per scan
CANDIDATE_LIMIT = int(os.getenv("EVO_CANDIDATE_LIMIT", "30"))
COOLDOWN_MIN = 30
ACACHE_TTL_MIN = 30

BOUNDS = {"tp": (5, 15), "sl": (1.5, 8), "hold": (3, 60), "min_score": (55, 90), "min_m5": (0, 3),
          "max_m5": (3, 15), "min_txns": (10, 200), "min_vol_h1": (20000, 500000),
          "min_liq": (20000, 200000), "min_holders": (1000, 10000), "max_top10": (10, 20)}
INTS = {"hold", "min_score", "min_txns", "min_holders"}
LOG_KEYS = {"min_vol_h1", "min_liq", "min_txns", "min_holders"}

SEEDS = [
    {"tp": 5, "sl": 3, "hold": 8, "min_score": 65, "min_m5": 0.5, "max_m5": 6, "min_txns": 20,
     "min_vol_h1": 50000, "min_liq": 20000, "min_holders": 1000, "max_top10": 20},
    {"tp": 6, "sl": 3, "hold": 10, "min_score": 70, "min_m5": 0.5, "max_m5": 5, "min_txns": 40,
     "min_vol_h1": 100000, "min_liq": 40000, "min_holders": 1500, "max_top10": 18},
    {"tp": 5, "sl": 2.5, "hold": 6, "min_score": 70, "min_m5": 1, "max_m5": 6, "min_txns": 30,
     "min_vol_h1": 80000, "min_liq": 30000, "min_holders": 1000, "max_top10": 20},
]


# ---------- state ----------
def load() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"variants": {}, "next_id": 1, "halted": False, "acache": {}, "tg_offset": None}


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
        x = min(hi, max(lo, float(gn[k])))
        out[k] = int(round(x)) if k in INTS else round(x, 2)
    out["sl"] = min(out["sl"], round(1.5 * out["tp"], 2))
    if out["max_m5"] < out["min_m5"] + 1:
        out["max_m5"] = round(out["min_m5"] + 1, 2)
    return out


def random_genome() -> dict:
    gn = {}
    for k, (lo, hi) in BOUNDS.items():
        gn[k] = math.exp(random.uniform(math.log(max(lo, 1)), math.log(hi))) if k in LOG_KEYS else random.uniform(lo, hi)
    return clamp(gn)


def fitness(v: dict) -> float:
    c = v["closed"]
    if not c:
        return -999
    wr = sum(1 for t in c if t["win"]) / len(c)
    return sum(t["pnl_pct"] for t in c) / len(c) + 5 * wr


def mutate(parent: dict):
    gn, notes = dict(parent["genome"]), []
    losses = [t for t in parent["closed"] if not t["win"]]
    if losses:
        top, _ = Counter(t["why"] for t in losses).most_common(1)[0]
        if top == "stop-loss":
            gn["min_score"] += 5; gn["max_m5"] -= 1; gn["min_vol_h1"] *= 1.25
            notes.append("losses were stop-outs: stricter entry (score+5, lower max 5m move, volume x1.25)")
        elif top == "max hold time":
            gn["hold"] *= 0.8; gn["tp"] -= 0.5
            notes.append("losses were timeouts: shorter hold, slightly lower TP")
        elif top == "liquidity collapse":
            gn["min_liq"] *= 1.5
            notes.append("losses were liquidity collapses: min liquidity x1.5")
    for k in random.sample(list(BOUNDS), random.randint(1, 3)):
        gn[k] = float(gn[k]) * random.uniform(0.8, 1.2)
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
        gn, notes = mutate(parent)
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


def entry_ok(p: dict, gn: dict) -> bool:
    ch = p.get("priceChange") or {}
    m5, h1 = ch.get("m5") or 0, ch.get("h1") or 0
    tx = (p.get("txns") or {}).get("m5") or {}
    buys, sells = tx.get("buys", 0), tx.get("sells", 0)
    vol = (p.get("volume") or {}).get("h1", 0) or 0
    liq = (p.get("liquidity") or {}).get("usd", 0) or 0
    return (gn["min_m5"] <= m5 <= gn["max_m5"] and h1 > 0 and buys + sells >= gn["min_txns"]
            and buys > sells and vol >= gn["min_vol_h1"] and liq >= gn["min_liq"])


# ---------- jobs ----------
async def monitor(g, S, pc, events, notes_out):
    for v in list(S["variants"].values()):
        if v["status"] == "retired":
            continue
        for mint, pos in list(v["positions"].items()):
            try:
                p = await get_pair(g, mint, pc, 15)
                if not p:
                    continue
                price = float(p["priceUsd"])
                liq = float((p.get("liquidity") or {}).get("usd", 0))
                gross = (price / pos["entry_price"] - 1) * 100
                age_min = (time.time() - pos["ts"]) / 60
                why = None
                if gross >= pos["tp"]: why = "take-profit"
                elif gross <= -pos["sl"]: why = "stop-loss"
                elif age_min >= pos["hold"]: why = "max hold time"
                elif liq < pos["entry_liq"] * 0.5: why = "liquidity collapse"
                if not why:
                    val = pos["size_tokens"] * price
                    await auto.n_patch(g, pos.get("page_id"), {"properties": {
                        "Current Price (USD)": auto._num(price), "Current Value (USD)": auto._num(val),
                        "Unrealised PnL (USD)": auto._num(val - pos["cost_usd"]),
                        "Unrealised PnL %": auto._num(gross / 100)}})
                    continue

                net = gross - COST_PCT
                pnl_usd = pos["cost_usd"] * net / 100
                now = time.time()
                v["paper_usd"] += pnl_usd
                v["closed"].append({"mint": mint, "pnl_pct": net, "pnl_usd": pnl_usd, "win": net > 0,
                                    "why": why, "ts": now})
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
                    "Notes": auto._text(f"Variant {v['id']} gen{v['gen']}")})
                await auto.n_create(g, auto.DB_EVENTS, {
                    "Event": auto._title(f"Exit {pos['name']} [{v['id']}]"), "Type": auto._sel("Exit"),
                    "Token": auto._text(pos["name"]), "Time": auto._date(now), "Price (USD)": auto._num(price),
                    "Realised PnL (USD)": auto._num(pnl_usd), "Cumulative PnL (USD)": auto._num(cum),
                    "Mode": auto._sel("Paper")})
                if NOTIFY_TRADES:
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
    # retire variants that never trade
    for v in list(free):
        if not v["closed"] and now - v["created"] > STALE_HOURS * 3600 and v["status"] in ("testing",):
            v["status"], v["reason"] = "retired", f"no trades in {STALE_HOURS}h (entry rules too tight or bankroll too small)"
            events.append(f"RETIRED {v['id']} gen{v['gen']}: {v['reason']}")
            free.remove(v)
            ensure_population(S, events)
    random.shuffle(free)

    analyses = 0
    for mint in (await auto.candidates(g))[:CANDIDATE_LIMIT]:
        if not free:
            break
        p = await get_pair(g, mint, pc, 60)
        if not p:
            continue
        wanting = [v for v in free if now - v["cooldown"].get(mint, 0) > COOLDOWN_MIN * 60 and entry_ok(p, v["genome"])]
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
            S["acache"][mint] = {"ts": now, "a": a}
        if a.get("holders") is None or a.get("top10") is None:
            continue

        price = float(p["priceUsd"])
        liq = float((p.get("liquidity") or {}).get("usd", 0))
        for v in wanting:
            gn = v["genome"]
            if a["score"] < gn["min_score"] or a["holders"] < gn["min_holders"] or a["top10"] > gn["max_top10"]:
                continue
            need_usd = TARGET_USD / ((gn["tp"] - COST_PCT) / 100)
            size_usd = min(need_usd, v["paper_usd"] * BANKROLL_FRAC)
            exp_net = size_usd * (gn["tp"] - COST_PCT) / 100
            if exp_net < TARGET_USD * 0.9:
                continue  # bankroll can't reach the $0.20 minimum at this TP
            name = auto.token_name(a, mint)
            size_tokens = size_usd / price
            ts = time.time()
            page_id = await auto.n_create(g, auto.DB_OPEN, {
                "Token": auto._title(name), "Mint": auto._text(mint), "Entry Time": auto._date(ts),
                "Entry Price (USD)": auto._num(price), "Current Price (USD)": auto._num(price),
                "Size (Tokens)": auto._num(size_tokens), "Cost Basis (USD)": auto._num(size_usd),
                "Current Value (USD)": auto._num(size_usd), "Unrealised PnL (USD)": auto._num(0),
                "Unrealised PnL %": auto._num(0), "Score": auto._num(a["score"]),
                "Holders": auto._num(a["holders"]), "Top 10 %": auto._num(a["top10"]),
                "Mode": auto._sel("Paper"),
                "Notes": auto._text(f"Variant {v['id']} gen{v['gen']} | TP {gn['tp']}% SL {gn['sl']}% hold {gn['hold']}m | target net ${exp_net:.2f}")})
            await auto.n_create(g, auto.DB_EVENTS, {
                "Event": auto._title(f"Entry {name} [{v['id']}]"), "Type": auto._sel("Entry"),
                "Token": auto._text(name), "Time": auto._date(ts), "Price (USD)": auto._num(price),
                "Mode": auto._sel("Paper")})
            v["positions"][mint] = {"name": name, "entry_price": price, "entry_liq": liq, "ts": ts,
                                    "cost_usd": size_usd, "size_tokens": size_tokens, "score": a["score"],
                                    "holders": a["holders"], "top10": a["top10"], "tp": gn["tp"],
                                    "sl": gn["sl"], "hold": gn["hold"], "page_id": page_id}
            v["cooldown"][mint] = ts
            free.remove(v)
            if NOTIFY_TRADES:
                events.append(f"[{v['id']}] opened {name} ~${size_usd:.2f}, target net ${exp_net:.2f}")


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
    return ("EVO " + ("HALTED" if S["halted"] else "running") + f" (paper)\n" + "\n".join(rows or ["no active variants"])
            + f"\nRetired so far: {retired}\nValidated candidates: {', '.join(validated) or 'none yet'}")


async def handle_commands(tb, g, S):
    try:
        updates = await tb.get_updates(offset=S.get("tg_offset"), timeout=0, allowed_updates=["message"])
    except Exception as e:
        log.warning("getUpdates failed: %s", e)
        return
    for u in updates:
        S["tg_offset"] = u.update_id + 1
        m = u.message
        if not m or not m.from_user or m.from_user.id != g["OWNER_ID"] or not m.text:
            continue
        parts = m.text.split()
        cmd = parts[0].split("@")[0].lower()
        try:
            if cmd == "/halt":
                S["halted"] = True
                await tb.send_message(g["OWNER_ID"], "Halted: no new entries. Open positions still get closed.")
            elif cmd in ("/evo", "/autostatus"):
                await tb.send_message(g["OWNER_ID"], leaderboard(S))
            elif cmd == "/analyze" and len(parts) > 1:
                await tb.send_message(g["OWNER_ID"], g["fmt_analysis"](await g["analyze"](parts[1])))
            elif cmd in ("/start", "/help"):
                await tb.send_message(g["OWNER_ID"], "Commands: /evo (leaderboard), /halt, /analyze <mint>")
        except Exception as e:
            log.warning("command failed: %s", e)


async def flush(tb, g, events):
    for e in events:
        try:
            await tb.send_message(g["OWNER_ID"], e[:3500])
        except Exception as ex:
            log.warning("notify failed: %s", ex)
    events.clear()


async def run(tb, g, loop_seconds: int):
    S = load()
    events: list[str] = []
    ensure_population(S, events)
    pc: dict = {}
    start = time.time()
    last_scan = last_cmd = 0.0
    while True:
        now = time.time()
        try:
            if now - last_cmd >= 60:
                await handle_commands(tb, g, S); last_cmd = now
            await monitor(g, S, pc, events, None)
            if now - last_scan >= 60:
                await scan(g, S, pc, events); last_scan = now
            save(S)
            await flush(tb, g, events)
        except Exception as e:
            log.exception("cycle error: %s", e)
        if time.time() - start + 20 >= loop_seconds:
            break
        import asyncio
        await asyncio.sleep(20)
    save(S)
