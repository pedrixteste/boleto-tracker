import gspread
from google.oauth2.service_account import Credentials
from datetime import date
import streamlit as st
import json

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADERS = [
    "Data Cadastro", "Tipo", "Beneficiário",
    "Valor (R$)", "Vencimento", "Dias p/ Vencer",
    "Status", "Código/Número", "Observações",
]


def _get_client():
    # Tenta carregar credenciais do secrets do Streamlit (deploy) ou do arquivo local
    try:
        info = json.loads(st.secrets["gcp_service_account"])
    except Exception:
        with open("credentials.json") as f:
            info = json.load(f)

    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def _aplicar_filtro(spreadsheet, sheet):
    """Adiciona filtro automático (setinhas) no cabeçalho."""
    spreadsheet.batch_update({"requests": [{
        "setBasicFilter": {
            "filter": {
                "range": {
                    "sheetId": sheet.id,
                    "startRowIndex": 0,
                    "startColumnIndex": 0,
                    "endColumnIndex": len(HEADERS),
                }
            }
        }
    }]})


def _get_sheet(spreadsheet_id: str):
    client = _get_client()
    spreadsheet = client.open_by_key(spreadsheet_id)
    try:
        sheet = spreadsheet.worksheet("Controle")
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title="Controle", rows=1000, cols=10)
        sheet.append_row(HEADERS)
        # Formata cabeçalho (negrito, cor de fundo azul, texto branco)
        sheet.format("A1:I1", {
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            "backgroundColor": {"red": 0.082, "green": 0.396, "blue": 0.753},
        })
        _aplicar_filtro(spreadsheet, sheet)
    return sheet


def append_row(spreadsheet_id: str, data: dict) -> bool:
    """Adiciona uma linha na planilha. Retorna True se sucesso."""
    try:
        sheet = _get_sheet(spreadsheet_id)
        today = date.today().strftime("%d/%m/%Y")
        # Fórmula de dias para vencer (coluna F referencia coluna E da mesma linha)
        next_row = len(sheet.get_all_values()) + 1
        dias_formula = f'=SE(E{next_row}="";"";VALOR(TEXTO(E{next_row};"DD/MM/AAAA"))-HOJE())'

        row = [
            today,
            data.get("tipo", ""),
            data.get("beneficiario", ""),
            data.get("valor", ""),
            data.get("vencimento", ""),
            dias_formula,
            "Pendente",
            data.get("codigo", ""),
            data.get("observacoes", ""),
        ]
        sheet.append_row(row, value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        st.error(f"Erro ao salvar na planilha: {e}")
        return False
