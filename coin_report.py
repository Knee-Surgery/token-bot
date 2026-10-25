"""Fact-based report for every coin the bots enter (owner, 2026-10-24: "weigh everything against hard facts").

Every line is a measured value from DexScreener, RugCheck or the bots' own trade history, checked against a fixed,
stated rule: pass (full weight), warn (half) or fail (none). The evidence score is the weighted share of checks
passed, 0-100, capped at 30 by a deal-breaker (a failed safety check, liquidity under $30k or a crashed price).
It is a tally of facts, not a price prediction. Missing data is shown as "no data" and left out of
the score. Price rises and paid promotion are listed as facts only and are not scored (they cut both ways).
"""
import time

PASS, WARN, FAIL, NODATA = "✅", "⚠️", "❌", "·"


def _n(x):
    return f"{x:,.0f}"


def _usd(x):
    return f"${x / 1e6:.2f}M" if x >= 1e6 else f"${x / 1e3:.1f}k" if x >= 1e3 else f"${x:.0f}"


def _ratio(tx):
    b, s = (tx or {}).get("buys", 0), (tx or {}).get("sells", 0)
    return (b / s if s else (float("inf") if b else None)), b, s


def checks(p: dict, a: dict, track: dict, now: float | None = None) -> list:
    """[(group, label, value text, rule text, mark, weight)]"""
    now = now or time.time()
    out = []

    def add(group, label, val, rule, mark, w):
        out.append((group, label, val, rule, mark, w))

    def band(x, good, ok, higher=True):
        if x is None:
            return NODATA
        return (PASS if x >= good else WARN if x >= ok else FAIL) if higher else (PASS if x <= good else WARN if x <= ok else FAIL)

    # --- on-chain safety (RugCheck)
    for k, label in (("mint_auth", "Mint authority"), ("freeze_auth", "Freeze authority")):
        v = a.get(k)
        add("Safety", label, "no data" if v is None else ("ON" if v else "off"), "must be off",
            NODATA if v is None else FAIL if v else PASS, 3)
    lp = a.get("lp_locked")
    add("Safety", "LP locked/burned", "no data" if lp is None else f"{lp:.0f}%", "pass 95%+, warn 50%+",
        band(lp, 95, 50), 3)
    fee = a.get("fee_pct")
    add("Safety", "Transfer fee", "no data" if fee is None else f"{fee:g}%", "pass 0%, warn up to 2%",
        NODATA if fee is None else PASS if fee == 0 else WARN if fee <= 2 else FAIL, 2)
    risks = a.get("risks")
    if a.get("rugged") or a.get("danger"):
        add("Safety", "RugCheck", "rugged" if a.get("rugged") else "danger-level risk", "no danger risks", FAIL, 3)
    else:
        add("Safety", "RugCheck risks", "no data" if risks is None else (", ".join(risks)[:60] or "none"),
            "pass none, warn some", NODATA if risks is None else PASS if not risks else WARN, 2)

    # --- who holds it
    h, t10, t1 = a.get("holders"), a.get("top10"), a.get("top1")
    add("Holders", "Holders", "no data" if h is None else _n(h), "pass 2,000+, warn 500+", band(h, 2000, 500), 2)
    add("Holders", "Top 10 share", "no data" if t10 is None else f"{t10:.1f}%", "pass <=25%, warn <=40%",
        band(t10, 25, 40, higher=False), 2)
    add("Holders", "Biggest wallet", "no data" if t1 is None else f"{t1:.1f}%", "pass <=5%, warn <=10%",
        band(t1, 5, 10, higher=False), 2)
    for k, label, good, ok in (("insider_pct", "Insiders", 5, 20), ("creator_pct", "Creator", 2, 15)):
        x = a.get(k)
        add("Holders", label, "no data" if x is None else f"{x:.1f}%", f"pass <={good}%, warn <={ok}%",
            band(x, good, ok, higher=False), 2)

    # --- can you get out (liquidity)
    liq = (p.get("liquidity") or {}).get("usd", 0) or 0
    mcap = p.get("marketCap") or p.get("fdv") or 0
    vol = p.get("volume") or {}
    add("Liquidity", "Liquidity", _usd(liq), "pass $100k+, warn $30k+", band(liq, 100000, 30000), 2)
    add("Liquidity", "Market cap / liquidity", f"{mcap / liq:.1f}x ({_usd(mcap)})" if liq and mcap else "no data",
        "pass <=10x, warn <=30x", band(mcap / liq if liq and mcap else None, 10, 30, higher=False), 2)
    vl1 = (vol.get("h1") or 0) / liq if liq else None
    add("Liquidity", "1h volume / liquidity", "no data" if vl1 is None else f"{vl1:.2f}x", "pass 0.5-2x, warn 0.2-5x",
        NODATA if vl1 is None else PASS if 0.5 <= vl1 <= 2 else WARN if 0.2 <= vl1 <= 5 else FAIL, 2)
    vl24 = (vol.get("h24") or 0) / liq if liq else None
    add("Liquidity", "24h volume / liquidity", "no data" if vl24 is None else f"{vl24:.1f}x",
        "pass <=10x, warn <=30x (higher = wash-trading risk)", band(vl24, 10, 30, higher=False), 1)

    # --- order flow
    tx = p.get("txns") or {}
    for win, w in (("m5", 1), ("h1", 2), ("h6", 2), ("h24", 1)):
        r, b, s = _ratio(tx.get(win))
        add("Flow", f"Buys/sells {win}", "no data" if r is None else f"{b:,}/{s:,} ({'∞' if r == float('inf') else f'{r:.2f}'})",
            "pass 1.2+, warn 0.9+", band(r, 1.2, 0.9), w)

    # --- trend and age
    ch = p.get("priceChange") or {}
    up = sum(1 for k in ("h1", "h6") if (ch.get(k) or 0) > 0)
    crashed = (ch.get("h6") or 0) <= -50 or (ch.get("h24") or 0) <= -70
    add("Trend", "1h and 6h price", f"1h {ch.get('h1') or 0:+g}%, 6h {ch.get('h6') or 0:+g}%",
        "pass both up, warn one, fail if 6h <=-50% or 24h <=-70%",
        FAIL if crashed else PASS if up == 2 else WARN if up == 1 else FAIL, 1)
    created = p.get("pairCreatedAt")
    age_h = (now * 1000 - float(created)) / 3.6e6 if created else None
    add("Trend", "Pair age", "no data" if age_h is None else f"{age_h:.1f}h", "pass 24h+, warn 4h+", band(age_h, 24, 4), 1)

    # --- the bots' own results on this coin
    n = track.get("n", 0)
    if n >= 10:
        add("Bots", "Bots' P&L on it", f"{n} trades, {track['wins'] / n:.0%} won, {track['pnl']:+.2f}$ "
            f"(last 6h {track['n6']} trades {track['pnl6']:+.2f}$)", "pass in profit, fail in loss (10+ trades)",
            PASS if track["pnl"] > 0 else FAIL, 2)
    else:
        add("Bots", "Bots' P&L on it", f"{n} trades so far", "needs 10+ trades", NODATA, 2)
    return out


DEAL_BREAKER_CAP = 30


def deal_breakers(cs: list) -> list:
    """Facts that outweigh everything else: any failed safety check, liquidity under $30k, or a crashed price."""
    return [f"{label}: {val}" for g, label, val, _, m, _ in cs
            if m == FAIL and (g == "Safety" or label in ("Liquidity", "1h and 6h price"))]


def score(cs: list):
    got = sum(w * (1 if m == PASS else 0.5 if m == WARN else 0) for _, _, _, _, m, w in cs if m != NODATA)
    tot = sum(w for _, _, _, _, m, w in cs if m != NODATA)
    if not tot:
        return None
    sc = round(100 * got / tot)
    return min(sc, DEAL_BREAKER_CAP) if deal_breakers(cs) else sc


def track_record(S: dict, mint: str, now: float | None = None) -> dict:
    now = now or time.time()
    T = [t for v in S["variants"].values() for t in v["closed"] if t["mint"] == mint]
    T6 = [t for t in T if now - t["ts"] <= 6 * 3600]
    return {"n": len(T), "wins": sum(1 for t in T if t["win"]), "pnl": sum(t["pnl_usd"] for t in T),
            "n6": len(T6), "pnl6": sum(t["pnl_usd"] for t in T6)}


def report(name: str, mint: str, p: dict, a: dict, track: dict, now: float | None = None):
    """(score, Telegram text under 1,500 characters)"""
    cs = checks(p, a, track, now)
    sc = score(cs)
    tally = {m: sum(1 for c in cs if c[4] == m) for m in (PASS, WARN, FAIL, NODATA)}
    ch = p.get("priceChange") or {}
    info = p.get("info") or {}
    socials = [s.get("type") for s in info.get("socials") or [] if s.get("type")] + (["website"] if info.get("websites") else [])
    boosts = (p.get("boosts") or {}).get("active") or 0
    lines = [f"🔎 COIN REPORT: {name}  {mint[:4]}…{mint[-4:]}",
             f"Evidence score {sc if sc is not None else '?'}/100: {tally[PASS]} {PASS} {tally[WARN]} {WARN} "
             f"{tally[FAIL]} {FAIL}" + (f", {tally[NODATA]} no data" if tally[NODATA] else ""),
             f"Price {p.get('priceUsd')}  5m {ch.get('m5') or 0:+g}%  1h {ch.get('h1') or 0:+g}%  "
             f"6h {ch.get('h6') or 0:+g}%  24h {ch.get('h24') or 0:+g}% (facts, not scored)",
             f"Attention: {boosts} paid DexScreener boosts; socials: {', '.join(socials) or 'none listed'} (not scored)"]
    db = deal_breakers(cs)
    if db:
        lines.insert(2, f"❌ Deal-breaker (score capped at {DEAL_BREAKER_CAP}): " + "; ".join(db))
    group = None
    for g, label, val, rule, mark, _ in cs:
        if g != group:
            lines.append(f"— {g}")
            group = g
        lines.append(f"{mark} {label}: {val}  [{rule}]")
    lines.append("Score = weighted share of checks passed. A tally of facts, not a prediction.")
    text = "\n".join(lines)
    if len(text) > 1490:  # drop the rule brackets first, then trim
        text = "\n".join(l.split("  [")[0] for l in lines)[:1490]
    return sc, text
