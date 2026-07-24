# -*- coding: utf-8 -*-
"""EIA 시간별 RTO 수요·발전 수집기 (PJM=OH, MISO=MI)."""
import os, sys, json, csv, time
import requests

RESPONDENTS = ["PJM", "MISO"]
TYPES       = ["D", "DF", "NG"]
START       = "2024-01-01T00"
BASE        = "https://api.eia.gov/v2/electricity/rto/region-data/data/"
OUT_JSON    = "eia_hourly_rto.json"
OUT_CSV     = "eia_hourly_rto.csv"
PAGE        = 5000


def get_key():
    k = os.environ.get("EIA_API_KEY", "").strip()
    if not k:
        print("!! EIA_API_KEY 가 비어있음", flush=True); sys.exit(1)
    return k


def fetch(key):
    params = [
        ("api_key", key), ("frequency", "hourly"), ("data[0]", "value"),
        ("start", START), ("sort[0][column]", "period"),
        ("sort[0][direction]", "asc"), ("length", str(PAGE)),
    ]
    for r_ in RESPONDENTS: params.append(("facets[respondent][]", r_))
    for t in TYPES: params.append(("facets[type][]", t))
    rows, offset, total = [], 0, None
    while True:
        p = params + [("offset", str(offset))]
        r = requests.get(BASE, params=p, timeout=90)
        if offset == 0:
            print(f"[HTTP] {r.status_code}", flush=True)
            if r.status_code != 200:
                print("[BODY]", r.text[:800], flush=True); r.raise_for_status()
        j = r.json(); resp = j.get("response", {}); data = resp.get("data", [])
        if total is None:
            total = int(resp.get("total", 0))
            print(f"[TOTAL] {total} rows", flush=True)
            if data: print("[FIRST ITEM]", json.dumps(data[0], ensure_ascii=False), flush=True)
        rows.extend(data); offset += PAGE
        if offset >= total or not data: break
        time.sleep(0.3)
    return rows


def main():
    key = get_key(); rows = fetch(key)
    if not rows: print("!! 0행", flush=True); sys.exit(1)
    combo = {}
    for d in rows:
        k = f"{d.get('respondent')}/{d.get('type')}"; combo[k] = combo.get(k, 0) + 1
    print("[권역/type 개수]", combo, flush=True)
    out = {"source": "EIA rto/region-data hourly", "respondents": RESPONDENTS,
           "types": TYPES, "unit": "MWh", "start": START, "rows": len(rows), "data": rows}
    with open(OUT_JSON, "w", encoding="utf-8") as f: json.dump(out, f, ensure_ascii=False)
    print(f"[OK] {OUT_JSON} ({len(rows)}행)", flush=True)
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f); w.writerow(["period", "respondent", "type", "value_MWh"])
        for d in rows: w.writerow([d.get("period"), d.get("respondent"), d.get("type"), d.get("value")])
    print(f"[OK] {OUT_CSV}", flush=True)


if __name__ == "__main__":
    main()
