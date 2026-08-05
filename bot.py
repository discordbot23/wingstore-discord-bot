import asyncio
import json
import logging
import os
import random
from datetime import datetime
from typing import Callable, TypeVar
from zoneinfo import ZoneInfo

import discord
import gspread
from discord.ext import commands, tasks
from google.oauth2.service_account import Credentials


# ============================================================
# CONFIGURACIÓN
# ============================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GOOGLE_CREDENTIALS_RAW = os.getenv("GOOGLE_CREDENTIALS")

SPREADSHEET_NAME = os.getenv(
    "SPREADSHEET_NAME",
    "Wingstore People Operations Master",
)
REGISTRO_SHEET_NAME = os.getenv(
    "REGISTRO_SHEET_NAME",
    "Respuestas de formulario 1",
)
EMPLEADOS_SHEET_NAME = os.getenv(
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
    raise RuntimeError("Falta la variable de entorno DISCORD_TOKEN.")

if not GOOGLE_CREDENTIALS_RAW:
    raise RuntimeError("Falta la variable de entorno GOOGLE_CREDENTIALS.")

try:
    SERVICE_ACCOUNT_INFO = json.loads(GOOGLE_CREDENTIALS_RAW)
except json.JSONDecodeError as exc:
    raise RuntimeError(
        "GOOGLE_CREDENTIALS no contiene un JSON válido."
    ) from exc

try:
    TIMEZONE = ZoneInfo(BOT_TIMEZONE)
except Exception as exc:
    raise RuntimeError(
        f"La zona horaria BOT_TIMEZONE no es válida: {BOT_TIMEZONE}"
    ) from exc


# ============================================================
# LOGS
# ============================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("tio_max")


# ============================================================
# GOOGLE SHEETS
# ============================================================

google_client: gspread.Client | None = None
spreadsheet: gspread.Spreadsheet | None = None
sheet_registro: gspread.Worksheet | None = None
sheet_empleados: gspread.Worksheet | None = None

IDS_CACHE: list[str] = []

# Serializa las operaciones administrativas sobre la hoja.
# Evita que dos interacciones del mismo proceso modifiquen la hoja a la vez.
SHEETS_ASYNC_LOCK = asyncio.Lock()

T = TypeVar("T")


def conectar_google_sync() -> None:
    """Crea nuevamente la conexión y obtiene las hojas necesarias."""
    global google_client, spreadsheet, sheet_registro, sheet_empleados

    credentials = Credentials.from_service_account_info(
        SERVICE_ACCOUNT_INFO,
        scopes=SCOPES,
    )

    google_client = gspread.authorize(credentials)
    spreadsheet = google_client.open(SPREADSHEET_NAME)
    sheet_registro = spreadsheet.worksheet(REGISTRO_SHEET_NAME)
    sheet_empleados = spreadsheet.worksheet(EMPLEADOS_SHEET_NAME)

    logger.info("Conexión con Google Sheets establecida.")


def asegurar_google_sync() -> None:
    if sheet_registro is None or sheet_empleados is None:
        conectar_google_sync()


def ejecutar_google_con_reintentos_sync(
    operation_name: str,
    operation: Callable[[], T],
) -> T:
    """
    Ejecuta una operación de Google con reintentos y reconexión.

    Tras agotar los intentos, vuelve a lanzar el último error para que
    Discord muestre un mensaje real de fallo en vez de confirmar algo
    que no quedó registrado.
    """
    global google_client, spreadsheet, sheet_registro, sheet_empleados

    last_error: Exception | None = None

    for attempt in range(1, MAX_GOOGLE_RETRIES + 1):
        try:
            asegurar_google_sync()
            return operation()

        except Exception as exc:
            last_error = exc

            logger.exception(
                "Falló '%s' en el intento %s/%s.",
                operation_name,
                attempt,
                MAX_GOOGLE_RETRIES,
            )

            # Fuerza una conexión nueva en el siguiente intento.
            google_client = None
            spreadsheet = None
            sheet_registro = None
            sheet_empleados = None

            if attempt < MAX_GOOGLE_RETRIES:
                # Espera exponencial con una pequeña variación.
                delay = min(2 ** (attempt - 1), 8) + random.uniform(0, 0.5)
                import time
                time.sleep(delay)

    assert last_error is not None
    raise last_error


def cargar_ids_sync() -> list[str]:
    def operation() -> list[str]:
        assert sheet_empleados is not None

        data = sheet_empleados.get("A4:A")

        # Elimina vacíos y duplicados conservando el orden.
        ids = list(
            dict.fromkeys(
                str(row[0]).strip()
                for row in data
                if row and str(row[0]).strip()
            )
        )

        return ids

    return ejecutar_google_con_reintentos_sync(
        "cargar IDs de empleados",
        operation,
    )


def obtener_registros_sync() -> list[list[str]]:
    def operation() -> list[list[str]]:
        assert sheet_registro is not None
        return sheet_registro.get("A:F")

    return ejecutar_google_con_reintentos_sync(
        "consultar registros",
        operation,
    )


def buscar_entrada_abierta(
    registros: list[list[str]],
    id_emp: str,
) -> tuple[int, list[str]] | None:
    """
    Devuelve (número_de_fila, fila) de la entrada abierta más reciente.

    Se considera abierta cuando:
    - Columna B = ID solicitado.
    - Columna C contiene hora de entrada.
    - Columna D está vacía.
    """
    for row_number in range(len(registros), 1, -1):
        row = registros[row_number - 1]
        normalized = row + [""] * max(0, 6 - len(row))

        employee_id = normalized[1].strip()
        entry_time = normalized[2].strip()
        exit_time = normalized[3].strip()

        if employee_id == id_emp and entry_time and not exit_time:
            return row_number, normalized

    return None


def registrar_entrada_sync(
    id_emp: str,
    actividad: str,
    usuario: str,
) -> tuple[bool, str, dict | None]:
    """
    Registra una nueva entrada incluso si existe una jornada anterior abierta.

    Cuando detecta una jornada sin salida:
    - conserva el registro anterior sin modificarlo;
    - registra normalmente la nueva entrada;
    - devuelve los datos de la incidencia para advertir al colaborador
      y notificar al canal de Recursos Humanos.
    """
    def operation() -> tuple[bool, str, dict | None]:
        assert sheet_registro is not None

        registros = sheet_registro.get("A:F")
        abierta = buscar_entrada_abierta(registros, id_emp)

        incidencia = None

        if abierta:
            row_number, row = abierta
            incidencia = {
                "fila": row_number,
                "fecha": row[0] or "fecha no disponible",
                "hora": row[2] or "hora no disponible",
                "id_emp": id_emp,
                "usuario": usuario,
            }

        ahora = datetime.now(TIMEZONE)
        fecha = ahora.strftime("%Y-%m-%d")
        hora = ahora.strftime("%H:%M")

        sheet_registro.append_row(
            [fecha, id_emp, hora, "", actividad.strip(), usuario],
            value_input_option="USER_ENTERED",
        )

        return True, f"Entrada registrada a las {hora}.", incidencia

    return ejecutar_google_con_reintentos_sync(
        f"registrar entrada de {id_emp}",
        operation,
    )


def registrar_salida_sync(
    id_emp: str,
    usuario: str,
) -> tuple[bool, str]:
    del usuario  # Reservado para futuras auditorías.

    def operation() -> tuple[bool, str]:
        assert sheet_registro is not None

        registros = sheet_registro.get("A:F")
        abierta = buscar_entrada_abierta(registros, id_emp)

        if not abierta:
            return (
                False,
                f"No encontré una jornada abierta para {id_emp}.",
            )

        row_number, row = abierta
        ahora = datetime.now(TIMEZONE)
        hora = ahora.strftime("%H:%M")

        sheet_registro.update(
            range_name=f"D{row_number}",
            values=[[hora]],
            value_input_option="USER_ENTERED",
        )

        fecha_entrada = row[0] or "fecha no disponible"
        hora_entrada = row[2] or "hora no disponible"

        return (
            True,
            "Salida registrada a las "
            f"{hora}. Entrada original: {fecha_entrada} {hora_entrada}.",
        )

    return ejecutar_google_con_reintentos_sync(
        f"registrar salida de {id_emp}",
        operation,
    )


async def actualizar_cache_ids() -> bool:
    global IDS_CACHE

    try:
        ids = await asyncio.to_thread(cargar_ids_sync)

        if not ids:
            logger.warning(
                "Google Sheets respondió, pero no devolvió IDs en A4:A."
            )
            return False

        IDS_CACHE = ids
        logger.info("Caché actualizada: %s IDs cargados.", len(IDS_CACHE))
        return True

    except Exception:
        logger.exception("No fue posible actualizar la caché de IDs.")
        return False


def obtener_grupo_ids(group_number: int) -> list[str]:
    if group_number == 1:
        return IDS_CACHE[:25]

    if group_number == 2:
        return IDS_CACHE[25:50]

    return []


async def notificar_incidencia_rrhh(
    interaction: discord.Interaction,
    incidencia: dict,
) -> None:
    """
    Envía la incidencia al canal privado de Recursos Humanos.

    Configure en Railway:
    HR_CHANNEL_ID=ID_NUMERICO_DEL_CANAL
    """
    if not HR_CHANNEL_ID:
        logger.warning(
            "Se detectó una incidencia, pero HR_CHANNEL_ID no está configurado."
        )
        return

    channel = bot.get_channel(HR_CHANNEL_ID)

    if channel is None:
        try:
            channel = await bot.fetch_channel(HR_CHANNEL_ID)
        except Exception:
            logger.exception(
                "No fue posible localizar el canal de RRHH con ID %s.",
                HR_CHANNEL_ID,
            )
            return

    embed = discord.Embed(
        title="🚨 Incidencia de jornada detectada",
        description=(
            "Se registró una nueva entrada aunque existía una "
            "jornada anterior sin salida."
        ),
        color=0xF59E0B,
        timestamp=datetime.now(TIMEZONE),
    )

    embed.add_field(
        name="Empleado",
        value=incidencia["id_emp"],
        inline=True,
    )
    embed.add_field(
        name="Usuario de Discord",
        value=str(interaction.user),
        inline=True,
    )
    embed.add_field(
        name="Entrada anterior sin cerrar",
        value=f'{incidencia["fecha"]} a las {incidencia["hora"]}',
        inline=False,
    )
    embed.add_field(
        name="Fila en Google Sheets",
        value=str(incidencia["fila"]),
        inline=True,
    )
    embed.add_field(
        name="Nueva entrada",
        value=datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M"),
        inline=True,
    )
    embed.set_footer(
        text="Tío Max • Wings Store Human Resources"
    )

    try:
        await channel.send(embed=embed)
        logger.info(
            "Incidencia enviada a RRHH | ID=%s | Fila=%s",
            incidencia["id_emp"],
            incidencia["fila"],
        )
    except Exception:
        logger.exception(
            "No fue posible enviar la incidencia al canal de RRHH."
        )


# ============================================================
# MODAL DE ACTIVIDAD
# ============================================================

class ActividadModal(discord.ui.Modal):
    def __init__(self, id_emp: str):
        super().__init__(
            title="REGISTRE LA ACTIVIDAD DE HOY",
            timeout=300,
        )

        self.id_emp = id_emp

        self.actividad = discord.ui.TextInput(
            label="Describe la actividad de hoy",
            style=discord.TextStyle.paragraph,
            placeholder=(
                "Ej.: Diseño de publicaciones, programación, "
                "atención a clientes..."
            ),
            required=True,
            max_length=300,
        )

        self.add_item(self.actividad)

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ) -> None:
        # Confirma inmediatamente a Discord que la operación está en proceso.
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            async with SHEETS_ASYNC_LOCK:
                success, detail, incidencia = await asyncio.to_thread(
                    registrar_entrada_sync,
                    self.id_emp,
                    str(self.actividad.value),
                    str(interaction.user),
                )

            if success and incidencia:
                await interaction.followup.send(
                    "✅ **Su nueva entrada fue registrada correctamente.**\n"
                    f"**{detail}**\n\n"
                    "⚠️ **Advertencia de jornada:** se detectó que la "
                    f"entrada del **{incidencia['fecha']} a las "
                    f"{incidencia['hora']}** no cuenta con una salida "
                    "registrada.\n\n"
                    "Puede continuar con normalidad. Recursos Humanos "
                    "fue notificado para revisar la incidencia.",
                    ephemeral=True,
                )

                await notificar_incidencia_rrhh(
                    interaction,
                    incidencia,
                )

            elif success:
                await interaction.followup.send(
                    "✅🪽 ¡Hola! Soy el Tío Max y ya registré su "
                    "entrada correctamente.\n"
                    f"**{detail}**\n"
                    "¡Te deseo una excelente jornada y mucho éxito!",
                    ephemeral=True,
                )

        except Exception:
            logger.exception(
                "Error definitivo registrando entrada | ID=%s | Usuario=%s",
                self.id_emp,
                interaction.user,
            )

            await interaction.followup.send(
                "❌ No pude registrar su entrada después de varios intentos. "
                "No se mostró una confirmación falsa. Inténtelo nuevamente "
                "en un momento o comuníquese con Recursos Humanos.",
                ephemeral=True,
            )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:
        logger.exception(
            "Error no controlado en ActividadModal.",
            exc_info=error,
        )

        message = (
            "❌ Ocurrió un error inesperado. Inténtelo nuevamente o "
            "comuníquese con Recursos Humanos."
        )

        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(
                message,
                ephemeral=True,
            )


# ============================================================
# SELECTORES
# ============================================================

class EntradaSelect(discord.ui.Select):
    def __init__(self, group_number: int):
        ids = obtener_grupo_ids(group_number)

        options = [
            discord.SelectOption(label=employee_id, value=employee_id)
            for employee_id in ids
        ]

        if group_number == 1:
            placeholder = "Seleccione su ID (grupo 1)"
            custom_id = "tio_max:entrada_select:1"
        else:
            placeholder = "Seleccione su ID (grupo 2)"
            custom_id = "tio_max:entrada_select:2"

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
            ActividadModal(self.values[0])
        )


class SalidaSelect(discord.ui.Select):
    def __init__(self, group_number: int):
        ids = obtener_grupo_ids(group_number)

        options = [
            discord.SelectOption(label=employee_id, value=employee_id)
            for employee_id in ids
        ]

        if group_number == 1:
            placeholder = "Seleccione su ID (grupo 1)"
            custom_id = "tio_max:salida_select:1"
        else:
            placeholder = "Seleccione su ID (grupo 2)"
            custom_id = "tio_max:salida_select:2"

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
        await interaction.response.defer(ephemeral=True, thinking=True)

        id_emp = self.values[0]

        try:
            async with SHEETS_ASYNC_LOCK:
                success, detail = await asyncio.to_thread(
                    registrar_salida_sync,
                    id_emp,
                    str(interaction.user),
                )

            if success:
                await interaction.followup.send(
                    "✅🪽 ¡Hola! Soy el Tío Max y ya registré su "
                    "salida correctamente.\n"
                    f"**{detail}**\n"
                    "Gracias por acompañarnos el día de hoy. "
                    "¡Que tengas un excelente descanso!",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"⚠️ **No se registró la salida.**\n{detail}",
                    ephemeral=True,
                )

        except Exception:
            logger.exception(
                "Error definitivo registrando salida | ID=%s | Usuario=%s",
                id_emp,
                interaction.user,
            )

            await interaction.followup.send(
                "❌ No pude registrar su salida después de varios intentos. "
                "No se mostró una confirmación falsa. Inténtelo nuevamente "
                "en un momento o comuníquese con Recursos Humanos.",
                ephemeral=True,
            )


class EntradaSelectView(discord.ui.View):
    def __init__(self, group_number: int):
        super().__init__(timeout=180)
        self.add_item(EntradaSelect(group_number))


class SalidaSelectView(discord.ui.View):
    def __init__(self, group_number: int):
        super().__init__(timeout=180)
        self.add_item(SalidaSelect(group_number))


# ============================================================
# PANEL PRINCIPAL PERSISTENTE
# ============================================================

class MainPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _ensure_ids(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if IDS_CACHE:
            return True

        await interaction.response.defer(ephemeral=True, thinking=True)

        updated = await actualizar_cache_ids()

        if not updated:
            await interaction.followup.send(
                "❌ No pude cargar la lista de IDs en este momento. "
                "Inténtelo nuevamente en unos segundos.",
                ephemeral=True,
            )
            return False

        return True

    async def _send_entry_menu(
        self,
        interaction: discord.Interaction,
        group_number: int,
    ) -> None:
        if not IDS_CACHE:
            if not await self._ensure_ids(interaction):
                return

            response_sender = interaction.followup.send
        else:
            response_sender = interaction.response.send_message

        ids = obtener_grupo_ids(group_number)

        if not ids:
            await response_sender(
                "⚠️ No hay IDs disponibles en este grupo.",
                ephemeral=True,
            )
            return

        await response_sender(
            "Seleccione su ID:",
            view=EntradaSelectView(group_number),
            ephemeral=True,
        )

    async def _send_exit_menu(
        self,
        interaction: discord.Interaction,
        group_number: int,
    ) -> None:
        if not IDS_CACHE:
            if not await self._ensure_ids(interaction):
                return

            response_sender = interaction.followup.send
        else:
            response_sender = interaction.response.send_message

        ids = obtener_grupo_ids(group_number)

        if not ids:
            await response_sender(
                "⚠️ No hay IDs disponibles en este grupo.",
                ephemeral=True,
            )
            return

        await response_sender(
            "Seleccione su ID:",
            view=SalidaSelectView(group_number),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Entrada WS-001 a WS-027",
        style=discord.ButtonStyle.success,
        custom_id="tio_max:panel:entrada:1",
        row=0,
    )
    async def entrada_grupo_1(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._send_entry_menu(interaction, 1)

    @discord.ui.button(
        label="Entrada WS-028 a WS-050",
        style=discord.ButtonStyle.success,
        custom_id="tio_max:panel:entrada:2",
        row=0,
    )
    async def entrada_grupo_2(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._send_entry_menu(interaction, 2)

    @discord.ui.button(
        label="Salida WS-001 a WS-027",
        style=discord.ButtonStyle.danger,
        custom_id="tio_max:panel:salida:1",
        row=1,
    )
    async def salida_grupo_1(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._send_exit_menu(interaction, 1)

    @discord.ui.button(
        label="Salida WS-028 a WS-050",
        style=discord.ButtonStyle.danger,
        custom_id="tio_max:panel:salida:2",
        row=1,
    )
    async def salida_grupo_2(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._send_exit_menu(interaction, 2)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        logger.exception(
            "Error en MainPanelView | Item=%s",
            item,
            exc_info=error,
        )

        message = (
            "❌ Ocurrió un error procesando la opción. "
            "Inténtelo nuevamente."
        )

        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
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


class TioMaxBot(commands.Bot):
    async def setup_hook(self) -> None:
        # Registra el panel persistente antes de conectarse completamente.
        self.add_view(MainPanelView())

        await actualizar_cache_ids()

        if not refrescar_ids.is_running():
            refrescar_ids.start()


bot = TioMaxBot(
    command_prefix="!",
    intents=intents,
    help_command=None,
)


@tasks.loop(minutes=IDS_REFRESH_MINUTES)
async def refrescar_ids() -> None:
    await actualizar_cache_ids()


@refrescar_ids.before_loop
async def before_refrescar_ids() -> None:
    await bot.wait_until_ready()


@bot.event
async def on_ready() -> None:
    logger.info(
        "Tío Max conectado como %s | ID=%s",
        bot.user,
        bot.user.id if bot.user else "desconocido",
    )


@bot.event
async def on_disconnect() -> None:
    logger.warning("Tío Max perdió temporalmente la conexión con Discord.")


@bot.event
async def on_resumed() -> None:
    logger.info("Tío Max reanudó la sesión con Discord.")


@bot.command(name="panel")
@commands.guild_only()
async def panel(ctx: commands.Context) -> None:
    embed = discord.Embed(
        title="REGISTRO OFICIAL DE PRESTACIÓN DE SERVICIOS WINGS STORE",
        description=(
            "Utilice los botones para registrar su entrada o salida.\n\n"
            "🏆FELICIDADES AL ÁREA DEL MÉS DE AGOSTO: ?????🏆.\n"
        ),
        color=0x5865F2,
    )

    embed.add_field(
        name="¡Hola! Soy el Tío Max",
        value=(
            "Estoy listo para registrar su jornada. "
            "Seleccione la opción correspondiente y después su ID."
        ),
        inline=False,
    )

    embed.add_field(
        name="🟢 Entrada 🟢",
        value=(
            "Registre el inicio de la jornada con su respectivo "
            "ID de empleado."
        ),
        inline=False,
    )

    embed.add_field(
        name="🔴 Salida 🔴",
        value=(
            "Registre la terminación de la jornada con su respectivo "
            "ID de empleado."
        ),
        inline=False,
    )

    embed.set_footer(
        text="Sistema automatizado y controlado por HR | Dept."
    )

    await ctx.send(embed=embed, view=MainPanelView())


@panel.error
async def panel_error(
    ctx: commands.Context,
    error: commands.CommandError,
) -> None:
    if isinstance(error, commands.NoPrivateMessage):
        await ctx.send("Este comando solo puede utilizarse en el servidor.")
        return

    logger.exception("Error ejecutando !panel.", exc_info=error)
    await ctx.send(
        "❌ No pude crear el panel. Revise los logs de Railway."
    )


# ============================================================
# EJECUCIÓN
# ============================================================

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)