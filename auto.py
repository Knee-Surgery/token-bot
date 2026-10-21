"""
Autonomous mode: scan -> analyse -> buy -> manage exits -> journal to Notion -> self-evaluate -> auto-HALT.

Paper trades while DRY_RUN=true (no real swaps; PnL from live prices), live otherwise.
Entry gates (all must pass): score >= AUTO_MIN_SCORE, liquidity, >= MIN_HOLDERS holders,
top-10 holders <= MAX_TOP10_PCT, price impact limit.

The bot halts itself (no new entries) if, after AUTO_EVAL_AFTER closed trades:
  - win rate < AUTO_TARGET_WINRATE (default 90%), OR
  - cumulative net PnL < 0
or at any time if drawdown exceeds AUTO_MAX_DRAWDOWN_PCT.
There is deliberately no auto-resume. Delete auto_state.json to reset.
"""
import json, os, time, logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("auto")
STATE = Path("auto_state.json")

TP = float(os.getenv("AUTO_TP_PCT", "12"))
SL = float(os.getenv("AUTO_SL_PCT", "8"))
MAX_HOLD_MIN = int(os.getenv("AUTO_MAX_HOLD_MIN", "120"))
MIN_SCORE = int(os.getenv("AUTO_MIN_SCORE", "75"))
MAX_OPEN = int(os.getenv("AUTO_MAX_OPEN", "2"))
TARGET_WR = float(os.getenv("AUTO_TARGET_WINRATE", "0.9"))
EVAL_AFTER = int(os.getenv("AUTO_EVAL_AFTER", "10"))
MAX_DD_PCT = float(os.getenv("AUTO_MAX_DRAWDOWN_PCT", "30"))
COST_PCT = float(os.getenv("AUTO_COST_PCT", "1.5"))  # est. fees+slippage per round trip
PAPER_START_SOL = float(os.getenv("AUTO_PAPER_START_SOL", "0.05"))
FEE_RESERVE_SOL = 0.01
SOL = "So11111111111111111111111111111111111111112"

# Notion journal (optional): create an internal integration, share the "Token Bot Journal" page with it
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DB_OPEN = os.getenv("NOTION_DB_OPEN", "32b8584b-2398-4b20-b92c-18a2dc1b05af")
DB_CLOSED = os.getenv("NOTION_DB_CLOSED", "255f98ab-f760-447a-a453-8550f416baff")
DB_EVENTS = os.getenv("NOTION_DB_EVENTS", "802d475b-a09c-4022-9a25-c609e9325cae")


# ---------- state ----------
def load() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"halted": False, "reason": "", "positions": {}, "closed": [], "seen": [],
            "paper_sol": PAPER_START_SOL, "start_sol": None}


def save(s: dict):
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


# ---------- market helpers ----------
async def px(g, mint):
    r = await g["http"].get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
    ps = [p for p in (r.json().get("pairs") or []) if p.get("chainId") == "solana"]
    if not ps:
        return None, 0.0
    p = max(ps, key=lambda x: (x.get("liquidity") or {}).get("usd", 0))
    return float(p["priceUsd"]), float((p.get("liquidity") or {}).get("usd", 0))


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
            gross = (price / pos["entry_price"] - 1) * 100
            age_min = (time.time() - pos["ts"]) / 60
            why = None
            if gross >= TP: why = "take-profit"
            elif gross <= -SL: why = "stop-loss"
            elif age_min >= MAX_HOLD_MIN: why = "max hold time"
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
            net = gross - COST_PCT
            pnl_usd = pos["cost_usd"] * net / 100
            now = time.time()
            s["closed"].append({"mint": mint, "sol_in": pos["sol_in"], "pnl_pct": net, "pnl_usd": pnl_usd,
                                "win": net > 0, "why": why, "ts": now})
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
                "Top 10 %": _num(pos.get("top10")), "Mode": _sel(mode)})
            await n_create(g, DB_EVENTS, {
                "Event": _title(f"Exit {pos['name']}"), "Type": _sel("Exit"), "Token": _text(pos["name"]),
                "Time": _date(now), "Price (USD)": _num(price), "Realised PnL (USD)": _num(pnl_usd),
                "Cumulative PnL (USD)": _num(cum), "Tx": _url(solscan(exit_sig)),
                "Tx Signature": _text(exit_sig or ""), "Mode": _sel(mode)})

            n, wins, pnl = stats(s)
            await notify(app, g, f"CLOSED {pos['name']} ({why}) net {net:+.1f}%\n"
                                 f"Record: {wins}/{n} wins ({wins/n:.0%}), net {pnl:+.4f} SOL"
                                 f"{' [PAPER]' if g['DRY_RUN'] else ''}")
            await evaluate(app, g, s)
        except Exception as e:
            log.exception("monitor error %s", mint)
            await notify(app, g, f"Exit problem on {pos.get('name', mint[:6])}: {e}")


async def scan(app, g):
    s = load()
    if s["halted"] or len(s["positions"]) >= MAX_OPEN:
        return
    mode = "Paper" if g["DRY_RUN"] else "Live"
    bal = await bankroll(g, s)
    if s["start_sol"] is None:
        s["start_sol"] = bal
    r = await g["http"].get("https://api.dexscreener.com/token-profiles/latest/v1")
    cands = [x["tokenAddress"] for x in r.json() if x.get("chainId") == "solana"]
    checked = 0
    for mint in cands:
        if mint in s["seen"] or mint in s["positions"]:
            continue
        if checked >= 12 or len(s["positions"]) >= MAX_OPEN:
            break
        checked += 1
        s["seen"] = (s["seen"] + [mint])[-500:]
        try:
            a = await g["analyze"](mint)
            if a["score"] < MIN_SCORE or a["liq"] < g["MIN_LIQUIDITY_USD"]:
                continue
            ok, why = g["holder_gate"](a)
            if not ok:
                log.info("skip %s: %s", mint, why)
                continue
            if g["DRY_RUN"]:
                size = min(g["MAX_SOL_PER_TRADE"], bal * 0.1)
            else:
                size = min(g["MAX_SOL_PER_TRADE"], max(0.0, bal - FEE_RESERVE_SOL) * 0.1)
            if size <= 0 or g["spent_today"]() + size > g["DAILY_SOL_LIMIT"]:
                continue
            price, liq = await px(g, mint)
            sol_px, _ = await px(g, SOL)
            if price is None or not sol_px:
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
            now = time.time()
            page_id = await n_create(g, DB_OPEN, {
                "Token": _title(name), "Mint": _text(mint), "Entry Time": _date(now),
                "Entry Price (USD)": _num(price), "Current Price (USD)": _num(price),
                "Size (Tokens)": _num(size_tokens), "Cost Basis (USD)": _num(cost_usd),
                "Current Value (USD)": _num(cost_usd), "Unrealised PnL (USD)": _num(0),
                "Unrealised PnL %": _num(0), "Entry Tx": _url(solscan(sig)),
                "Score": _num(a["score"]), "Holders": _num(a.get("holders")),
                "Top 10 %": _num(a.get("top10")), "Mode": _sel(mode)})
            await n_create(g, DB_EVENTS, {
                "Event": _title(f"Entry {name}"), "Type": _sel("Entry"), "Token": _text(name),
                "Time": _date(now), "Price (USD)": _num(price), "Tx": _url(solscan(sig)),
                "Tx Signature": _text(sig or ""), "Mode": _sel(mode)})

            s["positions"][mint] = {
                "name": name, "entry_price": price, "entry_liq": liq, "sol_in": size, "ts": now,
                "cost_usd": cost_usd, "size_tokens": size_tokens, "score": a["score"],
                "holders": a.get("holders"), "top10": a.get("top10"),
                "entry_tx": solscan(sig), "page_id": page_id}
            save(s)
            await notify(app, g, f"OPENED {name}\n{mint}\nSize {size:.4f} SOL (~${cost_usd:.2f}), score {a['score']}, "
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
            f"Closed: {n}, win rate {wr}, net PnL {pnl:+.4f} SOL\n"
            f"Open: {len(s['positions'])}  Paper bankroll: {s['paper_sol']:.4f} SOL\n"
            f"Halts if win rate < {TARGET_WR:.0%} or net PnL < 0 after {EVAL_AFTER} trades.\n"
            f"Notion journal: {'on' if NOTION_TOKEN else 'off (set NOTION_TOKEN)'}")

    async def cmd_halt(u, c):
        if u.effective_user.id != g["OWNER_ID"]: return
        s = load(); s["halted"], s["reason"] = True, "manual halt"; save(s)
        await u.message.reply_text("Halted. No new entries; open positions still get closed.")

    app.add_handler(CommandHandler("autostatus", cmd_autostatus))
    app.add_handler(CommandHandler("halt", cmd_halt))
    app.job_queue.run_repeating(_scan, interval=300, first=15)
    app.job_queue.run_repeating(_mon, interval=60, first=30)
    log.info("Autonomous mode ON (%s)", "PAPER" if g["DRY_RUN"] else "LIVE")
