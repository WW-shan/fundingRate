# -*- coding: utf-8 -*-
"""逐交易所官方对账：每家单独跑，保存日志并汇总"""
import subprocess, sys, os, time, re

exchanges = ["binance", "okx", "bybit", "gate", "bitget"]
results = {}
for ex in exchanges:
    print("=" * 70, flush=True)
    print(f"[{ex}] 开始对账 ...", flush=True)
    t0 = time.time()
    try:
        p = subprocess.run(
            [sys.executable, "-X", "utf8", "scripts/verify_all_exchanges.py", "--exchange", ex],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=420
        )
        out = (p.stdout or "") + (p.stderr or "")
        code = p.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + (e.stderr or "") + "\nTIMEOUT"
        code = -1
    log_path = f"logs/verify_{ex}.log"
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(out)
    elapsed = time.time() - t0
    lines = [ln for ln in out.splitlines() if ln.strip()]
    key = [ln for ln in lines if ln.startswith(("funding", "prices", "peg:", "references:", "internal:", "mismatch details:", "[跳过]", "SKIPPED")) or "active=" in ln or "official=" in ln]
    print(f"[{ex}] exit={code} elapsed={elapsed:.1f}s log={log_path}", flush=True)
    for ln in key[-8:]:
        print("  " + ln, flush=True)
    m = re.search(r"mismatch details:\s*(\d+)", out)
    results[ex] = {"exit": code, "mismatch": int(m.group(1)) if m else None, "log": log_path, "elapsed": round(elapsed, 1)}

print("=" * 70)
print("逐所对账汇总")
all_ok = True
for ex in exchanges:
    r = results[ex]
    ok = (r["exit"] == 0 and r["mismatch"] == 0)
    all_ok = all_ok and ok
    print(f"  {ex:8s} exit={r['exit']:>3} mismatch={r['mismatch']} elapsed={r['elapsed']}s {'PASS' if ok else 'FAIL'}")
print("ALL_PASS" if all_ok else "HAS_FAILURES")

sys.exit(0 if all_ok else 1)
