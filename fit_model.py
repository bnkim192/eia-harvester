# -*- coding: utf-8 -*-
"""
fit_model.py — 전력단가 예측 파일럿 성장경로·백테스트 (v3)

■ v3 변경 — 밴드를 **온라인 conformal** 로 보정한다 (v2 부트스트랩 실패의 정면 대응)
  v2 에서 경험분위(boot·boot60)를 넣었으나 **OH 를 고치지 못했다.** 세 방식이 거의 같은
  값을 냈다는 것은 잔차 분포의 **모양이 문제가 아니라는 뜻**이다. 역산하면 명확하다.
      OH h=12 : 밴드 ±1.28·s_in 인데 커버리지 50%  →  s_out / s_in = **1.90**
      US h=12 : 커버리지 100%                      →  s_out / s_in ≤ **0.51**
  즉 **표본내 잔차가 표본외 오차의 척도를 반대 방향으로 잘못 추정**한다.
      · OH  — 소매경쟁 레짐 점프. 과거 점프는 학습에서 일부 적합돼 잔차가 작아지지만
              미래 점프는 예측 불가라 실제 오차가 크다 → 밴드가 좁다(과신)
      · US·TN — 학습 구간(2018~2021)이 최근보다 변동이 컸다 → 밴드가 넓다(보수)
  분포 모양을 바꿔도 척도가 틀리면 고쳐지지 않는다. **표본외 오차로 직접 보정해야 한다.**

  → **온라인 conformal.** origin O 의 밴드는 **O 이전 origin 들의 실제 예측오차**만 쓴다.
      d_i   = r_실제_i − mu_i                       (변환공간의 표본외 오차)
      band  = mu_O + quantile({d_i : i < O}, 10%·90%)
  · 부호를 보존한 분위(|d| 가 아니라 d)를 쓰므로 **편의(bias)까지 자동 보정**된다.
  · 각 밴드가 자기 시점 이전 정보만 쓰므로 **커버리지 측정이 정직하다**(누출 없음).
  · 선행 origin 이 MIN_CONF(20) 개 이상 쌓인 뒤부터 밴드를 내고, 그 전은 norm 폴백.
  · live 예측의 밴드는 해당 (타깃·세트·h) 백테스트 오차 풀 전체의 분위를 쓴다.

  ■ 한계를 명시한다 — h 가 클수록 검증 표본이 남지 않는다
    OH h=12 는 `n_origins`=24 이므로 앞 20개를 보정에 쓰면 **4개로만 커버리지를 측정**한다.
    통계적으로 의미 있는 수가 아니다. h=1·3(46·42 origin)에서는 제대로 작동하지만
    **h≥12 의 밴드 검증은 표본이 더 쌓인 뒤에나 가능하다** → `n_conf` 를 함께 싣고
    작으면 "밴드 미검증"으로 읽어야 한다. 비교 공정성을 위해 같은 부분집합에서 잰
    `cover80_norm_sub` 도 함께 낸다.

■ v2 변경 — 예측구간을 정규분포 가정에서 경험분위로 바꾼다 (v3에서 비교군으로 유지)
  1차 백테스트(2026-08-11)에서 명목 80% 밴드의 실제 커버리지가 양방향으로 크게 벗어났다.
      OH  67 → 62 → 56 → 50 → 14 %  (h 커질수록 붕괴 — 과신)
      TN·US·PL·OT  92~100 %          (과대 — 보수)
  OH 는 소매경쟁 레짐 점프가 잔차 분포를 두껍게 만드는데 `mu ± z·s·sqrt(1+h0)` 의
  **정규분포 가정이 그 꼬리를 담지 못한다.** 위험 판단에서 과신은 과대보다 해롭다.

  → **스튜던트화 잔차의 경험분위**로 교체한다(잔차 부트스트랩의 해석적 형태).
      e_i        = y_i − x_i·beta                (학습 잔차)
      h_i        = x_i' (X'X)^-1 x_i             (관측별 leverage)
      es_i       = e_i / sqrt(1 − h_i)           (표준화 — 적합점 근처 과소분산 보정)
      h0         = x0' (X'X)^-1 x0
      band       = mu + quantile(es, 10%·90%) × sqrt(1 + h0)
  리샘플 루프가 없어 비용이 0 에 가깝고, **꼬리 모양을 잔차에서 그대로 가져온다.**
  log 변환 타깃은 이 구간을 지수화하므로 수준 공간에서 자동으로 **비대칭 밴드**가 된다.

  ■ 어느 방식이 맞는지 추측하지 않는다 — 3개를 동시에 재고 데이터가 고르게 한다
      cover80_norm    정규분포 가정 (v1 방식)
      cover80_boot    경험분위, 전 잔차
      cover80_boot60  경험분위, 최근 60개 잔차만 (현재 변동성 국면 반영)
  [v3에서 대체됨] 실측 결과 boot60 이 23행 중 14행으로 근소하게 이겼으나 OH 를 못 고쳤다.
  growth 의 기본 밴드는 conformal 이고, boot·norm 은 폴백·비교군으로만 남긴다.

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
Z80 = 1.2815515655446004       # 80% 양측 (정규 비교용)
LIVE_MAX_LAG = 4               # 최신 실적이 이보다 오래되면 live 예측 생략
BAND_LO, BAND_HI = 10.0, 90.0  # 경험분위 백분율 (80% 구간)
RESID_WIN = 60                 # boot60 의 최근 잔차 창(개월). 현재 변동성 국면 반영용
MIN_CONF = 20                  # 온라인 conformal 보정에 필요한 선행 origin 수
BAND_DEFAULT = "conformal"     # growth 에 실을 기본 밴드 (폴백: boot → norm)

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


def band_quantiles(X, yy, beta, XtXi, x0, window=None):
    """스튜던트화 잔차의 경험분위로 예측구간 오프셋 (lo, hi) 를 낸다.
       정규분포 가정 없이 꼬리 모양을 잔차에서 그대로 가져온다.
       X 는 시간 오름차순이므로 window 는 '최근 N개 잔차'를 뜻한다."""
    resid = yy - X @ beta
    h = np.einsum("ij,jk,ik->i", X, XtXi, X)          # 관측별 leverage
    es = resid / np.sqrt(np.clip(1.0 - h, 1e-6, None))
    if window and es.size > window:
        es = es[-window:]
    if es.size < 8:                                   # 분위가 불안정하면 포기
        return None, None
    scale = math.sqrt(1.0 + float(x0 @ XtXi @ x0))
    return (float(np.percentile(es, BAND_LO)) * scale,
            float(np.percentile(es, BAND_HI)) * scale)


def cover(act, lo, hi, mask=None):
    """실측이 밴드 안에 들어온 비율(%). mask 가 있으면 True 인 원소만 센다.
       반환 (비율, 표본수). 표본이 없으면 (None, 0)."""
    if not act:
        return None, 0
    idx = [i for i in range(len(act))
           if (mask is None or mask[i]) and lo[i] is not None and hi[i] is not None]
    if not idx:
        return None, 0
    a = np.array([act[i] for i in idx], dtype=float)
    l = np.array([lo[i] for i in idx], dtype=float)
    u = np.array([hi[i] for i in idx], dtype=float)
    return round(float(np.mean((a >= l) & (a <= u)) * 100.0), 1), len(idx)


def conf_offsets(errs):
    """표본외 오차 풀의 부호보존 분위 → (lo_off, hi_off). 편의까지 함께 보정된다."""
    if len(errs) < MIN_CONF:
        return None, None
    return (float(np.percentile(errs, BAND_LO)),
            float(np.percentile(errs, BAND_HI)))


# ── ETS (가법 Holt-Winters, numpy 없이도 되는 순수 파이썬) ──
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
def metrics(act, pred, last_known):
    """커버리지는 밴드 방식별로 따로 재므로(cover()) 여기서는 정확도만 낸다."""
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
    return {"n": n, "mape": round(mape, 3), "smape": round(smape, 3),
            "mae": round(mae, 4), "rmse": round(rmse, 4), "bias": round(bias, 4),
            "dir_acc": round(da, 1)}


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
    acc = {k: [] for k in ("act", "pred", "lk", "b_last", "b_s12", "b_ets",
                           "lo_n", "hi_n", "lo_b", "hi_b", "lo_b60", "hi_b60",
                           "lo_c", "hi_c", "conf_ok")}
    coefs = []
    errs = []          # 표본외 오차 풀(시간순). origin O 는 O 이전 것만 쓴다 → 누출 없음
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
        x0v = np.array(x0, dtype=float)
        beta, s2, XtXi = ols_fit(X, yy)
        mu, se = ols_pred(beta, s2, XtXi, x0v)
        acc["act"].append(actual)
        acc["pred"].append(untf(base, mu, tmode))
        acc["lk"].append(base)
        coefs.append(float(beta[1]))
        # 밴드 3종을 동시에 재서 어느 방식이 맞는지 데이터가 고르게 한다
        acc["lo_n"].append(untf(base, mu - Z80 * se, tmode))
        acc["hi_n"].append(untf(base, mu + Z80 * se, tmode))
        for tag, win in (("b", None), ("b60", RESID_WIN)):
            ql, qh = band_quantiles(X, yy, beta, XtXi, x0v, win)
            acc["lo_" + tag].append(untf(base, mu + ql, tmode) if ql is not None else None)
            acc["hi_" + tag].append(untf(base, mu + qh, tmode) if qh is not None else None)
        # v3) 온라인 conformal — 이 origin 이전의 표본외 오차만 쓴다
        clo, chi = conf_offsets(errs)
        if clo is None:
            acc["lo_c"].append(None); acc["hi_c"].append(None)
            acc["conf_ok"].append(False)
        else:
            acc["lo_c"].append(untf(base, mu + clo, tmode))
            acc["hi_c"].append(untf(base, mu + chi, tmode))
            acc["conf_ok"].append(True)
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
        # 채점이 끝난 뒤에 오차를 보정 풀에 넣는다 — 순서가 뒤바뀌면 누출이다
        errs.append((tf(actual, tmode) - tf(base, tmode)) - mu)

    m = metrics(acc["act"], acc["pred"], acc["lk"])
    if not m:
        return None
    cn, _ = cover(acc["act"], acc["lo_n"], acc["hi_n"])
    cb, _ = cover(acc["act"], acc["lo_b"], acc["hi_b"])
    c60, _ = cover(acc["act"], acc["lo_b60"], acc["hi_b60"])
    cc, n_conf = cover(acc["act"], acc["lo_c"], acc["hi_c"], acc["conf_ok"])
    cn_sub, _ = cover(acc["act"], acc["lo_n"], acc["hi_n"], acc["conf_ok"])
    out = {"model": m, "coef_gas_mean": round(float(np.mean(coefs)), 4) if coefs else None,
           "cover80_norm": cn, "cover80_boot": cb, "cover80_boot60": c60,
           "cover80_conf": cc, "n_conf": n_conf, "cover80_norm_sub": cn_sub,
           "conf": conf_offsets(errs), "n_errs": len(errs)}
    for bn, key in (("naive_last", "b_last"), ("naive_s12", "b_s12"), ("ets", "b_ets")):
        bm = metrics(acc["act"], acc[key], acc["lk"])
        out[bn] = bm
        out["skill_vs_" + bn] = skill(m, bm)
    return out


# ── live 성장경로 ────────────────────────────────────
def growth(name, y, gas_live, gas_hist, h, L, featfns, tmode, gmode, kinds, conf=None):
    """마지막 실적 기준 origin 에서 h 앞을 예측. gas_live 는 실적+forward 병합.
       밴드 우선순위: conformal(백테스트 오차 풀) → boot(학습 잔차) → norm."""
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
    x0v = np.array(x0, dtype=float)
    beta, s2, XtXi = ols_fit(X, yy)
    mu, se = ols_pred(beta, s2, XtXi, x0v)
    base12 = [y[ys(i)] for i in range(last_i - 11, last_i + 1) if ys(i) in y]
    b = (sum(base12) / len(base12)) if base12 else base
    lvl = untf(base, mu, tmode)
    lo_n = untf(base, mu - Z80 * se, tmode)
    hi_n = untf(base, mu + Z80 * se, tmode)
    ql, qh = band_quantiles(X, yy, beta, XtXi, x0v, None)
    lo_b = untf(base, mu + ql, tmode) if ql is not None else lo_n
    hi_b = untf(base, mu + qh, tmode) if qh is not None else hi_n
    if conf and conf[0] is not None:
        lo, hi, method = (untf(base, mu + conf[0], tmode),
                          untf(base, mu + conf[1], tmode), "conformal")
    elif ql is not None:
        lo, hi, method = lo_b, hi_b, "boot"
    else:
        lo, hi, method = lo_n, hi_n, "norm"
    return {"target_ym": t, "level": lvl, "lo": lo, "hi": hi,
            "lo_norm": lo_n, "hi_norm": hi_n, "base": b,
            "band_method": method,
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
    print(f"=== fit_model v3 · {NOW.isoformat()[:19]} ===", flush=True)
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

        conf_by_h = {}                     # (세트, h) → conformal 오프셋. growth 가 재사용한다
        for sname, featfns in sets:
            for h in HORIZONS:
                res = backtest(name, y, gas_hist, h, L, featfns, tmode, gmode)
                if res:
                    conf_by_h[(sname, h)] = res.get("conf")
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
                       "dir_acc": m["dir_acc"],
                       "cover80_norm": res.get("cover80_norm"),
                       "cover80_boot": res.get("cover80_boot"),
                       "cover80_boot60": res.get("cover80_boot60"),
                       "cover80_conf": res.get("cover80_conf"),
                       "n_conf": res.get("n_conf"),
                       "cover80_norm_sub": res.get("cover80_norm_sub"),
                       "skill_vs_naive_last": res.get("skill_vs_naive_last"),
                       "skill_vs_naive_s12": res.get("skill_vs_naive_s12"),
                       "skill_vs_ets": res.get("skill_vs_ets"),
                       "coef_gas": res.get("coef_gas_mean"),
                       "coef_flag": coef_flag(tmode, res.get("coef_gas_mean")),
                       "gate": gate, "gas_path": "realized", "transform": tmode,
                       "run_vintage": TODAY}
                score_rows.append(row)
                print(f"   {sname} h={h:2d} n={m['n']:3d} MAPE={m['mape']:6.2f}% "
                      f"cov80 norm={res.get('cover80_norm')} boot={res.get('cover80_boot')} "
                      f"b60={res.get('cover80_boot60')} "
                      f"CONF={res.get('cover80_conf')}(n={res.get('n_conf')} "
                      f"vs norm_sub={res.get('cover80_norm_sub')}) "
                      f"skill={sk[0]}/{sk[1]}/{sk[2]} → {gate}", flush=True)
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
                g = growth(name, y, gas_live, gas_hist, h, L, featfns, tmode, gmode,
                           kinds, conf_by_h.get((sname, h)))
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
                    "level_lo_norm": round(g["lo_norm"], 4),
                    "level_hi_norm": round(g["hi_norm"], 4),
                    "band_method": g["band_method"],
                    "pass_through": ptr, "base_window": g["base_window"],
                    "base_level": round(g["base"], 4), "gas_path": g["gas_kind"],
                    "n_train": g["n_train"], "coef_gas": g["coef_gas"],
                    "coef_flag": coef_flag(tmode, g["coef_gas"]),
                    "vintage": TODAY})

    # ── 저장 ──
    sc_cols = ["target", "set", "model", "horizon", "n_origins", "mape", "smape", "mae",
               "rmse", "bias", "dir_acc", "cover80_norm", "cover80_boot",
               "cover80_boot60", "cover80_conf", "n_conf", "cover80_norm_sub",
               "skill_vs_naive_last", "skill_vs_naive_s12",
               "skill_vs_ets", "coef_gas", "coef_flag", "gate", "gas_path",
               "transform", "run_vintage"]
    gr_cols = ["target", "market", "entity", "var", "set", "horizon", "ym", "tier", "kind",
               "transform", "index", "index_lo", "index_hi", "level_native", "level_lo",
               "level_hi", "level_lo_norm", "level_hi_norm", "band_method",
               "pass_through", "base_window", "base_level", "gas_path",
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
