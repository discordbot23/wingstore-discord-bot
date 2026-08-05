import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime
from typing import Callable, TypeVar
from zoneinfo import ZoneInfo

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

EMPLOYEE_IDS_CACHE: list[str] = []

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


def load_employee_ids_sync() -> list[str]:
    def operation() -> list[str]:
        assert employees_worksheet is not None

        data = employees_worksheet.get("A4:A")

        return list(
            dict.fromkeys(
                str(row[0]).strip()
                for row in data
                if row and str(row[0]).strip()
            )
        )

    return run_google_with_retries_sync(
        "load employee IDs",
        operation,
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
) -> tuple[str, dict | None]:
    """
    Records a new check-in.

    If a previous workday is still open, the new check-in is still recorded.
    The previous row remains unchanged and is returned as an HR incident.
    """
    def operation() -> tuple[str, dict | None]:
        assert records_worksheet is not None

        records = records_worksheet.get("A:F")
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

        now = datetime.now(TIMEZONE)
        sheet_date = now.strftime("%Y-%m-%d")
        sheet_time = now.strftime("%H:%M")
        display_time = now.strftime("%I:%M %p").lstrip("0")

        records_worksheet.append_row(
            [
                sheet_date,
                employee_id,
                sheet_time,
                "",
                activity.strip(),
                discord_user,
            ],
            value_input_option="USER_ENTERED",
        )

        return display_time, incident

    return run_google_with_retries_sync(
        f"record check-in for {employee_id}",
        operation,
    )


def record_check_out_sync(
    employee_id: str,
    discord_user: str,
) -> tuple[bool, dict]:
    del discord_user  # Reserved for future audit features.

    def operation() -> tuple[bool, dict]:
        assert records_worksheet is not None

        records = records_worksheet.get("A:F")
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
        now = datetime.now(TIMEZONE)
        sheet_time = now.strftime("%H:%M")
        display_time = now.strftime("%I:%M %p").lstrip("0")

        records_worksheet.update(
            range_name=f"D{row_number}",
            values=[[sheet_time]],
            value_input_option="USER_ENTERED",
        )

        return True, {
            "employee_id": employee_id,
            "check_out_time": display_time,
            "check_in_date": format_us_date(row[0]),
            "check_in_time": format_us_time(row[2]),
        }

    return run_google_with_retries_sync(
        f"record check-out for {employee_id}",
        operation,
    )


async def refresh_employee_ids_cache() -> bool:
    global EMPLOYEE_IDS_CACHE

    try:
        ids = await asyncio.to_thread(load_employee_ids_sync)

        if not ids:
            logger.warning(
                "Google Sheets responded, but no employee IDs were found "
                "in A4:A."
            )
            return False

        EMPLOYEE_IDS_CACHE = ids
        logger.info(
            "Employee ID cache updated: %s IDs loaded.",
            len(EMPLOYEE_IDS_CACHE),
        )
        return True

    except Exception:
        logger.exception("The employee ID cache could not be updated.")
        return False


def get_employee_id_group(group_number: int) -> list[str]:
    if group_number == 1:
        return EMPLOYEE_IDS_CACHE[:25]

    if group_number == 2:
        return EMPLOYEE_IDS_CACHE[25:50]

    return []


# ============================================================
# HUMAN RESOURCES NOTIFICATIONS
# ============================================================

async def notify_human_resources(
    interaction: discord.Interaction,
    incident: dict,
    new_check_in_time: str,
) -> None:
    """
    Sends a workday incident notification to the private HR Discord channel.

    Railway variable required:
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

    today_date, _ = current_us_date_time()

    embed = discord.Embed(
        title="🚨 Workday Incident",
        description=(
            "A new check-in was recorded while a previous workday "
            "remained open."
        ),
        color=0xF59E0B,
        timestamp=datetime.now(TIMEZONE),
    )

    embed.add_field(
        name="Employee ID",
        value=incident["employee_id"],
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
    embed.add_field(
        name="New Check-In",
        value=f"{today_date}\n{new_check_in_time}",
        inline=True,
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
# TODAY'S WORK ACTIVITY MODAL
# ============================================================

class WorkActivityModal(discord.ui.Modal):
    def __init__(self, employee_id: str):
        super().__init__(
            title="TODAY'S WORK ACTIVITY",
            timeout=300,
        )

        self.employee_id = employee_id

        self.activity = discord.ui.TextInput(
            label="What will you be working on today?",
            style=discord.TextStyle.paragraph,
            placeholder=(
                "Example: Customer Support, Graphic Design, Recruitment..."
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

        try:
            async with SHEETS_LOCK:
                check_in_time, incident = await asyncio.to_thread(
                    record_check_in_sync,
                    self.employee_id,
                    str(self.activity.value),
                    str(interaction.user),
                )

            if incident:
                await interaction.followup.send(
                    "✅ **You're all set!**\n\n"
                    "Your new workday has started successfully.\n"
                    f"**Check-In Time:** {check_in_time}\n\n"
                    "⚠️ **Open Workday Notice**\n"
                    "We noticed that your previous check-in from "
                    f"**{incident['previous_date']} at "
                    f"{incident['previous_time']}** does not have a "
                    "corresponding check-out.\n\n"
                    "You may continue working as usual. Human Resources "
                    "has already been notified and will review the incident.",
                    ephemeral=True,
                )

                await notify_human_resources(
                    interaction,
                    incident,
                    check_in_time,
                )

            else:
                await interaction.followup.send(
                    "✅ **You're all set!**\n\n"
                    "Your workday has started successfully.\n"
                    f"**Check-In Time:** {check_in_time}\n\n"
                    "Have an amazing day, and thank you for being part "
                    "of Wings Store!",
                    ephemeral=True,
                )

        except Exception:
            logger.exception(
                "Final check-in error | Employee=%s | User=%s",
                self.employee_id,
                interaction.user,
            )

            await interaction.followup.send(
                "❌ **We couldn't complete your check-in.**\n\n"
                "Please try again in a few moments. If the issue continues, "
                "contact the Human Resources Department.",
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

        message = (
            "❌ An unexpected error occurred. Please try again or contact "
            "the Human Resources Department."
        )

        if interaction.response.is_done():
            await interaction.followup.send(
                message,
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                message,
                ephemeral=True,
            )


# ============================================================
# EMPLOYEE ID SELECT MENUS
# ============================================================

class CheckInSelect(discord.ui.Select):
    def __init__(self, group_number: int):
        employee_ids = get_employee_id_group(group_number)

        options = [
            discord.SelectOption(
                label=employee_id,
                value=employee_id,
            )
            for employee_id in employee_ids
        ]

        if group_number == 1:
            placeholder = "Select your Employee ID (WS-001–WS-027)"
            custom_id = "uncle_max:check_in_select:1"
        else:
            placeholder = "Select your Employee ID (WS-028–WS-050)"
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
                label=employee_id,
                value=employee_id,
            )
            for employee_id in employee_ids
        ]

        if group_number == 1:
            placeholder = "Select your Employee ID (WS-001–WS-027)"
            custom_id = "uncle_max:check_out_select:1"
        else:
            placeholder = "Select your Employee ID (WS-028–WS-050)"
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

        try:
            async with SHEETS_LOCK:
                success, result = await asyncio.to_thread(
                    record_check_out_sync,
                    employee_id,
                    str(interaction.user),
                )

            if success:
                await interaction.followup.send(
                    "✅ **Workday completed!**\n\n"
                    "Your check-out has been successfully recorded.\n"
                    f"**Check-Out Time:** {result['check_out_time']}\n"
                    "**Original Check-In:** "
                    f"{result['check_in_date']} at "
                    f"{result['check_in_time']}\n\n"
                    "Thank you for your hard work today. Have a wonderful "
                    "rest of your day!",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "⚠️ **Unable to record your check-out.**\n\n"
                    f"{result['message']}",
                    ephemeral=True,
                )

        except Exception:
            logger.exception(
                "Final check-out error | Employee=%s | User=%s",
                employee_id,
                interaction.user,
            )

            await interaction.followup.send(
                "❌ **We couldn't complete your check-out.**\n\n"
                "Please try again in a few moments. If the issue continues, "
                "contact the Human Resources Department.",
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
        if EMPLOYEE_IDS_CACHE:
            return True

        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )

        updated = await refresh_employee_ids_cache()

        if not updated:
            await interaction.followup.send(
                "❌ We couldn't load the Employee ID list right now. "
                "Please try again in a few seconds.",
                ephemeral=True,
            )
            return False

        return True

    async def send_check_in_menu(
        self,
        interaction: discord.Interaction,
        group_number: int,
    ) -> None:
        if not EMPLOYEE_IDS_CACHE:
            if not await self.ensure_employee_ids(interaction):
                return

            send_response = interaction.followup.send
        else:
            send_response = interaction.response.send_message

        employee_ids = get_employee_id_group(group_number)

        if not employee_ids:
            await send_response(
                "⚠️ No Employee IDs are currently available in this group.",
                ephemeral=True,
            )
            return

        await send_response(
            "**Select your Employee ID:**",
            view=CheckInSelectView(group_number),
            ephemeral=True,
        )

    async def send_check_out_menu(
        self,
        interaction: discord.Interaction,
        group_number: int,
    ) -> None:
        if not EMPLOYEE_IDS_CACHE:
            if not await self.ensure_employee_ids(interaction):
                return

            send_response = interaction.followup.send
        else:
            send_response = interaction.response.send_message

        employee_ids = get_employee_id_group(group_number)

        if not employee_ids:
            await send_response(
                "⚠️ No Employee IDs are currently available in this group.",
                ephemeral=True,
            )
            return

        await send_response(
            "**Select your Employee ID:**",
            view=CheckOutSelectView(group_number),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Check In (WS-001–027)",
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
        label="Check In (WS-028–050)",
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
        label="Check Out (WS-001–027)",
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
        label="Check Out (WS-028–050)",
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

        message = (
            "❌ We couldn't process that request. Please try again."
        )

        if interaction.response.is_done():
            await interaction.followup.send(
                message,
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                message,
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
    embed = discord.Embed(
        title="WINGS STORE WORKDAY PORTAL",
        description=(
            "Welcome to the official Wings Store workday system.\n"
            "Use the buttons below to check in or check out."
        ),
        color=0x5865F2,
    )

    embed.add_field(
        name="👋 Welcome! I'm Uncle Max",
        value=(
            "I'm your virtual Workday Assistant, here to help you manage "
            "your daily check-ins and check-outs.\n\n"
            "Please select an option below to get started."
        ),
        inline=False,
    )

    embed.add_field(
        name="🏆 AREA OF THE MONTH",
        value=(
            "**To be announced**\n"
            "Congratulations to the team recognized for outstanding "
            "performance this month!"
        ),
        inline=False,
    )

    embed.add_field(
        name="🟢 Check In",
        value=(
            "Start your workday using your assigned Employee ID."
        ),
        inline=False,
    )

    embed.add_field(
        name="🔴 Check Out",
        value=(
            "Complete your workday using your assigned Employee ID."
        ),
        inline=False,
    )

    embed.set_footer(
        text="Powered by Uncle Max • Wings Store Human Resources"
    )

    await ctx.send(
        embed=embed,
        view=WorkdayPortalView(),
    )


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
        "❌ We couldn't create the Workday Portal. "
        "Please review the Railway logs."
    )


# ============================================================
# STARTUP
# ============================================================

if __name__ == "__main__":
    bot.run(
        DISCORD_TOKEN,
        log_handler=None,
    )