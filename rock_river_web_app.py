import hmac
import json
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

st.set_page_config(
    page_title="Rock River Live",
    page_icon="🦆",
    layout="wide",
)

# Browser refresh every 5 minutes. USGS gauges themselves may transmit less often.
st_autorefresh(interval=5 * 60 * 1000, key="rock-river-refresh")

# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def check_password():
    if st.session_state.get("authenticated", False):
        return True

    st.title("🦆 Rock River Live")
    st.caption("Private Rock River & tributary gauge dashboard")

    password = st.text_input("Password", type="password")
    if st.button("Log in", use_container_width=True):
        expected = st.secrets.get("APP_PASSWORD", "")
        if expected and hmac.compare_digest(password, expected):
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")

    return False

if not check_password():
    st.stop()

# ---------------------------------------------------------------------------
# Station configuration
# ---------------------------------------------------------------------------

UPPER_BRANCHES = {
    "05423100": "West Branch Rock / CTH D",
    "05423500": "South Branch Rock / Waupun",
    "05424000": "East Branch Rock / Mayville",
}

MAINSTEM = {
    "05424057": "Horicon",
    "05424081": "Hustisford / Tweedy St",
    "05424157": "Lebanon / County MM",
    "05425500": "Watertown",
    "05426031": "Jefferson",
    "05427085": "Fort Atkinson",
    "05427235": "Lake Koshkonong",
}

TRIBUTARIES = {
    "05425215": "Oconomowoc River / CTH BB",
    "05425912": "Beaverdam River / Beaver Dam",
    "05426000": "Crawfish River / Milford",
    "05426250": "Bark River / Rome",
    "05429700": "Yahara River / Stoughton",
    "05431486": "Turtle Creek / Clinton",
}

NOTES = {
    "05423100": "upper Rock headwater branch",
    "05423500": "upper Rock headwater branch",
    "05424000": "upper Rock headwater branch",
    "05424081": "Rock main stem below Lake Sinissippi",
    "05425215": "joins Rock upstream of Watertown",
    "05425912": "feeds Crawfish River",
    "05426000": "joins Rock upstream of Jefferson",
    "05426250": "major Middle Rock tributary",
    "05429700": "joins Rock below Lake Koshkonong",
    "05431486": "joins lower Rock near Beloit",
}

STATIONS = {**UPPER_BRANCHES, **MAINSTEM, **TRIBUTARIES}

PARAM_STAGE = "00065"
PARAM_FLOW = "00060"
LEBANON_SITE = "05424157"
STALE_MINUTES = 90

# Modern USGS Water Data APIs.
# USGS is replacing the legacy waterservices.usgs.gov NWIS /iv service.
API_BASES = [
    "https://api.waterdata.usgs.gov/ogcapi/v1",
    "https://api.waterdata.usgs.gov/ogcapi/v0",
]

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def parse_time(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

def api_request(collection, params, retries=3):
    """
    Request modern USGS OGC API data.
    Tries v1 first and v0 as a fallback, with retries for transient 5xx errors.
    """
    errors = []

    for base in API_BASES:
        url = f"{base}/collections/{collection}/items?" + urlencode(params)

        for attempt in range(1, retries + 1):
            try:
                req = Request(
                    url,
                    headers={
                        "User-Agent": "RockRiverLiveDashboard/2.0",
                        "Accept": "application/geo+json, application/json",
                        "Cache-Control": "no-cache",
                        "Pragma": "no-cache",
                    },
                )
                with urlopen(req, timeout=30) as response:
                    return json.load(response), url
            except Exception as exc:
                errors.append(f"{base} attempt {attempt}: {exc}")
                if attempt < retries:
                    time.sleep(attempt * 2)

    raise RuntimeError(" | ".join(errors[-4:]))

def usgs_site_ids():
    return ",".join(f"USGS-{site}" for site in STATIONS)

@st.cache_data(ttl=240, show_spinner=False)
def fetch_latest():
    params = {
        "f": "json",
        "monitoring_location_id": usgs_site_ids(),
        "parameter_code": f"{PARAM_STAGE},{PARAM_FLOW}",
        "skipGeometry": "true",
        "limit": "1000",
    }
    return api_request("latest-continuous", params)

@st.cache_data(ttl=240, show_spinner=False)
def fetch_history():
    params = {
        "f": "json",
        "monitoring_location_id": usgs_site_ids(),
        "parameter_code": f"{PARAM_STAGE},{PARAM_FLOW}",
        "datetime": "P2D",
        "skipGeometry": "true",
        "limit": "10000",
    }
    return api_request("continuous", params)

def props_from_features(payload):
    rows = []
    for feature in payload.get("features", []):
        p = feature.get("properties", {}) or {}
        site_id = p.get("monitoring_location_id", "")
        if site_id.startswith("USGS-"):
            site = site_id[5:]
        else:
            site = site_id

        if site not in STATIONS:
            continue

        pcode = str(p.get("parameter_code", ""))
        if pcode not in (PARAM_STAGE, PARAM_FLOW):
            continue

        try:
            value = float(p.get("value"))
        except (TypeError, ValueError):
            continue

        dt = parse_time(p.get("time"))
        if dt is None:
            continue

        rows.append({
            "site": site,
            "pcode": pcode,
            "value": value,
            "time": dt,
            "time_series_id": p.get("time_series_id") or p.get("timeseries_id") or "",
            "unit": p.get("unit_of_measure"),
            "approval": p.get("approval_status"),
            "location_name": p.get("monitoring_location_name"),
        })
    return rows

def choose_history_series(history_rows):
    """
    A station can occasionally expose multiple continuous time series for the
    same parameter. Choose one series per site/parameter, preferring the series
    with the most points and then the newest observation.
    """
    by_series = defaultdict(list)

    for row in history_rows:
        key = (row["site"], row["pcode"], row["time_series_id"])
        by_series[key].append((row["time"], row["value"]))

    candidates = defaultdict(list)
    for (site, pcode, tsid), points in by_series.items():
        points = sorted({t: v for t, v in points}.items())
        latest_time = points[-1][0] if points else datetime.min.replace(tzinfo=timezone.utc)
        candidates[(site, pcode)].append((len(points), latest_time, tsid, points))

    selected = {}
    for key, options in candidates.items():
        options.sort(key=lambda x: (x[0], x[1]), reverse=True)
        selected[key] = options[0][3]

    return selected

def choose_latest(latest_rows):
    """
    Pick the newest official latest-continuous record for each site/parameter.
    """
    selected = {}
    for row in latest_rows:
        key = (row["site"], row["pcode"])
        old = selected.get(key)
        if old is None or row["time"] > old["time"]:
            selected[key] = row
    return selected

def nearest_before(points, target):
    choices = [p for p in points if p[0] <= target]
    return choices[-1] if choices else None

def stage_change(points, hours):
    if not points:
        return None

    newest_time, newest_value = points[-1]
    old = nearest_before(points, newest_time - timedelta(hours=hours))
    if old is None:
        return None

    return newest_value - old[1]

def age_minutes(dt):
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 60

def fmt_delta(v):
    if v is None or pd.isna(v):
        return "—"
    if v > 0.01:
        return f"↑ {v:+.2f}"
    if v < -0.01:
        return f"↓ {v:+.2f}"
    return f"→ {v:+.2f}"

def station_link(site):
    return (
        f"https://waterdata.usgs.gov/monitoring-location/USGS-{site}/"
        "#dataTypeId=continuous-00065-0&period=P7D"
    )

# ---------------------------------------------------------------------------
# Fetch data
# ---------------------------------------------------------------------------

try:
    latest_payload, latest_source_url = fetch_latest()
    history_payload, history_source_url = fetch_history()
except Exception as exc:
    st.error("Could not reach the modern USGS Water Data API.")
    st.code(str(exc))
    if st.button("Retry now"):
        st.cache_data.clear()
        st.rerun()
    st.stop()

latest_rows = props_from_features(latest_payload)
history_rows = props_from_features(history_payload)

latest_by_key = choose_latest(latest_rows)
history_by_key = choose_history_series(history_rows)

def make_row(site, name, group):
    stage_rec = latest_by_key.get((site, PARAM_STAGE))
    flow_rec = latest_by_key.get((site, PARAM_FLOW))

    stage_points = history_by_key.get((site, PARAM_STAGE), [])
    flow_points = history_by_key.get((site, PARAM_FLOW), [])

    stage = stage_rec["value"] if stage_rec else (stage_points[-1][1] if stage_points else None)
    flow = flow_rec["value"] if flow_rec else (flow_points[-1][1] if flow_points else None)

    stage_time = stage_rec["time"] if stage_rec else (stage_points[-1][0] if stage_points else None)
    flow_time = flow_rec["time"] if flow_rec else (flow_points[-1][0] if flow_points else None)

    newest = max([t for t in (stage_time, flow_time) if t is not None], default=None)
    ages = [age_minutes(t) for t in (stage_time, flow_time) if t is not None]
    freshest_age = min(ages) if ages else None

    if freshest_age is None:
        status = "NO DATA"
    elif freshest_age <= STALE_MINUTES:
        status = "LIVE"
    else:
        status = "STALE"

    return {
        "Group": group,
        "Station": name,
        "USGS": site,
        "Stage ft": stage,
        "Flow CFS": flow,
        "1h ft": stage_change(stage_points, 1),
        "6h ft": stage_change(stage_points, 6),
        "24h ft": stage_change(stage_points, 24),
        "Age min": round(freshest_age, 0) if freshest_age is not None else None,
        "Status": status,
        "Observation": newest.astimezone().strftime("%m/%d/%Y %I:%M %p %Z") if newest else None,
        "Note": NOTES.get(site, ""),
    }

rows = []
for site, name in UPPER_BRANCHES.items():
    rows.append(make_row(site, name, "Upper Rock"))
for site, name in MAINSTEM.items():
    rows.append(make_row(site, name, "Main stem"))
for site, name in TRIBUTARIES.items():
    rows.append(make_row(site, name, "Tributary"))

df = pd.DataFrame(rows)

# ---------------------------------------------------------------------------
# Header and Lebanon summary
# ---------------------------------------------------------------------------

st.title("🦆 Rock River Live")
st.caption(
    "Official USGS modern Water Data API • page refreshes every 5 minutes • "
    "each gauge may transmit on a different schedule"
)

leb = df[df["USGS"] == LEBANON_SITE].iloc[0]
stage = leb["Stage ft"]
flow = leb["Flow CFS"]

c1, c2, c3, c4 = st.columns(4)

c1.metric(
    "Lebanon stage",
    "—" if pd.isna(stage) else f"{stage:.2f} ft",
    None if pd.isna(leb["1h ft"]) else fmt_delta(leb["1h ft"]),
)

c2.metric(
    "Lebanon flow",
    "—" if pd.isna(flow) else f"{flow:,.0f} CFS",
)

if pd.isna(stage):
    level_text = "No current stage"
elif stage >= 9.0:
    level_text = "9.0+ • LAKE EVERYWHERE"
elif stage >= 7.5:
    level_text = "7.5+ • STRONG FLOOD"
elif stage >= 7.0:
    level_text = "7.0+ • GOOD DUCK WATER"
else:
    level_text = "Below 7.0 ft"

c3.metric("Duck-water status", level_text)
c4.metric(
    "USGS data age",
    "—" if pd.isna(leb["Age min"]) else f"{int(leb['Age min'])} min",
)

st.link_button("Open Lebanon USGS gauge", station_link(LEBANON_SITE))

# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

def show_group(title, group_name):
    st.subheader(title)
    view = df[df["Group"] == group_name].copy()

    view["Stage ft"] = view["Stage ft"].map(
        lambda x: None if pd.isna(x) else round(x, 2)
    )
    view["Flow CFS"] = view["Flow CFS"].map(
        lambda x: None if pd.isna(x) else round(x)
    )
    view["1h"] = view["1h ft"].map(fmt_delta)
    view["6h"] = view["6h ft"].map(fmt_delta)
    view["24h"] = view["24h ft"].map(fmt_delta)
    view["Gauge"] = view["USGS"].map(station_link)

    st.dataframe(
        view[
            [
                "Station",
                "Stage ft",
                "Flow CFS",
                "1h",
                "6h",
                "24h",
                "Age min",
                "Status",
                "Observation",
                "Note",
                "Gauge",
            ]
        ],
        use_container_width=True,
        hide_index=True,
        column_config={
            "Gauge": st.column_config.LinkColumn("USGS"),
            "Stage ft": st.column_config.NumberColumn(format="%.2f"),
            "Flow CFS": st.column_config.NumberColumn(format="%d"),
        },
    )

show_group("Upper Rock branches / headwaters", "Upper Rock")
show_group("Rock River main stem / lake", "Main stem")
show_group("Major tributaries", "Tributary")

# ---------------------------------------------------------------------------
# Lebanon trend chart
# ---------------------------------------------------------------------------

st.subheader("Lebanon / County MM — last 48 hours")

leb_points = history_by_key.get((LEBANON_SITE, PARAM_STAGE), [])
if leb_points:
    chart_df = pd.DataFrame(leb_points, columns=["Time", "Stage ft"]).set_index("Time")
    st.line_chart(chart_df, y="Stage ft", height=320)
else:
    st.info("No 48-hour Lebanon stage history was returned.")

# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

with st.expander("About this dashboard"):
    st.write(
        "Data come directly from the modern official USGS Water Data APIs. "
        "USGS continuous data are often collected at 15-minute intervals, "
        "but telemetry and publication timing vary by gauge. USGS data are "
        "provisional and may be revised."
    )
    st.write(
        "Lebanon local benchmarks used here: 7.0 ft = decent flood / good "
        "duck water, 7.5 ft = stronger flood, 9.0 ft = widespread "
        "'lake everywhere' conditions."
    )
    st.link_button("USGS latest-continuous API", latest_source_url)

if st.button("Refresh USGS data now"):
    st.cache_data.clear()
    st.rerun()

if st.button("Log out"):
    st.session_state["authenticated"] = False
    st.rerun()
