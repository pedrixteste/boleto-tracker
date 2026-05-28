import gspread
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
ENTIDADES  = ["VITHALL", "RBM", "PESSOAL", "Anaelena", "Cleia"]
BANCOS     = ["Pagbank", "Banrisul", "Nubank", "Caixa", "Simples"]

# Aba de configuração (oculta — começa com _)
CONFIG_TAB = "_Config"


def _get_client():
    info = None
    try:
        raw = st.secrets["gcp_service_account"]
        # st.secrets pode retornar string (secrets.toml) ou dict (Streamlit Cloud)
        if isinstance(raw, str):
            info = json.loads(raw)
        else:
            # AttrDict → dict normal (json round-trip para converter objetos aninhados)
            info = json.loads(json.dumps(dict(raw)))
    except Exception:
        pass

    if info is None:
        try:
            with open("credentials.json") as f:
                info = json.load(f)
        except FileNotFoundError:
            raise RuntimeError("Credenciais Google não encontradas.")

    return gspread.service_account_from_dict(info, scopes=SCOPES)


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
    """Retorna linhas de TODAS as abas de boletos (ignora abas internas que começam com _)."""
    try:
        client = _get_client()
        spreadsheet = client.open_by_key(spreadsheet_id)
        all_rows = []
        for ws in spreadsheet.worksheets():
            if ws.title.startswith("_"):
                continue   # pula _Config e outras abas internas
            records = ws.get_all_records()
            for i, row in enumerate(records):
                row["_tab_name"]  = ws.title
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
        today    = date.today().strftime("%d/%m/%Y")
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


# ── Configuração de alertas ───────────────────────────────────────────────────

def get_config(spreadsheet_id: str) -> dict:
    """Lê configurações (horários, tópico ntfy) da aba _Config."""
    try:
        client = _get_client()
        spreadsheet = client.open_by_key(spreadsheet_id)
        try:
            ws = spreadsheet.worksheet(CONFIG_TAB)
        except gspread.WorksheetNotFound:
            return {}
        config = {}
        for row in ws.get_all_values()[1:]:   # pula header
            if len(row) >= 2 and row[0].strip():
                config[row[0].strip()] = row[1].strip()
        return config
    except Exception:
        return {}


def save_config(spreadsheet_id: str, ntfy_topic: str):
    """Salva o tópico ntfy na aba _Config. Retorna (True, '') ou (False, msg_erro)."""
    # DEBUG — confirma que a versão nova está rodando
    print(f"[save_config v3] sid={repr(str(spreadsheet_id))[:25]} topic={repr(ntfy_topic)[:30]}")
    try:
        client = _get_client()
        spreadsheet = client.open_by_key(spreadsheet_id)
        try:
            ws = spreadsheet.worksheet(CONFIG_TAB)
            ws.clear()
        except gspread.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title=CONFIG_TAB, rows=20, cols=2)

        ws.append_row(["Chave", "Valor"])
        ws.append_row(["ntfy_topic", ntfy_topic])
        return True, ""
    except Exception as e:
        import traceback
        msg = f"{type(e).__name__}: {e}"
        print(f"[save_config] ERRO: {msg}")
        print(traceback.format_exc())
        return False, msg
