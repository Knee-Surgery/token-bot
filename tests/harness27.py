"""Coin reports: live reports for coins the bots traded today, then the scan hook on the real saved state."""
import asyncio, os, sys, time, random

SRC, RUN = sys.argv[1], sys.argv[2]
os.chdir(RUN); sys.path.insert(0, SRC)
os.environ.update(TELEGRAM_BOT_TOKEN="1:x", EVO_POP="300"); os.environ.pop("NOTION_TOKEN", None)
if not os.path.exists("evo_state.json"):  # offline: the bot's own state from the repo (run_all.py copies it here)
    open("evo_state.json", "wb").write(open(os.path.join(SRC, "evo_state.json"), "rb").read())
import evo, auto, bot, coin_report  # noqa: E402
import fakenet  # noqa: E402
fakenet.install(bot)

fails = []
def check(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c: fails.append(m)

async def nc(g, db, props): return "page"
auto.n_create = nc

async def live(S, mint):
    full = await bot.analyze(mint)
    p = full.get("pair")
    a = {k: full.get(k) for k in ("score", "liq", "holders", "top10", "symbol")}
    a["danger"] = any("[danger]" in f for f in (full.get("flags") or []))
    for k, x in (full.get("rc") or {}).items():
        a[k] = x
    return p, a

async def main():
    random.seed(27)
    S = evo.load()
    names = {}
    for v in S["variants"].values():
        for t in v["closed"]:
            names[t["mint"]] = t.get("name")
    mints = sorted(names, key=lambda m: -sum(1 for v in S["variants"].values() for t in v["closed"] if t["mint"] == m))[:4]
    for m in mints:
        p, a = await live(S, m)
        if not p:
            print("   no pair for", names[m]); continue
        sc, text = coin_report.report(names[m], m, p, a, coin_report.track_record(S, m))
        print("\n" + text + f"\n   ({len(text)} chars)")
        check(len(text) <= 1500 and sc is not None and 0 <= sc <= 100, f"{names[m]}: score {sc}, {len(text)} chars")
    # missing data never counts as a pass
    sc, text = coin_report.report("EMPTY", "E" * 44, {"priceUsd": "1"}, {}, {"n": 0})
    check("no data" in text and (sc is None or sc <= 50), f"empty data: score {sc}, missing data shown as no data")
    # scan hook
    now = time.time()
    act = evo.active(S)
    for v in act: v["positions"] = {}; v["cooldown"] = {}
    for v in S["variants"].values():
        if v["status"] == "retired": v["positions"] = {}
    M = "Fou" + "4" * 41
    p = {"priceUsd": "1.0", "liquidity": {"usd": 80000}, "fdv": 2e6, "marketCap": 2e6, "pairCreatedAt": (now - 2 * 86400) * 1000,
         "txns": {"h1": {"buys": 60, "sells": 40}, "m5": {"buys": 12, "sells": 6}, "h6": {"buys": 600, "sells": 400}},
         "priceChange": {"m5": 4.2, "h1": 8, "h6": 30}, "volume": {"h1": 80000, "h24": 900000}, "dexId": "raydium",
         "info": {"socials": [{"type": "twitter"}]}, "boosts": {"active": 10}}
    S["acache"][M] = {"ts": now, "a": {"score": 60, "liq": 80000, "holders": 3000, "top10": 25, "symbol": "FOUR", "danger": False,
                                       "mint_auth": False, "freeze_auth": False, "lp_locked": 100, "top1": 5, "insider_pct": 0,
                                       "creator_pct": 0, "fee_pct": 0, "risks": []}}
    async def disc(g, age=0): return [M]
    auto.discover = disc
    ev = []
    await evo.scan({"http": None}, S, {M: (time.time(), p)}, ev)
    held = [v for v in act if M in v["positions"]]
    reps = [e for e in ev if e.startswith("🔎")]
    stored = (S.get("coin_reports") or {}).get(M) or {}
    check(len(held) == evo.MAX_PER_TOKEN and not reps, f"{len(held)} bots entered, no report sent to Telegram ({len(reps)})")
    check(stored.get("text", "").startswith("🔎") and stored.get("score") is not None, "the report is stored in the state instead")
    if stored: print("\n" + stored["text"])
    check(all(v["positions"][M]["feat"].get("evidence") is not None for v in held), "every new trade carries the evidence score")
    for v in held: del v["positions"][M]; v["cooldown"] = {}
    ev2 = []
    await evo.scan({"http": None}, S, {M: (time.time(), p)}, ev2)
    check(not [e for e in ev2 if e.startswith("🔎")] and sum(1 for v in act if M in v["positions"]) == evo.MAX_PER_TOKEN
          and S["coin_reports"][M]["ts"] >= stored["ts"], "same coin again: bots enter, nothing sent, stored report refreshed")
    print("\nALL PASSED" if not fails else f"\n{len(fails)} FAILED")

asyncio.run(main())
