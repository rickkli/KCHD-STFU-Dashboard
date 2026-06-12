from __future__ import annotations

import argparse
from datetime import date, datetime, time
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ICS_URL = (
    "https://outlook.office365.com/owa/calendar/"
    "0acc388e319f4921973ea7188a685c0a@kentcountymi.gov/"
    "111de85b10ad470997092aa0aaea4b4417987207977242946761/calendar.ics"
)
DEFAULT_OUTPUT_PATH = BASE_DIR / "stfu_calendar_prepared.csv"
DEFAULT_CACHE_PATH = BASE_DIR / "geocode_cache.csv"
DEFAULT_TIMEZONE = ZoneInfo("America/Detroit")
REQUEST_TIMEOUT_SECONDS = 30
AREA_PREFIX_PATTERN = re.compile(r"^\s*Ar(?:ea|e)?\s*\d*\s*[:\-]\s*", re.IGNORECASE)
PAREN_ADDRESS_PATTERN = re.compile(r"^(?P<name>.*?)\((?P<address>[^()]*)\)\s*$")
ADDRESS_HINT_PATTERN = re.compile(r"\d")
ADDRESS_START_PATTERN = re.compile(r"\d[\w\s\.\-#/]*")
ZIP_PATTERN = re.compile(r"\b\d{5}(?:-\d{4})?\b")
UNIT_FRAGMENT_PATTERN = re.compile(r"\s*,?\s*\b(?:ste|suite|unit|apt|apartment|rm|room)\b[^\.,]*", re.IGNORECASE)
STATE_NAME_PATTERN = re.compile(r"\bMichigan\b", re.IGNORECASE)
COUNTRY_PATTERN = re.compile(r",?\s*United States\s*$", re.IGNORECASE)
EXTRA_PERIOD_PATTERN = re.compile(r"\b([A-Za-z]{1,4})\.")
VENUE_PREFIX_PATTERN = re.compile(r"^(?P<prefix>.*?)[\-:]\s*(?P<address>\d.*)$")
GR_SHORTHAND_PATTERN = re.compile(r"\bGR\b", re.IGNORECASE)
NE_GR_PATTERN = re.compile(r",\s*NE\s+Grand Rapids\b", re.IGNORECASE)
SPACE_BEFORE_DIRECTION_PATTERN = re.compile(r"\b([NSEW])\s+([NSEW])\b")
MULTI_COMMA_PATTERN = re.compile(r"\s*,\s*,+")
PREPARED_COLUMNS = [
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
]
CACHE_COLUMNS = ["geocode_address", "latitude", "longitude", "geocode_status"]

STATEFUL_CITY_HINTS = {
    "Grand Rapids": "Grand Rapids, MI",
    "Kentwood": "Kentwood, MI",
    "Wyoming": "Wyoming, MI",
    "Rockford": "Rockford, MI",
    "Lowell": "Lowell, MI",
    "Byron Center": "Byron Center, MI",
    "Belmont": "Belmont, MI",
    "Walker": "Walker, MI",
    "Grandville": "Grandville, MI",
    "Ada": "Ada, MI",
    "Alto": "Alto, MI",
    "Comstock Park": "Comstock Park, MI",
    "Caledonia": "Caledonia, MI",
    "Northview": "Northview, MI",
}
STREET_WORD_NORMALIZATIONS = {
    "avenue": "Ave",
    "boulevard": "Blvd",
    "circle": "Cir",
    "court": "Ct",
    "drive": "Dr",
    "east": "E",
    "highway": "Hwy",
    "lane": "Ln",
    "north": "N",
    "northeast": "NE",
    "northwest": "NW",
    "parkway": "Pkwy",
    "place": "Pl",
    "plaza": "Plz",
    "road": "Rd",
    "south": "S",
    "southeast": "SE",
    "southwest": "SW",
    "street": "St",
    "terrace": "Ter",
    "trail": "Trl",
    "west": "W",
}


def clean_text(value) -> str | None:
    if value is None or pd.isna(value):
        return None

    text = str(value).strip()
    if not text or text.lower() == "null":
        return None

    return re.sub(r"\s+", " ", text)


def clean_business_name(subject: object) -> str | None:
    text = clean_text(subject)
    if not text:
        return None

    cleaned = AREA_PREFIX_PATTERN.sub("", text).strip(" -:")
    return cleaned or text


def titlecase_token(token: str) -> str:
    if token.upper() in {"N", "S", "E", "W", "NE", "NW", "SE", "SW", "MI"}:
        return token.upper()
    if token.isupper() and len(token) > 2 and not token.isdigit():
        return token.title()
    return token


def normalize_punctuation(text: str) -> str:
    cleaned = text.replace(";", ",").replace(" ,", ",")
    cleaned = EXTRA_PERIOD_PATTERN.sub(r"\1", cleaned)
    cleaned = re.sub(r"\s*-\s*", " - ", cleaned)
    cleaned = MULTI_COMMA_PATTERN.sub(", ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" ,")


def remove_venue_prefix(text: str) -> str:
    match = VENUE_PREFIX_PATTERN.match(text)
    if match:
        return match.group("address")

    address_start = ADDRESS_START_PATTERN.search(text)
    if address_start and address_start.start() > 0:
        prefix = text[: address_start.start()]
        if any(separator in prefix for separator in (" - ", ":", "(")):
            return text[address_start.start() :]

    return text

def normalize_directional_grand_rapids(text: str) -> str:
    text = GR_SHORTHAND_PATTERN.sub("Grand Rapids", text)
    text = NE_GR_PATTERN.sub(", Grand Rapids", text)
    text = re.sub(r"\bNW Grand Rapids\b", "Grand Rapids", text, flags=re.IGNORECASE)
    text = re.sub(r"\bSE Grand Rapids\b", "Grand Rapids", text, flags=re.IGNORECASE)
    text = re.sub(r"\bSW Grand Rapids\b", "Grand Rapids", text, flags=re.IGNORECASE)
    return text


def normalize_word_tokens(text: str) -> str:
    tokens = []
    for raw_token in text.split():
        token = titlecase_token(raw_token)
        bare = re.sub(r"^[^\w]*|[^\w]*$", "", token)
        if bare.upper() in {"N", "S", "E", "W", "NE", "NW", "SE", "SW", "MI"}:
            token = token.replace(bare, bare.upper())
        replacement = STREET_WORD_NORMALIZATIONS.get(bare.lower())
        if replacement:
            token = token.replace(bare, replacement)
        tokens.append(token)

    normalized = " ".join(tokens)
    normalized = SPACE_BEFORE_DIRECTION_PATTERN.sub(r"\1\2", normalized)
    return normalized


def append_missing_state(text: str) -> str:
    if re.search(r"\bMI\b", text) or STATE_NAME_PATTERN.search(text):
        return text

    for city, city_with_state in STATEFUL_CITY_HINTS.items():
        if re.search(rf"\b{re.escape(city)}\b", text):
            return re.sub(rf"\b{re.escape(city)}\b", city_with_state, text, count=1)

    return text


def canonicalize_address(address: str | None) -> str | None:
    text = clean_text(address)
    if not text:
        return None

    text = normalize_punctuation(text)
    text = remove_venue_prefix(text)
    text = normalize_directional_grand_rapids(text)
    text = COUNTRY_PATTERN.sub("", text)
    text = UNIT_FRAGMENT_PATTERN.sub("", text)
    text = normalize_word_tokens(text)
    text = append_missing_state(text)
    text = re.sub(r"\s+,", ",", text)
    text = re.sub(r",\s*", ", ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ,")


def parse_location(location: object) -> tuple[str, str | None]:
    text = clean_text(location)
    if not text:
        return "Address unavailable", None

    match = PAREN_ADDRESS_PATTERN.match(text)
    if match:
        address = clean_text(match.group("address"))
        canonical = canonicalize_address(address)
        if canonical:
            display = clean_text(COUNTRY_PATTERN.sub("", address or ""))
            return display or canonical, canonical

    extracted = remove_venue_prefix(text)

    if ADDRESS_HINT_PATTERN.search(extracted):
        canonical = canonicalize_address(extracted)
        if canonical:
            return clean_text(extracted) or canonical, canonical

    return text, None


def format_schedule(start_dt: pd.Timestamp, end_dt: pd.Timestamp) -> str:
    if pd.notna(start_dt) and pd.notna(end_dt):
        start_date = start_dt.strftime("%m/%d/%Y")
        end_date = end_dt.strftime("%m/%d/%Y")
        start_time = start_dt.strftime("%I:%M %p").lstrip("0")
        end_time = end_dt.strftime("%I:%M %p").lstrip("0")
        return f"{start_date} {start_time} - {end_date} {end_time}"

    if pd.notna(start_dt):
        return start_dt.strftime("%m/%d/%Y %I:%M %p").replace(" 0", " ")

    if pd.notna(end_dt):
        return end_dt.strftime("%m/%d/%Y %I:%M %p").replace(" 0", " ")

    return "Unknown schedule"


def build_geocode_queries(address: str) -> list[str]:
    canonical = canonicalize_address(address)
    if not canonical:
        return []

    candidates: list[str] = []

    def add(candidate: str | None):
        cleaned = clean_text(candidate)
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)

    add(canonical)
    add(f"{canonical}, United States")

    michigan_variant = re.sub(r"\bMI\b", "Michigan", canonical)
    add(michigan_variant)
    add(f"{michigan_variant}, United States")

    no_zip = ZIP_PATTERN.sub("", canonical)
    no_zip = re.sub(r",\s*,", ",", no_zip)
    no_zip = re.sub(r"\s+", " ", no_zip).strip(" ,")
    add(no_zip)
    add(f"{no_zip}, United States" if no_zip else None)

    no_country = COUNTRY_PATTERN.sub("", canonical).strip(" ,")
    add(no_country)

    return candidates


def ensure_columns(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    prepared = df.copy()
    for column in columns:
        if column not in prepared.columns:
            prepared[column] = pd.NA
    return prepared


def load_geocode_cache(cache_path: Path) -> pd.DataFrame:
    if not cache_path.exists():
        return pd.DataFrame(columns=CACHE_COLUMNS)

    cache_df = pd.read_csv(cache_path)
    cache_df = ensure_columns(cache_df, CACHE_COLUMNS)
    cache_df["geocode_address"] = cache_df["geocode_address"].map(clean_text)
    cache_df = cache_df.dropna(subset=["geocode_address"])
    cache_df = cache_df.drop_duplicates(subset=["geocode_address"], keep="last")
    return cache_df[CACHE_COLUMNS]


def save_geocode_cache(cache_df: pd.DataFrame, cache_path: Path) -> None:
    cache_df = ensure_columns(cache_df, CACHE_COLUMNS)
    cache_df = cache_df.sort_values(by="geocode_address", na_position="last").reset_index(drop=True)
    cache_df[CACHE_COLUMNS].to_csv(cache_path, index=False)


def initialize_geocode_cache(cache_path: Path, reset_cache: bool = False) -> pd.DataFrame:
    if reset_cache:
        empty_cache = pd.DataFrame(columns=CACHE_COLUMNS)
        save_geocode_cache(empty_cache, cache_path)
        return empty_cache

    return load_geocode_cache(cache_path)


def load_csv_calendar(input_path: Path) -> pd.DataFrame:
    df = pd.read_csv(input_path)
    required_columns = {"Subject", "Start Date", "Start Time", "End Date", "End Time", "Location"}
    missing_columns = required_columns.difference(df.columns)
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise ValueError(f"Calendar dataset is missing required columns: {missing}")
    return df


def fetch_ics_calendar(ics_url: str) -> str:
    response = requests.get(ics_url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.text


def normalize_calendar_datetime(value) -> datetime | None:
    if value is None or pd.isna(value):
        return None

    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(DEFAULT_TIMEZONE).replace(tzinfo=None)
        return value

    if isinstance(value, date):
        return datetime.combine(value, time.min)

    return pd.to_datetime(value, errors="coerce").to_pydatetime()


def format_calendar_date(value: datetime | None) -> str | None:
    if value is None:
        return None
    return f"{value.month}/{value.day}/{value.year}"


def format_calendar_time(value: datetime | None, all_day: bool = False) -> str | None:
    if value is None or all_day:
        return "12:00:00 AM" if value is not None else None
    return value.strftime("%I:%M:%S %p").lstrip("0")


def get_current_month_start(reference_date: date | datetime | None = None) -> pd.Timestamp:
    today = pd.Timestamp(reference_date or date.today())
    return today.normalize().replace(day=1)


def filter_calendar_to_current_month_and_future(df: pd.DataFrame, reference_date: date | datetime | None = None) -> pd.DataFrame:
    month_start = get_current_month_start(reference_date=reference_date)
    filtered = df[df["start_datetime"] >= month_start].copy()
    return filtered.reset_index(drop=True)


def calendar_property_text(event, property_name: str) -> str | None:
    value = event.get(property_name)
    if value is None:
        return None
    return clean_text(value)


def load_ics_calendar(ics_url: str) -> pd.DataFrame:
    try:
        from icalendar import Calendar
    except ImportError as exc:
        raise ImportError(
            "icalendar is required for remote Outlook calendar parsing. Install requirements.txt first."
        ) from exc

    calendar = Calendar.from_ical(fetch_ics_calendar(ics_url))
    rows = []
    for event in calendar.walk("VEVENT"):
        status = clean_text(event.get("STATUS"))
        if status and status.upper() == "CANCELLED":
            continue

        starts_at_raw = event.decoded("DTSTART", None)
        ends_at_raw = event.decoded("DTEND", None)
        starts_at = normalize_calendar_datetime(starts_at_raw)
        ends_at = normalize_calendar_datetime(ends_at_raw)
        all_day = isinstance(starts_at_raw, date) and not isinstance(starts_at_raw, datetime)

        rows.append(
            {
                "Subject": calendar_property_text(event, "SUMMARY"),
                "Start Date": format_calendar_date(starts_at),
                "Start Time": format_calendar_time(starts_at, all_day=all_day),
                "End Date": format_calendar_date(ends_at),
                "End Time": format_calendar_time(ends_at, all_day=all_day),
                "Location": calendar_property_text(event, "LOCATION"),
            }
        )

    return pd.DataFrame(rows, columns=["Subject", "Start Date", "Start Time", "End Date", "End Time", "Location"])


def load_calendar(input_path: Path | None = None, ics_url: str | None = None) -> pd.DataFrame:
    if input_path is not None:
        return load_csv_calendar(input_path)
    return load_ics_calendar(ics_url or DEFAULT_ICS_URL)


def prepare_calendar_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    prepared = df.copy()
    prepared["business_name"] = prepared["Subject"].map(clean_business_name)

    parsed_locations = prepared["Location"].map(parse_location)
    prepared["display_address"] = parsed_locations.map(lambda pair: pair[0])
    prepared["canonical_address"] = parsed_locations.map(lambda pair: pair[1])
    prepared["geocode_address"] = prepared["canonical_address"]

    prepared["start_date"] = prepared["Start Date"].map(clean_text)
    prepared["end_date"] = prepared["End Date"].map(clean_text)
    prepared["start_time"] = prepared["Start Time"].map(clean_text)
    prepared["end_time"] = prepared["End Time"].map(clean_text)

    prepared["start_datetime"] = pd.to_datetime(
        prepared["Start Date"].fillna("").astype(str) + " " + prepared["Start Time"].fillna("").astype(str),
        errors="coerce",
        format="mixed",
    )
    prepared["end_datetime"] = pd.to_datetime(
        prepared["End Date"].fillna("").astype(str) + " " + prepared["End Time"].fillna("").astype(str),
        errors="coerce",
        format="mixed",
    )
    prepared["display_schedule"] = [
        format_schedule(start_dt, end_dt)
        for start_dt, end_dt in zip(prepared["start_datetime"], prepared["end_datetime"])
    ]

    prepared["latitude"] = pd.NA
    prepared["longitude"] = pd.NA
    prepared["geocode_status"] = prepared["geocode_address"].map(
        lambda address: "pending" if address else "no_usable_address"
    )
    return prepared


def geocode_addresses(
    addresses: list[str],
    cache_path: Path,
    user_agent: str = "stfu-calendar-prep",
    min_delay_seconds: float = 1.0,
    reset_cache: bool = False,
) -> pd.DataFrame:
    cache_df = initialize_geocode_cache(cache_path, reset_cache=reset_cache)
    known_addresses = set(cache_df["geocode_address"].dropna())
    unique_addresses = sorted({clean_text(address) for address in addresses if clean_text(address)})
    addresses_to_geocode = [address for address in unique_addresses if address not in known_addresses]

    if not addresses_to_geocode:
        return cache_df

    try:
        from geopy.extra.rate_limiter import RateLimiter
        from geopy.geocoders import ArcGIS
    except ImportError as exc:
        raise ImportError(
            "geopy is required for calendar geocoding. Install dependencies before running the prep script."
        ) from exc

    # Swapped Nominatim for ArcGIS. ArcGIS doesn't require a user_agent string.
    geolocator = ArcGIS()
    geocode = RateLimiter(geolocator.geocode, min_delay_seconds=min_delay_seconds)

    new_rows = []
    for address in addresses_to_geocode:
        result = None
        for query in build_geocode_queries(address):
            try:
                result = geocode(query)
            except Exception as exc:
                print(f"[Geocode Error] {query}: {exc}")
                result = None

            if result is not None:
                break

        if result is None:
            new_rows.append(
                {
                    "geocode_address": address,
                    "latitude": pd.NA,
                    "longitude": pd.NA,
                    "geocode_status": "not_found",
                }
            )
            continue

        new_rows.append(
            {
                "geocode_address": address,
                "latitude": result.latitude,
                "longitude": result.longitude,
                "geocode_status": "geocoded",
            }
        )

    if new_rows:
        new_rows_df = pd.DataFrame(new_rows, columns=CACHE_COLUMNS)
        if cache_df.empty:
            updated_cache = new_rows_df
        else:
            updated_cache = pd.concat([cache_df, new_rows_df], ignore_index=True)
    else:
        updated_cache = cache_df
    updated_cache = updated_cache.drop_duplicates(subset=["geocode_address"], keep="last")
    save_geocode_cache(updated_cache, cache_path)
    return updated_cache[CACHE_COLUMNS]


def run(
    input_path: Path | None = None,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    ics_url: str | None = DEFAULT_ICS_URL,
    skip_geocoding: bool = False,
    cache_path: Path = DEFAULT_CACHE_PATH,
    reset_cache: bool = False,
    future_only: bool = False,
) -> pd.DataFrame:
    raw_df = load_calendar(input_path=input_path, ics_url=ics_url)
    prepared = prepare_calendar_dataframe(raw_df)
    if future_only:
        prepared = filter_calendar_to_current_month_and_future(prepared)

    if not skip_geocoding:
        geocode_df = geocode_addresses(
            prepared["geocode_address"].dropna().tolist(),
            cache_path=cache_path,
            reset_cache=reset_cache,
        )
        prepared = prepared.drop(columns=["latitude", "longitude", "geocode_status"]).merge(
            geocode_df,
            on="geocode_address",
            how="left",
        )
        prepared["geocode_status"] = prepared["geocode_status"].fillna("no_usable_address")
    else:
        prepared["geocode_status"] = prepared["geocode_status"].replace({"pending": "skipped"})

    prepared["latitude"] = pd.to_numeric(prepared["latitude"], errors="coerce")
    prepared["longitude"] = pd.to_numeric(prepared["longitude"], errors="coerce")
    prepared = prepared.sort_values(
        by=["start_datetime", "business_name", "display_address"],
        na_position="last",
    ).reset_index(drop=True)

    prepared[PREPARED_COLUMNS].to_csv(output_path, index=False)
    print(f"Saved prepared calendar dataset to '{output_path}'")
    return prepared[PREPARED_COLUMNS]


def parse_args():
    parser = argparse.ArgumentParser(description="Prepare a geocoded dashboard dataset from an Outlook ICS calendar.")
    parser.add_argument("--input", type=Path, default=None, help="Optional Outlook CSV export for local testing.")
    parser.add_argument("--ics-url", default=DEFAULT_ICS_URL, help="Published Outlook iCalendar URL to fetch.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE_PATH)
    parser.add_argument("--skip-geocoding", action="store_true")
    parser.add_argument("--reset-cache", action="store_true")
    parser.add_argument(
        "--future-only",
        action="store_true",
        help="Keep only events from the current month and future.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(
        input_path=args.input,
        output_path=args.output,
        ics_url=args.ics_url,
        skip_geocoding=args.skip_geocoding,
        cache_path=args.cache,
        reset_cache=args.reset_cache,
        future_only=args.future_only,
    )
