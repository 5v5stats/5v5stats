r"""The re-implemented 5v5stats rating must reproduce the site's published leaderboard exactly.
Run after import5v5\sync.py:  python tests\test_rating.py"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "web"))
import rating  # noqa: E402

site = json.load(open(os.path.join(ROOT, "import5v5", "data", "site.json"), encoding="utf-8"))
ok = True
for period, d in site["byPeriod"].items():
    worst = rating.validate(d["players"], period)
    mism = {k: v for k, v in worst.items() if k in ("qualified", "rank", "rating")}
    score = max(worst.get("PowerScore", 0), worst.get("AdjustedPowerScore", 0))
    good = not mism and score < 1e-9
    ok &= good
    print(f"{period:4} {len(d['players']):3} players: score diff {score:.1e}, rank/tier mismatches {mism or 0} -> "
          f"{'OK' if good else 'FAIL'}")
print("ALL OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
