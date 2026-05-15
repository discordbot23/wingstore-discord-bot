import discord
from discord.ext import commands
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os
import json

# =========================
# GOOGLE SHEETS
# =========================

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

creds = Credentials.from_service_account_info(
    json.loads(os.getenv("GOOGLE_CREDENTIALS")),
    scopes=SCOPES
)

client = gspread.authorize(creds)

spreadsheet = client.open("Wingstore People Operations Master")

sheet_registro = spreadsheet.worksheet("Respuestas de formulario 1")
sheet_empleados = spreadsheet.worksheet("EMPLEADOS Y CONTRATOS")

# =========================
# DISCORD BOT
# =========================

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# =========================
# OBTENER IDS
# =========================

def obtener_ids():

    data = sheet_empleados.get("A4:A")

    ids = []

    for fila in data:
        if fila:
            ids.append(fila[0])

    return ids

# =========================
# REGISTRAR ENTRADA
# =========================

def registrar_entrada(id_emp, actividad, usuario):

    ahora = datetime.utcnow() - timedelta(hours=4)

    fecha = ahora.strftime("%Y-%m-%d")
    hora = ahora.strftime("%H:%M")

    fila = len(sheet_registro.get_all_values()) + 1

    sheet_registro.update(
        f"A{fila}:F{fila}",
        [[fecha, id_emp, hora, "", actividad, usuario]]
    )

# =========================
# REGISTRAR SALIDA
# =========================

def registrar_salida(id_emp, usuario):

    registros = sheet_registro.get_all_values()

    for i in range(len(registros)-1, 0, -1):

        fila = registros[i]

        if fila[1] == id_emp and fila[3] == "":

            ahora = datetime.utcnow() - timedelta(hours=4)
            hora = ahora.strftime("%H:%M")

            sheet_registro.update(
                f"D{i+1}",
                [[hora]]
            )

            break

# =========================
# CLASS MODAL
# =========================
class ActividadModal(discord.ui.Modal, title="Registrar Actividad de Hoy"):


    actividad = discord.ui.TextInput(
        label="Describe tu actividad",
        style=discord.TextStyle.paragraph,
        placeholder="Ej: Diseño de publicaciones, programación, atención a clientes...",
        required=True,
        max_length=300
    )

    def __init__(self, id_emp, panel_message):
        super().__init__()
        self.id_emp = id_emp
        self.panel_message= panel_message

    async def on_submit(self, interaction: discord.Interaction):

        actividad = self.actividad.value

        registrar_entrada(self.id_emp, actividad, interaction.user.name)

        await interaction.response.send_message(
            "✅ Entrada registrada correctamente",
            ephemeral=True
        )

        await self.panel_message.delete()
        
# =========================
# SELECT ENTRADA
# =========================

class EntradaSelect(discord.ui.Select):

    def __init__(self):

        ids = obtener_ids()

        options = [
            discord.SelectOption(label=i, value=i)
            for i in ids[:25]
        ]

        super().__init__(
            placeholder="Selecciona tu ID (WS-001 WS-027)",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):

        id_emp = self.values[0]

        modal = ActividadModal(id_emp, interaction.message)

        await interaction.response.send_modal(modal)

class EntradaSelect2(discord.ui.Select):

    def __init__(self):

        ids = obtener_ids()

        options = [
            discord.SelectOption(label=i, value=i)
            for i in ids[25:50]
        ]

        super().__init__(
            placeholder="Selecciona tu ID (WS-028 WS-050)",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):

        id_emp = self.values[0]

        modal = ActividadModal(id_emp, interaction.message)

        await interaction.response.send_modal(modal)
        

# =========================
# SELECT SALIDA
# =========================

class SalidaSelect(discord.ui.Select):

    def __init__(self):

        ids = obtener_ids()

        options = [
            discord.SelectOption(label=i, value=i)
            for i in ids[:25]
        ]

        super().__init__(
            placeholder="Selecciona tu ID (WS-001 WS-027)",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):

        id_emp = self.values[0]

        registrar_salida(id_emp, interaction.user.name)

        await interaction.response.send_message(
            " ✅ Salida registrada",
            ephemeral=True
        )
        
        await interaction.message.delete()


class SalidaSelect2(discord.ui.Select):

    def __init__(self):

        ids = obtener_ids()

        options = [
            discord.SelectOption(label=i, value=i)
            for i in ids[25:50]
        ]

        super().__init__(
            placeholder="Selecciona tu ID (WS-028 WS-050)",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):

        id_emp = self.values[0]

        registrar_salida(id_emp, interaction.user.name)

        await interaction.response.send_message(
            " ✅ Salida registrada",
            ephemeral=True
        )
# =========================
# MENUS
# =========================

class EntradaMenu(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(EntradaSelect())
        self.add_item(EntradaSelect2())

class SalidaMenu(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(SalidaSelect())
        self.add_item(SalidaSelect2())

# =========================
# PANEL PRINCIPAL
# =========================

@bot.command()
async def panel(ctx):

    embed = discord.Embed(
        title="📊 WingsStore • Registro de Jornada • Human Resources Dept.📊 ",
        description="Selecciona una opción",
        color=0x5865F2
    )


    embed.add_field(
        name="🟢 Entrada 🟢",
        value="Registre el inicio de jornada con su respectivo ID de Empleado ",
        inline=False
    )

    embed.add_field(
        name="🔴 Salida 🔴",
        value="Registre el fin de su jornada con su respectivo ID de Empleado",
        inline=False
    )

    embed.set_footer(text="Sistema de registro automatizado")

    view = discord.ui.View(timeout=None)

    # =========================
    # BOTONES
    # =========================

    boton_entrada_1 = discord.ui.Button(
        label="Entrada WS-001 a WS-027",
        style=discord.ButtonStyle.success
    )

    boton_entrada_2 = discord.ui.Button(
        label="Entrada WS-028 a WS-050",
        style=discord.ButtonStyle.success
    )

    boton_salida_1 = discord.ui.Button(
        label="Salida WS-001 a WS-027",
        style=discord.ButtonStyle.danger
    )

    boton_salida_2 = discord.ui.Button(
        label="Salida WS-028 a WS-050",
        style=discord.ButtonStyle.danger
    )

    # =========================
    # CALLBACKS ENTRADA
    # =========================

    async def entrada1_callback(interaction):

        view_menu = discord.ui.View(timeout=None)
        view_menu.add_item(EntradaSelect())

        await interaction.response.send_message(
            "Selecciona tu ID",
            view=view_menu,
            ephemeral=True
        )

    async def entrada2_callback(interaction):

        view_menu = discord.ui.View(timeout=None)
        view_menu.add_item(EntradaSelect2())

        await interaction.response.send_message(
            "Selecciona tu ID",
            view=view_menu,
            ephemeral=True
        )

    # =========================
    # CALLBACKS SALIDA
    # =========================

    async def salida1_callback(interaction):

        view_menu = discord.ui.View(timeout=None)
        view_menu.add_item(SalidaSelect())

        await interaction.response.send_message(
            "Selecciona tu ID",
            view=view_menu,
            ephemeral=True
        )

    async def salida2_callback(interaction):

        view_menu = discord.ui.View(timeout=None)
        view_menu.add_item(SalidaSelect2())

        await interaction.response.send_message(
            "Selecciona tu ID",
            view=view_menu,
            ephemeral=True
        )

    # =========================
    # ASIGNAR CALLBACKS
    # =========================

    boton_entrada_1.callback = entrada1_callback
    boton_entrada_2.callback = entrada2_callback

    boton_salida_1.callback = salida1_callback
    boton_salida_2.callback = salida2_callback

    # =========================
    # AGREGAR BOTONES
    # =========================

    view.add_item(boton_entrada_1)
    view.add_item(boton_entrada_2)

    view.add_item(boton_salida_1)
    view.add_item(boton_salida_2)

    # =========================
    # ENVIAR PANEL
    # =========================

    await ctx.send(embed=embed, view=view)

# =========================
# BOT ONLINE
# =========================

@bot.event
async def on_ready():
    print(f"Bot conectado como {bot.user}")

# =========================
# RUN
# =========================

bot.run(os.getenv("DISCORD_TOKEN"))