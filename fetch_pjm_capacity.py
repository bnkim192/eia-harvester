# -*- coding: utf-8 -*-
"""
PJM 용량경매(RPM Base Residual Auction) 낙찰가 수집기 — GitHub Actions 전용.
v3 (2026-08-24): 프로브 -> 수집기 승격(v2) 후, 1차 실행 결과로 파서를 고쳤다(v3).

  · PJM RPM 페이지 200 · 데이터파일링크 396건 발견 (URL 추측 없이 본문에서)
  · rpm-auction-info/ 디렉터리가 2007-2008 ~ 2029-2030 까지 존재
  · BRA 보고서 PDF 는 2021-2022 ~ 2028-2029 **8개 연도** 확보 가능
  · pypdf 본문 추출 성공 — 실제로 읽어낸 줄 (2028/29 BRA):
        ATSI            7,519.1  7,519.1  $325.00  $0.00  $325.00
        ATSI-CLEVELAND  1,267.4  1,267.4  $325.00  $0.00  $325.00
        RTO           139,594.6 138,317.8 $325.00  $0.00  $325.00
  · api.pjm.com 401 (PJM_API_KEY Secret 비어있음) · GRIDSTATUS_API_KEY 도 비어있음
    → 둘 다 이 경로에 불필요하다. 공개 PDF 로 충분하다.

■ 왜 필요한가
  2026-08-24 walk-forward 에서 오하이오 급등 48pp 중 **가스 설명분 약 11pp** 이고
  **비연료(용량·송전) 몫이 약 37pp** 임을 특정했다. ATSI 는 오하이오 사업장이 속한
  LDA 이고 RTO 전체가와 갈린다 — RTO 만 받으면 의미가 없다.

■ forward 로서의 가치
  BRA 는 인도연도 **이전에** 확정된다. 2028-2029 까지 확보되면 인도월 2029-05 까지
  **이미 알려진 값**이다. 파일럿의 +36개월 상한과 정확히 맞는다.

■ 원칙
  1. URL 을 추측해 박지 않는다. 인덱스 페이지 본문에서 링크를 **발견**한다.
  2. 값을 만들어내지 않는다. PDF 에서 실제로 읽은 줄만 쓴다.
  3. **모호하면 버리지 말고 표시한다.** 한 LDA·연도에 서로 다른 가격이 2개 이상 나오면
     CSV 에는 둘 다 남기고 월별 시계열에서는 **제외**한다. 하나를 골라 넣지 않는다.
  4. 증분 수집 — 이미 CSV 에 있는 연도는 다시 내려받지 않는다(BRA 결과는 확정 후 불변).
     전량 재수집은 PJM_CAP_REFRESH=1. 파서 버전이 바뀌면 자동 무효화된다.
  5. 종료코드는 항상 0.

■ 산출
  pjm_capacity_bra.csv        연도·LDA·낙찰가 원장 (모호건 포함, 플래그로 구분)
  pjm_capacity_monthly.json   인도월 확장 시계열 (steo_forward_monthly.json 과 동일 스키마)
  pjm_capacity_probe.json     후보 판정 + 발견 링크
  pjm_capacity_lines.txt      추출 원문 줄 (검증용)

■ 의존
  requests · pypdf
"""
import os, sys, json, csv, re, io, time
import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      "Accept-Language": "en-US,en;q=0.9"}
TIMEOUT   = 90
API_SLEEP = 11          # api.pjm.com 익명 분당 6회 (collect.yml 기록 제약)
MAX_REQ   = 140
REFRESH   = os.environ.get("PJM_CAP_REFRESH", "").strip() == "1"
# 파서를 고치면 캐시는 무효다. CSV 에 이 값을 박아두고 다르면 자동 전량 재수집한다.
# (v1 은 구형 전치표를 못 읽고 simulated 페이지를 실제값으로 잡았다 — 그 CSV 를 이어쓰면 안 된다.)
PARSER_VER = "v3-2026-08-24"

OUT_CSV   = "pjm_capacity_bra.csv"
OUT_JSON  = "pjm_capacity_monthly.json"
OUT_PROBE = "pjm_capacity_probe.json"
OUT_LINES = "pjm_capacity_lines.txt"

# 월별 시계열로 내보낼 LDA. ATSI 가 오하이오, RTO 는 대조군.
WANT_LDA = ("ATSI", "ATSI-CLEVELAND", "RTO", "COMED", "DAY", "DEOK")

PROBE, FOUND, LINES, ROWS = [], [], [], []
_req = {"n": 0, "api_last": 0.0}


def log(*a):
    print(*a, flush=True)


def rec(pid, url, http=None, ok=False, note=""):
    PROBE.append({"id": pid, "url": url, "http": http, "ok": bool(ok), "note": note[:500]})
    log("[%s] %-30s http=%s %s" % ("OK " if ok else "NG ", pid, http, note[:200]))


def get(url, headers=None):
    if _req["n"] >= MAX_REQ:
        raise RuntimeError("MAX_REQ 상한 도달")
    _req["n"] += 1
    if "api.pjm.com" in url:
        gap = time.time() - _req["api_last"]
        if gap < API_SLEEP:
            time.sleep(API_SLEEP - gap)
        _req["api_last"] = time.time()
    h = dict(UA)
    if headers:
        h.update(headers)
    return requests.get(url, headers=h, timeout=TIMEOUT, allow_redirects=True)


# ══════════════════════════════════════════════════════════════════
#  1. 링크 발견 — 인덱스 페이지 본문에서
# ══════════════════════════════════════════════════════════════════
INDEX = [
    ("PJM_rpm",      "https://www.pjm.com/markets-and-operations/rpm"),
    ("PJM_rpm_user", "https://www.pjm.com/markets-and-operations/rpm/rpm-auction-user-info"),
    ("PJM_sitemap",  "https://www.pjm.com/sitemap.xml"),
]
FILE_RE = re.compile(r'''(?:href|HREF)\s*=\s*["']([^"']+\.(?:pdf|xlsx|xls|csv|zip))["']''')
LOC_RE  = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
KEY_RE  = re.compile(r"(rpm|capacity|base[-_ ]?residual|\bbra\b|auction)", re.I)
SUB_RE  = re.compile(r'''(?:href|HREF)\s*=\s*["']([^"']*(?:rpm|capacity|auction)[^"']*)["']''', re.I)

# BRA 결과보고서만. 보조자료(공급곡선·계획파라미터·증분경매·FRR 등)는 제외한다.
BRA_RE  = re.compile(r"(base-residual-auction-report|bra-results-report|bra-report)\.pdf$", re.I)
YEAR_RE = re.compile(r"rpm-auction-info/(\d{4})-(\d{4})/", re.I)


def absolutize(base, href):
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    m = re.match(r"(https?://[^/]+)", base)
    root = m.group(1) if m else ""
    return (root + href) if href.startswith("/") else (base.rsplit("/", 1)[0] + "/" + href)


def scan(pid, url, depth=0):
    try:
        r = get(url)
    except Exception as e:
        rec(pid, url, None, False, "EXC %s" % e)
        return
    if r.status_code != 200:
        rec(pid, url, r.status_code, False, r.text[:150])
        return
    body, hits = r.text, []
    if url.endswith(".xml"):
        hits = [l for l in LOC_RE.findall(body) if KEY_RE.search(l)]
    else:
        hits = [absolutize(url, h) for h in FILE_RE.findall(body) if KEY_RE.search(h)]
    hits = list(dict.fromkeys(hits))
    FOUND.extend(hits)
    rec(pid, url, 200, bool(hits), "링크 %d건 · 본문 %dKB" % (len(hits), len(body) // 1024))

    if depth == 0 and not url.endswith(".xml"):
        subs = [absolutize(url, h) for h in SUB_RE.findall(body)]
        subs = [s for s in dict.fromkeys(subs)
                if s.startswith("http") and s != url
                and not re.search(r"\.(pdf|xlsx?|csv|zip)$", s, re.I)][:4]
        for i, s in enumerate(subs):
            scan("%s_sub%d" % (pid, i + 1), s, depth=1)


# ══════════════════════════════════════════════════════════════════
#  2. PDF 에서 LDA 낙찰가 추출 — 보고서가 두 가지 레이아웃을 쓴다
#
#  (A) 최신형(2025/26~) : LDA 한 줄씩
#        "ATSI 7,519.1 7,519.1 $325.00 $0.00 $325.00"
#        마지막 $ 가 총 낙찰가다($325.00 + $0.00 = $325.00 로 검증됨).
#
#  (B) 구형(2021/22~2024/25) : **표가 전치되어 있다.** LDA 가 열 머리글이고 값이 한 줄에 몰린다.
#        머리글  "RTO MAAC EMAAC SWMAAC BGE PEPCO DPL-SOUTH ..."
#        값줄    "RCP for Capacity Performance Resources $28.92 $49.49 ... $96.24"
#        1차 실행에서 구형 4개 연도가 1~2행만 잡힌 원인이 이것이었다.
#        → 같은 페이지의 머리글 후보와 값 개수가 **정확히 일치할 때만** zip 한다.
#          개수가 어긋나면 추측하지 않고 ambiguous 로 남긴다.
#
#  ★ '시뮬레이션' 페이지 제외 — 1차 실행에서 정체불명이던 $388.57 / $529.80 / $542.83 은
#    보고서가 스스로 밝혔다. 20쪽 표의 근거 문장이 추출됐다:
#      "DOM LDA which cleared at $542.83. In the 2026/2027 simulated BRA,
#       all prices cleared at $388.57."
#    즉 20쪽은 **가정 시나리오(simulated/estimated)** 이고 실제 낙찰가는 9~10쪽 Table 3 이다.
#    따라서 'simulat' 또는 'estimated ... clearing price' 가 있는 페이지는 버린다.
#
#  ★ 산문 줄 배제 — 1차 실행에서 "COMED and DEOK LDA were constrained ... $187.87/MW-day"
#    같은 **본문 문장**이 표 행으로 잡혔다. 실제 표 행에는 소문자가 없다. 소문자가 있으면 버린다.
# ══════════════════════════════════════════════════════════════════
DOL_RE  = re.compile(r"\$\s*(-?[\d,]+\.\d{2})")
HEAD_RE = re.compile(r"^([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*)\s+")
STOP    = {"TOTAL", "TABLE", "THE", "RTO-WIDE", "NOTE", "SOURCE", "UCAP", "ICAP",
           "MW", "AND", "FOR", "ALL", "PJM", "LDA", "BRA", "IA", "RCP", "CP"}
CTX_RE  = re.compile(r"(Table\s+\d+[^\n]{0,120}|Resource Clearing Price[^\n]{0,60})", re.I)
SIM_RE  = re.compile(r"simulat|estimated\s+\S{0,12}\s*clearing\s+price", re.I)
LDA_TOK = re.compile(r"^[A-Z][A-Z0-9]{1,14}(?:-[A-Z0-9]+)*$")
RCP_RE  = re.compile(r"^\s*RCP\b|Resource\s+Clearing\s+Price", re.I)


def _push(year, lda, price, dol, page, incr, hint, url, raw, layout):
    ROWS.append({"delivery_year": year, "lda": lda,
                 "price_usd_mwday": price,
                 "all_dollar_fields": "|".join(dol),
                 "page": page, "incremental": incr,
                 "table_hint": hint, "layout": layout,
                 "source_url": url, "raw_line": raw[:180]})


def parse_pdf(url, year):
    from pypdf import PdfReader
    r = get(url)
    if r.status_code != 200 or len(r.content) < 5000:
        rec("PDF_" + year, url, r.status_code, False, "len=%d" % len(r.content))
        return 0
    rd = PdfReader(io.BytesIO(r.content))
    got, skipped_sim = 0, 0
    LINES.append("=" * 78)
    LINES.append("SOURCE %s  (인도연도 %s)" % (url, year))
    for pi, pg in enumerate(rd.pages[:60]):
        try:
            txt = pg.extract_text() or ""
        except Exception:
            continue
        if SIM_RE.search(txt):                       # 가정 시나리오 페이지는 버린다
            skipped_sim += 1
            LINES.append("p%-3d [SKIP simulated/estimated 페이지]" % (pi + 1))
            continue
        incr = bool(re.search(r"Incremental Auction", txt, re.I))
        ctx = CTX_RE.search(txt)
        hint = re.sub(r"\s+", " ", ctx.group(1))[:120] if ctx else ""

        rows_here = []
        heads, rcps = [], []
        for ln in txt.splitlines():
            ln = " ".join(ln.split())
            if not ln:
                continue
            # ── (B) 전치표 재료 수집 ─────────────────────────────
            toks = ln.split()
            if len(toks) >= 6 and all(LDA_TOK.match(t) for t in toks):
                heads.append(toks)                    # 머리글 후보
            if RCP_RE.search(ln):
                d = DOL_RE.findall(ln)
                if len(d) >= 6:
                    rcps.append((ln, d))
            # ── (A) LDA 한 줄씩 ────────────────────────────────
            if "$" not in ln or re.search(r"[a-z]", ln):
                continue                              # 산문 배제
            hm = HEAD_RE.match(ln)
            if not hm:
                continue
            lda = hm.group(1)
            if lda in STOP or len(lda) < 2:
                continue
            pre = ln.split("$", 1)[0]
            if len(re.findall(r"[\d,]+\.\d", pre)) < 2:
                continue                              # MW 2개가 앞에 있어야 표 행이다
            dol = DOL_RE.findall(ln)
            if not dol:
                continue
            rows_here.append((lda, float(dol[-1].replace(",", "")), dol, ln))

        for lda, price, dol, ln in rows_here:
            _push(year, lda, price, dol, pi + 1, incr, hint, url, ln, "per-row")
            LINES.append("p%-3d %s%s" % (pi + 1, "[IA] " if incr else "", ln[:180]))
            got += 1

        # (B) 전치표 — 머리글 개수와 값 개수가 정확히 같을 때만
        if not rows_here and rcps:
            for ln, d in rcps:
                match = [h for h in heads if len(h) == len(d)]
                if not match:
                    LINES.append("p%-3d [전치표 미해결] 값 %d개 · 머리글후보 %s"
                                 % (pi + 1, len(d), [len(h) for h in heads]))
                    _push(year, "_UNRESOLVED_TRANSPOSED", -1.0,
                          d, pi + 1, incr, hint, url, ln, "transposed-unresolved")
                    continue
                hdr = match[0]
                LINES.append("p%-3d [전치표 머리글] %s" % (pi + 1, " ".join(hdr)))
                LINES.append("p%-3d [전치표 값]     %s" % (pi + 1, " ".join(d)))
                for lda, v in zip(hdr, d):
                    if lda in STOP:
                        continue
                    _push(year, lda, float(v.replace(",", "")), [v],
                          pi + 1, incr, hint, url, ln, "transposed")
                    got += 1
        if got > 600:
            break
    rec("PDF_" + year, url, 200, got > 0,
        "pages=%d 행 %d개 · simulated 페이지 %d개 제외" % (len(rd.pages), got, skipped_sim))
    return got


# ══════════════════════════════════════════════════════════════════
#  3. 기존 CSV 로드 (증분 수집)
# ══════════════════════════════════════════════════════════════════
def load_existing():
    if REFRESH or not os.path.exists(OUT_CSV):
        return [], set()
    try:
        with open(OUT_CSV, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        stale = [r for r in rows if r.get("parser_ver") != PARSER_VER]
        if stale:
            log("[캐시무효] parser_ver 불일치 %d행 → 전량 재수집 (기대 %s)"
                % (len(stale), PARSER_VER))
            return [], set()
        for r in rows:
            r["price_usd_mwday"] = float(r["price_usd_mwday"])
            r["page"] = int(r["page"] or 0)
            r["incremental"] = str(r.get("incremental", "")).lower() == "true"
        return rows, {r["delivery_year"] for r in rows}
    except Exception as e:
        log("!! 기존 CSV 읽기 실패(무시하고 전량 수집): %s" % e)
        return [], set()


# ══════════════════════════════════════════════════════════════════
#  4. 인도연도 → 인도월 확장 (6월~다음해 5월)
#     한 LDA·연도에 서로 다른 가격이 2개 이상이면 **월별 시계열에서 제외**한다.
# ══════════════════════════════════════════════════════════════════
def build_monthly(rows):
    base = {}
    for r in rows:
        if r["incremental"]:
            continue                                  # 증분경매는 BRA 가 아니다
        if str(r["lda"]).startswith("_") or float(r["price_usd_mwday"]) <= 0:
            continue                                  # 미해결 전치표 표식은 시계열에 넣지 않는다
        base.setdefault((r["lda"], r["delivery_year"]), set()).add(round(float(r["price_usd_mwday"]), 2))
    series, dropped = {}, []
    for (lda, yr), prices in sorted(base.items()):
        if lda not in WANT_LDA:
            continue
        if len(prices) != 1:
            dropped.append({"lda": lda, "delivery_year": yr, "prices": sorted(prices)})
            continue
        p = list(prices)[0]
        y0 = int(yr.split("-")[0])
        s = series.setdefault(lda, [])
        for k in range(12):                            # 6월 시작
            t = y0 * 12 + 5 + k
            s.append({"period": "%04d-%02d" % (t // 12, t % 12 + 1), "value": p})
    for lda in series:
        series[lda].sort(key=lambda x: x["period"])
    return series, dropped


# ══════════════════════════════════════════════════════════════════
def main():
    log("=== fetch_pjm_capacity.py %s  refresh=%s ===" % (PARSER_VER, REFRESH))
    for pid, u in INDEX:
        try:
            scan(pid, u)
        except Exception as e:
            log("!! scan %s: %s" % (pid, e))

    # BRA 보고서만 골라 연도별로 묶는다
    by_year = {}
    for u in dict.fromkeys(FOUND):
        if not BRA_RE.search(u):
            continue
        m = YEAR_RE.search(u)
        if not m:
            continue
        by_year.setdefault("%s-%s" % (m.group(1), m.group(2)), []).append(u)
    log("[BRA] 연도 %d개 발견: %s" % (len(by_year), sorted(by_year)))

    old, have = load_existing()
    if have:
        log("[증분] 기존 CSV 연도 %d개 보유 → 건너뜀: %s" % (len(have), sorted(have)))

    try:
        from pypdf import PdfReader   # noqa: F401
        ok_pdf = True
    except Exception as e:
        ok_pdf = False
        rec("pypdf", "-", None, False, "미설치(%s) — 링크 발견까지만" % e)

    if ok_pdf:
        for yr in sorted(by_year):
            if yr in have:
                continue
            for u in sorted(by_year[yr], key=len, reverse=True):   # 결과보고서(긴 이름) 우선
                try:
                    if parse_pdf(u, yr):
                        break
                except Exception as e:
                    rec("PDF_" + yr, u, None, False, "EXC %s" % e)

    allrows = old + ROWS
    series, dropped = build_monthly(allrows)

    log("\n[요약] 요청 %d회 · 링크 %d건 · 신규행 %d · 총행 %d"
        % (_req["n"], len(dict.fromkeys(FOUND)), len(ROWS), len(allrows)))
    for lda in sorted(series):
        p = series[lda]
        log("  %-16s %s~%s (%d개월) 값 %s"
            % (lda, p[0]["period"], p[-1]["period"], len(p),
               sorted({x["value"] for x in p})))
    for d in dropped:
        log("  !! 모호 제외 %s %s prices=%s" % (d["lda"], d["delivery_year"], d["prices"]))

    with open(OUT_PROBE, "w", encoding="utf-8") as f:
        json.dump({"probe": PROBE, "braYears": sorted(by_year),
                   "files": list(dict.fromkeys(FOUND))[:400],
                   "ambiguous": dropped}, f, ensure_ascii=False, indent=2)
    log("[OK] %s" % OUT_PROBE)

    if allrows:
        cols = ["delivery_year", "lda", "price_usd_mwday", "all_dollar_fields",
                "page", "incremental", "layout", "parser_ver",
                "table_hint", "source_url", "raw_line"]
        with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in sorted(allrows, key=lambda x: (x["delivery_year"], x["lda"], x["page"])):
                r.setdefault("layout", "")
                r["parser_ver"] = PARSER_VER
                w.writerow(r)
        log("[OK] %s (%d행)" % (OUT_CSV, len(allrows)))
    else:
        log("!! 행 0개 — CSV 미갱신(기존 보존)")

    if series:
        data = {}
        for lda, pts in series.items():
            data[lda] = {"label": "PJM RPM BRA 낙찰가 " + lda,
                         "unit": "USD/MW-day (UCAP)",
                         "source": "PJM RPM Base Residual Auction Report (공개 PDF)",
                         "points": pts}
        with open(OUT_JSON, "w", encoding="utf-8") as f:
            json.dump({"source": "PJM RPM BRA 공개보고서 PDF 추출",
                       "note": "인도연도(6월~다음해5월)를 월로 확장한 계단형이다. "
                               "BRA 는 인도 전에 확정되므로 최신 연도는 forward 로 쓸 수 있다. "
                               "ATSI 가 오하이오(GM1·HD) LDA 이고 RTO 와 갈린다.",
                       "data": data}, f, ensure_ascii=False, indent=2)
        log("[OK] %s (계열 %d)" % (OUT_JSON, len(series)))
    else:
        log("!! 월별 계열 0개 — JSON 미갱신")

    if LINES:
        with open(OUT_LINES, "w", encoding="utf-8") as f:
            f.write("\n".join(LINES))
        log("[OK] %s (%d줄)" % (OUT_LINES, len(LINES)))


if __name__ == "__main__":
    main()
