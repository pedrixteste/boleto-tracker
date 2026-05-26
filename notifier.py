#!/usr/bin/env python3
"""
Notificador automático de boletos pendentes.
Executado pelo GitHub Actions a cada 30 minutos.

Lê os horários configurados na aba _Config do Google Sheets e,
se o horário atual (BRT) estiver dentro da janela, envia uma
notificação push via ntfy.sh para o celular.
"""
import json
import os
import sys
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import gspread
import requests
from google.oauth2.service_account import Credentials

# ── Configuração ──────────────────────────────────────────────────────────────

TIMEZONE         = ZoneInfo("America/Sao_Paulo")   # BRT (UTC-3)
TOLERANCIA_MIN   = 14   # ±14 min ao redor do horário configurado
CONFIG_TAB       = "_Config"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


# ── Google Sheets ─────────────────────────────────────────────────────────────

def _get_client() -> gspread.Client:
    raw = os.environ.get("GCP_SERVICE_ACCOUNT", "").strip()
    if not raw:
        # Fallback local para testes
        with open("credentials.json") as f:
            raw = f.read()
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def _get_config(spreadsheet_id: str) -> dict:
    try:
        client = _get_client()
        ws = client.open_by_key(spreadsheet_id).worksheet(CONFIG_TAB)
        config = {}
        for row in ws.get_all_values()[1:]:   # pula header
            if len(row) >= 2 and row[0].strip():
                config[row[0].strip()] = row[1].strip()
        return config
    except gspread.WorksheetNotFound:
        print("Aba _Config não encontrada. Configure os alertas no app.")
        return {}
    except Exception as e:
        print(f"Erro ao ler configuração: {e}")
        return {}


def _get_pendentes(spreadsheet_id: str) -> list:
    try:
        client = _get_client()
        spreadsheet = client.open_by_key(spreadsheet_id)
        pendentes = []
        for ws in spreadsheet.worksheets():
            if ws.title.startswith("_"):
                continue          # pula abas internas
            for i, row in enumerate(ws.get_all_records()):
                if row.get("Status", "").strip() in ("Pendente", ""):
                    row["_tab_name"] = ws.title
                    pendentes.append(row)
        return pendentes
    except Exception as e:
        print(f"Erro ao ler pendentes: {e}")
        return []


# ── Lógica de horário ─────────────────────────────────────────────────────────

def _deve_notificar(horarios: list[str]) -> bool:
    """Retorna True se o horário BRT atual está ±14 min de algum horário configurado."""
    now = datetime.now(tz=TIMEZONE)
    for h in horarios:
        try:
            hh, mm = map(int, h.strip().split(":"))
            agendado = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            diff_min = abs((now - agendado).total_seconds()) / 60
            if diff_min <= TOLERANCIA_MIN:
                return True
        except ValueError:
            continue
    return False


# ── Envio via ntfy.sh ─────────────────────────────────────────────────────────

def _enviar(topic: str, title: str, body: str, urgente: bool = False):
    priority = "urgent" if urgente else "default"
    try:
        resp = requests.post(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            headers={
                "Title":    title.encode("utf-8"),
                "Priority": priority,
                "Tags":     "money_with_wings,bell",
            },
            timeout=15,
        )
        resp.raise_for_status()
        print(f"✅ Notificação enviada para '{topic}' (prioridade: {priority}).")
    except Exception as e:
        print(f"❌ Erro ao enviar notificação: {e}")
        sys.exit(1)


# ── Principal ─────────────────────────────────────────────────────────────────

def main():
    spreadsheet_id = os.environ.get("SPREADSHEET_ID", "").strip()
    if not spreadsheet_id:
        print("SPREADSHEET_ID não configurado.")
        sys.exit(1)

    config     = _get_config(spreadsheet_id)
    ntfy_topic = config.get("ntfy_topic", "").strip()
    horarios   = [h for h in config.get("horarios", "").split(",") if h.strip()]

    if not ntfy_topic:
        print("ntfy_topic não configurado. Acesse o app → 🔔 Alertas.")
        sys.exit(0)
    if not horarios:
        print("Nenhum horário configurado. Acesse o app → 🔔 Alertas.")
        sys.exit(0)

    agora = datetime.now(tz=TIMEZONE)
    print(f"Hora atual (BRT): {agora.strftime('%H:%M')} | Horários: {horarios}")

    if not _deve_notificar(horarios):
        print("Fora da janela de notificação. Nada a fazer.")
        sys.exit(0)

    pendentes = _get_pendentes(spreadsheet_id)
    if not pendentes:
        print("Nenhum boleto pendente. Notificação não enviada.")
        sys.exit(0)

    today = date.today()
    vencidos, urgentes, outros = [], [], []

    for r in pendentes:
        benef = r.get("Beneficiário", "") or "Sem nome"
        valor = r.get("Valor (R$)", "")   or "—"
        venc  = r.get("Vencimento", "")   or "—"
        conta = r.get("_tab_name", "")
        linha = f"• {benef}  R${valor}  {venc}  [{conta}]"
        try:
            dias = (datetime.strptime(venc, "%d/%m/%Y").date() - today).days
            if dias < 0:
                vencidos.append(linha)
            elif dias <= 3:
                urgentes.append(linha)
            else:
                outros.append(linha)
        except Exception:
            outros.append(linha)

    partes = []
    if vencidos:
        partes.append(f"🔴 VENCIDOS ({len(vencidos)}):\n" + "\n".join(vencidos))
    if urgentes:
        partes.append(f"🟠 URGENTE — vence em até 3 dias ({len(urgentes)}):\n" + "\n".join(urgentes))
    if outros:
        partes.append(f"🟢 Outros pendentes ({len(outros)}):\n" + "\n".join(outros))

    body   = "\n\n".join(partes)
    total  = len(pendentes)
    emoji  = "🚨" if vencidos else ("⚠️" if urgentes else "📄")
    title  = f"{emoji} {total} boleto(s) pendente(s)"

    _enviar(ntfy_topic, title, body, urgente=bool(vencidos or urgentes))
    print(f"Total: {len(vencidos)} vencidos, {len(urgentes)} urgentes, {len(outros)} normais.")


if __name__ == "__main__":
    main()
