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

# Entidades e bancos disponíveis
ENTIDADES = ["VITHALL", "RBM", "PESSOAL"]
BANCOS = ["Pagbank", "Banrisul", "Nubank", "Caixa", "Simples"]


def _get_client():
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


def _get_or_create_sheet(spreadsheet_id: str, tab_name: str):
    """Abre ou cria uma aba com o nome dado, com headers e formatação."""
    client = _get_client()
    spreadsheet = client.open_by_key(spreadsheet_id)
    try:
        sheet = spreadsheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=tab_name, rows=1000, cols=10)
        sheet.append_row(HEADERS)
        sheet.format("A1:I1", {
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            "backgroundColor": {"red": 0.082, "green": 0.396, "blue": 0.753},
        })
        _aplicar_filtro(spreadsheet, sheet)
    return sheet


def get_all_rows(spreadsheet_id: str) -> list:
    """Retorna linhas de TODAS as abas, com _tab_name e _row_index."""
    try:
        client = _get_client()
        spreadsheet = client.open_by_key(spreadsheet_id)
        all_rows = []
        for ws in spreadsheet.worksheets():
            records = ws.get_all_records()
            for i, row in enumerate(records):
                row["_tab_name"] = ws.title
                row["_row_index"] = i + 2
            all_rows.extend(records)
        return all_rows
    except Exception as e:
        st.error(f"Erro ao carregar planilha: {e}")
        return []


def update_status(spreadsheet_id: str, tab_name: str, row_index: int, status: str) -> bool:
    """Atualiza a coluna Status (G=7) de uma linha em uma aba específica."""
    try:
        sheet = _get_or_create_sheet(spreadsheet_id, tab_name)
        sheet.update_cell(row_index, 7, status)
        return True
    except Exception as e:
        st.error(f"Erro ao atualizar status: {e}")
        return False


def append_row(spreadsheet_id: str, data: dict, tab_name: str) -> bool:
    """Adiciona uma linha na aba correta. Retorna True se sucesso."""
    try:
        sheet = _get_or_create_sheet(spreadsheet_id, tab_name)
        today = date.today().strftime("%d/%m/%Y")
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
