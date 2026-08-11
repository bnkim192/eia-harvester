# -*- coding: utf-8 -*-
"""
fit_model.py — 전력단가 예측 파일럿 성장경로·백테스트 (v1)

■ 입력  panel_monthly.csv (build_panel.py v2 산출) · steo_vintages.csv
■ 산출  growth_monthly.csv     앵커 없는 성장경로 + 80% 밴드
        backtest_scorecard.csv 타깃×모델×h 별 정확도·벤치마크 대비 skill
        model_status.json      적합 요약·게이트·계수 검산

■ 앵커를 곱하지 않는다 (02_method §3-3)
  앵커(사업장 회피단가)는 사내 고지서 원가이고 이 레포는 Public 이다. 따라서 여기서는
  **공개데이터만으로 만든 성장지수(index)와 native 단위 예측치**까지만 낸다.
  수준 환산은 HTML 또는 사내 경로에서 `앵커 × ((1−v) + v×index)` 로 수행한다.
  `pass_through` 컬럼이 그 v 를 명시한다 — 소매 기반 1.0(이미 T&D 완충 포함),
  도매 기반 0.6(도매→소매 전가 감쇠). 소매 성장경로에 0.6 을 또 곱하면 이중 완충이다.

■ 모델 — 직접 h 로그성장 OLS (02_method §1-4·§1-5)
  origin o 에서 t=o+h 를 예측한다. o 시점에 아는 마지막 실적은 y_(o−L) 이다(L=가용지연).
      r = f(y_t) − f(y_(o−L))            ← 마지막 기지값 대비 성장
      r = b0 + b1·[g(gas_t) − g(gas_(o−L))] + Fourier(2, month of t) + (세트B 피처) + ε
  · h 마다 **별도 적합**한다(direct multi-step). 반복예측의 오차 누적이 없다.
  · 변환은 자동 — 전 구간 양수면 log(탄력성 해석), 음수·0 가능하면 level(heat rate 해석).
    도매가는 음전(LMP)·환급(GA)이 있어 level 로 간다.
  · 트리계열을 쓰지 않는 이유는 forward 가 학습범위 밖 외삽이라 leaf 상수로 평평해지기
    때문이다(§1-5). 선형은 외삽이 정의되고 계수가 물리량으로 검산된다.

■ 세트 A / B (02_method §5-2) — 표본 길이가 달라 h 상한이 갈린다
  A 가스+계절+마지막기지값     : 겹침 2018-01~ (가스 시작)  → h 전부
  B A + 부하(origin 시점 값)   : 겹침 2019-01~ (일별 부하)  → h≤12
  부하 피처는 **origin 시점에 아는 값만** 쓴다 — 대상월 부하를 쓰면 look-ahead 다.
      load_yoy = log(d_load_mwh[o−1]) − log(d_load_mwh[o−13])
      df_err   = d_df_err[o−1]
  세트 B 는 부하 권역이 매핑되는 타깃에만 적용한다 — OH→PJM, MI→MISO. 나머지는 A 만.

■ 백테스트 (02_method §5-2·§5-3·§5-4)
  · rolling-origin, 최소 학습 48예제, origin 1개월 전진
  · **누출 차단** — origin O 의 학습셋은 `o + h ≤ O − L` 인 예제만. 즉 y_t 가 O 시점에
    이미 알려진 쌍만 쓴다. 소매는 L=3(실측 지연 3개월), 도매는 L=1.
  · **gas_path='realized'** — STEO forward 의 과거 vintage 가 축적되지 않았으므로
    (steo_vintages.csv 는 이번 달부터 쌓인다) 백테스트는 실현 가스를 쓴다. 즉 이 점수는
    "가스를 완벽히 알 때 전력 모델의 오차"이고, 가스 예측오차는 포함되지 않는다.
    이 사실을 scorecard 의 gas_path 컬럼에 남긴다. 완전연쇄 검증은 vintage 축적 후.
  · 벤치마크 3개 — 계절나이브(같은 달 최근 실적) · 마지막값 유지 · ETS(가법 HW, 자체구현)
  · 지표 — MAPE·sMAPE·MAE·RMSE·bias·방향적중률·80%밴드 커버리지·skill score

■ 게이트 (02_method §5-5) — 하드 기각이 아니라 플래그다
  n_origins < 24 → insufficient_n. 벤치마크 3개 전부에 skill>0 이어야 pass.
  계수 검산 — level 모델의 b1 은 implied heat rate 로 읽혀 6~10 이 통상범위,
  log 모델의 b1 은 탄력성이라 −0.2~0.8 이 통상범위. 벗어나면 coef_flag 에 남긴다.

■ 의존성: numpy (OLS·행렬). requests 불필요.
■ 진단 안전망: 줄 끝 백슬래시 미사용. main() 을 try/except 로 감싸 어떤 오류에도
  model_status.json 에 traceback 을 남긴다.
"""
import os, sys, json, csv, math, traceback
import datetime as dt
import numpy as np

IN_PANEL = "panel_monthly.csv"
IN_VINT = "steo_vintages.csv"
OUT_GROWTH = "growth_monthly.csv"
OUT_SCORE = "backtest_scorecard.csv"
OUT_STATUS = "model_status.json"

NOW = dt.datetime.now(dt.UTC)
TODAY = NOW.date().isoformat()

HORIZONS = [1, 3, 6, 12, 17, 24]
MIN_TRAIN = 48                 # 최소 학습 예제 수 (§5-2)
MIN_ORIGINS = 24               # 게이트 (§5-5)
Z80 = 1.2815515655446004       # 80% 양측
LIVE_MAX_LAG = 4               # 최신 실적이 이보다 오래되면 live 예측 생략

# 타깃 정의 — (market, entity, var, L(가용지연 개월), pass_through, 부하권역)
TARGETS = [
    ("US-RETAIL", "AZ", "retail_ind", 3, 1.0, None),
    ("US-RETAIL", "GA", "retail_ind", 3, 1.0, None),
    ("US-RETAIL", "MI", "retail_ind", 3, 1.0, "MISO"),
    ("US-RETAIL", "OH", "retail_ind", 3, 1.0, "PJM"),
    ("US-RETAIL", "TN", "retail_ind", 3, 1.0, None),
    ("US-RETAIL", "US", "retail_ind", 3, 1.0, None),
    ("PL-ENTSOE", "PL", "pl_price", 1, 0.6, None),
    ("CA-IESO", "OT", "ot_energy_hoep", 1, 0.6, None),   # 2025-04 종료 → 백테스트 전용
]

GAS_KEY = ("GLOBAL", "US", "hh_gas")

STATUSJ = {"built_at": TODAY, "notes": [], "targets": {}}


def note(msg):
    STATUSJ["notes"].append(msg)
    print(f"[NOTE] {msg}", flush=True)


# ── ym 유틸 ──────────────────────────────────────────
def yi(ym):
    return int(ym[:4]) * 12 + int(ym[5:7]) - 1


def ys(i):
    return f"{i // 12:04d}-{i % 12 + 1:02d}"


def mon(ym):
    return int(ym[5:7])


def fourier(m):
    a = 2.0 * math.pi * m / 12.0
    return [math.sin(a), math.cos(a), math.sin(2 * a), math.cos(2 * a)]


# ── 변환 ─────────────────────────────────────────────
def pick_mode(vals):
    """전 구간 충분히 양수면 log(탄력성), 아니면 level(heat rate)."""
    return "log" if vals and min(vals) > 0.05 else "level"


def tf(v, mode):
    return math.log(v) if mode == "log" else float(v)


def untf(base, r, mode):
    return base * math.exp(r) if mode == "log" else base + r


# ── 패널 로딩 ────────────────────────────────────────
def load_panel():
    """최신 vintage 만 취한다. actual/status=ok 는 A, forecast 는 F 로 분리."""
    if not os.path.exists(IN_PANEL):
        print(f"!! {IN_PANEL} 없음 — build_panel.py 먼저 실행", flush=True)
        return None, None
    A, F = {}, {}
    seen = {}
    n = 0
    with open(IN_PANEL, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            key = (r.get("market"), r.get("entity"), r.get("var"))
            ym = (r.get("ym") or "").strip()
            val = (r.get("value") or "").strip()
            if not ym or val == "":
                continue
            try:
                v = float(val)
            except Exception:
                continue
            kind = (r.get("kind") or "actual").strip()
            st = (r.get("status") or "ok").strip()
            vint = r.get("vintage") or ""
            sk = (key, ym, kind)
            if sk in seen and seen[sk] > vint:      # 더 새 vintage 가 이미 있음
                continue
            seen[sk] = vint
            if kind == "forecast":
                F.setdefault(key, {})[ym] = v
            elif st == "ok":
                A.setdefault(key, {})[ym] = v
            n += 1
    print(f"[PANEL] {n}행 읽음 · actual 계열 {len(A)}개 · forecast 계열 {len(F)}개", flush=True)
    return A, F


# ── OLS ──────────────────────────────────────────────
def ols_fit(X, y):
    beta, _r, _rk, _s = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    n, p = X.shape
    dof = max(n - p, 1)
    s2 = float(resid @ resid) / dof
    XtXi = np.linalg.pinv(X.T @ X)
    return beta, s2, XtXi


def ols_pred(beta, s2, XtXi, x0):
    mu = float(x0 @ beta)
    var = s2 * (1.0 + float(x0 @ XtXi @ x0))
    return mu, math.sqrt(max(var, 0.0))


# ── ETS (가법 Holt-Winters, 순수 파이썬) ──────────────
def hw_fit(y, m=12, grid=(0.1, 0.2, 0.3, 0.5)):
    """(a,b,g) 격자탐색으로 SSE 최소. 반환 (level, trend, seasonal, n) 또는 None."""
    n = len(y)
    if n < 2 * m + 4:
        return None
    best = None
    for a in grid:
        for b in grid:
            for g in grid:
                lv = sum(y[:m]) / m
                tr = (sum(y[m:2 * m]) / m - lv) / m
                s = [y[i] - lv for i in range(m)]
                sse = 0.0
                for t in range(n):
                    si = s[t % m]
                    e = y[t] - (lv + tr + si)
                    sse += e * e
                    lv_new = a * (y[t] - si) + (1 - a) * (lv + tr)
                    tr = b * (lv_new - lv) + (1 - b) * tr
                    s[t % m] = g * (y[t] - lv_new) + (1 - g) * si
                    lv = lv_new
                if not math.isfinite(sse):
                    continue
                if best is None or sse < best[0]:
                    best = (sse, lv, tr, list(s), n)
    if best is None:
        return None
    return best[1], best[2], best[3], best[4]


def hw_forecast(state, steps, m=12):
    lv, tr, s, n = state
    return lv + steps * tr + s[(n - 1 + steps) % m]


def contiguous(series, i_lo, i_hi):
    """i_lo..i_hi 가 빈틈 없이 있으면 리스트로, 아니면 None."""
    out = []
    for i in range(i_lo, i_hi + 1):
        v = series.get(ys(i))
        if v is None:
            return None
        out.append(v)
    return out


# ETS 적합은 학습창 끝(end_i)에만 의존하고 h 와 무관하다 → 캐시해서 h·세트 반복을 없앤다.
# (캐시 없으면 8타깃 × 6h × 2세트 × ~80origin = 7,680회 격자탐색으로 수 분 걸린다)
_HW_CACHE = {}


def hw_state(name, series, lo_i, end_i):
    ck = (name, lo_i, end_i)
    if ck in _HW_CACHE:
        return _HW_CACHE[ck]
    arr = contiguous(series, lo_i, end_i)
    st = hw_fit(arr) if arr else None
    _HW_CACHE[ck] = st
    return st


# ── 피처 행 만들기 ───────────────────────────────────
def make_x(oi, h, L, y, gas, featfns, tmode, gmode):
    """origin oi 의 설계행. 반환 (x, base, t_ym) 또는 None.
       base = y[oi−L] (마지막 기지값). gas 는 t 시점 값이 있어야 한다."""
    ti, ki = oi + h, oi - L
    t, k = ys(ti), ys(ki)
    yk = y.get(k)
    gt, gk = gas.get(t), gas.get(k)
    if yk is None or gt is None or gk is None:
        return None
    if tmode == "log" and yk <= 0:
        return None
    if gmode == "log" and (gt <= 0 or gk <= 0):
        return None
    x = [1.0, tf(gt, gmode) - tf(gk, gmode)] + fourier(mon(t))
    for _name, fn in featfns:
        v = fn(oi)
        if v is None or not math.isfinite(v):
            return None
        x.append(v)
    return x, yk, t


def build_examples(y, gas, h, L, featfns, tmode, gmode, oi_lo, oi_hi):
    """학습 예제 = (oi, ti, r, x). y_t 가 있어야 하므로 실적 구간에서만 만든다."""
    ex = []
    for oi in range(oi_lo, oi_hi + 1):
        got = make_x(oi, h, L, y, gas, featfns, tmode, gmode)
        if got is None:
            continue
        x, yk, t = got
        yt = y.get(t)
        if yt is None:
            continue
        if tmode == "log" and yt <= 0:
            continue
        ex.append((oi, oi + h, tf(yt, tmode) - tf(yk, tmode), x))
    return ex


# ── 지표 ─────────────────────────────────────────────
def metrics(act, pred, lo, hi, last_known):
    n = len(act)
    if n == 0:
        return None
    a = np.array(act, dtype=float)
    p = np.array(pred, dtype=float)
    err = p - a
    denom = np.where(np.abs(a) < 1e-9, np.nan, np.abs(a))
    mape = float(np.nanmean(np.abs(err) / denom) * 100.0)
    sden = (np.abs(a) + np.abs(p)) / 2.0
    sden = np.where(sden < 1e-9, np.nan, sden)
    smape = float(np.nanmean(np.abs(err) / sden) * 100.0)
    mae = float(np.mean(np.abs(err)))
    rmse = float(math.sqrt(float(np.mean(err ** 2))))
    bias = float(np.mean(err))
    lk = np.array(last_known, dtype=float)
    da = float(np.mean(np.sign(p - lk) == np.sign(a - lk)) * 100.0)
    cov = float(np.mean((a >= np.array(lo)) & (a <= np.array(hi))) * 100.0)
    return {"n": n, "mape": round(mape, 3), "smape": round(smape, 3),
            "mae": round(mae, 4), "rmse": round(rmse, 4), "bias": round(bias, 4),
            "dir_acc": round(da, 1), "cover80": round(cov, 1)}


def skill(m_model, m_bench):
    if not m_model or not m_bench or not m_bench.get("mape"):
        return None
    return round(1.0 - m_model["mape"] / m_bench["mape"], 4)


# ── 한 타깃×h 백테스트 ────────────────────────────────
def backtest(name, y, gas, h, L, featfns, tmode, gmode):
    yms = sorted(y)
    if len(yms) < MIN_TRAIN + h + L + 4:
        return None
    lo_i, hi_i = yi(yms[0]), yi(yms[-1])
    ex_all = build_examples(y, gas, h, L, featfns, tmode, gmode,
                            lo_i + L + h, hi_i - h)
    if len(ex_all) < MIN_TRAIN + 2:
        return None
    p = len(ex_all[0][3])
    acc = {k: [] for k in ("act", "pred", "lo", "hi", "lk",
                           "b_last", "b_s12", "b_ets")}
    coefs = []
    # origin O 는 y_(O+h) 가 실적으로 있는 구간만 (사후 채점 가능해야 한다)
    for Oi in range(lo_i + L + h, hi_i - h + 1):
        actual = y.get(ys(Oi + h))
        if actual is None:
            continue
        train = [e for e in ex_all if e[0] + h <= Oi - L]      # 누출 차단
        if len(train) < MIN_TRAIN:
            continue
        got = make_x(Oi, h, L, y, gas, featfns, tmode, gmode)
        if got is None:
            continue
        x0, base, _t = got
        X = np.array([e[3] for e in train], dtype=float)
        yy = np.array([e[2] for e in train], dtype=float)
        if X.shape[0] <= p:
            continue
        beta, s2, XtXi = ols_fit(X, yy)
        mu, se = ols_pred(beta, s2, XtXi, np.array(x0, dtype=float))
        acc["act"].append(actual)
        acc["pred"].append(untf(base, mu, tmode))
        acc["lo"].append(untf(base, mu - Z80 * se, tmode))
        acc["hi"].append(untf(base, mu + Z80 * se, tmode))
        acc["lk"].append(base)
        coefs.append(float(beta[1]))
        # 벤치마크 1) 마지막값 유지
        acc["b_last"].append(base)
        # 벤치마크 2) 계절나이브 — 같은 달의 가장 최근 실적(O−L 이하)
        s12 = None
        kk = Oi + h - 12
        while kk >= lo_i:
            if kk <= Oi - L and ys(kk) in y:
                s12 = y[ys(kk)]
                break
            kk -= 12
        acc["b_s12"].append(s12 if s12 is not None else base)
        # 벤치마크 3) ETS — O−L 까지로 적합해 h+L 스텝 예측 (캐시 사용)
        st = hw_state(name, y, lo_i, Oi - L)
        ev = hw_forecast(st, h + L) if st else None
        acc["b_ets"].append(ev if (ev is not None and math.isfinite(ev)) else base)

    m = metrics(acc["act"], acc["pred"], acc["lo"], acc["hi"], acc["lk"])
    if not m:
        return None
    out = {"model": m, "coef_gas_mean": round(float(np.mean(coefs)), 4) if coefs else None}
    for bn, key in (("naive_last", "b_last"), ("naive_s12", "b_s12"), ("ets", "b_ets")):
        # 벤치마크의 cover80 은 모델 밴드를 쓰므로 의미가 없다 → 출력 컬럼에서 제외한다.
        bm = metrics(acc["act"], acc[key], acc["lo"], acc["hi"], acc["lk"])
        out[bn] = bm
        out["skill_vs_" + bn] = skill(m, bm)
    return out


# ── live 성장경로 ────────────────────────────────────
def growth(name, y, gas_live, gas_hist, h, L, featfns, tmode, gmode, kinds):
    """마지막 실적 기준 origin 에서 h 앞을 예측. gas_live 는 실적+forward 병합."""
    yms = sorted(y)
    if not yms:
        return None
    last_i = yi(yms[-1])
    Oi = last_i + L                       # 그래야 Oi−L = 마지막 실적
    lo_i = yi(yms[0])
    ex = build_examples(y, gas_hist, h, L, featfns, tmode, gmode,
                        lo_i + L + h, last_i - h)
    if len(ex) < MIN_TRAIN:
        return None
    got = make_x(Oi, h, L, y, gas_live, featfns, tmode, gmode)
    if got is None:
        return None
    x0, base, t = got
    X = np.array([e[3] for e in ex], dtype=float)
    yy = np.array([e[2] for e in ex], dtype=float)
    if X.shape[0] <= X.shape[1]:
        return None
    beta, s2, XtXi = ols_fit(X, yy)
    mu, se = ols_pred(beta, s2, XtXi, np.array(x0, dtype=float))
    base12 = [y[ys(i)] for i in range(last_i - 11, last_i + 1) if ys(i) in y]
    b = (sum(base12) / len(base12)) if base12 else base
    lvl = untf(base, mu, tmode)
    lo = untf(base, mu - Z80 * se, tmode)
    hi = untf(base, mu + Z80 * se, tmode)
    return {"target_ym": t, "level": lvl, "lo": lo, "hi": hi, "base": b,
            "index": (lvl / b) if b else None,
            "index_lo": (lo / b) if b else None,
            "index_hi": (hi / b) if b else None,
            "n_train": int(X.shape[0]), "coef_gas": round(float(beta[1]), 4),
            "gas_kind": kinds.get(t, "unknown"),
            "base_window": f"{ys(max(last_i - 11, lo_i))}~{ys(last_i)}"}


def coef_flag(mode, b):
    """level → implied heat rate 6~10 · log → 탄력성 −0.2~0.8 (§5-5 물리검산)."""
    if b is None:
        return ""
    if mode == "level":
        return "" if 6.0 <= b <= 10.0 else f"heat_rate_out_of_range({b})"
    return "" if -0.2 <= b <= 0.8 else f"elasticity_out_of_range({b})"


def main():
    print(f"=== fit_model v1 · {NOW.isoformat()[:19]} ===", flush=True)
    A, F = load_panel()
    if A is None:
        STATUSJ["fatal"] = f"{IN_PANEL} 없음"
        json.dump(STATUSJ, open(OUT_STATUS, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        sys.exit(1)

    gas_hist = A.get(GAS_KEY) or {}
    gas_fwd = F.get(GAS_KEY) or {}
    if not gas_hist:
        note("hh_gas 실적이 없다 — 모든 모델이 적합 불가. panel 의 GLOBAL/US/hh_gas 확인.")
    gas_live = dict(gas_hist)
    gas_live.update(gas_fwd)
    kinds = {k: "actual" for k in gas_hist}
    kinds.update({k: "forecast" for k in gas_fwd})
    gh = sorted(gas_hist)
    gf = sorted(gas_fwd)
    print(f"[GAS] 실적 {len(gh)}개월 {gh[0] if gh else '-'}~{gh[-1] if gh else '-'} · "
          f"forward {len(gf)}개월 {gf[0] if gf else '-'}~{gf[-1] if gf else '-'}", flush=True)

    # STEO forward vintage 축적 현황 — 완전연쇄 백테스트가 언제 가능해지는지의 지표
    if os.path.exists(IN_VINT):
        vs = set()
        try:
            with open(IN_VINT, encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    if r.get("vintage_ym"):
                        vs.add(r["vintage_ym"])
        except Exception as e:
            note(f"{IN_VINT} 읽기 실패 {e}")
        STATUSJ["steo_vintages"] = {"count": len(vs), "list": sorted(vs)}
        print(f"[VINTAGE] STEO forward vintage {len(vs)}개 {sorted(vs)} — "
              f"24개 이상 쌓이면 가스 예측오차 포함 완전연쇄 백테스트 가능", flush=True)
    else:
        STATUSJ["steo_vintages"] = {"count": 0, "list": []}

    score_rows, growth_rows = [], []

    for market, entity, var, L, ptr, rto in TARGETS:
        y = A.get((market, entity, var)) or {}
        name = f"{market}/{entity}/{var}"
        yms = sorted(y)
        if len(yms) < MIN_TRAIN:
            STATUSJ["targets"][name] = {"months": len(yms), "verdict": "insufficient_months"}
            print(f"[SKIP] {name:34s} {len(yms)}개월 — 최소 {MIN_TRAIN} 미달", flush=True)
            continue
        vals = [y[k] for k in yms]
        tmode = pick_mode(vals)
        gmode = pick_mode([gas_hist[k] for k in gh]) if gh else "level"
        lag = yi(dt.date.today().strftime("%Y-%m")) - yi(yms[-1])
        live_ok = lag <= LIVE_MAX_LAG
        print(f"[TARGET] {name:34s} {len(yms)}개월 {yms[0]}~{yms[-1]} "
              f"transform={tmode} gas={gmode} lag={lag}개월 live={'Y' if live_ok else 'N'}",
              flush=True)
        STATUSJ["targets"][name] = {"months": len(yms), "range": [yms[0], yms[-1]],
                                    "transform": tmode, "gas_transform": gmode,
                                    "lag_months": lag, "live": live_ok,
                                    "pass_through": ptr, "rto": rto, "sets": {}}

        # 세트 정의
        sets = [("A", [])]
        if rto:
            dl = A.get(("US-RTO", rto, "d_load_mwh")) or {}
            de = A.get(("US-RTO", rto, "d_df_err")) or {}

            def f_load_yoy(oi, dl=dl):
                a, b = dl.get(ys(oi - 1)), dl.get(ys(oi - 13))
                if not a or not b or a <= 0 or b <= 0:
                    return None
                return math.log(a) - math.log(b)

            def f_dferr(oi, de=de):
                return de.get(ys(oi - 1))

            if dl and de:
                sets.append(("B", [("load_yoy", f_load_yoy), ("df_err", f_dferr)]))
            else:
                note(f"{name}: 부하 계열({rto}) 없음 → 세트 B 생략")

        for sname, featfns in sets:
            for h in HORIZONS:
                res = backtest(name, y, gas_hist, h, L, featfns, tmode, gmode)
                if not res:
                    score_rows.append({"target": name, "set": sname, "model": "direct_ols",
                                       "horizon": h, "n_origins": 0, "gate": "insufficient_n",
                                       "gas_path": "realized", "transform": tmode,
                                       "run_vintage": TODAY})
                    continue
                m = res["model"]
                sk = [res.get("skill_vs_naive_last"), res.get("skill_vs_naive_s12"),
                      res.get("skill_vs_ets")]
                if m["n"] < MIN_ORIGINS:
                    gate = "insufficient_n"
                elif all(s is not None and s > 0 for s in sk):
                    gate = "pass"
                else:
                    gate = "fail"
                row = {"target": name, "set": sname, "model": "direct_ols", "horizon": h,
                       "n_origins": m["n"], "mape": m["mape"], "smape": m["smape"],
                       "mae": m["mae"], "rmse": m["rmse"], "bias": m["bias"],
                       "dir_acc": m["dir_acc"], "cover80": m["cover80"],
                       "skill_vs_naive_last": res.get("skill_vs_naive_last"),
                       "skill_vs_naive_s12": res.get("skill_vs_naive_s12"),
                       "skill_vs_ets": res.get("skill_vs_ets"),
                       "coef_gas": res.get("coef_gas_mean"),
                       "coef_flag": coef_flag(tmode, res.get("coef_gas_mean")),
                       "gate": gate, "gas_path": "realized", "transform": tmode,
                       "run_vintage": TODAY}
                score_rows.append(row)
                print(f"   {sname} h={h:2d} n={m['n']:3d} MAPE={m['mape']:6.2f}% "
                      f"cov80={m['cover80']:5.1f}% skill(last/s12/ets)="
                      f"{sk[0]}/{sk[1]}/{sk[2]} → {gate}", flush=True)
                STATUSJ["targets"][name]["sets"].setdefault(sname, {})[str(h)] = {
                    "n_origins": m["n"], "mape": m["mape"], "gate": gate}

                # 벤치마크도 스코어카드에 남긴다(비교 근거)
                for bn in ("naive_last", "naive_s12", "ets"):
                    bm = res.get(bn)
                    if not bm:
                        continue
                    score_rows.append({"target": name, "set": sname, "model": bn,
                                       "horizon": h, "n_origins": bm["n"],
                                       "mape": bm["mape"], "smape": bm["smape"],
                                       "mae": bm["mae"], "rmse": bm["rmse"],
                                       "bias": bm["bias"], "dir_acc": bm["dir_acc"],
                                       "gate": "", "gas_path": "realized",
                                       "transform": tmode, "run_vintage": TODAY})

            if not live_ok:
                continue
            for h in HORIZONS:
                g = growth(name, y, gas_live, gas_hist, h, L, featfns, tmode, gmode, kinds)
                if not g:
                    continue
                growth_rows.append({
                    "target": name, "market": market, "entity": entity, "var": var,
                    "set": sname, "horizon": h, "ym": g["target_ym"],
                    "tier": "L1_forward" if g["gas_kind"] == "forecast" else "L1_realized",
                    "kind": "forecast", "transform": tmode,
                    "index": round(g["index"], 5) if g["index"] else "",
                    "index_lo": round(g["index_lo"], 5) if g["index_lo"] else "",
                    "index_hi": round(g["index_hi"], 5) if g["index_hi"] else "",
                    "level_native": round(g["level"], 4),
                    "level_lo": round(g["lo"], 4), "level_hi": round(g["hi"], 4),
                    "pass_through": ptr, "base_window": g["base_window"],
                    "base_level": round(g["base"], 4), "gas_path": g["gas_kind"],
                    "n_train": g["n_train"], "coef_gas": g["coef_gas"],
                    "coef_flag": coef_flag(tmode, g["coef_gas"]),
                    "vintage": TODAY})

    # ── 저장 ──
    sc_cols = ["target", "set", "model", "horizon", "n_origins", "mape", "smape", "mae",
               "rmse", "bias", "dir_acc", "cover80", "skill_vs_naive_last",
               "skill_vs_naive_s12", "skill_vs_ets", "coef_gas", "coef_flag", "gate",
               "gas_path", "transform", "run_vintage"]
    gr_cols = ["target", "market", "entity", "var", "set", "horizon", "ym", "tier", "kind",
               "transform", "index", "index_lo", "index_hi", "level_native", "level_lo",
               "level_hi", "pass_through", "base_window", "base_level", "gas_path",
               "n_train", "coef_gas", "coef_flag", "vintage"]
    for path, cols, rows in ((OUT_SCORE, sc_cols, score_rows),
                             (OUT_GROWTH, gr_cols, growth_rows)):
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)

    passes = [r for r in score_rows if r.get("gate") == "pass"]
    fails = [r for r in score_rows if r.get("gate") == "fail"]
    insuf = [r for r in score_rows if r.get("gate") == "insufficient_n"]
    STATUSJ["summary"] = {"scorecard_rows": len(score_rows), "growth_rows": len(growth_rows),
                          "pass": len(passes), "fail": len(fails),
                          "insufficient_n": len(insuf)}
    STATUSJ["gas_path_note"] = ("백테스트는 gas_path=realized 다. STEO forward 의 과거 "
                                "vintage 가 없어 완전연쇄(가스 예측오차 포함) 검증은 "
                                "steo_vintages.csv 축적 후에 가능하다.")
    json.dump(STATUSJ, open(OUT_STATUS, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[OK] {OUT_SCORE} {len(score_rows)}행 · {OUT_GROWTH} {len(growth_rows)}행 · "
          f"{OUT_STATUS} · pass {len(passes)} / fail {len(fails)} / "
          f"insufficient {len(insuf)}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        traceback.print_exc()
        STATUSJ["fatal"] = f"{type(e).__name__}: {e}"
        json.dump(STATUSJ, open(OUT_STATUS, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print(f"[OK] {OUT_STATUS} 에 예외 기록 저장 — fatal 확인", flush=True)
        sys.exit(1)
