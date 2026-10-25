"""Second review (2026-10-25): losing-coin rule on the ledger by entry decision, partial sells journalled in the
background, founders' records starting with v3.3, trainee exit labels. Usage: python test_review2.py <src> <run dir>"""
import asyncio, os, sys, time
SRC, RUN = sys.argv[1], sys.argv[2]
os.chdir(RUN); sys.path.insert(0, SRC)
os.environ.update(TELEGRAM_BOT_TOKEN="1:x", EVO_POP="300"); os.environ.pop("NOTION_TOKEN", None)
import evo, auto, trainee, bot  # noqa: E402
import fakenet  # noqa: E402
fakenet.install(bot)

fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)
now = time.time()
M = "Coin" + "9" * 40


def row(bot_id, entry, close, usd):
    return {"mint": M, "name": "C", "bot": bot_id, "entry_ts": now - entry, "ts": now - close, "pnl_usd": usd,
            "pnl_pct": usd * 30, "why": "x"}


print("--- losing-coin rule: ledger, decisions by entry time")
S = {"variants": {}, "ledger": [row("gone1", 3600, 3000, -0.4)]}  # a bot since retired and dropped from the state
check(evo.coin_losing(S, M, now) == "last decision on it lost (6h)", "a dropped bot's losing trade still counts (ledger)")
# one decision: 3 bots entered together; one took a big profit early, two closed small losses later
S = {"variants": {}, "ledger": [row("a", 3600, 3300, +0.9), row("b", 3590, 600, -0.2), row("c", 3595, 590, -0.2)]}
check(evo.coin_losing(S, M, now) is None, "winning decision (+0.90, -0.20, -0.20) whose last exits lost: not blocked")
S = {"variants": {}, "ledger": [row("a", 3600, 3300, +0.1), row("b", 3590, 600, -0.3)]}
check(evo.coin_losing(S, M, now) == "last decision on it lost (6h)", "losing decision (+0.10, -0.30): blocked")
S = {"variants": {}, "ledger": [row("a", 9 * 3600, 8 * 3600, -0.5)]}
check(evo.coin_losing(S, M, now) is None, "a loss older than 6 h: not blocked")
S = {"variants": {}, "ledger": [row(f"b{i}", 7 * 3600 + i, 6 * 3600 + i * 60, -0.1) for i in range(10)] + [row("w", 3600, 3500, +0.2)]}
check(evo.coin_losing(S, M, now) == "bots keep losing on it (24h)", "10+ trades in 24 h, net negative: blocked")
S = {"variants": {}, "ledger": [dict(row("r", 3600, 3000, -0.5), rec=True)]}
check(evo.coin_losing(S, M, now) is None, "recovery trades are left out")
S = {"variants": {"v": {"closed": [row("v", 3600, 3000, -0.4)]}}, "ledger": [row("z", 300, 200, +0.0)]}
S["ledger"][0]["mint"] = "Other" + "1" * 39
check(evo.coin_losing(S, M, now) == "last decision on it lost (6h)", "trades from before the ledger existed still count")

print("--- partial sells are journalled in the background")
S = evo.load()
v = evo.active(S)[0]; v["positions"] = {}
calls = []
async def slow(*a, **k):
    calls.append(a[1] if len(a) > 1 else None); await asyncio.sleep(2); return "page"
auto.n_create = auto.n_patch = slow
async def go():
    pos = {"name": "C", "entry_price": 1.0, "cost_usd": 1.0, "size_tokens": 1.0, "ps_frac": 0.25, "ts": now, "orig_cost": 1.0,
           "score": 1, "holders": 1, "top10": 1, "page_id": None}
    v["positions"][M] = pos
    t = time.time(); await evo.partial_sell({"http": None}, S, v, M, pos, 1.2, time.time(), frac=0.25); dt = time.time() - t
    check(dt < 0.5 and evo._BG, f"partial sell returns at once ({dt:.2f} s; Notion writes queued: {len(evo._BG)})")
    check(abs(pos["part"] - 0.25 * (20 - evo.COST_PCT)) < 1e-9 and abs(pos["left"] - 0.75) < 1e-9, "bookkeeping unchanged")
    await asyncio.wait(list(evo._BG), timeout=10)
    check(len(calls) == 3, f"all 3 Notion writes done in the background ({len(calls)})")
asyncio.run(go())

print("--- founders' records start with v3.3")
S = evo.load(); S.pop("m33", None); S.pop("x1_founders", None)
for i in range(3):
    evo.new_variant(S, evo.random_genome(), 2, "v2171", ["old cross"], line="X1")
evo.crossbreed(S, [])
f = evo.founders(S)[0]
f["closed"] = [{"mint": "GTA", "pnl_pct": -17.7, "pnl_usd": -0.5, "win": False, "why": "stop-loss", "ts": now - 900, "name": "GTA"}]
f["positions"] = {M: {"ts": now - 60}}
evo.introduce_v33(S, [])
evo.founder_records_v33(S)
check(f["closed"] == [] and len(f["closed_pre"]) == 1 and f["positions"][M].get("pre"), "earlier trades -> closed_pre, open ones marked")
evo.founder_records_v33(S); check(len(f["closed_pre"]) == 1, "runs once")
# run #197: v3.3 went live on code without the reset; founders then traded under v3.3 before the reset ran
S2 = evo.load(); S2.pop("x1_rec33", None)
S2["m33"] = now - 1800
f2 = evo.founders(S2)[0] if evo.founders(S2) else None
if f2:
    f2["closed"] = [{"mint": "OLD", "pnl_usd": -0.5, "ts": now - 2000, "entry_ts": now - 2400},
                    {"mint": "NEW", "pnl_usd": 0.3, "ts": now - 600, "entry_ts": now - 900}]
    f2["closed_pre"] = []
    f2["positions"] = {"A": {"ts": now - 2500}, "B": {"ts": now - 100}}
    evo.founder_records_v33(S2)
    check([t["mint"] for t in f2["closed"]] == ["NEW"] and [t["mint"] for t in f2["closed_pre"]] == ["OLD"]
          and f2["positions"]["A"].get("pre") and not f2["positions"]["B"].get("pre"),
          "late reset: only trades opened before v3.3 went live move; v3.3 trades stay in the record")
else:
    check(False, "live state has founders")
check(evo.founders(S)[1]["closed"] == [] and S["x1_founders"] > now - 60, "the redesign time is recorded (not the old cross's)")
async def close_it():
    pos = dict(f["positions"][M], name="C", entry_price=1.0, entry_liq=80000, cost_usd=1.0, size_tokens=1.0, orig_cost=1.0,
               sl=7.0, hold=30, peak_price=1.0, last_price=1.0, m31=True, lock=None, ps_frac=0, ps_at=0, trail=10,
               lock_pull=3, stall=8, score=1, holders=1, top10=1, page_id=None, feat={})
    f["positions"][M] = pos
    async def cp(g, m, price, ref): return price
    evo.checked_price = cp
    await evo.monitor({"http": None}, S, {M: (time.time(), {"priceUsd": "0.90", "liquidity": {"usd": 80000}})}, [], [])
asyncio.run(close_it())
check(M not in f["positions"] and f["closed"] == [] and len(f["closed_pre"]) == 2,
      "a trade opened before the record started closes into closed_pre, not the ranking")
check(S["ledger"][-1]["bot"] == f["id"], "its money is still in the ledger")

print("--- trainee: rug and crash exits keep their own label")
arm = {"tp": 35, "sl": 10, "add": "none", "at_tp": "all"}
L = trainee.new_leg(1.0, 1.0, 100000, now)
check(trainee.step(L, arm, 0.70, 40000, now, False, False) == [("exit", "liquidity collapse")], "liquidity pulled at -30%: 'liquidity collapse'")
L = trainee.new_leg(1.0, 1.0, 100000, now)
check(trainee.step(L, arm, 0.70, 100000, now, False, False) == [("exit", "crash exit")], "one tick -30%: 'crash exit'")
L = trainee.new_leg(1.0, 1.0, 100000, now); L["last"] = 0.97
check(trainee.step(L, arm, 0.94, 100000, now, False, False) == [("exit", "hard stop -5%")], "slide to -6%: the -5% hard stop")
print("\nALL PASSED" if not fails else f"\n{len(fails)} FAILED"); sys.exit(bool(fails))
