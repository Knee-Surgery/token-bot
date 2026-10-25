"""Chat housekeeping + family tree tests with a fake Telegram chat and a fake Notion API."""
import asyncio, os, sys, time, types, logging, uuid, json

SRC = sys.argv[1]
os.chdir(sys.argv[2])
sys.path.insert(0, SRC)
os.environ.update(TELEGRAM_BOT_TOKEN="123:fake", EVO_MODE="true", MIN_HOLDERS="50", MAX_TOP10_PCT="80",
                  DRY_RUN="true", TG_KEEP_HOURS=str(20 / 3600), NOTION_TOKEN="fake")
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s")
if os.path.exists("evo_state.json"):
    os.remove("evo_state.json")

import bot, auto, evo, menu, tree  # noqa: E402
import fakenet  # noqa: E402
fakenet.install(bot)  # offline: canned market, no internet

OWNER = bot.OWNER_ID
fails = []
def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond: fails.append(msg)


class Chat:
    """Fake Telegram chat: an ordered list of live messages."""
    def __init__(self): self.msgs = {}; self.next = 100; self.q = asyncio.Queue(); self.uid = 0; self.notif = []
    def last(self): return max(self.msgs) if self.msgs else None
    async def send_message(self, chat_id, text, reply_markup=None, disable_notification=None):
        self.next += 1; mid = self.next
        self.msgs[mid] = ("bot", text.splitlines()[0][:40], reply_markup); self.notif.append(disable_notification)
        return types.SimpleNamespace(message_id=mid)
    async def delete_message(self, chat_id, mid): self.msgs.pop(mid, None); return True
    async def delete_messages(self, chat_id, ids):
        for i in ids: self.msgs.pop(i, None)
        return True
    async def send_chat_action(self, chat_id, action): return True
    async def get_updates(self, offset=None, timeout=0, allowed_updates=None):
        try: return [await asyncio.wait_for(self.q.get(), timeout)]
        except asyncio.TimeoutError: return []
    # owner actions
    def user_text(self, text):
        self.next += 1; self.uid += 1
        self.msgs[self.next] = ("user", text, None)
        m = types.SimpleNamespace(message_id=self.next, from_user=types.SimpleNamespace(id=OWNER), text=text)
        self.q.put_nowait(types.SimpleNamespace(update_id=self.uid, callback_query=None, message=m))
    def tap(self, mid, data):
        self.uid += 1
        chat = self
        class Q:
            from_user = types.SimpleNamespace(id=OWNER); message = types.SimpleNamespace(message_id=mid)
            async def answer(self): pass
            async def edit_message_text(self, text, reply_markup=None):
                if mid not in chat.msgs: raise RuntimeError("message to edit not found")
                chat.msgs[mid] = ("bot", text.splitlines()[0][:40], reply_markup)
        q = Q(); q.data = data
        self.q.put_nowait(types.SimpleNamespace(update_id=self.uid, callback_query=q, message=None))


def is_menu(entry): return entry and entry[0] == "bot" and entry[1].startswith(("🤖 Token Bot", "📊 TOP BOTS", "💼 OPEN", "📜 LAST", "🔎 Send"))


async def chat_test():
    S = evo.load(); evo.ensure_population(S, []); evo.save(S)
    S = evo.load()
    tb = Chat(); g = vars(bot); pc = {}
    poller = asyncio.create_task(menu.poll(tb, g, S, pc))
    await menu.cleanup(tb, g, S); await menu.keep_menu_last(tb, g, S)
    check(S.get("menu_id") == tb.last() and is_menu(tb.msgs[tb.last()]), "a menu is posted at start and is last")

    tb.user_text("hi"); await asyncio.sleep(0.3)
    check(tb.last() == S["menu_id"], "after the owner types, the menu is the last message")
    check(sum(1 for e in tb.msgs.values() if e[2] is not None and is_menu(e)) == 1, "only one menu in the chat")

    events = ["⭐ Bot v1 passed its final test: 20/30 wins, +$4.00 (paper)."]
    await evo.flush(tb, g, S, events); await asyncio.sleep(0.1)
    check(tb.last() == S["menu_id"], "after an alert, the menu moves back under it")
    check(any(e[1].startswith("⭐ Bot v1") for e in tb.msgs.values()), "the alert itself is still in the chat")
    check(tb.notif[-1] is True, "menu repost is silent (no notification)")

    menu_before = S["menu_id"]
    tb.tap(S["menu_id"], "m:lb"); await asyncio.sleep(0.3)
    check(S["menu_id"] == menu_before and tb.msgs[menu_before][1].startswith("📊 TOP BOTS"), "screen opens inside the menu (edited in place)")
    tb.tap(S["menu_id"], "m:menu"); await asyncio.sleep(0.2)

    tb.tap(S["menu_id"], "m:analyze"); await asyncio.sleep(0.2)
    tb.user_text("bonk"); await asyncio.sleep(3)
    res = [m for m, e in tb.msgs.items() if e[1].startswith("🔎 bonk")]
    check(bool(res) and tb.last() == S["menu_id"] and res[0] < S["menu_id"], "search results appear above, menu stays last")
    if res:
        tb.tap(res[0], "m:a:DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"); await asyncio.sleep(3)
        check(any(e[1].startswith("🔍 Bonk") for e in tb.msgs.values()) and tb.last() == S["menu_id"], "analysis from a result button, menu stays last")
    tb.tap(menu_before, "m:open"); await asyncio.sleep(0.5)  # tap on a menu that has already been replaced
    check(tb.last() == S["menu_id"] and sum(1 for e in tb.msgs.values() if is_menu(e)) == 1, "tap on an old menu gives one fresh menu at the bottom")

    n_before = len(tb.msgs)
    await asyncio.sleep(21)  # TG_KEEP_HOURS is 20 s in this test
    await menu.cleanup(tb, g, S); await menu.keep_menu_last(tb, g, S)
    check(list(tb.msgs) == [S["menu_id"]], f"after the age limit everything is deleted except one fresh menu ({n_before} -> {len(tb.msgs)})")
    check(len(S["tg_msgs"]) == 1, "tracking list is pruned")
    poller.cancel()


class FakeNotion:
    """Fake Notion: pages/blocks in memory; rejects requests nested deeper than Notion allows."""
    def __init__(self): self.blocks = {}; self.children = {}; self.calls = 0; self.pages = {}
    def _new(self, b, parent, depth):
        if depth > 2: raise AssertionError("more than 2 nesting levels in one request")
        bid = str(uuid.uuid4()); self.blocks[bid] = b; self.children.setdefault(parent, []).append(bid)
        for c in b.get(b["type"], {}).get("children", []): self._new(c, bid, depth + 1)
        return bid
    async def request(self, method, url, headers=None, json=None):
        self.calls += 1
        path = url.split("/v1", 1)[1]
        out = {}
        if method == "POST" and path == "/pages":
            pid = str(uuid.uuid4()); self.pages[pid] = json; out = {"id": pid}
        elif method == "GET" and path.startswith("/pages/"):
            out = {"id": path.split("/")[2], "archived": False}
        elif method == "PATCH" and path.endswith("/children"):
            parent = path.split("/")[2]
            assert len(json["children"]) <= 100
            out = {"results": [{"id": self._new(b, parent, 1)} for b in json["children"]]}
        elif method == "GET" and "/children" in path:
            parent = path.split("/")[2]
            out = {"results": [{"id": i} for i in self.children.get(parent, [])], "has_more": False}
        elif method == "DELETE":
            bid = path.split("/")[2]
            for k in self.children: self.children[k] = [x for x in self.children[k] if x != bid]
        return types.SimpleNamespace(status_code=200, headers={}, json=lambda: out, raise_for_status=lambda: None)
    def depth(self, bid):
        kids = [k for k in self.children.get(bid, []) if self.blocks[k]["type"] == "toggle"]
        return 1 + max((self.depth(k) for k in kids), default=0)


async def tree_test():
    real_sleep = asyncio.sleep
    tree.asyncio = types.SimpleNamespace(sleep=lambda s: real_sleep(0))  # no rate-limit pauses in the test
    S = evo.load()
    # build a lineage: v1 -> v101 -> v102 -> v103 (4 generations) plus siblings, with some trades
    vs = list(S["variants"].values())
    chain_parent = vs[0]
    for gen in range(1, 5):
        child = evo.new_variant(S, dict(chain_parent["genome"]), gen, chain_parent["id"], ["test child"])
        child["closed"] = [{"mint": "m", "pnl_pct": 30, "pnl_usd": 1.5 * gen, "win": True, "why": "take-profit", "ts": time.time()}]
        chain_parent = child
    vs[1]["closed"] = [{"mint": "x", "pnl_pct": -50, "pnl_usd": -2, "win": False, "why": "stop-loss", "ts": time.time()}]
    vs[1]["status"], vs[1]["reason"] = "retired", "failed 5-trade check"
    fn = FakeNotion(); g = {"http": fn}
    await tree.refresh(g, S)
    pid = S["tree_page"]
    heads = [b for b in fn.children[pid] if fn.blocks[b]["type"] == "heading_2"]
    check(len(fn.children[pid]) == 3 and all(fn.blocks[h]["heading_2"].get("is_toggleable") for h in heads), "page = callout + 2 collapsible headings")
    top = fn.children[heads[0]]
    toggles = fn.children[heads[1]]
    check(fn.pages[pid]["parent"]["page_id"] == tree.PARENT, "tree page is created under the Token Bot Journal")
    check(len(toggles) == len([v for v in S["variants"].values() if not v["parent"]]), f"one top-level toggle per family ({len(toggles)})")
    first = fn.blocks[toggles[0]]["toggle"]["rich_text"]
    check("v1 " in first[0]["text"]["content"] and first[1]["annotations"]["color"] == "gray", "family with the best descendant is listed first")
    check(max(fn.depth(t) for t in toggles) == 5, "a 5-generation line is nested 5 toggles deep")
    total = sum(1 for b in fn.blocks.values() if b["type"] == "toggle")
    check(total == len(S["variants"]), f"every bot appears exactly once ({total})")
    top_list = [fn.blocks[b] for b in top if fn.blocks[b]["type"] == "numbered_list_item"]
    check(top_list and "🏆" in top_list[0]["numbered_list_item"]["rich_text"][0]["text"]["content"], "top list starts with a trophy bot")
    check(top_list[0]["numbered_list_item"]["rich_text"][1]["annotations"]["color"] == "green", "profitable bot shown green")
    calls1 = fn.calls
    await tree.refresh(g, S)  # second run: old blocks cleared, rebuilt on the same page
    heads2 = [b for b in fn.children[pid] if fn.blocks[b]["type"] == "heading_2"]
    check(S["tree_page"] == pid and len(fn.children[pid]) == 3 and len(fn.children[heads2[1]]) == len(toggles), "rebuild replaces the old tree on the same page")
    print(f"Notion calls per rebuild: {calls1} (first), {fn.calls - calls1} (second)")
    mk = menu.menu_markup(S)
    check(any(b.text.startswith("🌳") for row in mk.inline_keyboard for b in row), "menu has a Family tree button")


async def run_test():
    os.remove("evo_state.json")
    tb = Chat()
    errors = []
    h = logging.Handler(); h.emit = lambda r: errors.append(r.getMessage()) if r.levelno >= logging.ERROR else None
    logging.getLogger().addHandler(h)
    async def poke():
        await asyncio.sleep(5); tb.user_text("hi")
        await asyncio.sleep(3); tb.tap(evo.load().get("menu_id") or 0, "m:rules")
    asyncio.create_task(poke())
    await evo.run(tb, vars(bot), 25)
    S = evo.load()
    check(not errors, f"25 s of the real trading loop: no errors logged {errors[:2]}")
    check(S.get("menu_id") == tb.last(), "menu is the last message at the end of the run")
    check(sum(1 for e in tb.msgs.values() if is_menu(e)) == 1, "exactly one menu in the chat")


async def main():
    await chat_test()
    await tree_test()
    await run_test()
    print(f"offline requests: {dict(fakenet.calls)}" + (f"; unknown (404): {fakenet.unknown[:5]}" if fakenet.unknown else ""))
    print("\nALL PASSED" if not fails else f"\n{len(fails)} FAILED")

asyncio.run(main())
