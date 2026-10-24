"""The trainees: three bots trained intensely (paper only), each with its own personality, model, plan and money.

  - Trainee 1 · Balanced: the default rules below.
  - Trainee 2 · Careful: stricter bubble map (no linked group >= 10%, new wallets < 15%, insiders < 5%), must have
    an X account, higher score bar, only the tight stop-loss and never a double buy on a dip.
  - Trainee 3 · Bold: lower score bar and bigger stakes.
They share one research engine and the same shadow trades (so three trainees cost no more requests than one), but
each learns its own scoring model from them and picks its own exit plan and threshold.

The research covers every coin they look at the way a careful trader would, and they learn from all of them, not only
from the few they buy:
  - socials and community: X, Telegram (members, online now), website, from DexScreener / GeckoTerminal; the public
    t.me page gives the group size. X activity itself can't be read without a paid X API, so only "has an X" counts.
  - promotion and hype: DexScreener paid profile and boosts (dollars spent), buy/sell pressure, volume
    acceleration, GeckoTerminal trending.
  - big wallets: buys and sells over $1,000 in the last 30 min (GeckoTerminal), and whether the big buyers are
    established wallets or brand-new ones.
  - a home-made bubble map: who funded each of the 12 biggest holders (the fee payer of the wallet's first
    transaction, Solana RPC). Holders funded by the same wallet are one cluster; fresh wallets (few transactions)
    holding a lot is the classic bundled-launch pattern. Bubblemaps' own data isn't open, so this does the same job.
Every researched coin becomes a 'shadow trade': its price is followed and every exit plan in ARMS (take-profit,
stop-loss, partial sells, double buys, hold time) is simulated on the real price ticks. A small logistic model learns
which research signals came before a profit, and every few results the trainee switches to the plan and score
threshold that made the most on the coins it would actually have bought. Real (paper) trades use that plan.
Double buys: 'strength' adds 50% once the trade is up 8% and buyers still lead; 'dip' adds 50% on a pullback, only
for high-scoring coins with steady liquidity. The data decides which, if either, is used. Every entry, double buy,
partial sell and exit goes to its own Notion database with its own P&L chart. All the main bots' safety rules
(evo.rug_ok) apply on top of the research, and the trainees keep their own 4 h minimum age (MIN_AGE).
"""
import asyncio, calendar, logging, math, os, re, time

import auto
import evo

log = logging.getLogger("trainee")

DB = os.getenv("NOTION_DB_TRAINEE", "c5e3ed3c-c9ed-4c61-8c8f-0b8b20b5146c")
RPC = "https://api.mainnet-beta.solana.com"
GT = "https://api.geckoterminal.com/api/v2/networks/solana"
START = 10.0
STAKE = 0.3             # fraction of funds per trade (a double buy adds half of the first buy)
FEE = evo.COST_PCT / 2  # half of the round-trip cost on each buy and each sell
WHALE_USD = 1000
MAX_SHADOWS = 30
MAX_DONE = 250  # finished shadow trades kept for learning and plan choice
SHADOW_SAMPLE_S = 30
RESEARCH_EVERY_S = 30
TICK_S = 15
RE_RESEARCH_MIN = 45
MIN_AGE = 240        # trainees only research coins 4 h+ old (the 300 bots' own minimum is separate)
CANDS_EVERY_S = 120  # how often the trainees fetch their own list of established coins

# exit plans the trainee compares on every coin it researches (fine-tuning grid)
ARMS = [dict(tp=tp, sl=sl, ps=ps, add=add, hold=hold)
        for tp in (20, 35, 60) for sl in (6, 10) for ps in ((0, 0), (0.5, 15)) for add in ("none", "strength", "dip")
        for hold in (15, 30)]
HIGH_SCORE = 0.65
MIN_PICKED = 12
PATCH_OPEN_S = 120  # how often an open trade's row gets its current price and unrealised P&L

ALL_ARMS = tuple(range(len(ARMS)))
PROFILES = {
    "t1": {"name": "Trainee 1 · Balanced", "short": "Balanced", "stake": STAKE, "strict": False,
           "thresholds": (0.45, 0.55, 0.65), "arms": ALL_ARMS,
           "default": (ARMS.index(dict(tp=35, sl=6, ps=(0.5, 15), add="strength", hold=30)), 0.55)},
    "t2": {"name": "Trainee 2 · Careful", "short": "Careful", "stake": 0.25, "strict": True,
           "thresholds": (0.6, 0.7, 0.8), "arms": tuple(i for i, a in enumerate(ARMS) if a["sl"] == 6 and a["add"] != "dip"),
           "default": (ARMS.index(dict(tp=35, sl=6, ps=(0.5, 15), add="none", hold=30)), 0.7)},
    "t3": {"name": "Trainee 3 · Bold", "short": "Bold", "stake": 0.4, "strict": False,
           "thresholds": (0.35, 0.45, 0.55), "arms": ALL_ARMS,
           "default": (ARMS.index(dict(tp=60, sl=10, ps=(0.5, 15), add="strength", hold=30)), 0.45)},
}

# research signals the model weighs; priors from common memecoin experience, then learned from the shadow trades
PRIOR = {"bias": -0.2, "has_x": 0.3, "has_tg": 0.3, "has_web": 0.2, "tg_size": 0.4, "tg_online": 0.2, "promo": 0.1,
         "paid_profile": 0.1, "gt_score": 0.3, "holders": 0.3, "whale_net": 0.4, "whale_old": 0.3, "buy_ratio": 0.4,
         "vol_accel": 0.3, "m5": 0.2, "h1": 0.1, "trending": 0.2,
         "fresh_share": -0.8, "cluster_share": -1.0, "insider": -0.6, "top10": -0.4}


def blank(tid) -> dict:
    arm, thr = PROFILES[tid]["default"]
    return {"usd": START, "pnl": 0.0, "pos": None, "closed": [], "w": dict(PRIOR), "arm": arm, "thr": thr,
            "rugs_avoided": 0, "best_val": None}


def lab(S) -> dict:
    """Shared research results: shadow trades in progress, finished ones, coins recently researched."""
    L = S.setdefault("trainee_lab", {"shadows": {}, "done": [], "seen": {}, "n_research": 0})
    old = S.pop("trainee", None)  # one-off: the single trainee becomes Trainee 1, its research is shared
    if old:
        for k in ("shadows", "done", "seen", "n_research"):
            L[k] = old.get(k, L[k])
        S.setdefault("trainees", {})["t1"] = {k: old[k] for k in blank("t1") if k in old}
    return L


def trainees(S) -> dict:
    lab(S)
    TT = S.setdefault("trainees", {})
    for tid in PROFILES:
        T = TT.setdefault(tid, blank(tid))
        for k, x in blank(tid).items():
            T.setdefault(k, x)
    return TT


def own_flags(tid, f) -> list:
    """Extra red flags of a trainee's personality, from the stored research features."""
    if not PROFILES[tid]["strict"]:
        return []
    x = dict(zip(FEATS, vec(f)))
    out = []
    if x["cluster_share"] >= 0.5:
        out.append("careful: linked wallets hold 10%+")
    if x["fresh_share"] >= 0.5:
        out.append("careful: new wallets hold 15%+")
    if x["insider"] >= 0.25:
        out.append("careful: insiders hold 5%+")
    if not x["has_x"]:
        out.append("careful: no X account")
    return out


def clip(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def _log1p(x, scale):
    return math.log1p(max(0.0, x)) / math.log1p(scale)


# ---------- research ----------
async def _get(g, key, url):
    return await auto._get_json(g, key, url, cool=60)


async def _rpc(g, method, params):
    try:
        r = await g["http"].post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if r.status_code == 429:
            await asyncio.sleep(2)
            return None
        return r.json().get("result")
    except Exception as e:
        log.warning("rpc %s: %s", method, e)
        return None


async def wallet(g, owner) -> dict:
    """How established a wallet is (transactions, up to 100), and who funded it: the fee payer of its first
    transaction, when that is another wallet."""
    sigs = await _rpc(g, "getSignaturesForAddress", [owner, {"limit": 100}]) or []
    out = {"txs": len(sigs), "funder": None}
    if 0 < len(sigs) < 100:
        tx = await _rpc(g, "getTransaction", [sigs[-1]["signature"], {"maxSupportedTransactionVersion": 0,
                                                                     "encoding": "jsonParsed"}])
        keys = ((tx or {}).get("transaction") or {}).get("message", {}).get("accountKeys") or []
        payer = next((k["pubkey"] for k in keys if k.get("signer")), None)
        out["funder"] = payer if payer != owner else None
    await asyncio.sleep(0.15)  # public RPC rate limit
    return out


async def bubble_map(g, rep) -> dict:
    """Top 12 holders (pools excluded): supply share held by fresh wallets (< 20 transactions) and the largest group
    of holders funded by the same wallet."""
    skip = {m.get(k) for m in rep.get("markets") or [] for k in ("pubkey", "liquidityA", "liquidityB", "mintLP")}
    tops = [h for h in rep.get("topHolders") or []
            if h.get("owner") and h["owner"] not in skip and h.get("address") not in skip][:12]
    walls = [await wallet(g, h["owner"]) for h in tops]
    groups: dict = {}
    for h, w in zip(tops, walls):
        if w["funder"]:
            groups.setdefault(w["funder"], []).append(float(h.get("pct", 0)))
    linked = [sum(x) for x in groups.values() if len(x) >= 2]
    return {"fresh_pct": round(sum(float(h.get("pct", 0)) for h, w in zip(tops, walls) if w["txs"] < 20), 2),
            "fresh_n": sum(1 for w in walls if w["txs"] < 20), "cluster_pct": round(max(linked, default=0.0), 2),
            "clusters": len(linked), "checked": len(tops)}


async def telegram(g, url) -> tuple[int, int]:
    """Members and members online from the public t.me page (0, 0 when it can't be read)."""
    try:
        h = (await g["http"].get(url, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)).text
        extra = " ".join(re.findall(r'tgme_page_extra">([^<]+)<', h)).replace("\xa0", "").replace(" ", "")
        m = re.search(r"(\d+)(members|subscribers)", extra)
        o = re.search(r"(\d+)online", extra)
        return (int(m.group(1)) if m else 0), (int(o.group(1)) if o else 0)
    except Exception as e:
        log.warning("telegram page %s: %s", url, e)
        return 0, 0


async def whales(g, pool) -> dict:
    """Trades over WHALE_USD in the last 30 min: dollars bought and sold, distinct buyers, established buyers."""
    js = await _get(g, "gt_trades", f"{GT}/pools/{pool}/trades?trade_volume_in_usd_greater_than={WHALE_USD}") or {}
    cut = time.time() - 1800
    buy = sell = 0.0
    buyers: list = []
    for t in js.get("data") or []:
        a = t.get("attributes") or {}
        try:
            ts = calendar.timegm(time.strptime(a["block_timestamp"][:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            continue
        if ts < cut:
            continue
        usd = float(a.get("volume_in_usd") or 0)
        if a.get("kind") == "buy":
            buy += usd
            if a.get("tx_from_address") and a["tx_from_address"] not in buyers:
                buyers.append(a["tx_from_address"])
        else:
            sell += usd
    old = 0
    for b in buyers[:3]:
        old += (await wallet(g, b))["txs"] >= 100
    return {"buy": buy, "sell": sell, "buyers": len(buyers), "old": old, "checked": min(3, len(buyers))}


async def research(g, mint, p) -> dict:
    """Everything the trainee checks about a coin: features for the model, red flags, and a one-line summary."""
    info = p.get("info") or {}
    socials = {s.get("type"): s.get("url") for s in info.get("socials") or []}
    orders = await _get(g, "ds_orders", f"{auto.DS}/orders/v1/solana/{mint}") or {}
    promo = sum(float(b.get("amount") or 0) for b in orders.get("boosts") or [])
    paid = any(o.get("status") == "approved" for o in orders.get("orders") or [])
    gti = ((await _get(g, "gt_info", f"{GT}/tokens/{mint}/info")) or {}).get("data", {}).get("attributes", {})
    tg = socials.get("telegram") or (f"https://t.me/{gti['telegram_handle']}" if gti.get("telegram_handle") else None)
    has_x = bool(socials.get("twitter") or gti.get("twitter_handle"))
    has_web = bool(info.get("websites") or gti.get("websites"))
    members, online = await telegram(g, tg) if tg else (0, 0)
    W = await whales(g, p.get("pairAddress")) if p.get("pairAddress") else {"buy": 0, "sell": 0, "buyers": 0, "old": 0, "checked": 0}
    rep = await _get(g, "rugcheck_trainee", f"https://api.rugcheck.xyz/v1/tokens/{mint}/report") or {}
    bm = await bubble_map(g, rep) if rep else {"fresh_pct": 0, "fresh_n": 0, "cluster_pct": 0, "clusters": 0, "checked": 0}
    tr = await _get(g, "gt_trending", f"{GT}/trending_pools") or {}
    trending = bool(p.get("pairAddress")) and any(p["pairAddress"] in (d.get("id") or "") for d in tr.get("data") or [])
    m5 = (p.get("txns") or {}).get("m5") or {}
    vol, ch = p.get("volume") or {}, p.get("priceChange") or {}
    hold = gti.get("holders") or {}
    top10 = float((hold.get("distribution_percentage") or {}).get("top_10") or 0)
    insider = sum(float(h.get("pct", 0)) for h in rep.get("topHolders") or [] if h.get("insider"))
    nb, ns = m5.get("buys", 0), m5.get("sells", 0)
    f = {"bias": 1.0, "has_x": float(has_x), "has_tg": float(bool(tg)), "has_web": float(has_web),
         "tg_size": round(_log1p(members, 20000), 3), "tg_online": round(clip(online / max(members, 1) * 10, 0), 3),
         "promo": round(_log1p(promo, 2000), 3), "paid_profile": float(paid),
         "gt_score": round(float(gti.get("gt_score") or 0) / 100, 3),
         "holders": round(_log1p(float(hold.get("count") or 0), 20000), 3),
         "whale_net": round(clip((W["buy"] - W["sell"]) / 20000), 3), "whale_old": round(W["old"] / 3, 3),
         "buy_ratio": round(clip((nb - ns) / max(10, nb + ns)), 3),
         "vol_accel": round(clip((float(vol.get("h1") or 0) / max(float(vol.get("h6") or 0) / 6, 1) - 1) / 3), 3),
         "m5": round(clip(float(ch.get("m5") or 0) / 20), 3), "h1": round(clip(float(ch.get("h1") or 0) / 50), 3),
         "trending": float(trending), "fresh_share": round(clip(bm["fresh_pct"] / 30, 0), 3),
         "cluster_share": round(clip(bm["cluster_pct"] / 20, 0), 3), "insider": round(clip(insider / 20, 0), 3),
         "top10": round(clip(top10 / 60, 0), 3)}
    flags = []
    if bm["cluster_pct"] >= 15:
        flags.append(f"bubble map: {bm['cluster_pct']:.0f}% held by wallets funded from one source")
    if bm["fresh_pct"] >= 25:
        flags.append(f"bubble map: brand-new wallets hold {bm['fresh_pct']:.0f}% (bundled launch)")
    if not (has_x or tg or has_web):
        flags.append("no X, Telegram or website")
    if tg and members < 30 and not has_x:
        flags.append(f"Telegram has only {members} members and there is no X")
    if W["sell"] > 3 * max(W["buy"], WHALE_USD):
        flags.append(f"big wallets dumping (${W['sell']:,.0f} sold vs ${W['buy']:,.0f} bought in 30 min)")
    summary = (f"X {'yes' if has_x else 'no'} | Telegram {members:,} members, {online:,} online | website "
               f"{'yes' if has_web else 'no'} | promo ${promo:,.0f}{' + paid profile' if paid else ''}"
               f"{' | trending' if trending else ''} | big wallets 30 min: +${W['buy']:,.0f} / -${W['sell']:,.0f}, "
               f"{W['buyers']} buyers ({W['old']}/{W['checked']} established) | bubble map: new wallets "
               f"{bm['fresh_pct']:.0f}%, biggest linked group {bm['cluster_pct']:.0f}% ({bm['checked']} top holders) | "
               f"insiders {insider:.0f}% | top 10 {top10:.0f}%")
    return {"f": f, "flags": flags, "summary": summary}


FEATS = list(PRIOR)  # stored feature vectors are plain lists in this order (keeps the saved state small)


def vec(f) -> list:
    return f if isinstance(f, list) else [f.get(k, 0.0) for k in FEATS]


def score(w, f) -> float:
    z = sum(w.get(k, 0.0) * x for k, x in zip(FEATS, vec(f)))
    return 1 / (1 + math.exp(-clip(z, -30, 30)))


def learn(T, f, y):
    """One step of online logistic regression, with a small pull back toward the priors."""
    p = score(T["w"], f)
    for k, x in zip(FEATS, vec(f)):
        w = T["w"].get(k, 0.0)
        T["w"][k] = round(w + 0.05 * ((y - p) * x - 0.01 * (w - PRIOR.get(k, 0.0))), 4)


# ---------- one position under one exit plan ----------
def new_leg(price, usd, liq, now) -> dict:
    return {"cost": usd, "tokens": usd * (1 - FEE / 100) / price, "realized": 0.0, "peak": price, "last": price,
            "liq": liq, "ts": now, "ps_done": False, "added": False}


def step(L, arm, price, liq, now, buyers_lead, high_score) -> list:
    """Advance one position by one price tick; returns actions ('add', usd) / ('partial', frac) / ('exit', reason).
    Pure logic: the caller books the money (apply) and writes the rows."""
    acts = []
    avg = L["cost"] / (L["tokens"] / (1 - FEE / 100))  # average buy price
    gross = (price / avg - 1) * 100
    prev, L["last"] = L["last"], price
    L["peak"] = max(L["peak"], price)
    if liq < L["liq"] * (1 - evo.RUG_LIQ_DROP):
        return [("exit", "liquidity collapse")]
    if price < prev * (1 - evo.CRASH_TICK_PCT / 100):
        return [("exit", "crash exit")]
    if L["ps_done"] and gross <= evo.COST_PCT:
        return [("exit", "break-even stop after partial sell")]  # the rest can't turn the trade into a loss
    if gross <= -arm["sl"]:
        return [("exit", "stop-loss")]
    if not L["added"]:
        if arm["add"] == "strength" and gross >= 8 and buyers_lead:
            L["added"] = True
            acts.append(("add", L["cost"] * 0.5))
        elif arm["add"] == "dip" and gross <= -arm["sl"] / 2 and high_score and liq >= L["liq"] * 0.95:
            L["added"] = True
            acts.append(("add", L["cost"] * 0.5))
    frac, at = arm["ps"]
    if frac and not L["ps_done"] and gross >= at:
        L["ps_done"] = True
        acts.append(("partial", frac))
    if gross >= arm["tp"]:
        return acts + [("exit", "take-profit")]
    if (L["peak"] / avg - 1) * 100 >= 10 and price <= L["peak"] * 0.92:
        return acts + [("exit", "trailing-stop")]
    if now - L["ts"] >= arm["hold"] * 60:
        return acts + [("exit", "max hold time")]
    return acts


def apply(L, act, price) -> float:
    """Book one action on a position. Returns the dollars it realised (0 for a buy)."""
    kind, x = act
    if kind == "add":
        L["cost"] += x
        L["tokens"] += x * (1 - FEE / 100) / price
        return 0.0
    frac = x if kind == "partial" else 1.0
    proceeds = L["tokens"] * frac * price * (1 - FEE / 100)
    basis = L["cost"] * frac
    L["tokens"] -= L["tokens"] * frac
    L["cost"] -= basis
    L["realized"] += proceeds - basis
    return proceeds - basis


# shadow legs are stored compactly: [cost, tokens, realized, ps_done, added, out]; the price path is shared
def simulate_tick(sh, price, liq, now, buyers_lead):
    """Run every exit plan of one shadow trade a tick forward."""
    for i, arm in enumerate(ARMS):
        leg = sh["legs"][i]
        if leg[5] is not None:
            continue
        L = {"cost": leg[0], "tokens": leg[1], "realized": leg[2], "ps_done": leg[3], "added": leg[4],
             "peak": sh["peak"], "last": sh["last"], "liq": sh["liq"], "ts": sh["ts"]}
        out = None
        for act in step(L, arm, price, liq, now, buyers_lead, sh["score"] >= HIGH_SCORE):
            apply(L, act, price)
            if act[0] == "exit":
                out = round(L["realized"] * 100, 2)  # % of the first buy (1 dollar)
        sh["legs"][i] = [round(L["cost"], 6), round(L["tokens"], 6), round(L["realized"], 6), L["ps_done"], L["added"], out]
    sh["last"], sh["peak"] = price, max(sh["peak"], price)


def choose(T, tid, done):
    """Switch to the exit plan and score threshold (within the trainee's personality) that made the most on the
    clean coins it would have bought (average % per trade, shrunk toward 0 while there are few results)."""
    P = PROFILES[tid]
    done = [d for d in done if not d["flags"] and not own_flags(tid, d["f"])]
    best, best_val = P["default"], None
    for thr in P["thresholds"]:
        picked = [d for d in done if score(T["w"], d["f"]) >= thr]
        if len(picked) < MIN_PICKED:
            continue
        for i in P["arms"]:
            vals = [d["res"][i] for d in picked]
            val = sum(vals) / len(vals) * len(vals) / (len(vals) + 15)
            if best_val is None or val > best_val:
                best, best_val = (i, thr), val
    T["arm"], T["thr"] = best
    T["best_val"] = None if best_val is None else round(best_val, 3)


def plan_txt(arm) -> str:
    ps = f", sell {arm['ps'][0]:.0%} at +{arm['ps'][1]}%" if arm["ps"][0] else ""
    add = {"none": "", "strength": ", double buy on strength", "dip": ", double buy on a dip"}[arm["add"]]
    return f"TP +{arm['tp']}%, SL -{arm['sl']}%{ps}{add}, max {arm['hold']} min"


# ---------- Notion ----------
async def row(g, tid, T, kind, name, price, usd, why, pnl=None, extra=None):
    """One event row (Entry / Double buy / Partial sell / Exit) for a trainee. Returns the page id."""
    props = {"Event": auto._title(f"{kind} {name}"), "Type": auto._sel(kind), "Token": auto._text(name),
             "Trainee": auto._sel(PROFILES[tid]["name"]), "Time": auto._date(time.time()),
             "Price (USD)": auto._num(price), "Size (USD)": auto._num(usd), "Reason": auto._text(why),
             "Mode": auto._sel("Paper")}
    if pnl is not None:
        props.update({"Realised PnL (USD)": auto._num(pnl), "Cumulative PnL (USD)": auto._num(T["pnl"])})
    props.update(extra or {})
    return await auto.n_create(g, DB, props)


async def entry_page(g, tid, P):
    """The trade's Entry row (its 'open/closed trade' row). Positions opened before rows were tracked are looked up."""
    if P.get("page_id") or not auto.NOTION_TOKEN:
        return P.get("page_id")
    try:
        rows = await evo._n_query(g, DB, {"and": [{"property": "Type", "select": {"equals": "Entry"}},
                                                  {"property": "Token", "rich_text": {"equals": P["name"]}},
                                                  {"property": "Status", "select": {"does_not_equal": "Closed"}}]})
        P["page_id"] = rows[0]["id"] if rows else None
    except Exception as e:
        log.warning("trainee entry row lookup: %s", e)
    return P.get("page_id")


def value(L, price) -> float:
    """What the trade has made so far: sold parts plus the rest at the current price after the sell fee."""
    return L["realized"] + L["tokens"] * price * (1 - FEE / 100) - L["cost"]


# ---------- the loop ----------
async def manage_position(g, tid, T, pc, now):
    P = T["pos"]
    p = (pc.get(P["mint"]) or (0, None))[1]
    if not p or not p.get("priceUsd"):
        return
    price = await evo.checked_price(g, P["mint"], float(p["priceUsd"]), P["leg"]["last"])
    if not price:
        return
    liq = float((p.get("liquidity") or {}).get("usd", 0) or 0)
    m5 = (p.get("txns") or {}).get("m5") or {}
    arm = ARMS[P["arm"]]
    for act in step(P["leg"], arm, price, liq, now, m5.get("buys", 0) > m5.get("sells", 0), P["score"] >= HIGH_SCORE):
        if act[0] == "add":
            usd = min(act[1], T["usd"])
            if usd < 0.05:
                continue
            T["usd"] -= usd
            apply(P["leg"], ("add", usd), price)
            await row(g, tid, T, "Double buy", P["name"], price, usd,
                      "up 8%+ and buyers still lead" if arm["add"] == "strength" else "pullback on a high-scoring coin")
            continue
        frac = act[1] if act[0] == "partial" else 1.0
        basis = P["leg"]["cost"] * frac
        got = apply(P["leg"], act, price)
        T["usd"] += basis + got
        T["pnl"] += got
        if act[0] == "partial":
            await row(g, tid, T, "Partial sell", P["name"], price, basis, f"sold {frac:.0%} at +{arm['ps'][1]}%", pnl=got)
            continue
        whole = P["leg"]["realized"]
        pct = whole / P["base"] * 100
        T["closed"].append({"mint": P["mint"], "name": P["name"], "pnl_usd": round(whole, 4), "why": act[1], "ts": now,
                            "pct": round(pct, 2), "plan": P["arm"]})
        T["closed"] = T["closed"][-200:]
        result = auto._sel("Win" if whole > 0 else "Loss")
        await row(g, tid, T, "Exit", P["name"], price, basis, act[1], pnl=got, extra={"Result": result})
        pid = await entry_page(g, tid, P)
        await auto.n_patch(g, pid, {"properties": {
            "Status": auto._sel("Closed"), "Exit Price (USD)": auto._num(price), "Exit Time": auto._date(now),
            "Trade PnL (USD)": auto._num(whole), "Trade PnL %": auto._num(round(pct, 2)), "Result": result,
            "Exit Reason": auto._text(act[1]), "Current Price (USD)": auto._num(price),
            "Unrealised PnL (USD)": auto._num(0)}})
        T["pos"] = None
        return
    if now - P.get("patched", 0) >= PATCH_OPEN_S:  # keep the open-trades view current
        P["patched"] = now
        pid = await entry_page(g, tid, P)
        await auto.n_patch(g, pid, {"properties": {"Current Price (USD)": auto._num(price),
                                                   "Unrealised PnL (USD)": auto._num(round(value(P["leg"], price), 4)),
                                                   "Size (USD)": auto._num(round(P["leg"]["cost"], 4))}})


def finish_shadow(S, mint, sh, now):
    L = lab(S)
    res = [round(leg[5], 1) for leg in sh["legs"]]
    L["done"].append({"f": vec(sh["f"]), "flags": sh["flags"], "res": res, "ts": round(now), "mint": mint})
    L["done"] = L["done"][-MAX_DONE:]
    for tid, T in trainees(S).items():
        if (sh["flags"] or own_flags(tid, sh["f"])) and min(res) < -8:
            T["rugs_avoided"] += 1  # a coin this trainee refused went on to lose more than 8% under every plan
        learn(T, sh["f"], 1.0 if res[T["arm"]] > 0 else 0.0)
        if len(L["done"]) % 5 == 0:
            old = (T["arm"], T["thr"])
            choose(T, tid, L["done"])
            if (T["arm"], T["thr"]) != old:
                log.info("%s switched plan: %s, threshold %.2f", PROFILES[tid]["name"], plan_txt(ARMS[T["arm"]]), T["thr"])


async def tick(g, S, pc):
    L, TT = lab(S), trainees(S)
    now = time.time()
    mints = list(L["shadows"]) + [T["pos"]["mint"] for T in TT.values() if T["pos"]]
    if mints:
        await evo.prefetch(g, pc, list(set(mints)), 10)
    for tid, T in TT.items():
        if T["pos"]:
            await manage_position(g, tid, T, pc, now)
    for mint, sh in list(L["shadows"].items()):
        if now - sh.get("tick", 0) < SHADOW_SAMPLE_S - 1:
            continue
        sh["tick"] = now
        p = (pc.get(mint) or (0, None))[1]
        if not p or not p.get("priceUsd"):
            if now - sh["ts"] > 3600:
                del L["shadows"][mint]  # coin gone from the feeds
            continue
        price = await evo.checked_price(g, mint, float(p["priceUsd"]), sh["last"])
        if not price:
            continue
        m5 = (p.get("txns") or {}).get("m5") or {}
        simulate_tick(sh, price, float((p.get("liquidity") or {}).get("usd", 0) or 0), now,
                      m5.get("buys", 0) > m5.get("sells", 0))
        if all(leg[5] is not None for leg in sh["legs"]):
            finish_shadow(S, mint, sh, now)
            del L["shadows"][mint]


async def research_next(g, S, pc):
    """Research one fresh candidate from the main scan, start its shadow trade, and let each trainee buy it if it
    is good enough for that trainee."""
    L, TT = lab(S), trainees(S)
    now = time.time()
    L["seen"] = {m: t for m, t in L["seen"].items() if now - t < RE_RESEARCH_MIN * 60}
    if len(L["shadows"]) >= MAX_SHADOWS:
        return
    if now - g.get("tr_cands_ts", 0) >= CANDS_EVERY_S:  # established coins: trending, top volume, boosted
        g["tr_cands_ts"], g["tr_cands"] = now, await auto.discover(g, MIN_AGE)
    pool = list(dict.fromkeys((g.get("tr_cands") or []) + (g.get("cands") or [])))
    for mint in [m for m in pool if m not in L["seen"] and m not in L["shadows"]][:8]:
        p = await evo.get_pair(g, mint, pc, 60)
        if not p or not p.get("priceUsd"):
            continue
        age = evo.pair_age_min(p, now)
        if age is None or age < MIN_AGE:
            continue
        L["seen"][mint] = now
        ac = S["acache"].get(mint)
        if ac and now - ac["ts"] < evo.ACACHE_TTL_MIN * 60:
            a = ac["a"]
        else:
            try:
                full = await g["analyze"](mint)
            except Exception as e:
                log.warning("trainee analyze %s: %s", mint, e)
                return
            a = {k: full.get(k) for k in ("score", "liq", "holders", "top10", "symbol")}
            a["danger"] = any("[danger]" in x for x in (full.get("flags") or []))
            rc = full.get("rc") or {}
            a.update({k: rc[k] for k in ("mint_auth", "freeze_auth", "rugged", "top1", "insider_pct", "creator_pct",
                                         "lp_locked", "fee_pct", "risks") if rc.get(k) is not None})
        ok, why = evo.rug_ok(a, p)
        R = await research(g, mint, p)
        L["n_research"] += 1
        flags = R["flags"] + ([] if ok else [f"safety rule: {why}"])
        price = float(p["priceUsd"])
        liq = float((p.get("liquidity") or {}).get("usd", 0) or 0)
        scores = {tid: score(T["w"], R["f"]) for tid, T in TT.items()}
        leg = new_leg(price, 1.0, liq, now)
        # the shared shadow trade uses the trainees' average score for its dip-buy rule
        L["shadows"][mint] = {"ts": now, "f": vec(R["f"]), "flags": flags,
                              "score": round(sum(scores.values()) / len(scores), 3), "last": price, "peak": price,
                              "liq": liq, "legs": [[leg["cost"], leg["tokens"], 0.0, False, False, None] for _ in ARMS]}
        name = auto.token_name(a, mint)
        for tid, T in TT.items():
            sc = scores[tid]
            mine = flags + own_flags(tid, R["f"])
            if T["pos"] is None and not mine and sc >= T["thr"] and T["usd"] > 1 and not S.get("halted"):
                usd = T["usd"] * PROFILES[tid]["stake"]
                T["usd"] -= usd
                T["pos"] = {"mint": mint, "name": name, "arm": T["arm"], "score": sc, "base": usd,
                            "leg": new_leg(price, usd, liq, now), "patched": now}
                T["pos"]["page_id"] = await row(
                    g, tid, T, "Entry", name, price, usd,
                    f"score {sc:.2f} (needs {T['thr']:.2f}); plan: {plan_txt(ARMS[T['arm']])}",
                    extra={"Status": auto._sel("Open"), "Current Price (USD)": auto._num(price),
                           "Unrealised PnL (USD)": auto._num(0), "Score": auto._num(round(sc, 3)),
                           "Research": auto._text(R["summary"])})
        return  # one coin per call: the research takes about 30 requests


def snapshot(S) -> dict:
    """Per-trainee trade count and P&L, for the hourly summary's 'since' figures."""
    return {tid: [len(T["closed"]), T["pnl"]] for tid, T in (S.get("trainees") or {}).items()}


def digest_lines(S, D) -> list:
    if not S.get("trainees"):
        return []
    since = D.get("tr") or {}
    out = []
    for tid, T in trainees(S).items():
        n0, p0 = since.get(tid, [0, 0.0])
        n, d = len(T["closed"]) - n0, T["pnl"] - p0
        if n or T["pos"] or T["closed"]:
            out.append(f"🎓 {PROFILES[tid]['short']}: {n} trades {'+' if d >= 0 else '-'}${abs(d):,.2f} (total "
                       f"{'+' if T['pnl'] >= 0 else '-'}${abs(T['pnl']):,.2f}){' · in a trade' if T['pos'] else ''}")
    L = lab(S)
    if out or L["done"]:
        out.append(f"🎓 Learned from {len(L['done'])} coins · rugs avoided "
                   + " / ".join(str(T["rugs_avoided"]) for T in trainees(S).values()))
    return out


async def loop(g, S, pc):
    """Runs beside the main loop for the whole run, so slow research never holds up the 300 bots."""
    last_r = 0.0
    while True:
        try:
            await tick(g, S, pc)
            if time.time() - last_r >= RESEARCH_EVERY_S:
                last_r = time.time()
                await research_next(g, S, pc)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("trainee error: %s", e)
        await asyncio.sleep(TICK_S)
