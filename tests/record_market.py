"""Record a snapshot of the real market for the tests: python tests/record_market.py

Fetches real, public data (no keys) for about 8 established Solana coins the bots could trade (4 h+ old, $30k+
liquidity, from DexScreener's boosted lists) plus Bonk: their DexScreener pairs, RugCheck reports (trimmed to the
fields the bot reads), GeckoTerminal token info, DexScreener orders, and GeckoTerminal trending pools. Saved to
tests/fixtures/market.json; fakenet.py then answers the tests' requests from it (offline, the same every run).
Re-run it now and then to refresh the snapshot.
"""
import asyncio, json, sys, time
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parent / "fixtures" / "market.json"
DS, GT, RC = "https://api.dexscreener.com", "https://api.geckoterminal.com/api/v2/networks/solana", "https://api.rugcheck.xyz/v1"
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
RC_KEYS = ("mint", "score", "score_normalised", "risks", "rugged", "totalHolders", "mintAuthority", "freezeAuthority",
           "creator", "transferFee")
WANT = 8


async def get(c, url, **kw):
    for attempt in range(3):
        try:
            r = await c.get(url, **kw)
            if r.status_code == 429:
                await asyncio.sleep(3 * (attempt + 1)); continue
            return r.json() if r.status_code == 200 else None
        except Exception as e:
            print("  failed:", url[:80], e)
            await asyncio.sleep(1)
    return None


def trim_report(rep: dict) -> dict:
    out = {k: rep.get(k) for k in RC_KEYS if k in rep}
    out["topHolders"] = [{k: h.get(k) for k in ("address", "owner", "pct", "insider")} for h in (rep.get("topHolders") or [])[:20]]
    out["markets"] = [{"pubkey": m.get("pubkey"), "liquidityA": m.get("liquidityA"), "liquidityB": m.get("liquidityB"),
                       "mintLP": m.get("mintLP"), "lp": {"lpLockedPct": (m.get("lp") or {}).get("lpLockedPct")}}
                      for m in (rep.get("markets") or [])[:5]]
    return out


async def main():
    now = time.time()
    async with httpx.AsyncClient(timeout=20, headers={"Accept": "application/json"}) as c:
        boosts = (await get(c, f"{DS}/token-boosts/top/v1") or []) + (await get(c, f"{DS}/token-boosts/latest/v1") or [])
        mints = list(dict.fromkeys([BONK] + [b["tokenAddress"] for b in boosts if b.get("chainId") == "solana"]))
        pairs: dict = {}
        for i in range(0, len(mints), 30):
            for p in await get(c, f"{DS}/tokens/v1/solana/" + ",".join(mints[i:i + 30])) or []:
                m = (p.get("baseToken") or {}).get("address")
                if p.get("chainId") == "solana" and m and ((p.get("liquidity") or {}).get("usd") or 0) > \
                        ((pairs.get(m) or {}).get("liquidity") or {}).get("usd", 0):
                    pairs[m] = p
        ok = [m for m, p in pairs.items() if m != BONK and ((p.get("liquidity") or {}).get("usd") or 0) >= 30000
              and p.get("pairCreatedAt") and now - p["pairCreatedAt"] / 1000 >= 4 * 3600]
        keep = [BONK] + ok[:WANT]
        print(f"{len(pairs)} pairs, {len(ok)} established, keeping {len(keep)}")
        snap = {"recorded": now, "pairs": {m: pairs[m] for m in keep if m in pairs}, "rugcheck": {}, "gt_info": {},
                "orders": {}, "gt_trending": await get(c, f"{GT}/trending_pools", headers={"Accept": "application/json"})}
        for m in keep:
            rep = await get(c, f"{RC}/tokens/{m}/report")
            if rep:
                snap["rugcheck"][m] = trim_report(rep)
            snap["gt_info"][m] = await get(c, f"{GT}/tokens/{m}/info", headers={"Accept": "application/json"})
            snap["orders"][m] = await get(c, f"{DS}/orders/v1/solana/{m}")
            await asyncio.sleep(2.1)  # GeckoTerminal allows about 30 requests a minute
            print(" ", (pairs[m]["baseToken"].get("symbol") if m in pairs else m[:6]), "rugcheck" if rep else "no rugcheck")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(snap, separators=(",", ":")))
    print(f"saved {OUT} ({OUT.stat().st_size // 1024} KB, {len(snap['pairs'])} coins)")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
