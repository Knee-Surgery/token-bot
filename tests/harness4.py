"""Milestone stages: trigger at 75 consistent bots, spread strategies, proof test pass / fail."""
import os, sys, time, random

SRC = sys.argv[1]
os.chdir(sys.argv[2])
sys.path.insert(0, SRC)
os.environ.update(TELEGRAM_BOT_TOKEN="1:x", EVO_POP="300")
if os.path.exists("evo_state.json"):
    os.remove("evo_state.json")
import evo, menu  # noqa: E402

fails = []
def check(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c: fails.append(m)

def trades(n, tokens, win_usd, loss_usd, wr, ts=None, fluke=0.0):
    out = []
    for i in range(n):
        win = i < int(n * wr)
        out.append({"mint": f"tok{i % tokens}", "pnl_usd": win_usd if win else -loss_usd, "pnl_pct": 10 if win else -5, "win": win,
                    "why": "x", "ts": ts or time.time() - 3600})
    if fluke:
        out[0]["pnl_usd"] += fluke
    return out

random.seed(2)
S = evo.load(); evo.ensure_population(S, [])
vs = list(S["variants"].values())
check(not evo.consistent(trades(20, 10, 0.1, 0.5, 0.5, fluke=10)), "a bot profitable only thanks to one token does not count")
check(not evo.consistent(trades(20, 3, 1.0, 0.2, 0.5)), "too few different tokens does not count")
check(evo.consistent(trades(20, 10, 1.0, 0.3, 0.5)), "20 trades over 10 tokens, profitable without its best token, counts")

for v in vs[:74]:
    v["closed"] = trades(20, 10, 1.0, 0.3, 0.5)
ev = []
evo.milestone(S, ev)
check(S["milestone"]["stage"] == "searching" and S["milestone"]["count"] == 74 and not ev, "74 consistent bots: no trigger yet")
print("menu:", menu.menu_text(S).splitlines()[-1])

vs[74]["closed"] = trades(20, 10, 1.0, 0.3, 0.5)
S["total_pnl"], S["total_trades"] = 100.0, 1500
evo.milestone(S, ev)
M = S["milestone"]
good = {v["id"] for v in vs[:75]}
act = evo.active(S)
kids = [v for v in act if v["id"] not in good]
check(M["stage"] == "proving" and M["hit_count"] == 75, "75th consistent bot triggers the milestone")
check(len(act) == 300 and all(v["parent"] in good for v in kids), f"population refilled to 300 with children of the 75 ({len(kids)} children)")
check(sum(1 for v in S["variants"].values() if v["reason"].startswith("replaced by a consistent")) == 225, "the other 225 bots were replaced")
check(ev and ev[0].startswith("🎯 Goal reached: 75 bots"), "Telegram alert queued: " + ev[0].splitlines()[0])
print("menu:", menu.menu_text(S).splitlines()[-1])

ev.clear()
evo.milestone(S, ev)
check(M["stage"] == "proving" and not ev, "nothing happens before the proof window ends")

# proof window over: 80 bots consistent on fresh trades, population in profit
M["since"] -= evo.PROOF_HOURS * 3600 + 1
fresh_ts = M["since"] + 60
for v in evo.active(S)[:80]:
    v["closed"] += trades(10, 5, 1.0, 0.3, 0.5, ts=fresh_ts)
S["total_pnl"] += 250.0; S["total_trades"] += 800
evo.milestone(S, ev)
check(M["stage"] == "ready" and ev and ev[0].startswith("✅ Proof test passed"), "passing proof test -> ready alert")
check("Still paper only" in ev[0], "ready alert says nothing went live")
print("menu:", menu.menu_text(S).splitlines()[-1])

# failure path
S["milestone"] = {"stage": "proving", "since": time.time() - evo.PROOF_HOURS * 3600 - 1, "pnl_at_start": S["total_pnl"],
                  "trades_at_start": S["total_trades"], "hit_count": 75}
S["total_pnl"] -= 5.0; S["total_trades"] += 300
ev.clear(); evo.milestone(S, ev)
check(S["milestone"]["stage"] == "searching" and ev[0].startswith("❌ Proof test failed"), "losing proof test -> back to searching: " + ev[0][:90])
evo.save(S); S2 = evo.load()
check(S2["milestone"]["stage"] == "searching", "milestone state survives save/load")
print("\nALL PASSED" if not fails else f"\n{len(fails)} FAILED")
