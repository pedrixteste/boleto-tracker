import re
import numpy as np
from PIL import Image, ImageEnhance
from datetime import date, timedelta

# Configuração do Tesseract no Windows
import os as _os
try:
    import pytesseract
    _WIN_EXE = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if _os.path.exists(_WIN_EXE):
        pytesseract.pytesseract.tesseract_cmd = _WIN_EXE
        TESSERACT_OK = True
    else:
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
    import fitz
    FITZ_OK = True
except ImportError:
    FITZ_OK = False


# ── Utilitários de imagem ─────────────────────────────────────────────────────

def _preprocess(pil_img: Image.Image) -> Image.Image:
    """Melhora qualidade para OCR: upscale + contraste + threshold."""
    # Upscale se imagem for pequena (foto tirada pelo celular via câmera do app)
    w, h = pil_img.size
    if max(w, h) < 2000:
        scale = 2000 / max(w, h)
        pil_img = pil_img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    if not CV2_OK:
        # Sem OpenCV: só aumenta contraste
        gray = pil_img.convert("L")
        return ImageEnhance.Contrast(gray).enhance(2.0)

    img = np.array(pil_img.convert("RGB"))
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    # Sharpen
    kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
    sharpened = cv2.filter2D(gray, -1, kernel)
    # Threshold adaptativo
    thresh = cv2.adaptiveThreshold(
        sharpened, 255,
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


# ── Parser PIX EMV (QR Code) ──────────────────────────────────────────────────

def _parse_pix_emv(payload: str) -> dict:
    """
    Parseia QR Code PIX no formato EMV (começa com 000201).
    Extrai amount (tag 54) e merchant name (tag 59).
    """
    result = {}
    i = 0
    while i < len(payload) - 3:
        try:
            tag = payload[i:i+2]
            length = int(payload[i+2:i+4])
            value = payload[i+4:i+4+length]
            i += 4 + length

            if tag == "54":  # Transaction Amount
                try:
                    amount = float(value)
                    # Formata como "1.518,46"
                    inteiro = int(amount)
                    centavos = round((amount - inteiro) * 100)
                    result["valor"] = f"{inteiro:,}".replace(",", ".") + f",{centavos:02d}"
                except ValueError:
                    pass
            elif tag == "59":  # Merchant Name
                result["beneficiario"] = value.strip()
            elif tag == "26" or tag == "62":
                # Sub-campos — parseia recursivamente para pegar chave/txid
                pass
        except (ValueError, IndexError):
            break
    return result


# ── Extração de texto de boleto ───────────────────────────────────────────────

def _extrair_dados_texto(text: str) -> dict:
    """Extrai valor, vencimento, beneficiário e código de texto de boleto."""
    result = {}

    # Valor: prioriza "TOTAL A PAGAR" ou "Valor do Documento", depois R$ genérico
    for pattern in [
        r"TOTAL\s+A\s+PAGAR[\s\n:R$]*([\d]{1,3}(?:[.,][\d]{3})*[.,][\d]{2})",
        r"Valor\s+do\s+Documento[\s\S]{0,30}?([\d]{1,3}(?:[.,][\d]{3})*[.,][\d]{2})",
        r"Valor[\s\n]+R\$\s*([\d]{1,3}(?:[.,][\d]{3})*[.,][\d]{2})",
    ]:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            result["valor"] = m.group(1).strip()
            break

    if "valor" not in result:
        # Último recurso: qualquer R$ com valor razoável (até R$ 999.999,99)
        matches = re.findall(r"R\$\s*([\d]{1,3}(?:[.,][\d]{3})*[.,][\d]{2})", text, re.IGNORECASE)
        validos = []
        for v in matches:
            digits = re.sub(r"[.,]", "", v)
            if len(digits) <= 8:  # máximo 8 dígitos = R$ 999.999,99
                validos.append(v)
        if validos:
            result["valor"] = validos[-1].strip()

    # Vencimento: após palavra "Vencimento"
    m = re.search(r"Vencimento[\s\n:]+(\d{2}/\d{2}/\d{4})", text, re.IGNORECASE)
    if m:
        result["vencimento"] = m.group(1)
    else:
        # Datas com ano >= 2024 (evita pegar datas antigas do documento)
        dates = re.findall(r"\b(\d{2}/\d{2}/20[2-9]\d)\b", text)
        if dates:
            result["vencimento"] = dates[0]

    # Beneficiário: linha após "Beneficiário"
    m = re.search(r"Benefici[aá]rio[\s\n:]+(.+)", text, re.IGNORECASE)
    if m:
        ben = m.group(1).strip()
        ben = re.sub(r"\s+\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}.*", "", ben)
        ben = re.sub(r"\s+\d{3}\.\d{3}\.\d{3}-\d{2}.*", "", ben)
        result["beneficiario"] = ben[:60]

    # Código / linha digitável
    linha = re.search(r"(\d{5}\.\d{5}\s+\d{5}\.\d{6}\s+\d{5}\.\d{6}\s+\d\s+\d{14})", text)
    if linha:
        result["codigo"] = re.sub(r"\s+", " ", linha.group(1))
    else:
        nums = re.findall(r"\d{47,}", text.replace(" ", "").replace(".", ""))
        if nums:
            result["codigo"] = nums[0][:47]

    return result


# ── Extração direta de PDF ────────────────────────────────────────────────────

def extract_boleto_pdf(pdf_bytes: bytes) -> dict:
    """Extrai dados de boleto diretamente do texto do PDF (sem OCR)."""
    result = {"tipo": "Boleto", "beneficiario": "", "valor": "", "vencimento": "", "codigo": "", "observacoes": ""}

    if not FITZ_OK:
        return result

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    text = "".join(page.get_text() for page in doc)
    doc.close()

    result.update(_extrair_dados_texto(text))
    return result


# ── Extração de boleto a partir de imagem ─────────────────────────────────────

def _parse_boleto_44(code: str) -> dict:
    """Decodifica código de barras bancário de 44 dígitos (FEBRABAN)."""
    if len(code) < 44 or code.startswith("000201"):
        return {}

    valor_str = code[9:19]
    try:
        valor_cents = int(valor_str)
        valor = f"{valor_cents / 100:.2f}".replace(".", ",")
    except ValueError:
        valor = ""

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

    return {"valor": valor, "vencimento": vencimento, "codigo": code}


def extract_boleto(pil_img: Image.Image) -> dict:
    """Extrai dados de boleto: QR PIX > código de barras > OCR."""
    result = {"tipo": "Boleto", "beneficiario": "", "valor": "", "vencimento": "", "codigo": "", "observacoes": ""}

    if PYZBAR_OK:
        codes = pyzbar_decode(pil_img)
        for c in codes:
            raw = c.data.decode("utf-8", errors="ignore").strip()

            # QR Code PIX (EMV)
            if raw.startswith("000201"):
                parsed = _parse_pix_emv(raw)
                if parsed.get("valor"):
                    result.update(parsed)
                    result["codigo"] = raw[:60] + "..." if len(raw) > 60 else raw
                    # Ainda tenta pegar vencimento via OCR se não veio do PIX
                    if not result.get("vencimento") and TESSERACT_OK:
                        text = _ocr_text(pil_img)
                        m = re.search(r"Vencimento[\s\n:]+(\d{2}/\d{2}/\d{4})", text, re.IGNORECASE)
                        if not m:
                            dates = re.findall(r"\b(\d{2}/\d{2}/20[2-9]\d)\b", text)
                            if dates:
                                result["vencimento"] = dates[0]
                        else:
                            result["vencimento"] = m.group(1)
                    return result

            # Código de barras tradicional (44 dígitos)
            digits_only = re.sub(r"\D", "", raw)
            if len(digits_only) >= 44:
                parsed = _parse_boleto_44(digits_only[:44])
                if parsed:
                    result.update(parsed)
                    result["codigo"] = digits_only[:44]
                    return result

    # Fallback: OCR
    if TESSERACT_OK:
        text = _ocr_text(pil_img)
        result.update(_extrair_dados_texto(text))

    return result


# ── Extração de cheque ────────────────────────────────────────────────────────

def extract_cheque(pil_img: Image.Image) -> dict:
    """Extrai dados de cheque via OCR + regex."""
    result = {"tipo": "Cheque", "beneficiario": "", "valor": "", "vencimento": "", "codigo": "", "observacoes": ""}

    if not TESSERACT_OK:
        return result

    text = _ocr_text(pil_img)

    m = re.search(r"R\$\s*([\d.,]+)", text, re.IGNORECASE)
    if not m:
        m = re.search(r"\b(\d{1,3}(?:\.\d{3})*,\d{2})\b", text)
    if m:
        result["valor"] = m.group(1).strip()

    dates = re.findall(r"\b(\d{2}/\d{2}/(?:20\d{2}|\d{2}))\b", text)
    if dates:
        result["vencimento"] = dates[0]

    m = re.search(r"(?:Pague[\s\-]?[as]e?|Pay\s+to)[:\s]+(.+)", text, re.IGNORECASE)
    if m:
        result["beneficiario"] = m.group(1).strip()[:60]

    micr = re.findall(r"\b\d{6,7}\b", text)
    if micr:
        result["codigo"] = micr[0]

    return result
