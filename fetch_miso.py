name: EIA Harvester
on:
  schedule:
    - cron: "0 9 * * *"
  workflow_dispatch:
permissions:
  contents: write
jobs:
  collect:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install requests
      - name: EIA monthly retail
        env:
          EIA_API_KEY: ${{ secrets.EIA_API_KEY }}
        run: python fetch_eia.py
      - name: EIA hourly RTO
        env:
          EIA_API_KEY: ${{ secrets.EIA_API_KEY }}
        run: python fetch_eia_hourly.py
      - name: FX rates (ECB, no key)
        run: python fetch_fx.py
      - name: PJM DA LMP via API (파일/키 없으면 skip)
        env:
          PJM_API_KEY: ${{ secrets.PJM_API_KEY }}
        run: python fetch_pjm.py || echo "fetch_pjm skip"
      - name: PJM DA LMP via CSV (파일/폴더 없으면 skip)
        run: python aggregate_pjm_csv.py || echo "aggregate skip"
      - name: MISO DA LMP (keyless)
        run: python fetch_miso.py
      - name: upload artifact
        uses: actions/upload-artifact@v4
        with:
          name: eia-result
          if-no-files-found: ignore
          path: |
            eia_industrial_prices_monthly.json
            eia_hourly_rto.json
            fx_krw.json
            pjm_lmp_monthly.json
            miso_lmp_monthly.json
      - name: commit back
        run: |
          git config user.name "eia-harvester-bot"
          git config user.email "eia-harvester-bot@users.noreply.github.com"
          git add -A
          git commit -m "chore: auto collect" || echo "no change"
          git push || echo "push skip"
