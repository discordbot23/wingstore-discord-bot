import discord
from discord.ext import commands
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
import os
import json

# ==========================
# GOOGLE SHEETS CONFIG
# ==========================

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

creds = Credentials.from_service_account_info(
    json.loads(os.getenv("GOOGLE_CREDENTIALS")),
    scopes=SCOPES
)

client_gs = gspread.authorize(creds)

spreadsheet = client_gs.open("Wingstore People Operations Master")

sheet_registro = spreadsheet.worksheet("Respuestas de formulario 1")
sheet_empleados = spreadsheet.worksheet("EMPLEADOS Y CONTRATOS")

# ==========================
# DISCORD CONFIG
# ==========================

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ==========================
# OBTENER IDS
# ==========================

def obtener_ids():

    data = sheet_empleados.get("A4:A")

    ids = []

    for fila in data:
        if fila:
            ids.append(fila[0])

    return ids[:25]

# ==========================
# REGISTRAR ENTRADA
# ==========================

def registrar_entrada(id_emp, actividad, usuario):

    ahora = datetime.utcnow() + timedelta(hours=7)

    fecha = ahora.strftime("%Y-%m-%d")
    hora = ahora.strftime("%H:%M")

    fila = len(sheet_registro.get_all_values()) + 1

    sheet_registro.update(
        f"A{fila}:F{fila}",
        [[fecha, id_emp, hora, "", actividad, usuario]]
    )

# ==========================
# REGISTRAR SALIDA
# ==========================

def registrar_salida(id_emp, usuario):

    registros = sheet_registro.get_all_values()

    for i in range(len(registros)-1, 0, -1):

        fila = registros[i]

        if fila[1] == id_emp and fila[3] == "":

            ahora = datetime.utcnow() + timedelta(hours=7)
            salida = ahora.strftime("%H:%M")

            sheet_registro.update(
                f"D{i+1}",
                [[salida]]
            )

            sheet_registro.update(
                f"F{i+1}",
                [[usuario]]
            )

            return True

    return False

# ==========================
# SELECT ENTRADA
# ==========================

class EntradaSelect(discord.ui.Select):

    def __init__(self):

        ids = obtener_ids()

        opciones = []

        for id_emp in ids:

            opciones.append(
                discord.SelectOption(
                    label=id_emp,
                    value=id_emp
                )
            )

        super().__init__(
            placeholder="Selecciona tu ID",
            min_values=1,
            max_values=1,
            options=opciones
        )

    async def callback(self, interaction: discord.Interaction):

        id_emp = self.values[0]

        await interaction.response.send_message(
            "✏️ Escribe tu actividad:",
            ephemeral=True
        )

        def check(m):
            return m.author == interaction.user and not m.author.bot

        msg = await bot.wait_for("message", check=check)

        actividad = msg.content
        usuario = interaction.user.name

        registrar_entrada(id_emp, actividad, usuario)

        await msg.reply("✅ Entrada registrada")

# ==========================
# SELECT SALIDA
# ==========================

class SalidaSelect(discord.ui.Select):

    def __init__(self):

        ids = obtener_ids()

        opciones = []

        for id_emp in ids:

            opciones.append(
                discord.SelectOption(
                    label=id_emp,
                    value=id_emp
                )
            )

        super().__init__(
            placeholder="Selecciona tu ID",
            min_values=1,
            max_values=1,
            options=opciones
        )

    async def callback(self, interaction: discord.Interaction):

        id_emp = self.values[0]
        usuario = interaction.user.name

        resultado = registrar_salida(id_emp, usuario)

        if resultado:

            await interaction.response.send_message(
                f"🚪 Salida registrada para {id_emp}",
                ephemeral=True
            )

        else:

            await interaction.response.send_message(
                "⚠️ No hay entrada abierta",
                ephemeral=True
            )

# ==========================
# MENÚ ENTRADA
# ==========================

class EntradaMenu(discord.ui.View):

    def __init__(self):

        super().__init__(timeout=None)
        self.add_item(EntradaSelect())

# ==========================
# MENÚ SALIDA
# ==========================

class SalidaMenu(discord.ui.View):

    def __init__(self):

        super().__init__(timeout=None)
        self.add_item(SalidaSelect())

# ==========================
# PANEL PRINCIPAL
# ==========================

class Panel(discord.ui.View):

    def __init__(self):

        super().__init__(timeout=None)

    @discord.ui.button(
        label="Registrar Entrada",
        style=discord.ButtonStyle.success,
        emoji="🟢"
    )
    async def entrada(self, interaction: discord.Interaction, button: discord.ui.Button):

        await interaction.response.send_message(
            "Selecciona tu ID para registrar **ENTRADA**",
            view=EntradaMenu(),
            ephemeral=True
        )

    @discord.ui.button(
        label="Registrar Salida",
        style=discord.ButtonStyle.danger,
        emoji="🔴"
    )
    async def salida(self, interaction: discord.Interaction, button: discord.ui.Button):

        await interaction.response.send_message(
            "Selecciona tu ID para registrar **SALIDA**",
            view=SalidaMenu(),
            ephemeral=True
        )

# ==========================
# COMANDO PANEL
# ==========================

@bot.command()
async def panel(ctx):

    embed = discord.Embed(
        title="📊 Wingstore • Registro de Jornada",
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

    await ctx.send(embed=embed, view=Panel())

# ==========================
# BOT READY
# ==========================

@bot.event
async def on_ready():

    print(f"Bot conectado como {bot.user}")

# ==========================
# RUN BOT
# ==========================

bot.run(os.getenv("TOKEN"))