

import asyncio
from base64 import b64decode
from io import BytesIO
import json
import logging
import os
import random
import time
from datetime import datetime, time as clock_time, timedelta, timezone
from typing import Callable, TypeVar
from unicodedata import normalize
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
import gspread
from discord.ext import commands, tasks
from google.oauth2.service_account import Credentials


# ============================================================
# CONFIGURATION
# ============================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GOOGLE_CREDENTIALS_RAW = os.getenv("GOOGLE_CREDENTIALS")

SPREADSHEET_NAME = os.getenv(
    "SPREADSHEET_NAME",
    "Wingstore People Operations Master",
)
RECORDS_WORKSHEET_NAME = os.getenv(
    "REGISTRO_SHEET_NAME",
    "Respuestas de formulario 1",
)
EMPLOYEES_WORKSHEET_NAME = os.getenv(
    "EMPLEADOS_SHEET_NAME",
    "EMPLEADOS Y CONTRATOS",
)

EMPLOYEE_NAME_COLUMN = os.getenv("EMPLOYEE_NAME_COLUMN", "").strip().upper()
EMPLOYEE_TIMEZONE_COLUMN = os.getenv("EMPLOYEE_TIMEZONE_COLUMN", "").strip().upper()

BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "America/Caracas")
MAX_GOOGLE_RETRIES = int(os.getenv("MAX_GOOGLE_RETRIES", "4"))
IDS_REFRESH_MINUTES = int(os.getenv("IDS_REFRESH_MINUTES", "10"))
HR_CHANNEL_ID = int(os.getenv("HR_CHANNEL_ID", "0"))

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

if not DISCORD_TOKEN:
    raise RuntimeError("The DISCORD_TOKEN environment variable is missing.")

if not GOOGLE_CREDENTIALS_RAW:
    raise RuntimeError(
        "The GOOGLE_CREDENTIALS environment variable is missing."
    )

try:
    SERVICE_ACCOUNT_INFO = json.loads(GOOGLE_CREDENTIALS_RAW)
except json.JSONDecodeError as exc:
    raise RuntimeError(
        "GOOGLE_CREDENTIALS does not contain valid JSON."
    ) from exc

try:
    TIMEZONE = ZoneInfo(BOT_TIMEZONE)
    VENEZUELA_TIMEZONE = ZoneInfo("America/Caracas")
except Exception as exc:
    raise RuntimeError(
        f"The BOT_TIMEZONE value is invalid: {BOT_TIMEZONE}"
    ) from exc


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("uncle_max")


# ============================================================
# GOOGLE SHEETS
# ============================================================

google_client: gspread.Client | None = None
spreadsheet: gspread.Spreadsheet | None = None
records_worksheet: gspread.Worksheet | None = None
employees_worksheet: gspread.Worksheet | None = None

EMPLOYEES_CACHE: dict[str, tuple[str, ZoneInfo | None]] = {}

# Prevents two interactions in this process from editing the worksheet
# at exactly the same time.
SHEETS_LOCK = asyncio.Lock()

T = TypeVar("T")


def format_us_date(date_value: str) -> str:
    """Converts YYYY-MM-DD into a natural U.S. date when possible."""
    try:
        parsed = datetime.strptime(date_value, "%Y-%m-%d")
        return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"
    except (TypeError, ValueError):
        return date_value or "date unavailable"


def format_us_time(time_value: str) -> str:
    """Converts 24-hour HH:MM into U.S. 12-hour time when possible."""
    try:
        parsed = datetime.strptime(time_value, "%H:%M")
        return parsed.strftime("%I:%M %p").lstrip("0")
    except (TypeError, ValueError):
        return time_value or "time unavailable"


def current_us_date_time() -> tuple[str, str]:
    now = datetime.now(TIMEZONE)
    return (
        f"{now.strftime('%B')} {now.day}, {now.year}",
        now.strftime("%I:%M %p").lstrip("0"),
    )


def connect_google_sync() -> None:
    """Creates a fresh Google connection and loads the required worksheets."""
    global google_client
    global spreadsheet
    global records_worksheet
    global employees_worksheet

    credentials = Credentials.from_service_account_info(
        SERVICE_ACCOUNT_INFO,
        scopes=SCOPES,
    )

    google_client = gspread.authorize(credentials)
    spreadsheet = google_client.open(SPREADSHEET_NAME)
    records_worksheet = spreadsheet.worksheet(RECORDS_WORKSHEET_NAME)
    employees_worksheet = spreadsheet.worksheet(EMPLOYEES_WORKSHEET_NAME)

    logger.info("Connection to Google Sheets established.")


def ensure_google_sync() -> None:
    if records_worksheet is None or employees_worksheet is None:
        connect_google_sync()


def run_google_with_retries_sync(
    operation_name: str,
    operation: Callable[[], T],
) -> T:
    """
    Runs a Google Sheets operation with retries and reconnection.

    If every attempt fails, the final exception is raised so the bot never
    displays a false success confirmation.
    """
    global google_client
    global spreadsheet
    global records_worksheet
    global employees_worksheet

    last_error: Exception | None = None

    for attempt in range(1, MAX_GOOGLE_RETRIES + 1):
        try:
            ensure_google_sync()
            return operation()

        except Exception as exc:
            last_error = exc

            logger.exception(
                "'%s' failed on attempt %s/%s.",
                operation_name,
                attempt,
                MAX_GOOGLE_RETRIES,
            )

            google_client = None
            spreadsheet = None
            records_worksheet = None
            employees_worksheet = None

            if attempt < MAX_GOOGLE_RETRIES:
                delay = min(2 ** (attempt - 1), 8) + random.uniform(0, 0.5)
                time.sleep(delay)

    assert last_error is not None
    raise last_error


def employee_column(
    configured: str,
    headers: list[list[str]],
    labels: tuple[str, ...],
    default: int,
) -> int:
    if configured:
        if any(letter not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" for letter in configured):
            raise ValueError(f"Invalid Sheets column: {configured}")
        index = 0
        for letter in configured:
            index = index * 26 + ord(letter) - ord("A") + 1
        if index < 2:
            raise ValueError("Employee column A is reserved for the ID.")
        return index - 1

    for label in labels:
        for row in reversed(headers):
            for index, value in enumerate(row[1:], start=1):
                text = normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
                if " ".join(text.casefold().split()) == label:
                    return index
    return default


COUNTRY_TIMEZONES = {
    "venezuela": ("America/Caracas", "Venezuela"),
    "mexico": ("America/Mexico_City", "Central Mexico"),
    "brasil": ("America/Sao_Paulo", "Brazil · Brasília time"),
    "argentina": ("America/Argentina/Buenos_Aires", "Argentina"),
    "republica dominicana": ("America/Santo_Domingo", "Dominican Republic"),
    "colombia": ("America/Bogota", "Colombia"),
    "nigeria": ("Africa/Lagos", "Nigeria"),
    "filipinas": ("Asia/Manila", "Philippines"),
}
NATIONALITY_COUNTRIES = {
    "venezolan": "venezuela",
    "mexican": "mexico",
    "brasilen": "brasil",
    "argentin": "argentina",
    "dominican": "republica dominicana",
    "colombian": "colombia",
    "nigerian": "nigeria",
    "filipin": "filipinas",
}
TIMEZONE_REGIONS = {zone: region for zone, region in COUNTRY_TIMEZONES.values()}


def country_timezone(value: str) -> str:
    text = normalize("NFKD", value).encode("ascii", "ignore").decode().casefold()
    text = " ".join(text.split())
    country = NATIONALITY_COUNTRIES.get(text[:-1], text) if text.endswith(("a", "o")) else text
    zone, _ = COUNTRY_TIMEZONES.get(country, ("", ""))
    return zone


def load_employees_sync() -> dict[str, tuple[str, ZoneInfo | None]]:
    def operation() -> dict[str, tuple[str, ZoneInfo | None]]:
        assert employees_worksheet is not None

        headers = employees_worksheet.get("1:3")
        name_column = employee_column(
            EMPLOYEE_NAME_COLUMN,
            headers,
            (
                "nombre completo", "full name", "employee name",
                "nombre del colaborador", "nombre del prestador",
                "nombre y apellido", "nombres y apellidos",
                "nombre del empleado", "nombre", "name",
            ),
            default=1,
        )
        zone_column = employee_column(
            EMPLOYEE_TIMEZONE_COLUMN,
            headers,
            ("zona horaria", "zona horaria iana", "time zone", "timezone"),
            default=-1,
        )

        country_column = employee_column(
            "",
            headers,
            ("pais de trabajo", "pais de residencia", "pais", "country", "nacionalidad", "nationality"),
            default=-1,
        )

        def cell(row: list[str], index: int) -> str:
            if 0 <= index < len(row):
                return " ".join(str(row[index] or "").split())
            return ""

        employees = {}
        invalid_zone_ids = []
        last_column = gspread.utils.rowcol_to_a1(4, max(name_column, zone_column, country_column) + 1)
        last_column = last_column.rstrip("4")
        for row in employees_worksheet.get(f"A4:{last_column}"):
            employee_id = cell(row, 0)
            if not employee_id:
                continue
            name = cell(row, name_column)
            zone_key = cell(row, zone_column) or country_timezone(cell(row, country_column))
            zone = None
            if zone_key:
                try:
                    zone = ZoneInfo(zone_key)
                except (ZoneInfoNotFoundError, ValueError):
                    invalid_zone_ids.append(employee_id)
            previous_name, previous_zone = employees.get(employee_id, ("", None))
            employees[employee_id] = (previous_name or name, previous_zone or zone)
        if employees and not any(name for name, _ in employees.values()):
            logger.warning("No employee names found. Check EMPLOYEE_NAME_COLUMN.")
        if invalid_zone_ids:
            logger.warning("Invalid time zones for employee IDs: %s", ", ".join(invalid_zone_ids))
        return employees

    return run_google_with_retries_sync("load employees", operation)


def employee_display_name(employee_id: str) -> str:
    name, _ = EMPLOYEES_CACHE.get(employee_id, ("", None))
    return name.title() if name.isupper() else name


def employee_label(employee_id: str) -> str:
    name = employee_display_name(employee_id)
    return f"{employee_id} · {name}" if name else employee_id


def employee_display(employee_id: str) -> str:
    return discord.utils.escape_markdown(employee_label(employee_id))


def sheet_timestamp(instant: datetime, zone: ZoneInfo) -> str:
    local = instant.astimezone(zone)
    return f"{local.isoformat(sep=' ', timespec='seconds')} [{zone.key}]"


def display_registration_time(
    instant: datetime,
    zone: ZoneInfo,
    region: str,
) -> str:
    local = instant.astimezone(zone)
    clock = local.strftime("%I:%M %p").lstrip("0")
    date = f"{local.strftime('%b')} {local.day}, {local.year}"
    offset_minutes = int(local.utcoffset().total_seconds() / 60)
    sign = "−" if offset_minutes < 0 else "+"
    hours, minutes = divmod(abs(offset_minutes), 60)
    offset = f"GMT{sign}{hours}"
    if minutes:
        offset += f":{minutes:02d}"
    return f"**{clock}** · {date}\n{region} ({offset})"


def add_registration_times(
    embed: discord.Embed,
    instant: datetime,
    employee_zone: ZoneInfo | None,
    *,
    private_confirmation: bool = True,
) -> None:
    venezuela_time = display_registration_time(
        instant, VENEZUELA_TIMEZONE, "Caracas, Venezuela"
    )
    if employee_zone:
        region = TIMEZONE_REGIONS.get(employee_zone.key)
        if region is None:
            region = employee_zone.key.rsplit("/", 1)[-1].replace("_", " ")
        local_time = display_registration_time(instant, employee_zone, region)
    elif private_confirmation:
        stamp = int(instant.timestamp())
        local_time = f"**<t:{stamp}:t>** · <t:{stamp}:D>\nLocal GMT unavailable"
    else:
        local_time = "Employee time zone unavailable"

    local_label = "Your local time" if private_confirmation else "Employee local time"
    embed.add_field(
        name="Registration time",
        value=f"**{local_label}**\n{local_time}\n\n**Venezuela time**\n{venezuela_time}",
        inline=False,
    )


def find_open_workday(
    records: list[list[str]],
    employee_id: str,
) -> tuple[int, list[str]] | None:
    """
    Returns the most recent open workday for the selected employee.

    A workday is open when:
    - Column B matches the employee ID.
    - Column C contains a check-in time.
    - Column D does not contain a check-out time.
    """
    for row_number in range(len(records), 1, -1):
        row = records[row_number - 1]
        normalized = row + [""] * max(0, 6 - len(row))

        recorded_employee_id = normalized[1].strip()
        check_in_time = normalized[2].strip()
        check_out_time = normalized[3].strip()

        if (
            recorded_employee_id == employee_id
            and check_in_time
            and not check_out_time
        ):
            return row_number, normalized

    return None


def record_check_in_sync(
    employee_id: str,
    activity: str,
    discord_user: str,
    employee_name: str = "",
    employee_zone: ZoneInfo | None = None,
) -> tuple[datetime, dict | None]:
    """
    Records a new check-in.

    If a previous workday is still open, the new check-in is still recorded.
    The previous row remains unchanged and is returned as an HR incident.
    """
    now = datetime.now(TIMEZONE)

    def operation() -> tuple[datetime, dict | None]:
        assert records_worksheet is not None

        records = records_worksheet.get("A:G")
        open_workday = find_open_workday(records, employee_id)

        incident = None

        if open_workday:
            row_number, row = open_workday

            incident = {
                "employee_id": employee_id,
                "discord_user": discord_user,
                "row_number": row_number,
                "previous_date_raw": row[0] or "date unavailable",
                "previous_time_raw": row[2] or "time unavailable",
                "previous_date": format_us_date(row[0]),
                "previous_time": format_us_time(row[2]),
            }

        sheet_date = now.strftime("%Y-%m-%d")
        sheet_time = now.strftime("%H:%M")

        records_worksheet.append_row(
            [
                sheet_date,
                employee_id,
                sheet_time,
                "",
                activity.strip(),
                discord_user,
                f"'{employee_name}" if employee_name else "",  # G
                sheet_timestamp(now, VENEZUELA_TIMEZONE),  # H
                sheet_timestamp(now, employee_zone) if employee_zone else "",  # I
            ],
            value_input_option="USER_ENTERED",
        )

        return now, incident

    return run_google_with_retries_sync(
        f"record check-in for {employee_id}",
        operation,
    )


def recorded_timestamp(row: list[str], column: int) -> datetime | None:
    if column >= len(row) or not row[column]:
        return None
    try:
        parsed = datetime.fromisoformat(row[column].split(" [", 1)[0].strip())
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def legacy_clock(value: str) -> clock_time | None:
    for pattern in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M:%S %p"):
        try:
            return datetime.strptime(value.strip(), pattern).time()
        except ValueError:
            continue
    return None


def workday_start(row: list[str]) -> datetime | None:
    timestamp = recorded_timestamp(row, 7)
    if timestamp is not None:
        return timestamp
    clock = legacy_clock(row[2])
    for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y"):
        try:
            day = datetime.strptime(row[0].strip(), pattern).date()
        except ValueError:
            continue
        if clock is not None:
            return datetime.combine(day, clock, TIMEZONE).astimezone(timezone.utc)
        break
    return None


def closed_workday_end(row: list[str], start: datetime) -> datetime | None:
    timestamp = recorded_timestamp(row, 9)
    if timestamp is not None:
        return timestamp
    clock = legacy_clock(row[3])
    if clock is None:
        return None
    start_local = start.astimezone(TIMEZONE)
    day = start_local.date()
    if clock < start_local.time().replace(second=0, microsecond=0):
        day += timedelta(days=1)
    return datetime.combine(day, clock, TIMEZONE).astimezone(timezone.utc)


def workday_summary(
    records: list[list[str]],
    employee_id: str,
    row: list[str],
    registered_at: datetime,
) -> tuple[int | None, int | None]:
    start = workday_start(row)
    end = registered_at.astimezone(timezone.utc)
    if start is None or start > end:
        return None, None

    day = registered_at.astimezone(TIMEZONE).date()
    midnight = datetime.combine(day, clock_time.min, TIMEZONE).astimezone(timezone.utc)
    intervals = [(max(start, midnight), end)]
    for previous in records[1:]:
        if len(previous) < 4 or previous[1].strip() != employee_id or not previous[3].strip():
            continue
        previous_start = workday_start(previous)
        if previous_start is None:
            continue
        previous_end = closed_workday_end(previous, previous_start)
        if previous_end is None:
            continue
        clipped_start = max(previous_start, midnight)
        clipped_end = min(previous_end, end)
        if clipped_start < clipped_end:
            intervals.append((clipped_start, clipped_end))

    total_seconds = 0.0
    covered_until = midnight
    for interval_start, interval_end in sorted(intervals):
        if interval_end > covered_until:
            total_seconds += (interval_end - max(interval_start, covered_until)).total_seconds()
            covered_until = interval_end
    return int((end - start).total_seconds() // 60), int(total_seconds // 60)


def record_check_out_sync(
    employee_id: str,
    discord_user: str,
    employee_name: str = "",
    employee_zone: ZoneInfo | None = None,
) -> tuple[bool, dict]:
    del discord_user  # Reserved for future audit features.
    now = datetime.now(TIMEZONE)

    def operation() -> tuple[bool, dict]:
        assert records_worksheet is not None

        records = records_worksheet.get("A:K")
        open_workday = find_open_workday(records, employee_id)

        if not open_workday:
            return False, {
                "employee_id": employee_id,
                "message": (
                    f"No open workday was found for **{employee_id}**. "
                    "Please verify the Employee ID or contact Human Resources."
                ),
            }

        row_number, row = open_workday
        sheet_time = now.strftime("%H:%M")
        duration_minutes, today_minutes = workday_summary(records, employee_id, row, now)

        updates = [
            {"range": f"D{row_number}", "values": [[sheet_time]]},
            {
                "range": f"J{row_number}:K{row_number}",
                "values": [[
                    sheet_timestamp(now, VENEZUELA_TIMEZONE),
                    sheet_timestamp(now, employee_zone) if employee_zone else "",
                ]],
            },
        ]
        if employee_name and (len(row) < 7 or not row[6]):
            updates.append({"range": f"G{row_number}", "values": [[f"'{employee_name}"]]})
        records_worksheet.batch_update(updates, value_input_option="USER_ENTERED")

        return True, {
            "employee_id": employee_id,
            "registered_at": now,
            "check_in_date": format_us_date(row[0]),
            "check_in_time": format_us_time(row[2]),
            "activity": row[4],
            "duration_minutes": duration_minutes,
            "today_minutes": today_minutes,
        }

    return run_google_with_retries_sync(
        f"record check-out for {employee_id}",
        operation,
    )


async def refresh_employee_ids_cache() -> bool:
    global EMPLOYEES_CACHE

    try:
        employees = await asyncio.to_thread(load_employees_sync)

        if not employees:
            logger.warning(
                "Google Sheets responded, but no employee IDs were found "
                "in A4:A."
            )
            return False

        EMPLOYEES_CACHE = employees
        logger.info(
            "Employee ID cache updated: %s IDs loaded.",
            len(EMPLOYEES_CACHE),
        )
        return True

    except Exception:
        logger.exception("The employee ID cache could not be updated.")
        return False


def get_employee_id_group(group_number: int) -> list[str]:
    if group_number == 1:
        return list(EMPLOYEES_CACHE)[:25]

    if group_number == 2:
        return list(EMPLOYEES_CACHE)[25:50]

    return []


# ============================================================
# HUMAN RESOURCES NOTIFICATIONS
# ============================================================

WINGS_NAVY = 0x142743
WINGS_ORANGE = 0xFF8500
STATUS_GREEN = 0x23A559
STATUS_AMBER = 0xE8A317
STATUS_RED = 0xDA373C
# Brand thumbnails are embedded so deployment only needs bot.py.
UNCLE_MAX_ART = (
    "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"
    "F4mlAAAAAElFTkSuQmCC"
)
WNG_LOGO_ART = (
    "/9j/4AAQSkZJRgABAQEAdgB2AAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsK"
    "CwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRT/2wBDAQMEBAUEBQkFBQkUDQsNFBQUFBQU"
    "FBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBT/wAARCACAAIADASIA"
    "AhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQA"
    "AAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3"
    "ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWm"
    "p6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEA"
    "AwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSEx"
    "BhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElK"
    "U1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3"
    "uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD8qqKK"
    "KACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAoooo"
    "AKKKKACiiigAoor1P9nP4NyfGXx/FZTh00KxAudSmQ4Pl54jB7M54HoNx7Vx4zF0cDh54mu7Rirv"
    "+u72XmdeFwtXG14Yegryk7L+vzOs/Zz/AGWbz4shNe12SbS/CqPhDGMTXpB5EefuqDwX59Bk5I+0"
    "/DnwV8B+E7FLXTvCelRooAMk9qs8jf7zuCx/OussbG20uyt7OzgjtbS3jWKGCJdqRoowFA7ACrGe"
    "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"
    "oAKKKKACiiigAooooA//2Q=="
)
PORTAL_FOOTER = "Wings Store • Human Resources"


async def notify_human_resources(
    interaction: discord.Interaction,
    incident: dict,
    registered_at: datetime,
    employee_zone: ZoneInfo | None,
) -> None:
    """
    Sends a workday incident notification to the private HR Discord channel.

    Render environment variable required:
    HR_CHANNEL_ID=NUMERIC_DISCORD_CHANNEL_ID
    """
    if not HR_CHANNEL_ID:
        logger.warning(
            "A workday incident was detected, but HR_CHANNEL_ID is not "
            "configured."
        )
        return

    channel = bot.get_channel(HR_CHANNEL_ID)

    if channel is None:
        try:
            channel = await bot.fetch_channel(HR_CHANNEL_ID)
        except Exception:
            logger.exception(
                "The Human Resources channel with ID %s could not be found.",
                HR_CHANNEL_ID,
            )
            return

    embed = discord.Embed(
        title="Workday Incident",
        description=(
            "A new check-in was recorded while a previous workday "
            "remained open."
        ),
        color=STATUS_AMBER,
        timestamp=registered_at,
    )

    embed.add_field(
        name="Employee",
        value=employee_display(incident["employee_id"])[:1024],
        inline=True,
    )
    embed.add_field(
        name="Discord User",
        value=str(interaction.user),
        inline=True,
    )
    embed.add_field(
        name="Previous Open Workday",
        value=(
            f"{incident['previous_date']}\n"
            f"{incident['previous_time']}"
        ),
        inline=False,
    )
    add_registration_times(
        embed, registered_at, employee_zone, private_confirmation=False
    )
    embed.add_field(
        name="Google Sheets Row",
        value=str(incident["row_number"]),
        inline=True,
    )
    embed.add_field(
        name="Status",
        value="Pending HR Review",
        inline=False,
    )
    embed.set_footer(
        text="Uncle Max • Wings Store Human Resources"
    )

    try:
        await channel.send(embed=embed)
        logger.info(
            "Workday incident sent to Human Resources | Employee=%s | Row=%s",
            incident["employee_id"],
            incident["row_number"],
        )
    except Exception:
        logger.exception(
            "The incident notification could not be sent to Human Resources."
        )


# ============================================================
# CONFIRMATION CARDS
# ============================================================

def registration_notice(title: str, message: str, *, color: int) -> discord.Embed:
    return discord.Embed(title=title, description=message, color=color)


def confirmation_employee(employee_id: str) -> str:
    name = employee_display_name(employee_id)
    safe_id = discord.utils.escape_markdown(employee_id)
    if not name:
        return f"Employee ID · {safe_id}"
    safe_name = discord.utils.escape_markdown(name)
    return f"{safe_name}\nEmployee ID · {safe_id}"


def build_check_in_embed(
    employee_id: str,
    registered_at: datetime,
    incident: dict | None,
    employee_zone: ZoneInfo | None,
) -> discord.Embed:
    embed = discord.Embed(
        title="Check In Confirmed",
        description=confirmation_employee(employee_id),
        color=STATUS_GREEN,
    )

    add_registration_times(embed, registered_at, employee_zone)

    if incident:
        embed.add_field(
            name="⚠️ Previous workday still open",
            value=f"{incident['previous_date']} · {incident['previous_time']}",
            inline=False,
        )

    embed.set_footer(text=PORTAL_FOOTER)

    return embed


def display_work_minutes(minutes: int | None) -> str:
    if minutes is None:
        return "Unavailable"
    hours, remaining = divmod(minutes, 60)
    return f"{hours}h {remaining:02d}m" if hours else f"{remaining}m"


def build_check_out_embed(
    employee_id: str,
    result: dict,
    employee_zone: ZoneInfo | None,
) -> discord.Embed:
    embed = discord.Embed(
        title="Check Out Confirmed",
        description=confirmation_employee(employee_id),
        color=STATUS_GREEN,
    )

    add_registration_times(embed, result["registered_at"], employee_zone)
    embed.add_field(
        name="Original check-in",
        value=f"{result['check_in_date']} · {result['check_in_time']}",
        inline=False,
    )

    activity = discord.utils.escape_markdown(str(result["activity"]).strip())
    embed.add_field(name="Work activity", value=activity[:1024] or "None recorded", inline=False)
    day_label = "Venezuela time" if BOT_TIMEZONE == "America/Caracas" else BOT_TIMEZONE
    embed.add_field(
        name="Work summary",
        value=(
            f"**This workday** · {display_work_minutes(result['duration_minutes'])}\n"
            f"**Today's total ({day_label})** · {display_work_minutes(result['today_minutes'])}"
        ),
        inline=False,
    )
    embed.set_footer(text=PORTAL_FOOTER)

    return embed


# ============================================================
# TODAY'S WORK ACTIVITY MODAL
# ============================================================

class WorkActivityModal(discord.ui.Modal):
    def __init__(self, employee_id: str):
        super().__init__(
            title="Check In · Activity",
            timeout=300,
        )

        self.employee_id = employee_id

        self.activity = discord.ui.TextInput(
            label="Planned activity",
            style=discord.TextStyle.paragraph,
            placeholder=(
                "Briefly describe what you plan to work on."
            ),
            required=True,
            max_length=300,
        )

        self.add_item(self.activity)

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )

        employee_name, employee_zone = EMPLOYEES_CACHE.get(self.employee_id, ("", None))
        try:
            async with SHEETS_LOCK:
                registered_at, incident = await asyncio.to_thread(
                    record_check_in_sync,
                    self.employee_id,
                    str(self.activity.value),
                    str(interaction.user),
                    employee_name,
                    employee_zone,
                )

            await interaction.followup.send(
                embed=build_check_in_embed(
                    self.employee_id,
                    registered_at,
                    incident,
                    employee_zone,
                ),
                ephemeral=True,
            )

            if incident:
                await notify_human_resources(
                    interaction,
                    incident,
                    registered_at,
                    employee_zone,
                )

        except Exception:
            logger.exception(
                "Final check-in error | Employee=%s | User=%s",
                self.employee_id,
                interaction.user,
            )

            await interaction.followup.send(
                embed=registration_notice(
                    "Check In could not be confirmed",
                    "Check whether the entry was saved before trying again. "
                    "If you are unsure, contact Human Resources.",
                    color=STATUS_RED,
                ),
                ephemeral=True,
            )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:
        logger.exception(
            "Unhandled error in WorkActivityModal.",
            exc_info=error,
        )

        notice = registration_notice(
            "Request could not be completed",
            "If you were recording a workday, verify the entry before retrying.",
            color=STATUS_RED,
        )

        if interaction.response.is_done():
            await interaction.followup.send(
                embed=notice,
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                embed=notice,
                ephemeral=True,
            )


# ============================================================
# EMPLOYEE ID SELECT MENUS
# ============================================================

def employee_option_label(employee_id: str) -> str:
    name = employee_display_name(employee_id)
    if not name:
        return employee_id[:100]
    suffix = f" · {employee_id}"
    available = 100 - len(suffix)
    if available <= 0:
        return employee_id[:100]
    if len(name) > available:
        name = name[:available - 1].rstrip() + "…"
    return name + suffix


class CheckInSelect(discord.ui.Select):
    def __init__(self, group_number: int):
        employee_ids = get_employee_id_group(group_number)

        options = [
            discord.SelectOption(
                label=employee_option_label(employee_id),
                value=employee_id,
            )
            for employee_id in employee_ids
        ]

        if group_number == 1:
            placeholder = "Choose an employee · WS-001–WS-027"
            custom_id = "uncle_max:check_in_select:1"
        else:
            placeholder = "Choose an employee · WS-028–WS-050"
            custom_id = "uncle_max:check_in_select:2"

        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            custom_id=custom_id,
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await interaction.response.send_modal(
            WorkActivityModal(self.values[0])
        )


class CheckOutSelect(discord.ui.Select):
    def __init__(self, group_number: int):
        employee_ids = get_employee_id_group(group_number)

        options = [
            discord.SelectOption(
                label=employee_option_label(employee_id),
                value=employee_id,
            )
            for employee_id in employee_ids
        ]

        if group_number == 1:
            placeholder = "Choose an employee · WS-001–WS-027"
            custom_id = "uncle_max:check_out_select:1"
        else:
            placeholder = "Choose an employee · WS-028–WS-050"
            custom_id = "uncle_max:check_out_select:2"

        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            custom_id=custom_id,
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )

        employee_id = self.values[0]
        employee_name, employee_zone = EMPLOYEES_CACHE.get(employee_id, ("", None))

        try:
            async with SHEETS_LOCK:
                success, result = await asyncio.to_thread(
                    record_check_out_sync,
                    employee_id,
                    str(interaction.user),
                    employee_name,
                    employee_zone,
                )

            if success:
                await interaction.followup.send(
                    embed=build_check_out_embed(employee_id, result, employee_zone),
                    ephemeral=True,
                )
            else:
                display_name = employee_display(employee_id)[:200]
                message = result["message"].replace(f"**{employee_id}**", f"**{display_name}**")
                await interaction.followup.send(
                    embed=registration_notice(
                        "Check Out unavailable", message, color=STATUS_AMBER
                    ),
                    ephemeral=True,
                )

        except Exception:
            logger.exception(
                "Final check-out error | Employee=%s | User=%s",
                employee_id,
                interaction.user,
            )

            await interaction.followup.send(
                embed=registration_notice(
                    "Check Out could not be confirmed",
                    "Check whether the entry was saved before trying again. "
                    "If you are unsure, contact Human Resources.",
                    color=STATUS_RED,
                ),
                ephemeral=True,
            )


class CheckInSelectView(discord.ui.View):
    def __init__(self, group_number: int):
        super().__init__(timeout=180)
        self.add_item(CheckInSelect(group_number))


class CheckOutSelectView(discord.ui.View):
    def __init__(self, group_number: int):
        super().__init__(timeout=180)
        self.add_item(CheckOutSelect(group_number))


# ============================================================
# PERSISTENT WORKDAY PORTAL
# ============================================================

class WorkdayPortalView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def ensure_employee_ids(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if EMPLOYEES_CACHE:
            return True

        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )

        updated = await refresh_employee_ids_cache()

        if not updated:
            await interaction.followup.send(
                embed=registration_notice(
                    "Employee list unavailable",
                    "Please try again in a few seconds.",
                    color=STATUS_AMBER,
                ),
                ephemeral=True,
            )
            return False

        return True

    async def send_check_in_menu(
        self,
        interaction: discord.Interaction,
        group_number: int,
    ) -> None:
        if not EMPLOYEES_CACHE:
            if not await self.ensure_employee_ids(interaction):
                return

            send_response = interaction.followup.send
        else:
            send_response = interaction.response.send_message

        employee_ids = get_employee_id_group(group_number)

        if not employee_ids:
            await send_response(
                embed=registration_notice(
                    "No employees in this range",
                    "Check your employee ID and choose the matching range.",
                    color=STATUS_AMBER,
                ),
                ephemeral=True,
            )
            return

        await send_response(
            "**Check In** · Select your name.",
            view=CheckInSelectView(group_number),
            ephemeral=True,
        )

    async def send_check_out_menu(
        self,
        interaction: discord.Interaction,
        group_number: int,
    ) -> None:
        if not EMPLOYEES_CACHE:
            if not await self.ensure_employee_ids(interaction):
                return

            send_response = interaction.followup.send
        else:
            send_response = interaction.response.send_message

        employee_ids = get_employee_id_group(group_number)

        if not employee_ids:
            await send_response(
                embed=registration_notice(
                    "No employees in this range",
                    "Check your employee ID and choose the matching range.",
                    color=STATUS_AMBER,
                ),
                ephemeral=True,
            )
            return

        await send_response(
            "**Check Out** · Select your name.",
            view=CheckOutSelectView(group_number),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Check In · 001–027",
        style=discord.ButtonStyle.success,
        custom_id="uncle_max:portal:check_in:1",
        row=0,
    )
    async def check_in_group_one(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self.send_check_in_menu(interaction, 1)

    @discord.ui.button(
        label="Check In · 028–050",
        style=discord.ButtonStyle.success,
        custom_id="uncle_max:portal:check_in:2",
        row=0,
    )
    async def check_in_group_two(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self.send_check_in_menu(interaction, 2)

    @discord.ui.button(
        label="Check Out · 001–027",
        style=discord.ButtonStyle.danger,
        custom_id="uncle_max:portal:check_out:1",
        row=1,
    )
    async def check_out_group_one(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self.send_check_out_menu(interaction, 1)

    @discord.ui.button(
        label="Check Out · 028–050",
        style=discord.ButtonStyle.danger,
        custom_id="uncle_max:portal:check_out:2",
        row=1,
    )
    async def check_out_group_two(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self.send_check_out_menu(interaction, 2)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        logger.exception(
            "Error in WorkdayPortalView | Item=%s",
            item,
            exc_info=error,
        )

        notice = registration_notice(
            "Request could not be completed",
            "If you were recording a workday, verify the entry before retrying.",
            color=STATUS_RED,
        )

        if interaction.response.is_done():
            await interaction.followup.send(
                embed=notice,
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                embed=notice,
                ephemeral=True,
            )


# ============================================================
# BOT
# ============================================================

intents = discord.Intents.default()
intents.message_content = True


class UncleMaxBot(commands.Bot):
    async def setup_hook(self) -> None:
        self.add_view(WorkdayPortalView())

        await refresh_employee_ids_cache()

        if not refresh_ids_task.is_running():
            refresh_ids_task.start()


bot = UncleMaxBot(
    command_prefix="!",
    intents=intents,
    help_command=None,
)


@tasks.loop(minutes=IDS_REFRESH_MINUTES)
async def refresh_ids_task() -> None:
    await refresh_employee_ids_cache()


@refresh_ids_task.before_loop
async def before_refresh_ids_task() -> None:
    await bot.wait_until_ready()


@bot.event
async def on_ready() -> None:
    logger.info(
        "Uncle Max connected as %s | ID=%s",
        bot.user,
        bot.user.id if bot.user else "unknown",
    )


@bot.event
async def on_disconnect() -> None:
    logger.warning(
        "Uncle Max temporarily lost connection to Discord."
    )


@bot.event
async def on_resumed() -> None:
    logger.info(
        "Uncle Max resumed the Discord session."
    )


@bot.command(name="panel")
@commands.guild_only()
async def panel(ctx: commands.Context) -> None:
    controls = WorkdayPortalView()
    check_in = discord.ui.ActionRow()
    check_out = discord.ui.ActionRow()

    for button in list(controls.children):
        row = check_in if button.row == 0 else check_out
        controls.remove_item(button)
        row.add_item(button)

    mascot_name = "uncle-max.png"
    logo_name = "wng-logo.jpg"
    header = discord.ui.Section(
        discord.ui.TextDisplay("## Wings Store\n-# TIME & ATTENDANCE"),
        discord.ui.TextDisplay(
            "Welcome! I'm Uncle Max. Choose your action and employee ID range."
        ),
        accessory=discord.ui.Thumbnail(
            f"attachment://{mascot_name}",
            description="Uncle Max, the Wings Store eagle mascot",
        ),
    )
    attendance = discord.ui.Container(
        header,
        discord.ui.Separator(),
        check_in,
        check_out,
        accent_color=WINGS_NAVY,
    )
    champions = discord.ui.Container(
        discord.ui.Section(
            discord.ui.TextDisplay(
                "-# NOVEMBER PLAYOFFS\n"
                "### Play Us League Champions\n"
                "**Arturo Pérez** · **Renny Ramírez**\n"
                "**Gabriel Pérez** · **Osward Pacheco**"
            ),
            accessory=discord.ui.Thumbnail(
                f"attachment://{logo_name}",
                description="Wings Store's glowing orange WNG logo",
            ),
        ),
        accent_color=WINGS_ORANGE,
    )
    layout = discord.ui.LayoutView(timeout=None)
    layout.add_item(attendance)
    layout.add_item(champions)
    mascot = discord.File(BytesIO(b64decode(UNCLE_MAX_ART)), filename=mascot_name)
    logo = discord.File(BytesIO(b64decode(WNG_LOGO_ART)), filename=logo_name)
    await ctx.send(view=layout, files=[mascot, logo])


@panel.error
async def panel_error(
    ctx: commands.Context,
    error: commands.CommandError,
) -> None:
    if isinstance(error, commands.NoPrivateMessage):
        await ctx.send(
            "This command can only be used inside the Wings Store server."
        )
        return

    logger.exception(
        "Error while running !panel.",
        exc_info=error,
    )

    await ctx.send(
        "We couldn't create the Workday Portal. "
        "Please review the Render logs."
    )


# ============================================================
# STARTUP
# ============================================================

if __name__ == "__main__":
    bot.run(
        DISCORD_TOKEN,
        log_handler=None,
    )