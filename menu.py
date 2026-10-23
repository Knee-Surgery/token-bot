"""
Button menu for the Telegram bot (inline keyboard, edits the same message in place).
Tap /start or send any text to open it. Used by evo.py; no slash commands needed.

The menu is always the last message in the chat: whenever the bot posts something else (alerts, search results,
analyses) or you send a message, the old menu is deleted and a fresh one is posted underneath. Every message in
the chat (yours and the bot's) is deleted once it is TG_KEEP_HOURS old (default 2).
"""
import asyncio, os, time, logging

from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup

import auto
import evo

log = logging.getLogger("menu")
JOURNAL_URL = os.getenv("JOURNAL_URL", "https://app.notion.com/p/40093233c10481e2ac93db11c0daf96e")
BACK = Markup([[Btn("⬅️ Menu", callback_data="m:menu")]])
KEEP_HOURS = float(os.getenv("TG_KEEP_HOURS", "2"))


def menu_markup(S):
    halted = S["halted"]
    return Markup([
        [Btn("📊 Leaderboard", callback_data="m:lb"), Btn("💼 Open trades", callback_data="m:open")],
        [Btn("📜 Recent trades", callback_data="m:recent"), Btn("🏆 Candidates", callback_data="m:cand")],
        [Btn("🔎 Search / analyze", callback_data="m:analyze"), Btn("⚙️ Rules", callback_data="m:rules")],
        [Btn("▶️ Resume" if halted else "⏸ Pause", callback_data="m:resume" if halted else "m:pause"),
         Btn("📓 Journal", url=JOURNAL_URL)],
    ] + ([[Btn("🌳 Family tree", url=f"https://www.notion.so/{S['tree_page'].replace('-', '')}")]]
         if S.get("tree_page") else []))


def menu_text(S) -> str:
    vs = list(S["variants"].values())
    act = len(evo.active(S))
    val = sum(1 for v in vs if v["status"] == "validated")
    ret = sum(1 for v in vs if v["status"] == "retired")
    open_n = sum(len(v["positions"]) for v in vs)
    closed = sum(len(v["closed"]) for v in vs)
    pnl = S.get("total_pnl")
    M = S.get("milestone") or {}
    goal = {"proving": f"🎯 Proof test running since {time.strftime('%d %b %H:%M UTC', time.gmtime(M.get('since', 0)))}",
            "ready": "✅ Proof test passed: ready for a live test (still paper)"}.get(
        M.get("stage"), f"Consistent bots: {M.get('count', 0)}/{evo.GOAL_BOTS}")
    return (f"TOKEN BOT\n\nMode: PAPER ({'PAUSED' if S['halted'] else 'running'})\n"
            + (f"Overall P&L: {'-' if pnl < 0 else '+'}${abs(pnl):,.2f} (target $0+)\n" if pnl is not None else "")
            + goal + "\n" +
            f"Variants active: {act} · Validated: {val} · Retired: {ret}\n"
            f"Open trades: {open_n} · Closed trades: {closed}\n\nPick an option below.")


def _short(m): return f"{m[:4]}..{m[-4:]}"


def _age(minutes):
    if minutes is None:
        return "?"
    return f"{minutes:.0f}m" if minutes < 120 else f"{minutes / 60:.0f}h" if minutes < 2880 else f"{minutes / 1440:.0f}d"


def fmt_search(q, rows) -> str:
    if not rows:
        return f"No Solana coins found for '{q}'. Try another name, a symbol, or the mint address."
    lines = []
    for r in rows:
        ch = "" if r["h24"] is None else f", 24h {r['h24']:+.0f}%"
        lines.append(f"{r['symbol']} ({r['name'][:22]}) · age {_age(r['age_min'])}, liq ${r['liq']:,.0f}, "
                     f"vol24 ${r['vol24']:,.0f}{ch}\n{r['mint']}")
    return f"Results for '{q}' (Solana only):\n\n" + "\n\n".join(lines) + "\n\nTap a coin for the full analysis."


def search_markup(rows):
    return Markup([[Btn(f"🔍 {r['symbol'][:14]}", callback_data=f"m:a:{r['mint']}")] for r in rows[:5]])


async def analysis_text(g, mint: str) -> str:
    a = await g["analyze"](mint)
    text = g["fmt_analysis"](a)
    p = a.get("pair")  # analyze() already fetched the best pair; no second DexScreener call
    if p:
        ra = {"holders": a.get("holders"), "top10": a.get("top10"),
              "danger": any("[danger]" in f for f in (a.get("flags") or []))}
        ra.update({k: v for k, v in (a.get("rc") or {}).items() if v is not None})
        ok, why = evo.rug_ok(ra, p)
        text += "\n\nBot rug check: " + ("PASS" if ok else f"FAIL ({why})")
    return text


async def screen(g, S, key, pc):
    if key == "lb":
        return evo.leaderboard(S)
    if key == "open":
        # snapshot first: the trading loop may open/close positions while we await prices
        rows = [(v, mint, pos) for v in list(S["variants"].values()) for mint, pos in list(v["positions"].items())]
        shown = rows[:30]
        await evo.prefetch(g, pc, [m for _, m, _ in shown], 8)  # one batched request, shared with the trading loop
        lines = []
        for v, mint, pos in shown:
            try:
                p = await evo.get_pair(g, mint, pc, 8)
                cur = f"{(float(p['priceUsd']) / pos['entry_price'] - 1) * 100:+.1f}%"
            except Exception:
                cur = "?"
            lines.append(f"{v['id']} {pos['name']}  {cur}  (TP {pos['tp']}% / SL {pos['sl']}% / "
                         f"trail {pos.get('trail', '-')}%{' ARMED' if pos.get('armed') else ''}, "
                         f"{(time.time() - pos['ts']) / 60:.0f}m of {pos['hold']}m)")
        total = len(rows)
        return f"OPEN TRADES (paper): {total} open" + (" (showing 30)" if total > 30 else "") + "\n\n" + ("\n".join(lines) or "None open right now.")
    if key == "recent":
        rows = [(t["ts"], v["id"], t) for v in S["variants"].values() for t in v["closed"]]
        rows.sort(key=lambda r: r[0], reverse=True)
        lines = [f"{vid} {_short(t['mint'])}  {t['pnl_pct']:+.1f}%  {t['pnl_usd']:+.2f} USD  ({t['why']})"
                 for _, vid, t in rows[:10]]
        return "RECENT TRADES\n\n" + ("\n".join(lines) or "No closed trades yet.")
    if key == "cand":
        vs = [v for v in S["variants"].values() if v["status"] in ("validated", "validating")]
        if vs:
            out = []
            for v in vs:
                n, w = len(v["closed"]), sum(1 for t in v["closed"] if t["win"])
                out.append(f"{v['id']} gen{v['gen']} {v['status'].upper()}: {w}/{n} wins, "
                           f"{sum(t['pnl_usd'] for t in v['closed']):+.2f} USD\n{v['genome']}")
            return "CANDIDATES\n\n" + "\n\n".join(out)
        best = sorted([v for v in S["variants"].values() if v["closed"] and v["status"] != "retired"],
                      key=evo.fitness, reverse=True)[:3]
        lines = [f"{v['id']}: {sum(1 for t in v['closed'] if t['win'])}/{len(v['closed'])} wins" for v in best]
        return "CANDIDATES\n\nNo variant has passed its checks yet.\n\nClosest right now:\n" + ("\n".join(lines) or "none")
    if key == "rules":
        return ("RULES (cannot be changed by the bot)\n\n"
                "• Paper trading only\n"
                f"• Rug check on every entry: pair at least {evo.HARD_MIN_AGE:.0f} min old, "
                f"{evo.HARD_MIN_HOLDERS}+ holders, top 10 under {evo.HARD_MAX_TOP10:.0f}%, "
                f"no wallet over {evo.HARD_MAX_TOP1:.0f}%, creator under {evo.HARD_MAX_CREATOR:.0f}%, insiders under "
                f"{evo.HARD_MAX_INSIDER:.0f}%, mint + freeze authority revoked, LP {evo.MIN_LP_LOCKED:.0f}%+ locked, "
                f"liquidity ${evo.HARD_MIN_LIQ:,.0f}+, no RugCheck danger flags, no heavy sell pressure, no falling knives, "
                "no wash volume\n"
                "• Nothing else is fixed: max token age, momentum, volume are optional and evolved\n"
                "• Finds coins from: newest pools (3 pages), new profiles, boosts, trending, top volume, "
                "community takeovers and rotating keyword searches, Solana only\n"
                f"• No minimum hold. Out by {evo.MAX_HOLD_FLAT:.0f} min unless in profit, then up to {evo.HARD_HOLD:.0f} min\n"
                "• Take-profit 45%+ (aim 100%+), trailing stop from peak once past break-even\n"
                f"• Above {evo.DISCRETION_PCT:.0f}% profit a bot decides its own exit at any time (pullback or momentum stall)\n"
                f"• Rug exit if liquidity drops {evo.RUG_LIQ_DROP:.0%}+ or price crashes {evo.CRASH_TICK_PCT:.0f}%+ between checks\n"
                f"• Requirements: win rate {evo.REQ_WR:.0%}+ and net profit\n"
                f"• Checked after {evo.CHECK_1} trades and again at {evo.CHECK_2}: fail = retired, pass = breeds\n"
                f"• Pass both = validation on {evo.VAL_TRADES} fresh trades, same requirements\n"
                f"• Up to {evo.MAX_PER_TOKEN} bots in one token at a time, each with different exits (stop-loss "
                f"{evo.EXIT_GAP_SL:g}+ pts or trailing stop {evo.EXIT_GAP_TRAIL:g}+ pts apart)\n"
                + (f"• Copy trading: idle bots follow a proven bot's entry within {evo.COPY_WINDOW_MIN:.0f} min, "
                   "using their own exits\n" if evo.COPY_ON else "") +
                "• Candidates are reported only; nothing goes live automatically")
    return menu_text(S)


# ---------- chat housekeeping: menu always last, messages older than KEEP_HOURS deleted ----------
_lock = asyncio.Lock()  # the poll task and the trading loop both post; one menu at a time


def _track(S, msg_id):
    S.setdefault("tg_msgs", []).append([msg_id, time.time()])
    S["last_id"] = max(S.get("last_id") or 0, msg_id)


async def say(tb, g, S, text, markup=None):
    """Post a normal message (alerts, results). It lands above the menu; keep_menu_last() moves the menu below."""
    m = await tb.send_message(g["OWNER_ID"], text[:3900], reply_markup=markup)
    _track(S, m.message_id)
    return m


async def show_menu(tb, g, S, text=None, markup=None):
    """Delete the old menu and post a fresh one at the bottom (silently: no notification for a menu move)."""
    async with _lock:
        old = S.get("menu_id")
        m = await tb.send_message(g["OWNER_ID"], (text or menu_text(S))[:3900],
                                  reply_markup=markup or menu_markup(S), disable_notification=True)
        S["menu_id"] = m.message_id
        _track(S, m.message_id)
        if old:
            S["tg_msgs"] = [x for x in S["tg_msgs"] if x[0] != old]
            try:
                await tb.delete_message(g["OWNER_ID"], old)
            except Exception:
                pass  # already gone or too old for Telegram to delete


async def keep_menu_last(tb, g, S):
    if not S.get("menu_id") or (S.get("last_id") or 0) > S["menu_id"]:
        await show_menu(tb, g, S)


async def cleanup(tb, g, S):
    """Delete every tracked message (bot's and the owner's) older than KEEP_HOURS, except the live menu,
    which is re-posted instead so the chat is never left without one. Telegram only lets bots delete
    messages under 48 hours old, so this has to keep running."""
    cut = time.time() - KEEP_HOURS * 3600
    menu_id = S.get("menu_id")
    old = [mid for mid, ts in S.get("tg_msgs", []) if ts < cut and mid != menu_id]
    for i in range(0, len(old), 100):  # deleteMessages takes up to 100 ids; missing ones are skipped
        try:
            await tb.delete_messages(g["OWNER_ID"], old[i:i + 100])
        except Exception as e:
            log.warning("cleanup delete failed: %s", e)
    gone = set(old)
    S["tg_msgs"] = [x for x in S.get("tg_msgs", []) if x[0] not in gone]
    if menu_id and any(mid == menu_id and ts < cut for mid, ts in S["tg_msgs"]):
        await show_menu(tb, g, S)  # menu itself is old: replace it with a fresh one at the bottom


async def _menu_screen(tb, g, S, q, text, markup):
    """Show a screen inside the menu message. A tap on any other (older) message gets a fresh menu instead."""
    if q.message and q.message.message_id == S.get("menu_id"):
        try:
            await q.edit_message_text(text[:3900], reply_markup=markup)
            return
        except Exception as e:
            if "not modified" in str(e).lower():
                return
    await show_menu(tb, g, S, text, markup)


async def poll(tb, g, S, pc):
    """Long polling: Telegram holds the request open and returns the moment a message or tap arrives, so replies
    are near-instant. Runs as its own task next to the trading loop; cancelled when the run ends."""
    while True:
        try:
            updates = await tb.get_updates(offset=S.get("tg_offset"), timeout=25,
                                           allowed_updates=["message", "callback_query"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("getUpdates failed: %s", e)
            await asyncio.sleep(3)
            continue
        for u in updates:
            await handle_update(tb, g, S, pc, u)
            S["tg_offset"] = u.update_id + 1  # after handling: an update cut off by shutdown is retried next run


async def handle_update(tb, g, S, pc, u):
    owner = g["OWNER_ID"]
    try:
        if u.callback_query:
            q = u.callback_query
            if q.from_user.id != owner:
                return
            try:
                await q.answer()
            except Exception:
                pass  # too old if it waited for the next run; the edit below still works
            key = (q.data or "m:menu").split(":", 1)[1]
            S["await"] = None
            if key == "analyze":
                S["await"] = "analyze"
                await _menu_screen(tb, g, S, q, "Send a coin name, symbol or mint address.\n\nI'll search Solana "
                                                "coins and show the best matches.", BACK)
            elif key.startswith("a:"):
                await tb.send_chat_action(owner, "typing")
                await say(tb, g, S, await analysis_text(g, key[2:]))
            elif key in ("pause", "resume"):
                S["halted"] = key == "pause"
                await _menu_screen(tb, g, S, q, menu_text(S), menu_markup(S))
            elif key == "menu":
                await _menu_screen(tb, g, S, q, menu_text(S), menu_markup(S))
            else:
                await _menu_screen(tb, g, S, q, await screen(g, S, key, pc), BACK)
        elif u.message and u.message.from_user and u.message.from_user.id == owner:
            _track(S, u.message.message_id)  # the owner's messages are cleaned up too
            text = (u.message.text or "").strip()
            if S.get("await") == "analyze" and text and not text.startswith("/"):
                S["await"] = None
                await tb.send_chat_action(owner, "typing")
                if 32 <= len(text) <= 44 and text.isalnum():      # a mint address: analyse it directly
                    await say(tb, g, S, await analysis_text(g, text))
                else:                                              # a name or symbol: search
                    rows = await auto.search_coins(g, text)
                    await say(tb, g, S, fmt_search(text, rows), search_markup(rows) if rows else None)
            elif (text.split() or [""])[0].split("@")[0].lower() == "/halt":
                S["halted"] = True
            await show_menu(tb, g, S)  # anything the owner sends brings a fresh menu to the bottom
    except Exception as e:
        log.warning("menu update failed: %s", e)
        try:
            await say(tb, g, S, f"Sorry, that failed: {e}"[:500])
        except Exception:
            pass
    try:
        await keep_menu_last(tb, g, S)
    except Exception as e:
        log.warning("menu repost failed: %s", e)
