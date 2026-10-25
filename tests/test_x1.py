"""Line X1 founders + population cut. Usage: python test_x1.py <src> <run dir>"""
import os, sys, json, time, asyncio
SRC, RUN = sys.argv[1], sys.argv[2]
os.chdir(RUN); sys.path.insert(0, SRC)
os.environ.update(TELEGRAM_BOT_TOKEN="1:x", EVO_POP="300"); os.environ.pop("NOTION_TOKEN", None)
import evo, menu, auto  # noqa: E402

fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)

S = evo.load()
open0 = {v["id"] for v in S["variants"].values() if v["positions"]}
ev = []
evo.crossbreed(S, ev)
X = evo.founders(S)
check([v["id"] for v in X] == [f"X1-{i:02d}" for i in range(1, 11)], f"10 founders X1-01..X1-10: {[v['id'] for v in X]}")
check(all(v["gen"] == 1 and v["parent"] is None and v["line"] == "X1" and "m5_arm" not in v for v in X), "generation 1, no parents, outside the A/B test")
W = [evo.m31_window(v["genome"]) for v in X]; EX = [evo.m31_exits(v["genome"]) for v in X]
check(len({w[:2] for w in W}) == 10, f"every founder has its own 5 m window: {[w[:2] for w in W]}")
check({w[2] for w in W} == {30000.0, 50000.0, 80000.0, 120000.0} and {e["sl"] for e in EX} == {6.0, 7.0, 8.0}
      and {e["ps_at"] for e in EX} == {20.0, 22.5, 25.0}, "liquidity, stop-loss and partial-sell levels all used")
check(len({v["genome"]["size_frac"] for v in X}) == 1, "same stake for all (results compare directly)")
check(len({json.dumps(v["genome"], sort_keys=True) for v in X}) == 10, "10 distinct genomes")
check(S["breed_hold"] and ev and ev[0].startswith("🧬"), f"breeding on hold; alert: {ev[0][:80] if ev else None}")
evo.crossbreed(S, ev); check(len([v for v in S["variants"].values() if v.get("line")]) == 10, "one-off: runs once")

# an older version already made X1 bots (cross v2171 x v1844) -> redesigned in place, extra X1 bots retired
S2 = evo.load()
S2.pop("x1_founders", None)
for i in range(12):
    v = evo.new_variant(S2, evo.random_genome(), 2, "v2171", ["old cross"], line="X1"); v["parent2"] = "v1844"
S2["cross_bred"] = 1.0
evo.crossbreed(S2, [])
F2 = evo.founders(S2)
check([v["id"] for v in F2] == [f"X1-{i:02d}" for i in range(1, 11)] and all(v["parent"] is None and "parent2" not in v for v in F2),
      "already-made X1 bots get the founder genes in place")
check(all(S2["variants"][f"X1-{i}"]["status"] == "retired" for i in (11, 12)), "later X1 bots from the old cross retired")
check(json.dumps(F2[0]["genome"], sort_keys=True) == json.dumps(X[0]["genome"], sort_keys=True), "founder genes are the same every time")

pre = {v["id"] for v in evo.active(S) if not v.get("line")}
evo.cut_population(S, ev)
act = evo.active(S); main = [v for v in act if not v.get("line")]
check(len(act) == 60 and len(main) == 50, f"population 50 main + 10 founders ({len(main)} + {len(act) - len(main)})")
check(S["variants"]["v2171"]["status"] != "retired" and S["variants"]["v1844"]["status"] != "retired", "v2171 and v1844 kept")
best = sorted([v for v in S["variants"].values() if v["id"] in pre and len(evo.test_trades(v)) >= 5], key=evo.fitness, reverse=True)[:20]
check(all(v["status"] != "retired" for v in best), "the best main bots are among the kept")
check(all(S["variants"][i]["positions"] for i in open0), "retired bots keep their open trades")

# breeding hold
main[-1]["status"] = "retired"; n = len(S["variants"]); evo.ensure_population(S, ev)
check(len(S["variants"]) == n, "while on hold, a retired main bot is not replaced")
m = main[0]; m["closed"] = [{"mint": f"M{i}", "pnl_pct": 30, "pnl_usd": 1, "win": True, "why": "x", "ts": 1, "name": "M"} for i in range(6)]
evo.breed_proven(S, m, ev); check(len(S["variants"]) == n, "no breeding while on hold")
# founders are never retired by the checks or the no-trade rule
f = X[0]; f["closed"] = [{"mint": f"L{i}", "pnl_pct": -10, "pnl_usd": -0.3, "win": False, "why": "stop-loss", "ts": 1, "name": "L"} for i in range(12)]
evo.apply_judgement(S, f, ev); check(f["status"] == "testing", "a losing founder is not retired (12 losses)")
g = X[1]; g["created"] = time.time() - 72 * 3600; g["closed"] = []
async def disc(gg, age=0): return []
auto.discover = disc
asyncio.run(evo.scan({"http": None}, S, {}, ev)); check(g["status"] == "testing", "a founder with no trades in 48 h is not retired")

# founders get first pick of the 3 slots per coin
for v in evo.active(S): v["positions"] = {}; v["cooldown"] = {}
for v in X: v["genome"]["min_m5"], v["genome"]["max_m5"] = 3.0, 15.0
now = time.time(); M = "Fpk" + "4" * 41
p = {"priceUsd": "1.0", "liquidity": {"usd": 80000}, "fdv": 2e6, "pairCreatedAt": (now - 2 * 86400) * 1000, "dexId": "raydium",
     "txns": {"h1": {"buys": 60, "sells": 40}, "m5": {"buys": 12, "sells": 6}}, "priceChange": {"m5": 4.2, "h1": 8, "h6": 30},
     "volume": {"h1": 80000}, "baseToken": {"symbol": "F"}}
S["acache"][M] = {"ts": now, "a": {"score": 60, "liq": 80000, "holders": 3000, "top10": 25, "symbol": "F", "danger": False,
                                   "mint_auth": False, "freeze_auth": False, "lp_locked": 100, "top1": 5, "insider_pct": 0, "creator_pct": 0, "fee_pct": 0, "risks": []}}
async def disc2(gg, age=0): return [M]
auto.discover = disc2
async def opn(g_, v, mint, p_, a, age, size, ex, recovery=False): v["positions"][mint] = {"ts": time.time(), "feat": {}}
real_open = evo.open_position; evo.open_position = opn
asyncio.run(evo.scan({"http": None}, S, {M: (time.time(), p)}, ev))
evo.open_position = real_open
held = [v for v in S["variants"].values() if M in v["positions"]]
check(len(held) == evo.MAX_PER_TOKEN and all(v.get("founder") for v in held), f"founders take the coin's slots first ({[v['id'] for v in held]})")

# report at 15 trades
S.pop("x1_report_15", None)
for i, v in enumerate(X):
    v["closed"] = [{"mint": f"C{j % 7}", "pnl_pct": (i - 4) * 1.0, "pnl_usd": (i - 4) * 0.01, "win": i > 4, "why": "x", "ts": 1, "name": "C"} for j in range(15)]
ev2 = []; evo.founder_report(S, ev2)
print("\n" + (ev2[0] if ev2 else "no report"))
check(ev2 and ev2[0].startswith("🧬") and ev2[0].index("X1-10") < ev2[0].index("X1-01") and "Tell me which 2" in ev2[0], "15-trade report, ranked by profit")
ev3 = []; evo.founder_report(S, ev3); check(not ev3, "report sent once per level")
lb = evo.leaderboard(S)
check("🧬 LINE X1 (gen-1 founders" in lb and lb.index("X1-01") < lb.index("Main bots:"), "Top bots: founders in their own section, first")
check("🧬 Line X1: " in menu.menu_text(S), "menu shows the line")
print("\nALL PASSED" if not fails else f"\n{len(fails)} FAILED"); sys.exit(bool(fails))
