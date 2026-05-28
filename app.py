import streamlit as st
from PIL import Image
from datetime import date, datetime
import io
import re

import fitz  # PyMuPDF

from extractor import extract_boleto, extract_boleto_pdf, extract_cheque, TESSERACT_OK, PYZBAR_OK
from extractor import _parse_boleto_44, _parse_pix_emv, _extrair_dados_texto
import random
import string
import requests as _requests

import importlib, sheets as _sheets_mod
importlib.reload(_sheets_mod)   # garante que o módulo não está cacheado
from sheets import (append_row, get_all_rows, update_status, update_comprovante,
                    get_config, save_config, migrar_cabecalhos, atualizar_vencidos,
                    upload_imagem_drive, ENTIDADES, BANCOS)


def pdf_to_image(pdf_bytes: bytes) -> Image.Image:
    """Converte a primeira página de um PDF em imagem PIL."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]
    mat = fitz.Matrix(2.0, 2.0)
    pix = page.get_pixmap(matrix=mat)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    doc.close()
    return img


st.set_page_config(
    page_title="Boletos & Cheques",
    page_icon="📄",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# ID da planilha
try:
    SPREADSHEET_ID = st.secrets["spreadsheet_id"]
except Exception:
    SPREADSHEET_ID = ""

# CSS mobile-friendly
st.markdown("""
<style>
    .block-container { padding-top: 1.5rem; padding-bottom: 2rem; }
    div[data-testid="stCameraInput"] > label { font-size: 1.1rem; }
    .stButton > button {
        width: 100%;
        height: 3.2rem;
        font-size: 1.1rem;
        border-radius: 8px;
    }
</style>
""", unsafe_allow_html=True)


# ── Processamento do resultado do scanner ─────────────────────────────────────

def _processar_raw(raw: str, tipo: str) -> dict:
    """Converte string lida pelo ZXing em dict de dados do boleto."""
    result = {"tipo": tipo, "beneficiario": "", "valor": "", "vencimento": "", "codigo": "", "observacoes": ""}
    if raw.startswith("000201"):          # PIX EMV
        result.update(_parse_pix_emv(raw))
        result["codigo"] = raw[:60] + "..." if len(raw) > 60 else raw
    else:
        digits = re.sub(r"\D", "", raw)
        if len(digits) >= 44:
            parsed = _parse_boleto_44(digits[:44])
            if parsed:
                result.update(parsed)
                result["codigo"] = digits[:44]
    return result


def init_state():
    """
    Inicializa o session_state.
    IMPORTANTE: verifica ?scan= ANTES de tudo, porque o ZXing redireciona
    a página inteira (session_state zerado) e colocamos o resultado na URL.
    """
    scan = st.query_params.get("scan", "")
    if scan:
        # Recupera o estado que estava na sessão antes da navegação
        tipo     = st.query_params.get("stipo", "Boleto")
        entidade = st.query_params.get("sent",  ENTIDADES[0])
        banco    = st.query_params.get("sban",  BANCOS[0])
        raw      = scan.strip()
        st.query_params.clear()

        # Garante defaults necessários
        st.session_state.setdefault("dados",  {})
        st.session_state.setdefault("imagem", None)

        # Restaura conta selecionada
        st.session_state.tipo     = tipo
        st.session_state.entidade = entidade if entidade in ENTIDADES else ENTIDADES[0]
        st.session_state.banco    = banco    if banco    in BANCOS    else BANCOS[0]
        st.session_state.imagem   = None

        if raw.startswith("http://") or raw.startswith("https://"):
            # QR code de nota fiscal — não é boleto
            st.session_state.tela = "captura"
            st.session_state["_scan_msg"] = (
                "warning",
                "⚠️ Este QR code é um link de nota fiscal, não contém dados de pagamento. "
                "Use a aba **Linha digitável** ou **Digitar**.",
            )
        else:
            result = _processar_raw(raw, tipo)
            if result.get("valor") or result.get("codigo"):
                st.session_state.dados = result
                st.session_state.tela  = "revisao"
            else:
                st.session_state.tela = "captura"
                st.session_state["_scan_msg"] = (
                    "info",
                    "ℹ️ Código lido mas não reconhecido como boleto. "
                    "Tente a aba **Linha digitável** ou **Digitar**.",
                )
        return  # ← sai sem sobrescrever o que acabou de definir

    # Inicialização normal (primeira carga ou navegação interna)
    defaults = {
        "tela":     "inicio",
        "tipo":     None,
        "entidade": ENTIDADES[0],
        "banco":    BANCOS[0],
        "dados":    {},
        "imagem":   None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

    # Migração: corrige fórmulas, cabeçalhos e status nas abas existentes
    # Flag versionada — incrementar quando houver nova migração
    _MIGRATION_VERSION = "v5"
    if st.session_state.get("_migrado") != _MIGRATION_VERSION and SPREADSHEET_ID:
        migrar_cabecalhos(SPREADSHEET_ID)
        st.session_state["_migrado"] = _MIGRATION_VERSION


# ── Tela 1: Início ────────────────────────────────────────────────────────────

def tela_inicio():
    st.title("📄 Controle Financeiro")
    st.markdown("**O que você quer registrar?**")
    st.markdown("")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("🧾 Boleto", use_container_width=True):
            st.session_state.tipo = "Boleto"
            st.session_state.tela = "conta"
            st.rerun()
    with col2:
        if st.button("📝 Cheque", use_container_width=True):
            st.session_state.tipo = "Cheque"
            st.session_state.tela = "conta"
            st.rerun()

    if st.button("💸 PIX", use_container_width=True):
        st.session_state.tipo = "PIX"
        st.session_state.tela = "conta"
        st.rerun()

    st.markdown("")
    if st.button("📋 Ver pendentes", use_container_width=True):
        st.session_state.tela = "pendentes"
        st.rerun()

    st.markdown("")
    if st.button("📅 Lembretes de conta mensal", use_container_width=True):
        st.session_state.tela = "lembretes"
        st.rerun()

    st.markdown("")
    if st.button("🔔 Configurar alertas", use_container_width=True):
        st.session_state.tela = "config"
        st.rerun()

    avisos = []
    if not PYZBAR_OK:
        avisos.append("⚠️ pyzbar não instalado — leitura de código de barras desativada.")
    if not TESSERACT_OK:
        avisos.append("⚠️ Tesseract não instalado — OCR desativado.")
    if avisos:
        with st.expander("Avisos de configuração"):
            for a in avisos:
                st.warning(a)


# ── Tela 2: Seleção de Conta ──────────────────────────────────────────────────

def tela_conta():
    tipo = st.session_state.tipo
    emoji = {"Boleto": "🧾", "Cheque": "📝", "PIX": "💸"}.get(tipo, "📄")
    st.title(f"{emoji} {tipo}")
    st.markdown("**Para qual conta é esse documento?**")
    st.markdown("")

    entidade = st.selectbox(
        "Empresa / Titular",
        ENTIDADES,
        index=ENTIDADES.index(st.session_state.entidade) if st.session_state.entidade in ENTIDADES else 0,
    )
    banco = st.selectbox(
        "Banco",
        BANCOS,
        index=BANCOS.index(st.session_state.banco) if st.session_state.banco in BANCOS else 0,
    )

    tab_name = f"{entidade} - {banco}"
    st.caption(f"Será salvo na aba: **{tab_name}**")

    st.markdown("")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("← Voltar", use_container_width=True):
            st.session_state.tela = "inicio"
            st.rerun()
    with col2:
        if st.button("Continuar →", type="primary", use_container_width=True):
            st.session_state.entidade = entidade
            st.session_state.banco    = banco
            st.session_state.tela     = "pix" if st.session_state.tipo == "PIX" else "captura"
            st.rerun()


# ── Tela 3: Captura ───────────────────────────────────────────────────────────

def tela_captura():
    tipo     = st.session_state.tipo
    entidade = st.session_state.entidade
    banco    = st.session_state.banco
    tab_name = f"{entidade} - {banco}"

    st.title(f"{'🧾' if tipo == 'Boleto' else '📝'} {tipo}")
    st.caption(f"Conta: **{tab_name}**")

    aba_scanner, aba_foto, aba_codigo, aba_manual = st.tabs(
        ["📷 Scanner", "📎 PDF / Foto", "🔢 Linha digitável", "✏️ Digitar"]
    )

    imagem = None

    # ── Aba Scanner ──────────────────────────────────────────────────────────
    with aba_scanner:
        # Exibe mensagem de resultado anterior (vem de init_state após redirecionamento)
        if "_scan_msg" in st.session_state:
            level, msg = st.session_state.pop("_scan_msg")
            if level == "warning":
                st.warning(msg)
            else:
                st.info(msg)

        st.caption("Aponte a câmera para o **QR code PIX** ou **código de barras** do boleto.")

        # Scanner ZXing via HTML/JS
        # Quando lê um código, redireciona para ?scan=CODE&stipo=...&sent=...&sban=...
        # O Python lê esses parâmetros em init_state() (a sessão é zerada pela navegação).
        scanner_html = f"""
        <div id="scanner-box" style="width:100%;max-width:420px;margin:0 auto">
          <video id="video" style="width:100%;border-radius:8px" autoplay playsinline muted></video>
          <p id="status" style="text-align:center;font-size:14px;margin-top:8px;color:#666">
            Iniciando câmera...
          </p>
        </div>
        <script src="https://unpkg.com/@zxing/library@0.19.1/umd/index.min.js"></script>
        <script>
        (function() {{
          const hints = new Map();
          hints.set(ZXing.DecodeHintType.POSSIBLE_FORMATS, [
            ZXing.BarcodeFormat.QR_CODE,
            ZXing.BarcodeFormat.ITF,
            ZXing.BarcodeFormat.CODE_128,
            ZXing.BarcodeFormat.EAN_13,
          ]);
          hints.set(ZXing.DecodeHintType.TRY_HARDER, true);

          const reader = new ZXing.BrowserMultiFormatReader(hints);
          const video  = document.getElementById('video');
          const status = document.getElementById('status');

          reader.decodeFromConstraints(
            {{ video: {{ facingMode: 'environment', width: {{ ideal: 1280 }}, height: {{ ideal: 720 }} }} }},
            video,
            (result, err) => {{
              if (!result) return;
              status.textContent = '✅ Lido! Redirecionando...';
              status.style.color = '#2e7d32';
              reader.reset();

              const encoded = encodeURIComponent(result.getText());
              const base    = window.parent.location.href.split('?')[0];
              const url     = base
                + '?scan='  + encoded
                + '&stipo=' + encodeURIComponent('{tipo}')
                + '&sent='  + encodeURIComponent('{entidade}')
                + '&sban='  + encodeURIComponent('{banco}');
              try {{
                window.parent.location.href = url;
              }} catch(e) {{
                // Fallback: tenta via window.top
                try {{ window.top.location.href = url; }}
                catch(e2) {{
                  // Último recurso: exibe o código para a usuária copiar
                  status.innerHTML =
                    '📋 Cole na aba <b>Linha digitável</b>:<br>'
                    + '<code style="font-size:12px;word-break:break-all">'
                    + result.getText() + '</code>';
                }}
              }}
            }}
          );
        }})();
        </script>
        """
        st.components.v1.html(scanner_html, height=360)

    # ── Aba Foto / PDF ───────────────────────────────────────────────────────
    with aba_foto:
        st.caption("**PDFs funcionam perfeitamente.** Para fotos físicas, segure o celular firme e bem iluminado.")
        arquivo = st.file_uploader(
            "Foto ou PDF do documento",
            type=["jpg", "jpeg", "png", "pdf"],
            label_visibility="collapsed",
        )
        if arquivo:
            dados_arq = arquivo.read()
            if arquivo.name.lower().endswith(".pdf"):
                if tipo == "Boleto":
                    with st.spinner("Lendo PDF..."):
                        extracted = extract_boleto_pdf(dados_arq)
                    # PDF sem texto (ex: foto convertida para PDF) → tenta via imagem
                    _achou_algo = any([
                        extracted.get("valor"),
                        extracted.get("codigo"),
                        extracted.get("beneficiario"),
                    ])
                    if not _achou_algo:
                        with st.spinner("PDF parece ser uma imagem, tentando leitura visual..."):
                            img_pdf = pdf_to_image(dados_arq)
                            extracted = extract_boleto(img_pdf)
                    st.session_state.dados  = extracted
                    st.session_state.imagem = pdf_to_image(dados_arq)
                    st.session_state.tela   = "revisao"
                    st.rerun()
                else:
                    imagem = pdf_to_image(dados_arq)
            else:
                imagem = Image.open(io.BytesIO(dados_arq))

    # ── Aba Linha Digitável ──────────────────────────────────────────────────
    with aba_codigo:
        st.caption("Cole a sequência de números do boleto. Extração 100% precisa — a opção mais confiável.")
        linha = st.text_area("Linha digitável", placeholder="4326 0509 2575 5800 0121...", height=100)
        if st.button("Processar →", key="btn_linha", use_container_width=True):
            if linha.strip():
                raw    = linha.strip()
                digits = re.sub(r"\D", "", raw)
                result = {"tipo": tipo, "beneficiario": "", "valor": "", "vencimento": "", "codigo": "", "observacoes": ""}
                if raw.startswith("000201"):
                    result.update(_parse_pix_emv(raw))
                    result["codigo"] = raw[:60]
                elif len(digits) >= 44:
                    parsed = _parse_boleto_44(digits[:44])
                    if parsed:
                        result.update(parsed)
                        result["codigo"] = digits[:44]
                if not result.get("valor"):
                    result.update(_extrair_dados_texto(raw))
                st.session_state.dados  = result
                st.session_state.imagem = None
                st.session_state.tela   = "revisao"
                st.rerun()

    # ── Aba Manual ───────────────────────────────────────────────────────────
    with aba_manual:
        st.caption("Preencha os dados diretamente sem foto.")
        if st.button("Ir para o formulário →", key="btn_manual", use_container_width=True):
            st.session_state.dados  = {"tipo": tipo, "beneficiario": "", "valor": "", "vencimento": "", "codigo": "", "observacoes": ""}
            st.session_state.imagem = None
            st.session_state.tela   = "revisao"
            st.rerun()

    # Processa imagem carregada (foto ou PDF→imagem)
    if imagem:
        st.session_state.imagem = imagem
        with st.spinner("Lendo imagem... (pode demorar alguns segundos)"):
            if tipo == "Boleto":
                dados = extract_boleto(imagem)
            else:
                dados = extract_cheque(imagem)
        st.session_state.dados = dados

        # Feedback rápido antes de ir para revisão
        achou = bool(dados.get("valor") or dados.get("codigo"))
        if not achou:
            st.session_state["_scan_msg"] = (
                "warning",
                "⚠️ Não foi possível ler o código da imagem. "
                "Preencha os dados manualmente ou tente a aba **Linha digitável**.",
            )

        st.session_state.tela = "revisao"
        st.rerun()

    st.markdown("")
    if st.button("← Voltar"):
        st.session_state.tela = "conta"
        st.rerun()


# ── Tela 4: Revisão ───────────────────────────────────────────────────────────

def tela_revisao():
    tipo     = st.session_state.tipo
    dados    = st.session_state.dados.copy()
    imagem   = st.session_state.imagem
    entidade = st.session_state.entidade
    banco    = st.session_state.banco
    tab_name = f"{entidade} - {banco}"

    st.title("✏️ Confirmar dados")

    # Mensagem vinda do processamento de imagem
    if "_scan_msg" in st.session_state:
        level, msg = st.session_state.pop("_scan_msg")
        if level == "warning":
            st.warning(msg)
        else:
            st.info(msg)

    campos_preenchidos = sum(1 for k in ["beneficiario", "valor", "vencimento"] if dados.get(k, "").strip())
    if campos_preenchidos == 0:
        st.warning("⚠️ Não foi possível ler os dados automaticamente. Preencha os campos abaixo.")
    elif campos_preenchidos < 2:
        st.info("ℹ️ Alguns dados não foram lidos. Confira e complete antes de salvar.")
    else:
        st.caption("Verifique e corrija se necessário antes de salvar.")

    # Badge de conta
    st.markdown(
        f'<div style="background:#E3F2FD;padding:8px 14px;border-radius:8px;display:inline-block;margin-bottom:12px">'
        f'🏦 <b>{tab_name}</b></div>',
        unsafe_allow_html=True,
    )

    if imagem:
        st.image(imagem, width=300)

    st.markdown("---")

    beneficiario = st.text_input("Beneficiário / Empresa",       value=dados.get("beneficiario", ""))
    valor        = st.text_input("Valor (R$)",                   value=dados.get("valor", ""),       placeholder="Ex: 189,90")
    vencimento   = st.text_input("Vencimento (DD/MM/AAAA)",      value=dados.get("vencimento", ""),  placeholder="Ex: 30/05/2026")
    codigo       = st.text_input("Código / Número",              value=dados.get("codigo", ""))
    observacoes  = st.text_area("Observações (opcional)",        value=dados.get("observacoes", ""), height=80)

    st.markdown("")

    pode_salvar = bool(valor.strip() or vencimento.strip())
    if not pode_salvar:
        st.info("Preencha ao menos o **Valor** ou o **Vencimento** para salvar.")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("← Voltar"):
            st.session_state.tela = "captura"
            st.rerun()
    with col2:
        salvar_btn = st.button("💾 Salvar", disabled=not pode_salvar, type="primary")

    if salvar_btn:
        if not SPREADSHEET_ID:
            st.error("ID da planilha não configurado.")
            return

        dados_finais = {
            "tipo":        tipo,
            "beneficiario": beneficiario,
            "valor":       valor,
            "vencimento":  vencimento,
            "codigo":      codigo,
            "observacoes": observacoes,
        }

        drive_error = None
        with st.spinner(f"Salvando em '{tab_name}'..."):
            # Faz upload da foto para o Drive (se houver imagem)
            foto_url = ""
            if imagem:
                try:
                    buf = io.BytesIO()
                    imagem.save(buf, format="JPEG", quality=85)
                    nome_arq = f"{re.sub(chr(32), '_', beneficiario[:20])}_{date.today().strftime('%d%m%Y')}.jpg"
                    foto_url = upload_imagem_drive(buf.getvalue(), nome_arq)
                except Exception as _drive_err:
                    drive_error = str(_drive_err)

            sucesso = append_row(SPREADSHEET_ID, dados_finais, tab_name, foto_url=foto_url)

        if sucesso:
            if drive_error:
                # Guarda o erro para mostrar na tela de confirmação
                # (st.warning() dentro do spinner some antes de renderizar)
                st.session_state["_drive_warning"] = drive_error
            st.session_state.tela   = "confirmacao"
            st.session_state.dados  = {}
            st.session_state.imagem = None
            st.rerun()


# ── Tela 5: Confirmação ───────────────────────────────────────────────────────

def tela_confirmacao():
    tab_name = f"{st.session_state.entidade} - {st.session_state.banco}"
    st.title("✅ Salvo!")
    st.success(f"Dados salvos na aba **{tab_name}** com sucesso.")

    # Mostra erro de Drive (se o upload da foto falhou)
    if "_drive_warning" in st.session_state:
        warn = st.session_state.pop("_drive_warning")
        st.warning(
            f"⚠️ **A foto não foi salva** — erro no Google Drive:\n\n"
            f"```\n{warn}\n```\n\n"
            "Provável causa: a **Google Drive API** não está ativada no Google Cloud. "
            "[Clique aqui para ativar](https://console.cloud.google.com/apis/library/drive.googleapis.com)"
        )
    else:
        st.balloons()

    st.markdown("")
    if st.button("➕ Novo documento", type="primary"):
        st.session_state.tela = "inicio"
        st.rerun()


# ── Tela 6: Pendentes ─────────────────────────────────────────────────────────

def tela_pendentes():
    col_titulo, col_voltar = st.columns([4, 1])
    with col_titulo:
        st.title("📋 Pendentes")
    with col_voltar:
        st.markdown("<div style='padding-top:18px'>", unsafe_allow_html=True)
        if st.button("← Voltar", key="voltar_top"):
            st.session_state.tela = "inicio"
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    if not SPREADSHEET_ID:
        st.error("ID da planilha não configurado.")
        return

    with st.spinner("Carregando..."):
        atualizar_vencidos(SPREADSHEET_ID)
        rows = get_all_rows(SPREADSHEET_ID)

    pendentes = [r for r in rows if r.get("Status", "").strip() in ("Previsão", "Vencido", "Pendente", "")]

    if not pendentes:
        st.success("Nenhum boleto ou cheque pendente!")
        st.markdown("")
        if st.button("← Voltar"):
            st.session_state.tela = "inicio"
            st.rerun()
        return

    vencidos  = [r for r in pendentes if r.get("Status", "").strip() == "Vencido"]
    previsoes = [r for r in pendentes if r.get("Status", "").strip() != "Vencido"]

    st.caption(f"{len(pendentes)} item(ns) — {len(vencidos)} vencido(s)")
    st.markdown("")

    # Vencidos primeiro, depois previsões
    for row in (vencidos + previsoes):
        beneficiario = row.get("Beneficiário", "") or "Sem nome"
        vencimento   = row.get("Vencimento", "")   or "—"
        tipo         = row.get("Tipo", "")
        tab_name     = row.get("_tab_name", "")
        row_idx      = row["_row_index"]
        codigo       = str(row.get("Código/Número", "")).strip()
        foto_url     = str(row.get("Foto", "")).strip()
        comp_url     = str(row.get("Comprovante", "")).strip()

        _v = row.get("Valor (R$)", "")
        if isinstance(_v, float):
            valor = f"{_v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        elif isinstance(_v, int) and _v > 0:
            valor = f"{_v / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        else:
            valor = str(_v).strip() if _v else "—"

        emoji_prazo = "🟢"
        try:
            venc_date = datetime.strptime(vencimento, "%d/%m/%Y").date()
            dias = (venc_date - date.today()).days
            if dias < 0:
                emoji_prazo = "🔴"
            elif dias <= 3:
                emoji_prazo = "🟠"
        except Exception:
            pass

        tipo_emoji = {"Boleto": "🧾", "Cheque": "📝", "PIX": "💸"}.get(tipo, "📄")
        label_exp  = f"{emoji_prazo} {beneficiario}  ·  {tipo_emoji} R$ {valor}  ·  {vencimento}"

        with st.expander(label_exp):
            st.caption(f"🏦 {tab_name}")
            st.markdown("")

            # ── Código ───────────────────────────────────────────────────────
            if codigo:
                st.markdown("**🔢 Código para pagamento:**")
                st.code(codigo, language=None)

            # ── Foto do documento ────────────────────────────────────────────
            if foto_url:
                st.markdown(f"**📸 Foto:** [Ver documento]({foto_url})")
            elif tipo != "PIX":
                st.caption("📸 Foto não disponível (cadastrado antes dessa funcionalidade)")

            st.markdown("")

            # ── Comprovante ──────────────────────────────────────────────────
            if comp_url:
                st.markdown(f"✅ **Comprovante:** [Ver comprovante]({comp_url})")
            else:
                comp_file = st.file_uploader(
                    "📎 Anexar comprovante (opcional)",
                    type=["jpg", "jpeg", "png"],
                    key=f"comp_{tab_name}_{row_idx}",
                    label_visibility="visible",
                )

            st.markdown("")

            # ── Botões ───────────────────────────────────────────────────────
            col1, col2 = st.columns(2)
            with col1:
                if st.button("✅ Pago", key=f"pago_{tab_name}_{row_idx}", use_container_width=True, type="primary"):
                    with st.spinner("Salvando..."):
                        # Upload do comprovante se houver arquivo
                        if not comp_url:
                            comp_file_val = st.session_state.get(f"comp_{tab_name}_{row_idx}")
                            if comp_file_val is not None:
                                nome_comp = f"comp_{re.sub(chr(32),'_',beneficiario[:20])}_{date.today().strftime('%d%m%Y')}.jpg"
                                url_salva = upload_imagem_drive(comp_file_val.read(), nome_comp)
                                if url_salva:
                                    update_comprovante(SPREADSHEET_ID, tab_name, row_idx, url_salva)
                        update_status(SPREADSHEET_ID, tab_name, row_idx, "Pago")
                    st.rerun()
            with col2:
                if st.button("❌ Cancelar", key=f"cancel_{tab_name}_{row_idx}", use_container_width=True):
                    with st.spinner("Atualizando..."):
                        update_status(SPREADSHEET_ID, tab_name, row_idx, "Cancelado")
                    st.rerun()

    st.markdown("")
    if st.button("← Voltar"):
        st.session_state.tela = "inicio"
        st.rerun()


# ── Tela 7: Lembretes de Conta Mensal ────────────────────────────────────────

SECOES_LEMBRETES = ["Pessoal", "RBM", "Vithall", "Anaelena"]

# ── Dados: Lembretes Pessoal ─────────────────────────────────────────────────
LEMBRETES_PESSOAL = [
    {"Descrição": "Boletos Viagem 2 - Parcela 03/08",                   "Previsão de vencimento": "01/05/2026", "Situação": "PAGO"},
    {"Descrição": "Assistencia funeral - Diersmann",                    "Previsão de vencimento": "05/05/2026", "Situação": "PAGO"},
    {"Descrição": "Parcela de casa - volta pagto em 05/2026",           "Previsão de vencimento": "05/05/2026", "Situação": "COM ANA"},
    {"Descrição": "Viagem Pedro e Ana final de ano - Leticia 08/15",    "Previsão de vencimento": "07/05/2026", "Situação": "PAGO"},
    {"Descrição": "IPTU IMBE 2026 05/09",                               "Previsão de vencimento": "08/05/2026", "Situação": "PAGO"},
    {"Descrição": "Água Rua Taquara Imbé",                              "Previsão de vencimento": "09/05/2026", "Situação": "PAGO"},
    {"Descrição": "Parcela Karate Gabi",                                "Previsão de vencimento": "10/05/2026", "Situação": "COM ANA"},
    {"Descrição": "Cuidadora Cleia - VIVI",                             "Previsão de vencimento": "10/05/2026", "Situação": "PAGO"},
    {"Descrição": "Vigilancia casa Lajeado Itec",                       "Previsão de vencimento": "10/05/2026", "Situação": "PAGO"},
    {"Descrição": "Jardinheiro",                                        "Previsão de vencimento": "10/05/2026", "Situação": "COM ANA"},
    {"Descrição": "Uninter Pedro",                                      "Previsão de vencimento": "10/05/2026", "Situação": "COM ANA"},
    {"Descrição": "Brasrede internet casa",                             "Previsão de vencimento": "10/05/2026", "Situação": "PAGO"},
    {"Descrição": "Parcela Carro Palio 32/36",                          "Previsão de vencimento": "11/05/2026", "Situação": "PAGO"},
    {"Descrição": "Seguro Pedro Etios Cartão de Crédito 10x fatura",    "Previsão de vencimento": "15/05/2026", "Situação": "COM ANA"},
    {"Descrição": "Seguro Rafa Palio Cartão Porto Bank 10x",            "Previsão de vencimento": "15/05/2026", "Situação": "COM ANA"},
    {"Descrição": "Seguro Rafa Crossfox Cartão Porto Bank 10x",         "Previsão de vencimento": "15/05/2026", "Situação": "COM ANA"},
    {"Descrição": "Cartão de crédito PF",                               "Previsão de vencimento": "15/05/2026", "Situação": "COM ANA"},
    {"Descrição": "Limpeza piscina",                                    "Previsão de vencimento": "15/05/2026", "Situação": "COM ANA"},
    {"Descrição": "Energia eletrica Casa Certel",                       "Previsão de vencimento": "15/05/2026", "Situação": "GRUPO"},
    {"Descrição": "IPTU Casa - ROBINSON 2026 01/08",                    "Previsão de vencimento": "15/05/2026", "Situação": "GRUPO"},
    {"Descrição": "Parcelamento Dívida Grall - Rafa parcela 16/36",     "Previsão de vencimento": "18/05/2026", "Situação": "GRUPO"},
    {"Descrição": "IPTU CASA 2024/2025 - PARCELAS 01/08",               "Previsão de vencimento": "22/05/2026", "Situação": "GRUPO"},
    {"Descrição": "Fotos Hanna 10k - 1k entrada - 35x257,00",           "Previsão de vencimento": "25/05/2026", "Situação": "COM ANA"},
    {"Descrição": "Negociação Antiga Stilocar 02/20",                   "Previsão de vencimento": "25/05/2026", "Situação": "GRUPO"},
    {"Descrição": "Energia eletrica imbé - ceee",                       "Previsão de vencimento": "27/05/2026", "Situação": "GRUPO"},
    {"Descrição": "Água casa",                                          "Previsão de vencimento": "28/05/2026", "Situação": "GRUPO"},
    {"Descrição": "Serasa Ana Banco Bradesco Parcelado 19/48",          "Previsão de vencimento": "29/05/2026", "Situação": "GRUPO"},
    {"Descrição": "Serasa Ana Banco Itau 19/48",                        "Previsão de vencimento": "29/05/2026", "Situação": "GRUPO"},
    {"Descrição": "Vigilancia casa Imbé Suat",                          "Previsão de vencimento": "30/05/2026", "Situação": "GRUPO"},
    {"Descrição": "Parcelamento Dívida Stilocar - Rafa PARC 16/19",     "Previsão de vencimento": "30/05/2026", "Situação": "SOLICITADO"},
]


def tela_lembretes():
    import pandas as pd

    col_titulo, col_voltar = st.columns([4, 1])
    with col_titulo:
        st.title("📅 Lembretes de Conta Mensal")
    with col_voltar:
        st.markdown("<div style='padding-top:18px'>", unsafe_allow_html=True)
        if st.button("← Voltar", key="voltar_lembretes_top"):
            st.session_state.tela = "inicio"
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    st.caption("Contas fixas mensais que precisam ser fotografadas e registradas.")
    st.markdown("")

    secao = st.session_state.get("lembrete_secao", SECOES_LEMBRETES[0])

    cols = st.columns(len(SECOES_LEMBRETES))
    for i, nome in enumerate(SECOES_LEMBRETES):
        with cols[i]:
            if st.button(nome, use_container_width=True,
                         type="primary" if secao == nome else "secondary"):
                st.session_state.lembrete_secao = nome
                st.rerun()

    st.markdown("---")

    if secao == "Pessoal":
        df = pd.DataFrame(LEMBRETES_PESSOAL)
        st.caption(f"{len(df)} contas · {len(df[df['Situação']=='PAGO'])} pagas · "
                   f"{len(df[df['Situação']!='PAGO'])} pendentes")
        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Descrição":              st.column_config.TextColumn(width="large"),
                "Previsão de vencimento": st.column_config.TextColumn(width="medium"),
                "Situação":               st.column_config.TextColumn(width="small"),
            },
        )
    else:
        st.info(f"Seção **{secao}** — em breve.")

    st.markdown("")
    if st.button("← Voltar"):
        st.session_state.tela = "inicio"
        st.rerun()


# ── Tela 8: Configuração de Alertas ──────────────────────────────────────────

def tela_config():
    st.title("🔔 Configurar Alertas")
    st.markdown(
        "Todo dia às **07:30**, cada boleto não pago gera uma notificação "
        "no seu celular — 1 dia antes, no dia do vencimento e todo dia depois disso até pagar."
    )
    st.markdown("")

    # Carrega config salva (sem spinner — evita conflito de contexto Streamlit)
    config = {}
    if SPREADSHEET_ID:
        try:
            config = get_config(SPREADSHEET_ID)
        except Exception:
            config = {}

    # ── Passo 1: instalar ntfy ────────────────────────────────────────────────
    st.subheader("📱 Passo 1 — Instalar o app ntfy")
    st.markdown("""
Baixe o app **ntfy** no celular (gratuito, sem cadastro):
- 🤖 Android: [play.google.com → ntfy](https://play.google.com/store/apps/details?id=io.heckel.ntfy)
- 🍎 iPhone: [apps.apple.com → ntfy](https://apps.apple.com/app/ntfy/id1625396347)

Depois de instalar, toque em **＋** e coloque o tópico que você vai criar abaixo.
""")

    # ── Passo 2: tópico ntfy ─────────────────────────────────────────────────
    st.subheader("🔑 Passo 2 — Definir seu tópico")
    st.caption("É como um canal privado. Use um nome único para que só você receba.")

    saved_topic = config.get("ntfy_topic", "")
    if "_topic_sugerido" in st.session_state:
        saved_topic = st.session_state["_topic_sugerido"]

    col1, col2 = st.columns([3, 1])
    with col1:
        ntfy_topic = st.text_input(
            "Nome do tópico",
            value=saved_topic,
            placeholder="Ex: boletos-familia-2026-abc",
            label_visibility="collapsed",
        )
    with col2:
        if st.button("🎲 Sortear", use_container_width=True):
            aleatorio = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
            st.session_state["_topic_sugerido"] = f"boletos-{aleatorio}"
            st.rerun()

    if "_topic_sugerido" in st.session_state and ntfy_topic == st.session_state.get("_topic_sugerido"):
        st.info(f"Tópico sugerido: **{ntfy_topic}** — copie este nome exato no app ntfy!")

    # ── Botões Salvar / Testar ────────────────────────────────────────────────
    st.markdown("")
    col1, col2 = st.columns(2)

    with col1:
        if st.button("💾 Salvar", type="primary", use_container_width=True):
            if not ntfy_topic.strip():
                st.error("Digite um tópico ntfy.")
            elif not SPREADSHEET_ID:
                st.error("ID da planilha não configurado.")
            else:
                try:
                    ok, err_msg = save_config(SPREADSHEET_ID, ntfy_topic.strip())
                except Exception as e:
                    ok, err_msg = False, f"{type(e).__name__}: {e}"
                if ok:
                    st.session_state.pop("_topic_sugerido", None)
                    st.success("✅ Tópico salvo! Alertas serão enviados diariamente às 07:30.")
                else:
                    st.error(f"Erro ao salvar: {err_msg}")

    with col2:
        if st.button(
            "📨 Testar agora",
            disabled=not ntfy_topic.strip(),
            use_container_width=True,
        ):
            try:
                resp = _requests.post(
                    f"https://ntfy.sh/{ntfy_topic.strip()}",
                    data="✅ Configuração funcionando! Alertas de boletos chegarão aqui às 07:30.".encode("utf-8"),
                    headers={
                        "Title": "Teste — Boletos & Cheques".encode("utf-8"),
                        "Tags":  "white_check_mark,bell",
                    },
                    timeout=10,
                )
                if resp.status_code == 200:
                    st.success("📨 Notificação de teste enviada! Verifique o celular.")
                else:
                    st.error(f"Erro ao enviar (HTTP {resp.status_code}). Verifique o tópico.")
            except Exception as e:
                st.error(f"Erro de conexão: {e}")

    # ── Passo 3: GitHub Secrets (só uma vez) ─────────────────────────────────
    st.markdown("")
    with st.expander("⚙️ Passo 3 — Ativar envio automático (só uma vez, no computador)"):
        st.markdown("""
As notificações são disparadas pelo **GitHub Actions** todo dia às 07:30 —
funcionam mesmo com o app fechado.

**Para ativar:**

1. Acesse este link:
   👉 [Abrir configurações de secrets do GitHub](https://github.com/pedrixteste/boleto-tracker/settings/secrets/actions)

2. Clique em **New repository secret** e adicione **2 secrets**:

   | Nome | Valor |
   |------|-------|
   | `SPREADSHEET_ID` | `1-Hi9HR3PTOFxJigMmpZaTrSccjFpGDSbS8pNHTqereg` |
   | `GCP_SERVICE_ACCOUNT` | conteúdo inteiro do arquivo `credentials.json` |

3. Pronto! A partir daí as notificações são automáticas todo dia às 07:30.

---
💡 **Testar manualmente:** Acesse o repositório → aba **Actions** →
_"Notificações de Boletos Pendentes"_ → botão **Run workflow**.
""")

    st.markdown("")
    if st.button("← Voltar"):
        st.session_state.tela = "inicio"
        st.rerun()


# ── Tela PIX ─────────────────────────────────────────────────────────────────

def tela_pix():
    entidade = st.session_state.entidade
    banco    = st.session_state.banco
    tab_name = f"{entidade} - {banco}"

    # Inicializa lista de datas ao entrar na tela
    if "pix_datas" not in st.session_state:
        st.session_state.pix_datas = []

    col_titulo, col_voltar = st.columns([4, 1])
    with col_titulo:
        st.title("💸 Registrar PIX")
    with col_voltar:
        st.markdown("<div style='padding-top:18px'>", unsafe_allow_html=True)
        if st.button("← Voltar", key="voltar_pix_top"):
            st.session_state.pix_datas = []
            st.session_state.tela = "conta"
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    st.caption(f"Conta: **{tab_name}**")
    st.markdown("")

    chave = st.text_input(
        "🔑 Chave PIX",
        placeholder="CPF, e-mail, telefone, chave aleatória...",
    )

    beneficiario = st.text_input(
        "👤 Beneficiário",
        placeholder="Nome da pessoa ou empresa",
    )

    valor = st.text_input(
        "💰 Valor (R$)",
        placeholder="Ex: 150,00  —  opcional",
    )

    observacoes = st.text_area(
        "📝 Observações",
        placeholder="Opcional — ex: referente a...",
        height=80,
    )

    # ── Datas de pagamento ────────────────────────────────────────────────────
    st.markdown("**📅 Datas para pagar**")
    st.caption("Adicione uma ou várias datas para cadastrar os próximos pagamentos de uma vez.")

    col_data, col_add = st.columns([3, 1])
    with col_data:
        nova_data = st.date_input(
            "Data",
            value=date.today(),
            format="DD/MM/YYYY",
            label_visibility="collapsed",
        )
    with col_add:
        st.markdown("<div style='padding-top:4px'>", unsafe_allow_html=True)
        if st.button("➕ Adicionar", use_container_width=True):
            data_str = nova_data.strftime("%d/%m/%Y")
            if data_str not in st.session_state.pix_datas:
                st.session_state.pix_datas.append(data_str)
                st.session_state.pix_datas.sort(
                    key=lambda d: datetime.strptime(d, "%d/%m/%Y")
                )
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    # Lista de datas adicionadas
    if st.session_state.pix_datas:
        st.markdown("")
        for i, d in enumerate(list(st.session_state.pix_datas)):
            col_d, col_rm = st.columns([4, 1])
            with col_d:
                st.markdown(
                    f"<div style='background:#E8F5E9;padding:8px 12px;border-radius:8px;"
                    f"font-size:0.95rem'>📅 {d}</div>",
                    unsafe_allow_html=True,
                )
            with col_rm:
                if st.button("✕", key=f"rm_data_{i}", use_container_width=True):
                    st.session_state.pix_datas.pop(i)
                    st.rerun()
        st.markdown("")
    else:
        st.info("Nenhuma data adicionada. Clique em ➕ para adicionar.")
        st.markdown("")

    # ── Salvar ────────────────────────────────────────────────────────────────
    n = len(st.session_state.pix_datas)
    label_btn = f"💾 Salvar {n} pagamento(s)" if n > 0 else "💾 Salvar PIX"

    if st.button(label_btn, type="primary", use_container_width=True, disabled=(n == 0)):
        if not chave.strip():
            st.error("Informe a chave PIX.")
            return
        if not beneficiario.strip():
            st.error("Informe o beneficiário.")
            return

        with st.spinner(f"Salvando {n} pagamento(s)..."):
            erros = 0
            for data_str in st.session_state.pix_datas:
                dados = {
                    "tipo":         "PIX",
                    "beneficiario": beneficiario.strip(),
                    "valor":        valor.strip(),
                    "vencimento":   data_str,
                    "codigo":       chave.strip(),
                    "observacoes":  observacoes.strip(),
                }
                if not append_row(SPREADSHEET_ID, dados, tab_name):
                    erros += 1

        if erros == 0:
            st.session_state.pix_datas = []
            st.session_state.tela = "confirmacao"
            st.rerun()
        else:
            st.error(f"{erros} pagamento(s) não foram salvos. Tente novamente.")


# ── Roteador ──────────────────────────────────────────────────────────────────

init_state()

telas = {
    "inicio":       tela_inicio,
    "conta":        tela_conta,
    "captura":      tela_captura,
    "revisao":      tela_revisao,
    "confirmacao":  tela_confirmacao,
    "pendentes":    tela_pendentes,
    "lembretes":    tela_lembretes,
    "config":       tela_config,
    "pix":          tela_pix,
}

telas[st.session_state.tela]()
