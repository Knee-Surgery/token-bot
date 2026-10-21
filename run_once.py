"""
Single-cycle runner for GitHub Actions (no server needed).
Each run: handle Telegram commands -> manage open positions -> scan for new entries -> exit.
State lives in auto_state.json, which the workflow commits back to the repo.
Paper mode only unless ALLOW_LIVE_ACTIONS=true (not recommended: Actions can't enforce per-day limits across runs).
"""
import asyncio, os

os.environ.setdefault("AUTO_MODE", "true")

import bot, auto
from telegram import Bot


async def handle_commands(tb: Bot, s: dict):
    try:
        updates = await tb.get_updates(offset=s.get("tg_offset"), timeout=0, allowed_updates=["message"])
    except Exception as e:
        print("getUpdates failed:", e)
        return
    for u in updates:
        s["tg_offset"] = u.update_id + 1
        m = u.message
        if not m or not m.from_user or m.from_user.id != bot.OWNER_ID or not m.text:
            continue
        parts = m.text.split()
        cmd = parts[0].split("@")[0].lower()
        try:
            if cmd == "/halt":
                s["halted"], s["reason"] = True, "manual halt"
                await tb.send_message(bot.OWNER_ID, "Halted. No new entries; open positions still get closed.")
            elif cmd == "/autostatus":
                n, wins, pnl = auto.stats(s)
                wr = f"{wins/n:.0%}" if n else "n/a"
                await tb.send_message(bot.OWNER_ID,
                    f"{'HALTED: ' + s['reason'] if s['halted'] else 'Running'} "
                    f"({'PAPER' if bot.DRY_RUN else 'LIVE'})\n"
                    f"Closed: {n}, win rate {wr}, net PnL {pnl:+.4f} SOL\n"
                    f"Open: {len(s['positions'])}  Paper bankroll: {s['paper_sol']:.4f} SOL\n"
                    f"Halts if win rate < {auto.TARGET_WR:.0%} or net PnL < 0 after {auto.EVAL_AFTER} trades.")
            elif cmd == "/analyze" and len(parts) > 1:
                a = await bot.analyze(parts[1])
                await tb.send_message(bot.OWNER_ID, bot.fmt_analysis(a))
            elif cmd in ("/start", "/help"):
                await tb.send_message(bot.OWNER_ID,
                    "Commands: /autostatus, /halt, /analyze <mint>\nReplies can lag up to ~5-10 minutes.")
        except Exception as e:
            print("command failed:", e)
    auto.save(s)


async def main():
    if not bot.DRY_RUN and os.getenv("ALLOW_LIVE_ACTIONS") != "true":
        raise SystemExit("Live trading from GitHub Actions is disabled. Set DRY_RUN=true (paper mode).")
    tb = Bot(bot.BOT_TOKEN)

    class App:  # minimal shim: auto.py only needs app.bot.send_message
        bot = tb

    s = auto.load()
    await handle_commands(tb, s)
    g = vars(bot)
    await auto.monitor(App, g)
    await auto.scan(App, g)
    await bot.http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
