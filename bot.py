"""
Solana token analysis + permissioned trading bot (Telegram + Jupiter).

Commands (owner only):
  /analyze <mint>          risk/quality report
  /buy <mint> <sol>        quote -> Confirm/Cancel buttons -> swap
  /sell <mint> <percent>   quote -> Confirm/Cancel buttons -> swap
  /balance                 SOL balance + mode
  /status                  limits and today's spend

Every trade requires a button tap. Nothing runs automatically.
"""
import os, time, base64, logging, secrets
from datetime import date

import httpx
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tokenbot")

# ---------- config ----------
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_ID = int(os.getenv("OWNER_CHAT_ID", "8643619357"))
RPC_URL = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
JUP_BASE = os.getenv("JUPITER_BASE", "https://lite-api.jup.ag/swap/v1")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() != "false"
MAX_SOL_PER_TRADE = float(os.getenv("MAX_SOL_PER_TRADE", "0.1"))
DAILY_SOL_LIMIT = float(os.getenv("DAILY_SOL_LIMIT", "0.3"))
SLIPPAGE_BPS = int(os.getenv("SLIPPAGE_BPS", "300"))
MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "20000"))
MAX_PRICE_IMPACT_PCT = float(os.getenv("MAX_PRICE_IMPACT_PCT", "3"))
CONFIRM_TTL = 60  # seconds
MIN_HOLDERS = int(os.getenv("MIN_HOLDERS", "1000"))
MAX_TOP10_PCT = float(os.getenv("MAX_TOP10_PCT", "20"))  # top 10 holders combined, excl. liquidity pools

SOL = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000

wallet: Keypair | None = None
if os.getenv("WALLET_PRIVATE_KEY"):
    wallet = Keypair.from_base58_string(os.environ["WALLET_PRIVATE_KEY"])

pending: dict[str, dict] = {}
spend = {"day": date.today(), "sol": 0.0}
http = httpx.AsyncClient(timeout=20)


# ---------- helpers ----------
def spent_today() -> float:
    if spend["day"] != date.today():
        spend.update(day=date.today(), sol=0.0)
    return spend["sol"]


async def rpc(method: str, params: list):
    r = await http.post(RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(j["error"])
    return j["result"]


async def token_balance(mint: str) -> tuple[int, int]:
    res = await rpc("getTokenAccountsByOwner",
                    [str(wallet.pubkey()), {"mint": mint}, {"encoding": "jsonParsed"}])
    raw, dec = 0, 0
    for acc in res["value"]:
        ta = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
        raw += int(ta["amount"]); dec = ta["decimals"]
    return raw, dec


# ---------- analysis ----------
async def analyze(mint: str) -> dict:
    out = {"mint": mint, "flags": [], "score": 50, "liq": 0.0, "lines": []}
    r = await http.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
    pairs = [p for p in (r.json().get("pairs") or []) if p.get("chainId") == "solana"]
    if not pairs:
        out["flags"].append("No Solana trading pairs found")
        out["score"] = 0
        return out
    p = max(pairs, key=lambda x: (x.get("liquidity") or {}).get("usd", 0))
    liq = (p.get("liquidity") or {}).get("usd", 0) or 0
    vol24 = (p.get("volume") or {}).get("h24", 0) or 0
    ch = p.get("priceChange") or {}
    tx = (p.get("txns") or {}).get("h24") or {}
    buys, sells = tx.get("buys", 0), tx.get("sells", 0)
    age_h = (time.time() * 1000 - p["pairCreatedAt"]) / 3.6e6 if p.get("pairCreatedAt") else None
    fdv = p.get("fdv") or 0
    out["liq"] = liq
    out["symbol"] = p["baseToken"].get("symbol", "")

    s = 50
    if liq < MIN_LIQUIDITY_USD: s -= 25; out["flags"].append(f"Low liquidity (${liq:,.0f})")
    elif liq > 200_000: s += 10
    if vol24 and liq and vol24 / liq > 15: s -= 10; out["flags"].append("Volume/liquidity extremely high (wash-trading or pump)")
    if age_h is not None and age_h < 24: s -= 15; out["flags"].append(f"Pair is only {age_h:.1f}h old")
    if sells and buys / max(sells, 1) > 3: s -= 5; out["flags"].append("Heavily one-sided buys (possible honeypot / bot activity)")
    if buys and sells == 0: s -= 20; out["flags"].append("Zero sells - possible honeypot")
    if (ch.get("h24") or 0) > 300: s -= 10; out["flags"].append("Up >300% in 24h - chasing risk")

    # RugCheck (best effort)
    try:
        rc = await http.get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary")
        if rc.status_code == 200:
            j = rc.json()
            risks = j.get("risks") or []
            for rk in risks[:6]:
                lvl = rk.get("level", "")
                out["flags"].append(f"RugCheck [{lvl}]: {rk.get('name')}")
                if lvl == "danger": s -= 15
                elif lvl == "warn": s -= 5
            out["lines"].append(f"RugCheck score (higher = riskier): {j.get('score_normalised', j.get('score'))}")
    except Exception as e:
        log.warning("rugcheck failed: %s", e)

    # Holder distribution (best effort; the gate fails closed if this is missing)
    out["holders"], out["top10"] = None, None
    try:
        fr = await http.get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report")
        if fr.status_code == 200:
            rep = fr.json()
            out["holders"] = rep.get("totalHolders")
            skip = set()
            for m in rep.get("markets") or []:
                for k in ("pubkey", "liquidityA", "liquidityB", "mintLP"):
                    if m.get(k):
                        skip.add(m[k])
            tops = [h for h in (rep.get("topHolders") or [])
                    if h.get("address") not in skip and h.get("owner") not in skip]
            out["top10"] = round(sum(float(h.get("pct", 0)) for h in tops[:10]), 2)
    except Exception as e:
        log.warning("holder data failed: %s", e)
    if out["holders"] is not None:
        out["lines"].append(f"Holders: {out['holders']:,}   Top 10 (excl. pools): {out['top10']}%")
    ok, why = holder_gate(out)
    if not ok:
        s -= 20
        out["flags"].append(f"Holder gate: {why}")

    out["score"] = max(0, min(100, s))
    out["lines"] = [
        f"{p['baseToken']['symbol']} / {p['quoteToken']['symbol']} on {p.get('dexId')}",
        f"Price: ${p.get('priceUsd')}   FDV: ${fdv:,.0f}",
        f"Liquidity: ${liq:,.0f}   24h vol: ${vol24:,.0f}",
        f"Change  5m {ch.get('m5')}%  1h {ch.get('h1')}%  24h {ch.get('h24')}%",
        f"24h txns: {buys} buys / {sells} sells",
    ] + out["lines"]
    return out


def fmt_analysis(a: dict) -> str:
    verdict = "AVOID" if a["score"] < 35 else "HIGH RISK" if a["score"] < 55 else "MODERATE" if a["score"] < 75 else "LOWER RISK"
    txt = "\n".join(a["lines"]) + f"\n\nScore: {a['score']}/100 - {verdict}"
    if a["flags"]:
        txt += "\n\nFlags:\n- " + "\n- ".join(a["flags"])
    txt += "\n\nHeuristics only. Not financial advice; a good score is not safety."
    return txt


def holder_gate(a: dict) -> tuple[bool, str]:
    """Hard rule: >= MIN_HOLDERS holders and top-10 concentration <= MAX_TOP10_PCT. Fails closed."""
    if a.get("holders") is None or a.get("top10") is None:
        return False, "holder data unavailable"
    if a["holders"] < MIN_HOLDERS:
        return False, f"only {a['holders']:,} holders (min {MIN_HOLDERS:,})"
    if a["top10"] > MAX_TOP10_PCT:
        return False, f"top 10 hold {a['top10']}% (max {MAX_TOP10_PCT:g}%)"
    return True, ""


# ---------- Jupiter ----------
async def quote(inp: str, out: str, amount: int) -> dict:
    r = await http.get(f"{JUP_BASE}/quote", params={
        "inputMint": inp, "outputMint": out, "amount": amount, "slippageBps": SLIPPAGE_BPS})
    r.raise_for_status()
    return r.json()


async def execute_swap(q: dict) -> str:
    r = await http.post(f"{JUP_BASE}/swap", json={
        "quoteResponse": q, "userPublicKey": str(wallet.pubkey()),
        "wrapAndUnwrapSol": True, "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": {"priorityLevelWithMaxLamports": {"maxLamports": int(os.getenv("MAX_PRIORITY_LAMPORTS", "50000")), "priorityLevel": "high"}},
    })
    r.raise_for_status()
    raw = base64.b64decode(r.json()["swapTransaction"])
    tx = VersionedTransaction.from_bytes(raw)
    signed = VersionedTransaction(tx.message, [wallet])
    sig = await rpc("sendTransaction", [base64.b64encode(bytes(signed)).decode(),
                                        {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}])
    return sig


# ---------- telegram ----------
def owner_only(fn):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else None
        if uid != OWNER_ID:
            log.warning("blocked user %s", uid)
            return
        return await fn(update, ctx)
    return wrapper


@owner_only
async def cmd_start(u: Update, c):
    await u.message.reply_text(__doc__ + f"\nMode: {'DRY RUN (no real trades)' if DRY_RUN else 'LIVE'}")


@owner_only
async def cmd_analyze(u: Update, c):
    if not c.args: return await u.message.reply_text("Usage: /analyze <mint>")
    await u.message.reply_text("Analysing...")
    try:
        await u.message.reply_text(fmt_analysis(await analyze(c.args[0])))
    except Exception as e:
        await u.message.reply_text(f"Analysis failed: {e}")


@owner_only
async def cmd_balance(u: Update, c):
    if not wallet: return await u.message.reply_text("No wallet configured.")
    bal = (await rpc("getBalance", [str(wallet.pubkey())]))["value"] / LAMPORTS
    await u.message.reply_text(f"{wallet.pubkey()}\n{bal:.4f} SOL\nMode: {'DRY RUN' if DRY_RUN else 'LIVE'}")


@owner_only
async def cmd_status(u: Update, c):
    await u.message.reply_text(
        f"Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}\nMax/trade: {MAX_SOL_PER_TRADE} SOL\n"
        f"Daily buy limit: {DAILY_SOL_LIMIT} SOL (spent {spent_today():.3f})\n"
        f"Slippage: {SLIPPAGE_BPS/100:.1f}%  Min liquidity: ${MIN_LIQUIDITY_USD:,.0f}")


async def propose(u: Update, side: str, mint: str, in_amount: int, sol_value: float, desc: str):
    inp, out = (SOL, mint) if side == "buy" else (mint, SOL)
    q = await quote(inp, out, in_amount)
    impact = float(q.get("priceImpactPct", 0)) * 100
    if impact > MAX_PRICE_IMPACT_PCT:
        return await u.message.reply_text(f"Blocked: price impact {impact:.2f}% > {MAX_PRICE_IMPACT_PCT}% limit.")
    tid = secrets.token_hex(4)
    pending[tid] = {"side": side, "mint": mint, "in": in_amount, "sol": sol_value, "exp": time.time() + CONFIRM_TTL}
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Confirm", callback_data=f"ok:{tid}"),
                                InlineKeyboardButton("Cancel", callback_data=f"no:{tid}")]])
    await u.message.reply_text(
        f"{side.upper()} {desc}\nPrice impact: {impact:.2f}%  Slippage: {SLIPPAGE_BPS/100:.1f}%\n"
        f"Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}\nExpires in {CONFIRM_TTL}s.", reply_markup=kb)


@owner_only
async def cmd_buy(u: Update, c):
    if not wallet: return await u.message.reply_text("No wallet configured.")
    try:
        mint, sol = c.args[0], float(c.args[1])
    except Exception:
        return await u.message.reply_text("Usage: /buy <mint> <sol_amount>")
    if sol <= 0 or sol > MAX_SOL_PER_TRADE:
        return await u.message.reply_text(f"Amount must be 0 < x <= {MAX_SOL_PER_TRADE} SOL.")
    if spent_today() + sol > DAILY_SOL_LIMIT:
        return await u.message.reply_text(f"Daily limit hit ({spent_today():.3f}/{DAILY_SOL_LIMIT} SOL).")
    try:
        a = await analyze(mint)
        await u.message.reply_text(fmt_analysis(a))
        if a["liq"] < MIN_LIQUIDITY_USD:
            return await u.message.reply_text("Blocked: liquidity below minimum.")
        ok, why = holder_gate(a)
        if not ok:
            return await u.message.reply_text(f"Blocked: {why}.")
        await propose(u, "buy", mint, int(sol * LAMPORTS), sol, f"{sol} SOL of {mint[:6]}...")
    except Exception as e:
        await u.message.reply_text(f"Failed: {e}")


@owner_only
async def cmd_sell(u: Update, c):
    if not wallet: return await u.message.reply_text("No wallet configured.")
    try:
        mint, pct = c.args[0], float(c.args[1])
        assert 0 < pct <= 100
    except Exception:
        return await u.message.reply_text("Usage: /sell <mint> <percent 1-100>")
    try:
        raw, dec = await token_balance(mint)
        amt = int(raw * pct / 100)
        if amt <= 0: return await u.message.reply_text("No balance for that token.")
        await propose(u, "sell", mint, amt, 0, f"{pct:g}% ({amt/10**dec:,.4f}) of {mint[:6]}...")
    except Exception as e:
        await u.message.reply_text(f"Failed: {e}")


async def on_button(u: Update, c):
    q = u.callback_query
    if q.from_user.id != OWNER_ID: return
    await q.answer()
    action, tid = q.data.split(":")
    t = pending.pop(tid, None)
    if not t or action == "no":
        return await q.edit_message_text("Cancelled." if t else "Expired or already handled.")
    if time.time() > t["exp"]:
        return await q.edit_message_text("Expired. Re-run the command.")
    if t["side"] == "buy" and spent_today() + t["sol"] > DAILY_SOL_LIMIT:
        return await q.edit_message_text("Daily limit would be exceeded.")
    try:
        inp, out = (SOL, t["mint"]) if t["side"] == "buy" else (t["mint"], SOL)
        fresh = await quote(inp, out, t["in"])  # re-quote right before signing
        impact = float(fresh.get("priceImpactPct", 0)) * 100
        if impact > MAX_PRICE_IMPACT_PCT:
            return await q.edit_message_text(f"Aborted: impact now {impact:.2f}%.")
        if DRY_RUN:
            return await q.edit_message_text(f"DRY RUN: would send {t['side']} (out amount {fresh['outAmount']}). Nothing executed.")
        sig = await execute_swap(fresh)
        if t["side"] == "buy": spend["sol"] += t["sol"]
        await q.edit_message_text(f"Sent: https://solscan.io/tx/{sig}")
    except Exception as e:
        await q.edit_message_text(f"Trade failed: {e}")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    for name, fn in [("start", cmd_start), ("analyze", cmd_analyze), ("buy", cmd_buy), ("sell", cmd_sell),
                     ("balance", cmd_balance), ("status", cmd_status)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_button))
    if os.getenv("AUTO_MODE", "false").lower() == "true":
        import auto
        auto.register(app, globals())
    log.info("Bot running. DRY_RUN=%s", DRY_RUN)
    app.run_polling()


if __name__ == "__main__":
    main()
