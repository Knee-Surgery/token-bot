"""Three balanced trainees with one shared brain: migration, shared learning, one trainee per coin, feedback from
real trades, Notion names, summary lines."""
import asyncio, os, sys, time, random

SRC, RUN = sys.argv[1], sys.argv[2]
os.chdir(RUN)
sys.path.insert(0, SRC)
os.environ.update(TELEGRAM_BOT_TOKEN="1:x", EVO_POP="300", NOTION_TOKEN="test")
import evo, auto, trainee as tn  # noqa: E402

fails = []
def check(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c: fails.append(m)

created, patched = [], []
async def fake_create(g, db, props): created.append(props); return f"page{len(created)}"
async def fake_patch(g, pid, body): patched.append((pid, body))
auto.n_create, auto.n_patch = fake_create, fake_patch
async def no_prefetch(g, pc, m, ttl): return None
evo.prefetch = no_prefetch
async def same(g, m, p, ref): return p
evo.checked_price = same
evo.rug_ok = lambda a, p: (True, "")

async def main():
    now = time.time()
    # --- 1. migration from the three-personality state saved by a run
    w1 = dict(tn.PRIOR, has_tg=0.9)
    S = {"acache": {}, "halted": False, "variants": {},
         "trainee_lab": {"shadows": {}, "done": [], "seen": {}, "n_research": 20},
         "trainees": {"t1": {"usd": 7.0, "pnl": -0.5, "pos": None, "closed": [{"pnl_usd": -0.5}], "w": w1, "arm": 5, "thr": 0.45, "rugs_avoided": 2, "best_val": 1.0},
                      "t2": {"usd": 10.0, "pnl": 0.0, "pos": None, "closed": [], "w": dict(tn.PRIOR, has_x=2.0), "arm": 3, "thr": 0.7, "rugs_avoided": 5, "best_val": None},
                      "t3": {"usd": 9.0, "pnl": -1.0, "pos": None, "closed": [{"pnl_usd": -1.0}], "w": dict(tn.PRIOR), "arm": 7, "thr": 0.35, "rugs_avoided": 1, "best_val": None}}}
    TT, B = tn.trainees(S), tn.brain(S)
    check(B["w"]["has_tg"] == 0.9 and B["thr"] == 0.45 and B["arm"] == tn.DEFAULT[0],
          "shared brain starts from Trainee 1's model and score bar (plan reset: the plans changed)")
    check(B["rugs_avoided"] == 8, "rugs-avoided counts added together")
    check(all(set(T) == {"usd", "pnl", "pos", "closed"} for T in TT.values()), "each trainee keeps only its own money and trades")
    check(TT["t1"]["usd"] == 7.0 and TT["t3"]["pnl"] == -1.0, "money and P&L untouched")

    # --- 2. shared learning: one update per shadow trade
    random.seed(1)
    w_before = dict(B["w"])
    for k in range(20):
        good = k % 2 == 0
        fv = dict.fromkeys(tn.FEATS, 0.0); fv.update(bias=1.0, tg_size=0.8 if good else 0.1, cluster_share=0.0 if good else 0.9)
        sh = {"ts": now, "f": tn.vec(fv), "flags": [], "score": 0.6, "last": 1.0, "peak": 1.0, "liq": 50000,
              "legs": [[1.0, 1 - tn.FEE / 100, 0.0, False, False, None, None] for _ in tn.ARMS]}
        for i, pr in enumerate([1.05, 1.12, 1.2, 1.4, 1.3] if good else [0.97, 0.93, 0.88]):
            tn.simulate_tick(sh, pr, 50000, now + 30 * (i + 1), good)
        tn.simulate_tick(sh, 1.0, 50000, now + 3600, good)
        tn.finish_shadow(S, f"m{k}", sh, now)
    check(B["w"] != w_before and tn.score(B["w"], tn.lab(S)["done"][0]["f"]) > tn.score(B["w"], tn.lab(S)["done"][1]["f"]),
          "one model learns from the shared shadow trades")

    # --- 3. one trainee per coin, spread over different coins
    good = dict.fromkeys(tn.FEATS, 0.0); good.update(bias=1.0, has_x=1.0, has_tg=1.0, tg_size=0.9, whale_net=0.6, buy_ratio=0.5)
    async def research(g, m, p): return {"f": good, "flags": [], "summary": "X yes"}
    tn.research = research
    B["thr"] = 0.3
    coins = [f"C{i}" + "1" * 42 for i in range(4)]
    def pair(pr="1.0"): return {"priceUsd": pr, "liquidity": {"usd": 80000}, "pairCreatedAt": (now - 2 * 86400) * 1000,
                                "txns": {"m5": {"buys": 10, "sells": 5}}, "priceChange": {"m5": 3, "h1": 5}}
    async def get_pair(g, m, pc, ttl): return pair()
    evo.get_pair = get_pair
    for m in coins:
        S["acache"][m] = {"ts": now, "a": {"score": 60, "holders": 3000, "top10": 25, "symbol": m[:2]}}
    g = {"http": None, "tr_cands_ts": now, "tr_cands": coins}
    for _ in range(4):
        await tn.research_next(g, S, {})
    held = {tid: (T["pos"] or {}).get("mint") for tid, T in TT.items()}
    check(len({m for m in held.values() if m}) == 3 and sum(1 for m in held.values() if m) == 3,
          f"three coins in a row went to three different trainees: {held}")
    check(held["t2"] == coins[0], "the least experienced free trainee goes first (Trainee 2, no trades yet)")
    ent = [p for p in created if p["Type"]["select"]["name"] == "Entry"]
    check(len(ent) == 3 and {p["Trainee"]["select"]["name"] for p in ent} == set(tn.NAMES.values()),
          "Notion rows tagged Trainee 1/2/3 Â· Balanced")
    check(all(T["pos"]["arm"] == B["arm"] for T in TT.values()), "all three use the shared plan")

    # --- 4. feedback: real trades closing teach the shared model
    fb, w0 = B.get("feedback", 0), dict(B["w"])
    pcs = {T["pos"]["mint"]: (time.time(), pair("0.80")) for T in TT.values()}
    await tn.tick(g, S, pcs)
    check(all(T["pos"] is None for T in TT.values()) and B["feedback"] == fb + 3, "three losing trades fed back into the brain")
    check(tn.score(B["w"], good) < tn.score(w0, good), "after three losses on this kind of coin the brain scores it lower")

    lines = tn.digest_lines(S, {"tr": tn.snapshot(S)})
    print("   " + "\n   ".join(lines))
    check(lines[-1].startswith("🎓 Shared brain") and "3 real trades" in lines[-1], "summary shows the shared brain")
    print("\nALL PASSED" if not fails else f"\n{len(fails)} FAILED")

asyncio.run(main())

