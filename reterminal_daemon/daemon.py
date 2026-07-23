from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import importlib.resources as resources
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, cast

from bleak import BleakScanner  # pyright: ignore[reportMissingImports]
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import (
    InstalledAppFlow,  # pyright: ignore[reportMissingImports]
)
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from jinja2 import (  # pyright: ignore[reportMissingImports]
    Environment,
    select_autoescape,
)
from opendisplay import (  # pyright: ignore[reportMissingImports]
    MANUFACTURER_ID,
    parse_advertisement,
)
from PIL import Image  # pyright: ignore[reportMissingImports]

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
IMAGE_WIDTH = 800
IMAGE_HEIGHT = 480
BROWSER_CANDIDATES = [
    "google-chrome",
    "google-chrome-stable",
]
DAEMON_SCAN_INTERVAL_SECONDS = 10.0
DAEMON_REFRESH_MAX_ATTEMPTS = 5
DAEMON_REFRESH_RETRY_DELAY_SECONDS = 1.0
DAEMON_NO_CHANGE_SLEEP_SECONDS = 60.0
DEEP_SLEEP_SETTLE_SECONDS = 2.0
DEEP_SLEEP_RETRY_DELAY_SECONDS = 2.0
BATTERY_PERCENT_TABLE = [
    (4.15, 100.0),
    (3.96, 90.0),
    (3.91, 80.0),
    (3.85, 70.0),
    (3.80, 60.0),
    (3.75, 50.0),
    (3.68, 40.0),
    (3.58, 30.0),
    (3.49, 20.0),
    (3.41, 10.0),
    (3.30, 5.0),
    (3.27, 0.0),
]
CACHE_DIR = Path("~/.cache/reterminal-daemon").expanduser()
DEFAULT_CREDENTIALS_PATH = str(CACHE_DIR / "credentials.json")
DEFAULT_TOKEN_PATH = str(CACHE_DIR / "token.json")


def import_json_file(import_path: str, destination: str, label: str) -> Path:
    source_path = Path(import_path).expanduser()
    if not source_path.exists():
        raise FileNotFoundError(f"{label} file not found at '{source_path}'.")
    if not source_path.is_file():
        raise ValueError(f"{label} path is not a file: '{source_path}'.")

    destination_path = Path(destination).expanduser()
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, destination_path)
    return destination_path


def import_credentials_to_default_location(import_path: str) -> Path:
    return import_json_file(
        import_path,
        DEFAULT_CREDENTIALS_PATH,
        label="Credentials",
    )


def get_credentials(credentials_path: str, token_path: str) -> Credentials:
    credentials_file = Path(credentials_path).expanduser()
    token_file_path = Path(token_path).expanduser()

    creds: Credentials | None = None

    if token_file_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_file_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not credentials_file.exists():
                raise FileNotFoundError(
                    f"OAuth client file not found at '{credentials_file}'. "
                    "Download it from Google Cloud Console and try again."
                )

            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_file), SCOPES
            )
            creds = cast(Credentials, flow.run_local_server(port=0))

        if creds is None:
            raise RuntimeError("Failed to obtain Google OAuth credentials.")

        token_file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(token_file_path, "w", encoding="utf-8") as token_file:
            token_file.write(cast(Any, creds).to_json())

    if creds is None:
        raise RuntimeError("Failed to load credentials from token file.")

    return creds


def list_connected_calendar_ids(creds: Credentials) -> list[str]:
    service = cast(Any, build("calendar", "v3", credentials=creds))

    calendar_ids: list[str] = []
    page_token: str | None = None
    while True:
        calendars_result = (
            service.calendarList().list(pageToken=page_token, showHidden=True).execute()
        )
        for calendar in calendars_result.get("items", []):
            calendar_id = calendar.get("id")
            if calendar_id:
                calendar_ids.append(calendar_id)

        page_token = calendars_result.get("nextPageToken")
        if not page_token:
            break

    return calendar_ids


def list_upcoming_events(
    creds: Credentials, max_results: int, selected_calendar_ids: list[str] | None = None
) -> list[dict[str, Any]]:
    service = cast(Any, build("calendar", "v3", credentials=creds))
    now = dt.datetime.now(tz=dt.timezone.utc).isoformat()

    calendar_ids = list_connected_calendar_ids(creds)
    if selected_calendar_ids:
        connected = set(calendar_ids)
        calendar_ids = [
            calendar_id
            for calendar_id in selected_calendar_ids
            if calendar_id in connected
        ]
        if not calendar_ids:
            raise ValueError(
                "None of the selected calendar IDs are connected to this account."
            )

    all_events: list[dict[str, Any]] = []
    for calendar_id in calendar_ids:
        events_result = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=now,
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        all_events.extend(cast(list[dict[str, Any]], events_result.get("items", [])))

    def dedupe_key(event: dict[str, Any]) -> tuple[str, str, str, str, str]:
        start_info = event.get("start", {})
        end_info = event.get("end", {})
        return (
            event.get("iCalUID") or "",
            start_info.get("dateTime") or start_info.get("date") or "",
            end_info.get("dateTime") or end_info.get("date") or "",
            event.get("summary") or "",
            event.get("status") or "",
        )

    unique_events: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str, str, str]] = set()
    for event in all_events:
        key = dedupe_key(event)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique_events.append(event)

    def event_sort_key(event: dict[str, Any]) -> dt.datetime:
        start_info = event.get("start", {})
        start_raw = start_info.get("dateTime") or start_info.get("date")
        if not start_raw:
            return dt.datetime.max.replace(tzinfo=dt.timezone.utc)

        if "T" in start_raw:
            try:
                return dt.datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
            except ValueError:
                return dt.datetime.max.replace(tzinfo=dt.timezone.utc)

        try:
            start_date = dt.date.fromisoformat(start_raw)
            return dt.datetime.combine(start_date, dt.time.min, tzinfo=dt.timezone.utc)
        except ValueError:
            return dt.datetime.max.replace(tzinfo=dt.timezone.utc)

    unique_events.sort(key=event_sort_key)
    return unique_events[:max_results]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Authenticate with Google OAuth2 and list upcoming Calendar events."
    )
    parser.add_argument(
        "--import-credentials",
        default=None,
        help=(
            "Path to an OAuth client secrets JSON file to copy into the default "
            "credentials location and exit."
        ),
    )
    parser.add_argument(
        "--credentials",
        default=DEFAULT_CREDENTIALS_PATH,
        help="Path to OAuth client secrets JSON from Google Cloud Console.",
    )
    parser.add_argument(
        "--token",
        default=DEFAULT_TOKEN_PATH,
        help="Path where the user access/refresh token will be stored.",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=3,
        help="Maximum number of upcoming events to show.",
    )
    parser.add_argument(
        "--template",
        default=None,
        help=(
            "Optional path to a Jinja HTML template file. "
            "Defaults to the embedded template resource."
        ),
    )
    parser.add_argument(
        "--output",
        default="events.png",
        help="Path for rendered image output.",
    )
    parser.add_argument(
        "--eink-mac",
        default=None,
        help="Target e-ink device MAC address.",
    )
    parser.add_argument(
        "--eink-key",
        default=None,
        help="Encryption key as hex string for e-ink authentication.",
    )
    parser.add_argument(
        "--skip-device-upload",
        action="store_true",
        help="Render image only and skip uploading it to the e-ink device.",
    )
    parser.add_argument(
        "--list-calendar-ids",
        action="store_true",
        help="Print connected calendar IDs and exit.",
    )
    parser.add_argument(
        "--calendar-id",
        action="append",
        default=[],
        help=(
            "Restrict event fetching to specific connected calendar IDs. "
            "Repeat the flag to provide multiple IDs."
        ),
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help=(
            "Continuously scan for the target BLE device and refresh the "
            "display whenever it is found."
        ),
    )
    parser.add_argument(
        "--daemon-scan-interval",
        type=float,
        default=DAEMON_SCAN_INTERVAL_SECONDS,
        help="Seconds to wait between BLE scans in daemon mode.",
    )
    parser.add_argument(
        "--battery-cal",
        type=float,
        default=0.0,
        help="Voltage offset in volts to apply to the parsed battery reading.",
    )
    parser.add_argument(
        "--battery-threshold",
        dest="battery_threshold",
        type=float,
        default=20.0,
        help="Battery percentage threshold below which the low-battery icon is shown.",
    )
    args = parser.parse_args()
    if args.daemon and not args.eink_mac:
        parser.error("--eink-mac is required when --daemon is set.")
    return args


def normalize_events(events: list[dict[str, Any]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []

    for event in events:
        start_info = event.get("start", {})
        date_time_raw = start_info.get("dateTime", "")
        all_day_raw = start_info.get("date", "")

        month_text = ""
        day_text = ""
        date_text = ""
        time_text = ""

        if date_time_raw:
            month_text, day_text, date_text, time_text = format_timed_event_display(
                date_time_raw
            )
        else:
            month_text, day_text, date_text = format_all_day_event_display(all_day_raw)
        summary = event.get("summary", "(No title)")

        normalized.append(
            {
                "month": month_text,
                "day": day_text,
                "date": date_text,
                "time": time_text,
                "summary": summary,
            }
        )

    return normalized


def format_start_time(start_raw: str) -> str:
    if not start_raw:
        return ""

    try:
        if "T" in start_raw:
            parsed = dt.datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        else:
            parsed_date = dt.date.fromisoformat(start_raw)
            parsed = dt.datetime.combine(parsed_date, dt.time.min)

        date_part = parsed.strftime("%A, %b %d").replace(" 0", " ")
        time_part = parsed.strftime("%I:%M %p").lstrip("0")
        return f"{date_part} - {time_part}"
    except ValueError:
        return start_raw


def format_all_day_start(start_raw: str) -> str:
    if not start_raw:
        return ""

    try:
        parsed_date = dt.date.fromisoformat(start_raw)
        return parsed_date.strftime("%A, %b %d").replace(" 0", " ")
    except ValueError:
        return start_raw


def format_timed_event_display(start_raw: str) -> tuple[str, str, str, str]:
    if not start_raw:
        return "", "", "", ""

    try:
        parsed = dt.datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        month_text = parsed.strftime("%a").upper()
        day_text = str(parsed.day)
        date_text = ""
        time_text = parsed.strftime("%I:%M %p").lstrip("0")
        return month_text, day_text, date_text, time_text
    except ValueError:
        return "", "", "", ""


def format_all_day_event_display(start_raw: str) -> tuple[str, str, str]:
    if not start_raw:
        return "", "", ""

    try:
        parsed_date = dt.date.fromisoformat(start_raw)
        month_text = parsed_date.strftime("%a").upper()
        day_text = str(parsed_date.day)
        date_text = ""
        return month_text, day_text, date_text
    except ValueError:
        return "", "", ""


def get_display_day_and_date() -> tuple[str, str]:
    now = dt.datetime.now()
    day_text = now.strftime("%A")
    date_text = now.strftime("%B %d, %Y").replace(" 0", " ")
    return day_text, date_text


def load_template_html(template_path: str | None) -> str:
    if template_path:
        template_file = Path(template_path).expanduser()
        if not template_file.exists():
            raise FileNotFoundError(f"Template file not found at '{template_path}'.")
        return template_file.read_text(encoding="utf-8")

    return (
        resources.files("reterminal_daemon")
        .joinpath("template.html")
        .read_text(encoding="utf-8")
    )


def render_events_to_image(
    events: list[dict[str, Any]],
    template_path: str | None,
    output_path: str,
    voltage="unknown",
    low_battery=False,
) -> None:
    template_html = load_template_html(template_path)
    env = Environment(autoescape=select_autoescape(["html", "xml"]))
    template = env.from_string(template_html)

    day_text, date_text = get_display_day_and_date()

    html = template.render(
        day=day_text,
        date=date_text,
        events=normalize_events(events),
        generated_at=dt.datetime.now(),
        voltage=voltage,
        low_battery=low_battery,
    )

    browser_path = next(
        (shutil.which(name) for name in BROWSER_CANDIDATES if shutil.which(name)), None
    )
    if browser_path is None:
        raise RuntimeError(
            "No Chromium/Chrome executable found. Install Chromium or Google Chrome "
            "to enable image export."
        )

    output_file = str(Path(output_path).resolve())

    with tempfile.NamedTemporaryFile(
        "w", suffix=".html", delete=False, encoding="utf-8"
    ) as tmp_file:
        tmp_file.write(html)
        tmp_html_path = Path(tmp_file.name)

    try:
        cmd = [
            browser_path,
            "--headless",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--window-size={IMAGE_WIDTH},{IMAGE_HEIGHT}",
            f"--screenshot={output_file}",
            tmp_html_path.resolve().as_uri(),
        ]

        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"Browser screenshot failed: {exc.stderr.strip() or exc.stdout.strip() or exc}"
        ) from exc
    finally:
        try:
            tmp_html_path.unlink(missing_ok=True)
        except Exception:
            pass


def parse_encryption_key(key_text: str | None) -> bytes | None:
    if key_text is None:
        return None

    cleaned = key_text.strip().lower().replace("0x", "")
    for sep in (":", "-", " "):
        cleaned = cleaned.replace(sep, "")

    if len(cleaned) % 2 != 0:
        raise ValueError(
            "Encryption key must contain an even number of hex characters."
        )

    try:
        return bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ValueError("Encryption key must be a valid hex string.") from exc


async def upload_image_to_eink(
    image_path: str, mac_address: str, encryption_key: bytes | None
) -> None:
    from opendisplay.device import (
        OpenDisplayDevice,  # pyright: ignore[reportMissingImports]
    )

    image = Image.open(image_path).convert("RGB")

    async with OpenDisplayDevice(
        mac_address=mac_address,
        encryption_key=encryption_key,
        use_services_cache=False,
    ) as device:
        await device.upload_image(image)
        await asyncio.sleep(30)
        await device.deep_sleep()
        await asyncio.sleep(5)


async def deep_sleep_eink_device(
    mac_address: str,
    encryption_key: bytes | None,
    max_attempts: int = DAEMON_REFRESH_MAX_ATTEMPTS,
) -> None:
    from opendisplay.device import (
        OpenDisplayDevice,  # pyright: ignore[reportMissingImports]
    )

    last_error: Exception | None = None

    # Give BLE stack/device a moment after scan before initiating auth.
    await asyncio.sleep(DEEP_SLEEP_SETTLE_SECONDS)

    for attempt in range(1, max_attempts + 1):
        try:
            async with OpenDisplayDevice(
                mac_address=mac_address,
                encryption_key=encryption_key,
                use_services_cache=False,
            ) as device:
                await device.deep_sleep()
            return
        except Exception as exc:
            last_error = exc
            logger.warning(
                f"Deep sleep attempt {attempt}/{max_attempts} failed: {exc}",
            )
            if attempt < max_attempts:
                await asyncio.sleep(DEEP_SLEEP_RETRY_DELAY_SECONDS)

    raise RuntimeError(
        f"Deep sleep failed after {max_attempts} attempts."
    ) from last_error


async def refresh_display_once(
    args: argparse.Namespace,
    creds: Credentials,
    voltage="unknown",
    low_battery=False,
    events: list[dict[str, Any]] | None = None,
    output: str | None = None,
) -> None:
    if events is None:
        events = list_upcoming_events(
            creds,
            args.max_results,
            selected_calendar_ids=args.calendar_id,
        )

    if output is None:
        output = args.output
    assert output is not None

    render_events_to_image(
        events,
        args.template,
        output,
        voltage=voltage,
        low_battery=low_battery,
    )

    if not args.skip_device_upload:
        if not args.eink_mac:
            raise ValueError(
                "--eink-mac is required unless --skip-device-upload is set."
            )
        key_bytes = parse_encryption_key(args.eink_key)
        await upload_image_to_eink(output, args.eink_mac, key_bytes)


async def refresh_display_with_retries(
    args: argparse.Namespace,
    creds: Credentials,
    voltage: str = "unknown",
    low_battery: bool = False,
    max_attempts: int = DAEMON_REFRESH_MAX_ATTEMPTS,
    events: list[dict[str, Any]] | None = None,
    output: str | None = None,
) -> None:
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            await refresh_display_once(
                args,
                creds,
                voltage=voltage,
                low_battery=low_battery,
                events=events,
                output=output,
            )
            return
        except Exception as exc:
            last_error = exc
            logger.warning(
                f"Refresh attempt {attempt}/{max_attempts} failed: {exc}",
            )
            if attempt < max_attempts:
                await asyncio.sleep(DAEMON_REFRESH_RETRY_DELAY_SECONDS)

    raise RuntimeError(f"Refresh failed after {max_attempts} attempts.") from last_error


def parse_battery_voltage(advertisement_data: Any, battery_cal: float) -> str:
    manufacturer_data = getattr(advertisement_data, "manufacturer_data", {}) or {}
    payload = manufacturer_data.get(MANUFACTURER_ID)

    if payload is None and manufacturer_data:
        first_payload = next(iter(manufacturer_data.values()), None)
        if isinstance(first_payload, bytes):
            payload = first_payload

    if not isinstance(payload, bytes):
        return "unknown"

    try:
        parsed = parse_advertisement(payload)
        calibrated_voltage = parsed.battery_mv / 1000.0 + battery_cal
        return f"{calibrated_voltage:.3f}V"
    except Exception:
        return "unknown"


def battery_percentage_from_voltage(voltage: float) -> float:
    if voltage >= BATTERY_PERCENT_TABLE[0][0]:
        return BATTERY_PERCENT_TABLE[0][1]
    if voltage <= BATTERY_PERCENT_TABLE[-1][0]:
        return BATTERY_PERCENT_TABLE[-1][1]

    for (upper_voltage, upper_percent), (lower_voltage, lower_percent) in zip(
        BATTERY_PERCENT_TABLE, BATTERY_PERCENT_TABLE[1:]
    ):
        if upper_voltage >= voltage >= lower_voltage:
            span = upper_voltage - lower_voltage
            if span == 0:
                return upper_percent

            ratio = (voltage - lower_voltage) / span
            return lower_percent + ratio * (upper_percent - lower_percent)

    return BATTERY_PERCENT_TABLE[-1][1]


def parse_battery_percentage(
    advertisement_data: Any, battery_cal: float
) -> float | None:
    battery_voltage_text = parse_battery_voltage(advertisement_data, battery_cal)
    if battery_voltage_text == "unknown":
        return None

    try:
        battery_voltage = float(battery_voltage_text.rstrip("V"))
    except ValueError:
        return None

    return battery_percentage_from_voltage(battery_voltage)


async def scan_for_target_device(
    target_mac: str, battery_cal: float
) -> tuple[str, float | None]:
    target_mac_lower = target_mac.lower()
    loop = asyncio.get_running_loop()
    found_future: asyncio.Future[tuple[str, float | None]] = loop.create_future()

    def detection_callback(device: Any, advertisement_data: Any) -> None:
        if device.address.lower() != target_mac_lower:
            return

        battery_voltage = parse_battery_voltage(advertisement_data, battery_cal)
        battery_percentage = parse_battery_percentage(advertisement_data, battery_cal)
        logger.debug(
            "BLE packet: "
            f"address={device.address} "
            f"name={device.name!r} "
            f"rssi={getattr(advertisement_data, 'rssi', None)} "
            f"battery={battery_voltage} "
            f"battery_pct={battery_percentage if battery_percentage is not None else 'unknown'} "
            f"data={advertisement_data}"
        )

        if not found_future.done():
            found_future.set_result((battery_voltage, battery_percentage))

    async with BleakScanner(
        detection_callback=detection_callback, scanning_mode="active"
    ):
        return await found_future


async def run_daemon(args: argparse.Namespace, creds: Credentials) -> None:
    logger.info(f"Daemon mode enabled. Scanning for {args.eink_mac}...")
    last_calendar_signature: tuple[tuple[str, str], ...] | None = None
    last_low_battery: bool | None = None
    last_day_and_date: tuple[str, str] | None = None

    with tempfile.TemporaryDirectory() as tempdir:
        workdir = Path(tempdir)
        image_file = workdir / "events.png"

        while True:
            scan_task = asyncio.create_task(
                scan_for_target_device(args.eink_mac, args.battery_cal)
            )
            battery_voltage, battery_percentage = await scan_task
            low_battery = (
                battery_percentage is not None
                and battery_percentage < args.battery_threshold
            )

            events = list_upcoming_events(
                creds,
                args.max_results,
                selected_calendar_ids=args.calendar_id,
            )
            calendar_signature = tuple(
                (
                    event.get("start", {}).get(
                        "dateTime", event.get("start", {}).get("date", "")
                    )
                    or "",
                    event.get("summary", "(No title)"),
                )
                for event in events
            )
            day_and_date = get_display_day_and_date()

            unchanged = (
                last_calendar_signature == calendar_signature
                and last_low_battery == low_battery
                and last_day_and_date == day_and_date
            )
            if unchanged:
                if not args.skip_device_upload:
                    key_bytes = parse_encryption_key(args.eink_key)
                    await deep_sleep_eink_device(args.eink_mac, key_bytes)
                logger.info(
                    "No calendar, date/day, or battery-warning change detected; "
                    f"sleeping {int(DAEMON_NO_CHANGE_SLEEP_SECONDS)}s before rescanning."
                )
                await asyncio.sleep(DAEMON_NO_CHANGE_SLEEP_SECONDS)
                continue

            logger.info(f"Detected {args.eink_mac}; refreshing calendar and display.")
            await refresh_display_with_retries(
                args,
                creds,
                voltage=battery_voltage,
                low_battery=low_battery,
                events=events,
                output=str(image_file.absolute()),
            )
            last_calendar_signature = calendar_signature
            last_low_battery = low_battery
            last_day_and_date = day_and_date
            logger.info(f"Updated '{image_file}' for {args.eink_mac}.")

            await asyncio.sleep(args.daemon_scan_interval)

            ## BUG WORKAROUND
            ## https://github.com/OpenDisplay/py-opendisplay/issues/141
            # reconnections fail after a deep_sleep()
            # Exit the daemon and let systemd restart it.
            return


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()

    try:
        if args.import_credentials:
            destination = import_credentials_to_default_location(
                args.import_credentials
            )
            logger.info(f"Imported credentials to '{destination}'.")
            return 0

        creds = get_credentials(args.credentials, args.token)

        if args.list_calendar_ids:
            calendar_ids = list_connected_calendar_ids(creds)
            for calendar_id in calendar_ids:
                logger.info(calendar_id)
            return 0

        if args.daemon:
            asyncio.run(run_daemon(args, creds))
            return 0

        events = list_upcoming_events(
            creds,
            args.max_results,
            selected_calendar_ids=args.calendar_id,
        )
        render_events_to_image(events, args.template, args.output)
        if not args.skip_device_upload:
            if not args.eink_mac:
                raise ValueError(
                    "--eink-mac is required unless --skip-device-upload is set."
                )
            key_bytes = parse_encryption_key(args.eink_key)
            asyncio.run(upload_image_to_eink(args.output, args.eink_mac, key_bytes))
    except ValueError as exc:
        logger.error(f"Error: {exc}")
        return 1
    except FileNotFoundError as exc:
        logger.error(f"Error: {exc}")
        return 1
    except HttpError as exc:
        logger.error(f"Google API error: {exc}")
        return 1
    except Exception as exc:  # pragma: no cover
        logger.exception(f"Unexpected error: {exc}")
        return 1

    if not events:
        logger.info(
            f"No upcoming events found. Rendered empty page to '{args.output}'."
        )
        return 0

    logger.info(f"Rendered {len(events)} event(s) to '{args.output}'.")
    logger.info("Upcoming events:")
    for event in events:
        start_raw = event.get("start", {}).get(
            "dateTime", event.get("start", {}).get("date")
        )
        start = format_start_time(start_raw or "")
        summary = event.get("summary", "(No title)")
        logger.info(f"- {start}: {summary}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
