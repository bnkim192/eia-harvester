# -*- coding: utf-8 -*-
"""
build_panel.py — 전력단가 예측 파일럿 패널 빌더 (v1)

■ 하는 일
  전 수집기의 raw 산출물을 읽어 모델 입력의 단일 진실인 tidy long 패널을 만든다.
  회귀·백테스트는 하지 않는다(그건 fit_model.py). 이 파일이 실패해도 raw는 남고,
  fit_model.py 가 실패해도 패널은 남는 분리 구조다.

  입력 — 같은 레포(로컬 파일)
    eia_industrial_prices_monthly.json   주별 산업용 소매 (cents/kWh)
    eia_hourly_rto.csv                   PJM·MISO 시간별 D/DF/NG (MWh) → 월 집계
    steo_forward_monthly.json            Henry Hub 실적+전망 ($/MMBtu)
    miso_lmp_monthly.json                MICHIGAN.HUB·INDIANA.HUB 월평균 ($/MWh)
    fx_krw_monthly.csv                   ₩/현지통화 월평균·월말 (fetch_fx_history.py)
  입력 — 다른 Public 레포(raw 직접 fetch, 인증 불필요)
    ieso-harvester/ot_wholesale_monthly.json   HOEP·존가격(ZonalPrice) + components
    ieso-harvester/ot_ga_monthly.json          GA Class B 태그별 값
    entsoe-harvester/pl_wholesale_monthly.json 폴란드 day-ahead (EUR/MWh)
  ※ PJM은 확보 불가로 종결(EIA v2 라우트 404 · Data Miner2 로그인 도메인 차단).

  산출
    panel_monthly.csv    ym,market,entity,var,value,unit,kind,vintage,source,status
    steo_vintages.csv    vintage_ym,target_ym,horizon,series_id,value,unit
    status.json          소스별 행수·최신월·상태(HTML 상태 카드용)

■ 설계 결정 3개 (02_method 대비 변경분 — 근거 있음)
  1) 앵커(사업장 회피단가)를 이 스크립트에 넣지 않는다. Public 레포는 전세계 공개이고
     앵커는 사내 고지서 원가다. 따라서 산출물은 '공개데이터 기반 수준·성장경로'까지이고,
     앵커 곱셈은 HTML 또는 사내 경로에서 수행한다(공개/민감 분리 원칙).
  2) 시차 피처(hh_gas_l1..l3 등)를 패널에 저장하지 않는다. 파생값을 넣으면 패널이 4배로
     커지고 원본과 이중관리가 된다. 시차는 fit_model.py 가 계산한다.
  3) GA는 적재 게이트를 통과한 값만 status=ok 로 적재한다.
     ga_rate = ActualRate(확정) → 없으면 SecondEstimateRate(forecast).
     게이트: 총액·요율 부호 일치 AND |총액/요율| 이 8~14 TWh (온타리오 Class B 물량 규모).
     FirstEstimateRate 는 2개월 전 ActualRate 를 소수점까지 복사한 사례가 4건 있어
     예측값으로 신뢰할 수 없다 → 적재하되 var 이름을 분리하고 모델 입력에서 제외한다.

■ 적재 규칙 (용량 통제)
  매 실행 전량 append 하면 파일이 폭증한다. (ym,market,entity,var) 의 **직전 저장값과
  다를 때만** 새 vintage 행을 추가하는 변경이력 방식이다. 매월 갱신되는 STEO forward
  곡선은 패널에 넣지 않고 steo_vintages.csv 로 분리한다(백테스트의 point-in-time 소스).

■ 의존성: requests 만. (numpy 는 fit_model.py 에서만 필요)
■ 진단 우선: 소스별 [LOAD] 행수·최신월·상태를 print 한다. 로컬에 Python이 없어
  첫 Actions 실행이 사실상 문법검사다 → 단계마다 예외를 잡아 어디서 죽었는지 남긴다.
"""
import os, sys, json, csv, io
import datetime as dt
import requests

# ── 설정 ─────────────────────────────────────────────
OWNER, BRANCH = "bnkim192", "main"
RAW = "https://raw.githubusercontent.com/{o}/{r}/{b}/{f}"
IESO_REPO, ENTSOE_REPO = "ieso-harvester", "entsoe-harvester"
TIMEOUT = 90

OUT_PANEL = "panel_monthly.csv"
OUT_VINT  = "steo_vintages.csv"
OUT_STATUS = "status.json"

PANEL_COLS = ["ym", "market", "entity", "var", "value", "unit",
              "kind", "vintage", "source", "status"]
VINT_COLS = ["vintage_ym", "target_ym", "horizon", "series_id", "value", "unit"]

NOW = dt.datetime.now(dt.UTC)
TODAY = NOW.date().isoformat()
CUR_YM = f"{NOW.year:04d}-{NOW.month:02d}"

# GA 적재 게이트 — 온타리오 Class B 월 물량 범위(TWh). 12개월 실측 8.79~12.25 에서 여유.
GA_VOL_MIN, GA_VOL_MAX = 8.0, 14.0

STATUS = {}
NOTES = []


def note(msg):
    NOTES.append(msg)
    print(f"[NOTE] {msg}", flush=True)


def ym_index(ym):
    return int(ym[:4]) * 12 + int(ym[5:7]) - 1


def fmt(v):
    """부동소수 잡음으로 '변경'이 오탐되지 않게 표기를 고정한다."""
    if v is None:
        return ""
    return f"{float(v):.6g}"


# ── 로더 공통 ─────────────────────────────────────────
def local_json(path):
    if not os.path.exists(path):
        return None, "파일 없음"
    try:
        return json.load(open(path, encoding="utf-8")), None
    except Exception as e:
        return None, f"JSON 파싱 실패 {e}"


def raw_json(repo, fname):
    url = RAW.format(o=OWNER, r=repo, b=BRANCH, f=fname) + f"?t={int(NOW.timestamp())}"
    try:
        r = requests.get(url, timeout=TIMEOUT)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        return r.json(), None
    except Exception as e:
        return None, f"EXC {e}"


def report(name, rows, err=None, extra=None):
    yms = sorted({r["ym"] for r in rows}) if rows else []
    st = {"rows": len(rows), "latest_ym": yms[-1] if yms else None,
          "first_ym": yms[0] if yms else None, "ok": bool(rows) and err is None,
          "note": err or ""}
    if extra:
        st.update(extra)
    STATUS[name] = st
    print(f"[LOAD] {name:22s} rows={len(rows):6d} "
          f"range={st['first_ym']}~{st['latest_ym']} {'OK' if st['ok'] else 'FAIL: ' + str(err)}",
          flush=True)
    return rows


def mk(ym, market, entity, var, value, unit, source, kind="actual", status="ok"):
    return {"ym": ym, "market": market, "entity": entity, "var": var,
            "value": fmt(value), "unit": unit, "kind": kind,
            "vintage": TODAY, "source": source, "status": status}


# ── 1) EIA 주별 산업용 소매 ────────────────────────────
def load_retail():
    src = "eia_industrial_prices_monthly.json"
    j, err = local_json(src)
    if j is None:
        return report("eia_retail", [], err)
    rows = []
    for state, pts in (j.get("series") or {}).items():
        for p in pts:
            per, val = p.get("period"), p.get("price")
            if not per or val in (None, ""):
                continue
            rows.append(mk(per[:7], "US-RETAIL", state, "retail_ind", val,
                           "cents/kWh", src))
    return report("eia_retail", rows)


# ── 2) EIA 시간별 RTO → 월 집계 ────────────────────────
def load_rto():
    src = "eia_hourly_rto.csv"
    if not os.path.exists(src):
        return report("eia_hourly_rto", [], "파일 없음")
    # H[(resp, typ)][period] = value
    H = {}
    try:
        with open(src, encoding="utf-8-sig") as f:
            rd = csv.reader(f)
            next(rd, None)
            for r in rd:
                if len(r) < 4:
                    continue
                per, resp, typ, val = r[0].strip(), r[1].strip(), r[2].strip(), r[3].strip()
                try:
                    v = float(val)
                except Exception:
                    continue
                H.setdefault((resp, typ), {})[per] = v
    except Exception as e:
        return report("eia_hourly_rto", [], f"CSV 읽기 실패 {e}")

    resps = sorted({k[0] for k in H})
    rows = []
    for resp in resps:
        D = H.get((resp, "D"), {})
        DF = H.get((resp, "DF"), {})
        NG = H.get((resp, "NG"), {})
        # 월별 그룹
        gD, gNG, gErr = {}, {}, {}
        for per, v in D.items():
            gD.setdefault(per[:7], []).append(v)
        for per, v in NG.items():
            gNG.setdefault(per[:7], []).append(v)
        for per, v in D.items():
            f = DF.get(per)
            if f:                      # 0 나눗셈 회피
                gErr.setdefault(per[:7], []).append((v - f) / f)
        for ym in sorted(gD):
            vs = gD[ym]
            if not vs:
                continue
            mx = max(vs)
            rows.append(mk(ym, "US-RTO", resp, "load_mwh", sum(vs), "MWh", src))
            rows.append(mk(ym, "US-RTO", resp, "peak_mw", mx, "MW", src))
            if mx:
                rows.append(mk(ym, "US-RTO", resp, "load_factor",
                               (sum(vs) / len(vs)) / mx, "ratio", src))
            if ym in gErr and gErr[ym]:
                rows.append(mk(ym, "US-RTO", resp, "df_err",
                               sum(gErr[ym]) / len(gErr[ym]), "ratio", src))
            if ym in gNG and gNG[ym]:
                rows.append(mk(ym, "US-RTO", resp, "ng_net", sum(gNG[ym]), "MWh", src))
    return report("eia_hourly_rto", rows, None, {"respondents": resps})


# ── 3) STEO Henry Hub (패널 + vintage 곡선) ─────────────
def load_steo():
    src = "steo_forward_monthly.json"
    j, err = local_json(src)
    if j is None:
        return report("steo_gas", [], err), []
    rows, vints = [], []
    for sid, s in (j.get("data") or {}).items():
        unit = s.get("unit") or "$/MMBtu"
        for p in s.get("points") or []:
            per, val = p.get("period"), p.get("value")
            if not per or val is None:
                continue
            ym = per[:7]
            # 실적/전망 구분은 휴리스틱: 발표월(=오늘 월) 이전은 실적으로 본다.
            # STEO 실적은 통상 1~2개월 지연이라 최근 1~2개월은 잠정치일 수 있다(status.json 주석).
            kind = "actual" if ym < CUR_YM else "forecast"
            rows.append(mk(ym, "GLOBAL", "US", "hh_gas", val, unit, src, kind))
            if kind == "forecast":
                vints.append({"vintage_ym": CUR_YM, "target_ym": ym,
                              "horizon": ym_index(ym) - ym_index(CUR_YM),
                              "series_id": sid, "value": fmt(val), "unit": unit})
    note("STEO 실적/전망 경계는 '발표월 이전=실적' 휴리스틱이다. 최근 1~2개월은 잠정치 가능.")
    return report("steo_gas", rows), vints


# ── 4) MISO DA LMP ────────────────────────────────────
def load_miso():
    src = "miso_lmp_monthly.json"
    j, err = local_json(src)
    if j is None:
        return report("miso_lmp", [], err)
    rows = []
    for node, m in (j.get("monthly") or {}).items():
        for ym, v in m.items():
            if v is None:
                continue
            rows.append(mk(ym, "US-MISO", node, "lmp_da", v, "$/MWh", src))
    return report("miso_lmp", rows)


# ── 5) FX 시계열 ──────────────────────────────────────
def load_fx():
    src = "fx_krw_monthly.csv"
    if not os.path.exists(src):
        j, err = local_json("fx_krw.json")
        if j is None:
            return report("fx", [], f"{src} 없음 / fx_krw.json {err}")
        # 최신 스냅샷 폴백 — 시계열이 아니므로 백테스트에는 못 쓴다.
        d = j.get("date") or TODAY
        rows = [mk(d[:7], "GLOBAL", c, "fx_krw_avg", v, "KRW/unit", "fx_krw.json",
                   "actual", "partial") for c, v in (j.get("rates") or {}).items()]
        note("fx_krw_monthly.csv 없음 → 최신 스냅샷만 적재(status=partial). ₩환산 백테스트 불가. "
             "fetch_fx_history.py 실행 확인 필요.")
        return report("fx", rows, None, {"fallback": True})
    rows, partial = [], set()
    try:
        with open(src, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                ym, ccy = (r.get("ym") or "").strip(), (r.get("ccy") or "").strip()
                if not ym or not ccy:
                    continue
                # 부분월 가드 — 영업일 15일 미만은 월평균으로 쓸 수 없다.
                # 실측 2014-12는 1일뿐이다(2015-01-01이 ECB 휴일이라 직전 영업일 2014-12-31이
                # 범위 조회에 딸려온 것). 진행 중인 당월도 여기 걸린다.
                try:
                    nd = int(float(r.get("n_days") or 0))
                except Exception:
                    nd = 0
                st = "ok" if nd >= 15 else "partial"
                if st == "partial":
                    partial.add(f"{ym}({nd}일)")
                for col, var in (("fx_krw_avg", "fx_krw_avg"), ("fx_krw_eom", "fx_krw_eom")):
                    v = (r.get(col) or "").strip()
                    if v == "":
                        continue
                    rows.append(mk(ym, "GLOBAL", ccy, var, v, "KRW/unit", src, "actual", st))
    except Exception as e:
        return report("fx", [], f"CSV 읽기 실패 {e}")
    if partial:
        note(f"FX 부분월(영업일<15) status=partial: {sorted(partial)}")
    return report("fx", rows, None, {"partial_months": sorted(partial)})


# ── 6) IESO 온타리오 (HOEP · 존가격 · 성분) ─────────────
def load_ieso():
    src = f"{IESO_REPO}/ot_wholesale_monthly.json"
    j, err = raw_json(IESO_REPO, "ot_wholesale_monthly.json")
    if j is None:
        return report("ieso_price", [], err)
    rows = []
    for s in j.get("series") or []:            # 구 체계 HOEP (≤2025-04)
        if s.get("cad_mwh") is None:
            continue
        rows.append(mk(s["ym"], "CA-IESO", "OT", "ot_energy_hoep", s["cad_mwh"],
                       "CAD/MWh", src))
    for s in j.get("series_zonal") or []:      # 개편 후 ZonalPrice = 총 존 LMP
        if s.get("cad_mwh") is None:
            continue
        r = mk(s["ym"], "CA-IESO", "OT", "ot_energy_zonal", s["cad_mwh"], "CAD/MWh", src)
        if s.get("vintage"):
            r["vintage"] = s["vintage"]
        rows.append(r)
        comp = s.get("components") or {}
        for tag, var in (("LossPriceCapped", "ot_loss"),
                         ("CongestionPriceCapped", "ot_cong")):
            if tag in comp and comp[tag] is not None:
                rows.append(mk(s["ym"], "CA-IESO", "OT", var, comp[tag], "CAD/MWh", src))
    gap = (j.get("gap") or {}).get("months")
    if gap:
        note(f"OT 에너지 계열에 {gap}개월 영구 결손(HOEP 종료~존가격 보존 시작). "
             "두 계열은 가격 정의가 달라 결합하지 않는다.")
    return report("ieso_price", rows, None,
                  {"gap_months": gap, "zonal_months": len(j.get("series_zonal") or []),
                   "hoep_months": len(j.get("series") or [])})


# ── 7) IESO Global Adjustment (Class B) ────────────────
def ga_pick(by_tag):
    """확정 우선. (rate, amount, kind, status, vol_twh) 반환. 게이트는 부호일치+물량범위."""
    def g(k):
        v = by_tag.get(k)
        if isinstance(v, list) and v:
            try:
                return float(v[0])
            except Exception:
                return None
        return None

    for amt_k, rate_k, kind in (("Actual", "ActualRate", "actual"),
                                ("SecondEstimate", "SecondEstimateRate", "forecast")):
        amt, rate = g(amt_k), g(rate_k)
        if amt is None or rate in (None, 0):
            continue
        vol = abs(amt / rate) / 1e6
        ok = ((amt >= 0) == (rate >= 0)) and (GA_VOL_MIN <= vol <= GA_VOL_MAX)
        return rate, amt, kind, ("ok" if ok else "partial"), vol
    return None, None, None, "unavailable", None


def load_ga():
    src = f"{IESO_REPO}/ot_ga_monthly.json"
    j, err = raw_json(IESO_REPO, "ot_ga_monthly.json")
    if j is None:
        return report("ieso_ga", [], err)
    rows, fails = [], []
    for s in j.get("series") or []:
        ym, bt = s.get("ym"), s.get("by_tag") or {}
        if not ym:
            continue
        rate, amt, kind, status, vol = ga_pick(bt)
        if rate is None:
            continue
        r1 = mk(ym, "CA-IESO", "OT", "ga_class_b_rate", rate, "CAD/MWh", src, kind, status)
        r2 = mk(ym, "CA-IESO", "OT", "ga_class_b_amount", amt, "CAD", src, kind, status)
        if s.get("vintage"):
            r1["vintage"] = r2["vintage"] = s["vintage"]
        rows += [r1, r2]
        if status != "ok":
            fails.append({"ym": ym, "rate": rate, "amount": amt,
                          "implied_twh": None if vol is None else round(vol, 2)})
        # 1차추정은 별도 var로 분리 적재(모델 입력 금지 — 직전 확정치 복사 의심)
        fe = bt.get("FirstEstimateRate")
        if isinstance(fe, list) and fe:
            rows.append(mk(ym, "CA-IESO", "OT", "ga_first_estimate_rate", fe[0],
                           "CAD/MWh", src, "forecast", "partial"))
    if fails:
        note(f"GA 적재 게이트 미달 {len(fails)}건(부호 불일치 또는 물량 범위 밖) — "
             f"status=partial 로 표시만 하고 모델에서 제외: {fails}")
    return report("ieso_ga", rows, None, {"gate_fail": fails})


# ── 8) ENTSO-E 폴란드 ─────────────────────────────────
def load_pl():
    src = f"{ENTSOE_REPO}/pl_wholesale_monthly.json"
    j, err = raw_json(ENTSOE_REPO, "pl_wholesale_monthly.json")
    if j is None:
        return report("entsoe_pl", [], err)
    rows = []
    for s in j.get("series") or []:
        if s.get("eur_mwh") is None:
            continue
        rows.append(mk(s["ym"], "PL-ENTSOE", "PL", "pl_price", s["eur_mwh"],
                       "EUR/MWh", src))
    return report("entsoe_pl", rows)


# ── 패널 병합 저장 (변경이력 방식) ──────────────────────
def read_panel():
    if not os.path.exists(OUT_PANEL):
        return []
    try:
        with open(OUT_PANEL, encoding="utf-8-sig") as f:
            return [r for r in csv.DictReader(f)]
    except Exception as e:
        note(f"{OUT_PANEL} 읽기 실패({e}) — 병합 없이 새로 만든다")
        return []


def merge_panel(old, new):
    """(ym,market,entity,var) 의 최신 vintage 값과 다를 때만 새 행을 append."""
    latest = {}
    for r in old:
        k = (r.get("ym"), r.get("market"), r.get("entity"), r.get("var"))
        cur = latest.get(k)
        if cur is None or (r.get("vintage") or "") >= (cur.get("vintage") or ""):
            latest[k] = r
    out = list(old)
    added = changed = same = 0
    for r in new:
        k = (r["ym"], r["market"], r["entity"], r["var"])
        cur = latest.get(k)
        if cur is None:
            out.append(r); latest[k] = r; added += 1
        elif (cur.get("value") != r["value"] or cur.get("kind") != r["kind"]
              or cur.get("status") != r["status"]):
            out.append(r); latest[k] = r; changed += 1
        else:
            same += 1
    out.sort(key=lambda x: (x.get("market", ""), x.get("entity", ""), x.get("var", ""),
                            x.get("ym", ""), x.get("vintage", "")))
    print(f"[PANEL] 신규 {added} · 개정 {changed} · 무변경 {same} → 총 {len(out)}행 "
          f"(고유키 {len(latest)})", flush=True)
    return out, {"added": added, "revised": changed, "unchanged": same,
                 "rows": len(out), "keys": len(latest)}


def write_csv(path, cols, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def merge_vintages(new):
    old = []
    if os.path.exists(OUT_VINT):
        try:
            with open(OUT_VINT, encoding="utf-8-sig") as f:
                old = [r for r in csv.DictReader(f)]
        except Exception as e:
            note(f"{OUT_VINT} 읽기 실패({e}) — 새로 만든다")
    have = {(r.get("vintage_ym"), r.get("target_ym"), r.get("series_id")) for r in old}
    add = [r for r in new if (r["vintage_ym"], r["target_ym"], r["series_id"]) not in have]
    out = old + add
    out.sort(key=lambda x: (x.get("series_id", ""), x.get("vintage_ym", ""),
                            x.get("target_ym", "")))
    print(f"[VINTAGE] 이번 곡선 {len(new)}점 중 신규 {len(add)}점 → 총 {len(out)}행", flush=True)
    return out, {"rows": len(out), "added": len(add)}


# ── main ─────────────────────────────────────────────
def main():
    print(f"=== build_panel v1 · {NOW.isoformat()[:19]} · 기준월 {CUR_YM} ===", flush=True)
    new = []
    steo_rows, vints = load_steo()
    for part in (load_retail(), load_rto(), steo_rows, load_miso(),
                 load_fx(), load_ieso(), load_ga(), load_pl()):
        new += part

    if not new:
        print("!! 적재 0행 — 입력 파일을 하나도 못 읽었다. 위 [LOAD] 줄 확인.", flush=True)
        json.dump({"built_at": TODAY, "sources": STATUS, "notes": NOTES,
                   "fatal": "no input"}, open(OUT_STATUS, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        sys.exit(1)

    panel, pstat = merge_panel(read_panel(), new)
    write_csv(OUT_PANEL, PANEL_COLS, panel)
    vout, vstat = merge_vintages(vints)
    write_csv(OUT_VINT, VINT_COLS, vout)

    # 커버리지 요약 — 어느 시장이 몇 개월인지 한눈에
    cov = {}
    for r in panel:
        k = f"{r['market']}/{r['var']}"
        c = cov.setdefault(k, {"months": set(), "entities": set()})
        c["months"].add(r["ym"]); c["entities"].add(r["entity"])
    coverage = {k: {"months": len(v["months"]),
                    "range": [min(v["months"]), max(v["months"])],
                    "entities": sorted(v["entities"])}
                for k, v in sorted(cov.items())}
    print("[COVERAGE]", flush=True)
    for k, v in coverage.items():
        print(f"   {k:28s} {v['months']:4d}개월 {v['range'][0]}~{v['range'][1]} "
              f"{v['entities']}", flush=True)

    json.dump({"built_at": TODAY, "base_ym": CUR_YM,
               "sources": STATUS, "panel": pstat, "vintages": vstat,
               "coverage": coverage, "notes": NOTES,
               "excluded": {"US-PJM": "확보 불가 종결 — EIA v2 라우트 404, "
                                      "Data Miner2 로그인 도메인 사내망 차단"},
               "anchor_policy": "앵커(사업장 회피단가)는 사내 데이터이므로 이 Public 레포에 "
                                "두지 않는다. 수준 환산은 HTML 또는 사내 경로에서 수행한다."},
              open(OUT_STATUS, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[OK] {OUT_PANEL} {pstat['rows']}행 · {OUT_VINT} {vstat['rows']}행 · {OUT_STATUS}",
          flush=True)


if __name__ == "__main__":
    main()
