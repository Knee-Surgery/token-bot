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
BACK = Markup([[Btn("⬅️ Back", callback_data="m:menu")]])
KEEP_HOURS = float(os.getenv("TG_KEEP_HOURS", "2"))


def menu_markup(S):
    halted = S["halted"]
    return Markup([
        [Btn("📊 Top bots", callback_data="m:lb"), Btn("💼 Open trades", callback_data="m:open")],
        [Btn("📜 Recent", callback_data="m:recent"), Btn("🔎 Search", callback_data="m:analyze")],
        [Btn("▶️ Resume" if halted else "⏸ Pause", callback_data="m:resume" if halted else "m:pause"),
         Btn("📓 Journal", url=JOURNAL_URL)],
    ] + ([[Btn("🌳 Family tree", url=f"https://www.notion.so/{S['tree_page'].replace('-', '')}")]]
         if S.get("tree_page") else []))


def usd(x) -> str:
    return f"{'-' if x < 0 else '+'}${abs(x):,.2f}"


def _big(x) -> str:
    return f"${x / 1e6:,.1f}M" if x >= 1e6 else f"${x / 1e3:,.0f}k" if x >= 1e3 else f"${x:,.0f}"


def menu_text(S) -> str:
    pnl = S.get("total_pnl")
    M = S.get("milestone") or {}
    goal = {"proving": f"🎯 Proof test running (ends "
                       f"{time.strftime('%d %b %H:%M', time.gmtime(M.get('since', 0) + evo.PROOF_HOURS * 3600))} UTC)",
            "ready": "✅ Ready for a live test"}.get(M.get("stage"), f"🎯 Reliable bots: {M.get('count', 0)}/{evo.GOAL_BOTS}")
    open_n = sum(len(v["positions"]) for v in S["variants"].values())
    return (f"🤖 Token Bot · Paper · {'⏸ Paused' if S['halted'] else '▶️ Running'}\n\n"
            + (f"💰 P&L: {usd(pnl)}\n" if pnl is not None else "")
            + f"💼 Open trades: {open_n}\n{goal}")


def _age(minutes):
    if minutes is None:
        return "?"
    return f"{minutes:.0f}m" if minutes < 120 else f"{minutes / 60:.0f}h" if minutes < 2880 else f"{minutes / 1440:.0f}d"


def fmt_search(q, rows) -> str:
    if not rows:
        return f"🔎 Nothing found for '{q}'."
    lines = [f"{r['symbol']} · liq {_big(r['liq'])}" + ("" if r["h24"] is None else f" · 24h {r['h24']:+.0f}%")
             + f" · {_age(r['age_min'])} old" for r in rows[:5]]
    return f"🔎 {q}\n\n" + "\n".join(lines) + "\n\nTap one to check it."


def search_markup(rows):
    return Markup([[Btn(f"🔍 {r['symbol'][:14]}", callback_data=f"m:a:{r['mint']}")] for r in rows[:5]])


async def analysis_text(g, mint: str) -> str:
    a = await g["analyze"](mint)
    p = a.get("pair")
    if not p:
        return "🔍 No Solana trading pair found for that coin."
    ra = {"holders": a.get("holders"), "top10": a.get("top10"),
          "danger": any("[danger]" in f for f in (a.get("flags") or []))}
    ra.update({k: v for k, v in (a.get("rc") or {}).items() if v is not None})
    ok, why = evo.rug_ok(ra, p)
    score = a["score"]
    risk = "🟢 Lower risk" if score >= 75 else "🟡 Moderate" if score >= 55 else "🟠 High risk" if score >= 35 else "🔴 Avoid"
    ch = (p.get("priceChange") or {}).get("h24")
    lines = [f"🔍 {a.get('symbol') or mint[:6]}",
             f"Price ${p.get('priceUsd')} · Liq {_big(a.get('liq') or 0)}",
             f"24h vol {_big((p.get('volume') or {}).get('h24', 0) or 0)}" + ("" if ch is None else f" · 24h {ch:+.0f}%")]
    if a.get("holders") is not None:
        lines.append(f"Holders {a['holders']:,} · Top 10 {a.get('top10')}%")
    lines.append(f"Risk: {risk} ({score}/100)")
    lines.append("Bot check: ✅ pass" if ok else f"Bot check: ❌ {why}")
    return "\n".join(lines)


async def screen(g, S, key, pc):
    if key == "lb":
        live = [v for v in S["variants"].values() if v["status"] != "retired" and v["closed"]]
        live.sort(key=lambda v: -sum(t["pnl_usd"] for t in v["closed"]))
        rows = [f"{i}. {v['id']}  {usd(sum(t['pnl_usd'] for t in v['closed']))} · "
                f"{sum(1 for t in evo.test_trades(v) if t['win'])} wins of {evo.WIN_MIN_PCT:g}%+"
                for i, v in enumerate(live[:10], 1)]
        M = S.get("milestone") or {}
        return ("📊 TOP BOTS\n\n" + ("\n".join(rows) or "No trades yet.")
                + f"\n\nReliable: {M.get('count', 0)}/{evo.GOAL_BOTS}")
    if key == "open":
        # snapshot first: the trading loop may open/close positions while we await prices
        by_token: dict = {}
        for v in list(S["variants"].values()):
            for mint, pos in list(v["positions"].items()):
                by_token.setdefault(mint, []).append(pos)
        await evo.prefetch(g, pc, list(by_token), 8)  # one batched request, shared with the trading loop
        lines = []
        for mint, ps in sorted(by_token.items(), key=lambda kv: -len(kv[1]))[:15]:
            try:
                p = await evo.get_pair(g, mint, pc, 8)
                now_px = float(p["priceUsd"])
                chg = sum((now_px / pos["entry_price"] - 1) * 100 for pos in ps) / len(ps)
                cur = f"{chg:+.1f}%"
            except Exception:
                cur = "?"
            lines.append(f"{ps[0]['name']}  {cur}" + (f" · {len(ps)} bots" if len(ps) > 1 else ""))
        n = sum(len(ps) for ps in by_token.values())
        return f"💼 OPEN TRADES · {n}\n\n" + ("\n".join(lines) or "None right now.")
    if key == "recent":
        rows = sorted([(t, False) for v in S["variants"].values() for t in v["closed"]]
                      + [(t, True) for v in S["variants"].values() for t in v.get("recovery") or []],
                      key=lambda r: r[0]["ts"], reverse=True)[:10]
        lines = [f"{'🔁' if rec else '✅' if t['win'] else '❌'} {t.get('name') or t['mint'][:5]}  {t['pnl_pct']:+.1f}% · "
                 f"{usd(t['pnl_usd'])}" for t, rec in rows]
        return "📜 LAST 10 TRADES\n\n" + ("\n".join(lines) or "No trades yet.")
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
                await _menu_screen(tb, g, S, q, "🔎 Send a coin name, symbol or address.", BACK)
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
            await say(tb, g, S, "⚠️ That didn't work. Please try again.")
        except Exception:
            pass
    try:
        await keep_menu_last(tb, g, S)
    except Exception as e:
        log.warning("menu repost failed: %s", e)
