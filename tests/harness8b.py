"""Partial sells: genes, A/B rollout, accounting, Notion labels."""
import asyncio, os, sys, time, random

SRC = sys.argv[1]
os.chdir(sys.argv[2])
sys.path.insert(0, SRC)
os.environ.update(TELEGRAM_BOT_TOKEN="1:x", EVO_POP="300")
os.environ.pop("NOTION_TOKEN", None)
if os.path.exists("evo_state.json"):
    os.remove("evo_state.json")
import evo, auto, menu  # noqa: E402

fails = []
def check(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c: fails.append(m)

notion = []
async def fake_create(g, db, props):
    notion.append(("create", db, props)); return "page-1"
async def fake_patch(g, pid, body):
    notion.append(("patch", pid, body))
auto.n_create, auto.n_patch = fake_create, fake_patch

now = time.time()
def pair(price):
    return {"priceUsd": str(price), "liquidity": {"usd": 80000}, "pairCreatedAt": (now - 2 * 86400) * 1000,
            "priceChange": {"m5": 1, "h1": 2}, "volume": {"h1": 60000}, "txns": {}}
A = {"score": 60, "holders": 3000, "top10": 25, "symbol": "TEST"}

def sel(props, k):
    return (props.get(k) or {}).get("select", {}).get("name")
def title(props, k):
    return props[k]["title"][0]["text"]["content"]

async def main():
    random.seed(6)
    check([evo.clamp(dict(evo._BASE, ps_frac=x))["ps_frac"] for x in (0, 0.07, 0.2, 0.4, 0.6, 0.9, 1.3)] == [0, 0.1, 0.25, 0.5, 0.5, 1.0, 1.0],
          "partial-sell size snaps to off / 10 / 25 / 50 / 75 / 100%")
    S = evo.load(); evo.ensure_population(S, [])
    vs = evo.active(S)
    vs[0]["proven"] = True
    ev = []
    evo.introduce_partial_test(S, ev)
    on = [v for v in vs if "partial-sell test" in (v.get("notes") or [])]
    check(140 <= len(on) <= 150 and not vs[0]["genome"]["ps_frac"], f"half the unproven bots get partial selling ({len(on)}), proven untouched")
    check({v["genome"]["ps_frac"] for v in on} <= {0.1, 0.25, 0.5, 0.75, 1.0} and all(5 <= v["genome"]["ps_at"] <= 40 for v in on),
          "sizes are 10/25/50/75/100% at +5-40%")
    print("   mix:", {f"{k:.0%}": sum(1 for v in on if v["genome"]["ps_frac"] == k) for k in (0.1, 0.25, 0.5, 0.75, 1.0)})
    ev2 = []; evo.introduce_partial_test(S, ev2)
    check(ev and not ev2, "rollout happens once: " + ev[0])

    # a 25% partial at +10%, then the rest exits at +30% take-profit
    v = on[0]; v["genome"].update(ps_frac=0.25, ps_at=10, tp=30, sl=4, trail=30, hold=15)
    v["positions"] = {}; v["paper_usd"] = 10.0
    mint = "Mint1111111111111111111111111111111111111111"[:44]
    await evo.open_position({}, v, mint, pair(1.0), A, 2880, 4.0, v["genome"])
    S["total_pnl"] = 0.0
    notion.clear()
    await evo.monitor({}, S, {mint: (time.time(), pair(1.12))}, [], None)  # +12% gross -> partial 25%
    if evo._BG: await asyncio.wait(list(evo._BG))  # partial-sell rows are written in the background, like exits
    pos = v["positions"].get(mint)
    part_pnl = 4.0 * 0.25 * (12 - evo.COST_PCT) / 100
    check(pos and pos["ps_done"] and abs(pos["cost_usd"] - 3.0) < 1e-9, "25% sold at +12%, 75% of the position still open")
    check(abs(v["paper_usd"] - 10.0 - part_pnl) < 1e-9 and abs(S["total_pnl"] - part_pnl) < 1e-9,
          f"partial profit booked at once: +${part_pnl:.3f}")
    rows = [p for kind, db, p in notion if kind == "create"]
    check(any(sel(p, "Exit Reason") == "Partial sell 25%" for p in rows), "Notion Closed Trades row labelled 'Partial sell 25%'")
    check(any(sel(p, "Type") == "Partial sell 25%" and title(p, "Event").startswith("Partial sell 25%") for p in rows),
          "Notion Trade Events row typed 'Partial sell 25%'")
    check(any(kind == "patch" and "Size (Tokens)" in b.get("properties", {}) for kind, _, b in notion), "Open Trades row updated with the size left")
    check(len(v["closed"]) == 0, "the partial is not a separate trade for testing")
    notion.clear()
    await evo.monitor({}, S, {mint: (time.time(), pair(1.31))}, [], None)  # +31% -> take-profit on the rest
    if evo._BG: await asyncio.wait(list(evo._BG))
    rest_pnl = 3.0 * (31 - evo.COST_PCT) / 100
    t = v["closed"][-1] if v["closed"] else {}
    whole = part_pnl + rest_pnl
    check(mint not in v["positions"] and abs(t.get("pnl_usd", 0) - whole) < 1e-9, f"whole trade recorded once: +${whole:.3f}")
    check(abs(t["pnl_pct"] - whole / 4.0 * 100) < 1e-9 and t["partial"] == 0.25, f"tested on the combined result: {t['pnl_pct']:+.2f}%")
    check(abs(S["total_pnl"] - whole) < 1e-9, "overall total = partial + rest, nothing counted twice")
    closed_rows = [p for kind, db, p in notion if kind == "create" and db == auto.DB_CLOSED]
    check(closed_rows and abs(closed_rows[0]["Realised PnL (USD)"]["number"] - rest_pnl) < 1e-9,
          "final Notion row holds only the rest, so Notion's sum matches")

    # 100% option: the whole position goes at the partial level
    v2 = on[1]; v2["genome"].update(ps_frac=1.0, ps_at=8, tp=60); v2["positions"] = {}
    m2 = "Mint2222222222222222222222222222222222222222"[:44]
    await evo.open_position({}, v2, m2, pair(1.0), A, 2880, 3.0, v2["genome"])
    notion.clear()
    await evo.monitor({}, S, {m2: (time.time(), pair(1.09))}, [], None)
    if evo._BG: await asyncio.wait(list(evo._BG))
    rows = [p for kind, db, p in notion if kind == "create"]
    check(m2 not in v2["positions"] and v2["closed"][-1]["why"] == "Partial sell 100%", "100% sells everything at its level")
    check(any(sel(p, "Exit Reason") == "Partial sell 100%" for p in rows) and any(sel(p, "Type") == "Partial sell 100%" for p in rows),
          "labelled 'Partial sell 100%' in both Notion tables")
    rec = await menu.screen({}, S, "recent", {})
    check("✂️25%" in rec, "Telegram recent trades mark partial sells with ✂️")
    print("\nALL PASSED" if not fails else f"\n{len(fails)} FAILED")

asyncio.run(main())
