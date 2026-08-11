# -*- coding: utf-8 -*-
"""
fetch_eia_daily.py — EIA v2 일별 RTO 수요·발전 (부하 히스토리 10년 확보용)

■ 왜 필요한가
  `fetch_eia_hourly.py` 는 START=2024-01 이라 부하 피처가 **32개월뿐**이고, 02_method §5-2 의
  최소 학습창 48개월을 못 넘겨 백테스트에서 부하를 쓰는 모델이 전부 insufficient_n 이 된다.
  월별 모델에 시간별 해상도는 필요 없다 → **일별로 받으면 2015년부터, 용량은 1/24**.
  PJM 도매 LMP가 확보 불가로 종결됐고 MISO LMP도 8개월뿐이므로(01 §6-①·⑪),
  OH·MI 모델에 남은 원인변수는 가스와 **부하**뿐이다. 부하 히스토리가 곧 모델 성능이다.

■ 라우트는 미확정이다 — 추측을 코드에 박지 않는다
  2026-08-11 `pjm_probe.json` 에서 `/electricity/rto/daily-interchange/` 는 404였다.
  일별 라우트 이름이 확인된 바 없으므로 3단계로 실측한다.
    1) GET /v2/electricity/rto/              → 하위 라우트 전체를 로그에 나열
    2) GET /v2/electricity/rto/{route}/      → 그 라우트가 지원하는 frequency 목록 확인
    3) 후보를 순서대로 3행만 조회해 200 + data 인 것을 채택
  **후보 1번은 기존 `region-data` 라우트에 `frequency=daily`** 다. EIA v2 는 한 라우트가
  여러 frequency 를 지원하는 경우가 많아 이게 가장 유력하다. 2)의 frequency 목록이
  사실상 최종 판정이다.

■ 산출 — 월 집계만 커밋한다
  일별 원본은 4,000일 × 2권역 × 3type ≈ 24,000행(약 850KB)이고 매일 커밋하면 레포가
  비대해진다. 월별 모델에 필요한 건 집계값뿐이므로 다음만 남긴다.
    eia_rto_monthly.csv   ym, respondent, load_mwh, peak_day_mwh, mean_day_mwh,
                          days, df_err, ng_mwh
    eia_rto_monthly.json  채택 라우트·frequency 기록 + 탐색 결과 + 월 집계

■ 시간별 파일과 의미가 다른 컬럼 (혼동 금지)
  `peak_day_mwh` 는 **일 최대 에너지**이고 시간별의 `peak_mw`(순간 최대 수요)가 아니다.
  `load_factor` 도 일 단위라 시간별보다 완만하다. 그래서 build_panel 은 두 소스를
  **각각 다른 var 이름으로** 적재한다(일별은 `d_` 접두어). 시간별 32개월은 진짜 peak_mw
  용도로 계속 유지한다 — 대체가 아니라 병행이다.

■ 의존성: requests. Secret: EIA_API_KEY (기존 키 재사용)
■ 전량 재수집 방식 — EIA는 롤링 보존이 아니므로(MISO·IESO와 다름) 매번 다시 받아도
  같은 결과가 나온다. 증분 상태를 두지 않아 깨질 상태가 없다(자기치유).
"""
import os, sys, json, csv, time
import datetime as dt
import requests

BASE = "https://api.eia.gov/v2"
RESPONDENTS = ["PJM", "MISO"]
TYPES = ["D", "DF", "NG"]          # D=수요, DF=전일예측, NG=순발전
START = os.environ.get("EIA_DAILY_START", "2015-01-01")
PAGE = 5000
TIMEOUT = 90

OUT_CSV = "eia_rto_monthly.csv"
OUT_JSON = "eia_rto_monthly.json"

# (라우트, frequency) 후보 — 앞에서부터 실측해 200+data 인 것을 채택
CANDIDATES = [
    ("electricity/rto/region-data", "daily"),
    ("electricity/rto/daily-region-data", "daily"),
    ("electricity/rto/daily-region-sub-ba-data", "daily"),
]

PROBE = {"checked_at": dt.datetime.now(dt.UTC).isoformat()[:16]}


def get_key():
    k = os.environ.get("EIA_API_KEY", "").strip()   # 끝 개행(%0A) 방지
    if not k:
        print("!! EIA_API_KEY 가 비어있음 (Secret 등록 확인)", flush=True)
        sys.exit(1)
    return k


def api(url, params):
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
    except Exception as e:
        return 0, None, f"EXC {e}"
    if r.status_code != 200:
        return r.status_code, None, r.text[:400]
    try:
        return 200, r.json(), None
    except Exception as e:
        return 200, None, f"JSON 파싱 실패 {e} / {r.text[:300]}"


# ── 1) 하위 라우트 나열 ────────────────────────────────
def list_routes(key):
    code, j, err = api(f"{BASE}/electricity/rto/", {"api_key": key})
    print(f"[탐색1] /electricity/rto/ HTTP {code}", flush=True)
    if code != 200 or not j:
        print(f"   실패: {err}", flush=True)
        PROBE["rto_routes_http"] = code
        return []
    routes = (j.get("response") or {}).get("routes") or []
    ids = [r.get("id") for r in routes if r.get("id")]
    PROBE["rto_routes"] = ids
    print(f"   하위 라우트 {len(ids)}개: {ids}", flush=True)
    daily_like = [i for i in ids if "daily" in i.lower()]
    if daily_like:
        print(f"   ↳ daily 계열: {daily_like}", flush=True)
        PROBE["rto_routes_daily"] = daily_like
    return ids


# ── 2) 라우트가 지원하는 frequency 확인 (사실상 최종 판정) ──
def route_freqs(key, route):
    code, j, err = api(f"{BASE}/{route}/", {"api_key": key})
    if code != 200 or not j:
        print(f"[탐색2] {route} 메타 HTTP {code} — {err}", flush=True)
        return None
    resp = j.get("response") or {}
    freqs = [f.get("id") for f in (resp.get("frequency") or []) if f.get("id")]
    facets = list((resp.get("facets") or {}).keys()) if isinstance(resp.get("facets"), dict) \
        else [f.get("id") for f in (resp.get("facets") or []) if isinstance(f, dict)]
    cols = list((resp.get("data") or {}).keys()) if isinstance(resp.get("data"), dict) else []
    print(f"[탐색2] {route} frequency={freqs} facets={facets} data={cols}", flush=True)
    PROBE.setdefault("route_meta", {})[route] = {"frequency": freqs, "facets": facets,
                                                 "data": cols}
    return freqs


# ── 3) 후보를 3행만 조회해 채택 ────────────────────────
def base_params(key, freq, length):
    p = [("api_key", key), ("frequency", freq), ("data[0]", "value"),
         ("start", START), ("sort[0][column]", "period"),
         ("sort[0][direction]", "asc"), ("length", str(length))]
    for r_ in RESPONDENTS:
        p.append(("facets[respondent][]", r_))
    for t in TYPES:
        p.append(("facets[type][]", t))
    return p


def pick_route(key):
    tried = []
    for route, freq in CANDIDATES:
        freqs = route_freqs(key, route)
        if freqs is not None and freq not in freqs:
            print(f"  ❌ {route} 는 frequency={freq} 미지원 (지원: {freqs})", flush=True)
            tried.append({"route": route, "freq": freq, "reason": f"미지원 {freqs}"})
            continue
        code, j, err = api(f"{BASE}/{route}/data/", base_params(key, freq, 3))
        ok = False
        n = 0
        if code == 200 and j:
            data = ((j.get("response") or {}).get("data") or [])
            n = len(data)
            ok = n > 0
            if ok:
                print(f"  ✅ {route} freq={freq} 채택 · 첫 item="
                      f"{json.dumps(data[0], ensure_ascii=False)}", flush=True)
        if not ok:
            print(f"  ❌ {route} freq={freq} HTTP {code} rows={n} — {err}", flush=True)
        tried.append({"route": route, "freq": freq, "http": code, "rows": n, "ok": ok})
        if ok:
            PROBE["candidates"] = tried
            PROBE["adopted"] = {"route": route, "frequency": freq}
            return route, freq
    PROBE["candidates"] = tried
    print("!! 일별 라우트를 찾지 못했다 — 위 [탐색1] 라우트 목록과 [탐색2] frequency 를 보고 "
          "CANDIDATES 를 갱신해야 한다.", flush=True)
    return None, None


# ── 수집 ─────────────────────────────────────────────
def fetch(key, route, freq):
    rows, offset, total = [], 0, None
    while True:
        p = base_params(key, freq, PAGE) + [("offset", str(offset))]
        code, j, err = api(f"{BASE}/{route}/data/", p)
        if code != 200 or not j:
            print(f"[FETCH] offset={offset} HTTP {code} — {err}", flush=True)
            break
        resp = j.get("response") or {}
        data = resp.get("data") or []
        if total is None:
            total = int(resp.get("total") or 0)
            print(f"[TOTAL] {total} rows · dateFormat={resp.get('dateFormat')}", flush=True)
        rows.extend(data)
        offset += PAGE
        if total and offset >= total:
            break
        if not data:
            break
        time.sleep(0.3)
    print(f"[FETCH] 누적 {len(rows)}행", flush=True)
    return rows


def aggregate(rows):
    """일별 → 월 집계. D/DF 는 날짜 짝을 맞춰야 df_err 이 성립한다."""
    D, DF, NG = {}, {}, {}
    bad = 0
    for d in rows:
        per = str(d.get("period") or "")[:10]
        resp = d.get("respondent")
        typ = d.get("type")
        v = d.get("value")
        if not per or not resp or typ not in ("D", "DF", "NG"):
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            bad += 1
            continue
        {"D": D, "DF": DF, "NG": NG}[typ].setdefault(resp, {})[per] = v
    if bad:
        print(f"[주의] 숫자 아님으로 버린 값 {bad}건", flush=True)

    out = {}
    for resp in sorted(set(list(D.keys()) + list(NG.keys()))):
        dd, df, ng = D.get(resp, {}), DF.get(resp, {}), NG.get(resp, {})
        gD, gErr, gNG = {}, {}, {}
        for per, v in dd.items():
            gD.setdefault(per[:7], []).append(v)
            f = df.get(per)
            if f:                                   # 0 나눗셈 회피
                gErr.setdefault(per[:7], []).append((v - f) / f)
        for per, v in ng.items():
            gNG.setdefault(per[:7], []).append(v)
        m = {}
        for ym in sorted(set(list(gD.keys()) + list(gNG.keys()))):
            vs = gD.get(ym) or []
            rec = {"days": len(vs)}
            if vs:
                rec["load_mwh"] = round(sum(vs), 1)
                rec["peak_day_mwh"] = round(max(vs), 1)
                rec["mean_day_mwh"] = round(sum(vs) / len(vs), 1)
            if gErr.get(ym):
                rec["df_err"] = round(sum(gErr[ym]) / len(gErr[ym]), 6)
            if gNG.get(ym):
                rec["ng_mwh"] = round(sum(gNG[ym]), 1)
            m[ym] = rec
        out[resp] = m
        yms = sorted(m)
        if yms:
            print(f"[집계] {resp:5s} {len(yms):4d}개월 {yms[0]}~{yms[-1]} "
                  f"(일수 최소 {min(m[y]['days'] for y in yms)} / 최대 "
                  f"{max(m[y]['days'] for y in yms)})", flush=True)
    return out


def main():
    key = get_key()
    print(f"=== fetch_eia_daily · START={START} ===", flush=True)
    list_routes(key)
    route, freq = pick_route(key)
    if not route:
        json.dump({"source": "EIA v2 rto (daily)", "adopted": None, "probe": PROBE},
                  open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"[OK] {OUT_JSON} 에 탐색 결과만 저장 — 라우트 확정 후 재실행", flush=True)
        sys.exit(1)

    rows = fetch(key, route, freq)
    if not rows:
        print("!! 수집 0행 — 파라미터/권한 확인 필요", flush=True)
        json.dump({"source": "EIA v2 rto (daily)", "adopted": {"route": route,
                   "frequency": freq}, "probe": PROBE},
                  open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        sys.exit(1)

    monthly = aggregate(rows)
    if not any(monthly.values()):
        print("!! 월 집계 0 — respondent/type 값 확인 필요", flush=True)
        sys.exit(1)

    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["ym", "respondent", "load_mwh", "peak_day_mwh", "mean_day_mwh",
                    "days", "df_err", "ng_mwh"])
        for resp in sorted(monthly):
            for ym in sorted(monthly[resp]):
                r = monthly[resp][ym]
                w.writerow([ym, resp, r.get("load_mwh", ""), r.get("peak_day_mwh", ""),
                            r.get("mean_day_mwh", ""), r.get("days", ""),
                            r.get("df_err", ""), r.get("ng_mwh", "")])

    allyms = sorted({y for m in monthly.values() for y in m})
    json.dump({
        "source": "EIA v2 electricity/rto (daily) — 월 집계",
        "adopted": {"route": route, "frequency": freq},
        "note": "peak_day_mwh 는 일 최대 에너지이며 시간별 peak_mw(순간 최대 수요)가 아니다. "
                "build_panel 은 일별 계열을 d_ 접두어 var 로 적재해 시간별과 분리한다.",
        "respondents": sorted(monthly.keys()), "types": TYPES, "start": START,
        "daily_rows": len(rows), "months": len(allyms),
        "range": [allyms[0], allyms[-1]] if allyms else None,
        "monthly": monthly, "probe": PROBE,
    }, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[OK] {OUT_CSV} · {OUT_JSON} · 일별 {len(rows)}행 → {len(allyms)}개월 "
          f"({allyms[0]}~{allyms[-1]})", flush=True)


if __name__ == "__main__":
    main()
