import re
import numpy as np
from PIL import Image
from datetime import date, timedelta

# Configuração do Tesseract no Windows
import os as _os
try:
    import pytesseract
    # Windows
    _WIN_EXE = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if _os.path.exists(_WIN_EXE):
        pytesseract.pytesseract.tesseract_cmd = _WIN_EXE
        TESSERACT_OK = True
    else:
        # Linux (Streamlit Cloud)
        TESSERACT_OK = _os.path.exists("/usr/bin/tesseract")
except ImportError:
    TESSERACT_OK = False

try:
    from pyzbar.pyzbar import decode as pyzbar_decode
    PYZBAR_OK = True
except ImportError:
    PYZBAR_OK = False

try:
    import cv2
    CV2_OK = True
except ImportError:
    CV2_OK = False

try:
    import fitz  # PyMuPDF
    FITZ_OK = True
except ImportError:
    FITZ_OK = False


# --- Utilitários de imagem ---

def _preprocess(pil_img: Image.Image) -> Image.Image:
    """Melhora contraste para OCR: escala de cinza + threshold adaptativo."""
    if not CV2_OK:
        return pil_img.convert("L")

    img = np.array(pil_img.convert("RGB"))
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    denoised = cv2.fastNlMeansDenoising(gray, h=10)
    thresh = cv2.adaptiveThreshold(
        denoised, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, 31, 10,
    )
    return Image.fromarray(thresh)


def _ocr_text(pil_img: Image.Image) -> str:
    if not TESSERACT_OK:
        return ""
    processed = _preprocess(pil_img)
    text = pytesseract.image_to_string(processed, lang="por", config="--psm 6")
    return text


# --- Extração de texto de boleto (texto puro ou OCR) ---

def _extrair_dados_texto(text: str) -> dict:
    """
    Extrai valor, vencimento, beneficiário e código a partir de texto
    de boleto (funciona tanto com texto direto de PDF quanto com OCR).
    """
    result = {}

    # Valor: prioriza "Valor do Documento" ou "Valor R$", depois qualquer R$
    m = re.search(r"Valor do Documento[\s\S]{0,30}?([\d]+[.,][\d]{2})", text, re.IGNORECASE)
    if not m:
        m = re.search(r"Valor[\s\n]+R\$\s*([\d.,]+)", text, re.IGNORECASE)
    if not m:
        # Última ocorrência de R$ X antes do vencimento
        matches = re.findall(r"R\$\s*([\d]{1,3}(?:[.,][\d]{3})*[.,][\d]{2})", text, re.IGNORECASE)
        if matches:
            # Filtra valores absurdos (ex: mais de 6 dígitos antes da vírgula)
            validos = [v for v in matches if len(re.sub(r"[.,]", "", v)) <= 8]
            if validos:
                result["valor"] = validos[-1].strip()
    if m and "valor" not in result:
        result["valor"] = m.group(1).strip()

    # Vencimento: pega data que aparece após a palavra "Vencimento"
    m = re.search(r"Vencimento[\s\n:]+(\d{2}/\d{2}/\d{4})", text, re.IGNORECASE)
    if m:
        result["vencimento"] = m.group(1)
    else:
        # Qualquer data no formato DD/MM/AAAA com ano >= 2024
        dates = re.findall(r"\b(\d{2}/\d{2}/20[2-9]\d)\b", text)
        if dates:
            result["vencimento"] = dates[0]

    # Beneficiário: linha após "Beneficiário"
    m = re.search(r"Benefici[aá]rio[\s\n:]+(.+)", text, re.IGNORECASE)
    if m:
        ben = m.group(1).strip()
        # Remove CNPJ/CPF da mesma linha se houver
        ben = re.sub(r"\s+\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}.*", "", ben)
        ben = re.sub(r"\s+\d{3}\.\d{3}\.\d{3}-\d{2}.*", "", ben)
        result["beneficiario"] = ben[:60]

    # Código de barras / linha digitável
    linha = re.search(r"(\d{5}\.\d{5}\s+\d{5}\.\d{6}\s+\d{5}\.\d{6}\s+\d\s+\d{14})", text)
    if linha:
        result["codigo"] = re.sub(r"\s+", " ", linha.group(1))
    else:
        # Código compacto sem pontos/espaços
        nums = re.findall(r"\d{47,}", text.replace(" ", "").replace(".", ""))
        if nums:
            result["codigo"] = nums[0][:47]

    return result


# --- Extração direta de PDF ---

def extract_boleto_pdf(pdf_bytes: bytes) -> dict:
    """Extrai dados de boleto diretamente do texto do PDF (sem OCR)."""
    result = {"tipo": "Boleto", "beneficiario": "", "valor": "", "vencimento": "", "codigo": "", "observacoes": ""}

    if not FITZ_OK:
        return result

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    text = ""
    for page in doc:
        text += page.get_text()
    doc.close()

    dados = _extrair_dados_texto(text)
    result.update(dados)
    return result


# --- Extração de boleto a partir de imagem ---

def _parse_boleto_44(code: str) -> dict:
    """
    Decodifica código de barras bancário de 44 dígitos (FEBRABAN).
    Ignora se for PIX (começa com 000201).
    """
    if len(code) < 44:
        return {}

    # PIX EMV — não é boleto tradicional, ignora
    if code.startswith("000201"):
        return {}

    banco = code[:3]

    # Valor: posições 10-19 (índices 9 a 18)
    valor_str = code[9:19]
    try:
        valor_cents = int(valor_str)
        valor = f"{valor_cents / 100:.2f}".replace(".", ",")
    except ValueError:
        valor = ""

    # Vencimento: fator de dias a partir de 07/10/1997 (posições 6-9)
    fator_str = code[5:9]
    vencimento = ""
    try:
        fator = int(fator_str)
        if fator > 0:
            base = date(1997, 10, 7)
            venc_date = base + timedelta(days=fator)
            vencimento = venc_date.strftime("%d/%m/%Y")
    except (ValueError, OverflowError):
        pass

    return {"valor": valor, "vencimento": vencimento, "banco": banco, "codigo": code}


def extract_boleto(pil_img: Image.Image) -> dict:
    """Extrai dados de boleto a partir de imagem: código de barras primeiro, depois OCR."""
    result = {"tipo": "Boleto", "beneficiario": "", "valor": "", "vencimento": "", "codigo": "", "observacoes": ""}

    # Tenta código de barras (ignora PIX automaticamente)
    if PYZBAR_OK:
        codes = pyzbar_decode(pil_img)
        for c in codes:
            raw = c.data.decode("utf-8", errors="ignore").strip()
            digits_only = re.sub(r"\D", "", raw)
            if len(digits_only) >= 44 and not raw.startswith("000201"):
                parsed = _parse_boleto_44(digits_only[:44])
                if parsed:
                    result.update(parsed)
                    result["codigo"] = digits_only[:44]
                    return result

    # Fallback: OCR
    if TESSERACT_OK:
        text = _ocr_text(pil_img)
        dados = _extrair_dados_texto(text)
        result.update(dados)

    return result


# --- Extração de cheque ---

def extract_cheque(pil_img: Image.Image) -> dict:
    """Extrai dados de cheque via OCR + regex."""
    result = {"tipo": "Cheque", "beneficiario": "", "valor": "", "vencimento": "", "codigo": "", "observacoes": ""}

    if not TESSERACT_OK:
        return result

    text = _ocr_text(pil_img)

    # Valor monetário
    m = re.search(r"R\$\s*([\d.,]+)", text, re.IGNORECASE)
    if not m:
        m = re.search(r"\b(\d{1,3}(?:\.\d{3})*,\d{2})\b", text)
    if m:
        result["valor"] = m.group(1).strip()

    # Data de emissão / bom para
    dates = re.findall(r"\b(\d{2}/\d{2}/(?:20\d{2}|\d{2}))\b", text)
    if dates:
        result["vencimento"] = dates[0]

    # Beneficiário
    m = re.search(r"(?:Pague[\s\-]?[as]e?|Pay\s+to)[:\s]+(.+)", text, re.IGNORECASE)
    if m:
        result["beneficiario"] = m.group(1).strip()[:60]

    # Número do cheque (MICR)
    micr = re.findall(r"\b\d{6,7}\b", text)
    if micr:
        result["codigo"] = micr[0]

    return result
