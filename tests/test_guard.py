import os, sys, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import settings_guard as g
here = os.path.dirname(os.path.abspath(__file__))
f = os.path.join(here, "work.vcfg"); shutil.copyfile(g.active_cfg_file(), f)
src = g.active_cfg_file(); shutil.copyfile(src, f)
g.PENDING = os.path.join(here, "pending.json"); g.active_cfg_file = lambda: f
orig_text = open(f, encoding="utf-8").read()
orig = g.snapshot(); print("snapshot:", orig)
# simulate CS2 saving our recording values (+ a key that didn't exist before)
vals = dict(g.TOUCHED); g.write_values(f, vals)
print("during:", g.read_values(f, g.TOUCHED))
print("fixed:", g.restore_pending())
after = open(f, encoding="utf-8").read()
print("identical to original:", after == orig_text, "| pending removed:", not os.path.exists(g.PENDING))
