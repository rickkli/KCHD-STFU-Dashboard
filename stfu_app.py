from __future__ import annotations

import re
from html import escape
from pathlib import Path

import folium
from branca.element import Element
from folium.plugins import MarkerCluster
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

try:
    from streamlit_folium import st_folium
except ImportError:
    st_folium = None


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATASET_PATH = BASE_DIR / "stfu_calendar_prepared.csv"
DEFAULT_MAP_CENTER = (42.9634, -85.6681)
DEFAULT_ZOOM = 11
SELECTED_ZOOM = 15
MAP_HEIGHT = 1000
MARKER_COLOR = "blue"
MARKER_ICON = "cutlery"
TRAILING_COUNTRY_PATTERN = re.compile(r",?\s*United States\s*$", re.IGNORECASE)
STATE_NAME_PATTERN = re.compile(r"\bMichigan\b", re.IGNORECASE)

def clean_text(value) -> str | None:
    if value is None or pd.isna(value):
        return None

    text = str(value).strip()
    if not text:
        return None

    return re.sub(r"\s+", " ", text)


def clean_display_address(value) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    text = TRAILING_COUNTRY_PATTERN.sub("", text)
    text = STATE_NAME_PATTERN.sub("MI", text)
    return clean_text(text)


def load_dataset(dataset_path: Path) -> pd.DataFrame:
    df = pd.read_csv(dataset_path)
    required_columns = {
        "business_name",
        "start_date",
        "end_date",
        "start_time",
        "end_time",
        "display_schedule",
        "display_address",
        "geocode_address",
        "latitude",
        "longitude",
        "geocode_status",
    }
    missing_columns = required_columns.difference(df.columns)
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise ValueError(f"Prepared calendar dataset is missing required columns: {missing}")

    df = df.copy()
    df["business_name"] = df["business_name"].map(clean_text)
    df["start_date"] = df["start_date"].map(clean_text)
    df["end_date"] = df["end_date"].map(clean_text)
    df["start_time"] = df["start_time"].map(clean_text)
    df["end_time"] = df["end_time"].map(clean_text)
    df["display_schedule"] = df["display_schedule"].map(clean_text)
    df["display_address"] = df["display_address"].map(clean_display_address).fillna("Address unavailable")
    df["geocode_address"] = df["geocode_address"].map(clean_text)
    df["geocode_status"] = df["geocode_status"].map(clean_text).fillna("not_geocoded")

    df["start_datetime"] = pd.to_datetime(
        df["start_date"].fillna("").astype(str) + " " + df["start_time"].fillna("").astype(str),
        errors="coerce",
    )
    df["start_date_parsed"] = df["start_datetime"].dt.normalize()
    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")

    df = df.sort_values(
        by=["start_date_parsed", "business_name", "display_address"],
        na_position="last",
    ).reset_index(drop=True)
    df["record_id"] = [f"unit-{index}" for index in range(len(df))]
    return df


def get_default_date_bounds(df: pd.DataFrame):
    valid_dates = df["start_date_parsed"].dropna()
    if valid_dates.empty:
        today = pd.Timestamp.today().normalize()
        return today.date(), today.date()
    return valid_dates.min().date(), valid_dates.max().date()


def normalize_date_range(date_range, min_date, max_date):
    if isinstance(date_range, tuple) and len(date_range) == 2:
        return date_range
    return (min_date, max_date)


def filter_records(df: pd.DataFrame, date_range, search_text: str) -> pd.DataFrame:
    start_date, end_date = date_range
    filtered = df.loc[
        df["start_date_parsed"].between(pd.Timestamp(start_date), pd.Timestamp(end_date), inclusive="both")
    ]

    query = (search_text or "").strip().lower()
    if query:
        filtered = filtered.loc[filtered["business_name"].fillna("").str.lower().str.contains(query)]

    return filtered.reset_index(drop=True)


def build_popup_html(row) -> str:
        name_text = escape(row.business_name or "Unknown Unit")
        schedule_text = escape(row.display_schedule or "Unknown schedule")
        address_text = escape(row.display_address or "Address unavailable")
        return f"""
        <div style="width: 250px;">
            <strong>{name_text}</strong><br/>
            <span style='font-size: 0.92em;'>{schedule_text}</span><br/>
            <span style='font-size: 0.92em;'>{address_text}</span>
        </div>
        """


def add_cluster_markers(cluster_layer, rows, selected_record_id: str | None):
    for row in rows:
        popup = folium.Popup(build_popup_html(row), max_width=300, show=row.record_id == selected_record_id)
        folium.Marker(
            location=[row.latitude, row.longitude],
            popup=popup,
            tooltip=row.business_name or "STFU Unit",
            icon=folium.Icon(color=MARKER_COLOR, icon=MARKER_ICON, prefix="fa"),
        ).add_to(cluster_layer)


def add_persistent_cluster_click_behavior(dashboard_map: folium.Map, cluster_layer: MarkerCluster):
    cluster_var = cluster_layer.get_name()
    script = f"""
    (function() {{
        function attach() {{
            var persistentCluster = window['{cluster_var}'];
            if (typeof persistentCluster !== 'undefined' && persistentCluster) {{
                persistentCluster.on('clusterclick', function(event) {{
                    if (event.layer && typeof event.layer.spiderfy === 'function') {{
                        event.layer.spiderfy();
                    }}
                }});
            }} else {{
                setTimeout(attach, 100);
            }}
        }}
        attach();
    }})();
    """
    dashboard_map.get_root().script.add_child(Element(script))


def build_map(df: pd.DataFrame, selected_record_id: str | None) -> folium.Map:
    geocoded_df = df.dropna(subset=["latitude", "longitude"])
    selected_row = None

    if selected_record_id:
        match = geocoded_df.loc[geocoded_df["record_id"] == selected_record_id]
        if not match.empty:
            selected_row = match.iloc[0]

    if selected_row is not None:
        center_lat = float(selected_row["latitude"])
        center_lon = float(selected_row["longitude"])
        zoom_start = SELECTED_ZOOM
    else:
        center_lat, center_lon = DEFAULT_MAP_CENTER
        zoom_start = DEFAULT_ZOOM

    dashboard_map = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=zoom_start,
        control_scale=True,
        tiles="OpenStreetMap",
    )

    regular_cluster = MarkerCluster(
        options={
            "disableClusteringAtZoom": SELECTED_ZOOM,
            "spiderfyOnMaxZoom": True,
        }
    ).add_to(dashboard_map)

    persistent_cluster = MarkerCluster(
        options={
            "zoomToBoundsOnClick": False,
            "spiderfyOnMaxZoom": True,
            "showCoverageOnHover": False,
        }
    ).add_to(dashboard_map)

    grouped_rows = geocoded_df.groupby(["latitude", "longitude"], sort=False)
    has_persistent_clusters = False

    for _, group in grouped_rows:
        rows = list(group.itertuples())
        target_cluster = persistent_cluster if len(rows) > 1 else regular_cluster
        add_cluster_markers(target_cluster, rows, selected_record_id)
        has_persistent_clusters = has_persistent_clusters or len(rows) > 1

    if has_persistent_clusters:
        add_persistent_cluster_click_behavior(dashboard_map, persistent_cluster)

    return dashboard_map


def render_map(folium_map: folium.Map, height: int | None = None):
    height_to_use = MAP_HEIGHT if height is None else height
    if st_folium is not None:
        st_folium(folium_map, width=None, height=height_to_use, returned_objects=[])
        return

    components.html(folium_map._repr_html_(), height=height_to_use, scrolling=False)


def render_card(row):
    line_1 = row.display_schedule or "Unknown schedule"
    line_2 = f"**{row.business_name or 'Unknown Unit'}**"
    line_3 = row.display_address or "Address unavailable"

    label = "\n".join([line_1, line_2, line_3])

    if st.button(label, key=f"card-{row.record_id}", use_container_width=True):
        st.session_state["selected_record_id"] = row.record_id

    if pd.isna(row.latitude) or pd.isna(row.longitude):
        st.caption("Map coordinate unavailable")


def render_results(df: pd.DataFrame):
    if df.empty:
        st.info("No matching units.")
        return

    try:
        results_container = st.container(height=650, border=False)
    except TypeError:
        results_container = st.container()

    with results_container:
        count = len(df)
        st.markdown(f"**Showing {count} matching STFU unit{'s' if count != 1 else ''}**")
        for row in df.itertuples():
            with st.container(border=True):
                render_card(row)


def inject_minimal_styles():
    st.markdown(
        """
        <style>
                    .block-container {
                        padding-top: 3.25rem;
                        padding-left: 0.75rem;
                        padding-right: 0.75rem;
                        max-width: none;
                    }

                    html, body, .block-container {
                        height: 100vh;
                        overflow: hidden;
                    }

                    [data-testid="stHorizontalBlock"] {
                        display: flex;
                        align-items: stretch;
                        height: calc(100vh - 56px);
                    }

                    [data-testid="stHorizontalBlock"] > div:nth-child(1) {
                        display: flex;
                        flex-direction: column;
                        overflow: hidden;
                        height: 100%;
                        padding-right: 0.5rem;
                    }

                    [data-testid="stHorizontalBlock"] > div:nth-child(1) > div:first-child {
                        flex: none;
                    }

                    [data-testid="stHorizontalBlock"] > div:nth-child(1) > div:last-child {
                        flex: 1 1 auto;
                        min-height: 0;
                        overflow: auto;
                        padding-right: 0.25rem;
                    }

                    [data-testid="stHorizontalBlock"] > div:nth-child(2) {
                        display: flex;
                        flex-direction: column;
                        overflow: hidden;
                        height: 100%;
                    }

                    .stButton > button {
                        background-color: var(--secondary-background-color);
                        border-radius: 10px;
                        padding: 0.5rem 0.75rem;
                        margin-bottom: 0;
                        box-shadow: 0 6px 16px rgba(19,24,41,0.04);
                        border: 1px solid rgba(0,0,0,0.06);
                        font-family: inherit;
                        min-height: 88px;
                        text-align: left;
                        white-space: pre-line;
                        line-height: 1.35;
                        transition: box-shadow 120ms ease, transform 120ms ease;
                    }

                    .stButton > button:hover {
                        box-shadow: 0 10px 30px rgba(19,24,41,0.08);
                        transform: translateY(-2px);
                        border-color: rgba(0,0,0,0.1);
                    }

                    .stButton > button > div {
                        width: 100%;
                        justify-content: flex-start;
                    }

                    iframe {
                        border-radius: 0.5rem;
                        height: calc(100vh - 0px) !important;
                        width: 100% !important;
                    }
        </style>
        """,
        unsafe_allow_html=True,
    )


def main():
    st.set_page_config(page_title="STFU Dashboard", layout="wide")
    inject_minimal_styles()

    dataset_path = DEFAULT_DATASET_PATH
    if not dataset_path.exists():
        st.error(
            "Prepared calendar dataset not found. Run "
            "`python prepare_stfu_calendar.py` from the repo root first."
        )
        st.stop()

    try:
        df = load_dataset(dataset_path)
    except Exception as exc:
        st.error(f"Failed to load prepared calendar dataset: {exc}")
        st.stop()

    min_date, max_date = get_default_date_bounds(df)

    left_col, right_col = st.columns([1, 3], gap="medium")

    with left_col:
        with st.container(border=True):
            st.subheader("Filters")
            date_range = st.date_input(
                "Date range",
                value=(min_date, max_date),
                min_value=min_date,
                max_value=max_date,
                format="MM/DD/YYYY",
            )
            search_text = st.text_input("Search", placeholder="Search by business name")

    date_range = normalize_date_range(date_range, min_date, max_date)
    filtered_df = filter_records(df, date_range=date_range, search_text=search_text)

    valid_selected_id = st.session_state.get("selected_record_id")
    if valid_selected_id and valid_selected_id not in set(filtered_df["record_id"]):
        st.session_state["selected_record_id"] = None

    with left_col:
        with st.container(border=True):
            st.subheader("Results")
            render_results(filtered_df)

    with right_col:
        dashboard_map = build_map(filtered_df, st.session_state.get("selected_record_id"))
        render_map(dashboard_map)


if __name__ == "__main__":
    main()
