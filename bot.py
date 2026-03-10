import discord
from discord.ext import commands
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os
import json

# =========================
# ZONA HORARIA CORRECTA
# =========================

CARACAS = ZoneInfo("America/Caracas")

def hora_actual():
    return datetime.now(CARACAS)

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

    ahora = datetime.utcnow()  - timedelta(hours=4)

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
# COMANDOS
# =========================

@bot.command()
async def entrada(ctx, id_emp: str, *, actividad: str):

    ids_validos = obtener_ids()

    if id_emp not in ids_validos:
        await ctx.send("ID no válido")
        return

    registrar_entrada(id_emp, actividad, ctx.author.name)

    await ctx.send("Entrada registrada")

@bot.command()
async def salida(ctx, id_emp: str):

    registrar_salida(id_emp, ctx.author.name)

    await ctx.send("Salida registrada")

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