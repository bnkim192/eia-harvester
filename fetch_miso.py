# -*- coding: utf-8 -*-
"""MISO DA Ex-Post LMP → 월평균 $/MWh (키 불필요).
구조: Node(A)·Type(B:Hub/Loadzone/Gennode)·Value(C:LMP/MCC/MLC)·HE 1..24
대상: CONS.AZ = 미시간(Consumers Energy) 허브."""
import os, sys, json, csv, io, time
from datetime import date, timedelta
import requests

NODES = ["MICHIGAN.HUB"]     # 미시간 대표 허브. [Hub 노드 목록] 로그 보고 교체/추가 가능
BASE  = "https://docs.misoenergy.org/marketreports/{ymd}_da_expost_lmp.csv"
OUT_JSON = "miso_lmp_monthly.json"; OUT_CSV = "miso_lmp_monthly.csv"
BACKFILL_START = os.environ.get("MISO_START", "2026-01-01")   # 첫 실행 백필 시작(가볍게 올해부터)

def load_state():
    if os.path.exists(OUT_JSON):
        try:
            j = json.load(open(OUT_JSON, encoding="utf-8")); return j.get("acc", {}), j.get("last_date")
        except Exception: pass
    return {}, None

def parse_day(text, diag=False):
    out = {}; rows = list(csv.reader(io.StringIO(text)))
    hi=None; node_j=0; val_j=None; type_j=None; he=[]
    for i, r in enumerate(rows):
        up=[c.strip().upper() for c in r]
        if "NODE" in up and any(c.replace(" ","").startswith("HE") for c in up):
            hi=i; node_j=up.index("NODE")
            val_j=up.index("VALUE") if "VALUE" in up else None
            type_j=up.index("TYPE") if "TYPE" in up else None
            he=[j for j,c in enumerate(up) if c.replace(" ","").startswith("HE")]; break
    if hi is None: return out, "header not found"
    if diag and type_j is not None:
        hubs=sorted({r[node_j].strip() for r in rows[hi+1:] if len(r)>type_j and r[type_j].strip().upper()=="HUB"})
        print("[Hub 노드 목록]", hubs, flush=True)
    for r in rows[hi+1:]:
        if not r or len(r)<=(max(he) if he else 0): continue
        node=r[node_j].strip()
        val=(r[val_j].strip().upper() if (val_j is not None and val_j<len(r)) else "LMP")
        if node in NODES and val=="LMP":
            vals=[]
            for j in he:
                try: vals.append(float(r[j]))
                except Exception: pass
            if vals: out.setdefault(node, []).extend(vals)
    return out, None

def main():
    acc, last = load_state()
        _need = (not last) or (not acc) or any(n not in acc for n in NODES)
    if _need: acc = {}
    start = date.fromisoformat(BACKFILL_START) if _need else (date.fromisoformat(last)+timedelta(days=1))
    end=date.today()-timedelta(days=1)
    print(f"[RANGE] {start} ~ {end} (last={last})", flush=True)
    d, fetched, first = start, 0, True
    while d <= end:
        ymd=d.strftime("%Y%m%d")
        try:
            r=requests.get(BASE.format(ymd=ymd), timeout=60)
            if r.status_code==200:
                day, err = parse_day(r.text, diag=first)
                if first: print(f"[HTTP] 200 {ymd} nodesFound={list(day.keys())} err={err}", flush=True); first=False
                ym=d.strftime("%Y-%m")
                for node, vals in day.items():
                    a=acc.setdefault(node, {}).setdefault(ym, [0.0,0]); a[0]+=sum(vals); a[1]+=len(vals)
                fetched+=1
        except Exception as e:
            print(f"[ERR] {ymd} {e}", flush=True)
        d+=timedelta(days=1)
        if fetched and fetched%25==0: time.sleep(0.3)
    monthly={n:{ym:round(s/c,3) for ym,(s,c) in sorted(m.items()) if c} for n,m in acc.items()}
    out={"source":"MISO da_expost_lmp monthly avg LMP","unit":"$/MWh","nodes":NODES,
         "last_date":end.isoformat(),"acc":acc,"monthly":monthly}
    if not any(monthly.values()): print("!! 월별 0 — [Hub 노드 목록] 보고 NODES 수정.", flush=True)
    json.dump(out, open(OUT_JSON,"w",encoding="utf-8"), ensure_ascii=False)
    print(f"[OK] {OUT_JSON} fetched={fetched} months={ {n:len(m) for n,m in monthly.items()} }", flush=True)
    with open(OUT_CSV,"w",newline="",encoding="utf-8-sig") as f:
        w=csv.writer(f); w.writerow(["node","ym","lmp_da_avg_usd_mwh"])
        for n,m in monthly.items():
            for ym,v in m.items(): w.writerow([n,ym,v])
    print(f"[OK] {OUT_CSV}", flush=True)

if __name__=="__main__": main()
