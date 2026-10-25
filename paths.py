"""Price-path recorder (2026-10-25): the data for honest benchmarks. Measurement only, it never trades.

Every coin that passes the safety rules (evo.rug_ok, from the bots' scan or the trainees' research) is followed for
REC_H hours after it last passed, whether or not any bot buys it: every REC_S seconds one row with what the entry
rules look at (price, liquidity, 5 m / 1 h / 6 h change, 5 m buys and sells, 1 h volume). Any entry or exit rule can
then be replayed on the same coins (bench.py), instead of judging rules on the handful of coins the bots happened to
buy. Each run writes its own file, paths/<run start UTC>.jsonl.gz (one line per coin), and never touches older ones,
so git stores each file once. Files older than KEEP_DAYS are deleted. The list of coins being followed is kept in
the state (S["rec_live"]), so the next run carries on.
"""
import asyncio, calendar, gzip, json, logging, os, time
from pathlib import Path

log = logging.getLogger("paths")
DIR = Path("paths")
REC_S = float(os.getenv("PATHS_REC_S", "15"))
REC_H = float(os.getenv("PATHS_REC_H", "2"))
MAX_LIVE = int(os.getenv("PATHS_MAX_LIVE", "60"))
KEEP_DAYS = float(os.getenv("PATHS_KEEP_DAYS", "7"))
FLUSH_S = 60
COLS = ["dt", "price", "liq", "m5", "h1", "h6", "b5", "s5", "vol_h1"]  # dt: seconds since the segment's t0

_seg: dict = {}   # mint -> this run's segment {"mint", "name", "src", "a", "created", "t0", "rows"}
_file = None      # this run's file


def mark(S, mint, p, a, src):
    """A coin just passed the safety rules: follow it (again) for REC_H hours."""
    live = S.setdefault("rec_live", {})
    now = time.time()
    if mint not in live and len(live) >= MAX_LIVE:
        return
    e = live.setdefault(mint, {"src": src})
    e["until"] = now + REC_H * 3600
    e["name"] = (a or {}).get("symbol") or ((p.get("baseToken") or {}).get("symbol")) or mint[:6]
    e["created"] = p.get("pairCreatedAt")
    e["a"] = {k: (a or {}).get(k) for k in ("holders", "top10", "top1", "score")}
    if src not in e["src"].split("+"):
        e["src"] += "+" + src


def row(p, t0, now) -> list | None:
    try:
        price = float(p["priceUsd"])
    except (KeyError, TypeError, ValueError):
        return None
    ch, tx = p.get("priceChange") or {}, (p.get("txns") or {}).get("m5") or {}
    r = lambda x, n=2: None if x is None else round(float(x), n)
    return [round(now - t0), float(f"{price:.6g}"), round((p.get("liquidity") or {}).get("usd", 0) or 0),
            r(ch.get("m5")), r(ch.get("h1")), r(ch.get("h6")), tx.get("buys", 0), tx.get("sells", 0),
            round((p.get("volume") or {}).get("h1", 0) or 0)]


async def sample(g, S, pc):
    import evo
    live = S.setdefault("rec_live", {})
    now = time.time()
    for m in [m for m, e in live.items() if e["until"] < now]:
        del live[m]
    if not live:
        return
    await evo.prefetch(g, pc, list(live), REC_S - 1)
    for m, e in live.items():
        hit = pc.get(m)
        if not hit or not hit[1]:
            continue
        sg = _seg.get(m)
        if sg is None:
            sg = _seg[m] = {"mint": m, "name": e["name"], "src": e["src"], "a": e["a"], "created": e["created"],
                            "t0": round(now), "rows": []}
        rw = row(hit[1], sg["t0"], now)
        if rw and (not sg["rows"] or rw[0] > sg["rows"][-1][0]):
            sg["rows"].append(rw)
        sg["a"], sg["src"] = e["a"], e["src"]


def flush():
    """Rewrite this run's file with every segment recorded so far (atomic: temp file, then rename)."""
    global _file
    segs = [s for s in _seg.values() if s["rows"]]
    if not segs:
        return
    DIR.mkdir(exist_ok=True)
    if _file is None:
        _file = DIR / (time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + ".jsonl.gz")
    tmp = _file.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        for s in segs:
            f.write(json.dumps(dict(s, cols=COLS), separators=(",", ":")) + "\n")
    os.replace(tmp, _file)


def _ftime(f) -> float | None:
    """A file's start time from its name (file dates change on every git checkout, names don't)."""
    try:
        return calendar.timegm(time.strptime(f.name[:15], "%Y%m%dT%H%M%S"))
    except ValueError:
        return None


def prune():
    cut = time.time() - KEEP_DAYS * 86400
    for f in DIR.glob("*.jsonl.gz") if DIR.exists() else []:
        ft = _ftime(f)
        if ft is not None and ft < cut and f != _file:
            f.unlink()


def load(hours: float, until: float | None = None) -> dict:
    """{mint: {"name", "a", "created", "src", "rows": [[ts, price, ...], ...]}} from the files of the last `hours`,
    segments of one coin joined in time order (rows carry absolute timestamps)."""
    until = until or time.time()
    cut = until - hours * 3600
    out: dict = {}
    for f in sorted(DIR.glob("*.jsonl.gz")) if DIR.exists() else []:
        ft = _ftime(f)
        if ft is None or ft < cut - 3600 or ft > until:
            continue
        try:
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                for line in fh:
                    s = json.loads(line)
                    c = out.setdefault(s["mint"], {"name": s["name"], "a": s["a"], "created": s["created"],
                                                   "src": s["src"], "rows": []})
                    c["a"] = s["a"] or c["a"]
                    c["rows"] += [[s["t0"] + r[0]] + r[1:] for r in s["rows"] if cut <= s["t0"] + r[0] <= until]
        except (OSError, EOFError, ValueError) as e:
            log.warning("paths file %s unreadable: %s", f.name, e)
    for c in out.values():
        c["rows"].sort(key=lambda r: r[0])
    return {m: c for m, c in out.items() if c["rows"]}


async def loop(g, S, pc):
    """Beside the main loop: one sample every REC_S seconds, the file rewritten every FLUSH_S seconds."""
    last_flush = time.time()
    try:
        prune()
    except OSError as e:
        log.warning("paths prune: %s", e)
    try:
        while True:
            try:
                await sample(g, S, pc)
                if time.time() - last_flush >= FLUSH_S:
                    flush(); last_flush = time.time()
            except Exception as e:
                log.warning("paths sample: %s", e)
            await asyncio.sleep(REC_S)
    finally:
        try:
            flush()
        except Exception as e:
            log.warning("paths final flush: %s", e)
