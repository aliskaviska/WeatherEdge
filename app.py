
import math
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"

# Presets use the airport / observing-site area commonly associated with the city.
# Always verify the exact settlement source in the Kalshi contract rules before trading.
PRESETS = {
    "New York (Central Park area)": {"series": "KXHIGHNY", "lat": 40.7812, "lon": -73.9665, "tz": "America/New_York"},
    "Chicago (O'Hare)": {"series": "KXHIGHCHI", "lat": 41.9742, "lon": -87.9073, "tz": "America/Chicago"},
    "Miami (MIA)": {"series": "KXHIGHMIA", "lat": 25.7959, "lon": -80.2870, "tz": "America/New_York"},
    "Los Angeles (LAX)": {"series": "KXHIGHLAX", "lat": 33.9416, "lon": -118.4085, "tz": "America/Los_Angeles"},
    "Denver (DEN)": {"series": "KXHIGHDEN", "lat": 39.8561, "lon": -104.6737, "tz": "America/Denver"},
}

HEADERS = {
    "User-Agent": "KalshiWeatherEdge/0.1 (personal research dashboard)",
    "Accept": "application/json",
}

def get_json(url, params=None, timeout=20):
    r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.json()

@st.cache_data(ttl=30)
def get_kalshi_markets(series_ticker):
    all_markets, cursor = [], None
    for _ in range(10):
        params = {"series_ticker": series_ticker, "status": "open", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        data = get_json(f"{KALSHI_BASE}/markets", params=params)
        all_markets.extend(data.get("markets", []))
        cursor = data.get("cursor")
        if not cursor:
            break
    return all_markets

def money(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

def infer_market_date(m, tz_name):
    # occurrence_datetime is preferred when supplied.
    for key in ("occurrence_datetime", "expected_expiration_time", "expiration_time", "close_time"):
        raw = m.get(key)
        if raw:
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                return dt.astimezone(ZoneInfo(tz_name)).date()
            except Exception:
                pass
    # Fallback: search YYYY-MM-DD in metadata.
    blob = " ".join(str(m.get(k, "")) for k in ("title","subtitle","ticker","event_ticker"))
    match = re.search(r"(20\d{2})-(\d{2})-(\d{2})", blob)
    if match:
        return datetime.strptime(match.group(0), "%Y-%m-%d").date()
    return None

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
                "nws_name": p.get("name"),
                "nws_detail": p.get("shortForecast"),
            })
    return rows

@st.cache_data(ttl=900)
def get_gfs_ensemble_hourly(lat, lon, tz_name, forecast_days=8):
    # Open-Meteo Ensemble API exposes individual GFS ensemble members.
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
    member_keys = [k for k in hourly.keys() if k.startswith("temperature_2m")]
    if not member_keys:
        raise ValueError("No GFS ensemble temperature members were returned.")
    df = pd.DataFrame({"time": times})
    for k in member_keys:
        df[k] = pd.to_numeric(hourly[k], errors="coerce")
    df["date"] = df["time"].dt.date
    daily = df.groupby("date")[member_keys].max()
    return daily

def market_condition(m):
    floor = m.get("floor_strike")
    cap = m.get("cap_strike")
    fstrike = str(m.get("functional_strike") or "").lower()
    yes_sub = str(m.get("yes_sub_title") or "")
    subtitle = str(m.get("subtitle") or "")
    title = str(m.get("title") or "")
    text = " ".join([yes_sub, subtitle, title]).lower()

    # Normalize numeric strikes where present.
    floor = float(floor) if isinstance(floor, (int, float)) else None
    cap = float(cap) if isinstance(cap, (int, float)) else None

    # Kalshi bracket markets generally expose floor/cap strikes.
    if floor is not None and cap is not None and cap >= floor:
        return ("range", floor, cap, f"{floor:g}–{cap:g}°F")
    if floor is not None:
        if "below" in text or "less" in text or fstrike in ("less", "lt", "below"):
            return ("below", None, floor, f"below {floor:g}°F")
        return ("above", floor, None, f"{floor:g}°F or above")
    if cap is not None:
        if "above" in text or "greater" in text or fstrike in ("greater", "gt", "above"):
            return ("above", cap, None, f"{cap:g}°F or above")
        return ("below", None, cap, f"below {cap:g}°F")

    # Last-resort parser for titles.
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", " ".join([yes_sub, subtitle]))]
    if len(nums) >= 2:
        lo, hi = sorted(nums[:2])
        return ("range", lo, hi, f"{lo:g}–{hi:g}°F")
    if len(nums) == 1:
        n = nums[0]
        if any(w in text for w in ("above", "greater", "at least", "or higher")):
            return ("above", n, None, f"{n:g}°F or above")
        if any(w in text for w in ("below", "less", "or lower")):
            return ("below", None, n, f"below {n:g}°F")
    return (None, None, None, "unparsed")

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

def wilson_lower(phat, n, z=1.2816):  # ~80% one-sided confidence bound
    if phat is None or n <= 0:
        return None
    denom = 1 + z*z/n
    center = phat + z*z/(2*n)
    margin = z * math.sqrt((phat*(1-phat) + z*z/(4*n))/n)
    return max(0.0, (center - margin) / denom)

def fmt_pct(x):
    return "—" if x is None or pd.isna(x) else f"{100*x:.1f}%"

st.set_page_config(page_title="Weather Edge", page_icon="🌦️", layout="wide")
st.title("🌦️ Kalshi Weather Edge")
st.caption("Live Kalshi prices + weather-model probabilities. Research tool, not a profit guarantee.")

with st.sidebar:
    st.header("Market")
    preset = st.selectbox("Preset", list(PRESETS.keys()))
    p = PRESETS[preset]
    series = st.text_input("Kalshi series ticker", p["series"])
    lat = st.number_input("Latitude", value=float(p["lat"]), format="%.5f")
    lon = st.number_input("Longitude", value=float(p["lon"]), format="%.5f")
    tz_name = st.text_input("Timezone", p["tz"])
    min_edge = st.slider("Minimum model edge", 0, 30, 5, 1) / 100
    st.caption("For a new city, use the exact settlement station coordinates from the Kalshi rules.")

try:
    markets = get_kalshi_markets(series.strip().upper())
    if not markets:
        st.warning("No open markets were found for that series ticker.")
        st.stop()

    ens = get_gfs_ensemble_hourly(lat, lon, tz_name)
    nws_rows = get_nws_daily(lat, lon)
    nws_by_date = {r["date"]: r for r in nws_rows}

    rows = []
    for m in markets:
        d = infer_market_date(m, tz_name)
        if d is None or d not in ens.index:
            continue

        kind, lo, hi, bracket = market_condition(m)
        if kind is None:
            continue

        q, n = probability(ens.loc[d].values, kind, lo, hi)
        q_low = wilson_lower(q, n)

        yes_ask = money(m.get("yes_ask_dollars"))
        no_ask = money(m.get("no_ask_dollars"))

        # If NO ask is unavailable, infer an indicative value from YES bid where possible.
        if no_ask is None:
            yb = money(m.get("yes_bid_dollars"))
            if yb is not None:
                no_ask = max(0.0, min(1.0, 1.0 - yb))

        candidates = []
        if yes_ask and 0 < yes_ask < 1:
            ev = q - yes_ask
            cev = (q_low - yes_ask) if q_low is not None else None
            candidates.append(("YES", yes_ask, q, q_low, ev, cev))
        if no_ask and 0 < no_ask < 1:
            qn = 1 - q
            qn_low = wilson_lower(qn, n)
            ev = qn - no_ask
            cev = qn_low - no_ask
            candidates.append(("NO", no_ask, qn, qn_low, ev, cev))

        for side, ask, prob, prob_low, edge, conservative_edge in candidates:
            nws = nws_by_date.get(d, {})
            volume = money(m.get("volume_fp")) or 0
            oi = money(m.get("open_interest_fp")) or 0
            rows.append({
                "date": d,
                "market": m.get("title") or m.get("subtitle") or m.get("ticker"),
                "bracket": bracket,
                "side": side,
                "ask": ask,
                "model_prob": prob,
                "conservative_prob": prob_low,
                "edge": edge,
                "conservative_edge": conservative_edge,
                "expected_roi": edge / ask if ask else None,
                "n_members": n,
                "nws_high_f": nws.get("nws_high_f"),
                "nws_forecast": nws.get("nws_detail"),
                "volume": volume,
                "open_interest": oi,
                "ticker": m.get("ticker"),
                "rules": m.get("rules_primary"),
            })

    if not rows:
        st.warning("Markets loaded, but none could be matched to the ensemble forecast dates/strikes.")
        st.stop()

    df = pd.DataFrame(rows)
    df = df.sort_values(["conservative_edge", "edge"], ascending=False)

    best = df.iloc[0]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Best side", f"{best['side']} {best['bracket']}")
    c2.metric("Ask", f"{best['ask']:.0%}")
    c3.metric("Model probability", fmt_pct(best["model_prob"]))
    c4.metric("Model edge", f"{best['edge']*100:+.1f} pp")

    if best["conservative_edge"] >= min_edge:
        st.success(
            f"Top candidate: {best['side']} {best['bracket']} on {best['date']}. "
            f"Conservative model edge ≈ {best['conservative_edge']*100:+.1f} percentage points."
        )
    else:
        st.info(
            "No contract currently clears your conservative edge threshold. "
            "That is a perfectly valid result: sometimes the best trade is no trade."
        )

    display = df.copy()
    display["Ask"] = display["ask"].map(lambda x: f"{x:.0%}")
    display["Model P"] = display["model_prob"].map(fmt_pct)
    display["Conservative P"] = display["conservative_prob"].map(fmt_pct)
    display["Edge"] = display["edge"].map(lambda x: f"{x*100:+.1f} pp")
    display["Conservative edge"] = display["conservative_edge"].map(lambda x: f"{x*100:+.1f} pp")
    display["Expected ROI*"] = display["expected_roi"].map(lambda x: f"{x*100:+.1f}%")
    display = display.rename(columns={
        "date": "Date", "bracket": "Bracket", "side": "Side",
        "nws_high_f": "NWS high °F", "volume": "Volume", "open_interest": "Open interest",
        "ticker": "Ticker"
    })
    cols = ["Date","Bracket","Side","Ask","Model P","Conservative P","Edge","Conservative edge",
            "Expected ROI*","NWS high °F","Volume","Open interest","Ticker"]
    st.dataframe(display[cols], use_container_width=True, hide_index=True)

    st.subheader("How the ranking works")
    st.markdown(
        """
**Model probability** is the fraction of GFS ensemble members whose forecast daily maximum falls inside the contract outcome.

**Edge** = model probability − market ask price. If a YES contract costs 35¢ and the model estimates 50%, the raw model edge is +15 percentage points.

**Conservative probability** uses an 80% one-sided Wilson lower bound instead of taking the ensemble percentage at face value. The table ranks by this more cautious edge.

**Expected ROI*** is a simplified pre-fee estimate: `(model probability − ask) / ask`. It is *not* a guaranteed return and it omits Kalshi fees, slippage, model bias, settlement-station mismatch, and forecast error.
        """
    )

    st.warning(
        "Before placing a trade, open the Kalshi contract rules and verify the exact settlement station, "
        "measurement definition, rounding, and reporting source. A forecast for the wrong station can create a fake 'edge'."
    )

except requests.HTTPError as e:
    st.error(f"API request failed: {e}")
except Exception as e:
    st.exception(e)
