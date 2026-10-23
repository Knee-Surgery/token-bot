"""
Family tree of the evolving bots, written to a Notion page as nested toggles: click a bot to expand its children.

Each bot shows its status, record and net paper PnL (green = profit, red = loss); the most profitable bots get a
trophy. Families (a seed or random bot plus everything bred from it) are sorted by their best member, and children
by profit, so the strongest lines sit at the top. The page lives under the Token Bot Journal and is rebuilt once per
run (about every 25 minutes).
"""
import asyncio, logging, os, time
from datetime import datetime, timezone

import auto

log = logging.getLogger("tree")
PARENT = os.getenv("NOTION_TREE_PARENT", "40093233c10481e2ac93db11c0daf96e")  # Token Bot Journal page
API = "https://api.notion.com/v1"
TOP_N = 10
ICON = {"testing": "🧪", "validating": "🎓", "validated": "✅", "retired": "💀"}


def record(v) -> tuple[int, int, float]:
    c = v["closed"]
    return len(c), sum(1 for t in c if t["win"]), sum(t["pnl_usd"] for t in c)


def page_url(page_id: str) -> str:
    return f"https://www.notion.so/{page_id.replace('-', '')}"


def _txt(s, color="default", bold=False):
    return {"type": "text", "text": {"content": str(s)[:1900]}, "annotations": {"color": color, "bold": bold}}


def _label(v, top: set, kids: int) -> list:
    n, w, pnl = record(v)
    color = "green" if pnl > 0 else "red" if pnl < 0 else "gray"
    head = f"{'🏆 ' if v['id'] in top else ''}{ICON.get(v['status'], '')} {v['id']} · gen {v['gen']} · {v['status']}"
    rec = f"  ·  {w}/{n} wins ({w / n:.0%})  ·  {pnl:+.2f} USD" if n else "  ·  no trades yet"
    tail = f"{'  ·  ' + str(len(v['positions'])) + ' open' if v['positions'] else ''}" \
           f"{'  ·  ' + str(kids) + ' child' + ('ren' if kids != 1 else '') if kids else ''}"
    return [_txt(head, bold=v["id"] in top), _txt(rec, color), _txt(tail, "gray")]


def _details(v) -> str:
    gn = v["genome"]
    age = "any age" if gn["max_age_min"] >= 10080 else f"tokens up to {gn['max_age_min']} min old"
    parts = [f"TP {gn['tp']}% · SL {gn['sl']}% · trail {gn['trail']}% · hold {gn['hold']} min · {age} · "
             f"size {gn['size_frac']:.0%} of bankroll"]
    if v.get("notes"):
        parts.append("Born: " + "; ".join(v["notes"])[:400])
    if v.get("reason"):
        parts.append("Retired: " + v["reason"])
    return "\n".join(parts)


async def _req(g, method, path, body=None):
    for attempt in range(4):
        r = await g["http"].request(method, f"{API}{path}", headers=auto._h(), json=body)
        if r.status_code == 429:  # Notion allows ~3 requests/s; back off and retry
            await asyncio.sleep(float(r.headers.get("Retry-After", 1)) + attempt)
            continue
        r.raise_for_status()
        await asyncio.sleep(0.35)  # stay under the rate limit while the trading loop also writes to Notion
        return r.json()
    raise RuntimeError(f"Notion rate limit on {path}")


async def _append(g, parent_id, blocks) -> list[str]:
    ids = []
    for i in range(0, len(blocks), 100):
        js = await _req(g, "PATCH", f"/blocks/{parent_id}/children", {"children": blocks[i:i + 100]})
        ids += [b["id"] for b in js["results"]]
    return ids


async def _page(g, S) -> str:
    """The tree page, created once under the journal and remembered in the state file."""
    pid = S.get("tree_page")
    if pid:
        try:
            js = await _req(g, "GET", f"/pages/{pid}")
            if not js.get("archived") and not js.get("in_trash"):
                return pid
        except Exception:
            pass  # deleted or no access any more: make a new one
    js = await _req(g, "POST", "/pages", {
        "parent": {"page_id": PARENT}, "icon": {"type": "emoji", "emoji": "🌳"},
        "properties": {"title": {"title": [_txt("Bot Family Tree")]}}})
    S["tree_page"] = js["id"]
    return js["id"]


async def refresh(g, S):
    """Rebuild the tree page from a snapshot of the population."""
    if not auto.NOTION_TOKEN:
        return
    snap = {k: dict(v, closed=list(v["closed"]), positions=dict(v["positions"])) for k, v in S["variants"].items()}
    pid = await _page(g, S)

    # clear the old tree (deleting a block removes everything nested inside it)
    old, cursor = [], None
    while True:
        js = await _req(g, "GET", f"/blocks/{pid}/children?page_size=100" + (f"&start_cursor={cursor}" if cursor else ""))
        old += [b["id"] for b in js["results"]]
        cursor = js.get("next_cursor")
        if not js.get("has_more"):
            break
    for bid in old:
        await _req(g, "DELETE", f"/blocks/{bid}")

    kids: dict = {}
    for v in snap.values():
        kids.setdefault(v["parent"] if v["parent"] in snap else None, []).append(v)
    best: dict = {}

    def family_best(v):  # best PnL anywhere in this bot's line, used to sort families and siblings
        if v["id"] not in best:
            best[v["id"]] = max([record(v)[2]] + [family_best(c) for c in kids.get(v["id"], [])])
        return best[v["id"]]

    for vs in kids.values():
        vs.sort(key=lambda v: (-family_best(v), v["id"]))
    traded = sorted((v for v in snap.values() if v["closed"]), key=lambda v: -record(v)[2])
    top = {v["id"] for v in traded[:3] if record(v)[2] > 0}

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    alive = sum(1 for v in snap.values() if v["status"] != "retired")
    header = [
        {"type": "callout", "callout": {"icon": {"type": "emoji", "emoji": "🌳"}, "color": "gray_background", "rich_text": [
            _txt(f"Updated {now} · {alive} active bots, {len(snap) - alive} retired shown · paper trading only.\n"),
            _txt("Click ▸ to expand a bot and see its settings and the bots bred from it. Families are sorted by "
                 "their best member. 🏆 top 3 by profit · 🧪 testing · 🎓 validating · ✅ validated · 💀 retired. "
                 "Green = net profit, red = net loss.", "gray")]}},
        {"type": "heading_2", "heading_2": {"is_toggleable": True, "rich_text": [_txt(f"Top {TOP_N} bots by profit")],
            "children": [{"type": "numbered_list_item", "numbered_list_item": {
                "rich_text": _label(v, top, len(kids.get(v["id"], [])))}} for v in traded[:TOP_N]]
            or [{"type": "paragraph", "paragraph": {"rich_text": [_txt("No closed trades yet.", "gray")]}}]}},
        {"type": "heading_2", "heading_2": {"is_toggleable": True, "rich_text": [_txt("Family tree")]}},
    ]
    # everything sits under these three blocks, so the next rebuild clears the page with three deletes
    family_root = (await _append(g, pid, header))[-1]

    def node(v):
        return {"type": "toggle", "toggle": {"rich_text": _label(v, top, len(kids.get(v["id"], []))), "children": [
            {"type": "paragraph", "paragraph": {"rich_text": [_txt(_details(v), "gray")]}}]}}

    # Notion takes two nesting levels per request, so the tree is written one generation at a time
    level = [(family_root, kids.get(None, []))]
    while level:
        nxt = []
        for parent_block, vs in level:
            ids = await _append(g, parent_block, [node(v) for v in vs])
            nxt += [(bid, kids[v["id"]]) for bid, v in zip(ids, vs) if kids.get(v["id"])]
        level = nxt
    log.info("family tree updated: %d bots", len(snap))
