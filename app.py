import streamlit as st
from PIL import Image
from datetime import date
import io
import re

import fitz  # PyMuPDF

from extractor import extract_boleto, extract_boleto_pdf, extract_cheque, TESSERACT_OK, PYZBAR_OK
from extractor import _parse_boleto_44, _parse_pix_emv, _extrair_dados_texto
from sheets import append_row, get_all_rows, update_status, ENTIDADES, BANCOS

try:
    from streamlit_qrcode_scanner import qrcode_scanner
    SCANNER_OK = True
except ImportError:
    SCANNER_OK = False


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


def init_state():
    defaults = {
        "tela": "inicio",
        "tipo": None,
        "entidade": ENTIDADES[0],
        "banco": BANCOS[0],
        "dados": {},
        "imagem": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


# ── Tela 1: Início ────────────────────────────────────────────────────────────

def tela_inicio():
    st.title("📄 Boletos & Cheques")
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

    st.markdown("")
    if st.button("📋 Ver pendentes", use_container_width=True):
        st.session_state.tela = "pendentes"
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
    st.title(f"{'🧾' if tipo == 'Boleto' else '📝'} {tipo}")
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
            st.session_state.banco = banco
            st.session_state.tela = "captura"
            st.rerun()


# ── Tela 3: Captura ───────────────────────────────────────────────────────────

def tela_captura():
    tipo = st.session_state.tipo
    tab_name = f"{st.session_state.entidade} - {st.session_state.banco}"
    st.title(f"{'🧾' if tipo == 'Boleto' else '📝'} {tipo}")
    st.caption(f"Conta: **{tab_name}**")
    st.markdown(f"Tire uma foto ou envie uma imagem do **{tipo.lower()}**.")

    aba_scanner, aba_foto, aba_codigo, aba_manual = st.tabs(["📷 Scanner", "📎 PDF / Foto", "🔢 Linha digitável", "✏️ Digitar"])

    imagem = None

    with aba_scanner:
        if not SCANNER_OK:
            st.warning("Scanner não disponível. Use a aba PDF/Foto.")
        else:
            st.caption("Aponte a câmera para o **QR code** ou **código de barras** do boleto.")
            raw = qrcode_scanner(key=f"scanner_{tipo}")
            if raw:
                result = {"tipo": tipo, "beneficiario": "", "valor": "", "vencimento": "", "codigo": "", "observacoes": ""}
                if raw.startswith("000201"):
                    result.update(_parse_pix_emv(raw))
                    result["codigo"] = raw[:60] + "..." if len(raw) > 60 else raw
                else:
                    digits = re.sub(r"\D", "", raw)
                    if len(digits) >= 44:
                        parsed = _parse_boleto_44(digits[:44])
                        if parsed:
                            result.update(parsed)
                            result["codigo"] = digits[:44]
                if not result.get("valor"):
                    result.update(_extrair_dados_texto(raw))
                st.session_state.dados = result
                st.session_state.imagem = None
                st.session_state.tela = "revisao"
                st.rerun()

    with aba_foto:
        st.caption("Melhor para PDFs digitais. Fotos físicas podem ter qualidade reduzida pelo celular.")
        arquivo = st.file_uploader(
            "Foto ou PDF do documento",
            type=["jpg", "jpeg", "png", "pdf"],
            label_visibility="collapsed",
        )
        if arquivo:
            dados = arquivo.read()
            if arquivo.name.lower().endswith(".pdf"):
                if tipo == "Boleto":
                    with st.spinner("Lendo PDF..."):
                        extracted = extract_boleto_pdf(dados)
                    st.session_state.dados = extracted
                    st.session_state.imagem = pdf_to_image(dados)
                    st.session_state.tela = "revisao"
                    st.rerun()
                else:
                    imagem = pdf_to_image(dados)
            else:
                imagem = Image.open(io.BytesIO(dados))

    with aba_codigo:
        st.caption("Cole a sequência de números do boleto (linha digitável). Extração 100% precisa.")
        linha = st.text_area("Linha digitável", placeholder="4326 0509 2575 5800 0121...", height=100)
        if st.button("Processar →", key="btn_linha", use_container_width=True):
            if linha.strip():
                raw = linha.strip()
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
                st.session_state.dados = result
                st.session_state.imagem = None
                st.session_state.tela = "revisao"
                st.rerun()

    with aba_manual:
        st.caption("Preencha os dados diretamente sem foto.")
        if st.button("Ir para o formulário →", key="btn_manual", use_container_width=True):
            st.session_state.dados = {"tipo": tipo, "beneficiario": "", "valor": "", "vencimento": "", "codigo": "", "observacoes": ""}
            st.session_state.imagem = None
            st.session_state.tela = "revisao"
            st.rerun()

    if imagem:
        st.session_state.imagem = imagem
        with st.spinner("Lendo imagem..."):
            if tipo == "Boleto":
                dados = extract_boleto(imagem)
            else:
                dados = extract_cheque(imagem)
        st.session_state.dados = dados
        st.session_state.tela = "revisao"
        st.rerun()

    st.markdown("")
    if st.button("← Voltar"):
        st.session_state.tela = "conta"
        st.rerun()


# ── Tela 4: Revisão ───────────────────────────────────────────────────────────

def tela_revisao():
    tipo = st.session_state.tipo
    dados = st.session_state.dados.copy()
    imagem = st.session_state.imagem
    entidade = st.session_state.entidade
    banco = st.session_state.banco
    tab_name = f"{entidade} - {banco}"

    st.title("✏️ Confirmar dados")

    # Aviso se poucos campos foram preenchidos automaticamente
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

    beneficiario = st.text_input("Beneficiário / Empresa", value=dados.get("beneficiario", ""))
    valor = st.text_input("Valor (R$)", value=dados.get("valor", ""), placeholder="Ex: 189,90")
    vencimento = st.text_input("Vencimento (DD/MM/AAAA)", value=dados.get("vencimento", ""), placeholder="Ex: 30/05/2026")
    codigo = st.text_input("Código / Número", value=dados.get("codigo", ""))
    observacoes = st.text_area("Observações (opcional)", value=dados.get("observacoes", ""), height=80)

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
            "tipo": tipo,
            "beneficiario": beneficiario,
            "valor": valor,
            "vencimento": vencimento,
            "codigo": codigo,
            "observacoes": observacoes,
        }

        with st.spinner(f"Salvando em '{tab_name}'..."):
            sucesso = append_row(SPREADSHEET_ID, dados_finais, tab_name)

        if sucesso:
            st.session_state.tela = "confirmacao"
            st.session_state.dados = {}
            st.session_state.imagem = None
            st.rerun()


# ── Tela 5: Confirmação ───────────────────────────────────────────────────────

def tela_confirmacao():
    tab_name = f"{st.session_state.entidade} - {st.session_state.banco}"
    st.title("✅ Salvo!")
    st.success(f"Dados salvos na aba **{tab_name}** com sucesso.")
    st.balloons()

    st.markdown("")
    if st.button("➕ Novo documento", type="primary"):
        st.session_state.tela = "inicio"
        st.rerun()


# ── Tela 6: Pendentes ─────────────────────────────────────────────────────────

def tela_pendentes():
    st.title("📋 Pendentes")

    if not SPREADSHEET_ID:
        st.error("ID da planilha não configurado.")
        return

    with st.spinner("Carregando..."):
        rows = get_all_rows(SPREADSHEET_ID)

    pendentes = [r for r in rows if r.get("Status", "").strip() in ("Pendente", "")]

    if not pendentes:
        st.success("Nenhum boleto ou cheque pendente!")
        st.markdown("")
        if st.button("← Voltar"):
            st.session_state.tela = "inicio"
            st.rerun()
        return

    st.caption(f"{len(pendentes)} item(ns) pendente(s)")
    st.markdown("")

    for row in pendentes:
        beneficiario = row.get("Beneficiário", "") or "Sem nome"
        valor = row.get("Valor (R$)", "") or "—"
        vencimento = row.get("Vencimento", "") or "—"
        tipo = row.get("Tipo", "")
        tab_name = row.get("_tab_name", "")
        row_idx = row["_row_index"]

        cor = "#FFF8E1"
        emoji_prazo = "🟡"
        try:
            from datetime import datetime
            venc_date = datetime.strptime(vencimento, "%d/%m/%Y").date()
            dias = (venc_date - date.today()).days
            if dias < 0:
                cor, emoji_prazo = "#FFEBEE", "🔴"
            elif dias <= 3:
                cor, emoji_prazo = "#FFF3E0", "🟠"
            else:
                cor, emoji_prazo = "#E8F5E9", "🟢"
        except Exception:
            pass

        st.markdown(
            f"""<div style="background:{cor};padding:12px 16px;border-radius:10px;margin-bottom:6px">
            <b>{emoji_prazo} {beneficiario}</b><br>
            <span style="font-size:0.85rem">{tipo} · R$ {valor} · Vence: {vencimento}</span><br>
            <span style="font-size:0.78rem;color:#555">🏦 {tab_name}</span>
            </div>""",
            unsafe_allow_html=True,
        )

        col1, col2 = st.columns(2)
        with col1:
            if st.button("✅ Pago", key=f"pago_{tab_name}_{row_idx}", use_container_width=True):
                with st.spinner("Atualizando..."):
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


# ── Roteador ──────────────────────────────────────────────────────────────────

init_state()

telas = {
    "inicio": tela_inicio,
    "conta": tela_conta,
    "captura": tela_captura,
    "revisao": tela_revisao,
    "confirmacao": tela_confirmacao,
    "pendentes": tela_pendentes,
}

telas[st.session_state.tela]()
