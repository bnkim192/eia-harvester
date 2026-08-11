# -*- coding: utf-8 -*-
"""
환율 시계열 수집기 — ₩/현지통화 일별·월별 히스토리 (ECB 참조환율 · Frankfurter · 키 불필요)

■ 왜 필요한가
  기존 fetch_fx.py 는 /latest 만 받아 매일 같은 파일을 덮어쓴다 → 과거 환율 이력이
  어디에도 남지 않아 ₩ 환산 백테스트가 불가능하다. 이 스크립트가 2015-01-01~현재
  이력을 만든다. fetch_fx.py 는 손대지 않는다(HTML override 소스 계약 유지).

■ 산출
  fx_krw_daily.csv    date, ccy, krw_per_unit
  fx_krw_monthly.csv  ym, ccy, fx_krw_avg, fx_krw_eom, n_days, status   ← 패널 적재 대상
  fx_krw_history.json 채택 라우트 기록 + 월별 시계열

■ 계산 규칙 (틀리기 쉬운 지점)
  1) 날짜별로 먼저 ₩/c 를 환산한 뒤 월 집계한다.
     ₩/EUR 월평균 ÷ (c/EUR) 월평균 은 틀린 값이다 — 평균의 비율 ≠ 비율의 평균.
  2) 전력비는 flow 이므로 월평균(fx_krw_avg)이 기본, 월말(fx_krw_eom)은 대조용.
  3) ECB 는 영업일만 발표한다(주말·유럽 공휴일 없음) → n_days 를 함께 남겨
     결측월과 짧은 월을 구분한다.

■ 부분월 가드 (v2 추가) — 이게 없으면 1일 표본 월이 정상월처럼 패널에 섞인다
  4) START_DATE 가 ECB 휴일이면 Frankfurter 가 **직전 영업일을 딸려 보낸다.**
     예: FX_START=2015-01-01 → 2014-12-31 이 응답에 들어오고 2014-12 가
     n_days=1 인 월로 생성된다. → START_DATE 의 월(START_YM) 보다 이른 ym 은 **버린다.**
  5) status 3단계로 표기한다.
       ok      : n_days >= MIN_DAYS (기본 15) — 패널 적재 대상
       partial : n_days <  MIN_DAYS — 적재하지 않는다(또는 적재 시 반드시 플래그 유지)
       running : 조회 시점의 당월 — 월이 아직 끝나지 않아 평균이 확정 아님
     **패널(panel_monthly.csv)에는 status=ok 만 넣는다.** partial·running 은 참고용.

■ 진단 우선 (blind 수정 금지)
  [PROBE]/[ROUTE] 어느 후보가 살았는지 · [FIRST KEY] rates 첫 날짜·값 ·
  [FETCH] 연도별 일수 · [DROP] 경계월 제외 · [PARTIAL] 짧은 월 목록 ·
  [통화별 결측] · [월 수] · [검산] 최신월 ₩/CAD
  사내망에서 외부 확인이 불가해 아래 라우트는 '후보'다. 첫 실행 로그가 최종 판정이다.
"""
import os, sys, json, csv, time
import datetime as dt
import requests

# ── 설정 ─────────────────────────────────────────────
CCYS       = ["USD", "EUR", "PLN", "CNY", "CAD", "IDR"]   # 파일럿 FX 키 (fetch_fx.py 와 동일)
NON_EUR    = [c for c in CCYS if c != "EUR"]
TO_PARAM   = ",".join(["KRW"] + NON_EUR)                  # EUR 는 base 라 to 에 넣지 않는다
START_DATE = os.environ.get("FX_START", "2015-01-01")     # Frankfurter 이력 시작은 1999-01-04
START_YM   = START_DATE[:7]                               # 경계월 절단 기준 (docstring 4)
MIN_DAYS   = int(os.environ.get("FX_MIN_DAYS", "15"))     # ok/partial 경계 (docstring 5)

# (이름, URL 템플릿, 종료일 사용여부)
ROUTES = [
    ("C1", "https://api.frankfurter.app/{start}..{end}",    True),
    ("C2", "https://api.frankfurter.app/{start}..",         False),
    ("C3", "https://api.frankfurter.dev/v1/{start}..{end}", True),
]

OUT_DAILY   = "fx_krw_daily.csv"
OUT_MONTHLY = "fx_krw_monthly.csv"
OUT_JSON    = "fx_krw_history.json"


def call(url):
    return requests.get(url, params={"from": "EUR", "to": TO_PARAM}, timeout=90)


def probe():
    """최근 30일로 라우트를 실제 확인한다. rates 가 '날짜 → {통화:값}' 중첩이어야 채택."""
    today = dt.date.today()
    s = (today - dt.timedelta(days=30)).isoformat()
    for name, tpl, has_end in ROUTES:
        url = tpl.format(start=s, end=today.isoformat())
        try:
            r = call(url)
            print(f"[PROBE] {name} HTTP {r.status_code} len={len(r.text)}", flush=True)
            if r.status_code != 200:
                print("[BODY]", r.text[:300], flush=True)
                continue
            rates = r.json().get("rates", {})
            if rates and isinstance(next(iter(rates.values())), dict):
                k = sorted(rates)[0]
                print(f"[ROUTE] {name} 채택", flush=True)
                print(f"[FIRST KEY] {k} -> {json.dumps(rates[k], ensure_ascii=False)}", flush=True)
                return name, tpl, has_end
            print(f"[PROBE] {name} 200 이지만 rates 가 날짜중첩 구조가 아님 — 다음 후보", flush=True)
        except Exception as e:
            print(f"[PROBE] {name} 예외 {e}", flush=True)
    print("!! 살아있는 라우트 없음 — ROUTES 후보 갱신 필요", flush=True)
    sys.exit(1)


def collect(tpl, has_end):
    """연도 단위 전량 재수집(증분 상태 없음 → 자기치유). C2 는 종료일이 없어 단일 호출."""
    today = dt.date.today()
    y0 = int(START_DATE[:4])
    rates = {}
    if not has_end:
        r = call(tpl.format(start=START_DATE))
        got = r.json().get("rates", {}) if r.status_code == 200 else {}
        print(f"[FETCH] open-ended HTTP {r.status_code} days={len(got)}", flush=True)
        rates.update(got)
    else:
        for y in range(y0, today.year + 1):
            s = START_DATE if y == y0 else f"{y}-01-01"
            e = today.isoformat() if y == today.year else f"{y}-12-31"
            r = call(tpl.format(start=s, end=e))
            got = r.json().get("rates", {}) if r.status_code == 200 else {}
            print(f"[FETCH] {y} HTTP {r.status_code} days={len(got)}", flush=True)
            if r.status_code != 200:
                print("[BODY]", r.text[:200], flush=True)
            rates.update(got)
            time.sleep(0.3)
    return rates


def convert(rates):
    """날짜별 ₩/c 환산 → 월 집계. ₩/c = (₩/EUR) ÷ (c/EUR)."""
    today_ym = dt.date.today().strftime("%Y-%m")
    daily, acc = [], {}
    missing = {c: 0 for c in CCYS}
    for d in sorted(rates):
        row = rates[d] or {}
        eur_krw = row.get("KRW")                 # 1 EUR = ? KRW
        if not eur_krw:
            for c in CCYS:
                missing[c] += 1
            continue
        for c in CCYS:
            if c == "EUR":
                v = eur_krw                      # ₩/EUR 는 rates.KRW 그대로
            else:
                per_eur = row.get(c)             # c per 1 EUR
                v = (eur_krw / per_eur) if per_eur else None
            if v is None:
                missing[c] += 1
                continue
            v = round(v, 4)
            daily.append((d, c, v))
            a = acc.setdefault((d[:7], c), [0.0, 0, None])
            a[0] += v
            a[1] += 1
            a[2] = v                             # 날짜 오름차순 순회라 마지막 값 = 월말 영업일

    monthly, dropped = [], {}
    for (ym, c), (s, n, eom) in sorted(acc.items()):
        if not n:
            continue
        if ym < START_YM:                        # docstring 4 — 경계월 절단
            dropped[ym] = dropped.get(ym, 0) + 1
            continue
        if ym == today_ym:
            status = "running"
        elif n >= MIN_DAYS:
            status = "ok"
        else:
            status = "partial"
        monthly.append((ym, c, round(s / n, 4), eom, n, status))
    return daily, monthly, missing, dropped


def main():
    name, tpl, has_end = probe()
    rates = collect(tpl, has_end)
    if not rates:
        print("!! 수집 0일 — 라우트/파라미터 확인 필요", flush=True)
        sys.exit(1)

    daily, monthly, missing, dropped = convert(rates)
    if not monthly:
        print("!! 월 집계 0 — KRW 응답 확인 필요", flush=True)
        sys.exit(1)

    if dropped:
        print(f"[DROP] START_YM({START_YM}) 이전 경계월 제외: "
              f"{ {k: v for k, v in sorted(dropped.items())} }  "
              f"(원인: START_DATE 가 ECB 휴일이면 직전 영업일이 딸려온다)", flush=True)

    print("[통화별 결측(일수)]", missing, flush=True)
    yms = sorted({m[0] for m in monthly})
    print(f"[월 수] {len(yms)}  range={yms[0]}~{yms[-1]}  영업일={len(rates)}", flush=True)

    by_status = {}
    for m in monthly:
        by_status[m[5]] = by_status.get(m[5], 0) + 1
    print(f"[STATUS] 행 기준 {by_status}  (MIN_DAYS={MIN_DAYS})", flush=True)
    partial_yms = sorted({m[0] for m in monthly if m[5] == "partial"})
    if partial_yms:
        det = {y: next(m[4] for m in monthly if m[0] == y) for y in partial_yms}
        print(f"[PARTIAL] 영업일 < {MIN_DAYS} 인 월 — 패널 적재 제외: {det}", flush=True)
    run_yms = sorted({m[0] for m in monthly if m[5] == "running"})
    if run_yms:
        det = {y: next(m[4] for m in monthly if m[0] == y) for y in run_yms}
        print(f"[RUNNING] 진행 중인 당월 — 평균 미확정: {det}", flush=True)
    ok_yms = sorted({m[0] for m in monthly if m[5] == "ok"})
    print(f"[적재대상] status=ok 월 {len(ok_yms)}개  range={ok_yms[0]}~{ok_yms[-1]}"
          if ok_yms else "!! status=ok 월이 없다 — MIN_DAYS 또는 라우트 확인", flush=True)

    for c in CCYS:
        if not any(m[1] == c for m in monthly):
            print(f"!! {c} 전 구간 결측 — ECB 참조환율 목록에 없을 수 있음. 이 통화는 수동 대체로 전환", flush=True)

    cad_ok = [m for m in monthly if m[1] == "CAD" and m[5] == "ok"]
    if cad_ok:
        print(f"[검산] 최신 확정월 {cad_ok[-1][0]}  ₩/CAD avg={cad_ok[-1][2]}"
              f"   (OT 앵커 0.0964 CAD/kWh ≈ ₩104.6/kWh 정합 기대치 ≈ 1,085)", flush=True)

    with open(OUT_DAILY, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["date", "ccy", "krw_per_unit"])
        w.writerows(daily)
    with open(OUT_MONTHLY, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["ym", "ccy", "fx_krw_avg", "fx_krw_eom", "n_days", "status"])
        w.writerows(monthly)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump({
            "source": "Frankfurter (ECB reference rates)",
            "route": name,
            "unit": "KRW per 1 unit foreign currency",
            "note": "fx_krw_avg=월평균(기본·flow용) / fx_krw_eom=월말 영업일(대조용)",
            "panel_rule": f"panel_monthly.csv 에는 status=ok 만 적재. partial(<{MIN_DAYS}영업일)·running(당월) 제외",
            "start": START_DATE, "start_ym": START_YM, "min_days": MIN_DAYS,
            "dropped_boundary_months": sorted(dropped.keys()),
            "ccys": CCYS,
            "business_days": len(rates), "months": len(yms),
            "months_ok": len(ok_yms),
            "monthly": [{"ym": a, "ccy": b, "avg": c, "eom": d, "n": n, "status": st}
                        for a, b, c, d, n, st in monthly],
        }, f, ensure_ascii=False)
    print(f"[OK] {OUT_DAILY} ({len(daily)}행) / {OUT_MONTHLY} ({len(monthly)}행) / {OUT_JSON} 저장", flush=True)


if __name__ == "__main__":
    main()
