"""Tests for the Codex-review fixes 1-3 (+ ledger/entry_ts). Usage: python test_review_fixes.py <src> <run dir>"""
import asyncio, os, sys, time, random, json
SRC, RUN = sys.argv[1], sys.argv[2]
os.chdir(RUN); sys.path.insert(0, SRC)
os.environ.update(TELEGRAM_BOT_TOKEN="1:x", EVO_POP="300", DRY_RUN="true"); os.environ.pop("NOTION_TOKEN", None)
import bot, evo, auto, trainee  # noqa: E402

fails = []
def check(c, msg):
    print(("PASS " if c else "FAIL ") + msg)
    c or fails.append(msg)

now = time.time()
PAIR = {"chainId": "solana", "priceUsd": "1.0", "liquidity": {"usd": 80000}, "volume": {"h24": 400000, "h1": 60000},
        "priceChange": {"m5": 4, "h1": 8, "h6": 20, "h24": 30}, "txns": {"h24": {"buys": 900, "sells": 800},
        "h1": {"buys": 60, "sells": 40}, "m5": {"buys": 12, "sells": 6}}, "pairCreatedAt": (now - 2 * 86400) * 1000,
        "fdv": 2e6, "baseToken": {"symbol": "TST"}, "quoteToken": {"symbol": "SOL"}, "dexId": "raydium"}

class R:
    status_code = 200
    def json(self): return {"pairs": [PAIR]}

async def fake_get(url, **k): return R()
bot.http.get = fake_get

async def analyze_with(rep):
    async def rc(m): return rep
    bot._rugcheck = rc
    return await bot.analyze("T" * 44)

def as_a(full):  # what evo.scan builds from analyze()
    a = {k: full.get(k) for k in ("score", "liq", "holders", "top10", "symbol")}
    a["danger"] = False
    rc = full.get("rc") or {}
    for k in ("mint_auth", "freeze_auth", "rugged", "top1", "insider_pct", "creator_pct", "lp_locked", "fee_pct", "risks"):
        if rc.get(k) is not None:
            a[k] = rc[k]
    return a

async def t1():
    print("--- 1. safety data fails closed")
    full = await analyze_with({"totalHolders": 3000, "markets": [], "risks": []})  # no topHolders, no authority keys
    rc = full["rc"]
    check(full["top10"] is None and rc["top1"] is None and rc["insider_pct"] is None and rc["creator_pct"] is None,
          f"no topHolders -> top10/top1/insider/creator None ({full['top10']}, {rc['top1']}, {rc['insider_pct']}, {rc['creator_pct']})")
    check(rc["mint_auth"] is None and rc["freeze_auth"] is None, "authority keys missing -> mint_auth/freeze_auth None")
    ok, why = evo.rug_ok(as_a(full), PAIR)
    check(not ok and why == "safety data unavailable", f"rug_ok blocks: {why!r}")
    full = await analyze_with({"totalHolders": 3000, "markets": [], "risks": [], "topHolders": [],
                               "mintAuthority": None, "freezeAuthority": None})
    check(full["top10"] is None, "empty topHolders list -> top10 None")
    check(full["rc"]["mint_auth"] is False and full["rc"]["freeze_auth"] is False, "authority keys present but null -> False (revoked)")
    holders = [{"address": f"w{i}", "pct": 2.0, "insider": i == 0} for i in range(12)]
    full = await analyze_with({"totalHolders": 3000, "markets": [], "risks": [], "topHolders": holders,
                               "mintAuthority": None, "freezeAuthority": None, "creator": "w1"})
    rc = full["rc"]
    check(full["top10"] == 20.0 and rc["top1"] == 2.0 and rc["insider_pct"] == 2.0 and rc["creator_pct"] == 2.0,
          f"full data -> real numbers ({full['top10']}, {rc['top1']}, {rc['insider_pct']}, {rc['creator_pct']})")
    ok, why = evo.rug_ok(as_a(full), PAIR)
    check(ok, f"full safe data passes rug_ok ({why!r})")
    for k in ("top10", "mint_auth", "freeze_auth"):
        a = as_a(full); a.pop(k, None); a["top10" if k == "top10" else k] = None if k == "top10" else a.get(k)
        ok, why = evo.rug_ok(a, PAIR)
        check(not ok and why == "safety data unavailable", f"{k} None -> blocked ({why!r})")
    full = await analyze_with({"totalHolders": 3000, "markets": [], "risks": [], "topHolders": holders,
                               "mintAuthority": "Auth111", "freezeAuthority": None})
    check(full["rc"]["mint_auth"] is True and not evo.rug_ok(as_a(full), PAIR)[0], "mint authority set -> True, blocked")
    # acache retry when safety data missing
    S = evo.load()
    M = "Q" * 44
    S["acache"].pop(M, None)
    async def an(m): return await analyze_with({"totalHolders": 3000, "markets": [], "risks": []})
    async def disc(g, age=0): return [M]
    auto.discover = disc
    for v in evo.active(S): v["positions"] = {}; v["cooldown"] = {}; v["genome"]["min_m5"], v["genome"]["max_m5"] = 3, 5
    await evo.scan({"http": None, "analyze": an}, S, {M: (time.time(), PAIR)}, [])
    ac = S["acache"].get(M)
    check(ac is not None and time.time() - ac["ts"] > evo.ACACHE_TTL_MIN * 60 - 130, "missing safety data -> cached for a ~2 min retry only")
    check(not any(M in v["positions"] for v in S["variants"].values()), "no bot entered a coin with missing safety data")
    check(S["rejects"].get("safety data unavailable", 0) >= 1, "rejection counted as 'safety data unavailable'")

def t2():
    print("--- 2. A/B test: inheritance, grouping by m5_arm and entry time")
    S = evo.load()
    random.seed(1)
    par = next(v for v in evo.active(S) if v.get("m5_arm") == "+8-15%")
    for _ in range(30):
        gn, notes = evo.mutate(par, S)
        ch = evo.new_variant(S, gn, par["gen"] + 1, par["id"], notes)
        lo, hi, _ = evo.m31_window(ch["genome"])
        if ch.get("m5_arm") != "+8-15%" or not (8 <= lo and hi <= 15):
            break
    check(ch.get("m5_arm") == "+8-15%", f"child of a +8-15% bot inherits the label ({ch.get('m5_arm')})")
    check(8 <= lo < hi <= 15, f"child's 5 m window stays inside its group after mutation ({lo:g}-{hi:g})")
    par2 = next(v for v in evo.active(S) if v.get("m5_arm") == "+3-5%")
    ch2 = evo.new_variant(S, evo.mutate(par2, S)[0], par2["gen"] + 1, par2["id"], [])
    lo2, hi2, _ = evo.m31_window(ch2["genome"])
    check(ch2.get("m5_arm") == "+3-5%" and 3 <= lo2 < hi2 <= 5, f"child of a +3-5% bot: label and window ({lo2:g}-{hi2:g})")
    orphan = evo.new_variant(S, evo.random_genome(), 0, None, ["random"])
    check("m5_arm" not in orphan, "bot without a labelled parent gets no label")
    # grow() children
    S["last_grow"] = 0
    evo.GROW_MAX, evo.GROW_PER_HOUR = 10 ** 6, 20  # past the live population ceiling, for the test only
    n0 = S["next_id"]
    evo.grow(S, [])
    kids = [S["variants"][f"v{i}"] for i in range(n0, S["next_id"]) if f"v{i}" in S["variants"]]
    check(kids and all(k.get("m5_arm") == S["variants"][k["parent"]].get("m5_arm") for k in kids),
          f"grow(): {len(kids)} children all carry their parent's label")
    # ab_lines groups by label and entry time
    T = {"since": S["m32"]}
    A = S["variants"][par["id"]]; B = S["variants"][par2["id"]]
    for v in S["variants"].values():  # retired bots too: only the test trades below may count
        v["closed"] = [t for t in v["closed"] if t["ts"] < S["m32"]]
    A["genome"]["min_m5"] = 3.0  # label wins over the genome value
    t0 = S["m32"] + 1000
    base = {"pnl_pct": 1, "win": True, "why": "x", "feat": {}, "name": "X", "partial": None}
    # three exits on one coin spread over 10 min, but one entry time -> one decision
    A["closed"] += [dict(base, mint="M1", pnl_usd=0.5, ts=t0 + d, entry_ts=t0 - 300) for d in (0, 240, 600)]
    B["closed"] += [dict(base, mint="M1", pnl_usd=-0.2, ts=t0 + d, entry_ts=t0 - 300 + d * 0 + i * 600) for i, d in enumerate((0, 5))]
    out = evo.ab_lines(S, lambda x: f"{x:+.2f}")
    print("   " + "\n   ".join(out))
    a_line = next((l for l in out if "+8-15%" in l), "")
    b_line = next((l for l in out if "+3-5%" in l), "")
    check("1 decisions (3 trades)" in a_line, "+8-15% bot counted in its labelled group; 3 exits of one entry = 1 decision")
    check("2 decisions (2 trades)" in b_line, "two entries 10 min apart = 2 decisions even if they exit together")

def t3():
    print("--- 3. trainee: take-profit before the strength/dip add")
    arm = {"tp": 8, "sl": 5, "add": "strength", "at_tp": "all"}
    L = trainee.new_leg(1.0, 1.0, 50000, now)
    acts = trainee.step(L, arm, 1.20, 50000, now, True, True)
    check(acts == [("exit", "take-profit")] and not L["added"], f"TP tick with buyers leading: {acts}")
    L = trainee.new_leg(1.0, 1.0, 50000, now)
    acts = trainee.step(L, dict(arm, at_tp="half"), 1.20, 50000, now, True, True)
    check(acts == [("partial", 0.5)] and not L["added"], f"TP (half) tick: {acts}")
    L = trainee.new_leg(1.0, 1.0, 50000, now)
    acts = trainee.step(L, dict(arm, tp=45), 1.10, 50000, now, True, True)
    check(acts and acts[0][0] == "add" and L["added"], f"below TP the strength add still happens: {acts}")
    L = trainee.new_leg(1.0, 1.0, 50000, now)
    acts = trainee.step(L, dict(arm, tp=45, add="dip"), 0.97, 50000, now, False, True)
    check(acts and acts[0][0] == "add", f"dip add unaffected: {acts}")

def t4():
    print("--- 4. ledger + entry_ts on closed trades")
    S = evo.load()
    v = evo.active(S)[0]; v["positions"] = {}
    M = "L" * 44
    ts0 = now - 120
    v["positions"][M] = {"name": "LED", "entry_price": 1.0, "entry_liq": 80000, "ts": ts0, "cost_usd": 1.0, "size_tokens": 1.0,
                         "score": 50, "holders": 3000, "top10": 20, "tp": 1000, "sl": 7, "hold": 30, "trail": 10,
                         "page_id": None, "lock_pull": 3, "stall": 8, "feat": {}, "peak_price": 1.0, "last_price": 1.0,
                         "recovery": False, "ps_frac": 0, "ps_at": 0, "orig_cost": 1.0, "realized_usd": 0.0, "m31": True, "lock": None}
    n0 = len(S.get("ledger") or [])
    p = dict(PAIR, priceUsd="0.85")
    async def go():
        async def cp(g, m, price, ref): return price
        evo.checked_price = cp
        await evo.monitor({"http": None}, S, {M: (time.time(), p)}, [], [])
    asyncio.run(go())
    rec = v["closed"][-1]
    check(rec["mint"] == M and rec.get("entry_ts") == ts0, "closed record carries entry_ts")
    L = S.get("ledger") or []
    check(len(L) == n0 + 1 and L[-1]["bot"] == v["id"] and L[-1]["entry_ts"] == ts0 and abs(L[-1]["pnl_usd"] - rec["pnl_usd"]) < 1e-9
          and set(L[-1]) >= {"mint", "name", "bot", "entry_ts", "ts", "pnl_usd", "pnl_pct", "why"}, f"ledger row: {L[-1] if L else None}")
    for _ in range(3):
        evo.save(S)
    S2 = json.load(open("evo_state.json"))
    check(len(S2["ledger"]) == n0 + 1, "ledger survives save() untrimmed")

asyncio.run(t1()); t2(); t3(); t4()
print("\nALL PASSED" if not fails else f"\n{len(fails)} FAILED")
sys.exit(1 if fails else 0)
