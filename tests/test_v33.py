"""v3.3 exits: 25% minimum take-profit on the whole trade, partial-sell ladder, protective floors, 8% rule gone.
Usage: python test_v33.py <src> <run dir>"""
import asyncio, os, sys, time
SRC, RUN = sys.argv[1], sys.argv[2]
os.chdir(RUN); sys.path.insert(0, SRC)
os.environ.update(TELEGRAM_BOT_TOKEN="1:x", EVO_POP="300"); os.environ.pop("NOTION_TOKEN", None)
import evo, bench, bot  # noqa: E402
import fakenet  # noqa: E402
fakenet.install(bot)

fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)
C = evo.COST_PCT
t0 = time.time() - 3600


def pos_for(ex):
    return {"entry_price": 1.0, "entry_liq": 80000, "ts": t0, "sl": ex["sl"], "hold": ex["hold"], "peak_price": 1.0,
            "last_price": 1.0, "lock": None, "ps_frac": ex["ps_frac"], "ps_at": ex["ps_at"], "ps_step": ex["ps_step"],
            "ps_next": ex["ps_at"], "part": 0.0, "left": 1.0}


def walk(prices, ex, step_s=5):
    """Run m33_step over a price path; returns (whole net %, reason, partial sells)."""
    pos, n = pos_for(ex), 0
    for k, pr in enumerate(prices[1:], 1):
        g = (pr - 1) * 100
        act = evo.m33_step(pos, pr, 80000, t0 + k * step_s)
        if act and act[0] == "partial":
            s = pos["left"] * act[1]; pos["part"] += s * (g - C); pos["left"] -= s; n += 1
        elif act:
            slip = evo.RUG_EXIT_SLIP if act[1] in ("liquidity collapse", "crash exit") else 0
            return pos["part"] + pos["left"] * (g - C - slip), act[1], n
    return None, None, n


gn = dict(evo.random_genome(), sl=7.0, ps_at=10.0, ps_step=10.0, ps_frac=0.25)
ex = evo.m33_exits(gn)
print("--- the 8% rule is gone")
v = {"closed": [{"pnl_pct": p, "pnl_usd": p / 100, "win": p > 0} for p in (2.0, 5.0, 30.0, -8.0)]}
check(len(evo.test_trades(v)) == 4, "every closed trade counts in testing, small wins included")

print("--- profit is only taken once the whole trade nets 25%")
up_down = [1 + x / 100 for x in list(range(0, 23)) + list(range(22, -1, -1))]  # up to +22%, back down
res, why, n = walk(up_down, ex)
check(why != "rising profit lock" and (res is None or res < 25), f"peak +22% gross: no profit-taking exit ({why}, {res})")
check(n >= 1, f"partial sells happen below 25% ({n} at +10%, +20%)")
up = [1 + x / 100 for x in range(0, 61)] + [1.60 - x / 100 for x in range(1, 40)]  # to +60%, then down
res, why, n = walk(up, ex)
check(why == "rising profit lock" and res >= evo.MIN_TP_NET - 0.5, f"take-profit exit: whole trade {res:+.2f}% (25% within one price step) ({why})")
check(n >= 4, f"as many partial sells as the price allows: {n} (+10, +20, +30, +40, +50, +60)")
# lock ratchets with the whole trade: big run, then a fall -> keeps far more than 25
big = [1 + x / 100 for x in range(0, 151)] + [2.5 - x / 100 for x in range(1, 100)]
res2, why2, _ = walk(big, evo.m33_exits(dict(gn, ps_frac=0)))
check(why2 == "rising profit lock" and res2 > 60, f"after a +150% run the lock keeps {res2:+.1f}% (no partial sells)")
# partial sold low -> the rest must go higher before profit is taken
lowps = evo.m33_exits(dict(gn, ps_at=6.0, ps_step=50.0, ps_frac=0.5))
p = [1 + x / 100 for x in range(0, 29)] + [1.28 - x / 1000 for x in range(1, 40)]  # to +28%, then down slowly
res3, why3, n3 = walk(p, lowps)
check(n3 == 1 and why3 != "rising profit lock", f"half sold at +6%: at +28% the whole trade is short of 25%, no take-profit ({why3})")

print("--- protective floors")
f5 = [1 + x / 100 for x in range(0, 12)] + [1.11 - x / 100 for x in range(1, 12)]  # +11% gross (+8.5 net), back down
res, why, _ = walk(f5, evo.m33_exits(dict(gn, ps_frac=0)))
check(why == "protective floor +5%" and abs(res - 5) < 1.01, f"been +5% net, falls back: sold at the +5% floor ({res:+.2f}%)")
f0 = [1.0, 1.01, 1.02, 1.03, 1.035, 1.03, 1.02, 1.01, 1.0]  # +3.5% gross (+1 net), back to entry
res, why, _ = walk(f0, evo.m33_exits(dict(gn, ps_frac=0)))
check(why == "protective floor 0%" and -1.01 < res <= 0, f"been in profit, falls back: sold at break-even ({res:+.2f}%)")
never = [1.0, 0.99, 0.97, 0.95, 0.93, 0.92]
res, why, _ = walk(never, evo.m33_exits(dict(gn, ps_frac=0)))
check(why == "stop-loss", f"never in profit: the normal stop-loss ({res:+.1f}%)")
crash = [1.0, 1.05, 1.10, 1.12, 0.80]  # +12%, then one tick -29%
res, why, _ = walk(crash, evo.m33_exits(dict(gn, ps_frac=0)))
check(why == "crash exit" and res < -20, f"a crash that jumps past the floor sells at the real price, no better ({res:+.1f}%)")

print("--- live monitor = replay")
S = evo.load()
v = evo.active(S)[0]; v["positions"] = {}
v["genome"] = evo.clamp(dict(v["genome"], ps_at=10.0, ps_step=10.0, ps_frac=0.25, sl=7.0))
M = "FakeA" + "1" * 39
path = [1 + x / 100 for x in range(0, 82, 3)] + [1.81 - x / 100 for x in range(3, 110, 3)]  # to +81%, then down to -28%
ex_v = evo.m33_exits(v["genome"])
async def go():
    async def nojournal(*a, **k): return None
    evo.auto.n_patch = evo.auto.n_create = nojournal
    async def cp(g, m, price, ref): return price
    evo.checked_price = cp
    await evo.open_position({"http": None}, v, M, {"priceUsd": "1.0", "liquidity": {"usd": 80000}, "pairCreatedAt": (time.time() - 86400 * 2) * 1000},
                            {"score": 60, "holders": 3000, "top10": 20, "symbol": "FA"}, 3000, 1.0, ex_v)
    pos = v["positions"][M]; pos["ts"] = time.time(); pos["entry_price"] = 1.0; pos["peak_price"] = pos["last_price"] = 1.0
    pos["orig_cost"] = pos["cost_usd"] = 1.0; pos["size_tokens"] = 1.0
    for k, pr in enumerate(path[1:], 1):
        if M not in v["positions"]:
            break
        pc = {M: (time.time(), {"priceUsd": str(pr), "liquidity": {"usd": 80000}})}
        await evo.monitor({"http": None}, S, pc, [], [])
asyncio.run(go())
rec = v["closed"][-1]
rows = [[t0 + k * 15, pr, 80000, 4, 8, 20, 12, 6, 80000] for k, pr in enumerate(path)]
r_pct, r_why, _ = bench.exit_from(rows, 0, dict(ex_v, sl=round(ex_v["sl"] * 4) / 4))
check(rec["why"] == r_why and abs(rec["pnl_pct"] - r_pct) < 1e-6 and rec["partials"] >= 3,
      f"monitor {rec['pnl_pct']:+.2f}% ({rec['why']}, {rec['partials']} partial sells) = replay {r_pct:+.2f}% ({r_why})")
check(rec["pnl_pct"] >= 25 - 1.0, f"the live trade ended at +25% (within one price step): {rec['pnl_pct']:+.2f}%")

print("--- one-off for the population")
S = evo.load(); S.pop("m33", None)
for v in evo.active(S): v["genome"]["ps_step"] = None
ev = []; evo.introduce_v33(S, ev)
G = [v["genome"] for v in evo.active(S)]
check(all(10 <= g["ps_step"] <= 30 or g.get("ps_step") in (10.0, 20.0) for g in G) and ev and ev[0].startswith("🆕"),
      f"every active bot gets a ladder; alert: {ev[0][:80] if ev else None}")
evo.introduce_v33(S, ev); check(len(ev) == 1, "runs once")
print("\nALL PASSED" if not fails else f"\n{len(fails)} FAILED"); sys.exit(bool(fails))
