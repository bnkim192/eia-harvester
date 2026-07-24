# -*- coding: utf-8 -*-
"""환율 수집기 — KRW per 외화 (ECB 참조환율, Frankfurter·키 불필요)."""
import json, csv, sys
import requests

CCYS = ["USD", "EUR", "PLN", "CNY", "CAD", "IDR"]
NON_EUR = [c for c in CCYS if c != "EUR"]
URL = "https://api.frankfurter.app/latest?from=EUR&to=KRW," + ",".join(NON_EUR)


def main():
    r = requests.get(URL, timeout=60)
    print("[HTTP]", r.status_code, r.url, flush=True)
    if r.status_code != 200:
        print("[BODY]", r.text[:500], flush=True); r.raise_for_status()
    j = r.json()
    date = j.get("date"); rates = j.get("rates", {})
    eur_krw = rates.get("KRW")
    if not eur_krw:
        print("!! KRW 환율 없음", j, flush=True); sys.exit(1)
    out = {}
    for c in CCYS:
        if c == "EUR":
            out[c] = round(eur_krw, 4)
        else:
            per_eur = rates.get(c)
            if per_eur: out[c] = round(eur_krw / per_eur, 4)
    print("[기준일]", date, "  [환율 ₩/현지]", out, flush=True)
    res = {"source": "Frankfurter (ECB reference rates)", "date": date,
           "unit": "KRW per 1 unit foreign currency", "rates": out}
    with open("fx_krw.json", "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    with open("fx_krw.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f); w.writerow(["ccy", "krw_per_unit", "date"])
        for c, v in out.items(): w.writerow([c, v, date])
    print("[OK] fx_krw.json / fx_krw.csv 저장", flush=True)


if __name__ == "__main__":
    main()
