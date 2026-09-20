import hmac
import json
import time
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

# Refresh the browser every 5 minutes.
st_autorefresh(interval=5 * 60 * 1000, key="rock-river-refresh")

# -----------------------------
# Shared-password login
# -----------------------------
def check_password():
    if st.session_state.get("authenticated", False):
        return True

    st.title("🦆 Rock River Live")
    st.caption("Private dashboard")

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

# -----------------------------
# Station configuration
# -----------------------------
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
    "05424081": "main stem below Lake Sinissippi",
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
API_URL = "https://waterservices.usgs.gov/nwis/iv/"
STALE_MINUTES = 90

# -----------------------------
# USGS data
# -----------------------------
def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

@st.cache_data(ttl=120)
def fetch_usgs():
    params = {
        "format": "json",
        "sites": ",".join(STATIONS),
        "parameterCd": f"{PARAM_FLOW},{PARAM_STAGE}",
        "siteStatus": "all",
        "period": "P2D",
    }
    url = API_URL + "?" + urlencode(params)
    req = Request(
        url,
        headers={
            "User-Agent": "RockRiverWebDashboard/1.0",
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    with urlopen(req, timeout=30) as response:
        return json.load(response), url

def unpack(data):
    out = {
        site: {"stage": [], "flow": []}
        for site in STATIONS
    }

    for series in data.get("value", {}).get("timeSeries", []):
        source = series.get("sourceInfo", {})
        site_codes = source.get("siteCode", [])
        if not site_codes:
            continue
        site = site_codes[0].get("value")
        if site not in out:
            continue

        variable_codes = series.get("variable", {}).get("variableCode", [])
        if not variable_codes:
            continue

        pcode = variable_codes[0].get("value")
        key = "stage" if pcode == PARAM_STAGE else "flow" if pcode == PARAM_FLOW else None
        if key is None:
            continue

        for block in series.get("values", []):
            for obs in block.get("value", []):
                try:
                    dt = parse_time(obs["dateTime"])
                    value = float(obs["value"])
                except Exception:
                    continue
                out[site][key].append((dt, value))

    for rec in out.values():
        for key in ("stage", "flow"):
            dedup = {}
            for dt, value in rec[key]:
                dedup[dt] = value
            rec[key] = sorted(dedup.items())

    return out

def latest(points):
    return points[-1] if points else (None, None)

def nearest_before(points, target):
    candidates = [p for p in points if p[0] <= target]
    return candidates[-1] if candidates else None

def stage_change(points, hours):
    if len(points) < 2:
        return None
    newest_time, newest_value = points[-1]
    old = nearest_before(points, newest_time - timedelta(hours=hours))
    if not old:
        return None
    return newest_value - old[1]

def age_minutes(dt):
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 60

def row_for(site, name, group, records):
    rec = records[site]
    stage_time, stage = latest(rec["stage"])
    flow_time, flow = latest(rec["flow"])
    newest = max([t for t in (stage_time, flow_time) if t is not None], default=None)
    freshest_age = min(
        [a for a in (age_minutes(stage_time), age_minutes(flow_time)) if a is not None],
        default=None,
    )
    status = "LIVE" if freshest_age is not None and freshest_age <= STALE_MINUTES else "STALE" if freshest_age is not None else "NO DATA"

    return {
        "Group": group,
        "Station": name,
        "USGS": site,
        "Stage ft": stage,
        "Flow CFS": flow,
        "1h ft": stage_change(rec["stage"], 1),
        "6h ft": stage_change(rec["stage"], 6),
        "24h ft": stage_change(rec["stage"], 24),
        "Age min": round(freshest_age, 0) if freshest_age is not None else None,
        "Status": status,
        "Observation": newest.astimezone().strftime("%m/%d/%Y %I:%M %p %Z") if newest else None,
        "Note": NOTES.get(site, ""),
    }

def fmt_delta(v):
    if v is None or pd.isna(v):
        return "—"
    if v > 0.01:
        return f"↑ {v:+.2f}"
    if v < -0.01:
        return f"↓ {v:+.2f}"
    return f"→ {v:+.2f}"

def station_link(site):
    return f"https://waterdata.usgs.gov/monitoring-location/USGS-{site}/#dataTypeId=continuous-00065-0&period=P7D"

try:
    raw, source_url = fetch_usgs()
    records = unpack(raw)
except Exception as exc:
    st.error(f"Could not reach USGS: {exc}")
    st.stop()

rows = []
for site, name in UPPER_BRANCHES.items():
    rows.append(row_for(site, name, "Upper Rock", records))
for site, name in MAINSTEM.items():
    rows.append(row_for(site, name, "Main stem", records))
for site, name in TRIBUTARIES.items():
    rows.append(row_for(site, name, "Tributary", records))

df = pd.DataFrame(rows)

# -----------------------------
# Header / Lebanon status
# -----------------------------
st.title("🦆 Rock River Live")
st.caption("Official USGS instantaneous data • auto-refreshes every 5 minutes")

leb = df[df["USGS"] == LEBANON_SITE].iloc[0]
stage = leb["Stage ft"]
flow = leb["Flow CFS"]

c1, c2, c3, c4 = st.columns(4)

c1.metric(
    "Lebanon stage",
    "—" if pd.isna(stage) else f"{stage:.2f} ft",
    fmt_delta(leb["1h ft"]) if not pd.isna(leb["1h ft"]) else None,
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
c4.metric("USGS data age", "—" if pd.isna(leb["Age min"]) else f"{int(leb['Age min'])} min")

st.link_button("Open Lebanon USGS gauge", station_link(LEBANON_SITE))

# -----------------------------
# Display tables
# -----------------------------
def show_group(title, group_name):
    st.subheader(title)
    view = df[df["Group"] == group_name].copy()
    view["Stage ft"] = view["Stage ft"].map(lambda x: None if pd.isna(x) else round(x, 2))
    view["Flow CFS"] = view["Flow CFS"].map(lambda x: None if pd.isna(x) else round(x))
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

# -----------------------------
# Lebanon trend chart
# -----------------------------
st.subheader("Lebanon / County MM — last 48 hours")

leb_points = records[LEBANON_SITE]["stage"]
if leb_points:
    chart_df = pd.DataFrame(leb_points, columns=["Time", "Stage ft"]).set_index("Time")
    st.line_chart(chart_df, y="Stage ft", height=300)

# -----------------------------
# Footer / controls
# -----------------------------
with st.expander("About this dashboard"):
    st.write(
        "Data come directly from the official USGS Instantaneous Values service. "
        "USGS data are provisional and may be revised. The dashboard refreshes "
        "every 5 minutes; each station may transmit on a different schedule."
    )
    st.write(
        "Lebanon thresholds are local hunting/flood benchmarks used for this dashboard: "
        "7.0 ft = decent flood / good duck water, 7.5 ft = stronger flood, "
        "9.0 ft = widespread 'lake everywhere' conditions."
    )
    st.link_button("Official USGS Instantaneous Values service", source_url)

if st.button("Log out"):
    st.session_state["authenticated"] = False
    st.rerun()
