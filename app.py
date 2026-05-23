import streamlit as st
from PIL import Image
import io

import fitz  # PyMuPDF

from extractor import extract_boleto, extract_boleto_pdf, extract_cheque, TESSERACT_OK, PYZBAR_OK
from sheets import append_row


def pdf_to_image(pdf_bytes: bytes) -> Image.Image:
    """Converte a primeira página de um PDF em imagem PIL."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]
    mat = fitz.Matrix(2.0, 2.0)  # 2x zoom para melhor qualidade
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

# ID da planilha Google Sheets — preencher após criar a planilha
SPREADSHEET_ID = st.secrets.get("spreadsheet_id", "") if hasattr(st, "secrets") else ""

# Tenta ler do secrets ou pede ao usuário
if not SPREADSHEET_ID:
    try:
        SPREADSHEET_ID = st.secrets["spreadsheet_id"]
    except Exception:
        SPREADSHEET_ID = ""

# CSS para melhorar aparência no celular
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
    .status-box {
        padding: 1rem;
        border-radius: 8px;
        margin: 0.5rem 0;
        font-size: 0.9rem;
    }
</style>
""", unsafe_allow_html=True)


def init_state():
    defaults = {
        "tela": "inicio",
        "tipo": None,
        "dados": {},
        "imagem": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def tela_inicio():
    st.title("📄 Boletos & Cheques")
    st.markdown("**O que você quer registrar?**")
    st.markdown("")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("🧾 Boleto", use_container_width=True):
            st.session_state.tipo = "Boleto"
            st.session_state.tela = "captura"
            st.rerun()
    with col2:
        if st.button("📝 Cheque", use_container_width=True):
            st.session_state.tipo = "Cheque"
            st.session_state.tela = "captura"
            st.rerun()

    # Aviso de dependências ausentes
    avisos = []
    if not PYZBAR_OK:
        avisos.append("⚠️ pyzbar não instalado — leitura de código de barras desativada.")
    if not TESSERACT_OK:
        avisos.append("⚠️ Tesseract não instalado — OCR de texto desativado.")
    if avisos:
        with st.expander("Avisos de configuração"):
            for a in avisos:
                st.warning(a)


def tela_captura():
    tipo = st.session_state.tipo
    st.title(f"{'🧾' if tipo == 'Boleto' else '📝'} {tipo}")
    st.markdown(f"Tire uma foto ou envie uma imagem do **{tipo.lower()}**.")

    tab_cam, tab_upload = st.tabs(["📷 Câmera", "🖼️ Galeria"])

    imagem = None
    with tab_cam:
        foto = st.camera_input("Tire a foto")
        if foto:
            imagem = Image.open(io.BytesIO(foto.getvalue()))

    with tab_upload:
        arquivo = st.file_uploader("Escolha uma imagem ou PDF", type=["jpg", "jpeg", "png", "pdf"])
        if arquivo:
            dados = arquivo.read()
            if arquivo.name.lower().endswith(".pdf"):
                # PDF digital: extrai texto direto (muito mais preciso que OCR)
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
        st.session_state.tela = "inicio"
        st.rerun()


def tela_revisao():
    tipo = st.session_state.tipo
    dados = st.session_state.dados.copy()
    imagem = st.session_state.imagem

    st.title("✏️ Confirmar dados")
    st.caption("Verifique e corrija se necessário antes de salvar.")

    # Miniatura da imagem
    if imagem:
        st.image(imagem, width=300)

    st.markdown("---")

    beneficiario = st.text_input("Beneficiário / Empresa", value=dados.get("beneficiario", ""))
    valor = st.text_input("Valor (R$)", value=dados.get("valor", ""), placeholder="Ex: 189,90")
    vencimento = st.text_input("Vencimento (DD/MM/AAAA)", value=dados.get("vencimento", ""), placeholder="Ex: 30/05/2026")
    codigo = st.text_input("Código / Número", value=dados.get("codigo", ""))
    observacoes = st.text_area("Observações (opcional)", value=dados.get("observacoes", ""), height=80)

    st.markdown("")

    # Validação básica antes de salvar
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
            st.error("ID da planilha não configurado. Consulte o README para configurar.")
            return

        dados_finais = {
            "tipo": tipo,
            "beneficiario": beneficiario,
            "valor": valor,
            "vencimento": vencimento,
            "codigo": codigo,
            "observacoes": observacoes,
        }

        with st.spinner("Salvando na planilha..."):
            sucesso = append_row(SPREADSHEET_ID, dados_finais)

        if sucesso:
            st.session_state.tela = "confirmacao"
            st.session_state.dados = {}
            st.session_state.imagem = None
            st.rerun()


def tela_confirmacao():
    st.title("✅ Salvo!")
    st.success("Os dados foram adicionados à planilha com sucesso.")
    st.balloons()

    st.markdown("")
    if st.button("➕ Novo documento", type="primary"):
        st.session_state.tela = "inicio"
        st.rerun()


# --- Roteador principal ---

init_state()

telas = {
    "inicio": tela_inicio,
    "captura": tela_captura,
    "revisao": tela_revisao,
    "confirmacao": tela_confirmacao,
}

telas[st.session_state.tela]()
