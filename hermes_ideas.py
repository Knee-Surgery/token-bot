"""
Hermes ideas: trade ideas from the Hermes agent on Zeshan's laptop, checked by this bot's own rules, approved on
Telegram, traded on paper, and fed back to Hermes so both sides improve.

Flow
  1. Hermes appends one JSON line per idea to ideas.jsonl in a separate repo (HERMES_IDEAS_REPO). Hermes can only
     write there: it never touches this repo, its secrets or a wallet.
  2. Every POLL_S seconds this module reads that file. Each new idea must pass, in order:
       format and limits -> not expired -> trust threshold (confidence) -> daily and open-position caps
       -> the same rug checks the evo bots use (evo.rug_ok)
  3. A passing idea is sent to the owner with Approve / Reject buttons. No tap within APPROVE_MIN = dropped.
  4. Approved ideas open a paper position (at most MAX_FRAC of the paper bankroll x trust size multiplier). Exits: take-profit,
     stop-loss, trailing stop once in profit, max hold, liquidity collapse.
  5. Every closed trade, rejection and skip (with the reason) is written to hermes_results.json, which Hermes reads
     before proposing anything new and uses to rewrite its own lessons.

Self-improvement on this side ("trust"): after every close the last WINDOW Hermes trades are scored.
  losing  -> min confidence +0.05 (up to 0.9) and size x0.75 (down to 0.25)
  winning -> min confidence -0.02 (down to 0.5) and size x1.10 (back up to 1.0, the full per-trade ceiling)
  last WINDOW trades lose more than PAUSE_LOSS_USD -> Hermes ideas paused until the owner taps Resume.
Hermes cannot change any of this: the numbers live in this bot's state, not in anything Hermes writes.

Goal: GOAL_PCT (8) % a day on the Hermes paper bankroll (starts at START_USD). Each UTC day's return is recorded;
the results file shows today, the 7-day average and how many of the last 7 days hit the goal, plus results per
strategy label so Hermes can drop what loses and repeat what wins. When the 7-day average reaches the goal with
enough trades, the owner is told. Nothing else happens: see the next paragraph.

PAPER ONLY, by construction: this module has no code that signs, swaps or touches a wallet, and it switches itself
off if the bot is ever started with DRY_RUN=false. Going live is not a setting here; it would need new code that
the owner asks for. Nothing Hermes writes can change limits, trust, the goal or the mode.

Off unless HERMES_IDEAS_REPO is set.
"""
import asyncio, json, logging, os, re, time
from datetime import datetime, timezone

import auto

log = logging.getLogger("hermes")

REPO = os.getenv("HERMES_IDEAS_REPO", "").strip()            # e.g. Knee-Surgery/token-bot-ideas
IDEAS_PATH = os.getenv("HERMES_IDEAS_PATH", "ideas.jsonl")
START_USD = float(os.getenv("HERMES_START_USD", "10"))         # paper bankroll Hermes starts with
MAX_FRAC = float(os.getenv("HERMES_MAX_FRAC", "0.10"))         # hard ceiling per trade: this share of the bankroll
MAX_USD = float(os.getenv("HERMES_MAX_USD", "25"))             # and never more than this (paper USD)
GOAL_PCT = float(os.getenv("HERMES_GOAL_PCT", "8"))            # target return per day on the paper bankroll
GOAL_DAYS, GOAL_MIN_TRADES = 7, 30                             # goal counts once held on average this long
MAX_BYTES, MAX_LINES = 200_000, 200                            # ideas file larger than this is ignored / cut
DAILY_MAX = int(os.getenv("HERMES_DAILY_MAX", "8"))            # ideas sent for approval per UTC day
MAX_OPEN = int(os.getenv("HERMES_MAX_OPEN", "3"))
MAX_AGE_MIN = float(os.getenv("HERMES_MAX_AGE_MIN", "15"))     # ideas older than this are stale whatever they say
APPROVE_MIN = float(os.getenv("HERMES_APPROVE_MIN", "10"))
PAUSE_LOSS_USD = float(os.getenv("HERMES_PAUSE_LOSS_USD", "3"))
MAX_DRIFT_PCT = float(os.getenv("HERMES_MAX_DRIFT_PCT", "15"))  # price ran this far since the check: don't chase
WINDOW = 10
POLL_S, MONITOR_S = 30, 15
RESULTS_FILE = "hermes_results.json"

MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")   # no ':' (Telegram button data is split on it)
STRAT_RE = re.compile(r"^[a-z0-9_-]{1,30}$")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")  # control / direction marks
LIMITS = {"tp_pct": (5, 300), "sl_pct": (2, 50), "max_hold_min": (1, 120)}


def enabled() -> bool:
    """Needs the ideas repo, and paper mode: started with DRY_RUN=false this module stays off."""
    return bool(REPO) and os.getenv("DRY_RUN", "true").lower() != "false"


def state(S) -> dict:
    h = S.setdefault("hermes", {})
    h.setdefault("seen", [])
    h.setdefault("pending", {})
    h.setdefault("positions", {})
    h.setdefault("closed", [])
    h.setdefault("skipped", [])
    h.setdefault("trust", {"min_conf": 0.6, "size_mult": 1.0, "paused": False, "changes": []})
    h.setdefault("day", "")
    h.setdefault("sent_today", 0)
    h.setdefault("equity", START_USD)
    h.setdefault("days", {})  # "YYYY-MM-DD": {"start": equity at the first close of the day, "pnl": ..., "trades": n}
    h.setdefault("goal_told", False)
    return h


def _now_iso(ts=None) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).isoformat(timespec="seconds")


def _parse_ts(x):
    try:
        return datetime.fromisoformat(str(x).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _skip(h, idea, why):
    h["skipped"].append({"id": idea.get("id"), "mint": idea.get("mint"), "why": why, "at": _now_iso(),
                         "confidence": idea.get("confidence")})
    h["skipped"] = h["skipped"][-60:]
    log.info("hermes idea %s skipped: %s", idea.get("id"), why)


def validate(idea: dict, now: float) -> tuple[dict | None, str]:
    """Format and limits only. Returns a cleaned copy or (None, reason)."""
    if not isinstance(idea, dict):
        return None, "not an object"
    if not ID_RE.match(str(idea.get("id", ""))):
        return None, "bad id"
    if not MINT_RE.match(str(idea.get("mint", ""))):
        return None, "bad mint address"
    if idea.get("side", "buy") != "buy":
        return None, "only buys are supported"
    strat = str(idea.get("strategy", "unlabelled")).lower()
    out = {"id": str(idea["id"]), "mint": str(idea["mint"]), "strategy": strat if STRAT_RE.match(strat) else "unlabelled",
           "reason": _CTRL.sub(" ", str(idea.get("reason", "")))[:500]}
    try:
        out["confidence"] = float(idea.get("confidence"))
        out["size_usd"] = float(idea.get("size_usd", MAX_USD))
        for k, (lo, hi) in LIMITS.items():
            out[k] = min(hi, max(lo, float(idea.get(k, {"tp_pct": 40, "sl_pct": 10, "max_hold_min": 30}[k]))))
    except (TypeError, ValueError):
        return None, "numbers missing or invalid"
    if not 0 <= out["confidence"] <= 1:
        return None, "confidence must be 0-1"
    if out["size_usd"] <= 0:
        return None, "size must be positive"
    created = _parse_ts(idea.get("created"))
    expires = _parse_ts(idea.get("expires"))
    if created is None:
        return None, "missing created time"
    if now - created > MAX_AGE_MIN * 60 or (expires and expires < now):
        return None, "expired before the bot saw it"
    out["created"] = created
    return out, ""


async def fetch_ideas(g) -> list[dict]:
    headers = {"Accept": "application/vnd.github.raw+json", "X-GitHub-Api-Version": "2022-11-28"}
    tok = os.getenv("GITHUB_TOKEN")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    r = await g["http"].get(f"https://api.github.com/repos/{REPO}/contents/{IDEAS_PATH}", headers=headers)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    if len(r.content) > MAX_BYTES:
        log.warning("hermes ideas file too large (%d bytes): ignored", len(r.content))
        return []
    ideas = []
    for line in r.text.splitlines()[-MAX_LINES:]:
        line = line.strip()
        if line:
            try:
                ideas.append(json.loads(line))
            except ValueError:
                continue
    return ideas


async def safety(g, mint: str):
    """The evo bots' rug checks, on the same analysis fields. Returns (ok, why, a, p)."""
    import evo
    full = await g["analyze"](mint)
    a = {k: full.get(k) for k in ("score", "liq", "holders", "top10", "symbol")}
    a["danger"] = any("[danger]" in f for f in (full.get("flags") or []))
    for k, v in (full.get("rc") or {}).items():
        if k in ("mint_auth", "freeze_auth", "rugged", "top1", "insider_pct", "creator_pct", "lp_locked", "fee_pct",
                 "risks") and v is not None:
            a[k] = v
    p = full.get("pair") or await auto.best_pair(g, mint)
    if not p or not p.get("priceUsd"):
        return False, "no Solana pair / price", a, p
    ok, why = evo.rug_ok(a, p)
    return ok, why, a, p


def cap_usd(h) -> float:
    return max(0.0, min(MAX_USD, h["equity"] * MAX_FRAC))


def size_for(h, idea) -> float:
    return round(min(idea["size_usd"], cap_usd(h) * min(1.0, h["trust"]["size_mult"])), 2)


async def check_new(tb, g, S, pc):
    import menu
    h = state(S)
    now = time.time()
    day = _now_iso(now)[:10]
    if h["day"] != day:
        h["day"], h["sent_today"] = day, 0
    try:
        ideas = await fetch_ideas(g)
    except Exception as e:
        log.warning("hermes fetch: %s", e)
        return
    seen = set(h["seen"])
    for raw in ideas:
        rid = str(raw.get("id", "")) if isinstance(raw, dict) else ""
        if not rid or rid in seen:
            continue
        seen.add(rid)
        h["seen"] = (h["seen"] + [rid])[-500:]
        idea, why = validate(raw, now)
        if not idea:
            _skip(h, raw if isinstance(raw, dict) else {}, why)
            continue
        if S.get("halted"):
            _skip(h, idea, "bot paused")
            continue
        if h["trust"]["paused"]:
            _skip(h, idea, "Hermes ideas paused after losses")
            continue
        if idea["confidence"] < h["trust"]["min_conf"]:
            _skip(h, idea, f"confidence {idea['confidence']:.2f} below current bar {h['trust']['min_conf']:.2f}")
            continue
        if h["sent_today"] >= DAILY_MAX:
            _skip(h, idea, f"daily limit of {DAILY_MAX} ideas reached")
            continue
        if len(h["positions"]) + len(h["pending"]) >= MAX_OPEN:
            _skip(h, idea, f"{MAX_OPEN} Hermes trades already open or waiting")
            continue
        if any(x["mint"] == idea["mint"] for x in list(h["positions"].values()) + list(h["pending"].values())):
            _skip(h, idea, "already holding or waiting on this coin")
            continue
        try:
            ok, why, a, p = await safety(g, idea["mint"])
        except Exception as e:
            _skip(h, idea, f"analysis failed: {e}"[:120])
            continue
        if not ok:
            _skip(h, idea, f"rug check: {why}")
            continue
        idea.update(name=_CTRL.sub(" ", str(auto.token_name(a, idea["mint"])))[:40], check_price=float(p["priceUsd"]),
                    a={k: a.get(k) for k in ("score", "holders", "top10")}, sent=now, size=size_for(h, idea))
        if idea["size"] < 0.05:
            _skip(h, idea, "paper bankroll too small for a trade")
            continue
        h["pending"][idea["id"]] = idea
        h["sent_today"] += 1
        text = (f"🧠 Hermes idea [{idea['strategy']}]: {idea['name']}\n{idea['mint']}\n\n{idea['reason']}\n\n"
                f"Confidence {idea['confidence']:.0%} | size ${idea['size']:.2f} (paper)\n"
                f"TP +{idea['tp_pct']:.0f}% | SL -{idea['sl_pct']:.0f}% | max hold {idea['max_hold_min']:.0f} min\n"
                f"Rug checks: passed | holders {a.get('holders')} | top 10 {a.get('top10')}%\n"
                f"Expires in {APPROVE_MIN:.0f} min.")
        from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup
        kb = Markup([[Btn("✅ Approve", callback_data=f"h:ok:{idea['id']}"),
                      Btn("❌ Reject", callback_data=f"h:no:{idea['id']}")]])
        try:
            await menu.say(tb, g, S, text, kb)
        except Exception as e:
            log.warning("hermes send: %s", e)


def expire_pending(h, now):
    for iid, idea in list(h["pending"].items()):
        if now - idea["sent"] > APPROVE_MIN * 60:
            del h["pending"][iid]
            _skip(h, idea, "not approved in time")


async def on_button(tb, g, S, pc, q):
    """Callback data h:ok:<id> or h:no:<id>."""
    import menu
    h = state(S)
    _, act, iid = (q.data.split(":", 2) + ["", ""])[:3]
    if act == "resume":
        h["trust"]["paused"] = False
        _trust_note(h, "resumed by owner")
        await menu.say(tb, g, S, "🧠 Hermes ideas resumed.")
        return
    idea = h["pending"].pop(iid, None)
    if not idea:
        await menu.say(tb, g, S, "That Hermes idea has expired or was already handled.")
        return
    if act == "no":
        _skip(h, idea, "owner rejected")
        await menu.say(tb, g, S, f"Rejected {idea['name']}. Hermes will see that.")
        return
    try:
        price, liq = await auto.px(g, idea["mint"])
    except Exception:
        price, liq = None, 0.0
    if not price:
        _skip(h, idea, "no price at approval")
        await menu.say(tb, g, S, f"Couldn't get a price for {idea['name']}; not opened.")
        return
    drift = (price / idea["check_price"] - 1) * 100
    if drift > MAX_DRIFT_PCT:
        _skip(h, idea, f"price ran {drift:+.0f}% between check and approval")
        await menu.say(tb, g, S, f"{idea['name']} already ran {drift:+.0f}%; not chasing it.")
        return
    open_position(g, S, idea, price, liq)
    await menu.say(tb, g, S, f"🧠 Opened {idea['name']} for ${idea['size']:.2f} (paper) at ${price:.8g}.")


def open_position(g, S, idea, price, liq):
    h = state(S)
    now = time.time()
    pos = {k: idea[k] for k in ("id", "mint", "name", "reason", "confidence", "tp_pct", "sl_pct", "max_hold_min",
                                "size", "a", "strategy")}
    pos.update(entry_price=price, entry_liq=liq, ts=now, peak=price, page_id=None)
    h["positions"][idea["id"]] = pos
    if not getattr(auto, "NOTION_TOKEN", None):
        return
    import evo
    a = idea["a"]
    open_row = {
        "Token": auto._title(pos["name"]), "Mint": auto._text(pos["mint"]), "Entry Time": auto._date(now),
        "Entry Price (USD)": auto._num(price), "Current Price (USD)": auto._num(price),
        "Size (Tokens)": auto._num(pos["size"] / price), "Cost Basis (USD)": auto._num(pos["size"]),
        "Current Value (USD)": auto._num(pos["size"]), "Unrealised PnL (USD)": auto._num(0),
        "Unrealised PnL %": auto._num(0), "Score": auto._num(a.get("score")), "Holders": auto._num(a.get("holders")),
        "Top 10 %": auto._num(a.get("top10")), "Mode": auto._sel("Paper"),
        "Notes": auto._text(f"Source: Hermes | conf {pos['confidence']:.2f} | TP {pos['tp_pct']:.0f}% "
                            f"SL {pos['sl_pct']:.0f}% hold {pos['max_hold_min']:.0f}m | {pos['reason']}")}
    event_row = {"Event": auto._title(f"Entry {pos['name']} [Hermes]"), "Type": auto._sel("Entry"),
                 "Token": auto._text(pos["name"]), "Time": auto._date(now), "Price (USD)": auto._num(price),
                 "Mode": auto._sel("Paper")}
    task = asyncio.create_task(evo._journal_entry(g, pos, open_row, event_row))
    evo._BG.add(task)
    task.add_done_callback(evo._BG.discard)


def exit_reason(pos, price, liq, now) -> str | None:
    gain = (price / pos["entry_price"] - 1) * 100
    peak_gain = (pos["peak"] / pos["entry_price"] - 1) * 100
    if gain >= pos["tp_pct"]:
        return "take-profit"
    if gain <= -pos["sl_pct"]:
        return "stop-loss"
    if peak_gain >= 10:  # trailing stop once clearly in profit, never below break-even
        floor = max(pos["entry_price"], pos["peak"] * (1 - pos["sl_pct"] / 100))
        if price <= floor:
            return "trailing stop"
    if pos.get("entry_liq") and liq < pos["entry_liq"] * 0.5:
        return "liquidity collapse"
    if now - pos["ts"] >= pos["max_hold_min"] * 60:
        return "max hold"
    return None


async def monitor(tb, g, S):
    import evo, menu
    h = state(S)
    now = time.time()
    for iid, pos in list(h["positions"].items()):
        try:
            price, liq = await auto.px(g, pos["mint"])
        except Exception as e:
            log.warning("hermes price %s: %s", pos["mint"], e)
            continue
        if not price:
            if now - pos["ts"] < pos["max_hold_min"] * 60:
                continue
            price, liq = pos.get("last_price") or pos["entry_price"], 0.0  # no price at max hold: book it flat-ish
        pos["last_price"] = price
        pos["peak"] = max(pos["peak"], price)
        why = exit_reason(pos, price, liq, now)
        if not why:
            continue
        pct = (price / pos["entry_price"] - 1) * 100
        pnl = pos["size"] * pct / 100
        rec = {"id": iid, "mint": pos["mint"], "name": pos["name"], "confidence": pos["confidence"],
               "strategy": pos.get("strategy", "unlabelled"),
               "reason": pos["reason"], "tp_pct": pos["tp_pct"], "sl_pct": pos["sl_pct"],
               "max_hold_min": pos["max_hold_min"], "size": pos["size"], "entry": pos["entry_price"],
               "exit": price, "peak_pct": round((pos["peak"] / pos["entry_price"] - 1) * 100, 1),
               "pnl_pct": round(pct, 1), "pnl_usd": round(pnl, 4), "exit_reason": why,
               "held_min": round((now - pos["ts"]) / 60, 1), "opened": _now_iso(pos["ts"]), "closed": _now_iso(now),
               "holders": pos["a"].get("holders"), "top10": pos["a"].get("top10")}
        h["closed"].append(rec)
        h["closed"] = h["closed"][-300:]
        del h["positions"][iid]
        book_day(h, pnl, now)
        S["total_pnl"] = cum = (S.get("total_pnl") or 0.0) + pnl
        await menu.say(tb, g, S, f"🧠 Hermes trade closed: {pos['name']} ({why}) {pct:+.1f}% = {pnl:+.2f} USD")
        if getattr(auto, "NOTION_TOKEN", None):
            closed_row = {
                "Token": auto._title(pos["name"]), "Mint": auto._text(pos["mint"]),
                "Entry Time": auto._date(pos["ts"]), "Exit Time": auto._date(now),
                "Entry Price (USD)": auto._num(pos["entry_price"]), "Exit Price (USD)": auto._num(price),
                "Size (Tokens)": auto._num(pos["size"] / pos["entry_price"]),
                "Hold Time (hrs)": auto._num((now - pos["ts"]) / 3600), "Realised PnL (USD)": auto._num(pnl),
                "PnL %": auto._num(pct / 100), "Result": auto._sel("Win" if pnl > 0 else "Loss" if pnl < 0 else "Breakeven"),
                "Exit Reason": auto._sel(why), "Holders": auto._num(pos["a"].get("holders")),
                "Top 10 %": auto._num(pos["a"].get("top10")), "Mode": auto._sel("Paper"),
                "Notes": auto._text(f"Source: Hermes | conf {pos['confidence']:.2f} | peak {rec['peak_pct']:+.0f}%")}
            event_row = {"Event": auto._title(f"Exit {pos['name']} [Hermes]"), "Type": auto._sel("Exit"),
                         "Token": auto._text(pos["name"]), "Time": auto._date(now), "Price (USD)": auto._num(price),
                         "Realised PnL (USD)": auto._num(pnl), "Cumulative PnL (USD)": auto._num(cum),
                         "Mode": auto._sel("Paper")}
            evo._journal(lambda pos=pos: auto.n_patch(g, pos.get("page_id"), {"archived": True}),
                         lambda r=closed_row: auto.n_create(g, auto.DB_CLOSED, r),
                         lambda r=event_row: auto.n_create(g, auto.DB_EVENTS, r))
        note = adapt(h)
        if note:
            await menu.say(tb, g, S, f"🧠 Hermes trust update: {note}", _resume_kb() if h["trust"]["paused"] else None)
        gl = goal(h)
        if gl["met"] and not h["goal_told"]:
            h["goal_told"] = True
            await menu.say(tb, g, S, f"🎯 Hermes paper goal reached: {gl['avg_pct']:+.1f}% a day on average over "
                                     f"{gl['days']} days ({gl['trades']} trades). Still paper only. Nothing goes live "
                                     f"unless you ask for it.")
        elif not gl["met"]:
            h["goal_told"] = False


def _resume_kb():
    from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup
    return Markup([[Btn("▶️ Resume Hermes", callback_data="h:resume:-")]])


def _trust_note(h, note):
    h["trust"]["changes"] = (h["trust"]["changes"] + [{"at": _now_iso(), "note": note}])[-30:]


def window_stats(closed: list) -> dict:
    w = closed[-WINDOW:]
    n = len(w)
    wins = sum(1 for t in w if t["pnl_usd"] > 0)
    net = sum(t["pnl_usd"] for t in w)
    return {"trades": n, "wins": wins, "win_rate": round(wins / n, 3) if n else None, "net_usd": round(net, 4)}


def adapt(h) -> str | None:
    """Adjust how much the bot trusts Hermes from its recent record. Returns a note when something changed."""
    t = h["trust"]
    st = window_stats(h["closed"])
    if st["trades"] < 5:
        return None
    old = (t["min_conf"], t["size_mult"], t["paused"])
    if st["net_usd"] <= -PAUSE_LOSS_USD and not t["paused"]:
        t["paused"] = True
    if st["net_usd"] < 0:
        t["min_conf"] = min(0.9, round(t["min_conf"] + 0.05, 2))
        t["size_mult"] = max(0.25, round(t["size_mult"] * 0.75, 3))
    elif st["win_rate"] >= 0.5:
        t["min_conf"] = max(0.5, round(t["min_conf"] - 0.02, 2))
        t["size_mult"] = min(1.0, round(t["size_mult"] * 1.10, 3))
    if (t["min_conf"], t["size_mult"], t["paused"]) == old:
        return None
    note = (f"last {st['trades']} trades: {st['wins']} wins, net {st['net_usd']:+.2f} USD -> "
            f"min confidence {t['min_conf']:.2f}, size x{t['size_mult']:.2f}"
            + (". PAUSED: losses passed the limit" if t["paused"] and not old[2] else ""))
    _trust_note(h, note)
    return note


def book_day(h, pnl, now):
    day = _now_iso(now)[:10]
    d = h["days"].setdefault(day, {"start": h["equity"], "pnl": 0.0, "trades": 0})
    d["pnl"] = round(d["pnl"] + pnl, 6)
    d["trades"] += 1
    h["equity"] = round(h["equity"] + pnl, 6)
    for k in sorted(h["days"])[:-60]:
        del h["days"][k]


def goal(h, today=None) -> dict:
    """Progress to GOAL_PCT a day. Only finished days count toward 'met'; days without trades count as 0%."""
    from datetime import date, timedelta
    today = today or _now_iso()[:10]
    pct = lambda d: round(d["pnl"] / d["start"] * 100, 2) if d["start"] > 0 else 0.0
    days = h["days"]
    first = min(days) if days else today
    t0 = date.fromisoformat(today)
    last = [(t0 - timedelta(days=i)).isoformat() for i in range(1, GOAL_DAYS + 1)]
    last = [k for k in last if k >= first]
    pcts = [pct(days[k]) if k in days else 0.0 for k in last]
    trades = sum(days[k]["trades"] for k in last if k in days)
    avg = round(sum(pcts) / len(pcts), 2) if pcts else None
    return {"target_pct_per_day": GOAL_PCT, "today_pct": pct(days[today]) if today in days else 0.0,
            "avg_pct": avg, "days": len(pcts), "days_at_target": sum(p >= GOAL_PCT for p in pcts), "trades": trades,
            "met": bool(len(pcts) >= GOAL_DAYS and avg >= GOAL_PCT and trades >= GOAL_MIN_TRADES),
            "equity": round(h["equity"], 4), "start_usd": START_USD}


def by_strategy(closed: list) -> list:
    out = {}
    for t in closed:
        o = out.setdefault(t.get("strategy", "unlabelled"), {"trades": 0, "wins": 0, "net_usd": 0.0, "pct": 0.0})
        o["trades"] += 1; o["wins"] += t["pnl_usd"] > 0; o["net_usd"] += t["pnl_usd"]; o["pct"] += t["pnl_pct"]
    return sorted(({"strategy": k, "trades": o["trades"], "win_rate": round(o["wins"] / o["trades"], 3),
                    "net_usd": round(o["net_usd"], 4), "avg_pnl_pct": round(o["pct"] / o["trades"], 1)}
                   for k, o in out.items()), key=lambda x: -x["net_usd"])


def calibration(closed: list) -> list:
    """Win rate and average result per confidence band: shows whether Hermes's confidence means anything."""
    bands = []
    for lo, hi in ((0, .6), (.6, .7), (.7, .8), (.8, .9), (.9, 1.01)):
        ts = [t for t in closed if lo <= t["confidence"] < hi]
        if ts:
            bands.append({"confidence": f"{lo:.1f}-{min(hi, 1):.1f}", "trades": len(ts),
                          "win_rate": round(sum(t["pnl_usd"] > 0 for t in ts) / len(ts), 3),
                          "avg_pnl_pct": round(sum(t["pnl_pct"] for t in ts) / len(ts), 1)})
    return bands


def write_results(S, path=RESULTS_FILE):
    """What Hermes reads to learn. Small, plain and committed with the bot's state at the end of each run."""
    h = state(S)
    closed = h["closed"]
    n = len(closed)
    out = {
        "updated": _now_iso(),
        "mode": "PAPER ONLY",
        "goal": goal(h),
        "by_strategy": by_strategy(closed),
        "days": {k: h["days"][k] for k in sorted(h["days"])[-14:]},
        "rules": {"max_trade_usd_now": round(cap_usd(h), 2), "max_frac": MAX_FRAC, "daily_max": DAILY_MAX, "max_open": MAX_OPEN, "max_age_min": MAX_AGE_MIN,
                  "approve_min": APPROVE_MIN, "limits": LIMITS},
        "trust": h["trust"],
        "all_time": {"trades": n, "wins": sum(t["pnl_usd"] > 0 for t in closed),
                     "net_usd": round(sum(t["pnl_usd"] for t in closed), 4)},
        "last_window": window_stats(closed),
        "by_confidence": calibration(closed),
        "open": [{k: p[k] for k in ("id", "name", "mint", "confidence", "size")} for p in h["positions"].values()],
        "closed": closed[-50:],
        "skipped": h["skipped"][-40:],
    }
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, indent=1)
    os.replace(tmp, path)


async def loop(tb, g, S, pc):
    """Runs beside the trading loop for the whole workflow run."""
    if not enabled():
        return
    state(S)
    last_poll = last_write = 0.0
    while True:
        try:
            now = time.time()
            expire_pending(state(S), now)
            if now - last_poll >= POLL_S:
                last_poll = now
                await check_new(tb, g, S, pc)
            await monitor(tb, g, S)
            if now - last_write >= 60:
                last_write = now
                write_results(S)
        except asyncio.CancelledError:
            write_results(S)
            raise
        except Exception as e:
            log.exception("hermes loop: %s", e)
        await asyncio.sleep(MONITOR_S)
