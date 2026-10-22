"""
Autonomous mode with two strategies, journalled to Notion, with an auto-HALT kill switch.

  swing : slower trades (TP/SL/hold from AUTO_* vars)
  scalp : trending / high-volume tokens, very short holds, small targets. Position is sized so a
          take-profit nets >= SCALP_TARGET_USD after estimated costs (skips the trade if it can't).

Candidate sources: GeckoTerminal trending pools + top-volume pools, DexScreener boosts + new profiles.
Hard gates for every entry: liquidity, >= MIN_HOLDERS holders, top-10 <= MAX_TOP10_PCT, score threshold.

Halts itself (no new entries) if, after AUTO_EVAL_AFTER closed trades, win rate < AUTO_TARGET_WINRATE
or net PnL < 0, or if drawdown exceeds AUTO_MAX_DRAWDOWN_PCT. No auto-resume: delete auto_state.json.
"""
import json, os, time, logging, urllib.parse
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("auto")
STATE = Path("auto_state.json")

# ---- swing ----
TP = float(os.getenv("AUTO_TP_PCT", "12"))
SL = float(os.getenv("AUTO_SL_PCT", "8"))
MAX_HOLD_MIN = int(os.getenv("AUTO_MAX_HOLD_MIN", "120"))
MIN_SCORE = int(os.getenv("AUTO_MIN_SCORE", "75"))
MAX_OPEN = int(os.getenv("AUTO_MAX_OPEN", "2"))
COST_PCT = float(os.getenv("AUTO_COST_PCT", "1.5"))

# ---- scalp ----
SCALP_ON = os.getenv("AUTO_SCALP", "true").lower() == "true"
SCALP_TARGET_USD = float(os.getenv("SCALP_TARGET_USD", "0.20"))   # min net profit per winning trade
SCALP_TP = float(os.getenv("SCALP_TP_PCT", "5"))
SCALP_SL = float(os.getenv("SCALP_SL_PCT", "3"))
SCALP_COST = float(os.getenv("SCALP_COST_PCT", "2"))               # est. fees + slippage, round trip
SCALP_HOLD = int(os.getenv("SCALP_MAX_HOLD_MIN", "8"))
SCALP_MAX_OPEN = int(os.getenv("SCALP_MAX_OPEN", "1"))
SCALP_MIN_SCORE = int(os.getenv("SCALP_MIN_SCORE", "65"))
SCALP_BANKROLL_FRAC = float(os.getenv("SCALP_MAX_BANKROLL_FRAC", "0.85"))
SCALP_MIN_M5 = float(os.getenv("SCALP_MIN_M5_PCT", "0.5"))        # 5m price change window: rising, not parabolic
SCALP_MAX_M5 = float(os.getenv("SCALP_MAX_M5_PCT", "6"))
SCALP_MIN_TXNS_M5 = int(os.getenv("SCALP_MIN_TXNS_M5", "20"))
SCALP_MIN_VOL_H1 = float(os.getenv("SCALP_MIN_VOL_H1", "50000"))

# ---- shared ----
TARGET_WR = float(os.getenv("AUTO_TARGET_WINRATE", "0.9"))
EVAL_AFTER = int(os.getenv("AUTO_EVAL_AFTER", "10"))
MAX_DD_PCT = float(os.getenv("AUTO_MAX_DRAWDOWN_PCT", "30"))
PAPER_START_SOL = float(os.getenv("AUTO_PAPER_START_SOL", "0.05"))
SEEN_TTL_MIN = int(os.getenv("SEEN_TTL_MIN", "120"))
SCAN_LIMIT = int(os.getenv("SCAN_CHECK_LIMIT", "6"))
FEE_RESERVE_SOL = 0.01
SOL = "So11111111111111111111111111111111111111112"
SKIP_MINTS = {SOL, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}

# ---- Notion journal (optional) ----
NOTION_TOKEN = (os.getenv("NOTION_TOKEN") or "").strip() or None
DB_OPEN = os.getenv("NOTION_DB_OPEN", "32b8584b-2398-4b20-b92c-18a2dc1b05af")
DB_CLOSED = os.getenv("NOTION_DB_CLOSED", "255f98ab-f760-447a-a453-8550f416baff")
DB_EVENTS = os.getenv("NOTION_DB_EVENTS", "802d475b-a09c-4022-9a25-c609e9325cae")


# ---------- state ----------
def load() -> dict:
    if STATE.exists():
        s = json.loads(STATE.read_text())
    else:
        s = {"halted": False, "reason": "", "positions": {}, "closed": [], "seen": {},
             "paper_sol": PAPER_START_SOL, "start_sol": None}
    if isinstance(s.get("seen"), list):  # migrate old format
        s["seen"] = {m: 0 for m in s["seen"]}
    return s


def save(s: dict):
    if len(s["seen"]) > 800:  # keep the newest entries
        s["seen"] = dict(sorted(s["seen"].items(), key=lambda kv: kv[1])[-500:])
    STATE.write_text(json.dumps(s, indent=1))


# ---------- notion ----------
def _h():
    return {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28",
            "Content-Type": "application/json"}

def _iso(ts): return datetime.fromtimestamp(ts, timezone.utc).isoformat()
def _title(x): return {"title": [{"text": {"content": str(x)[:200]}}]}
def _text(x): return {"rich_text": [{"text": {"content": str(x)[:1900]}}]}
def _num(x): return {"number": None if x is None else float(x)}
def _date(ts): return {"date": {"start": _iso(ts)}}
def _sel(x): return {"select": {"name": x}}
def _url(x): return {"url": x or None}


async def n_create(g, db, props):
    if not (NOTION_TOKEN and db):
        return None
    try:
        r = await g["http"].post("https://api.notion.com/v1/pages", headers=_h(),
                                 json={"parent": {"database_id": db}, "properties": props})
        r.raise_for_status()
        return r.json()["id"]
    except Exception as e:
        log.warning("notion create failed: %s", e)
        return None


async def n_patch(g, page_id, body):
    if not (NOTION_TOKEN and page_id):
        return
    try:
        r = await g["http"].patch(f"https://api.notion.com/v1/pages/{page_id}", headers=_h(), json=body)
        r.raise_for_status()
    except Exception as e:
        log.warning("notion patch failed: %s", e)


def token_name(a: dict, mint: str) -> str:
    sym = (a.get("symbol") or "").strip()
    return sym.upper() if sym.isalnum() and len(sym) <= 12 else f"Token{mint[:2]}{mint[-2:]}"


def solscan(sig): return f"https://solscan.io/tx/{sig}" if sig else None


# ---------- market data ----------
async def best_pair(g, mint):
    r = await g["http"].get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
    ps = [p for p in (r.json().get("pairs") or []) if p.get("chainId") == "solana"]
    return max(ps, key=lambda x: (x.get("liquidity") or {}).get("usd", 0)) if ps else None


async def px(g, mint):
    p = await best_pair(g, mint)
    if not p:
        return None, 0.0
    return float(p["priceUsd"]), float((p.get("liquidity") or {}).get("usd", 0))


async def candidates(g) -> list[str]:
    """Trending + high-volume first, then boosted and brand-new profiles. Deduped, order kept."""
    out: list[str] = []
    gt_headers = {"Accept": "application/json;version=20230302"}
    for path in ("trending_pools", "pools?sort=h24_volume_usd_desc"):
        try:
            r = await g["http"].get(f"https://api.geckoterminal.com/api/v2/networks/solana/{path}", headers=gt_headers)
            for d in r.json().get("data", []):
                tid = d["relationships"]["base_token"]["data"]["id"]  # "solana_<mint>"
                out.append(tid.split("_", 1)[1])
        except Exception as e:
            log.warning("geckoterminal %s failed: %s", path, e)
    for url in ("https://api.dexscreener.com/token-boosts/top/v1",
                "https://api.dexscreener.com/token-boosts/latest/v1",
                "https://api.dexscreener.com/token-profiles/latest/v1"):
        try:
            r = await g["http"].get(url)
            out += [x["tokenAddress"] for x in r.json() if x.get("chainId") == "solana"]
        except Exception as e:
            log.warning("%s failed: %s", url, e)
    seen, uniq = set(), []
    for m in out:
        if m not in seen and m not in SKIP_MINTS:
            seen.add(m); uniq.append(m)
    return uniq


# ---------- wide discovery: new pools first, then everything else ----------
GT_BASE = "https://api.geckoterminal.com/api/v2/networks/solana"
GT_HEADERS = {"Accept": "application/json;version=20230302"}
DS = "https://api.dexscreener.com"
# keyword searches rotate through these; DexScreener matches name, symbol and address (pump.fun mints end in "pump")
SEARCH_TERMS = ["pump", "sol", "meme", "ai", "cat", "dog", "pepe", "trump", "frog", "moon", "baby", "inu",
                "agent", "gold", "bonk", "wif", "coin", "token", "elon", "dao", "game", "fart", "king", "bull"]
_cool: dict[str, float] = {}
_tick = {"n": 0}


async def _get_json(g, key, url, headers=None, cool=90):
    """GET with a per-source cooldown after a 429, so one rate-limited source never blocks the others."""
    if time.time() < _cool.get(key, 0):
        return None
    try:
        r = await g["http"].get(url, headers=headers)
        if r.status_code == 429:
            _cool[key] = time.time() + cool
            log.warning("%s rate limited, pausing it for %ss", key, cool)
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("%s failed: %s", key, e)
        return None


def _gt_mints(js) -> list[str]:
    out = []
    for d in (js or {}).get("data", []):
        try:
            out.append(d["relationships"]["base_token"]["data"]["id"].split("_", 1)[1])  # "solana_<mint>"
        except Exception:
            continue
    return out


def _ds_mints(js) -> list[str]:
    return [x["tokenAddress"] for x in (js if isinstance(js, list) else [])
            if x.get("chainId") == "solana" and x.get("tokenAddress")]


def _pick_best(pairs: list) -> dict:
    """Highest-liquidity Solana pair for each base token."""
    best: dict = {}
    for p in pairs or []:
        if p.get("chainId") != "solana":
            continue
        m = (p.get("baseToken") or {}).get("address")
        if not m:
            continue
        liq = (p.get("liquidity") or {}).get("usd", 0) or 0
        if m not in best or liq > ((best[m].get("liquidity") or {}).get("usd", 0) or 0):
            best[m] = p
    return best


async def discover(g) -> list[str]:
    """Candidate mints, newest first. Several sources every scan, plus rotating ones, so coverage keeps widening."""
    n = _tick["n"]
    _tick["n"] += 1
    out: list[str] = []
    for page in (1, 2 + n % 2):  # newest pools: page 1 every scan, pages 2 and 3 alternating
        out += _gt_mints(await _get_json(g, f"gt_new_{page}", f"{GT_BASE}/new_pools?page={page}", GT_HEADERS))
    out += _ds_mints(await _get_json(g, "ds_profiles", f"{DS}/token-profiles/latest/v1"))
    out += _ds_mints(await _get_json(g, "ds_boosts", f"{DS}/token-boosts/latest/v1"))
    rot = n % 4  # one rotating source per scan keeps every API inside its rate limit
    if rot == 0:
        out += _gt_mints(await _get_json(g, "gt_trending", f"{GT_BASE}/trending_pools", GT_HEADERS))
    elif rot == 1:
        out += _gt_mints(await _get_json(g, "gt_volume", f"{GT_BASE}/pools?sort=h24_volume_usd_desc", GT_HEADERS))
    elif rot == 2:
        out += _ds_mints(await _get_json(g, "ds_boosts_top", f"{DS}/token-boosts/top/v1"))
    else:
        out += _ds_mints(await _get_json(g, "ds_takeovers", f"{DS}/community-takeovers/latest/v1"))
    term = SEARCH_TERMS[n % len(SEARCH_TERMS)]  # keyword search across everything DexScreener knows, newest first
    js = await _get_json(g, "ds_search", f"{DS}/latest/dex/search?q={term}")
    pairs = [p for p in (js or {}).get("pairs", []) if p.get("chainId") == "solana"]
    pairs.sort(key=lambda p: p.get("pairCreatedAt") or 0, reverse=True)
    out += [(p.get("baseToken") or {}).get("address") for p in pairs]
    seen, uniq = set(), []
    for m in out:
        if m and m not in seen and m not in SKIP_MINTS:
            seen.add(m)
            uniq.append(m)
    return uniq


async def best_pairs(g, mints: list[str]) -> dict:
    """Batch lookup, 30 mints per request: {mint: best Solana pair}. Missing mints are simply absent."""
    out: dict = {}
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        js = await _get_json(g, "ds_batch", f"{DS}/tokens/v1/solana/" + ",".join(chunk))
        pairs = js if isinstance(js, list) else ((js or {}).get("pairs") or [])
        want = set(chunk)
        out.update({m: p for m, p in _pick_best(pairs).items() if m in want})
    return out


async def search_coins(g, query: str, limit: int = 6) -> list[dict]:
    """Search any Solana coin by name, symbol or mint address (DexScreener). Highest liquidity first."""
    js = await _get_json(g, "ds_search_user", f"{DS}/latest/dex/search?q=" + urllib.parse.quote(query.strip()), cool=30)
    now = time.time()
    rows = []
    for m, p in _pick_best((js or {}).get("pairs") or []).items():
        created = p.get("pairCreatedAt")
        rows.append({"mint": m, "symbol": (p["baseToken"].get("symbol") or "?"), "name": (p["baseToken"].get("name") or ""),
                     "age_min": (now * 1000 - float(created)) / 60000 if created else None,
                     "liq": (p.get("liquidity") or {}).get("usd", 0) or 0,
                     "vol24": (p.get("volume") or {}).get("h24", 0) or 0,
                     "h24": (p.get("priceChange") or {}).get("h24")})
    rows.sort(key=lambda r: r["liq"], reverse=True)
    return rows[:limit]


def scalp_ok(p: dict) -> bool:
    """Momentum filter: rising but not parabolic over 5m, buyers ahead of sellers, real volume."""
    ch = p.get("priceChange") or {}
    m5, h1 = ch.get("m5") or 0, ch.get("h1") or 0
    tx = (p.get("txns") or {}).get("m5") or {}
    buys, sells = tx.get("buys", 0), tx.get("sells", 0)
    vol_h1 = (p.get("volume") or {}).get("h1", 0) or 0
    return (SCALP_MIN_M5 <= m5 <= SCALP_MAX_M5 and h1 > 0 and buys + sells >= SCALP_MIN_TXNS_M5
            and buys > sells and vol_h1 >= SCALP_MIN_VOL_H1)


async def notify(app, g, text):
    try:
        await app.bot.send_message(g["OWNER_ID"], text)
    except Exception as e:
        log.warning("notify failed: %s", e)


async def bankroll(g, s) -> float:
    if g["DRY_RUN"]:
        return s["paper_sol"]
    return (await g["rpc"]("getBalance", [str(g["wallet"].pubkey())]))["value"] / 1e9


def stats(s):
    c = s["closed"]
    n = len(c)
    wins = sum(1 for t in c if t["win"])
    pnl = sum(t["sol_in"] * t["pnl_pct"] / 100 for t in c)
    return n, wins, pnl


def breakdown(s) -> str:
    parts = []
    for st in ("swing", "scalp"):
        c = [t for t in s["closed"] if t.get("strat", "swing") == st]
        if c:
            w = sum(1 for t in c if t["win"])
            parts.append(f"{st}: {w}/{len(c)} wins, {sum(t.get('pnl_usd', 0) for t in c):+.2f} USD")
    return " | ".join(parts) or "no closed trades yet"


async def evaluate(app, g, s):
    n, wins, pnl = stats(s)
    start = s["start_sol"] or PAPER_START_SOL
    reason = ""
    if pnl < -start * MAX_DD_PCT / 100:
        reason = f"drawdown {pnl:.4f} SOL exceeds {MAX_DD_PCT:.0f}% of starting bankroll"
    elif n >= EVAL_AFTER:
        wr = wins / n
        if wr < TARGET_WR:
            reason = f"win rate {wr:.0%} below target {TARGET_WR:.0%} after {n} trades"
        elif pnl < 0:
            reason = f"win rate {wr:.0%} but net PnL negative ({pnl:.4f} SOL)"
    if reason and not s["halted"]:
        s["halted"], s["reason"] = True, reason
        save(s)
        await notify(app, g, f"BOT HALTED: {reason}\nNo new entries. Open positions will still be closed out.")


async def sell_all(g, mint):
    raw, _ = await g["token_balance"](mint)
    if raw <= 0:
        return None
    q = await g["quote"](mint, SOL, raw)
    return await g["execute_swap"](q)


# ---------- jobs ----------
async def monitor(app, g):
    s = load()
    mode = "Paper" if g["DRY_RUN"] else "Live"
    for mint, pos in list(s["positions"].items()):
        try:
            price, liq = await px(g, mint)
            if price is None:
                continue
            strat = pos.get("strat", "swing")
            tp, sl = pos.get("tp", TP), pos.get("sl", SL)
            hold, cost = pos.get("max_hold", MAX_HOLD_MIN), pos.get("cost_pct", COST_PCT)
            gross = (price / pos["entry_price"] - 1) * 100
            age_min = (time.time() - pos["ts"]) / 60
            why = None
            if gross >= tp: why = "take-profit"
            elif gross <= -sl: why = "stop-loss"
            elif age_min >= hold: why = "max hold time"
            elif liq < pos["entry_liq"] * 0.5: why = "liquidity collapse"

            if not why:  # refresh the open-trade row
                val = pos["size_tokens"] * price
                await n_patch(g, pos.get("page_id"), {"properties": {
                    "Current Price (USD)": _num(price), "Current Value (USD)": _num(val),
                    "Unrealised PnL (USD)": _num(val - pos["cost_usd"]),
                    "Unrealised PnL %": _num(gross / 100)}})
                continue

            exit_sig = None
            if not g["DRY_RUN"]:
                exit_sig = await sell_all(g, mint)
            net = gross - cost
            pnl_usd = pos["cost_usd"] * net / 100
            now = time.time()
            s["closed"].append({"mint": mint, "strat": strat, "sol_in": pos["sol_in"], "pnl_pct": net,
                                "pnl_usd": pnl_usd, "win": net > 0, "why": why, "ts": now})
            if g["DRY_RUN"]:
                s["paper_sol"] += pos["sol_in"] * net / 100
            del s["positions"][mint]
            save(s)

            cum = sum(t.get("pnl_usd", 0) for t in s["closed"])
            result = "Win" if net > 0 else "Loss" if net < 0 else "Breakeven"
            await n_patch(g, pos.get("page_id"), {"archived": True})
            await n_create(g, DB_CLOSED, {
                "Token": _title(pos["name"]), "Mint": _text(mint),
                "Entry Time": _date(pos["ts"]), "Exit Time": _date(now),
                "Entry Price (USD)": _num(pos["entry_price"]), "Exit Price (USD)": _num(price),
                "Size (Tokens)": _num(pos["size_tokens"]), "Hold Time (hrs)": _num((now - pos["ts"]) / 3600),
                "Realised PnL (USD)": _num(pnl_usd), "PnL %": _num(net / 100),
                "Result": _sel(result), "Exit Reason": _sel(why),
                "Entry Tx": _url(pos.get("entry_tx")), "Exit Tx": _url(solscan(exit_sig)),
                "Score": _num(pos.get("score")), "Holders": _num(pos.get("holders")),
                "Top 10 %": _num(pos.get("top10")), "Mode": _sel(mode),
                "Notes": _text(f"Strategy: {strat}")})
            await n_create(g, DB_EVENTS, {
                "Event": _title(f"Exit {pos['name']}"), "Type": _sel("Exit"), "Token": _text(pos["name"]),
                "Time": _date(now), "Price (USD)": _num(price), "Realised PnL (USD)": _num(pnl_usd),
                "Cumulative PnL (USD)": _num(cum), "Tx": _url(solscan(exit_sig)),
                "Tx Signature": _text(exit_sig or ""), "Mode": _sel(mode)})

            n, wins, pnl = stats(s)
            await notify(app, g, f"CLOSED [{strat}] {pos['name']} ({why}) net {net:+.1f}% = {pnl_usd:+.2f} USD\n"
                                 f"Record: {wins}/{n} wins ({wins/n:.0%}), net {pnl:+.4f} SOL\n{breakdown(s)}"
                                 f"{' [PAPER]' if g['DRY_RUN'] else ''}")
            await evaluate(app, g, s)
        except Exception as e:
            log.exception("monitor error %s", mint)
            await notify(app, g, f"Exit problem on {pos.get('name', mint[:6])}: {e}")


async def scan(app, g):
    s = load()
    if s["halted"]:
        return
    swing_open = sum(1 for p in s["positions"].values() if p.get("strat", "swing") == "swing")
    scalp_open = len(s["positions"]) - swing_open
    swing_slots = MAX_OPEN - swing_open
    scalp_slots = (SCALP_MAX_OPEN - scalp_open) if SCALP_ON else 0
    if swing_slots <= 0 and scalp_slots <= 0:
        return

    mode = "Paper" if g["DRY_RUN"] else "Live"
    bal = await bankroll(g, s)
    if s["start_sol"] is None:
        s["start_sol"] = bal
    sol_px, _ = await px(g, SOL)
    if not sol_px:
        return

    now = time.time()
    checked = 0
    for mint in await candidates(g):
        if checked >= SCAN_LIMIT or (swing_slots <= 0 and scalp_slots <= 0):
            break
        if mint in s["positions"] or now - s["seen"].get(mint, 0) < SEEN_TTL_MIN * 60:
            continue
        checked += 1
        s["seen"][mint] = now
        try:
            p = await best_pair(g, mint)
            if not p:
                continue
            want_scalp = scalp_slots > 0 and scalp_ok(p)
            want_swing = swing_slots > 0
            if not (want_scalp or want_swing):
                continue

            a = await g["analyze"](mint)
            if a["liq"] < g["MIN_LIQUIDITY_USD"]:
                continue
            ok, why = g["holder_gate"](a)
            if not ok:
                log.info("skip %s: %s", mint, why)
                continue
            if want_scalp and a["score"] >= SCALP_MIN_SCORE:
                strat = "scalp"
            elif want_swing and a["score"] >= MIN_SCORE:
                strat = "swing"
            else:
                continue

            price = float(p["priceUsd"])
            liq = float((p.get("liquidity") or {}).get("usd", 0))
            usable = bal if g["DRY_RUN"] else max(0.0, bal - FEE_RESERVE_SOL)
            if strat == "scalp":
                tp, sl, hold, cost = SCALP_TP, SCALP_SL, SCALP_HOLD, SCALP_COST
                need_usd = SCALP_TARGET_USD / ((tp - cost) / 100)
                size = min(need_usd / sol_px, usable * SCALP_BANKROLL_FRAC, g["MAX_SOL_PER_TRADE"])
                exp_net = size * sol_px * (tp - cost) / 100
                if exp_net < SCALP_TARGET_USD * 0.9:
                    log.info("skip %s: bankroll can't reach $%.2f target (would net $%.2f)", mint, SCALP_TARGET_USD, exp_net)
                    continue
            else:
                tp, sl, hold, cost = TP, SL, MAX_HOLD_MIN, COST_PCT
                size = min(g["MAX_SOL_PER_TRADE"], usable * 0.1)
                exp_net = size * sol_px * (tp - cost) / 100
            if size <= 0 or g["spent_today"]() + size > g["DAILY_SOL_LIMIT"]:
                continue

            sig = None
            if not g["DRY_RUN"]:
                q = await g["quote"](SOL, mint, int(size * 1e9))
                if float(q.get("priceImpactPct", 0)) * 100 > g["MAX_PRICE_IMPACT_PCT"]:
                    continue
                sig = await g["execute_swap"](q)
                g["spend"]["sol"] += size

            name = token_name(a, mint)
            cost_usd = size * sol_px
            size_tokens = cost_usd / price
            ts = time.time()
            page_id = await n_create(g, DB_OPEN, {
                "Token": _title(name), "Mint": _text(mint), "Entry Time": _date(ts),
                "Entry Price (USD)": _num(price), "Current Price (USD)": _num(price),
                "Size (Tokens)": _num(size_tokens), "Cost Basis (USD)": _num(cost_usd),
                "Current Value (USD)": _num(cost_usd), "Unrealised PnL (USD)": _num(0),
                "Unrealised PnL %": _num(0), "Entry Tx": _url(solscan(sig)),
                "Score": _num(a["score"]), "Holders": _num(a.get("holders")),
                "Top 10 %": _num(a.get("top10")), "Mode": _sel(mode),
                "Notes": _text(f"Strategy: {strat} | TP {tp}% SL {sl}% max hold {hold}m | target net ${exp_net:.2f}")})
            await n_create(g, DB_EVENTS, {
                "Event": _title(f"Entry {name}"), "Type": _sel("Entry"), "Token": _text(name),
                "Time": _date(ts), "Price (USD)": _num(price), "Tx": _url(solscan(sig)),
                "Tx Signature": _text(sig or ""), "Mode": _sel(mode)})

            s["positions"][mint] = {
                "name": name, "strat": strat, "entry_price": price, "entry_liq": liq, "sol_in": size, "ts": ts,
                "cost_usd": cost_usd, "size_tokens": size_tokens, "score": a["score"],
                "holders": a.get("holders"), "top10": a.get("top10"), "tp": tp, "sl": sl,
                "max_hold": hold, "cost_pct": cost, "entry_tx": solscan(sig), "page_id": page_id}
            save(s)
            if strat == "scalp": scalp_slots -= 1
            else: swing_slots -= 1
            await notify(app, g, f"OPENED [{strat}] {name}\n{mint}\n"
                                 f"Size {size:.4f} SOL (~${cost_usd:.2f}), TP {tp}% / SL {sl}% / {hold}m max\n"
                                 f"Target net ~${exp_net:.2f} if TP hits. Score {a['score']}, "
                                 f"{a.get('holders'):,} holders, top10 {a.get('top10')}%"
                                 f"{' [PAPER]' if g['DRY_RUN'] else ''}")
        except Exception as e:
            log.warning("scan %s: %s", mint, e)
    save(s)


def register(app, g):
    from telegram.ext import CommandHandler

    async def _scan(ctx): await scan(app, g)
    async def _mon(ctx): await monitor(app, g)

    async def cmd_autostatus(u, c):
        if u.effective_user.id != g["OWNER_ID"]: return
        s = load(); n, wins, pnl = stats(s)
        wr = f"{wins/n:.0%}" if n else "n/a"
        await u.message.reply_text(
            f"{'HALTED: ' + s['reason'] if s['halted'] else 'Running'} "
            f"({'PAPER' if g['DRY_RUN'] else 'LIVE'})\n"
            f"Closed: {n}, win rate {wr}, net PnL {pnl:+.4f} SOL\n{breakdown(s)}\n"
            f"Open: {len(s['positions'])}  Paper bankroll: {s['paper_sol']:.4f} SOL\n"
            f"Halts if win rate < {TARGET_WR:.0%} or net PnL < 0 after {EVAL_AFTER} trades.\n"
            f"Notion journal: {'on' if NOTION_TOKEN else 'off (set NOTION_TOKEN)'}")

    async def cmd_halt(u, c):
        if u.effective_user.id != g["OWNER_ID"]: return
        s = load(); s["halted"], s["reason"] = True, "manual halt"; save(s)
        await u.message.reply_text("Halted. No new entries; open positions still get closed.")

    app.add_handler(CommandHandler("autostatus", cmd_autostatus))
    app.add_handler(CommandHandler("halt", cmd_halt))
    app.job_queue.run_repeating(_scan, interval=120, first=15)
    app.job_queue.run_repeating(_mon, interval=20, first=30)
    log.info("Autonomous mode ON (%s)", "PAPER" if g["DRY_RUN"] else "LIVE")
