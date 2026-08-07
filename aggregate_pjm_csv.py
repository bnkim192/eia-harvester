# -*- coding: utf-8 -*-
"""
PJM 키리스 경로 — Data Miner 2 웹에서 **로그인 없이** 내려받은 CSV를 월평균으로 집계.
(API 키/가입 불필요. 사용자가 CSV를 레포 pjm_csv/ 폴더에 넣기만 하면 GitHub Actions가 처리.)

사용법:
  1) dataminer2.pjm.com → Day-Ahead Hourly LMPs → Pnode=ATSI 필터 → CSV 다운로드
  2) 받은 파일을 레포 `pjm_csv/` 폴더에 커밋(아무 파일명, 연도별 여러 개 OK)
  3) Actions 실행 → pjm_lmp_monthly.json/csv 생성(= fetch_pjm.py와 동일 포맷)

인식 컬럼(Data Miner DA Hourly LMP): datetime_beginning_ept, pnode_name, total_lmp_da
"""
import os, sys, json, csv, io, re, glob

INDIR    = "pjm_csv"
OUT_JSON = "pjm_lmp_monthly.json"
OUT_CSV  = "pjm_lmp_monthly.csv"


def ym_of(s):
    s = str(s)
    m = re.match(r"(\d{4})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)   # M/D/YYYY (Data Miner UI 기본)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}"
    return None


def find_cols(header):
    low = [h.strip().lower() for h in header]
    def pick(cands):
        for i, h in enumerate(low):
            if h in cands:
                return i
        for i, h in enumerate(low):
            if any(c in h for c in cands):
                return i
        return None
    dt = pick(["datetime_beginning_ept", "datetime_beginning_utc", "datetime", "date"])
    nd = pick(["pnode_name", "pnode_id", "pnode", "name"])
    val = pick(["total_lmp_da", "total_lmp_rt", "total_lmp", "lmp"])
    return dt, nd, val


def main():
    files = sorted(glob.glob(os.path.join(INDIR, "*.csv")))
    if not files:
        print(f"[SKIP] {INDIR}/ 에 CSV 없음 — Data Miner CSV를 넣고 다시 실행.", flush=True)
        return
    acc = {}   # node -> {ym: [sum, cnt]}
    for fp in files:
        with open(fp, encoding="utf-8-sig", errors="ignore") as f:
            rd = csv.reader(f)
            rows = list(rd)
        if not rows:
            continue
        dt, nd, val = find_cols(rows[0])
        if dt is None or val is None:
            print(f"[WARN] {os.path.basename(fp)}: 컬럼 인식 실패 header={rows[0][:8]}", flush=True)
            continue
        cnt = 0
        for r in rows[1:]:
            if len(r) <= max(dt, val, (nd if nd is not None else 0)):
                continue
            ym = ym_of(r[dt])
            node = (r[nd].strip() if nd is not None and nd < len(r) else "ATSI")
            try:
                v = float(r[val])
            except Exception:
                continue
            if not ym:
                continue
            a = acc.setdefault(node, {}).setdefault(ym, [0.0, 0])
            a[0] += v; a[1] += 1
            cnt += 1
        print(f"[읽음] {os.path.basename(fp)}: {cnt}행 · 컬럼(date={rows[0][dt]},node={rows[0][nd] if nd is not None else '-'},val={rows[0][val]})", flush=True)

    monthly = {n: {ym: round(s / c, 3) for ym, (s, c) in sorted(m.items()) if c} for n, m in acc.items()}
    if not any(monthly.values()):
        print("!! 집계 0행 — CSV 형식/컬럼 확인", flush=True); sys.exit(1)
    out = {
        "source": "PJM Data Miner DA Hourly LMP (수동 CSV 집계, 키리스) — monthly avg",
        "unit": "$/MWh", "nodes": list(monthly.keys()), "data": monthly,
    }
    json.dump(out, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f); w.writerow(["node", "ym", "lmp_avg_usd_mwh"])
        for n, m in monthly.items():
            for ym, v in m.items():
                w.writerow([n, ym, v])
    print(f"[OK] {OUT_JSON} · months={ {n: len(m) for n, m in monthly.items()} }", flush=True)


if __name__ == "__main__":
    main()
