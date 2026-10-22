"""
Runner for GitHub Actions (no server needed). Each workflow run stays alive for LOOP_SECONDS (default 240).

EVO_MODE=true (default): evolutionary engine (evo.py), several paper variants, state in evo_state.json.
EVO_MODE=false         : the single bot (auto.py), state in auto_state.json.
Paper mode only unless ALLOW_LIVE_ACTIONS=true (only affects the single bot; not recommended).
"""
import asyncio, logging, os, time

os.environ.setdefault("AUTO_MODE", "true")
if os.getenv("EVO_MODE", "true").lower() == "true":
    os.environ["MIN_HOLDERS"] = "50"     # rug-check floor; evolution can only make it stricter
    os.environ["MAX_TOP10_PCT"] = "80"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

import bot, auto, evo
from telegram import Bot

LOOP_SECONDS = int(os.getenv("LOOP_SECONDS", "240"))
EVO_MODE = os.getenv("EVO_MODE", "true").lower() == "true"
MONITOR_EVERY, SCAN_EVERY, CMD_EVERY = 20, 60, 60


async def handle_commands(tb: Bot):
    s = auto.load()
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
                    f"Closed: {n}, win rate {wr}, net PnL {pnl:+.4f} SOL\n{auto.breakdown(s)}\n"
                    f"Open: {len(s['positions'])}  Paper bankroll: {s['paper_sol']:.4f} SOL")
            elif cmd == "/analyze" and len(parts) > 1:
                a = await bot.analyze(parts[1])
                await tb.send_message(bot.OWNER_ID, bot.fmt_analysis(a))
            elif cmd in ("/start", "/help"):
                await tb.send_message(bot.OWNER_ID, "Commands: /autostatus, /halt, /analyze <mint>")
        except Exception as e:
            print("command failed:", e)
    auto.save(s)


async def main():
    tb = Bot(bot.BOT_TOKEN)
    g = vars(bot)
    if EVO_MODE:
        await evo.run(tb, g, LOOP_SECONDS)
        await bot.http.aclose()
        return

    if not bot.DRY_RUN and os.getenv("ALLOW_LIVE_ACTIONS") != "true":
        raise SystemExit("Live trading from GitHub Actions is disabled. Set DRY_RUN=true (paper mode).")

    class App:  # minimal shim: auto.py only needs app.bot.send_message
        bot = tb

    start = time.time()
    last_scan = last_cmd = 0.0
    while True:
        now = time.time()
        try:
            if now - last_cmd >= CMD_EVERY:
                await handle_commands(tb); last_cmd = now
            await auto.monitor(App, g)
            if now - last_scan >= SCAN_EVERY:
                await auto.scan(App, g); last_scan = now
        except Exception as e:
            print("cycle error:", e)
        if time.time() - start + MONITOR_EVERY >= LOOP_SECONDS:
            break
        await asyncio.sleep(MONITOR_EVERY)
    await bot.http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
