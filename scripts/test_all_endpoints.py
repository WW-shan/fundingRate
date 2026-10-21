# -*- coding: utf-8 -*-
"""实战端点测试：覆盖 Web 全部页面 + REST API（含认证、过滤参数、写操作）"""
import json
import sys
import time

import requests

BASE = "http://127.0.0.1:5000"
USERNAME = "admin"
PASSWORD = "admin123"

results = []


def record(name, ok, detail=""):
    results.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name} {detail}")


def check(name, resp, expect_status=200, expect_success=None, min_items=None, key="data"):
    ok = resp.status_code == expect_status
    detail = f"HTTP {resp.status_code}"
    body = None
    try:
        body = resp.json()
    except Exception:
        body = None
    if ok and expect_success is not None and isinstance(body, dict):
        if body.get("success") is not expect_success:
            ok = False
            detail += f" success={body.get('success')}"
    if ok and min_items is not None and isinstance(body, dict):
        data = body.get(key)
        n = len(data) if isinstance(data, (list, dict)) else 0
        detail += f" items={n}"
        if n < min_items:
            ok = False
            detail += f" (<{min_items})"
    record(name, ok, detail)
    return body


s = requests.Session()

print("=" * 70)
print("PART 1: 未认证访问（应被拦截）")
print("=" * 70)
for path in ["/api/summary", "/api/funding/apr", "/api/funding/spreads", "/api/basis",
             "/api/stablecoins", "/api/stablecoins/history", "/api/issuer-spreads",
             "/api/alerts", "/api/alerts/stats"]:
    r = s.get(BASE + path, allow_redirects=False)
    check(f"GET {path} (未认证)", r, expect_status=401, expect_success=False)

for path in ["/", "/funding", "/stablecoin", "/alerts"]:
    r = s.get(BASE + path, allow_redirects=False)
    ok = r.status_code == 302 and "/login" in r.headers.get("Location", "")
    record(f"GET {path} (未认证跳登录)", ok, f"HTTP {r.status_code} -> {r.headers.get('Location')}")

print()
print("=" * 70)
print("PART 2: 登录流程")
print("=" * 70)
r = s.get(BASE + "/login")
record("GET /login", r.status_code == 200 and "login" in r.text.lower(), f"HTTP {r.status_code}")

r = s.post(BASE + "/login", json={"username": USERNAME, "password": "wrong-password"})
check("POST /login 错误密码", r, expect_success=False)

r = s.post(BASE + "/login", json={"username": USERNAME, "password": PASSWORD})
check("POST /login 正确密码", r, expect_success=True)

print()
print("=" * 70)
print("PART 3: 公开 API")
print("=" * 70)
r = s.get(BASE + "/api/status")
body = check("GET /api/status", r)
if body:
    record("  status字段", body.get("status") == "running", f"status={body.get('status')} mode={body.get('mode')}")
    record("  exchanges 5家", len(body.get("exchanges", [])) == 5, f"{body.get('exchanges')}")
    record("  symbols_tracked>0", (body.get("symbols_tracked") or 0) > 0, f"{body.get('symbols_tracked')}")

r = s.get(BASE + "/api/health")
body = check("GET /api/health", r, expect_status=200)
if body:
    record("  health=healthy", body.get("status") == "healthy",
           f"components={body.get('components')} missing={body.get('missing_exchanges')}")

print()
print("=" * 70)
print("PART 4: 认证 API（基础）")
print("=" * 70)
r = s.get(BASE + "/api/summary")
body = check("GET /api/summary", r, expect_success=True)
if body:
    d = body.get("data", {})
    record("  summary 字段完整",
           all(k in d for k in ["max_abs_apr", "max_positive_apr", "max_negative_apr", "top_apr", "top_spreads", "top_basis", "depeg", "issuer_spreads", "alert_stats", "timestamp"]),
           f"keys={sorted(d.keys())}")
    record("  top_apr 非空", len(d.get("top_apr", [])) > 0, f"n={len(d.get('top_apr', []))}")

r = s.get(BASE + "/api/funding/apr")
body = check("GET /api/funding/apr", r, expect_success=True, min_items=100)
if body and body.get("data"):
    row = body["data"][0]
    record("  APR 行字段完整",
           all(k in row for k in ["exchange", "symbol", "funding_rate", "apr", "funding_interval_hours", "next_funding_time", "rate_semantics"]),
           f"sample={ {k: row.get(k) for k in ['exchange','symbol','funding_rate','apr','funding_interval_hours']} }")
    record("  APR 排序(abs_desc)", abs(body["data"][0]["apr"] or 0) >= abs(body["data"][-1]["apr"] or 0),
           f"first={body['data'][0]['apr']:.2f} last={body['data'][-1]['apr']:.2f}")

r = s.get(BASE + "/api/funding/apr", params={"limit": 5})
body = check("GET /api/funding/apr?limit=5", r, expect_success=True)
if body:
    record("  limit=5 生效", len(body.get("data", [])) == 5, f"n={len(body.get('data', []))}")

r = s.get(BASE + "/api/funding/apr", params={"exchange": "okx"})
body = check("GET /api/funding/apr?exchange=okx", r, expect_success=True, min_items=1)
if body:
    ok = all(x["exchange"] == "okx" for x in body["data"])
    record("  exchange 过滤生效", ok, f"n={len(body['data'])}")

r = s.get(BASE + "/api/funding/apr", params={"sort": "apr_desc"})
body = check("GET /api/funding/apr?sort=apr_desc", r, expect_success=True, min_items=1)
if body and body["data"]:
    vals = [x["apr"] for x in body["data"] if x.get("apr") is not None]
    record("  apr_desc 排序", vals == sorted(vals, reverse=True), f"first={vals[0]:.2f} last={vals[-1]:.2f}")

r = s.get(BASE + "/api/funding/apr", params={"sort": "apr_asc"})
body = check("GET /api/funding/apr?sort=apr_asc", r, expect_success=True, min_items=1)
if body and body["data"]:
    vals = [x["apr"] for x in body["data"] if x.get("apr") is not None]
    record("  apr_asc 排序", vals == sorted(vals), f"first={vals[0]:.2f} last={vals[-1]:.2f}")

r = s.get(BASE + "/api/funding/apr", params={"symbol": "BTC"})
body = check("GET /api/funding/apr?symbol=BTC", r, expect_success=True, min_items=1)
if body:
    ok = all("BTC" in x["symbol"] for x in body["data"])
    record("  symbol 过滤生效", ok, f"n={len(body['data'])} sample={body['data'][0]['symbol'] if body['data'] else None}")

r = s.get(BASE + "/api/funding/apr", params={"limit": "abc"})
body = check("GET /api/funding/apr?limit=abc (非法值回退)", r, expect_success=True)
if body:
    record("  非法limit回退默认1000", len(body.get("data", [])) <= 1000, f"n={len(body.get('data', []))}")

r = s.get(BASE + "/api/funding/spreads")
body = check("GET /api/funding/spreads", r, expect_success=True)
if body:
    record("  spreads 数据存在", body.get("total", 0) > 0, f"total={body.get('total')}")
    if body.get("data"):
        row = body["data"][0]
        record("  spread 行字段", all(k in row for k in ["symbol", "exchange_a", "exchange_b", "abs_spread", "apr_spread", "rate_a", "rate_b"]),
               f"sample={ {k: row.get(k) for k in ['symbol','exchange_a','exchange_b','abs_spread']} }")

r = s.get(BASE + "/api/funding/spreads", params={"limit": 3})
body = check("GET /api/funding/spreads?limit=3", r, expect_success=True)
if body:
    record("  limit=3 生效", len(body.get("data", [])) <= 3, f"n={len(body.get('data', []))}")

r = s.get(BASE + "/api/basis")
body = check("GET /api/basis", r, expect_success=True)
if body:
    record("  basis 数据存在", body.get("total", 0) > 0, f"total={body.get('total')}")
    if body.get("data"):
        row = body["data"][0]
        record("  basis 行字段", all(k in row for k in ["symbol", "exchange", "basis", "spot_price", "futures_price"]),
               f"sample={ {k: row.get(k) for k in ['symbol','exchange','basis']} }")

# 冷启动时锚定资产采集可能尚未完成，最多等待 60 秒
body = None
cats = {}
waited = 0
for _ in range(20):
    r = s.get(BASE + "/api/stablecoins")
    body = r.json()
    cats = {}
    for x in body.get("data", []):
        cats[x.get("category")] = cats.get(x.get("category"), 0) + 1
    if len(cats) >= 2:
        break
    time.sleep(3)
    waited += 3
body = check("GET /api/stablecoins", r, expect_success=True, min_items=1)
if body:
    record("  stablecoins 分类", len(cats) >= 2, f"categories={cats} waited={waited}s")
    row = body["data"][0]
    record("  depeg 行字段", all(k in row for k in ["symbol", "category", "price", "deviation", "abs_deviation", "threshold_warning", "threshold_critical"]),
           f"sample={ {k: row.get(k) for k in ['symbol','category','price','deviation']} }")

r = s.get(BASE + "/api/stablecoins", params={"category": "usd_stable"})
body = check("GET /api/stablecoins?category=usd_stable", r, expect_success=True)
if body:
    ok = all(x.get("category") == "usd_stable" for x in body["data"])
    record("  category 过滤生效", ok, f"n={len(body['data'])}")

r = s.get(BASE + "/api/stablecoins/history", params={"symbol": "USDC", "hours": 24})
body = check("GET /api/stablecoins/history?symbol=USDC", r, expect_success=True)
if body:
    record("  history 返回结构", "data" in body, f"n={len(body.get('data', []))}")

r = s.get(BASE + "/api/stablecoins/history", params={"symbol": "USDC", "hours": 99999})
body = check("GET /api/stablecoins/history?hours=99999 (上限钳制)", r, expect_success=True)

r = s.get(BASE + "/api/issuer-spreads")
body = check("GET /api/issuer-spreads", r, expect_success=True)
if body:
    record("  issuer 数据存在", body.get("total", 0) > 0, f"total={body.get('total')}")
    if body.get("data"):
        row = body["data"][0]
        record("  issuer 行字段", all(k in row for k in ["underlying", "abs_spread", "issuer_count", "exchange_count", "quotes"]),
               f"sample={ {k: row.get(k) for k in ['underlying','abs_spread']} }")

r = s.get(BASE + "/api/alerts", params={"hours": 24, "limit": 50})
body = check("GET /api/alerts", r, expect_success=True, min_items=1)
if body:
    row = body["data"][0]
    record("  alert 行字段", all(k in row for k in ["alert_type", "level", "symbol", "message", "timestamp"]),
           f"sample_type={row.get('alert_type')} level={row.get('level')}")

r = s.get(BASE + "/api/alerts", params={"level": "critical", "limit": 20})
body = check("GET /api/alerts?level=critical", r, expect_success=True)
if body:
    ok = all(x["level"] == "critical" for x in body["data"])
    record("  level 过滤生效", ok, f"n={len(body['data'])}")

r = s.get(BASE + "/api/alerts", params={"type": "stablecoin_depeg", "limit": 20})
body = check("GET /api/alerts?type=stablecoin_depeg", r, expect_success=True)
if body:
    ok = all(x["alert_type"] == "stablecoin_depeg" for x in body["data"])
    record("  type 过滤生效", ok, f"n={len(body['data'])}")

r = s.get(BASE + "/api/alerts/stats", params={"hours": 24})
body = check("GET /api/alerts/stats", r, expect_success=True)
if body:
    d = body.get("data", {})
    record("  stats 字段", "total" in d and "by_level" in d, f"keys={sorted(d.keys())}")

print()
print("=" * 70)
print("PART 5: CONFIG ENDPOINTS REMOVED")
print("=" * 70)
r = s.get(BASE + "/config")
record("GET /config removed", r.status_code == 404, f"HTTP {r.status_code}")
r = s.get(BASE + "/api/config")
record("GET /api/config removed", r.status_code == 404, f"HTTP {r.status_code}")
r = s.post(BASE + "/api/config/update", json={"category": "alerts", "key": "x", "value": 1})
record("POST /api/config/update removed", r.status_code == 404, f"HTTP {r.status_code}")
r = s.post(BASE + "/api/exchanges/reload")
record("POST /api/exchanges/reload removed", r.status_code == 404, f"HTTP {r.status_code}")

print()
print("=" * 70)
print("PART 6: 页面渲染（已认证）")
print("=" * 70)
for path, marker in [("/", "dashboard"), ("/funding", "funding"), ("/stablecoin", "stablecoin"),
                     ("/alerts", "alerts")]:
    r = s.get(BASE + path)
    ok = r.status_code == 200 and len(r.text) > 1000
    record(f"GET {path} 页面渲染", ok, f"HTTP {r.status_code} bytes={len(r.text)}")

print()
print("=" * 70)
print("PART 7: 登出")
print("=" * 70)
r = s.get(BASE + "/logout", allow_redirects=False)
record("GET /logout 重定向", r.status_code == 302 and "/login" in r.headers.get("Location", ""),
       f"HTTP {r.status_code} -> {r.headers.get('Location')}")
r = s.get(BASE + "/api/summary")
check("登出后 API 拒绝", r, expect_status=401, expect_success=False)

print()
print("=" * 70)
passed = sum(1 for _, ok, _ in results if ok)
failed = [(n, d) for n, ok, d in results if not ok]
print(f"总计: {len(results)} 项 | 通过: {passed} | 失败: {len(failed)}")
if failed:
    print("失败项:")
    for n, d in failed:
        print(f"  - {n}: {d}")
    sys.exit(1)
print("全部端点测试通过")
