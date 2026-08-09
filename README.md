
# Kalshi Weather Edge

A small Streamlit research dashboard that:

- pulls open Kalshi markets from Kalshi's public REST API;
- pulls the official NWS point forecast as a reference;
- pulls individual GFS ensemble-member forecasts from Open-Meteo;
- converts ensemble daily-high forecasts into an estimated probability for each Kalshi temperature bracket;
- compares that probability with the live YES/NO ask;
- ranks candidates by a conservative probability edge.

## Run it

Requires Python 3.10+.

```bash
python -m pip install -r requirements.txt
streamlit run app.py
```

Streamlit will open a local browser tab, usually at http://localhost:8501.

## What “best” means

The app does **not** predict guaranteed winners. It ranks contracts where the forecast model probability appears higher than the price-implied probability.

Raw edge:

`model probability - ask price`

The default ranking uses an 80% one-sided Wilson lower confidence bound on the ensemble hit rate, then subtracts the ask. This deliberately penalizes small ensemble samples.

The displayed “Expected ROI” is a simplified, pre-fee number:

`(model probability - ask) / ask`

It does not include Kalshi fees, bid/ask slippage, forecast-model bias, settlement quirks, or station mismatch.

## Important setup note

Weather contracts settle according to the source and station stated in each Kalshi contract. The included city presets are convenient starting points, not a legal definition of settlement. **Verify the exact settlement station in the contract rules and adjust the latitude/longitude if needed before relying on the score.**

## APIs

- Kalshi public REST market data: https://external-api.kalshi.com/trade-api/v2
- National Weather Service API: https://api.weather.gov
- Open-Meteo Ensemble API: https://ensemble-api.open-meteo.com/v1/ensemble

No Kalshi API key is required for the public REST market-data endpoint used by this version.

## Next useful upgrades

- Automatically read settlement-station metadata from each Kalshi series/rules.
- Add ECMWF/ICON ensembles and blend models.
- Learn city/station-specific bias from historical forecasts versus final climate reports.
- Model Kalshi fees and actual order-book depth.
- Add push alerts only when conservative edge exceeds a threshold.
- Backtest the scoring rule before allowing any automated execution.
