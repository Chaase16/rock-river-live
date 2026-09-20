import hmac
import json
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from urllib.error import HTTPError
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

# Refresh the browser every 5 minutes. Individual USGS gauges may transmit less often.
st_autorefresh(interval=5 * 60 * 1000, key="rock-river-refresh")

# ---------------------------------------------------------------------------
# Shared-password login
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
# Stations
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
    "05424000": "historical East Branch station",
    "05424081": "main stem below Lake Sinissippi",
    "05425215": "joins Rock upstream of Watertown",
    "05425912": "feeds the Crawfish system",
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

# Modern USGS OGC API.
API_BASES = [
    "https://api.waterdata.usgs.gov/ogcapi/v1",
    "https://api.waterdata.usgs.gov/ogcapi/v0",
]

# ---------------------------------------------------------------------------
# USGS API helpers
# ---------------------------------------------------------------------------

def parse_time(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

def read_http_error(exc):
    try:
        body = exc.read().decode("utf-8", errors="replace")
        return body[:800]
    except Exception:
        return str(exc)

def request_items(base, collection, site_ids, *, period=None):
    """
    Match the query style shown in the official USGS dataRetrieval examples:
      monitoring_location_id=USGS-...,USGS-...
      parameter_code=00060,00065
      skipGeometry=TRUE
      time=P2D               (continuous history only)
    """
    params = {
        "f": "json",
        "lang": "en-US",
        "monitoring_location_id": ",".join(f"USGS-{s}" for s in site_ids),
        "parameter_code": f"{PARAM_FLOW},{PARAM_STAGE}",
        "skipGeometry": "TRUE",
        "limit": "50000",
    }
    if period is not None:
        # IMPORTANT: the modern USGS continuous API uses `time`, not `datetime`.
        params["time"] = period

    url = f"{base}/collections/{collection}/items?" + urlencode(
        params, safe=","
    )

    req = Request(
        url,
        headers={
            "User-Agent": "RockRiverLiveDashboard/3.0",
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )

    last_error = None
    for attempt in range(1, 4):
        try:
            with urlopen(req, timeout=35) as response:
                return json.load(response), url
        except HTTPError as exc:
            body = read_http_error(exc)
            last_error = RuntimeError(
                f"HTTP {exc.code} for {collection}: {body}"
            )
            # A 400 is a query/site-specific problem; don't hammer it three times.
            if exc.code == 400:
                raise last_error
            if attempt < 3:
                time.sleep(attempt * 2)
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(attempt * 2)

    raise RuntimeError(str(last_error))

def fetch_chunk(collection, site_ids, *, period=None):
    """
    Try v1, then v0. If a multi-site request gets a 400, recursively split the
    station list. This prevents one retired/problematic gauge from taking down
    the whole dashboard.
    """
    errors = []
    for base in API_BASES:
        try:
            return request_items(
                base, collection, site_ids, period=period
            )
        except Exception as exc:
            errors.append(f"{base}: {exc}")

    if len(site_ids) > 1:
        mid = len(site_ids) // 2
        left_ids = site_ids[:mid]
        right_ids = site_ids[mid:]

        left_payload, left_urls, left_failed = fetch_chunk(
            collection, left_ids, period=period
        )
        right_payload, right_urls, right_failed = fetch_chunk(
            collection, right_ids, period=period
        )

        return (
            {
                "type": "FeatureCollection",
                "features": (
                    left_payload.get("features", [])
                    + right_payload.get("features", [])
                ),
            },
            left_urls + right_urls,
            left_failed + right_failed,
        )

    # One station failed on both v1 and v0. Keep dashboard running and report it.
    return (
        {"type": "FeatureCollection", "features": []},
        [],
        [(site_ids[0], " | ".join(errors))],
    )

@st.cache_data(ttl=240, show_spinner=False)
def fetch_collection(collection, period=None):
    ids = list(STATIONS.keys())

    # Start with moderate chunks to keep URLs manageable and isolate bad sites.
    chunk_size = 6
    features = []
    urls = []
    failures = []

    for i in range(0, len(ids), chunk_size):
        chunk = ids[i:i + chunk_size]
        payload, used_urls, failed = fetch_chunk(
            collection, chunk, period=period
        )
        features.extend(payload.get("features", []))
        urls.extend(used_urls)
        failures.extend(failed)

    return {
        "type": "FeatureCollection",
        "features": features,
    }, urls, failures

def props_from_features(payload):
    rows = []
    for feature in payload.get("features", []):
        p = feature.get("properties", {}) or {}

        site_id = str(p.get("monitoring_location_id", ""))
        site = site_id[5:] if site_id.startswith("USGS-") else site_id
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
            "time_series_id": (
                p.get("time_series_id")
                or p.get("timeseries_id")
                or ""
            ),
            "unit": p.get("unit_of_measure"),
            "approval": p.get("approval_status"),
            "location_name": p.get("monitoring_location_name"),
        })
    return rows

def choose_latest(rows):
    selected = {}
    for row in rows:
        key = (row["site"], row["pcode"])
        old = selected.get(key)
        if old is None or row["time"] > old["time"]:
            selected[key] = row
    return selected

def choose_history_series(rows):
    by_series = defaultdict(list)

    for row in rows:
        key = (row["site"], row["pcode"], row["time_series_id"])
        by_series[key].append((row["time"], row["value"]))

    candidates = defaultdict(list)

    for (site, pcode, tsid), points in by_series.items():
        dedup = sorted({t: v for t, v in points}.items())
        if not dedup:
            continue
        candidates[(site, pcode)].append(
            (len(dedup), dedup[-1][0], tsid, dedup)
        )

    selected = {}
    for key, options in candidates.items():
        options.sort(key=lambda x: (x[0], x[1]), reverse=True)
        selected[key] = options[0][3]

    return selected

def nearest_before(points, target):
    candidates = [p for p in points if p[0] <= target]
    return candidates[-1] if candidates else None

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
    return (
        datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
    ).total_seconds() / 60

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
# Fetch current + history
# ---------------------------------------------------------------------------

latest_payload, latest_urls, latest_failures = fetch_collection(
    "latest-continuous"
)
history_payload, history_urls, history_failures = fetch_collection(
    "continuous", period="P2D"
)

latest_rows = props_from_features(latest_payload)
history_rows = props_from_features(history_payload)

latest_by_key = choose_latest(latest_rows)
history_by_key = choose_history_series(history_rows)

def make_row(site, name, group):
    stage_rec = latest_by_key.get((site, PARAM_STAGE))
    flow_rec = latest_by_key.get((site, PARAM_FLOW))

    stage_points = history_by_key.get((site, PARAM_STAGE), [])
    flow_points = history_by_key.get((site, PARAM_FLOW), [])

    stage = (
        stage_rec["value"]
        if stage_rec
        else (stage_points[-1][1] if stage_points else None)
    )
    flow = (
        flow_rec["value"]
        if flow_rec
        else (flow_points[-1][1] if flow_points else None)
    )

    stage_time = (
        stage_rec["time"]
        if stage_rec
        else (stage_points[-1][0] if stage_points else None)
    )
    flow_time = (
        flow_rec["time"]
        if flow_rec
        else (flow_points[-1][0] if flow_points else None)
    )

    newest = max(
        [t for t in (stage_time, flow_time) if t is not None],
        default=None,
    )
    ages = [
        age_minutes(t)
        for t in (stage_time, flow_time)
        if t is not None
    ]
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
        "Age min": (
            round(freshest_age, 0)
            if freshest_age is not None
            else None
        ),
        "Status": status,
        "Observation": (
            newest.astimezone().strftime("%m/%d/%Y %I:%M %p %Z")
            if newest
            else None
        ),
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
# Dashboard
# ---------------------------------------------------------------------------

st.title("🦆 Rock River Live")
st.caption(
    "Official USGS Water Data API • auto-refreshes every 5 minutes • "
    "USGS transmission timing varies by gauge"
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

st.link_button(
    "Open Lebanon USGS gauge",
    station_link(LEBANON_SITE),
)

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

st.subheader("Lebanon / County MM — last 48 hours")

leb_points = history_by_key.get((LEBANON_SITE, PARAM_STAGE), [])

if leb_points:
    chart_df = pd.DataFrame(
        leb_points,
        columns=["Time", "Stage ft"],
    ).set_index("Time")
    st.line_chart(chart_df, y="Stage ft", height=320)
else:
    st.info("No 48-hour Lebanon stage history was returned.")

all_failures = {
    site: message
    for site, message in (latest_failures + history_failures)
}

if all_failures:
    with st.expander(
        f"USGS stations with API issues ({len(all_failures)})"
    ):
        st.write(
            "The dashboard kept running. These stations failed individually "
            "and did not block the other gauges:"
        )
        for site, message in all_failures.items():
            st.write(f"**{STATIONS.get(site, site)} ({site})**")
            st.code(message[:1200])

with st.expander("About this dashboard"):
    st.write(
        "Current readings and recent history come directly from the modern "
        "USGS Water Data OGC API. The page refreshes every 5 minutes. "
        "Individual gauges may publish on different schedules, and USGS data "
        "are provisional and may be revised."
    )
    st.write(
        "Lebanon local benchmarks: 7.0 ft = decent flood / good duck water; "
        "7.5 ft = stronger flood; 9.0 ft = widespread 'lake everywhere' "
        "conditions."
    )

if st.button("Refresh USGS data now"):
    st.cache_data.clear()
    st.rerun()

if st.button("Log out"):
    st.session_state["authenticated"] = False
    st.rerun()
