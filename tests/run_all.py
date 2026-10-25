"""Run every test in this folder: python tests/run_all.py [name ...]

Each test runs in its own temporary folder with a copy of the bot's state files (evo_state.json, auto_state.json),
so nothing in the repo is changed. Tokens are removed from the environment: the tests use fakes only, paper only.
No internet needed: tests that exercise real network code (harness2's search, analysis and 25 s trading loop,
harness27) answer every request from fakenet.py's canned market, so results don't depend on API speed or limits.

Tests (arguments: repo folder, run folder):
  harness2       chat housekeeping (menu always last, cleanup) and family tree, fake Telegram + fake Notion
  harness3b      overall running total: Notion history backfill, exits, menu header
  harness4       milestone stages: 75 consistent bots, spreading strategies, proof test
  harness8b      partial sells: genes, rollout, accounting, Notion labels
  harness17      the three trainees: one shared brain, one trainee per coin, feedback
  harness27      coin evidence reports, one report per coin per 6 h, bots per coin (evo.MAX_PER_TOKEN)
  test_review_fixes  fail-closed safety data, v3.2 A/B labels + entry-time decisions, trainee TP before add, ledger
  test_menu6h    menu line: trades and P&L of the last 6 h, refreshed in place
  test_paths_bench   price-path recorder (paths.py) and virtual replay (bench.py)
  test_x1        line X1: 10 founders, population 50 + 10, breeding hold, founder priority and 15/30-trade report
"""
import os, shutil, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
TESTS = ["harness2", "harness3b", "harness4", "harness8b", "harness17", "harness27",
         "test_review_fixes", "test_menu6h", "test_paths_bench", "test_x1"]


def run(name: str) -> tuple[bool, str, float]:
    env = {k: v for k, v in os.environ.items() if k not in ("NOTION_TOKEN", "TELEGRAM_BOT_TOKEN")}
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    with tempfile.TemporaryDirectory(prefix=f"tb_{name}_") as d:
        for f in ("evo_state.json", "auto_state.json"):
            if (SRC / f).exists():
                shutil.copy(SRC / f, d)
        t = time.time()
        try:
            p = subprocess.run([sys.executable, str(HERE / f"{name}.py"), str(SRC), d], env=env, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=400)
            out = p.stdout + p.stderr
            ok = p.returncode == 0 and "ALL PASSED" in out
        except subprocess.TimeoutExpired:
            ok, out = False, "timed out after 400 s"
        fails = [l for l in out.splitlines() if l.startswith("FAIL")]
        last = next((l for l in reversed(out.splitlines()) if l.strip()), "")
        return ok, "\n      ".join(fails) or last, time.time() - t


if __name__ == "__main__":
    names = sys.argv[1:] or TESTS
    bad = 0
    for n in names:
        ok, info, dt = run(n)
        bad += not ok
        print(f"{'PASS' if ok else 'FAIL'} {n:<18} {dt:5.0f}s" + ("" if ok else f"\n      {info}"), flush=True)
    print(f"\n{len(names) - bad}/{len(names)} passed")
    sys.exit(1 if bad else 0)
