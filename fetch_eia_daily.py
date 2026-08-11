# -*- coding: utf-8 -*-
"""
fetch_eia_daily.py — EIA v2 일별 RTO 수요·발전 (부하 히스토리 확보용) · v3

■ 왜 필요한가
  `fetch_eia_hourly.py` 는 START=2024-01 이라 부하 피처가 **32개월뿐**이고, 02_method §5-2 의
  최소 학습창 48개월을 못 넘겨 백테스트에서 부하를 쓰는 모델이 전부 insufficient_n 이 된다.
  월별 모델에 시간별 해상도는 필요 없다 → **일별로 받으면 용량이 1/24**.
  PJM 도매 LMP가 확보 불가로 종결됐고 MISO LMP도 8개월뿐이므로(01 §6-①·⑪),
  OH·MI 모델에 남은 원인변수는 가스와 **부하**뿐이다. 부하 히스토리가 곧 모델 성능이다.

■ v2 실행(2026-08-11)에서 확정된 사실 — 추측이 아니라 실측이다
  1) **라우트 = `electricity/rto/daily-region-data`, frequency=`daily`.**
     1순위 가설이던 `region-data`+daily 는 **미지원**이었다(`frequency: ["hourly",
     "local-hourly"]`). 탐색 로직이 이를 잡아 후보 2로 자동 이동했다.
  2) `/electricity/rto/` 하위 라우트 8개 확정 — region-data · fuel-type-data ·
     region-sub-ba-data · interchange-data · daily-region-data ·
     daily-region-sub-ba-data · daily-fuel-type-data · **daily-interchange-data**.
     (과거 pjm_probe 에서 `daily-interchange` 가 404였던 이유 = `-data` 접미사 누락)
  3) 데이터 시작은 **2019-01** 이다(START=2015-01-01 로 요청해도 그 이전은 없음).
     확보 92개월(2019-01~2026-08) → 48개월 학습창을 넘어 §5-5 게이트를 통과한다.
  4) ⚠️ **`timezone` facet 이 있다.** 필터 없이 받으면 같은 (날짜·권역·type) 이
     timezone 값마다 반복된다. v2 실측 82,454행 ÷ 예상 16,680행 = **4.94배**.
     v2 의 집계는 dict 덮어쓰기라 **마지막으로 순회된 timezone 값만 남았고**, 응답 순서가
     바뀌면 값도 바뀌어 **재현 불가**였다(절대원칙 1: 추적 가능성 위반).

■ v3가 고치는 것
  A) `GET /v2/{route}/facet/timezone/` 로 **유효값을 실측**해 로그에 찍는다.
  B) 우선순위(Eastern → Central → Mountain → Pacific → Arizona)로 **하나를 고정**해
     `facets[timezone][]` 에 넣고, 채택값을 `adopted.timezone` 으로 산출물에 남긴다.
     Eastern 우선 근거 — PJM 은 EPT 기준이고 MISO 는 Eastern/Central 이 걸쳐 있지만,
     **월 집계에서 하루 경계 차이는 거의 상쇄**된다. 중요한 건 어느 쪽이냐가 아니라
     전 구간·전 권역이 **동일 기준**이고 그 값이 기록된다는 점이다.
  C) 중복 자동 감지 2중 — ① 같은 (날짜·권역·type) 이 덮어써지면 건수를 세어 경고
     ② 예상 최대 행수 대비 실제 비율을 로그에 찍는다(2배 이상이면 facet 중복).
  D) 응답에 실제로 섞여 온 timezone 분포를 probe 에 남긴다.

■ 산출 — 월 집계만 커밋한다
  일별 원본은 매일 커밋하면 레포가 비대해진다. 월별 모델에 필요한 건 집계값뿐이다.
    eia_rto_monthly.csv   ym, respondent, load_mwh, peak_day_mwh, mean_day_mwh,
                          days, df_err, ng_mwh
    eia_rto_monthly.json  채택 라우트·frequency·timezone + 탐색 결과 + 검산 + 월 집계

■ 시간별 파일과 의미가 다른 컬럼 (혼동 금지)
  `peak_day_mwh` 는 **일 최대 에너지**이고 시간별의 `peak_mw`(순간 최대 수요)가 아니다.
  그래서 build_panel 은 두 소스를 **각각 다른 var 이름으로** 적재한다(일별은 `d_` 접두어).
  시간별 32개월은 진짜 peak_mw 용도로 계속 유지한다 — 대체가 아니라 병행이다.

■ 진단 안전망
  줄 끝 백슬래시(줄이음)를 쓰지 않는다 — 뒤에 공백 한 칸만 붙어도 SyntaxError 라
  모듈이 컴파일 단계에서 죽고 산출 파일이 하나도 안 생겨 원인 추적이 불가능해진다.
  main() 을 try/except 로 감싸 **어떤 오류에도 eia_rto_monthly.json 에 traceback 을 남긴다**.

■ 의존성: requests. Secret: EIA_API_KEY (기존 키 재사용)
■ 전량 재수집 — EIA는 롤링 보존이 아니므로(MISO·IESO와 다름) 매번 받아도 같은 결과다.
"""
import os, sys, json, csv, time, traceback
import datetime as dt
import requests

BASE = "https://api.eia.gov/v2"
ROUTE = "electricity/rto/daily-region-data"      # v2 실측 확정
FREQ = "daily"
RESPONDENTS = ["PJM", "MISO"]
TYPES = ["D", "DF", "NG"]          # D=수요, DF=전일예측, NG=순발전
START = os.environ.get("EIA_DAILY_START", "2015-01-01")
PAGE = 5000
TIMEOUT = 90

# 하루 경계 기준. 앞에서부터 실측 유효값과 대조해 채택한다.
TZ_PRIORITY = ["Eastern", "Central", "Mountain", "Pacific", "Arizona"]

# 라우트가 바뀔 때만 쓰는 폴백 후보(1순위는 위 ROUTE)
CANDIDATES = [
    (ROUTE, FREQ),
    ("electricity/rto/daily-region-sub-ba-data", "daily"),
    ("electricity/rto/region-data", "daily"),
]

OUT_CSV = "eia_rto_monthly.csv"
OUT_JSON = "eia_rto_monthly.json"

PROBE = {"checked_at": dt.datetime.now(dt.UTC).isoformat()[:16]}


def dump_probe(adopted=None, extra=None):
    """어떤 경로로 끝나든 진단 파일을 남긴다."""
    out = {"source": "EIA v2 electricity/rto (daily) — 월 집계",
           "adopted": adopted, "probe": PROBE}
    if extra:
        out.update(extra)
    json.dump(out, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def get_key():
    k = os.environ.get("EIA_API_KEY", "").strip()   # 끝 개행(%0A) 방지
    if not k:
        print("!! EIA_API_KEY 가 비어있음 (Secret 등록 확인)", flush=True)
        PROBE["fatal"] = "EIA_API_KEY empty"
        dump_probe()
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
        PROBE["rto_routes_err"] = str(err)[:300]
        return []
    routes = (j.get("response") or {}).get("routes") or []
    ids = [r.get("id") for r in routes if isinstance(r, dict) and r.get("id")]
    PROBE["rto_routes"] = ids
    print(f"   하위 라우트 {len(ids)}개: {ids}", flush=True)
    return ids


# ── 2) 라우트 메타 — frequency·facets 확인 ──────────────
def route_meta(key, route):
    code, j, err = api(f"{BASE}/{route}/", {"api_key": key})
    if code != 200 or not j:
        print(f"[탐색2] {route} 메타 HTTP {code} — {err}", flush=True)
        return None, []
    resp = j.get("response") or {}
    freqs = [f.get("id") for f in (resp.get("frequency") or [])
             if isinstance(f, dict) and f.get("id")]
    # facets·data 는 dict 일 때도 list 일 때도 있다. 줄이음 없이 분기한다.
    fac = resp.get("facets")
    if isinstance(fac, dict):
        facets = sorted(fac.keys())
    elif isinstance(fac, list):
        facets = [f.get("id") for f in fac if isinstance(f, dict)]
    else:
        facets = []
    dat = resp.get("data")
    if isinstance(dat, dict):
        cols = sorted(dat.keys())
    else:
        cols = []
    print(f"[탐색2] {route} frequency={freqs} facets={facets} data={cols}", flush=True)
    PROBE.setdefault("route_meta", {})[route] = {"frequency": freqs, "facets": facets,
                                                 "data": cols}
    return freqs, facets


# ── A) timezone 유효값 실측 ────────────────────────────
def facet_values(key, route, facet):
    code, j, err = api(f"{BASE}/{route}/facet/{facet}/", {"api_key": key})
    if code != 200 or not j:
        print(f"[TZ] facet 조회 HTTP {code} — {err}", flush=True)
        return []
    resp = j.get("response") or {}
    items = resp.get("facets")
    if isinstance(items, dict):
        items = items.get("facets") or []
    if not isinstance(items, list):
        items = []
    out = []
    for it in items:
        if isinstance(it, dict):
            v = it.get("id") or it.get("name")
            if v:
                out.append(v)
        elif isinstance(it, str):
            out.append(it)
    return out


# ── B) 하나를 고정한다 ─────────────────────────────────
def resolve_timezone(key, route, facets):
    if "timezone" not in (facets or []):
        print("[TZ] 이 라우트에 timezone facet 없음 — 필터 불필요", flush=True)
        return None
    vals = facet_values(key, route, "timezone")
    PROBE["timezone_values"] = vals
    if not vals:
        print("!! [TZ] 유효값 목록을 못 받았다 — 필터 없이 진행하면 (날짜·권역·type) 이 "
              "timezone 수만큼 중복되어 집계가 재현 불가해진다. 아래 [중복]·[검산] 확인 필수.",
              flush=True)
        return None
    print(f"[TZ] 유효값 {len(vals)}개: {vals}", flush=True)
    for want in TZ_PRIORITY:
        for v in vals:
            if str(v).strip().lower() == want.lower():
                print(f"[TZ] '{v}' 고정 — 전 구간·전 권역 동일 기준", flush=True)
                return v
    print(f"[TZ] 우선순위에 없는 값들 — 첫 값 '{vals[0]}' 고정", flush=True)
    return vals[0]


# ── 3) 라우트 채택 ────────────────────────────────────
def base_params(key, freq, length, tz=None):
    p = [("api_key", key), ("frequency", freq), ("data[0]", "value"),
         ("start", START), ("sort[0][column]", "period"),
         ("sort[0][direction]", "asc"), ("length", str(length))]
    for r_ in RESPONDENTS:
        p.append(("facets[respondent][]", r_))
    for t in TYPES:
        p.append(("facets[type][]", t))
    if tz:
        p.append(("facets[timezone][]", tz))
    return p


def pick_route(key):
    tried = []
    for route, freq in CANDIDATES:
        freqs, facets = route_meta(key, route)
        if freqs is not None and freq not in freqs:
            print(f"  ❌ {route} 는 frequency={freq} 미지원 (지원: {freqs})", flush=True)
            tried.append({"route": route, "freq": freq, "reason": f"미지원 {freqs}"})
            continue
        tz = resolve_timezone(key, route, facets)
        code, j, err = api(f"{BASE}/{route}/data/", base_params(key, freq, 3, tz))
        ok = False
        n = 0
        if code == 200 and j:
            data = (j.get("response") or {}).get("data") or []
            n = len(data)
            ok = n > 0
            if ok:
                print(f"  ✅ {route} freq={freq} tz={tz} 채택 · 첫 item="
                      f"{json.dumps(data[0], ensure_ascii=False)}", flush=True)
        if not ok:
            print(f"  ❌ {route} freq={freq} HTTP {code} rows={n} — {err}", flush=True)
        tried.append({"route": route, "freq": freq, "tz": tz, "http": code,
                      "rows": n, "ok": ok})
        if ok:
            PROBE["candidates"] = tried
            return route, freq, tz
    PROBE["candidates"] = tried
    print("!! 일별 라우트를 찾지 못했다 — [탐색1] 라우트 목록과 [탐색2] frequency 를 보고 "
          "CANDIDATES 를 갱신해야 한다.", flush=True)
    return None, None, None


# ── 수집 ─────────────────────────────────────────────
def fetch(key, route, freq, tz):
    rows, offset, total = [], 0, None
    while True:
        p = base_params(key, freq, PAGE, tz) + [("offset", str(offset))]
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


# ── C·D) 집계 + 중복 감지 ──────────────────────────────
def aggregate(rows):
    D, DF, NG = {}, {}, {}
    bad = dup = 0
    tzs = {}
    for d in rows:
        per = str(d.get("period") or "")[:10]
        resp = d.get("respondent")
        typ = d.get("type")
        v = d.get("value")
        z = d.get("timezone")
        if z:
            tzs[z] = tzs.get(z, 0) + 1
        if not per or not resp or typ not in ("D", "DF", "NG"):
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            bad += 1
            continue
        tgt = {"D": D, "DF": DF, "NG": NG}[typ].setdefault(resp, {})
        if per in tgt:
            dup += 1
        tgt[per] = v
    if bad:
        print(f"[주의] 숫자 아님으로 버린 값 {bad}건", flush=True)
    if tzs:
        print(f"[TZ] 응답에 실제로 온 timezone 분포: {tzs}", flush=True)
        PROBE["timezone_in_response"] = tzs
    if dup:
        print(f"!! [중복] 같은 (날짜·권역·type) 이 {dup}건 덮어써졌다 — timezone 필터가 "
              f"안 걸렸거나 값이 여러 개다. 집계가 재현 불가하므로 채택하지 말 것.", flush=True)
        PROBE["duplicate_overwrites"] = dup
    else:
        print("[중복] 0건 — 날짜·권역·type 유일성 확보", flush=True)
        PROBE["duplicate_overwrites"] = 0

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


def rowcount_check(n_rows):
    """예상 최대 행수 대비 실제. 2배 이상이면 facet 중복이다.
       데이터 시작이 START 보다 늦으면 1보다 작게 나오는 것이 정상이다."""
    try:
        d0 = dt.date.fromisoformat(START)
    except Exception:
        return
    days = (dt.date.today() - d0).days + 1
    expect = days * len(RESPONDENTS) * len(TYPES)
    ratio = (n_rows / expect) if expect else 0
    print(f"[검산] 실제 {n_rows}행 / START 기준 예상 최대 {expect}행 = {ratio:.2f}배 "
          f"(1.0 이하 정상 · 2배 이상이면 facet 중복)", flush=True)
    PROBE["rowcount_check"] = {"actual": n_rows, "expect_max": expect,
                              "ratio": round(ratio, 3)}


def main():
    key = get_key()
    print(f"=== fetch_eia_daily v3 · START={START} ===", flush=True)
    list_routes(key)
    route, freq, tz = pick_route(key)
    if not route:
        dump_probe(None)
        print(f"[OK] {OUT_JSON} 에 탐색 결과만 저장 — 라우트 확정 후 재실행", flush=True)
        sys.exit(1)

    adopted = {"route": route, "frequency": freq, "timezone": tz}
    rows = fetch(key, route, freq, tz)
    rowcount_check(len(rows))
    if not rows:
        print("!! 수집 0행 — 파라미터/권한 확인 필요", flush=True)
        dump_probe(adopted)
        sys.exit(1)

    monthly = aggregate(rows)
    if not any(monthly.values()):
        print("!! 월 집계 0 — respondent/type 값 확인 필요", flush=True)
        dump_probe(adopted, {"daily_rows": len(rows)})
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
    dump_probe(adopted, {
        "note": "peak_day_mwh 는 일 최대 에너지이며 시간별 peak_mw(순간 최대 수요)가 아니다. "
                "build_panel 은 일별 계열을 d_ 접두어 var 로 적재해 시간별과 분리한다. "
                "하루 경계는 adopted.timezone 기준이다.",
        "respondents": sorted(monthly.keys()), "types": TYPES, "start": START,
        "daily_rows": len(rows), "months": len(allyms),
        "range": [allyms[0], allyms[-1]] if allyms else None,
        "monthly": monthly,
    })
    print(f"[OK] {OUT_CSV} · {OUT_JSON} · 일별 {len(rows)}행 → {len(allyms)}개월 "
          f"({allyms[0]}~{allyms[-1]}) · tz={tz}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        traceback.print_exc()
        PROBE["fatal"] = f"{type(e).__name__}: {e}"
        dump_probe(None)
        print(f"[OK] {OUT_JSON} 에 예외 기록 저장 — probe.fatal 확인", flush=True)
        sys.exit(1)
