"""
Button menu for the Telegram bot (inline keyboard, edits the same message in place).
Tap /start or send any text to open it. Used by evo.py; no slash commands needed.
"""
import os, time, logging

from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup

import evo

log = logging.getLogger("menu")
JOURNAL_URL = os.getenv("JOURNAL_URL", "https://app.notion.com/p/40093233c10481e2ac93db11c0daf96e")
BACK = Markup([[Btn("⬅️ Menu", callback_data="m:menu")]])


def menu_markup(S):
    halted = S["halted"]
    return Markup([
        [Btn("📊 Leaderboard", callback_data="m:lb"), Btn("💼 Open trades", callback_data="m:open")],
        [Btn("📜 Recent trades", callback_data="m:recent"), Btn("🏆 Candidates", callback_data="m:cand")],
        [Btn("🔍 Analyze token", callback_data="m:analyze"), Btn("⚙️ Rules", callback_data="m:rules")],
        [Btn("▶️ Resume" if halted else "⏸ Pause", callback_data="m:resume" if halted else "m:pause"),
         Btn("📓 Journal", url=JOURNAL_URL)],
    ])


def menu_text(S) -> str:
    vs = list(S["variants"].values())
    act = len(evo.active(S))
    val = sum(1 for v in vs if v["status"] == "validated")
    ret = sum(1 for v in vs if v["status"] == "retired")
    open_n = sum(len(v["positions"]) for v in vs)
    closed = sum(len(v["closed"]) for v in vs)
    return (f"TOKEN BOT\n\nMode: PAPER ({'PAUSED' if S['halted'] else 'running'})\n"
            f"Variants active: {act} · Validated: {val} · Retired: {ret}\n"
            f"Open trades: {open_n} · Closed trades: {closed}\n\nPick an option below.")


def _short(m): return f"{m[:4]}..{m[-4:]}"


async def screen(g, S, key):
    if key == "lb":
        return evo.leaderboard(S)
    if key == "open":
        lines = []
        for v in S["variants"].values():
            for mint, pos in v["positions"].items():
                try:
                    p = await evo.get_pair(g, mint, {}, 0)
                    cur = f"{(float(p['priceUsd']) / pos['entry_price'] - 1) * 100:+.1f}%"
                except Exception:
                    cur = "?"
                lines.append(f"{v['id']} {pos['name']}  {cur}  (TP {pos['tp']}% / SL {pos['sl']}%, "
                             f"{(time.time() - pos['ts']) / 60:.0f}m of {pos['hold']}m)")
        return "OPEN TRADES (paper)\n\n" + ("\n".join(lines) or "None open right now.")
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
        return "CANDIDATES\n\nNo variant has passed 30 trades yet.\n\nClosest right now:\n" + ("\n".join(lines) or "none")
    if key == "rules":
        return ("RULES (cannot be changed by the bot)\n\n"
                "• Paper trading only\n• 1,000+ holders\n• Top 10 holders 20% or less\n"
                f"• Each winning trade targets ${evo.TARGET_USD:.2f}+ net\n• Take-profit 5% or more, stop-loss at most 1.5x take-profit\n"
                f"• Retire a variant after {evo.EVAL_AFTER} trades if win rate < {evo.TARGET_WR:.0%} or PnL < 0\n"
                f"• Survive {evo.GRAD_TRADES} trades, then {evo.VAL_TRADES} fresh validation trades\n"
                "• Candidates are reported only; nothing goes live automatically")
    return menu_text(S)


async def _edit(tb, g, q, text, markup):
    try:
        await q.edit_message_text(text[:3900], reply_markup=markup)
    except Exception as e:
        if "not modified" in str(e).lower():
            return
        await tb.send_message(g["OWNER_ID"], text[:3900], reply_markup=markup)


async def handle(tb, g, S):
    try:
        updates = await tb.get_updates(offset=S.get("tg_offset"), timeout=0,
                                       allowed_updates=["message", "callback_query"])
    except Exception as e:
        log.warning("getUpdates failed: %s", e)
        return
    owner = g["OWNER_ID"]
    for u in updates:
        S["tg_offset"] = u.update_id + 1
        try:
            if u.callback_query:
                q = u.callback_query
                if q.from_user.id != owner:
                    continue
                try:
                    await q.answer()
                except Exception:
                    pass  # too old if it waited for the next run; the edit below still works
                key = (q.data or "m:menu").split(":", 1)[1]
                S["await"] = None
                if key == "analyze":
                    S["await"] = "analyze"
                    await _edit(tb, g, q, "Send me a token mint address to analyse.", BACK)
                elif key in ("pause", "resume"):
                    S["halted"] = key == "pause"
                    await _edit(tb, g, q, menu_text(S), menu_markup(S))
                elif key == "menu":
                    await _edit(tb, g, q, menu_text(S), menu_markup(S))
                else:
                    await _edit(tb, g, q, await screen(g, S, key), BACK)
            elif u.message and u.message.from_user and u.message.from_user.id == owner and u.message.text:
                text = u.message.text.strip()
                if S.get("await") == "analyze" and not text.startswith("/"):
                    S["await"] = None
                    if not (32 <= len(text) <= 44 and text.isalnum()):
                        await tb.send_message(owner, "That doesn't look like a Solana token address.", reply_markup=BACK)
                        continue
                    await tb.send_message(owner, "Analysing...")
                    a = await g["analyze"](text)
                    await tb.send_message(owner, g["fmt_analysis"](a)[:3900], reply_markup=BACK)
                elif text.split()[0].split("@")[0].lower() == "/halt":
                    S["halted"] = True
                    await tb.send_message(owner, menu_text(S), reply_markup=menu_markup(S))
                else:
                    await tb.send_message(owner, menu_text(S), reply_markup=menu_markup(S))
        except Exception as e:
            log.warning("menu update failed: %s", e)
