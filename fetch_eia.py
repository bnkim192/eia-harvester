# -*- coding: utf-8 -*-
"""
EIA 미국 산업용(IND) 전력 소매요금 수집기 — GitHub Actions에서 실행.
전력단가예측 파일럿(US 법인 backtest)용. 주(州)별 월별 price(cents/kWh) 수집.
"""
import os, sys, json, csv, time
import requests

STATES  = ["AZ", "GA", "OH", "TN", "MI", "US"]
SECTOR  = "IND"
START   = "2015-01"
BASE    = "https://api.eia.gov/v2/electricity/retail-sales/data/"
OUT_JSON = "eia_industrial_prices_monthly.json"
OUT_CSV  = "eia_industrial_prices_monthly.csv"
PAGE    = 5000


def get_key():
    k = os.environ.get("EIA_API_KEY", "").strip()
    if not k:
        print("!! EIA_API_KEY 가 비어있음 (Secret 등록 확인)", flush=True)
        sys.exit(1)
    return k


def fetch(key):
    params = [
        ("api_key", key),
        ("frequency", "monthly"),
        ("data[0]", "price"),
        ("facets[sectorid][]", SECTOR),
        ("start", START),
        ("sort[0][column]", "period"),
        ("sort[0][direction]", "asc"),
        ("length", str(PAGE)),
    ]
    for s in STATES:
        params.append(("facets[stateid][]", s))

    rows, offset, total = [], 0, None
    while True:
        p = params + [("offset", str(offset))]
        r = requests.get(BASE, params=p, timeout=60)
        if offset == 0:
            print(f"[HTTP] {r.status_code}  URL(키가림)={r.url.split('api_key=')[0]}...", flush=True)
            if r.status_code != 200:
                print("[BODY]", r.text[:800], flush=True)
                r.raise_for_status()
        j = r.json()
        resp = j.get("response", {})
        data = resp.get("data", [])
        if total is None:
            total = int(resp.get("total", 0))
            print(f"[TOTAL] {total} rows, dateFormat={resp.get('dateFormat')}", flush=True)
            if data:
                print("[FIRST ITEM]", json.dumps(data[0], ensure_ascii=False), flush=True)
        rows.extend(data)
        offset += PAGE
        if offset >= total or not data:
            break
        time.sleep(0.3)
    return rows


def main():
    key = get_key()
    rows = fetch(key)
    if not rows:
        print("!! 수집 0행 — 파라미터/권한 확인 필요", flush=True)
        sys.exit(1)

    by_state = {}
    for d in rows:
        by_state[d.get("stateid")] = by_state.get(d.get("stateid"), 0) + 1
    print("[주별 개수]", by_state, flush=True)

    series = {}
    for d in rows:
        st = d.get("stateid")
        series.setdefault(st, []).append({
            "period": d.get("period"),
            "price":  d.get("price"),
        })

    out = {
        "source": "EIA v2 electricity/retail-sales",
        "sector": SECTOR,
        "unit":   "cents per kWh",
        "states": STATES,
        "rows":   len(rows),
        "series": series,
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[OK] {OUT_JSON} 저장 ({len(rows)}행)", flush=True)

    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["period", "stateid", "price_cents_per_kwh"])
        for d in sorted(rows, key=lambda x: (x.get("stateid",""), x.get("period",""))):
            w.writerow([d.get("period"), d.get("stateid"), d.get("price")])
    print(f"[OK] {OUT_CSV} 저장", flush=True)


if __name__ == "__main__":
    main()
