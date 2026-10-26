"""Coin analyser, trained by evolution like the bots (owner, 2026-10-26). Offline only: it never trades.

Each analyser weighs the 21 facts the trainees research on every coin (trainee.research):
  - safety and holders: bubble-map clusters, brand-new wallets, insiders, top-10 share, holder count
  - hype and promotion: X / Telegram / website, Telegram group size and how many are online, paid DexScreener
    profile and boost spend, GeckoTerminal trending and score
  - money flow: big-wallet buys vs sells in the last 30 min and whether the buyers are established wallets,
    5 min buy/sell balance, volume acceleration, 5 m / 1 h price moves
Its genes are a weight per fact, a score bar and one of the trainees' 36 exit plans (trainee.ARMS). It "takes" a coin
when its score clears the bar; a take is right when that plan, replayed on the coin's real price ticks after the
research (the trainees' shadow trades), closed in profit after costs.

Training is evolution: a population of analysers, the best on the training data breed (mutation + crossover), the
rest are replaced, generation after generation. Fitness is the average return of the coins it takes, judged on two
halves of the training data separately (the worse half counts), so an analyser has to work on both, not on a lucky
few coins. It must take at least MIN_SHARE of the coins in each half.

Then it is tested on data it never saw: (1) the latest 30% of the period, (2) a quarter of the coins held out
entirely. Every prediction comes with its analysis: the facts for and against, and how much each one counted.

News and X activity are not in the facts yet: X needs a paid API, and coin news needs a news API key.

    python analyser.py [history.json.gz ...]   train, test, save the best analyser to analyser_best.json
Goal (owner): 95% of its takes right on both tests, consistently, on 10,000+ researched coins; then validation.
"""
import gzip, json, math, random, statistics, sys
from pathlib import Path

import trainee

FEATS = trainee.FEATS
LABELS = {"bias": "baseline", "has_x": "has an X account", "has_tg": "has a Telegram", "has_web": "has a website",
          "tg_size": "Telegram group size", "tg_online": "Telegram members online", "promo": "paid boost spend",
          "paid_profile": "paid DexScreener profile", "gt_score": "GeckoTerminal score", "holders": "holder count",
          "whale_net": "big wallets net buying (30 min)", "whale_old": "big buyers are established wallets",
          "buy_ratio": "5 min buys vs sells", "vol_accel": "volume accelerating", "m5": "5 min price move",
          "h1": "1 h price move", "trending": "trending on GeckoTerminal", "fresh_share": "brand-new wallets' share",
          "cluster_share": "linked-wallet cluster share", "insider": "insider share", "top10": "top-10 holders' share"}
POP, GENS, ELITE = 200, 60, 40
MIN_PICKS = 15
MIN_SHARE = 0.05  # and at least this share of the coins in each half, so it can't hide in a few lucky ones
GOAL, MIN_DATA = 0.95, 10000
N_ARMS = len(trainee.ARMS)


def load(files) -> list:
    """Research results [{mint, ts, f: [21 facts], flags, res: [net % per plan]}], de-duplicated, oldest first."""
    seen = {}
    for p in files:
        for r in json.load(gzip.open(p, "rt", encoding="utf-8")):
            if len(r.get("res") or []) == N_ARMS and len(r.get("f") or []) == len(FEATS):
                seen.setdefault((r["mint"], r["ts"]), r)
    return sorted(seen.values(), key=lambda r: r["ts"])


def score(g, f) -> float:
    z = sum(w * x for w, x in zip(g["w"], f))
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))


def picks(g, R) -> list:
    return [r["res"][g["arm"]] for r in R if score(g, r["f"]) >= g["thr"]]


def fitness(g, halves) -> float:
    vals = []
    for H in halves:
        v = picks(g, H)
        if len(v) < max(MIN_PICKS // 2, MIN_SHARE * len(H)):
            return -100.0 + len(v)  # takes too few coins to judge
        vals.append(statistics.mean(v))
    return min(vals)


def random_genome() -> dict:
    return {"w": [trainee.PRIOR[k] + random.gauss(0, 0.5) for k in FEATS], "thr": random.uniform(0.3, 0.8),
            "arm": random.randrange(N_ARMS)}


def child(a, b) -> dict:
    w = [(x if random.random() < 0.5 else y) + random.gauss(0, 0.15) for x, y in zip(a["w"], b["w"])]
    thr = min(0.95, max(0.05, (a["thr"] if random.random() < 0.5 else b["thr"]) + random.gauss(0, 0.03)))
    arm = a["arm"] if random.random() < 0.9 else random.randrange(N_ARMS)
    return {"w": w, "thr": thr, "arm": arm}


def evolve(R, seed=0) -> dict:
    random.seed(seed)
    mid = len(R) // 2
    halves = (R[:mid], R[mid:])  # in time order: an analyser must work early and late in its training data
    pop = [random_genome() for _ in range(POP)]
    for _ in range(GENS):
        pop.sort(key=lambda g: fitness(g, halves), reverse=True)
        elite = pop[:ELITE]
        pop = elite + [child(random.choice(elite), random.choice(elite)) for _ in range(POP - ELITE)]
    best = max(pop, key=lambda g: fitness(g, halves))
    return dict(best, fit=round(fitness(best, halves), 2))


def judge(g, R) -> dict:
    v = picks(g, R)
    base = [r["res"][g["arm"]] for r in R]
    return {"coins": len(R), "takes": len(v), "right": (sum(1 for x in v if x > 0) / len(v)) if v else None,
            "avg": statistics.mean(v) if v else None, "base_right": sum(1 for x in base if x > 0) / len(base),
            "base_avg": statistics.mean(base)}


def explain(g, r, top=4) -> str:
    """The analysis behind one prediction: probability, verdict, the facts that counted most for and against."""
    contrib = sorted(((w * x, k) for w, x, k in zip(g["w"], r["f"], FEATS) if k != "bias" and x), reverse=True)
    p = score(g, r["f"])
    pro = ", ".join(f"{LABELS[k]} ({c:+.2f})" for c, k in contrib[:top] if c > 0) or "none"
    con = ", ".join(f"{LABELS[k]} ({c:+.2f})" for c, k in contrib[::-1][:top] if c < 0) or "none"
    return (f"win chance {p:.0%} -> {'TAKE' if p >= g['thr'] else 'skip'} (bar {g['thr']:.0%}) | for: {pro} | "
            f"against: {con}" + (f" | red flags: {'; '.join(r['flags'])}" if r.get("flags") else ""))


def show(name, j):
    if not j["takes"]:
        print(f"  {name}: took no coins")
        return 0.0
    print(f"  {name}: took {j['takes']} of {j['coins']}, {j['right']:.0%} right, {j['avg']:+.2f}%/trade "
          f"(taking everything: {j['base_right']:.0%} right, {j['base_avg']:+.2f}%/trade)")
    return j["right"] if j["takes"] >= 30 else 0.0


def main():
    files = sys.argv[1:] or [str(p) for p in sorted(Path("paths").glob("research_*.json.gz"))]
    R = load(files)
    coins = sorted({r["mint"] for r in R})
    print(f"{len(R)} researched coins-at-a-time on {len(coins)} coins; goal {MIN_DATA}+")
    if len(R) < 200:
        sys.exit("Too little data to train yet.")
    cut = int(len(R) * 0.7)
    g1 = evolve(R[:cut])
    print(f"\nTest 1, trained on the first 70%, tested on the latest 30% (plan {trainee.ARMS[g1['arm']]}, "
          f"bar {g1['thr']:.0%}, training fitness {g1['fit']:+.2f}%/trade)")
    p1 = show("later data", judge(g1, R[cut:]))
    random.seed(1)
    held = set(random.sample(coins, len(coins) // 4))
    g2 = evolve([r for r in R if r["mint"] not in held], seed=1)
    print(f"\nTest 2, {len(held)} coins never seen (plan {trainee.ARMS[g2['arm']]}, bar {g2['thr']:.0%})")
    p2 = show("unseen coins", judge(g2, [r for r in R if r["mint"] in held]))
    g = evolve(R, seed=2)  # final analyser on everything, for use in reports
    json.dump({"w": dict(zip(FEATS, g["w"])), "thr": g["thr"], "arm": g["arm"], "plan": trainee.ARMS[g["arm"]],
               "trained_on": len(R)}, open("analyser_best.json", "w"), indent=1)
    print("\nWhat the analyser has learned (strongest weights):")
    for w, k in sorted(((w, k) for w, k in zip(g["w"], FEATS) if k != "bias"), key=lambda x: -abs(x[0]))[:8]:
        print(f"  {LABELS[k]:40s} {w:+.2f}")
    print("\nExample analyses (latest coins):")
    for r in R[-3:]:
        print(f"  {r['mint'][:6]}…: {explain(g, r)}  -> actual {r['res'][g['arm']]:+.1f}%")
    ok = len(R) >= MIN_DATA and min(p1, p2) >= GOAL
    print(f"\nGoal ({MIN_DATA}+ coins researched, {GOAL:.0%} of takes right on both tests, 30+ takes): "
          + ("MET" if ok else f"not met (data {len(R)}/{MIN_DATA}, right {p1:.0%} / {p2:.0%})"))


if __name__ == "__main__":
    main()
