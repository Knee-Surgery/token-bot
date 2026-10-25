"""Recorder (paths.py) and virtual replay (bench.py). Usage: python test_paths_bench.py <src> <run dir>"""
import asyncio, os, sys, time, json, gzip
from collections import Counter
SRC, RUN = sys.argv[1], sys.argv[2]
os.chdir(RUN); sys.path.insert(0, SRC)
os.environ.update(TELEGRAM_BOT_TOKEN="1:x", EVO_POP="300"); os.environ.pop("NOTION_TOKEN", None)
import evo, auto, paths, bench  # noqa: E402

fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)

now = time.time()
def pair(price, m5=4.0, liq=80000, h1=8, h6=20, b=12, s=6, vol=80000):
    return {"priceUsd": str(price), "liquidity": {"usd": liq}, "priceChange": {"m5": m5, "h1": h1, "h6": h6},
            "txns": {"m5": {"buys": b, "sells": s}}, "volume": {"h1": vol}, "pairCreatedAt": (now - 2 * 86400) * 1000,
            "baseToken": {"symbol": "TST"}}
A = {"holders": 3000, "top10": 25, "top1": 3, "score": 60, "symbol": "TST"}

print("--- recorder")
S = {"variants": {}}
paths.mark(S, "M1", pair(1.0), A, "scan")
paths.mark(S, "M1", pair(1.0), A, "trainee")
check(S["rec_live"]["M1"]["src"] == "scan+trainee" and S["rec_live"]["M1"]["until"] > now + 7000, "mark: followed for 2 h, both sources")
paths.MAX_LIVE = 2
paths.mark(S, "M2", pair(2.0), A, "scan"); paths.mark(S, "M3", pair(3.0), A, "scan")
check(set(S["rec_live"]) == {"M1", "M2"}, "MAX_LIVE caps the coins followed")
S["rec_live"]["M2"]["until"] = now - 1
prices = iter([1.0, 1.1, 1.2])
async def bp(g, mints):
    pr = next(prices)
    return {m: pair(pr) for m in mints}
auto.best_pairs = bp
pc = {}
async def go():
    for _ in range(3):
        await paths.sample({}, S, pc)
        for m in list(pc): pc[m] = (0, pc[m][1])  # force a fresh fetch next sample
        await asyncio.sleep(1.1)
asyncio.run(go())
check("M2" not in S["rec_live"], "expired coin dropped")
seg = paths._seg["M1"]
check(len(seg["rows"]) == 3 and [r[1] for r in seg["rows"]] == [1.0, 1.1, 1.2] and seg["rows"][0][3] == 4.0,
      f"3 rows recorded with price and momentum: {seg['rows']}")
paths.flush()
files = list(paths.DIR.glob("*.jsonl.gz"))
check(len(files) == 1, f"one file for this run: {[f.name for f in files]}")
L = paths.load(1)
check("M1" in L and len(L["M1"]["rows"]) == 3 and abs(L["M1"]["rows"][0][0] - now) < 30, "load(): rows back with absolute times")
old = paths.DIR / (time.strftime("%Y%m%dT%H%M%S", time.gmtime(now - 9 * 86400)) + ".jsonl.gz")
with gzip.open(old, "wt") as f: f.write(json.dumps({"mint": "OLD", "name": "o", "src": "s", "a": {}, "created": None, "t0": round(now - 9 * 86400), "rows": [[0, 1, 1, 1, 1, 1, 1, 1, 1]]}) + "\n")
os.utime(old, None)  # checkout-fresh file date: pruning must go by the name
paths.prune()
check(not old.exists() and files[0].exists(), "prune() deletes files older than 7 days by name, keeps this run's")

print("--- replay")
gn = dict(evo.active(evo.load())[0]["genome"], min_m5=3.0, max_m5=5.0, sl=7.0, ps_at=22.0, min_liq=30000)
t0 = now - 3600
def rows_of(prices, m5=4.0, step=15, start=t0):
    return [[start + i * step, p, 80000, m5, 8, 20, 12, 6, 80000] for i, p in enumerate(prices)]
coin = lambda rows: {"name": "X", "a": A, "created": (now - 2 * 86400) * 1000, "src": "scan", "rows": rows}
# winner: up to +40%, then back down -> partial at +22%, rest trails 10% below the peak
up = [1.0 + 0.02 * i for i in range(21)] + [1.40 - 0.02 * i for i in range(1, 15)]
P = bench.prepare({"W": coin(rows_of(up))})
T = bench.replay(gn, P)
check(len(T) >= 1 and T[0][1] == t0, f"enters at the first row that meets the v3.2 rule ({len(T)} trades)")
# reproduce with evo's own functions
ex = evo.m33_exits(gn)
pos = {"entry_price": 1.0, "entry_liq": 80000, "ts": t0, "sl": ex["sl"], "hold": ex["hold"], "peak_price": 1.0, "last_price": 1.0,
       "lock": None, "ps_frac": ex["ps_frac"], "ps_at": ex["ps_at"], "ps_step": ex["ps_step"], "ps_next": ex["ps_at"], "part": 0.0, "left": 1.0}
why, exp = None, None
for r in rows_of(up)[1:]:
    g_ = (r[1] - 1) * 100
    act = evo.m33_step(pos, r[1], r[2], r[0])
    if act and act[0] == "partial":
        s_ = pos["left"] * act[1]; pos["part"] += s_ * (g_ - evo.COST_PCT); pos["left"] -= s_
    elif act:
        why, exp = act[1], pos["part"] + pos["left"] * (g_ - evo.COST_PCT); break
check(T and abs(T[0][2] - exp) < 1e-9 and T[0][3] == why, f"same result as the live exit code: {T[0][2]:+.2f}% ({T[0][3]}) vs {exp:+.2f}% ({why})")
# loser: straight down -> stop-loss
P = bench.prepare({"L": coin(rows_of([1.0, 0.98, 0.96, 0.93, 0.90]))})
T = bench.replay(gn, P)
check(T and T[0][3] == "stop-loss" and abs(T[0][2] - (-10 - evo.COST_PCT)) < 1e-9, f"stop-loss booked at the row's price: {T}")
# window: +8-15% genome doesn't take a +4% signal
check(not bench.replay(dict(gn, min_m5=8.0, max_m5=15.0), P), "a +8-15% bot ignores a +4% entry signal")
# cooldown: steady signal for 2 h -> trades spaced by >= 30 min
flat = rows_of([1.0 + (0.001 if i % 2 else 0) for i in range(480)])
T = bench.replay(gn, bench.prepare({"F": coin(flat)}))
gaps = [b[1] - a[1] for a, b in zip(T, T[1:])]
check(T and all(x >= evo.COOLDOWN_MIN * 60 for x in gaps), f"one trade at a time per coin + 30 min cooldown ({len(T)} trades, min gap {min(gaps) / 60 if gaps else 0:.0f} min)")
# bad single price row is ignored
spike = rows_of([1.0, 1.0, 5.0, 1.0, 0.99, 1.0])
check(len(bench.clean(spike)) == 5, "a single 50%+ bad price row is dropped")
# gap between runs: sold at the last row before the gap
g_rows = rows_of([1.0, 1.01, 1.02]) + rows_of([0.5, 0.5], start=t0 + 1800)
T = bench.replay(gn, bench.prepare({"G": coin(g_rows)}))
check(T and T[0][3] == "end of recording" and abs(T[0][2] - (2 - evo.COST_PCT)) < 1e-6, f"recording gap: sold at the last row before it {T[:1]}")
# holders rule
c = coin(rows_of(up)); c["a"] = dict(A, holders=100)
check(not bench.replay(gn, bench.prepare({"H": c})), "coins failing the v3.2 holder rule are not entered")

print("--- scoring the population")
S = evo.load()
paths._seg.clear(); paths._seg["W"] = {"mint": "W", "name": "W", "src": "scan", "a": A, "created": (now - 2 * 86400) * 1000, "t0": round(t0), "rows": [[r[0] - round(t0)] + r[1:] for r in rows_of(up)]}
paths._file = None; paths.flush()
t = time.time(); info = bench.score_bots(S); dt = time.time() - t
V = [v for v in evo.active(S) if v.get("virt")]
check(len(V) == len(evo.active(S)) and info["coins"] >= 1, f"every active bot scored ({len(V)}, {info}, {dt:.2f}s)")
lines = bench.digest_lines(S); print("   " + "\n   ".join(lines))
check(lines and "Virtual replay" in lines[0], "hourly summary line")
print("--- real recorded price paths (the bot's own paths/ folder)")
from pathlib import Path  # noqa: E402
real_dir = Path(SRC) / "paths"
if not list(real_dir.glob("*.jsonl.gz")):
    print("   no recorded paths in the repo yet: skipped")
else:
    paths.DIR = real_dir
    R = paths.load(24 * 7)
    P = bench.prepare(R)
    rows_n = sum(len(c["rows"]) for c in P.values())
    print(f"   {len(R)} real coins, {rows_n} rows, {sum(1 for c in P.values() if c['cand'])} with v3.2 entry signals")
    check(len(R) > 0 and rows_n > 0, "real paths load")
    S = evo.load()
    designs = [evo.x1_genome(i) for i in range(len(evo.X1_DESIGN))]
    bad = []
    tot = {"v3.3": [], "v3.2": []}
    for gn in designs:
        for ver in ("v3.3", "v3.2"):
            evo.M33 = ver == "v3.3"
            for c in P.values():
                c["memo"] = {}
            T = bench.replay(gn, P)
            tot[ver] += T
            for m, ts, pct, why in T:
                rows = P[m]["rows"]
                i = next(k for k, r in enumerate(rows) if r[0] == ts)
                if i not in P[m]["cand"] or pct < -100 or not (lo_hi := evo.m31_window(gn))[0] <= (rows[i][3] or 0) <= lo_hi[1]:
                    bad.append((ver, m[:6], ts, pct, why))
                if ver == "v3.3" and why == "rising profit lock" and pct < evo.MIN_TP_NET - 5:
                    bad.append(("v3.3 take-profit under 25%", m[:6], pct))
    evo.M33 = True
    check(not bad, f"on real paths every replayed entry meets the rules, no loss past -100%, v3.3 profit-taking at ~25%+ ({bad[:3]})")
    for ver, T in tot.items():
        if T:
            print(f"   {ver} on real paths, 10 founder designs: {len(T)} trades, {sum(t[2] for t in T) / len(T):+.2f}%/trade, "
                  f"{sum(1 for t in T if t[2] > 0)} won, exits: {dict(Counter(t[3] for t in T).most_common(4))}")
        else:
            print(f"   {ver} on real paths: no entry signals for the founder designs yet")
print("\nALL PASSED" if not fails else f"\n{len(fails)} FAILED"); sys.exit(bool(fails))
