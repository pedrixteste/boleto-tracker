import gspread
from datetime import date, datetime
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
    "Mês Cadastro", "Mês Vencimento",
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
        # Verifica se os cabeçalhos novos já existem; adiciona se faltar
        existing = sheet.row_values(1)
        missing = [h for h in HEADERS if h not in existing]
        if missing:
            next_col = len(existing) + 1
            for i, header in enumerate(missing):
                sheet.update_cell(1, next_col + i, header)
            sheet.format(f"A1:K1", {
                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                "backgroundColor": {"red": 0.082, "green": 0.396, "blue": 0.753},
            })
            _aplicar_filtro(spreadsheet, sheet)
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=tab_name, rows=1000, cols=11)
        sheet.append_row(HEADERS)
        sheet.format("A1:K1", {
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            "backgroundColor": {"red": 0.082, "green": 0.396, "blue": 0.753},
        })
        _aplicar_filtro(spreadsheet, sheet)
    return sheet


def migrar_cabecalhos(spreadsheet_id: str):
    """
    Migração completa das abas existentes:
    - Adiciona colunas J/K (Mês Cadastro / Mês Vencimento) se faltarem
    - Corrige fórmula de Dias p/ Vencer (#VALUE! → fórmula com DATA())
    - Atualiza fórmulas de Mês nas linhas existentes
    - Muda status: "Pendente" com data futura → "Previsão", com data passada → "Vencido"
    """
    try:
        client = _get_client()
        spreadsheet = client.open_by_key(spreadsheet_id)
        today = date.today()

        for ws in spreadsheet.worksheets():
            if ws.title.startswith("_"):
                continue

            # ── 1. Cabeçalhos ────────────────────────────────────────────────
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

        return True
    except Exception as e:
        st.error(f"Erro ao migrar planilha: {e}")
        return False


def atualizar_vencidos(spreadsheet_id: str):
    """Marca como 'Vencido' boletos com data passada e status 'Previsão'."""
    try:
        client = _get_client()
        spreadsheet = client.open_by_key(spreadsheet_id)
        today = date.today()
        for ws in spreadsheet.worksheets():
            if ws.title.startswith("_"):
                continue
            records = ws.get_all_records()
            updates = []
            for i, row in enumerate(records):
                if row.get("Status", "").strip() != "Previsão":
                    continue
                venc = row.get("Vencimento", "").strip()
                if not venc:
                    continue
                try:
                    if datetime.strptime(venc, "%d/%m/%Y").date() < today:
                        updates.append({"range": f"G{i + 2}", "values": [["Vencido"]]})
                except Exception:
                    pass
            if updates:
                ws.batch_update(updates)
        return True
    except Exception as e:
        st.error(f"Erro ao atualizar vencidos: {e}")
        return False


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
