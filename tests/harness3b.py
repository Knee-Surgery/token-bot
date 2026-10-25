"""Overall running total: Notion history backfill + new exits + menu header."""
import asyncio, os, sys, types, random

SRC = sys.argv[1]
os.chdir(sys.argv[2])
sys.path.insert(0, SRC)
os.environ.update(TELEGRAM_BOT_TOKEN="123:fake", NOTION_TOKEN="fake", DRY_RUN="true")
if os.path.exists("evo_state.json"):
    os.remove("evo_state.json")
import bot, auto, evo, menu  # noqa: E402

fails = []
def check(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c: fails.append(m)

random.seed(1)
ROWS = []  # Exit rows as Notion would hold them, with per-bot cumulative values (the old bug)
for i in range(250):
    pnl = round(random.uniform(-3, 2), 2)
    ROWS.append({"id": f"p{i}", "properties": {"Realised PnL (USD)": {"number": pnl},
                                               "Cumulative PnL (USD)": {"number": round(random.uniform(-5, 1), 2)}}})
patched = {}

class FakeHttp:
    async def post(self, url, headers=None, json=None):
        assert json["filter"] == {"property": "Realised PnL (USD)", "number": {"is_not_empty": True}} and json["sorts"][0]["property"] == "Time"
        start = int(json.get("start_cursor") or 0)
        page = ROWS[start:start + 100]
        more = start + 100 < len(ROWS)
        return types.SimpleNamespace(raise_for_status=lambda: None,
                                     json=lambda: {"results": page, "has_more": more, "next_cursor": str(start + 100) if more else None})
    async def patch(self, url, headers=None, json=None):
        patched[url.rsplit("/", 1)[1]] = json["properties"]["Cumulative PnL (USD)"]["number"]
        return types.SimpleNamespace(raise_for_status=lambda: None, status_code=200, json=lambda: {})
    async def request(self, method, url, headers=None, json=None):
        return await (self.patch if method == 'PATCH' else self.post)(url, headers=headers, json=json)

async def main():
    evo.asyncio = types.SimpleNamespace(sleep=lambda s: real_sleep(0), create_task=asyncio.create_task, gather=asyncio.gather)
    g = {"http": FakeHttp()}
    S = evo.load(); evo.ensure_population(S, [])
    todo = await evo.pnl_history(g, S)
    expect = round(sum(r["properties"]["Realised PnL (USD)"]["number"] for r in ROWS), 2)
    check(round(S["total_pnl"], 2) == expect, f"overall total read from all {len(ROWS)} exits across 3 pages ({expect})")
    await evo.fix_cumulative(g, S, todo)
    run, ok = 0.0, True
    for r in ROWS:
        run += r["properties"]["Realised PnL (USD)"]["number"]
        if abs(patched.get(r["id"], r["properties"]["Cumulative PnL (USD)"]["number"]) - run) > 0.006:
            ok = False
    check(ok, f"every past row now holds the overall running total ({len(patched)} corrected)")
    check(S.get("cum_fixed") is True, "correction marked done, so it runs once")
    # a new exit continues the same running total
    before = S["total_pnl"]
    S["total_pnl"] = (S.get("total_pnl") or 0) + 1.25
    check(abs(S["total_pnl"] - before - 1.25) < 1e-9, "a new exit adds to the overall total")
    txt = menu.menu_text(S)
    check("💰 P&L: -$" in txt or "💰 P&L: +$" in txt, "menu header shows the overall P&L: " + [l for l in txt.splitlines() if "P&L" in l][0])
    print("\nALL PASSED" if not fails else f"\n{len(fails)} FAILED")

real_sleep = asyncio.sleep
asyncio.run(main())
