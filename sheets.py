import gspread
from datetime import date, datetime
import streamlit as st
import json
import io
import re
import base64
import requests as _requests

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADERS = [
    "Data Cadastro", "Tipo", "Beneficiário",
    "Valor (R$)", "Vencimento", "Dias p/ Vencer",
    "Status", "Código/Número", "Observações",
    "Mês Cadastro", "Mês Vencimento",
    "Foto", "Comprovante",
]

# Índices das colunas especiais (1-based para gspread)
COL_STATUS      = 7   # G
COL_FOTO        = 12  # L
COL_COMPROVANTE = 13  # M

# Entidades e bancos disponíveis
ENTIDADES  = ["VITHALL", "RBM", "PESSOAL", "Anaelena", "Cleia"]
BANCOS     = ["Pagbank", "Banrisul", "Nubank", "Caixa", "Simples"]

# Aba de configuração (oculta — começa com _)
CONFIG_TAB = "_Config"


def _get_credentials_info() -> dict:
    """Retorna o dict de credenciais do service account."""
    info = None
    try:
        raw = st.secrets["gcp_service_account"]
        if isinstance(raw, str):
            info = json.loads(raw)
        else:
            info = json.loads(json.dumps(dict(raw)))
    except Exception:
        pass
    if info is None:
        try:
            with open("credentials.json") as f:
                info = json.load(f)
        except FileNotFoundError:
            raise RuntimeError("Credenciais Google não encontradas.")
    return info


def _get_client():
    return gspread.service_account_from_dict(_get_credentials_info(), scopes=SCOPES)


# ── Hospedagem de imagens (ImgBB) ─────────────────────────────────────────────

def upload_imagem_drive(image_bytes: bytes, filename: str) -> str:
    """
    Faz upload da imagem para o ImgBB (hosting gratuito) e retorna URL pública.
    Comprime para JPEG 1200px / qualidade 75 antes de enviar.

    Requer a chave de API do ImgBB em st.secrets["imgbb_api_key"].
    Cadastro gratuito em: https://imgbb.com/
    """
    from PIL import Image as _PILImage

    # Chave de API
    try:
        api_key = st.secrets["imgbb_api_key"]
    except Exception:
        raise RuntimeError(
            "SETUP_IMGBB: Chave do ImgBB não configurada. "
            "Crie uma conta gratuita em imgbb.com, copie sua API key e adicione "
            "imgbb_api_key = \"sua-chave\" nos Secrets do Streamlit Cloud."
        )

    # Comprime imagem
    img = _PILImage.open(io.BytesIO(image_bytes))
    if img.width > 1200:
        ratio = 1200 / img.width
        img = img.resize((1200, int(img.height * ratio)), _PILImage.LANCZOS)
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=75)
    compressed = buf.getvalue()

    # Upload via API
    b64 = base64.b64encode(compressed).decode("utf-8")
    safe_name = re.sub(r"[^\w\-.]", "_", filename)
    resp = _requests.post(
        "https://api.imgbb.com/1/upload",
        data={"key": api_key, "image": b64, "name": safe_name},
        timeout=30,
    )
    data = resp.json()
    if data.get("success"):
        return data["data"]["url"]
    raise RuntimeError(f"ImgBB: {data.get('error', {}).get('message', resp.text[:200])}")


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
    """Abre ou cria uma aba com o nome dado, com headers e formatação.
    Para abas existentes, apenas abre — cabeçalhos são gerenciados pela migração.
    """
    client = _get_client()
    spreadsheet = client.open_by_key(spreadsheet_id)
    try:
        sheet = spreadsheet.worksheet(tab_name)
        # Só expande colunas se necessário (col_count vem dos metadados, sem leitura extra)
        if sheet.col_count < len(HEADERS):
            sheet.resize(rows=sheet.row_count, cols=len(HEADERS))
        return sheet
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=tab_name, rows=1000, cols=len(HEADERS))
        sheet.append_row(HEADERS)
        last_col = chr(ord("A") + len(HEADERS) - 1)  # ex: "M" para 13 colunas
        sheet.format(f"A1:{last_col}1", {
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            "backgroundColor": {"red": 0.082, "green": 0.396, "blue": 0.753},
        })
        _aplicar_filtro(spreadsheet, sheet)
        return sheet
    return sheet


_MIGRATION_VERSION = "v5"
_MIGRATION_KEY     = "_migration_version"


def migrar_cabecalhos(spreadsheet_id: str) -> bool:
    """
    Migração completa das abas existentes.
    Persiste a versão na aba _Config para não re-executar a cada reinício do servidor.
    """
    # ── Verifica se já foi migrado (lê _Config — 1 chamada apenas) ────────────
    try:
        config_atual = get_config(spreadsheet_id)
        if config_atual.get(_MIGRATION_KEY) == _MIGRATION_VERSION:
            return True   # já migrado — sai sem fazer nenhuma chamada pesada
    except Exception:
        pass

    try:
        client = _get_client()
        spreadsheet = client.open_by_key(spreadsheet_id)
        today = date.today()

        for ws in spreadsheet.worksheets():
            if ws.title.startswith("_"):
                continue

            # ── 1. Expande a grade se necessário ─────────────────────────────
            if ws.col_count < len(HEADERS):
                ws.resize(rows=ws.row_count, cols=len(HEADERS))

            # ── 2. Cabeçalhos ────────────────────────────────────────────────
            existing_headers = ws.row_values(1)
            missing = [h for h in HEADERS if h not in existing_headers]
            if missing:
                next_col = len(existing_headers) + 1
                for i, header in enumerate(missing):
                    ws.update_cell(1, next_col + i, header)
                ws.format("A1:K1", {
                    "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                    "backgroundColor": {"red": 0.082, "green": 0.396, "blue": 0.753},
                })
                _aplicar_filtro(spreadsheet, ws)

            # ── 2. Corrige linhas existentes ─────────────────────────────────
            all_values = ws.get_all_values()
            if len(all_values) <= 1:
                continue  # só cabeçalho, sem dados

            updates = []
            for idx, row_vals in enumerate(all_values[1:], start=2):
                # Expande a lista caso tenha menos de 11 colunas
                while len(row_vals) < 11:
                    row_vals.append("")

                venc_str   = row_vals[4].strip()   # col E
                cad_str    = row_vals[0].strip()   # col A
                status_cur = row_vals[6].strip()   # col G

                # Col F — Dias p/ Vencer com fórmula correta
                dias_f = f'=SE(E{idx}="";"";DATA(DIREITA(E{idx};4);EXT.TEXTO(E{idx};4;2);ESQUERDA(E{idx};2))-HOJE())'
                updates.append({"range": f"F{idx}", "values": [[dias_f]]})

                # Col J — Mês Cadastro
                if not row_vals[9]:
                    mes_cad = f'=SE(A{idx}="";"";EXT.TEXTO(A{idx};4;2)&"/"&DIREITA(A{idx};4))'
                    updates.append({"range": f"J{idx}", "values": [[mes_cad]]})

                # Col K — Mês Vencimento
                if not row_vals[10]:
                    mes_venc = f'=SE(E{idx}="";"";EXT.TEXTO(E{idx};4;2)&"/"&DIREITA(E{idx};4))'
                    updates.append({"range": f"K{idx}", "values": [[mes_venc]]})

                # Col G — Status: Pendente → Previsão ou Vencido
                if status_cur == "Pendente":
                    novo_status = "Previsão"
                    if venc_str:
                        try:
                            venc_date = datetime.strptime(venc_str, "%d/%m/%Y").date()
                            if venc_date < today:
                                novo_status = "Vencido"
                        except Exception:
                            pass
                    updates.append({"range": f"G{idx}", "values": [[novo_status]]})

            if updates:
                ws.batch_update(updates, value_input_option="USER_ENTERED")

        # ── Persiste versão de migração no _Config ────────────────────────────
        try:
            try:
                cfg_ws = spreadsheet.worksheet(CONFIG_TAB)
            except gspread.WorksheetNotFound:
                cfg_ws = spreadsheet.add_worksheet(title=CONFIG_TAB, rows=20, cols=2)
                cfg_ws.append_row(["Chave", "Valor"])

            # Atualiza linha existente ou adiciona nova
            cfg_vals = cfg_ws.get_all_values()
            for i, row in enumerate(cfg_vals):
                if row and row[0].strip() == _MIGRATION_KEY:
                    cfg_ws.update_cell(i + 1, 2, _MIGRATION_VERSION)
                    break
            else:
                cfg_ws.append_row([_MIGRATION_KEY, _MIGRATION_VERSION])
        except Exception:
            pass   # não bloqueia se _Config falhar

        return True
    except Exception as e:
        st.error(f"Erro ao migrar planilha: {e}")
        return False


def atualizar_vencidos(spreadsheet_id: str):
    """Compat: mantido para não quebrar imports existentes. Use carregar_pendentes()."""
    carregar_pendentes(spreadsheet_id)


def get_all_rows(spreadsheet_id: str) -> list:
    """Compat: mantido para não quebrar imports existentes. Use carregar_pendentes()."""
    return carregar_pendentes(spreadsheet_id)


def carregar_pendentes(spreadsheet_id: str) -> list:
    """
    Lê todas as abas, atualiza status 'Vencido' se necessário e retorna
    todos os registros — em UMA ÚNICA passagem pela planilha.

    Substitui a dupla chamada atualizar_vencidos() + get_all_rows() que
    dobrava as requisições à Sheets API e causava erro 429 (quota exceeded).
    """
    try:
        client = _get_client()
        spreadsheet = client.open_by_key(spreadsheet_id)
        today = date.today()
        all_rows = []

        for ws in spreadsheet.worksheets():
            if ws.title.startswith("_"):
                continue

            records = ws.get_all_records()
            updates = []

            for i, row in enumerate(records):
                row["_tab_name"]  = ws.title
                row["_row_index"] = i + 2

                # Atualiza vencidos em memória e prepara batch write
                if row.get("Status", "").strip() == "Previsão":
                    venc = row.get("Vencimento", "").strip()
                    if venc:
                        try:
                            if datetime.strptime(venc, "%d/%m/%Y").date() < today:
                                row["Status"] = "Vencido"
                                updates.append({"range": f"G{i + 2}", "values": [["Vencido"]]})
                        except Exception:
                            pass

            if updates:
                ws.batch_update(updates)

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


def update_comprovante(spreadsheet_id: str, tab_name: str, row_index: int, url: str) -> bool:
    """Salva o link do comprovante na coluna M de uma linha."""
    try:
        sheet = _get_or_create_sheet(spreadsheet_id, tab_name)
        sheet.update_cell(row_index, COL_COMPROVANTE, url)
        return True
    except Exception as e:
        st.error(f"Erro ao salvar comprovante: {e}")
        return False


def append_row(spreadsheet_id: str, data: dict, tab_name: str, foto_url: str = "") -> bool:
    """Adiciona uma linha na aba correta. Retorna True se sucesso."""
    try:
        sheet = _get_or_create_sheet(spreadsheet_id, tab_name)
        today    = date.today().strftime("%d/%m/%Y")
        next_row = len(sheet.get_all_values()) + 1
        dias_formula      = f'=SE(E{next_row}="";"";DATA(DIREITA(E{next_row};4);EXT.TEXTO(E{next_row};4;2);ESQUERDA(E{next_row};2))-HOJE())'
        mes_cad_formula   = f'=SE(A{next_row}="";"";EXT.TEXTO(A{next_row};4;2)&"/"&DIREITA(A{next_row};4))'
        mes_venc_formula  = f'=SE(E{next_row}="";"";EXT.TEXTO(E{next_row};4;2)&"/"&DIREITA(E{next_row};4))'

        # Prefixo "'" força Google Sheets a tratar o valor como texto,
        # evitando que "333,98" seja interpretado como 33.398 (formato americano).
        valor_raw = data.get("valor", "")
        valor_cell = f"'{valor_raw}" if valor_raw else ""

        row = [
            today,
            data.get("tipo", ""),
            data.get("beneficiario", ""),
            valor_cell,
            data.get("vencimento", ""),
            dias_formula,
            "Previsão",
            data.get("codigo", ""),
            data.get("observacoes", ""),
            mes_cad_formula,
            mes_venc_formula,
            foto_url,
            "",   # Comprovante — preenchido depois ao marcar como Pago
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
