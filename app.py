
import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"

# These are convenient starting coordinates for the observing-site area.
# Always verify the settlement source shown in the app before trading.
PRESETS = {
    "New York": {
        "series": "KXHIGHNY",
        "lat": 40.7812, "lon": -73.9665,
        "tz": "America/New_York",
        "station": "Central Park / NYC settlement area",
    },
    "Chicago": {
        "series": "KXHIGHCHI",
        "lat": 41.9742, "lon": -87.9073,
        "tz": "America/Chicago",
        "station": "Chicago O'Hare area",
    },
    "Miami": {
        "series": "KXHIGHMIA",
        "lat": 25.7959, "lon": -80.2870,
        "tz": "America/New_York",
        "station": "Miami International Airport area",
    },
    "Los Angeles": {
        "series": "KXHIGHLAX",
        "lat": 33.9416, "lon": -118.4085,
        "tz": "America/Los_Angeles",
        "station": "Los Angeles International Airport area",
    },
    "Denver": {
        "series": "KXHIGHDEN",
        "lat": 39.8561, "lon": -104.6737,
        "tz": "America/Denver",
        "station": "Denver International Airport area",
    },
}


# Known Kalshi public-page slugs for the temperature series.
KALSHI_SERIES_SLUGS = {
    "KXHIGHNY": "highest-temperature-in-nyc",
    "KXHIGHCHI": "highest-temperature-in-chicago",
    "KXHIGHMIA": "highest-temperature-in-miami",
    "KXHIGHLAX": "highest-temperature-in-los-angeles",
    "KXHIGHDEN": "highest-temperature-in-denver",
}

def kalshi_event_url(series_ticker, event_ticker):
    """Build the public Kalshi event page URL for a dated weather event."""
    slug = KALSHI_SERIES_SLUGS.get(series_ticker)
    if slug and event_ticker:
        return (
            f"https://kalshi.com/markets/"
            f"{series_ticker.lower()}/{slug}/{event_ticker.lower()}"
        )
    return "https://kalshi.com/"

HEADERS = {
    "User-Agent": "WeatherEdge/2.0 (personal research dashboard)",
    "Accept": "application/json",
}

def get_json(url, params=None, timeout=25):
    r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.json()

def to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

@st.cache_data(ttl=30)
def get_kalshi_markets(series_ticker):
    out, cursor = [], None
    for _ in range(10):
        params = {
            "series_ticker": series_ticker,
            "status": "open",
            "limit": 1000,
            "mve_filter": "exclude",
        }
        if cursor:
            params["cursor"] = cursor
        data = get_json(f"{KALSHI_BASE}/markets", params=params)
        out.extend(data.get("markets", []))
        cursor = data.get("cursor")
        if not cursor:
            break
    return out

@st.cache_data(ttl=3600)
def get_series_info(series_ticker):
    data = get_json(f"{KALSHI_BASE}/series/{series_ticker}")
    return data.get("series", {})

@st.cache_data(ttl=300)
def get_event(event_ticker):
    if not event_ticker:
        return {}
    data = get_json(f"{KALSHI_BASE}/events/{event_ticker}")
    return data.get("event", {})

@st.cache_data(ttl=900)
def get_nws_daily(lat, lon):
    point = get_json(f"https://api.weather.gov/points/{lat},{lon}")
    forecast_url = point["properties"]["forecast"]
    forecast = get_json(forecast_url)
    rows = []
    for p in forecast["properties"]["periods"]:
        if p.get("isDaytime"):
            rows.append({
                "date": datetime.fromisoformat(p["startTime"]).date(),
                "nws_high_f": p.get("temperature"),
                "nws_detail": p.get("shortForecast"),
            })
    return rows

@st.cache_data(ttl=900)
def get_gfs_ensemble_daily_highs(lat, lon, tz_name, forecast_days=8):
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m",
        "models": "gfs_seamless",
        "temperature_unit": "fahrenheit",
        "timezone": tz_name,
        "forecast_days": forecast_days,
    }
    data = get_json("https://ensemble-api.open-meteo.com/v1/ensemble", params=params)
    hourly = data.get("hourly", {})
    times = pd.to_datetime(hourly.get("time", []))
    member_keys = [k for k in hourly if k.startswith("temperature_2m")]
    if not member_keys:
        raise ValueError("No GFS ensemble members returned.")
    df = pd.DataFrame({"time": times})
    for k in member_keys:
        df[k] = pd.to_numeric(hourly[k], errors="coerce")
    df["date"] = df["time"].dt.date
    return df.groupby("date")[member_keys].max()

def infer_market_date(m, tz_name):
    for key in ("occurrence_datetime", "expected_expiration_time", "expiration_time", "close_time"):
        raw = m.get(key)
        if raw:
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                return dt.astimezone(ZoneInfo(tz_name)).date()
            except Exception:
                pass
    blob = " ".join(str(m.get(k, "")) for k in ("title", "subtitle", "ticker", "event_ticker"))
    match = re.search(r"(20\d{2})-(\d{2})-(\d{2})", blob)
    if match:
        return datetime.strptime(match.group(0), "%Y-%m-%d").date()
    return None

def market_condition(m):
    floor = to_float(m.get("floor_strike"))
    cap = to_float(m.get("cap_strike"))
    strike = str(m.get("functional_strike") or "").lower()
    yes_sub = str(m.get("yes_sub_title") or "")
    subtitle = str(m.get("subtitle") or "")
    title = str(m.get("title") or "")
    text = " ".join([yes_sub, subtitle, title]).lower()

    if floor is not None and cap is not None and cap >= floor:
        return "range", floor, cap, f"{floor:g}–{cap:g}°F"

    if strike in ("less", "below", "lt") or ("below" in text or "less than" in text):
        boundary = cap if cap is not None else floor
        if boundary is not None:
            return "below", None, boundary, f"below {boundary:g}°F"

    if strike in ("greater", "above", "gt") or any(w in text for w in ("above", "greater than", "at least", "or higher")):
        boundary = floor if floor is not None else cap
        if boundary is not None:
            return "above", boundary, None, f"{boundary:g}°F or above"

    if floor is not None:
        return "above", floor, None, f"{floor:g}°F or above"
    if cap is not None:
        return "below", None, cap, f"below {cap:g}°F"

    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", " ".join([yes_sub, subtitle]))]
    if len(nums) >= 2:
        lo, hi = sorted(nums[:2])
        return "range", lo, hi, f"{lo:g}–{hi:g}°F"
    if len(nums) == 1:
        n = nums[0]
        if any(w in text for w in ("above", "higher", "at least")):
            return "above", n, None, f"{n:g}°F or above"
        if any(w in text for w in ("below", "lower", "less")):
            return "below", None, n, f"below {n:g}°F"
    return None, None, None, "unparsed"

def probability(values, kind, lo, hi):
    s = pd.Series(values).dropna().astype(float)
    if s.empty:
        return None, 0
    if kind == "range":
        hits = ((s >= lo) & (s <= hi)).sum()
    elif kind == "above":
        hits = (s >= lo).sum()
    elif kind == "below":
        hits = (s < hi).sum()
    else:
        return None, len(s)
    return hits / len(s), len(s)

def wilson_lower(phat, n, z=1.2816):
    # About an 80% one-sided lower bound.
    if phat is None or n <= 0:
        return None
    denom = 1 + z*z/n
    center = phat + z*z/(2*n)
    margin = z * math.sqrt((phat*(1-phat) + z*z/(4*n))/n)
    return max(0.0, (center - margin) / denom)

def pretty_date(d):
    try:
        return d.strftime("%a, %b %-d")
    except Exception:
        return d.strftime("%a, %b %d").replace(" 0", " ")

def classify(edge, suspicious):
    if suspicious:
        return "⚠️ CHECK"
    if edge >= 0.15:
        return "🟢 STRONG"
    if edge >= 0.08:
        return "🟢 GOOD"
    if edge >= 0.04:
        return "🟡 MAYBE"
    return "⚪ PASS"

def source_names(series_info, event_info):
    sources = event_info.get("settlement_sources") or series_info.get("settlement_sources") or []
    names = [s.get("name") for s in sources if s.get("name")]
    return ", ".join(names) if names else "Check Kalshi contract rules"

def build_city_rows(city, cfg):
    markets = get_kalshi_markets(cfg["series"])
    if not markets:
        return []

    series_info = get_series_info(cfg["series"])
    ens = get_gfs_ensemble_daily_highs(cfg["lat"], cfg["lon"], cfg["tz"])
    nws = {r["date"]: r for r in get_nws_daily(cfg["lat"], cfg["lon"])}

    event_cache = {}
    rows = []

    for m in markets:
        d = infer_market_date(m, cfg["tz"])
        if d is None or d not in ens.index:
            continue

        kind, lo, hi, bracket = market_condition(m)
        if kind is None:
            continue

        daily_members = pd.Series(ens.loc[d].values).dropna().astype(float)
        p_yes, n = probability(daily_members.values, kind, lo, hi)
        if p_yes is None:
            continue
        ensemble_median = float(daily_members.median()) if not daily_members.empty else None
        ensemble_low = float(daily_members.quantile(0.10)) if not daily_members.empty else None
        ensemble_high = float(daily_members.quantile(0.90)) if not daily_members.empty else None

        event_ticker = m.get("event_ticker")
        if event_ticker not in event_cache:
            try:
                event_cache[event_ticker] = get_event(event_ticker)
            except Exception:
                event_cache[event_ticker] = {}
        event_info = event_cache[event_ticker]

        title = event_info.get("title") or m.get("title") or series_info.get("title") or f"{city} high temperature"
        subtitle = m.get("subtitle") or m.get("yes_sub_title") or bracket
        settlement = source_names(series_info, event_info)
        contract_url = series_info.get("contract_url")

        side_data = [
            ("YES", to_float(m.get("yes_ask_dollars")), p_yes),
            ("NO", to_float(m.get("no_ask_dollars")), 1 - p_yes),
        ]

        for side, ask, p in side_data:
            # Never synthesize the opposite ask. Use the actual live side price only.
            if ask is None or not (0 < ask < 1):
                continue
            p_low = wilson_lower(p, n)
            edge = p - ask
            conservative_edge = p_low - ask if p_low is not None else None
            suspicious = (
                conservative_edge is not None
                and conservative_edge >= 0.30
            )

            nrow = nws.get(d, {})
            rows.append({
                "city": city,
                "station_hint": cfg["station"],
                "date": d,
                "date_label": pretty_date(d),
                "series_ticker": cfg["series"],
                "event_ticker": event_ticker,
                "market_ticker": m.get("ticker"),
                "event_title": title,
                "market_subtitle": subtitle,
                "bracket": bracket,
                "side": side,
                "ask": ask,
                "model_prob": p,
                "conservative_prob": p_low,
                "edge": edge,
                "conservative_edge": conservative_edge,
                "expected_roi": edge / ask,
                "n_members": n,
                "nws_high_f": nrow.get("nws_high_f"),
                "nws_forecast": nrow.get("nws_detail"),
                "ensemble_median_f": ensemble_median,
                "ensemble_low_f": ensemble_low,
                "ensemble_high_f": ensemble_high,
                "settlement_source": settlement,
                "contract_url": contract_url,
                "kalshi_event_url": kalshi_event_url(cfg["series"], event_ticker),
                "volume": to_float(m.get("volume_fp")) or 0,
                "open_interest": to_float(m.get("open_interest_fp")) or 0,
                "suspicious": suspicious,
            })
    return rows

def fmt_pct(x):
    return "—" if x is None or pd.isna(x) else f"{100*x:.1f}%"

st.set_page_config(page_title="WeatherEdge", page_icon="🌦️", layout="wide")
st.title("🌦️ WeatherEdge")
st.caption("Forecast vs. Kalshi price, in plain English. Prices are not true probabilities, and model estimates are not guarantees.")

with st.sidebar:
    st.header("Scanner")
    scan_mode = st.radio("Scan", ["All preset cities", "One city"], index=0)
    selected_city = None
    if scan_mode == "One city":
        selected_city = st.selectbox("City", list(PRESETS.keys()))
    top_n = st.slider("How many top options?", 3, 10, 5, 1)
    min_edge = st.slider("Minimum conservative edge", 0, 30, 5, 1) / 100
    include_suspicious = st.checkbox("Show unusually large edges", value=True)
    st.caption("Huge edges can be real, but they can also signal a station/contract mismatch. Treat them as 'verify first'.")

cities = [selected_city] if scan_mode == "One city" else list(PRESETS.keys())

all_rows = []
errors = []

with st.spinner("Scanning live Kalshi weather markets and forecasts…"):
    for city in cities:
        try:
            all_rows.extend(build_city_rows(city, PRESETS[city]))
        except Exception as e:
            errors.append(f"{city}: {e}")

if errors:
    with st.expander("Some cities could not be scanned"):
        for e in errors:
            st.write(e)

if not all_rows:
    st.warning("I couldn't match any open preset weather markets to forecast dates right now.")
    st.stop()

df = pd.DataFrame(all_rows)
df = df[df["conservative_edge"].notna()].copy()
if not include_suspicious:
    df = df[~df["suspicious"]]
df = df.sort_values(["conservative_edge", "edge", "volume"], ascending=[False, False, False])

qualified = df[df["conservative_edge"] >= min_edge].head(top_n)
if qualified.empty:
    qualified = df.head(top_n)

st.subheader("🏆 Top opportunities right now")
st.caption("These are rankings from the model, not guaranteed winners. Verify the exact settlement source before placing a bet.")

for rank, (_, r) in enumerate(qualified.iterrows(), start=1):
    label = classify(r["conservative_edge"], r["suspicious"])
    with st.container(border=True):
        st.markdown(f"### #{rank} {label} · {r['city']} · {r['date_label']}")
        st.markdown(f"## **Kalshi contract: {r['bracket']} · {r['side']}**")
        st.write(f"**Event:** {r['event_title']}")
        st.write(f"**Exact market label:** {r['market_subtitle']}")

        st.markdown("#### 🌤️ Weather forecast")
        f1, f2, f3 = st.columns(3)
        nws_high_text = "—" if pd.isna(r["nws_high_f"]) else f"{int(r['nws_high_f'])}°F"
        ens_med_text = "—" if pd.isna(r["ensemble_median_f"]) else f"{r['ensemble_median_f']:.1f}°F"
        ens_range_text = "—" if pd.isna(r["ensemble_low_f"]) or pd.isna(r["ensemble_high_f"]) else f"{r['ensemble_low_f']:.1f}–{r['ensemble_high_f']:.1f}°F"
        f1.metric("NWS forecast high", nws_high_text)
        f2.metric("Ensemble median", ens_med_text)
        f3.metric("80% ensemble range", ens_range_text)
        if r["nws_forecast"]:
            st.write(f"**NWS conditions:** {r['nws_forecast']}")

        st.markdown("#### 💵 Kalshi price vs. model")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Current ask", f"{r['ask']*100:.0f}¢")
        c2.metric("Approx. market-implied*", f"{r['ask']*100:.0f}%")
        c3.metric("Ensemble estimate", fmt_pct(r["model_prob"]))
        c4.metric("Conservative estimate", fmt_pct(r["conservative_prob"]))

        st.write(
            f"**Model/market gap:** {fmt_pct(r['conservative_prob'])} conservative estimate "
            f"minus {r['ask']*100:.0f}% current ask = **{r['conservative_edge']*100:+.1f} percentage points**."
        )
        st.caption(
            "*The ask is a trading price, not a perfect probability. Fees, spread, liquidity, stale quotes and order-book conditions matter."
        )

        st.markdown("#### 🧭 What the bet means")
        if r["side"] == "YES":
            st.write(f"Buying **YES** means you are betting the official high for **{r['city']} on {r['date_label']}** lands in **{r['bracket']}**.")
        else:
            st.write(f"Buying **NO** means you are betting the official high for **{r['city']} on {r['date_label']}** does **not** land in **{r['bracket']}**.")

        st.write(f"**Settlement source reported by Kalshi:** {r['settlement_source']}")
        st.write(f"**WeatherEdge location cross-check:** {r['station_hint']}")

        if r["ask"] > 0:
            one_profit = 1 - r["ask"]
            st.markdown("#### 🧮 Payout example")
            st.write(
                f"At **{r['ask']*100:.0f}¢**, one contract costs about **${r['ask']:.2f}** and settles at **$1.00** if it wins, "
                f"for about **${one_profit:.2f} gross profit before fees**."
            )

        if r["suspicious"]:
            st.error(
                "⚠️ VERY LARGE DISAGREEMENT. Do not read this as easy money. Verify the exact date, station, market side, "
                "settlement rule and live executable price. A huge gap can also come from a parsing or data mismatch."
            )
        elif r["conservative_edge"] >= 0.15:
            st.warning("Large model/market disagreement. Verify the contract details and live price before considering a trade.")

        st.link_button(
            f"🎯 OPEN EXACT {r['city'].upper()} EVENT ON KALSHI ↗",
            r["kalshi_event_url"],
            use_container_width=True,
        )
        st.caption(f"On Kalshi, find **{r['bracket']}** and choose **{r['side']}**. Market ticker: `{r['market_ticker']}`")
        if r["contract_url"]:
            st.link_button("Read Kalshi contract/rules source ↗", r["contract_url"], use_container_width=True)

st.subheader("📋 All scanned options")
table = df.copy()
table["Rank"] = range(1, len(table) + 1)
table["Bet"] = table["side"] + " " + table["bracket"]
table["Price"] = table["ask"].map(lambda x: f"{x*100:.0f}¢")
table["Market implied*"] = table["ask"].map(lambda x: f"{x*100:.0f}%")
table["Ensemble"] = table["model_prob"].map(fmt_pct)
table["Conservative"] = table["conservative_prob"].map(fmt_pct)
table["Difference"] = table["conservative_edge"].map(lambda x: f"{x*100:+.1f} pp")
table["NWS high"] = table["nws_high_f"].map(lambda x: "—" if pd.isna(x) else f"{int(x)}°F")
table["Ensemble median"] = table["ensemble_median_f"].map(lambda x: "—" if pd.isna(x) else f"{x:.1f}°F")
table["Flag"] = table.apply(lambda r: classify(r["conservative_edge"], r["suspicious"]), axis=1)

display_cols = [
    "Rank", "Flag", "city", "date_label", "Bet", "Price",
    "Market implied*", "NWS high", "Ensemble median",
    "Ensemble", "Conservative", "Difference",
    "market_ticker", "kalshi_event_url"
]
st.dataframe(
    table[display_cols].rename(columns={
        "city": "City",
        "date_label": "Date",
        "market_ticker": "Market ticker",
        "event_ticker": "Event ticker",
        "kalshi_event_url": "Direct Kalshi link",
    }),
    use_container_width=True,
    hide_index=True,
)

st.subheader("How to read WeatherEdge")
st.markdown(
    """
**1. Read the forecast first.** Compare the NWS forecast high with the ensemble median and the ensemble range.

**2. Then read the Kalshi price.** A 40¢ ask is commonly discussed as roughly a 40% market-implied chance, but it is still a market price, not an objective probability.

**3. Compare them.** WeatherEdge shows the conservative model estimate beside the ask. A positive gap means this weather model is more optimistic about that side than the current market price suggests.

**4. Never treat 100% as certainty.** It only means every ensemble member in this model run landed on that side. Weather, observations and models can still be wrong.

**5. Huge gaps are verification signals.** A 1¢ contract with a 95% model estimate is not automatically a jackpot. Check the city, date, station, bracket, YES/NO side, settlement source and live price manually.

**6. Use the direct Kalshi button.** Confirm the ticker shown in WeatherEdge before placing anything.
"""
)
st.info(
    "WeatherEdge is a research/comparison tool. It currently uses NWS forecasts and a GFS ensemble. "
    "It does not yet model Kalshi fees, order-book depth, historical calibration, station-specific model bias, "
    "or every available weather model."
)
