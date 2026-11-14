import asyncio, json, os, sys, time
SRC, RUN = sys.argv[1], sys.argv[2]
os.chdir(RUN); sys.path.insert(0, SRC)
os.environ.update(TELEGRAM_BOT_TOKEN="1:x", EVO_POP="300", HERMES_IDEAS_REPO="o/r"); os.environ.pop("NOTION_TOKEN", None)
import auto, evo, menu, hermes_ideas as H
from datetime import datetime, timezone
fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)
iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).isoformat()
M1, M2, M3 = "So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
def idea(i, mint=M1, **k):
    d = {"id": i, "mint": mint, "side": "buy", "size_usd": 5, "tp_pct": 40, "sl_pct": 10, "max_hold_min": 30,
         "confidence": 0.8, "reason": "test", "created": iso(time.time())}
    d.update(k); return d
class TB:
    def __init__(s): s.sent, s.n = [], 100
    async def send_message(s, chat, text, **k): s.n += 1; s.sent.append((text, k.get("reply_markup"))); return type("M", (), {"message_id": s.n})()
    async def edit_message_text(s, text, **k): pass
    async def delete_message(s, *a): pass
class Q:
    def __init__(s, data): s.data = data; s.from_user = type("U", (), {"id": 1})()
    async def answer(s): pass
feed, prices, rug = [], {}, {}
async def fake_fetch(g): return feed
async def fake_safety(g, mint):
    ok, why = rug.get(mint, (True, ""))
    return ok, why, {"score": 70, "holders": 500, "top10": 20, "symbol": "T" + mint[:3]}, {"priceUsd": str(prices[mint][0])}
async def fake_px(g, mint): return prices.get(mint, (None, 0.0))
H.fetch_ideas, H.safety, auto.px = fake_fetch, fake_safety, fake_px

# format checks
now = time.time()
check(H.validate(idea("a"), now)[0] is not None, "valid idea accepted")
check(H.validate(idea("a", mint="bad"), now)[0] is None, "bad mint refused")
check(H.validate(idea("a", side="sell"), now)[0] is None, "sells refused")
check(H.validate(idea("a", created=iso(now - 3600)), now)[0] is None, "stale idea refused")
check(H.validate(idea("a", confidence="x"), now)[0] is None, "non-numeric confidence refused")
check(H.validate(idea("a", tp_pct=9999, sl_pct=0.1), now)[0]["tp_pct"] == 300, "TP clamped to the limit")
check(H.validate({"id": "a; rm -rf", "mint": M1}, now)[0] is None, "odd id refused")

async def go():
    S = {"variants": {}, "halted": False}
    tb, g, pc = TB(), {"OWNER_ID": 1}, {}
    h = H.state(S)
    prices.update({M1: (1.0, 50000.0), M2: (2.0, 50000.0), M3: (3.0, 50000.0)})
    rug[M2] = (False, "top 10 hold 85%")
    feed[:] = [idea("i1"), idea("i2", mint=M2), idea("i3", mint=M3, confidence=0.4), {"junk": 1}]
    await H.check_new(tb, g, S, pc)
    check(list(h["pending"]) == ["i1"], "only the idea passing every check waits for approval")
    check(h["pending"]["i1"]["size"] == 1.0, "size capped at 10% of the $10 paper bankroll although Hermes asked for 5")
    why = {s["id"]: s["why"] for s in h["skipped"]}
    check("rug check" in why.get("i2", "") and "confidence" in why.get("i3", ""), "skips recorded with reasons")
    check(sum("Hermes idea" in t for t, _ in tb.sent) == 1 and tb.sent[-1][1] is not None, "one approval message with buttons")
    await H.check_new(tb, g, S, pc)
    check(len(h["pending"]) == 1 and sum("Hermes idea" in t for t, _ in tb.sent) == 1, "same ideas are not sent twice")
    check(not h["positions"], "nothing opens without approval")
    # approval through the real menu handler
    u = type("U", (), {"callback_query": Q("h:ok:i1"), "message": None, "update_id": 1})()
    await menu.handle_update(tb, g, S, pc, u)
    check("i1" in h["positions"] and not h["pending"], "Approve opens the paper position")
    # take-profit exit
    prices[M1] = (1.5, 50000.0)
    await H.monitor(tb, g, S)
    check(not h["positions"] and h["closed"][-1]["exit_reason"] == "take-profit" and abs(h["closed"][-1]["pnl_usd"] - 0.5) < 1e-6, "take-profit closes and books +50%")
    check(abs(h["equity"] - 10.5) < 1e-6 and list(h["days"].values())[0]["trades"] == 1, "paper bankroll and the day's record updated")
    # rejection, and price that ran
    feed[:] = [idea("i4", mint=M3), idea("i5")]
    await H.check_new(tb, g, S, pc)
    await H.on_button(tb, g, S, pc, Q("h:no:i4"))
    check(any(s["id"] == "i4" and s["why"] == "owner rejected" for s in h["skipped"]), "Reject is recorded for Hermes")
    prices[M1] = (1.5 * 1.3, 50000.0)
    await H.on_button(tb, g, S, pc, Q("h:ok:i5"))
    check("i5" not in h["positions"], "price that ran since the check is not chased")
    # unapproved ideas expire
    prices[M1] = (1.0, 50000.0)
    feed[:] = [idea("i6")]
    await H.check_new(tb, g, S, pc)
    H.expire_pending(h, time.time() + H.APPROVE_MIN * 60 + 1)
    check(not h["pending"], "idea not approved in time is dropped")
    # stop-loss, trailing stop, max hold
    def pos(i, **k):
        d = {"id": i, "mint": M1, "name": "T", "reason": "", "confidence": 0.8, "tp_pct": 40, "sl_pct": 10, "max_hold_min": 30,
             "size": 1.0, "a": {}, "entry_price": 1.0, "entry_liq": 50000.0, "ts": time.time(), "peak": 1.0, "page_id": None}
        d.update(k); return d
    t = time.time()
    check(H.exit_reason(pos("p"), 0.89, 50000, t) == "stop-loss", "stop-loss")
    check(H.exit_reason(pos("p", peak=1.3), 1.16, 50000, t) == "trailing stop", "trailing stop after a run")
    check(H.exit_reason(pos("p", peak=1.12), 1.0, 50000, t) == "trailing stop", "trailing stop never below break-even")
    check(H.exit_reason(pos("p"), 1.05, 50000, t + 31 * 60) == "max hold", "max hold")
    check(H.exit_reason(pos("p"), 1.05, 10000, t) == "liquidity collapse", "liquidity collapse")
    check(H.exit_reason(pos("p"), 1.05, 50000, t) is None, "otherwise holds")
    # trust adapts: five losers raise the bar, cut the size and pause
    h["closed"] = [{"pnl_usd": -0.7, "pnl_pct": -70, "confidence": 0.8} for _ in range(5)]
    note = H.adapt(h)
    tr = h["trust"]
    check(note and tr["min_conf"] == 0.65 and tr["size_mult"] == 0.75 and tr["paused"], f"losing streak: {note}")
    feed[:] = [idea("i7")]
    await H.check_new(tb, g, S, pc)
    check(not h["pending"] and "paused" in h["skipped"][-1]["why"], "paused: new ideas are skipped")
    await H.on_button(tb, g, S, pc, Q("h:resume:-"))
    check(not tr["paused"], "owner can resume")
    h["closed"] = [{"pnl_usd": 0.3, "pnl_pct": 30, "confidence": 0.8} for _ in range(6)]
    H.adapt(h)
    check(tr["min_conf"] == 0.63 and tr["size_mult"] == 0.825, "winning run: bar eases, size grows")
    for _ in range(40):
        H.adapt(h)
    check(tr["size_mult"] == 1.0 and tr["min_conf"] == 0.5 and H.size_for(h, {"size_usd": 99}) == round(h["equity"] * H.MAX_FRAC, 2), "trust is bounded; 10% of the bankroll stays the hard ceiling")
    # halted bot takes no ideas
    S["halted"] = True; feed[:] = [idea("i8")]
    await H.check_new(tb, g, S, pc)
    check(not h["pending"], "paused bot takes no Hermes ideas")
    # daily cap
    S["halted"] = False; h["sent_today"] = H.DAILY_MAX; feed[:] = [idea("i9")]
    await H.check_new(tb, g, S, pc)
    check(not h["pending"] and "daily limit" in h["skipped"][-1]["why"], "daily cap")
    H.write_results(S)
    r = json.load(open(H.RESULTS_FILE))
    check(r["trust"]["min_conf"] == 0.5 and r["skipped"] and "by_confidence" in r and r["mode"] == "PAPER ONLY" and r["goal"]["target_pct_per_day"] == 8, "results file for Hermes written")
    # goal: 7 finished days averaging 8%+ with enough trades
    from datetime import date, timedelta
    h2 = H.state({})
    check(not H.goal(h2)["met"], "goal not met with no history")
    for i in range(1, 8):
        h2["days"][(date(2026, 11, 20) - timedelta(days=i)).isoformat()] = {"start": 10.0, "pnl": 0.9, "trades": 5}
    gl = H.goal(h2, "2026-11-20")
    check(gl["met"] and gl["avg_pct"] == 9.0 and gl["days_at_target"] == 7, f"goal met: {gl['avg_pct']}% over {gl['days']} days")
    h2["days"]["2026-11-16"]["pnl"] = -3.0
    check(not H.goal(h2, "2026-11-20")["met"], "one bad day pulls the average under the goal")
    h2["days"]["2026-11-16"]["pnl"] = 0.9; del h2["days"]["2026-11-15"]
    check(H.goal(h2, "2026-11-20")["avg_pct"] < 8, "a day without trades counts as 0%")
    # strategies are scored separately; untrusted text is cleaned
    bs = H.by_strategy([{"strategy": "a", "pnl_usd": 1, "pnl_pct": 10}, {"strategy": "b", "pnl_usd": -1, "pnl_pct": -10}, {"pnl_usd": 0.5, "pnl_pct": 5}])
    check([x["strategy"] for x in bs] == ["a", "unlabelled", "b"], "results per strategy, best first")
    v = H.validate(idea("z", strategy="Launch Snipe!!", reason="ok\u202e\x07 hidden"), time.time())[0]
    check(v["strategy"] == "unlabelled" and "\u202e" not in v["reason"] and "\x07" not in v["reason"], "odd strategy label and control characters cleaned")
    check(H.validate(idea("a:b"), time.time())[0] is None, "id with ':' refused")
    # paper only: off when the bot is not in paper mode, and no trading code in the module
    os.environ["DRY_RUN"] = "false"
    check(not H.enabled(), "module is off when DRY_RUN=false")
    os.environ["DRY_RUN"] = "true"
    check(H.enabled(), "on in paper mode")
    src = open(os.path.join(SRC, "hermes_ideas.py")).read()
    check(not any(w in src for w in ("execute_swap", "Keypair", "sell_all", "solders", "quote(", "token_balance")), "no swap, signing or wallet calls in the module")
asyncio.run(go())
print("\nALL PASSED" if not fails else f"\n{len(fails)} FAILED"); sys.exit(bool(fails))
