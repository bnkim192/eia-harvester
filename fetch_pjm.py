# -*- coding: utf-8 -*-
"""
PJM Day-Ahead Hourly LMP 수집기 — GitHub Actions에서 실행.
사내망/작업환경 웹차단과 무관(깃허브 러너가 대신 호출→커밋). EIA·FX 하베스터와 동일 방식.

- Secret: PJM_API_KEY  (Data Miner 2 구독키 Ocp-Apim-Subscription-Key)
  발급: pjm.com 무료 계정(Account Manager) → Data Miner 2 API Access → Subscription Key 복사
- 대상 노드:
    ATSI            = GM1(오하이오·FirstEnergy 오하이오에디슨) 실제 존
    AEP-DAYTON HUB  = WoodMac PPA 매핑 참조용(비교)
  ※ 미시간(MIL/MIH)·인디애나(HD)는 MISO라 PJM에 없음 → 별도 MISO 하베스터 필요
- 산출: pjm_lmp_monthly.json (+ .csv)  = 노드별 월평균 Day-Ahead LMP ($/MWh)
  → 파일럿 ⟳업데이트 탭(대상=US GM1)에 'YYYY-MM,값'으로 붙여넣기 or raw fetch
"""
import os, sys, json, csv, time, re
import requests

NODES      = ["ATSI", "AEP-DAYTON HUB"]   # 필요시 가감
START_YEAR = 2020
END_YEAR   = 2026
BASE       = "https://api.pjm.com/api/v1/da_hrl_lmps"
FIELDS     = "datetime_beginning_ept,pnode_name,total_lmp_da"
OUT_JSON   = "pjm_lmp_monthly.json"
OUT_CSV    = "pjm_lmp_monthly.csv"
ROWCOUNT   = 50000
SLEEP      = 11          # 비회원 분당 6회 제한 → 안전하게 11초 간격


def get_key():
    k = os.environ.get("PJM_API_KEY", "").strip()
    if not k:
        # 키 없으면 API 경로는 건너뜀(워크플로 실패 아님). 키리스 경로=aggregate_pjm_csv.py 사용.
        print("[SKIP] PJM_API_KEY 없음 → API 수집 생략. (키리스: aggregate_pjm_csv.py로 CSV 집계)", flush=True)
        sys.exit(0)
    return k


def ym_of(s):
    """datetime 문자열에서 'YYYY-MM' 추출 (ISO '2024-01-01T..' 및 M/D/YYYY 둘 다 지원)"""
    s = str(s)
    m = re.match(r"(\d{4})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}"
    return None


def fetch_year(key, node, yr, first):
    headers = {"Ocp-Apim-Subscription-Key": key}
    params = {
        "rowCount": ROWCOUNT,
        "startRow": 1,
        "fields": FIELDS,
        "pnode_name": node,
        "datetime_beginning_ept": f"{yr}-01-01T00:00:00to{yr}-12-31T23:59:59",
    }
    rows, start = [], 1
    while True:
        params["startRow"] = start
        r = requests.get(BASE, params=params, headers=headers, timeout=90)
        if first:
            print(f"[HTTP] {r.status_code}  node={node} yr={yr}  URL(키헤더)={r.url}", flush=True)
            if r.status_code != 200:
                print("[BODY]", r.text[:600], flush=True)
            first = False
        r.raise_for_status()
        j = r.json()
        items = (j.get("items", []) if isinstance(j, dict) else j) or []
        if start == 1 and items:
            print("[FIRST ITEM]", json.dumps(items[0], ensure_ascii=False), flush=True)
        rows.extend(items)
        if len(items) < ROWCOUNT:
            break
        start += ROWCOUNT
        time.sleep(SLEEP)
    return rows


def main():
    key = get_key()
    monthly = {n: {} for n in NODES}   # node -> {ym: [sum, count]}
    first = True
    for node in NODES:
        for yr in range(START_YEAR, END_YEAR + 1):
            rows = fetch_year(key, node, yr, first)
            first = False
            for d in rows:
                ym = ym_of(d.get("datetime_beginning_ept", ""))
                v = d.get("total_lmp_da")
                if ym is None or v is None:
                    continue
                a = monthly[node].setdefault(ym, [0.0, 0])
                a[0] += float(v); a[1] += 1
            print(f"[{node} {yr}] {len(rows)}행 수집", flush=True)
            time.sleep(SLEEP)

    out = {
        "source": "PJM Data Miner 2 da_hrl_lmps (Day-Ahead Hourly LMP) — monthly average",
        "unit": "$/MWh",
        "nodes": NODES,
        "range": f"{START_YEAR}-{END_YEAR}",
        "data": {},
    }
    for node, mm in monthly.items():
        out["data"][node] = {ym: round(s / c, 3) for ym, (s, c) in sorted(mm.items()) if c}
        print(f"[집계] {node}: {len(out['data'][node])}개월", flush=True)

    if not any(out["data"].values()):
        print("!! 월별 집계 0 — 날짜 파라미터/노드명/키 확인 필요", flush=True)
        sys.exit(1)

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"[OK] {OUT_JSON} 저장", flush=True)

    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["node", "ym", "lmp_da_avg_usd_mwh"])
        for node, mm in out["data"].items():
            for ym, v in mm.items():
                w.writerow([node, ym, v])
    print(f"[OK] {OUT_CSV} 저장", flush=True)


if __name__ == "__main__":
    main()
