"""Virtual replay (2026-10-25): every bot's rules on every recorded coin (paths.py). Measurement only.

The real bots share about 2 decisions an hour (max 3 bots per coin), so after a day a bot has a handful of trades and
evolution judges luck. Here each bot's own entry rule (Momentum v3.2: evo.m31_pair_ok + its 5 m window +
evo.m31_holders_ok) and exits (evo.m31_exits / evo.m31_exit, partial sell at ps_at, costs, rug/crash penalty) run
on the same recorded price rows, on every coin that passed the safety rules, with the 30 min per-coin cooldown.
Per-coin results are independent of money (percent of the stake), so one bot gets many separate results.

Not yet used to retire or breed bots: the scores are stored (v["virt"]) and reported, and switching evolution to
them is decided on a benchmark first. `python bench.py [hours]` prints the comparison.

Rows from the recorder: [ts, price, liq, m5, h1, h6, b5, s5, vol_h1]. Optimistic like all paper trading: fills at
the recorded price, one row every ~15 s (exits can slip further between rows than live, which checks every 5 s).
"""
import bisect, statistics, sys, time

import evo
import paths

VIRT_H = 48          # hours of recorded paths each bot is scored on
JUMP = 0.5           # a single row 50%+ away from both neighbours is a bad price (see evo.checked_price): dropped


def clean(rows) -> list:
    out = []
    for i, r in enumerate(rows):
        if 0 < i < len(rows) - 1:
            a, b = rows[i - 1][1], rows[i + 1][1]
            if a and b and abs(r[1] / a - 1) >= JUMP and abs(r[1] / b - 1) >= JUMP:
                continue
        out.append(r)
    return out


def pair_of(r) -> dict:
    """The parts of a DexScreener pair the entry rules read, rebuilt from one row."""
    return {"priceUsd": r[1], "liquidity": {"usd": r[2]}, "priceChange": {"m5": r[3], "h1": r[4], "h6": r[5]},
            "txns": {"m5": {"buys": r[6], "sells": r[7]}}, "volume": {"h1": r[8]}}


def prepare(coins: dict) -> dict:
    """Per coin: cleaned rows and the row indexes where the shared v3.2 market rule holds (genome-independent)."""
    out = {}
    for m, c in coins.items():
        rows = clean(c["rows"])
        if len(rows) < 2:
            continue
        cand = []
        if evo.m31_holders_ok(c["a"] or {}):
            for i, r in enumerate(rows):
                age = (r[0] * 1000 - float(c["created"])) / 60000 if c.get("created") else None
                p = pair_of(r)
                vl = (r[8] or 0) / r[2] if r[2] else 0
                if vl >= evo.HARD_MIN_VOL_LIQ and evo.m31_pair_ok(p, age):
                    cand.append(i)
        out[m] = {"name": c["name"], "rows": rows, "cand": cand, "cand_ts": [rows[i][0] for i in cand], "memo": {}}
    return out


def exit_from(rows, i, ex) -> tuple[float, str, int]:
    """Enter at row i with exit settings ex; returns (whole-trade net %, exit reason, exit row index)."""
    r0 = rows[i]
    pos = {"entry_price": r0[1], "entry_liq": r0[2], "ts": r0[0], "sl": ex["sl"], "hold": ex["hold"],
           "peak_price": r0[1], "last_price": r0[1], "lock": None, "ps_done": False}
    part = 0.0  # net % booked by the partial sell, as a share of the whole stake
    left = 1.0
    last = i
    for j in range(i + 1, len(rows)):
        r = rows[j]
        if r[0] - rows[last][0] > 600:  # a gap of 10+ min in the recording (between runs): sell at the last row
            break
        last = j
        price, liq = r[1], r[2]
        gross = (price / pos["entry_price"] - 1) * 100
        if ex.get("ps_frac") and not pos["ps_done"] and gross >= ex["ps_at"]:
            part += ex["ps_frac"] * (gross - evo.COST_PCT)
            left -= ex["ps_frac"]
            pos["ps_done"] = True
        why = evo.m31_exit(pos, price, liq, r[0])
        if why:
            slip = evo.RUG_EXIT_SLIP if why in ("liquidity collapse", "crash exit") else 0.0
            net = max(-100.0, gross - evo.COST_PCT - slip)
            return part + left * net, why, j
    gross = (rows[last][1] / pos["entry_price"] - 1) * 100
    return part + left * max(-100.0, gross - evo.COST_PCT), "end of recording", last


def replay(gn: dict, P: dict) -> list:
    """Every virtual trade one genome makes on the prepared coins: [(mint, entry ts, net %, why)].
    The stop-loss is replayed to the nearest 0.25% and the partial-sell level to the nearest 1%, so bots with nearly
    the same exits share one simulation (P[mint]["memo"]); 600 bots stay a few seconds instead of minutes."""
    lo, hi, min_liq = evo.m31_window(gn)
    ex = evo.m31_exits(gn)
    ex = dict(ex, sl=round(ex["sl"] * 4) / 4, ps_at=float(round(ex["ps_at"])))
    key = (ex["sl"], ex["ps_at"])
    out = []
    for m, c in P.items():
        rows, cand, ts, memo = c["rows"], c["cand"], c["cand_ts"], c["memo"]
        k = 0
        while k < len(cand):
            i = cand[k]
            r = rows[i]
            if not (lo <= (r[3] or 0) <= hi and (r[2] or 0) >= min_liq):
                k += 1
                continue
            hit = memo.get((i, key))
            if hit is None:
                hit = memo[(i, key)] = exit_from(rows, i, ex)
            pct, why, j = hit
            out.append((m, r[0], pct, why))
            free_at = max(rows[j][0], r[0] + evo.COOLDOWN_MIN * 60)  # one trade at a time per coin, then the cooldown
            k = bisect.bisect_left(ts, free_at, k + 1)
    return out


def summary(T: list) -> dict:
    if not T:
        return {"n": 0, "coins": 0, "avg": 0.0, "sum": 0.0, "wins": 0}
    by: dict = {}
    for m, _, pct, _ in T:
        by.setdefault(m, []).append(pct)
    return {"n": len(T), "coins": len(by), "avg": round(statistics.mean(t[2] for t in T), 2),
            "sum": round(sum(t[2] for t in T), 1), "wins": sum(1 for t in T if t[2] > 0),
            "coins_up": sum(1 for x in by.values() if sum(x) > 0)}


def score_bots(S, hours=VIRT_H) -> dict:
    """Replay every active bot on the last `hours` of recorded paths; store the summary on the bot (v["virt"])."""
    P = prepare(paths.load(hours))
    now = time.time()
    for v in evo.active(S):
        v["virt"] = dict(summary(replay(v["genome"], P)), ts=round(now), h=hours)
    return {"coins": len(P), "with_entries": sum(1 for c in P.values() if c["cand"])}


SCORES, BOTS_IN = "virt_scores.json", "virt_bots.json"


async def score_in_background(S):
    """Run the replay in a separate, low-priority process (it can take a minute on two days of paths) so exits and
    scans never wait for it, then copy the scores onto the bots that still exist."""
    import asyncio, json, os
    from pathlib import Path
    here = Path(__file__).resolve().parent
    # the replay only needs each active bot's genes: a small snapshot, not the state file (may not be saved yet)
    Path(BOTS_IN).write_text(json.dumps({"variants": {v["id"]: {k: v.get(k) for k in ("id", "genome", "status", "m5_arm")}
                                                      for v in evo.active(S)}}))
    proc = await asyncio.create_subprocess_exec(sys.executable, str(here / "bench.py"), "--score", BOTS_IN,
                                                SCORES, stdout=asyncio.subprocess.DEVNULL)
    try:
        if await proc.wait() != 0:
            evo.log.warning("virtual replay process failed (exit %s)", proc.returncode)
            return
    except asyncio.CancelledError:
        proc.kill()
        raise
    R = json.loads(Path(SCORES).read_text())
    for f in (SCORES, BOTS_IN):
        os.remove(f)
    for vid, virt in R["virt"].items():
        if vid in S["variants"]:
            S["variants"][vid]["virt"] = virt
    evo.log.info("virtual replay: %s coins, %s bots scored", R["coins"], len(R["virt"]))


def digest_lines(S) -> list:
    """For the hourly summary: what the recorded paths say about the population (only once there is data)."""
    V = [v["virt"] for v in evo.active(S) if (v.get("virt") or {}).get("n")]
    if not V:
        return []
    out = [f"🎞 Virtual replay ({V[0]['h']} h of recorded coins): {len(V)} bots traded, median "
           f"{statistics.median(x['avg'] for x in V):+.1f}%/trade, {sum(1 for x in V if x['sum'] > 0)} net positive"]
    for arm in evo.M32_ARMS:
        A = [v["virt"] for v in evo.active(S) if v.get("m5_arm") == arm and (v.get("virt") or {}).get("n")]
        if A:
            out.append(f"   {arm}: median {statistics.median(x['avg'] for x in A):+.1f}%/trade over "
                       f"{statistics.median(x['n'] for x in A):.0f} trades")
    return out


if __name__ == "__main__":
    if sys.argv[1:2] == ["--score"]:  # background scoring for evo.run: bots' genes in, scores out
        import json, os
        if hasattr(os, "nice"):
            os.nice(10)
        S = json.load(open(sys.argv[2]))
        info = score_bots(S)
        json.dump({"coins": info["coins"], "virt": {v["id"]: v["virt"] for v in evo.active(S)}}, open(sys.argv[3], "w"))
        sys.exit(0)
    H = float(sys.argv[1]) if len(sys.argv) > 1 else VIRT_H
    S = evo.load()
    P = prepare(paths.load(H))
    print(f"{len(P)} coins recorded, {sum(1 for c in P.values() if c['cand'])} with v3.2 entry signals, "
          f"{sum(len(c['rows']) for c in P.values())} rows")
    res = sorted(((summary(replay(v["genome"], P)), v) for v in evo.active(S)), key=lambda x: -x[0]["sum"])
    for s, v in res[:10]:
        print(f"{v['id']:>7} {v.get('m5_arm') or '-':>7} n{s['n']:>4} coins{s['coins']:>3} avg{s['avg']:+6.2f}% "
              f"sum{s['sum']:+7.1f}% wins {s['wins']}")
    for arm in evo.M32_ARMS:
        A = [s for s, v in res if v.get("m5_arm") == arm and s["n"]]
        if A:
            print(f"{arm}: {len(A)} bots, median avg {statistics.median(x['avg'] for x in A):+.2f}%/trade")
