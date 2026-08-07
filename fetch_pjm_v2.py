# -*- coding: utf-8 -*-
"""
PJM 오하이오 도매 LMP 월평균 수집기 v2 — 탐색 우선(discovery-first)

■ 왜 필요한가
  HD·GM1(둘 다 오하이오)은 EIA '주 산업용 소매 평균'을 대리지표로 쓰는데, 오하이오는
  소매경쟁 주라 그 평균에 불규칙 레짐 점프가 생겨 ETS MAPE가 높다.
  도매 LMP(원인 변수)를 확보하면 소매를 `a + b×LMP(시차)`로 부분 설명할 수 있어
  예측 정확도를 구조적으로 개선할 수 있다.

■ 대상 노드 (오하이오 사업장 관련)
  · ATSI zone      — 북부 오하이오(FirstEnergy). HD·GM1 소재 권역
  · AEP-DAYTON HUB — 남부/중부 오하이오 허브
  · WESTERN HUB    — PJM 대표 유동성 허브(비교·검증용)

■ 경로 3개 — 어느 게 살아있는지 이 스크립트가 직접 확인해 로그로 알려준다
  R1) EIA API v2 도매가 시리즈  … EIA_API_KEY만 있으면 완전 자동(이미 보유)
  R2) PJM Data Miner 2 API      … PJM_KEY(무료 가입) 필요
  R3) 수동 CSV                  … pjm_csv/ 폴더에 넣으면 aggregate_pjm_csv.py가 처리

  ⚠️ 작성자(Claude)는 사내 프록시로 외부 확인이 불가해 아래 후보는 '추정'이다.
     --discover 가 실제 응답을 로그로 남기므로 첫 실행 로그를 보고 확정한다.

■ 사용
  python fetch_pjm_v2.py --discover   # 경로 탐색만(진단)
  python fetch_pjm_v2.py              # 탐색 + 수집·집계·저장
"""
import os, sys, json, csv, re, io
import datetime as dt
import requests

OUT_JSON  = "pjm_lmp_monthly.json"
OUT_CSV   = "pjm_lmp_monthly.csv"
OUT_PROBE = "pjm_probe.json"

EIA_KEY = os.environ.get("EIA_API_KEY", "").strip()
PJM_KEY = (os.environ.get("PJM_API_KEY") or os.environ.get("PJM_KEY") or "").strip()   # 기존 워크플로 규약=PJM_API_KEY
TIMEOUT = 90
START_YEAR = 2019

# ── R1) EIA API v2 후보 라우트 ───────────────────────────────────────────────
# EIA는 ICE 기반 도매 허브가격을 공개한다. v2 라우트명이 확실하지 않아 후보를 나열하고
# /v2/electricity 의 라우트 목록도 함께 조회해 실제 이름을 로그로 남긴다.
EIA_ROOT = "https://api.eia.gov/v2"
EIA_ROUTE_CANDIDATES = [
    "/electricity/wholesale/prices/data/",
    "/electricity/wholesale-prices/data/",
    "/electricity/rto/daily-region-price/data/",
    "/electricity/rto/region-price/data/",
    "/electricity/rto/daily-interchange/data/",     # 대조용(존재 확인된 계열 형태)
]

# ── R2) PJM Data Miner 2 후보 ───────────────────────────────────────────────
PJM_ROOT = "https://api.pjm.com/api/v1"
PJM_FEEDS = ["da_hrl_lmps", "rt_hrl_lmps", "mnt_da_hrl_lmps"]
NODES = ["ATSI", "AEP-DAYTON HUB", "WESTERN HUB"]


def get(url, **kw):
    try:
        r = requests.get(url, timeout=TIMEOUT, **kw)
        return r.status_code, r.text
    except Exception as e:
        return 0, f"EXC {e}"


def discover():
    found = {"checked_at": dt.datetime.utcnow().isoformat()[:16], "eia": {}, "pjm": {}}

    print("### R1) EIA API v2 — 라우트 목록 조회", flush=True)
    if not EIA_KEY:
        print("  ⚠️ EIA_API_KEY 없음 — Secret 확인(이미 eia-harvester에 등록돼 있어야 함)", flush=True)
    else:
        code, body = get(f"{EIA_ROOT}/electricity/?api_key={EIA_KEY}")
        print(f"  /electricity/ HTTP {code} len={len(body)}", flush=True)
        if code == 200:
            try:
                routes = json.loads(body).get("response", {}).get("routes", [])
                names = [r.get("id") for r in routes]
                print(f"  하위 라우트 {len(names)}개: {names}", flush=True)
                found["eia"]["routes"] = names
                # 가격 관련 라우트만 추려 한 단계 더 들어가 본다
                for n in names:
                    if n and re.search(r"price|wholesale|market", n, re.I):
                        c2, b2 = get(f"{EIA_ROOT}/electricity/{n}/?api_key={EIA_KEY}")
                        print(f"    ↳ {n}: HTTP {c2} len={len(b2)}", flush=True)
                        print(f"      {b2[:400]}", flush=True)
                        found["eia"][n] = {"http": c2, "head": b2[:600]}
            except Exception as e:
                print(f"  라우트 파싱 실패: {e}", flush=True)
                print(f"  본문: {body[:400]}", flush=True)

        print("### R1b) EIA 후보 라우트 직접 호출", flush=True)
        for rt in EIA_ROUTE_CANDIDATES:
            url = f"{EIA_ROOT}{rt}?api_key={EIA_KEY}&frequency=monthly&data[0]=price&length=3"
            code, body = get(url)
            ok = (code == 200 and '"data"' in body)
            print(f"  {'✅' if ok else '❌'} {rt} HTTP {code} len={len(body)}", flush=True)
            if code == 200:
                print(f"      {body[:300]}", flush=True)
            found["eia"][rt] = {"http": code, "head": body[:400]}

    print("### R2) PJM Data Miner 2", flush=True)
    if not PJM_KEY:
        print("  ⚠️ PJM_KEY 없음 — 무료 가입 후 Secret 등록 시 이 경로가 열립니다", flush=True)
        found["pjm"]["key"] = "absent"
    else:
        for feed in PJM_FEEDS:
            url = f"{PJM_ROOT}/{feed}?rowCount=3&format=json"
            code, body = get(url, headers={"Ocp-Apim-Subscription-Key": PJM_KEY})
            print(f"  {'✅' if code==200 else '❌'} {feed} HTTP {code} len={len(body)}", flush=True)
            if code == 200:
                print(f"      {body[:300]}", flush=True)
            found["pjm"][feed] = {"http": code, "head": body[:400]}

    print("### R3) 수동 CSV 폴더", flush=True)
    n = len([f for f in os.listdir("pjm_csv")]) if os.path.isdir("pjm_csv") else -1
    print(f"  pjm_csv/ = {'없음(폴더 미생성)' if n < 0 else str(n) + '개 파일'}"
          f" — 있으면 aggregate_pjm_csv.py 가 처리합니다", flush=True)
    found["pjm"]["manual_csv_files"] = n

    json.dump(found, open(OUT_PROBE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[DISCOVER] {OUT_PROBE} 저장 — 이 파일을 보고 다음 버전에서 경로를 확정한다", flush=True)
    return found


def collect_pjm_api():
    """R2가 열려 있을 때만 동작. 월별 집계까지 수행."""
    if not PJM_KEY:
        return {}, {}
    acc, used = {}, {}
    now = dt.datetime.utcnow()
    for y in range(START_YEAR, now.year + 1):
        for feed in ["mnt_da_hrl_lmps", "da_hrl_lmps"]:
            url = (f"{PJM_ROOT}/{feed}?rowCount=50000&format=json"
                   f"&datetime_beginning_ept={y}-01-01to{y}-12-31")
            code, body = get(url, headers={"Ocp-Apim-Subscription-Key": PJM_KEY})
            if code != 200:
                print(f"[PJM {y}/{feed}] HTTP {code}", flush=True)
                continue
            try:
                items = json.loads(body).get("items", [])
            except Exception as e:
                print(f"[PJM {y}] JSON 실패 {e}", flush=True); continue
            cnt = 0
            for it in items:
                nd = str(it.get("pnode_name") or it.get("pnode_id") or "")
                if not any(k.lower() in nd.lower() for k in NODES):
                    continue
                d = str(it.get("datetime_beginning_ept") or "")[:7].replace("/", "-")
                v = it.get("total_lmp_da", it.get("total_lmp_rt"))
                if not d or v is None:
                    continue
                a = acc.setdefault(nd, {}).setdefault(d, [0.0, 0])
                a[0] += float(v); a[1] += 1; cnt += 1
            print(f"[PJM {y}/{feed}] {cnt}행 채택", flush=True)
            if cnt:
                used[str(y)] = url
                break
    monthly = {n: {ym: round(s / c, 3) for ym, (s, c) in sorted(m.items()) if c}
               for n, m in acc.items()}
    return monthly, used


def main():
    disc = discover()
    if "--discover" in sys.argv:
        return

    monthly, used = collect_pjm_api()
    if not monthly:
        print("!! 자동 수집 0건 — 위 탐색 로그를 보고 경로를 확정해야 합니다.", flush=True)
        print("   당장 쓰려면: dataminer2.pjm.com 에서 ATSI/AEP-DAYTON HUB CSV를 내려받아", flush=True)
        print("   레포 pjm_csv/ 에 올리고 aggregate_pjm_csv.py 를 실행하세요.", flush=True)
        sys.exit(1)

    out = {"source": "PJM Data Miner 2 (DA hourly LMP) — monthly avg",
           "unit": "$/MWh", "nodes": list(monthly.keys()),
           "note": "오하이오(HD·GM1) 소매 예측의 원인 변수. 소매 = a + b×LMP(시차) 회귀에 사용.",
           "used_urls": used, "monthly": monthly}
    json.dump(out, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f); w.writerow(["node", "ym", "lmp_avg_usd_mwh"])
        for n, m in monthly.items():
            for ym, v in m.items():
                w.writerow([n, ym, v])
    print(f"[OK] {OUT_JSON} · { {n: len(m) for n, m in monthly.items()} }", flush=True)


if __name__ == "__main__":
    main()
