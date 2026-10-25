import asyncio, os, sys, time
SRC, RUN = sys.argv[1], sys.argv[2]
os.chdir(RUN); sys.path.insert(0, SRC)
os.environ.update(TELEGRAM_BOT_TOKEN="1:x", EVO_POP="300"); os.environ.pop("NOTION_TOKEN", None)
import evo, menu
fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)
S = evo.load()
cut = time.time() - 6 * 3600
L = S.get("ledger") or []
start = L[0]["ts"] if L else float("inf")
exp = [t for t in L if t["ts"] >= cut] + [t for v in S["variants"].values() for t in v["closed"] + (v.get("recovery") or []) if cut <= t["ts"] < start]
n, p = menu.recent_trades(S)
check(n == len(exp) and abs(p - sum(t["pnl_usd"] for t in exp)) < 1e-9, f"real state: {n} trades, {menu.usd(p)} (ledger rows {len(L)})")
txt = menu.menu_text(S); print(txt)
check(f"Last 6 h: {n} trades, {menu.usd(p)}" in txt, "menu shows the 6 h line")
# no double counting once the ledger covers the window
now = time.time()
S2 = {"ledger": [{"ts": now - 100, "pnl_usd": 1.0}, {"ts": now - 7 * 3600, "pnl_usd": 5.0}],
      "variants": {"a": {"closed": [{"ts": now - 100, "pnl_usd": 1.0}, {"ts": now - 7 * 3600, "pnl_usd": 5.0}]}}}
check(menu.recent_trades(S2) == (1, 1.0), f"ledger covers window: no double count {menu.recent_trades(S2)}")
S3 = {"ledger": [{"ts": now - 100, "pnl_usd": 1.0}],
      "variants": {"a": {"closed": [{"ts": now - 3 * 3600, "pnl_usd": -2.0}, {"ts": now - 100, "pnl_usd": 1.0}],
                         "recovery": [{"ts": now - 2 * 3600, "pnl_usd": 0.5}]}}}
check(menu.recent_trades(S3) == (3, -0.5), f"before the ledger: bot lists + recovery fill in {menu.recent_trades(S3)}")
class TB:
    def __init__(s): s.sent, s.edits, s.n = [], [], 100
    async def send_message(s, chat, text, **k): s.n += 1; s.sent.append(text); return type("M", (), {"message_id": s.n})()
    async def edit_message_text(s, text, **k): s.edits.append((k["message_id"], text))
    async def delete_message(s, *a): pass
async def go():
    tb, g = TB(), {"OWNER_ID": 1}
    await menu.show_menu(tb, g, S)
    await menu.refresh_menu(tb, g, S)
    check(not tb.edits, "no edit when nothing changed")
    S.setdefault("ledger", []).append({"mint": "x", "name": "X", "bot": "v1", "entry_ts": time.time() - 60, "ts": time.time(), "pnl_usd": 0.42, "pnl_pct": 4, "why": "t"})
    await menu.refresh_menu(tb, g, S)
    check(len(tb.edits) == 1 and tb.edits[0][0] == S["menu_id"] and f"{n + 1} trades" in tb.edits[0][1], "new trade -> menu edited in place with the new count")
    await menu.show_menu(tb, g, S, "📊 Top bots ...", menu.BACK)
    S["ledger"].append(dict(S["ledger"][-1], ts=time.time()))
    await menu.refresh_menu(tb, g, S)
    check(len(tb.edits) == 1, "menu showing another screen is left alone")
asyncio.run(go())
print("\nALL PASSED" if not fails else f"\n{len(fails)} FAILED"); sys.exit(bool(fails))
