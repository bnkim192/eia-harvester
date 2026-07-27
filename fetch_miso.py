# -*- coding: utf-8 -*-
import os, sys, json, csv, io, time
from datetime import date, timedelta
import requests

NODES = ["MICHIGAN.HUB", "INDIANA.HUB"]
BASE  = "https://docs.misoenergy.org/marketreports/{ymd}_da_expost_lmp.csv"
OUT_JSON = "miso_lmp_monthly.json"
OUT_CSV  = "miso_lmp_monthly.csv"
BACKFILL_START = os.environ.get("MISO_START", "2024-01-01")


def load_state():
    if os.path.exists(OUT_JSON):
        try:
            j = json.load(open(OUT_JSON, encoding="utf-8"))
            return j.get("acc", {}), j.get("last_date")
        except Exception:
            pass
    return {}, None


def parse_day(text):
    out = {}
    rows = list(csv.reader(io.StringIO(text)))
    hi, he_cols, node_j, type_j = None, [], 0, None
    for i, r in enumerate(rows):
        up = [c.strip().upper() for c in r]
        if any(c in ("NODE", "CPNODE", "NAME") for c in up) and any(c.replace(" ", "").startswith("HE") for c in up):
            hi = i
            he_cols = [j for j, c in enumerate(up) if c.replace(" ", "").startswith("HE")]
            node_j  = next((j for j, c in enumerate(up) if c in ("NODE", "CPNODE", "NAME")), 0)
            type_j  = next((j for j, c in enumerate(up) if c == "TYPE"), None)
            break
    if hi is None:
        return out, "header not found"
    for r in rows[hi + 1:]:
        if not r or len(r) <= (max(he_cols) if he_cols else 0):
            continue
        node = r[node_j].strip()
        typ = (r[type_j].strip().upper() if (type_j is not None and type_j < len(r)) else "LMP")
        if node in NODES and typ == "LMP":
            vals = []
            for j in he_cols:
                try:
                    vals.append(float(r[j]))
                except Exception:
                    pass
            if vals:
                out.setdefault(node, []).extend(vals)
    return out, None


def main():
    acc, last = load_state()
    start = (date.fromisoformat(last) + timedelta(days=1)) if last else date.fromisoformat(BACKFILL_START)
    end = date.today() - timedelta(days=1)
    print(f"[RANGE] {start} ~ {end}  (last_date={last})", flush=True)

    d, fetched, first = start, 0, True
    while d <= end:
        ymd = d.strftime("%Y%m%d")
        try:
            r = requests.get(BASE.format(ymd=ymd), timeout=60)
            if r.status_code == 200:
                day, err = parse_day(r.text)
                if first:
                    print(f"[HTTP] 200 {ymd} nodesFound={list(day.keys())} err={err}", flush=True)
                    hubs = set()
                    for line in r.text.splitlines()[:8000]:
                        if "HUB" in line.upper():
                            hubs.add(line.split(",")[0].strip())
                    print("[HUBS candidates]", sorted(hubs)[:25], flush=True)
                    first = False
                ym = d.strftime("%Y-%m")
                for node, vals in day.items():
                    a = acc.setdefault(node, {}).setdefault(ym, [0.0, 0])
                    a[0] += sum(vals); a[1] += len(vals)
                fetched += 1
            elif r.status_code != 404:
                if first:
                    print(f"[HTTP] {r.status_code} {ymd} -- {r.text[:200]}", flush=True)
        except Exception as e:
            print(f"[ERR] {ymd} {e}", flush=True)
        d += timedelta(days=1)
        if fetched and fetched % 25 == 0:
            time.sleep(0.4)

    monthly = {n: {ym: round(s / c, 3) for ym, (s, c) in sorted(m.items()) if c} for n, m in acc.items()}
    out = {
        "source": "MISO da_expost_lmp (Day-Ahead Ex-Post LMP) monthly avg",
        "unit": "$/MWh", "nodes": NODES,
        "last_date": end.isoformat(), "acc": acc, "monthly": monthly,
    }
    if not any(monthly.values()):
        print("!! monthly empty -- check node name (MICHIGAN.HUB) or file format. see [HUBS candidates].", flush=True)
    json.dump(out, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"[OK] {OUT_JSON}  fetched_days={fetched}", flush=True)
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f); w.writerow(["node", "ym", "lmp_da_avg_usd_mwh"])
        for n, m in monthly.items():
            for ym, v in m.items():
                w.writerow([n, ym, v])
    print(f"[OK] {OUT_CSV}", flush=True)


if __name__ == "__main__":
    main()
