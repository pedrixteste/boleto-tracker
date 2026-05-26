#!/usr/bin/env python3
"""
Notificador automático de boletos pendentes.
Executado pelo GitHub Actions diariamente às 07:30 BRT (10:30 UTC).

Regras:
  - Boleto vence amanhã  → 1 notificação por boleto (aviso antecipado)
  - Boleto vence hoje    → 1 notificação por boleto (urgente)
  - Boleto vencido       → 1 notificação por boleto todo dia até ser marcado como pago
  - Boleto com vencimento em 2+ dias → sem notificação (ainda não é hora)

Cada boleto gera sua própria notificação com valor e prazo.
"""
import json
import os
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

import gspread
import requests
from google.oauth2.service_account import Credentials

# ── Configuração ──────────────────────────────────────────────────────────────

TIMEZONE   = ZoneInfo("America/Sao_Paulo")   # BRT — sem horário de verão desde 2020
CONFIG_TAB = "_Config"
SCOPES     = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


# ── Google Sheets ─────────────────────────────────────────────────────────────

def _get_client() -> gspread.Client:
    raw = os.environ.get("GCP_SERVICE_ACCOUNT", "").strip()
    if not raw:
        with open("credentials.json") as f:
            raw = f.read()
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def _get_ntfy_topic(spreadsheet_id: str) -> str:
    try:
        ws = _get_client().open_by_key(spreadsheet_id).worksheet(CONFIG_TAB)
        for row in ws.get_all_values()[1:]:
            if len(row) >= 2 and row[0].strip() == "ntfy_topic":
                return row[1].strip()
        return ""
    except gspread.WorksheetNotFound:
        print("Aba _Config não encontrada. Configure o tópico ntfy no app.")
        return ""
    except Exception as e:
        print(f"Erro ao ler configuração: {e}")
        return ""


def _get_pendentes(spreadsheet_id: str) -> list:
    try:
        pendentes = []
        spreadsheet = _get_client().open_by_key(spreadsheet_id)
        for ws in spreadsheet.worksheets():
            if ws.title.startswith("_"):
                continue
            for row in ws.get_all_records():
                if row.get("Status", "").strip() in ("Pendente", ""):
                    row["_tab_name"] = ws.title
                    pendentes.append(row)
        return pendentes
    except Exception as e:
        print(f"Erro ao ler pendentes: {e}")
        return []


# ── Envio via ntfy.sh ─────────────────────────────────────────────────────────

def _enviar(topic: str, title: str, body: str, priority: str = "default"):
    """Envia uma notificação push via ntfy.sh."""
    try:
        resp = requests.post(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            headers={
                "Title":    title.encode("utf-8"),
                "Priority": priority,
                "Tags":     "money_with_wings",
            },
            timeout=15,
        )
        resp.raise_for_status()
        print(f"  ✅ Enviado: {title}")
    except Exception as e:
        print(f"  ❌ Falha ao enviar '{title}': {e}")


# ── Principal ─────────────────────────────────────────────────────────────────

def main():
    spreadsheet_id = os.environ.get("SPREADSHEET_ID", "").strip()
    if not spreadsheet_id:
        print("SPREADSHEET_ID não configurado.")
        sys.exit(1)

    ntfy_topic = _get_ntfy_topic(spreadsheet_id)
    if not ntfy_topic:
        print("ntfy_topic não configurado. Acesse o app → 🔔 Alertas.")
        sys.exit(0)

    today     = date.today()
    agora_brt = datetime.now(tz=TIMEZONE).strftime("%d/%m/%Y %H:%M")
    print(f"Rodando em {agora_brt} BRT | Planilha: {spreadsheet_id}")

    pendentes = _get_pendentes(spreadsheet_id)
    if not pendentes:
        print("Nenhum boleto pendente. Nada a notificar.")
        sys.exit(0)

    enviados = 0

    for row in pendentes:
        benef = row.get("Beneficiário", "") or "Boleto sem nome"
        valor = row.get("Valor (R$)", "")   or "—"
        venc  = row.get("Vencimento", "")   or ""
        conta = row.get("_tab_name", "")

        # Calcula dias para vencer
        try:
            venc_date = datetime.strptime(venc, "%d/%m/%Y").date()
            dias = (venc_date - today).days
        except Exception:
            # Sem data de vencimento → notifica todo dia como "pendente sem data"
            dias = 0

        # ── Regra: notifica se vence amanhã, hoje ou já venceu ────────────────
        if dias > 1:
            print(f"  ⏭️  {benef} R${valor} — vence em {dias} dias, aguardando.")
            continue

        # Monta título e corpo conforme o prazo
        if dias == 1:
            title    = f"⚠️ Vence AMANHÃ — R$ {valor}"
            body     = (
                f"Beneficiário: {benef}\n"
                f"Valor:        R$ {valor}\n"
                f"Vencimento:   {venc} (amanhã)\n"
                f"Conta:        {conta}"
            )
            priority = "high"

        elif dias == 0:
            title    = f"🚨 Vence HOJE — R$ {valor}"
            body     = (
                f"Beneficiário: {benef}\n"
                f"Valor:        R$ {valor}\n"
                f"Vencimento:   {venc} (HOJE)\n"
                f"Conta:        {conta}\n"
                f"⚠️ Pague hoje para evitar multa!"
            )
            priority = "urgent"

        else:  # dias < 0 — vencido
            atraso = abs(dias)
            title  = f"🔴 {atraso}d em atraso — R$ {valor}"
            body   = (
                f"⛔ BOLETO NÃO PAGO\n\n"
                f"Beneficiário: {benef}\n"
                f"Valor:        R$ {valor}\n"
                f"Venceu em:    {venc} ({atraso} dia(s) atrás)\n"
                f"Conta:        {conta}\n\n"
                f"Marque como pago no app quando quitar."
            )
            priority = "urgent"

        _enviar(ntfy_topic, title, body, priority)
        enviados += 1

    print(f"\nTotal de notificações enviadas: {enviados}/{len(pendentes)}")


if __name__ == "__main__":
    main()
