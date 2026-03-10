import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

creds = Credentials.from_service_account_file(
    "credenciales.json",
    scopes=SCOPES
)

client = gspread.authorize(creds)

sheet = client.open("Wingstore People Operations Master").sheet1

sheet.append_row(["PRUEBA", "BOT", "FUNCIONA"])

print("Wingstore People Operations Master")