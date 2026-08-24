# -*- coding: utf-8 -*-
"""
PJM 용량경매(RPM Base Residual Auction) 낙찰가 프로브 — GitHub Actions 전용.

■ 왜 지금 이걸 하나
  2026-08-24 walk-forward 검증에서 오하이오 급등 48pp 중 **가스 설명분이 약 11pp(23%)**
  이고 **비연료 요인이 약 37pp** 임을 특정했다(08_무료연료데이터_설명력측정 §8.4).
  즉 GM1·HD 의 MAPE 15.25 를 내리려면 용량·송전 성분이 필요하다. 헨리허브로는
  방향만 맞고 크기가 3.7배 부족했다.
  슈나이더 요청은 포기했고, 사내망은 pjm.com 을 차단한다. **그러나 이 러너는 차단되지 않는다.**

■ 기존 PJM 스크립트와 겹치지 않는다
  fetch_pjm_lmp.py --discover 는 **LMP(에너지 가격) API** 만 때렸다.
  용량경매 결과는 API 가 아니라 **공개 보고서(PDF/XLS)** 로 공표되는 별개 산출물이고
  한 번도 조회한 적이 없다.

■ 원칙 (fetch_fuel.py 와 동일)
  1. URL 을 추측해 박지 않는다. 인덱스 페이지를 받아 **본문에서 링크를 발견**한다.
     World Bank Pink Sheet 에서 통한 방식이다.
  2. 파싱 못 하는 건 상태코드·본문머리만 남기는 프로브로 둔다.
  3. 값을 만들어내지 않는다. PDF 에서 실제로 읽어낸 줄만 출력한다.
  4. 종료코드는 항상 0. 판정은 pjm_capacity_probe.json 과 로그로 한다.

■ 요율제한 준수
  collect.yml 에 기록된 제약 — **api.pjm.com 익명은 분당 6회**다. 그 호스트에만
  11초 간격을 강제한다(API_SLEEP). 여러 스크립트가 같은 엔드포인트를 연달아 쳐서
  서로를 429 로 밀어내고 '경로가 죽었다' 는 잘못된 판정을 만드는 것을 막는다.

■ 산출
  pjm_capacity_probe.json   후보별 판정 + 발견한 데이터파일 링크 목록
  pjm_capacity_lines.txt    PDF 에서 실제로 추출된 낙찰가 관련 줄 (pypdf 있을 때만)

■ 의존
  requests (필수) · pypdf (있으면 PDF 본문 추출, 없으면 링크 발견까지만)
"""
import os, sys, json, re, io, time
import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      "Accept-Language": "en-US,en;q=0.9"}
TIMEOUT   = 60
API_SLEEP = 11          # api.pjm.com 익명 분당 6회 → 11초 간격
MAX_REQ   = 70          # 폭주 방지 상한
OUT_PROBE = "pjm_capacity_probe.json"
OUT_LINES = "pjm_capacity_lines.txt"

PROBE, FOUND, LINES = [], [], []
_req = {"n": 0, "api_last": 0.0}


def log(*a):
    print(*a, flush=True)


def rec(pid, url, http=None, ok=False, note=""):
    PROBE.append({"id": pid, "url": url, "http": http, "ok": bool(ok), "note": note[:500]})
    log("[%s] %-26s http=%s %s" % ("OK " if ok else "NG ", pid, http, note[:200]))


def get(url, headers=None, stream=False):
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
    return requests.get(url, headers=h, timeout=TIMEOUT, stream=stream, allow_redirects=True)


# ══════════════════════════════════════════════════════════════════
#  1. 인덱스 페이지 → 본문에서 데이터파일 링크 발견
# ══════════════════════════════════════════════════════════════════
INDEX = [
    ("PJM_rpm",       "https://www.pjm.com/markets-and-operations/rpm"),
    ("PJM_rpm_user",  "https://www.pjm.com/markets-and-operations/rpm/rpm-auction-user-info"),
    ("PJM_sitemap",   "https://www.pjm.com/sitemap.xml"),
    ("IMM_som_index", "https://www.monitoringanalytics.com/reports/PJM_State_of_the_Market/"),
    ("IMM_reports",   "https://www.monitoringanalytics.com/reports/"),
]
# 파일 링크 판정: 확장자 + 용량경매 관련 키워드
FILE_RE = re.compile(r'''(?:href|HREF)\s*=\s*["']([^"']+\.(?:pdf|xlsx|xls|csv|zip))["']''')
LOC_RE  = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
KEY_RE  = re.compile(r"(rpm|capacity|base[-_ ]?residual|\bbra\b|auction)", re.I)
SUB_RE  = re.compile(r'''(?:href|HREF)\s*=\s*["']([^"']*(?:rpm|capacity|auction)[^"']*)["']''', re.I)


def absolutize(base, href):
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    m = re.match(r"(https?://[^/]+)", base)
    root = m.group(1) if m else ""
    if href.startswith("/"):
        return root + href
    return base.rsplit("/", 1)[0] + "/" + href


def scan(pid, url, depth=0):
    try:
        r = get(url)
    except Exception as e:
        rec(pid, url, None, False, "EXC %s" % e)
        return
    if r.status_code != 200:
        rec(pid, url, r.status_code, False, r.text[:150])
        return
    body = r.text
    hits = []
    if url.endswith(".xml"):                       # sitemap 은 <loc> 로 온다
        for loc in LOC_RE.findall(body):
            if KEY_RE.search(loc):
                hits.append(loc)
    else:
        for href in FILE_RE.findall(body):
            if KEY_RE.search(href):
                hits.append(absolutize(url, href))
    hits = list(dict.fromkeys(hits))
    for h in hits:
        FOUND.append({"from": pid, "url": h})
    rec(pid, url, 200, bool(hits), "데이터파일링크 %d건 · 본문 %dKB" % (len(hits), len(body) // 1024))

    if depth == 0 and not url.endswith(".xml"):    # 한 단계만 더 들어간다
        subs = []
        for href in SUB_RE.findall(body):
            a = absolutize(url, href)
            if a.startswith("http") and not re.search(r"\.(pdf|xlsx?|csv|zip)$", a, re.I):
                subs.append(a)
        subs = [s for s in dict.fromkeys(subs) if s != url][:4]
        for i, s in enumerate(subs):
            scan("%s_sub%d" % (pid, i + 1), s, depth=1)


# ══════════════════════════════════════════════════════════════════
#  2. API 후보 — 파라미터를 상상해 채우지 않는다. 살아있는지만.
#     GRIDSTATUS_API_KEY 는 이미 레포 Secret 에 있다(collect.yml 참조) → 데이터셋 목록을
#     받아 capacity 관련 항목이 있는지 **목록으로** 확인한다. 이름을 추측하지 않는다.
# ══════════════════════════════════════════════════════════════════
def probe_apis():
    pk = os.environ.get("PJM_API_KEY", "").strip()
    gk = os.environ.get("GRIDSTATUS_API_KEY", "").strip()

    for pid, u in [("PJM_api_root", "https://api.pjm.com/api/v1/"),
                   ("PJM_dm2_root", "https://dataminer2.pjm.com/")]:
        try:
            h = {"Ocp-Apim-Subscription-Key": pk} if (pk and "api.pjm.com" in u) else None
            r = get(u, headers=h)
            ct = r.headers.get("Content-Type", "")
            rec(pid, u, r.status_code, r.status_code == 200,
                "key=%s ct=%s | %s" % ("있음" if pk else "없음", ct,
                                       re.sub(r"\s+", " ", r.text[:200])))
        except Exception as e:
            rec(pid, u, None, False, "EXC %s" % e)

    if not gk:
        rec("GS_datasets", "https://api.gridstatus.io/v1/datasets", None, False,
            "GRIDSTATUS_API_KEY 비어있음 — Secret 확인")
        return
    u = "https://api.gridstatus.io/v1/datasets"
    try:
        r = get(u, headers={"x-api-key": gk})
        if r.status_code != 200:
            rec("GS_datasets", u, r.status_code, False, r.text[:200])
            return
        j = r.json()
        items = j if isinstance(j, list) else (j.get("data") or j.get("datasets") or [])
        names = []
        for it in items:
            s = it if isinstance(it, str) else (it.get("id") or it.get("name") or "")
            if s:
                names.append(s)
        cap = [n for n in names if re.search(r"capacity|rpm|auction", n, re.I)]
        pjm = [n for n in names if n.lower().startswith("pjm")]
        rec("GS_datasets", u, 200, bool(cap),
            "전체 %d개 · 용량관련 %s · PJM계열 %d개 %s"
            % (len(names), cap[:12], len(pjm), pjm[:12]))
    except Exception as e:
        rec("GS_datasets", u, None, False, "EXC %s" % e)


# ══════════════════════════════════════════════════════════════════
#  3. 발견한 PDF 에서 낙찰가 줄만 실제 추출 (pypdf 있을 때만)
#     ATSI = 오하이오 사업장이 속한 LDA. RTO 전체가와 갈린다.
# ══════════════════════════════════════════════════════════════════
HIT_RE = re.compile(r"(MW-day|MW-Day|Clearing Price|Resource Clearing|ATSI|COMED|RTO)", re.I)


def extract_pdfs(limit=4):
    try:
        from pypdf import PdfReader
    except Exception as e:
        rec("pypdf", "-", None, False, "미설치(%s) — 링크 발견까지만 수행" % e)
        return
    cands = [f for f in FOUND if f["url"].lower().endswith(".pdf")]
    # 파일명에 BRA/base residual/capacity 가 든 것을 먼저
    cands.sort(key=lambda f: 0 if re.search(r"base[-_ ]?residual|\bbra\b", f["url"], re.I) else 1)
    for f in cands[:limit]:
        u = f["url"]
        try:
            r = get(u)
            if r.status_code != 200 or len(r.content) < 5000:
                rec("PDF_" + u.rsplit("/", 1)[-1][:24], u, r.status_code, False,
                    "len=%d" % len(r.content))
                continue
            rd = PdfReader(io.BytesIO(r.content))
            got = 0
            LINES.append("=" * 70)
            LINES.append("SOURCE " + u)
            for pi, pg in enumerate(rd.pages[:40]):
                try:
                    txt = pg.extract_text() or ""
                except Exception:
                    continue
                for ln in txt.splitlines():
                    ln = ln.strip()
                    if len(ln) > 3 and HIT_RE.search(ln):
                        LINES.append("p%-3d %s" % (pi + 1, ln[:200]))
                        got += 1
                if got > 400:
                    break
            rec("PDF_" + u.rsplit("/", 1)[-1][:24], u, 200, got > 0,
                "pages=%d 낙찰가관련줄 %d개" % (len(rd.pages), got))
        except Exception as e:
            rec("PDF_" + u.rsplit("/", 1)[-1][:24], u, None, False, "EXC %s" % e)


# ══════════════════════════════════════════════════════════════════
def main():
    log("=== fetch_pjm_capacity.py (프로브) ===")
    for pid, u in INDEX:
        try:
            scan(pid, u)
        except Exception as e:
            log("!! scan %s 실패: %s" % (pid, e))
    try:
        probe_apis()
    except Exception as e:
        log("!! probe_apis 실패: %s" % e)
    try:
        extract_pdfs()
    except Exception as e:
        log("!! extract_pdfs 실패: %s" % e)

    uniq = list(dict.fromkeys([f["url"] for f in FOUND]))
    log("\n[요약] 요청 %d회 · 후보판정 %d건 · 데이터파일링크 %d건 · 추출줄 %d개"
        % (_req["n"], len(PROBE), len(uniq), len(LINES)))
    for u in uniq[:40]:
        log("   링크 " + u)

    with open(OUT_PROBE, "w", encoding="utf-8") as f:
        json.dump({"probe": PROBE, "files": uniq, "extractedLines": len(LINES)},
                  f, ensure_ascii=False, indent=2)
    log("[OK] %s" % OUT_PROBE)

    if LINES:
        with open(OUT_LINES, "w", encoding="utf-8") as f:
            f.write("\n".join(LINES))
        log("[OK] %s (%d줄)" % (OUT_LINES, len(LINES)))
    else:
        log("[SKIP] 추출줄 0 — PDF 미발견이거나 pypdf 없음. 출력파일 미생성")


if __name__ == "__main__":
    main()
