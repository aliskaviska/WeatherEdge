
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
    """
    Parse the exact YES outcome wording first, so the forecast logic and the
    label shown to the user refer to the same contract.
    """
    exact = str(m.get("yes_sub_title") or m.get("subtitle") or "").strip()
    text = exact.lower().replace("º", "°")

    # 83° or above / 83 or above / 83°+
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*°?\s*(?:or\s+above|\+|and\s+above)", text)
    if match:
        n = float(match.group(1))
        return "above", n, None, exact or f"{n:g}°F or above"

    # 77° or below
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*°?\s*(?:or\s+below|and\s+below)", text)
    if match:
        n = float(match.group(1))
        return "below_equal", None, n, exact or f"{n:g}°F or below"

    # 78° to 79° / 78-79°
    match = re.search(
        r"(-?\d+(?:\.\d+)?)\s*°?\s*(?:to|-|–|—)\s*(-?\d+(?:\.\d+)?)\s*°?",
        text,
    )
    if match:
        lo, hi = sorted((float(match.group(1)), float(match.group(2))))
        return "range", lo, hi, exact or f"{lo:g}–{hi:g}°F"

    # Fallback to API strikes only when exact wording cannot be parsed.
    floor = to_float(m.get("floor_strike"))
    cap = to_float(m.get("cap_strike"))
    strike = str(m.get("functional_strike") or "").lower()

    if floor is not None and cap is not None and cap >= floor:
        return "range", floor, cap, exact or f"{floor:g}–{cap:g}°F"
    if strike in ("greater", "above", "gt") and floor is not None:
        return "above", floor, None, exact or f"{floor:g}°F or above"
    if strike in ("less", "below", "lt") and cap is not None:
        return "below", None, cap, exact or f"below {cap:g}°F"
    if floor is not None:
        return "above", floor, None, exact or f"{floor:g}°F or above"
    if cap is not None:
        return "below", None, cap, exact or f"below {cap:g}°F"
    return None, None, None, exact or "unparsed"


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
    elif kind == "below_equal":
        hits = (s <= hi).sum()
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


def point_forecast_supports_yes(temp, kind, lo, hi):
    if temp is None or pd.isna(temp):
        return None
    t = float(temp)
    if kind == "range":
        return lo <= t <= hi
    if kind == "above":
        return t >= lo
    if kind == "below":
        return t < hi
    if kind == "below_equal":
        return t <= hi
    return None

def side_supported_by_point(temp, side, kind, lo, hi):
    yes_support = point_forecast_supports_yes(temp, kind, lo, hi)
    if yes_support is None:
        return None
    return yes_support if side == "YES" else (not yes_support)

def agreement_label(nws_support, median_support):
    if nws_support is True and median_support is True:
        return "✅ NWS + ensemble agree"
    if nws_support is False and median_support is False:
        return "❌ Both forecasts oppose"
    return "⚠️ Forecasts conflict"

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

            nrow = nws.get(d, {})
            nws_high = nrow.get("nws_high_f")
            nws_support = side_supported_by_point(nws_high, side, kind, lo, hi)
            median_support = side_supported_by_point(ensemble_median, side, kind, lo, hi)
            forecasts_agree = (nws_support is True and median_support is True)

            # Strict candidate rule: both NWS and ensemble median must support
            # the same side, the ensemble must be meaningfully confident, and
            # the conservative estimate must still exceed the live ask.
            qualifies = (
                forecasts_agree
                and p >= 0.65
                and p_low is not None
                and p_low >= 0.55
                and conservative_edge is not None
                and conservative_edge >= 0.05
            )

            suspicious = (
                conservative_edge is not None and conservative_edge >= 0.30
            )

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
                "nws_high_f": nws_high,
                "nws_forecast": nrow.get("nws_detail"),
                "ensemble_median_f": ensemble_median,
                "ensemble_low_f": ensemble_low,
                "ensemble_high_f": ensemble_high,
                "nws_support": nws_support,
                "median_support": median_support,
                "forecasts_agree": forecasts_agree,
                "qualifies": qualifies,
                "agreement": agreement_label(nws_support, median_support),
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
st.caption(
    "Find weather contracts where the NWS forecast and the ensemble model point to the same outcome, "
    "then compare that agreement with the live Kalshi price."
)

with st.sidebar:
    st.header("Scanner")
    scan_mode = st.radio("Scan", ["All preset cities", "One city"], index=0)
    selected_city = None
    if scan_mode == "One city":
        selected_city = st.selectbox("City", list(PRESETS.keys()))
    top_n = st.slider("Top candidates", 3, 10, 5, 1)
    min_gap = st.slider("Minimum conservative model/market gap", 0, 30, 5, 1) / 100

cities = [selected_city] if scan_mode == "One city" else list(PRESETS.keys())
all_rows, errors = [], []

with st.spinner("Comparing live Kalshi contracts with NWS + ensemble forecasts…"):
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
    st.warning("No matching open markets were found.")
    st.stop()

df = pd.DataFrame(all_rows)
df = df[df["conservative_edge"].notna()].copy()

# Candidates must pass the forecast-agreement gate first.
qualified = df[
    (df["forecasts_agree"] == True)
    & (df["model_prob"] >= 0.65)
    & (df["conservative_prob"] >= 0.55)
    & (df["conservative_edge"] >= min_gap)
].copy()

qualified = qualified.sort_values(
    ["conservative_edge", "model_prob", "volume"],
    ascending=[False, False, False],
).head(top_n)

st.subheader("🏆 Forecast-aligned candidates")

if qualified.empty:
    st.info(
        "No contracts currently pass the strict filter. That means WeatherEdge does not see a case where "
        "NWS and the ensemble agree strongly enough AND the live price leaves enough model/market gap."
    )
else:
    st.success(
        "These are the contracts where the NWS point forecast and ensemble median support the SAME side. "
        "They are candidates for review, not guaranteed winning bets."
    )

for rank, (_, r) in enumerate(qualified.iterrows(), start=1):
    with st.container(border=True):
        st.markdown(f"### #{rank} · {r['city']} · {r['date_label']}")
        st.markdown(f"## **{r['side']} on: {r['market_subtitle']}**")
        st.write(f"**Kalshi event:** {r['event_title']}")
        st.success(r["agreement"])

        st.markdown("#### 🌤️ Why the forecasts support this side")
        f1, f2, f3 = st.columns(3)
        nws_txt = "—" if pd.isna(r["nws_high_f"]) else f"{int(r['nws_high_f'])}°F"
        med_txt = "—" if pd.isna(r["ensemble_median_f"]) else f"{r['ensemble_median_f']:.1f}°F"
        range_txt = (
            "—" if pd.isna(r["ensemble_low_f"]) or pd.isna(r["ensemble_high_f"])
            else f"{r['ensemble_low_f']:.1f}–{r['ensemble_high_f']:.1f}°F"
        )
        f1.metric("NWS high", nws_txt)
        f2.metric("Ensemble median", med_txt)
        f3.metric("80% ensemble range", range_txt)
        if r["nws_forecast"]:
            st.write(f"**NWS conditions:** {r['nws_forecast']}")

        if r["side"] == "YES":
            st.write(
                f"Both forecasts support **YES** on the exact Kalshi outcome **“{r['market_subtitle']}”**."
            )
        else:
            st.write(
                f"Both forecasts support **NO** on the exact Kalshi outcome **“{r['market_subtitle']}”**."
            )

        st.markdown("#### 💵 Forecast estimate vs. live Kalshi price")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Current ask", f"{r['ask']*100:.0f}¢")
        c2.metric("Approx. price-implied*", f"{r['ask']*100:.0f}%")
        c3.metric("Ensemble estimate", fmt_pct(r["model_prob"]))
        c4.metric("Conservative estimate", fmt_pct(r["conservative_prob"]))
        st.write(
            f"**Conservative model/market gap:** **{r['conservative_edge']*100:+.1f} percentage points**."
        )
        st.caption(
            "*The ask is a trading price, not a guaranteed or perfectly calibrated probability. "
            "Fees, spread, liquidity and stale quotes can matter."
        )

        if r["suspicious"]:
            st.warning(
                "The gap is unusually large. Verify the live Kalshi page, date, station, side and settlement rules "
                "before treating this as a real opportunity."
            )

        st.write(f"**Settlement source:** {r['settlement_source']}")
        st.write(f"**WeatherEdge location check:** {r['station_hint']}")
        st.code(
            f"Market ticker: {r['market_ticker']}\nEvent ticker: {r['event_ticker']}",
            language=None,
        )
        st.link_button(
            f"🎯 OPEN EXACT {r['city'].upper()} EVENT ON KALSHI ↗",
            r["kalshi_event_url"],
            use_container_width=True,
        )
        st.caption(
            f"On Kalshi, find the exact row **“{r['market_subtitle']}”** and choose **{r['side']}**. "
            f"Confirm ticker `{r['market_ticker']}`."
        )

st.subheader("🔎 Why other contracts were rejected")
rejected = df[~df.index.isin(qualified.index)].copy()
if not rejected.empty:
    rejected["Forecast agreement"] = rejected["agreement"]
    rejected["Ask"] = rejected["ask"].map(lambda x: f"{x*100:.0f}¢")
    rejected["Model"] = rejected["model_prob"].map(fmt_pct)
    rejected["Conservative"] = rejected["conservative_prob"].map(fmt_pct)
    rejected["Gap"] = rejected["conservative_edge"].map(lambda x: f"{x*100:+.1f} pp")
    rejected["NWS high"] = rejected["nws_high_f"].map(
        lambda x: "—" if pd.isna(x) else f"{int(x)}°F"
    )
    rejected["Ensemble median"] = rejected["ensemble_median_f"].map(
        lambda x: "—" if pd.isna(x) else f"{x:.1f}°F"
    )
    st.dataframe(
        rejected[
            [
                "city", "date_label", "side", "market_subtitle",
                "Forecast agreement", "NWS high", "Ensemble median",
                "Ask", "Model", "Conservative", "Gap",
            ]
        ].rename(
            columns={
                "city": "City",
                "date_label": "Date",
                "side": "Side",
                "market_subtitle": "Exact Kalshi outcome",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )

st.subheader("What counts as a candidate now?")
st.markdown(
    """
WeatherEdge v5 only puts a contract in the **top candidates** section when:

1. The **NWS forecast high supports the side**.
2. The **ensemble median supports the same side**.
3. At least **65% of ensemble members** support that side.
4. The conservative ensemble estimate is at least **55%**.
5. The conservative estimate still exceeds the live ask by your chosen minimum gap.
6. The exact contract wording shown to you is taken from Kalshi's own market label, so the app no longer silently changes “83° or above” into a different threshold.

A contract that fails the agreement check is shown only in the rejected table, not as a recommendation.
"""
)

st.info(
    "This is still a screening model, not proof that a trade will win. Kalshi weather markets settle from the "
    "specified NWS Daily Climate Report, so settlement-station accuracy matters. The next serious upgrade would "
    "be historical backtesting and station-specific calibration."
)
