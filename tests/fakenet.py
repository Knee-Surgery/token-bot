"""Network for the tests: every HTTP request the bot makes is answered from real market data recorded earlier
(tests/fixtures/market.json, made by record_market.py), so the tests run on real coins (their DexScreener pairs,
RugCheck reports, GeckoTerminal info) without depending on the internet, API speed or rate limits. Usage (before the
code under test runs):

    import bot, fakenet; fakenet.install(bot)

It swaps bot.http (the one client everything uses, directly or as g["http"]) for an httpx client with a
MockTransport. Prices start at the recorded ones and take a small seeded random step on every price request, so
trades open and close the same way every run. Notion, Jupiter, Solana RPC and t.me get minimal valid answers;
anything unknown gets a 404. Modes (environment):
  FAKENET_LIVE=1   no fake at all: the tests hit the real APIs (python tests/run_all.py --live)
  FAKENET_SYNTH=1  made-up coins instead of the recording (also used when there is no recording)
`fakenet.calls` counts requests per host; `fakenet.unknown` lists 404s.
"""
import copy, json, os, random, re, time, uuid
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "market.json"
LIVE = os.getenv("FAKENET_LIVE") == "1"
calls: Counter = Counter()
unknown: list = []
_rnd = random.Random(7)
_born = time.time() - 2 * 86400

SNAP = None
if FIXTURE.exists() and os.getenv("FAKENET_SYNTH") != "1":
    SNAP = json.loads(FIXTURE.read_text(encoding="utf-8"))
    SOURCE = f"real market recorded {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(SNAP['recorded']))}"
    COINS = {m: (p["baseToken"].get("symbol"), p["baseToken"].get("name"), float(p["priceUsd"])) for m, p in SNAP["pairs"].items()}
else:
    SOURCE = "made-up market"
    COINS = {BONK: ("Bonk", "Bonk", 0.0000201), "FakeA" + "1" * 39: ("FAKEA", "Fake A", 0.0105),
             "FakeB" + "2" * 39: ("FAKEB", "Fake B", 0.00042), "FakeC" + "3" * 39: ("FAKEC", "Fake C", 1.37)}
_price = {m: c[2] for m, c in COINS.items()}


def pair(m: str) -> dict:
    _price[m] *= 1 + _rnd.gauss(0.002, 0.02)
    if SNAP:
        p = copy.deepcopy(SNAP["pairs"][m])
        p["priceUsd"] = f"{_price[m]:.8g}"
        return p
    sym, name, _ = COINS[m]
    return {"chainId": "solana", "dexId": "raydium", "pairAddress": "P" + m[1:], "url": f"https://dexscreener.com/solana/{m}",
            "baseToken": {"address": m, "name": name, "symbol": sym}, "quoteToken": {"address": "So1111", "symbol": "SOL"},
            "priceUsd": f"{_price[m]:.8g}", "liquidity": {"usd": 90000}, "fdv": 2.5e6, "marketCap": 2.5e6,
            "volume": {"m5": 9000, "h1": 90000, "h6": 400000, "h24": 1200000},
            "priceChange": {"m5": round(_rnd.uniform(3, 9), 2), "h1": 6.5, "h6": 18.0, "h24": 40.0},
            "txns": {"m5": {"buys": 40, "sells": 25}, "h1": {"buys": 400, "sells": 300},
                     "h6": {"buys": 2000, "sells": 1600}, "h24": {"buys": 7000, "sells": 6000}},
            "pairCreatedAt": int(_born * 1000), "info": {"socials": [{"type": "twitter", "url": "https://x.com/fake"}],
                                                          "websites": [{"url": "https://fake.example"}]}}


def report(m: str) -> dict | None:
    if SNAP:
        return (SNAP.get("rugcheck") or {}).get(m)
    holders = [{"address": f"H{i}{m[:8]}", "owner": f"O{i}{m[:8]}", "pct": 2.5 - i * 0.1, "insider": False} for i in range(12)]
    return {"mint": m, "score": 100, "score_normalised": 1, "risks": [], "rugged": False, "totalHolders": 6400,
            "topHolders": holders, "mintAuthority": None, "freezeAuthority": None, "creator": "Creator" + m[:8],
            "transferFee": {"pct": 0}, "markets": [{"pubkey": "P" + m[1:], "lp": {"lpLockedPct": 100}}]}


def _mints(path_part: str) -> list:
    return [x for x in path_part.split(",") if x in COINS]


def _json(data, status=200) -> httpx.Response:
    return httpx.Response(status, json=data)


def _gt_list() -> dict:
    if SNAP and SNAP.get("gt_trending"):
        return SNAP["gt_trending"]
    return {"data": [{"id": f"solana_P{m[1:]}", "attributes": {"name": COINS[m][0]},
                      "relationships": {"base_token": {"data": {"id": f"solana_{m}"}}}} for m in COINS]}


def route(req: httpx.Request) -> httpx.Response:
    u = urlparse(str(req.url))
    host, path, q = u.netloc, u.path, parse_qs(u.query)
    calls[host] += 1
    if host == "api.dexscreener.com":
        if path.startswith("/latest/dex/search"):
            term = (q.get("q") or [""])[0].lower()
            hit = [m for m, c in COINS.items() if term in (c[0] or "").lower() or term in (c[1] or "").lower() or term == m.lower()]
            return _json({"pairs": [pair(m) for m in (hit or list(COINS))]})
        if path.startswith("/latest/dex/tokens/"):
            return _json({"pairs": [pair(m) for m in _mints(path.rsplit("/", 1)[1])]})
        if path.startswith("/tokens/v1/solana/"):
            return _json([pair(m) for m in _mints(path.rsplit("/", 1)[1])])
        if path.startswith("/orders/v1/"):
            m = path.rsplit("/", 1)[1]
            rec = (SNAP or {}).get("orders", {}).get(m) if SNAP else None
            return _json(rec if rec is not None else {"orders": [{"type": "tokenProfile", "status": "approved"}],
                                                      "boosts": [{"amount": 10}]})
        if path.startswith(("/token-boosts/", "/token-profiles/", "/community-takeovers/")):
            return _json([{"chainId": "solana", "tokenAddress": m, "amount": 10, "totalAmount": 10} for m in COINS])
    if host == "api.geckoterminal.com":
        if re.search(r"/(trending_pools|new_pools|pools)$", path):
            return _json(_gt_list())
        mm = re.search(r"/tokens/([^/]+)/info$", path)
        if mm:
            rec = (SNAP or {}).get("gt_info", {}).get(mm.group(1)) if SNAP else None
            return _json(rec or {"data": {"attributes": {"gt_score": 60, "twitter_handle": "fake", "telegram_handle": "fakecoin",
                                                         "websites": ["https://fake.example"],
                                                         "holders": {"count": 6400, "distribution_percentage": {"top_10": 22}}}}})
        return _json({"data": []})
    if host == "api.rugcheck.xyz":
        parts = path.split("/")
        rep = report(parts[3]) if len(parts) > 3 and parts[3] in COINS else None
        return _json(rep) if rep else _json({}, 404)
    if host == "lite-api.jup.ag" and "/price/" in path:
        ids = (q.get("ids") or [""])[0].split(",")
        return _json({m: {"usdPrice": _price[m]} for m in ids if m in COINS})
    if host.endswith("solana.com") or "rpc" in host:
        body = json.loads(req.content or b"{}")
        res = {"getSignaturesForAddress": [], "getTransaction": None}.get(body.get("method"), {"value": []})
        return _json({"jsonrpc": "2.0", "id": body.get("id", 1), "result": res})
    if host == "t.me":
        return httpx.Response(200, text='<div class="tgme_page_extra">12 345 members, 678 online</div>')
    if host == "api.notion.com":
        if path.endswith("/query"):
            return _json({"object": "list", "results": [], "has_more": False, "next_cursor": None})
        if "/children" in path:
            if req.method == "GET":
                return _json({"object": "list", "results": [], "has_more": False, "next_cursor": None})
            n = len((json.loads(req.content or b"{}")).get("children", []))
            return _json({"object": "list", "results": [{"id": str(uuid.uuid4())} for _ in range(n)]})
        return _json({"object": "page", "id": str(uuid.uuid4()), "properties": {}, "parent": {}})
    unknown.append(f"{req.method} {req.url}")
    return _json({}, 404)


def install(bot_module):
    """Answer the bot's requests from the recorded market (or, with FAKENET_LIVE=1, leave the real client alone)."""
    if LIVE:
        return bot_module.http
    bot_module.http = httpx.AsyncClient(transport=httpx.MockTransport(route), timeout=5)
    return bot_module.http
