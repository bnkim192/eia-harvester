# -*- coding: utf-8 -*-
"""
EIA STEO(Short-Term Energy Outlook) forward 수집기 — GitHub Actions에서 실행.
전력단가예측 파일럿의 L1 단기(0~24개월) forward 앵커용.

핵심: STEO는 '실적 + 향후 ~24개월 전망'을 한 시계열로 준다(무료).
      Henry Hub 가스 forward를 받아 → 파일럿이 heat-rate 회귀로 전력 도매로 환산.
      (슈나이더식 forward 방법론을 EIA STEO 무료데이터로 복제 = 중복 지불 회피)

- Secret: EIA_API_KEY  (fetch_eia.py 와 동일 키 재사용)
- 산출: steo_forward_monthly.json  (+ steo_forward_monthly.csv)
- 진단: 첫 실행 로그에 HTTP상태·total·첫 item·시리즈별 개수/마지막period 를 찍는다.
        (시리즈ID가 틀리면 total=0 → 로그 보고 SERIES 조정. blind 수정 반복 금지.)
"""
import os, sys, json, csv, time
import requests

# ── 설정 ─────────────────────────────────────────────
# STEO seriesId. Henry Hub 가스가 미국 전력 forward의 1차 드라이버.
#   NGHHMCF = Henry Hub Natural Gas Spot Price (nominal $/MMBtu, 월별·전망포함)
# 전력 소매/도매 참고 시리즈는 첫 로그로 ID 확인 후 추가(빈 응답이면 자동 skip).
#   env STEO_SERIES 로 콤마구분 오버라이드 가능 (예: "NGHHMCF,ELIND_US")
SERIES  = [s.strip() for s in os.environ.get(
    "STEO_SERIES", "NGHHMCF").split(",") if s.strip()]
START   = "2018-01"      # 회귀 학습에 충분한 히스토리
BASE    = "https://api.eia.gov/v2/steo/data/"
OUT_JSON = "steo_forward_monthly.json"
OUT_CSV  = "steo_forward_monthly.csv"
PAGE    = 5000


def get_key():
    k = os.environ.get("EIA_API_KEY", "").strip()   # 끝 개행(%0A) 방지
    if not k:
        print("!! EIA_API_KEY 가 비어있음 (Secret 등록 확인)", flush=True)
        sys.exit(1)
    return k


def fetch(key):
    params = [
        ("api_key", key),
        ("frequency", "monthly"),
        ("data[0]", "value"),
        ("start", START),
        ("sort[0][column]", "period"),
        ("sort[0][direction]", "asc"),
        ("length", str(PAGE)),
    ]
    for s in SERIES:
        params.append(("facets[seriesId][]", s))

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
            else:
                print("!! data 0행 — SERIES ID 확인 필요:", SERIES, flush=True)
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
        print("!! 수집 0행 — seriesId/권한 확인 필요", flush=True)
        sys.exit(1)

    # 시리즈별 정규화: {seriesId: {label, unit, points:[{period,value}...]}}
    series = {}
    for d in rows:
        sid = d.get("seriesId")
        s = series.setdefault(sid, {
            "label": d.get("seriesDescription") or d.get("seriesId"),
            "unit":  d.get("unit"),
            "points": [],
        })
        v = d.get("value")
        try:
            v = float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            v = None
        s["points"].append({"period": d.get("period"), "value": v})

    # 진단: 시리즈별 개수·마지막 period (전망이 어디까지 오는지 확인)
    for sid, s in series.items():
        pts = s["points"]
        last = pts[-1]["period"] if pts else "-"
        print(f"[시리즈] {sid}  n={len(pts)}  last={last}  unit={s['unit']}", flush=True)

    out = {
        "source": "EIA v2 STEO (Short-Term Energy Outlook, forecast 포함)",
        "note":   "실적+향후~24개월 전망 혼재. 파일럿은 현재월 이후를 forward로 사용.",
        "series": SERIES,
        "rows":   len(rows),
        "data":   series,
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[OK] {OUT_JSON} 저장 ({len(rows)}행)", flush=True)

    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["seriesId", "period", "value", "unit", "label"])
        for sid, s in series.items():
            for p in s["points"]:
                w.writerow([sid, p["period"], p["value"], s["unit"], s["label"]])
    print(f"[OK] {OUT_CSV} 저장", flush=True)


if __name__ == "__main__":
    main()
