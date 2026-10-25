"""Offline network for the tests: every HTTP request the bot makes is answered from a small fake market, so the
tests never depend on the internet, API speed or rate limits. Usage (before the code under test runs):

    import bot, fakenet; fakenet.install(bot)

It swaps bot.http (the one client everything uses, directly or as g["http"]) for an httpx client with a
MockTransport. The market: Bonk (for the search test) and three made-up coins, all 2 days old with healthy
liquidity and holders, whose prices take a small seeded random step on every price request, so trades open and
close the same way every run. Notion, RugCheck, Jupiter, GeckoTerminal, Solana RPC and t.me get minimal valid
answers; anything else gets a 404. `fakenet.calls` counts requests per host; `fakenet.unknown` lists 404s.
"""
import json, random, re, time, uuid
from collections import Counter
from urllib.parse import parse_qs, urlparse

import httpx

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
COINS = {BONK: ("Bonk", "Bonk", 0.0000201), "FakeA" + "1" * 39: ("FAKEA", "Fake A", 0.0105),
         "FakeB" + "2" * 39: ("FAKEB", "Fake B", 0.00042), "FakeC" + "3" * 39: ("FAKEC", "Fake C", 1.37)}
calls: Counter = Counter()
unknown: list = []
_rnd = random.Random(7)
_price = {m: c[2] for m, c in COINS.items()}
_born = time.time() - 2 * 86400


def pair(m: str) -> dict:
    sym, name, _ = COINS[m]
    _price[m] *= 1 + _rnd.gauss(0.002, 0.02)
    return {"chainId": "solana", "dexId": "raydium", "pairAddress": "P" + m[1:], "url": f"https://dexscreener.com/solana/{m}",
            "baseToken": {"address": m, "name": name, "symbol": sym}, "quoteToken": {"address": "So1111", "symbol": "SOL"},
            "priceUsd": f"{_price[m]:.8g}", "liquidity": {"usd": 90000}, "fdv": 2.5e6, "marketCap": 2.5e6,
            "volume": {"m5": 9000, "h1": 90000, "h6": 400000, "h24": 1200000},
            "priceChange": {"m5": round(_rnd.uniform(3, 9), 2), "h1": 6.5, "h6": 18.0, "h24": 40.0},
            "txns": {"m5": {"buys": 40, "sells": 25}, "h1": {"buys": 400, "sells": 300},
                     "h6": {"buys": 2000, "sells": 1600}, "h24": {"buys": 7000, "sells": 6000}},
            "pairCreatedAt": int(_born * 1000), "info": {"socials": [{"type": "twitter", "url": "https://x.com/fake"}],
                                                          "websites": [{"url": "https://fake.example"}]}}


def report(m: str) -> dict:
    holders = [{"address": f"H{i}{m[:8]}", "owner": f"O{i}{m[:8]}", "pct": 2.5 - i * 0.1, "insider": False} for i in range(12)]
    return {"mint": m, "score": 100, "score_normalised": 1, "risks": [], "rugged": False, "totalHolders": 6400,
            "topHolders": holders, "mintAuthority": None, "freezeAuthority": None, "creator": "Creator" + m[:8],
            "transferFee": {"pct": 0}, "markets": [{"pubkey": "P" + m[1:], "lp": {"lpLockedPct": 100}}]}


def _mints(path_part: str) -> list:
    return [x for x in path_part.split(",") if x in COINS]


def _json(data, status=200) -> httpx.Response:
    return httpx.Response(status, json=data)


def route(req: httpx.Request) -> httpx.Response:
    u = urlparse(str(req.url))
    host, path, q = u.netloc, u.path, parse_qs(u.query)
    calls[host] += 1
    if host == "api.dexscreener.com":
        if path.startswith("/latest/dex/search"):
            term = (q.get("q") or [""])[0].lower()
            hit = [m for m, c in COINS.items() if term in c[0].lower() or term in c[1].lower() or term == m.lower()]
            return _json({"pairs": [pair(m) for m in (hit or list(COINS))]})
        if path.startswith("/latest/dex/tokens/"):
            return _json({"pairs": [pair(m) for m in _mints(path.rsplit("/", 1)[1])]})
        if path.startswith("/tokens/v1/solana/"):
            return _json([pair(m) for m in _mints(path.rsplit("/", 1)[1])])
        if path.startswith("/orders/v1/"):
            return _json({"orders": [{"type": "tokenProfile", "status": "approved"}], "boosts": [{"amount": 10}]})
        if path.startswith(("/token-boosts/", "/token-profiles/", "/community-takeovers/")):
            return _json([{"chainId": "solana", "tokenAddress": m, "amount": 10, "totalAmount": 10} for m in COINS])
    if host == "api.geckoterminal.com":
        if re.search(r"/(trending_pools|new_pools|pools)$", path):
            return _json({"data": [{"id": f"solana_P{m[1:]}", "attributes": {"name": COINS[m][0]},
                                    "relationships": {"base_token": {"data": {"id": f"solana_{m}"}}}} for m in COINS]})
        if re.search(r"/tokens/[^/]+/info$", path):
            return _json({"data": {"attributes": {"gt_score": 60, "twitter_handle": "fake", "telegram_handle": "fakecoin",
                                                  "websites": ["https://fake.example"],
                                                  "holders": {"count": 6400, "distribution_percentage": {"top_10": 22}}}}})
        return _json({"data": []})
    if host == "api.rugcheck.xyz":
        m = path.split("/")[3] if len(path.split("/")) > 3 else ""
        return _json(report(m)) if m in COINS else _json({}, 404)
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
    """Replace the bot's HTTP client with the offline one (safe to call more than once)."""
    bot_module.http = httpx.AsyncClient(transport=httpx.MockTransport(route), timeout=5)
    return bot_module.http
