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
class ActividadModal(discord.ui.Modal, title="Registrar Actividad"):


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
            placeholder="Selecciona tu ID",
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
            placeholder="Selecciona tu ID",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):

        id_emp = self.values[0]

        registrar_salida(id_emp, interaction.user.name)

        await interaction.response.send_message(
            "🚪 Salida registrada",
            ephemeral=True
        )
        await interaction.message.delete()

        await interaction.message.delete()

# =========================
# MENUS
# =========================

class EntradaMenu(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(EntradaSelect())


class SalidaMenu(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(SalidaSelect())

# =========================
# PANEL PRINCIPAL
# =========================

@bot.command()
async def panel(ctx):

    await ctx.message.delete()

    embed = discord.Embed(
        title="📊 WingsStore • Registro de Jornada",
        description="Selecciona una opción",
        color=0x5865F2
    )

    embed.add_field(
        name="🟢 Entrada",
        value="Registrar inicio de jornada",
        inline=False
    )

    embed.add_field(
        name="🔴 Salida",
        value="Registrar fin de jornada",
        inline=False
    )

    embed.set_footer(text="Sistema de registro automatizado")

    view = discord.ui.View(timeout=None)

    boton_entrada = discord.ui.Button(
        label="Registrar Entrada",
        style=discord.ButtonStyle.success
    )

    boton_salida = discord.ui.Button(
        label="Registrar Salida",
        style=discord.ButtonStyle.danger
    )

    async def entrada_callback(interaction):
        await interaction.response.send_message(
            "Selecciona tu ID",
            view=EntradaMenu(),
            ephemeral=True
        )

    async def salida_callback(interaction):
        await interaction.response.send_message(
            "Selecciona tu ID",
            view=SalidaMenu(),
            ephemeral=True
        )

    boton_entrada.callback = entrada_callback
    boton_salida.callback = salida_callback

    view.add_item(boton_entrada)
    view.add_item(boton_salida)

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