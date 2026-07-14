from __future__ import annotations

import re
import time
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

    # Parse start date
    df["start_datetime"] = pd.to_datetime(
        df["start_date"].fillna("").astype(str) + " " + df["start_time"].fillna("").astype(str),
        errors="coerce",
    )
    df["start_date_parsed"] = df["start_datetime"].dt.normalize()
    
    # Parse end date
    df["end_datetime"] = pd.to_datetime(
        df["end_date"].fillna("").astype(str) + " " + df["end_time"].fillna("").astype(str),
        errors="coerce",
    )
    df["end_date_parsed"] = df["end_datetime"].dt.normalize()
    
    # Fallback: If an event has no end date, assume it ends on the same day it starts
    df["end_date_parsed"] = df["end_date_parsed"].fillna(df["start_date_parsed"])

    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")

    df = df.sort_values(
        by=["start_date_parsed", "business_name", "display_address"],
        na_position="last",
    ).reset_index(drop=True)
    df["record_id"] = [f"unit-{index}" for index in range(len(df))]
    return df


def get_default_date_bounds(df: pd.DataFrame):
    valid_start_dates = df["start_date_parsed"].dropna()
    valid_end_dates = df["end_date_parsed"].dropna()
    
    if valid_start_dates.empty:
        today = pd.Timestamp.today().normalize()
        return today.date(), today.date()
        
    min_date = valid_start_dates.min().date()
    max_date = valid_end_dates.max().date()
    
    return min_date, max_date


def normalize_date_range(date_range, min_date, max_date):
    if isinstance(date_range, tuple) and len(date_range) == 2:
        return date_range
    return (min_date, max_date)


def filter_records(df: pd.DataFrame, date_range, search_text: str) -> pd.DataFrame:
    start_date, end_date = date_range
    filter_start = pd.Timestamp(start_date)
    filter_end = pd.Timestamp(end_date)

    # Overlap Logic: Event starts before/on filter end AND ends after/on filter start
    overlap_mask = (
        (df["start_date_parsed"] <= filter_end) & 
        (df["end_date_parsed"] >= filter_start)
    )
    
    filtered = df.loc[overlap_mask]

    query = (search_text or "").strip().lower()
    if query:
        filtered = filtered.loc[filtered["business_name"].fillna("").str.lower().str.contains(query)]

    return filtered.reset_index(drop=True)


def build_popup_html(row) -> str:
        name_text = escape(row.business_name or "Unknown Unit")
        schedule_text = escape(row.display_schedule or "Unknown schedule")
        address_text = escape(row.display_address or "Address unavailable")
        return f"""
        <div style="width: 220px;">
            <strong>{name_text}</strong><br/>
            <span style='font-size: 0.92em;'>{schedule_text}</span><br/>
            <span style='font-size: 0.92em;'>{address_text}</span>
        </div>
        """


def add_cluster_markers(cluster_layer, rows, selected_record_id: str | None, dashboard_map: folium.Map, is_cluster: bool):
    selected_marker = None
    for row in rows:
        is_selected = row.record_id == selected_record_id
        
        # Only show the popup on load if it's selected AND NOT part of a shared-location cluster
        show_popup = is_selected and not is_cluster
        popup = folium.Popup(
            build_popup_html(row),
            max_width=260,
            show=show_popup,
            autoPan=False,
        )
        
        marker = folium.Marker(
            location=[row.latitude, row.longitude],
            popup=popup,
            tooltip=row.business_name or "STFU Unit",
            icon=folium.Icon(color=MARKER_COLOR, icon=MARKER_ICON, prefix="fa"),
        )
        
        # Keep the selected marker directly on the map so it can always reopen
        # its popup, even when the same sidebar item is clicked repeatedly.
        if is_selected:
            marker.add_to(dashboard_map)
            selected_marker = marker
        else:
            marker.add_to(cluster_layer)

    return selected_marker


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


def add_map_resize_behavior(dashboard_map: folium.Map):
    map_var = dashboard_map.get_name()
    script = f"""
    (function() {{
        function invalidate() {{
            var map = window['{map_var}'];
            if (typeof map !== 'undefined' && map) {{
                map.invalidateSize(true);
            }} else {{
                setTimeout(invalidate, 100);
            }}
        }}

        setTimeout(invalidate, 250);
        window.addEventListener('resize', function() {{
            setTimeout(invalidate, 50);
        }});
    }})();
    """
    dashboard_map.get_root().script.add_child(Element(script))


def add_selected_marker_focus_behavior(
    dashboard_map: folium.Map,
    marker: folium.Marker | None,
    selected_row,
    selection_nonce: int | None,
):
    if marker is None or selected_row is None:
        return

    map_var = dashboard_map.get_name()
    marker_var = marker.get_name()
    lat = float(selected_row["latitude"])
    lon = float(selected_row["longitude"])
    nonce = 0 if selection_nonce is None else int(selection_nonce)
    script = f"""
    (function() {{
        var selectionNonce = {nonce};

        function focusSelectedMarker() {{
            var map = window['{map_var}'];
            var marker = window['{marker_var}'];
            if (typeof map !== 'undefined' && map && typeof marker !== 'undefined' && marker) {{
                if (marker.getPopup()) {{
                    marker.closePopup();
                }}
                map.invalidateSize(true);
                map.setView([{lat}, {lon}], {SELECTED_ZOOM}, {{animate: true}});
                setTimeout(function() {{
                    if (marker.getPopup()) {{
                        marker.openPopup();
                    }}
                }}, 150);
            }} else {{
                setTimeout(focusSelectedMarker, 100);
            }}
        }}

        focusSelectedMarker();
    }})();
    """
    dashboard_map.get_root().script.add_child(Element(script))


def build_map(df: pd.DataFrame, selected_record_id: str | None, selection_nonce: int | None = None) -> folium.Map:
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
    selected_marker = None

    for _, group in grouped_rows:
        rows = list(group.itertuples())
        is_cluster = len(rows) > 1  # Determine if this exact location has multiple units
        
        target_cluster = persistent_cluster if is_cluster else regular_cluster
        
        # Pass the is_cluster flag to your marker builder.
        maybe_selected_marker = add_cluster_markers(target_cluster, rows, selected_record_id, dashboard_map, is_cluster)
        if maybe_selected_marker is not None:
            selected_marker = maybe_selected_marker
        
        has_persistent_clusters = has_persistent_clusters or is_cluster

    if has_persistent_clusters:
        add_persistent_cluster_click_behavior(dashboard_map, persistent_cluster)

    if selected_marker is not None and selected_row is not None:
        # Reopen and recenter on every sidebar click, even when the same
        # record is clicked repeatedly.
        add_selected_marker_focus_behavior(dashboard_map, selected_marker, selected_row, selection_nonce)

    add_map_resize_behavior(dashboard_map)

    return dashboard_map


def render_map(folium_map: folium.Map, height: int | None = None):
    height_to_use = MAP_HEIGHT if height is None else height
    if st_folium is not None:
        map_key = f"stfu-map-{st.session_state.get('selection_nonce', 0)}"
        try:
            st_folium(
                folium_map,
                width=None,
                height=height_to_use,
                use_container_width=True,
                returned_objects=[],
                key=map_key,
            )
        except TypeError:
            st_folium(
                folium_map,
                width=None,
                height=height_to_use,
                use_container_width=True,
                returned_objects=[],
            )
        return

    components.html(folium_map._repr_html_(), height=height_to_use, scrolling=False)


def render_card(row):
    line_1 = row.display_schedule or "Unknown schedule"
    line_2 = f"**{row.business_name or 'Unknown Unit'}**"
    line_3 = row.display_address or "Address unavailable"

    label = "\n".join([line_1, line_2, line_3])

    def select_row():
        st.session_state["selected_record_id"] = row.record_id
        st.session_state["selection_nonce"] = time.time_ns()

    st.button(
        label,
        key=f"card-{row.record_id}",
        use_container_width=True,
        on_click=select_row,
    )

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
                padding-top: 2.75rem;
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
                min-height: 0;
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

            [data-testid="stHorizontalBlock"] > div:nth-child(2) iframe {
                border-radius: 0.5rem;
                width: 100% !important;
                height: min(88vh, 1000px) !important;
            }

            @media (max-width: 768px) {
                .block-container {
                    padding-top: 0.35rem;
                    padding-left: 0.5rem;
                    padding-right: 0.5rem;
                }

                html, body, .block-container {
                    height: auto;
                    min-height: 100dvh;
                    overflow-x: hidden;
                    overflow-y: auto;
                }

                [data-testid="stHorizontalBlock"] {
                    display: block;
                    height: auto;
                }

                [data-testid="stHorizontalBlock"] > div {
                    width: 100% !important;
                    max-width: 100% !important;
                    padding-right: 0 !important;
                    height: auto !important;
                    min-height: 0 !important;
                    overflow: visible !important;
                }

                [data-testid="stHorizontalBlock"] > div:nth-child(1) {
                    margin-bottom: 0.75rem;
                }

                [data-testid="stHorizontalBlock"] > div:nth-child(1) > div:last-child {
                    overflow: visible;
                    padding-right: 0;
                }

                [data-testid="stHorizontalBlock"] > div:nth-child(2) {
                    margin-top: 0.5rem;
                }

                [data-testid="stHorizontalBlock"] > div:nth-child(2) iframe {
                    height: 60vh !important;
                    min-height: 420px !important;
                    max-height: 75vh !important;
                }

                .stButton > button {
                    min-height: 66px;
                }
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

    # 1. Get the true absolute boundaries of your data
    min_date, max_date = get_default_date_bounds(df)

    # 2. Dynamically calculate the first day of the current month
    first_of_month = pd.Timestamp.today().replace(day=1).date()

    # 3. Safety check: Ensure default_start falls strictly between min_date and max_date
    # to satisfy Streamlit's widget validation constraints.
    if min_date <= first_of_month <= max_date:
        default_start = first_of_month
    else:
        default_start = min_date

    left_col, right_col = st.columns([1, 3], gap="medium")

    with left_col:
        with st.container(border=True):
            st.subheader("Filters")
            date_range = st.date_input(
                "Date range",
                value=(default_start, max_date),  # Defaults to first of current month
                min_value=default_start,          # Lower limit is the same as default start to ensure it's valid
                max_value=max_date,               # Upper limit reflects true latest event end date
                format="MM/DD/YYYY",
            )
            search_text = st.text_input("Search", placeholder="Search by business name")

    date_range = normalize_date_range(date_range, min_date, max_date)
    filtered_df = filter_records(df, date_range=date_range, search_text=search_text)

    valid_selected_id = st.session_state.get("selected_record_id")
    if valid_selected_id and valid_selected_id not in set(filtered_df["record_id"]):
        st.session_state["selected_record_id"] = None
        st.session_state["selection_nonce"] = 0

    with left_col:
        with st.container(border=True):
            st.subheader("Results")
            render_results(filtered_df)

    with right_col:
        dashboard_map = build_map(
            filtered_df,
            st.session_state.get("selected_record_id"),
            st.session_state.get("selection_nonce", 0),
        )
        render_map(dashboard_map)


if __name__ == "__main__":
    main()
