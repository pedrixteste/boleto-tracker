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
    import zxingcpp
    ZXINGCPP_OK = True
except ImportError:
    ZXINGCPP_OK = False

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
    try:
        text = pytesseract.image_to_string(processed, lang="por", config="--psm 6")
    except Exception:
        # Pacote 'por' não instalado — tenta inglês como fallback
        try:
            text = pytesseract.image_to_string(processed, lang="eng", config="--psm 6")
        except Exception:
            return ""
    return text


def _ocr_vencimento(pil_img: Image.Image) -> str:
    """
    Extrai data de vencimento via OCR para faturas de concessionárias
    (energia, água, gás, telecom) onde o vencimento NÃO está codificado
    no código de barras arrecadação.

    Tenta vários padrões de layout comuns em faturas brasileiras:
      - "Vencimento: 22/06/2026"
      - "VENCIMENTO\\n22/06/2026"
      - "VENC. 22/06/2026"
      - "Data de Vencimento 22/06/2026"
      - "VENCE EM 22/06/2026"
    Fallback: primeira data futura (ano >= 2024) encontrada no texto.
    """
    if not TESSERACT_OK:
        return ""
    text = _ocr_text(pil_img)
    if not text:
        return ""

    # Padrões em ordem de prioridade.
    # Usamos {0,80} para tolerar tabelas multi-coluna onde a palavra-chave
    # "VENCIMENTO" é cabeçalho de coluna e a data está na linha seguinte,
    # separada pelas demais colunas (ex: Certel — "VENCIMENTO TOTAL A PAGAR
    # COMPETÊNCIA\n63.328.095-39 22/06/2026 1.518,46 MAI/2026").
    _PAT_VENC = [
        r"Vencimento[\s\S]{0,80}?(\d{2}/\d{2}/\d{4})",
        r"\bVENC(?:IMENTO)?\b[\s\S]{0,80}?(\d{2}/\d{2}/\d{4})",
        r"Data\s+(?:de\s+)?Venc(?:imento)?[\s\S]{0,80}?(\d{2}/\d{2}/\d{4})",
        r"Vence(?:\s+em)?[\s\S]{0,40}?(\d{2}/\d{2}/\d{4})",
    ]
    for pat in _PAT_VENC:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1)

    # Fallback: primeira data futura encontrada no texto
    dates = re.findall(r"\b(\d{2}/\d{2}/20[2-9]\d)\b", text)
    if dates:
        return dates[0]

    return ""


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

    # Vencimento: múltiplos padrões de faturas brasileiras
    _venc_pats = [
        r"Vencimento[\s\S]{0,80}?(\d{2}/\d{2}/\d{4})",
        r"\bVENC(?:IMENTO)?\b[\s\S]{0,80}?(\d{2}/\d{2}/\d{4})",
        r"Data\s+(?:de\s+)?Venc(?:imento)?[\s\S]{0,80}?(\d{2}/\d{2}/\d{4})",
        r"Vence(?:\s+em)?[\s\S]{0,40}?(\d{2}/\d{2}/\d{4})",
    ]
    for _pat in _venc_pats:
        m = re.search(_pat, text, re.IGNORECASE)
        if m:
            result["vencimento"] = m.group(1)
            break
    if "vencimento" not in result:
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

def _parse_arrecadacao_44(code: str) -> dict:
    """
    Decodifica código de arrecadação de concessionárias (energia, água, gás,
    telecom) — barras que começam com '8' (FEBRABAN cobrança/arrecadação).
    Nesses códigos o valor está nas posições 4–14 (11 dígitos).
    """
    if len(code) < 44 or code[0] != '8':
        return {}
    try:
        valor_cents = int(code[4:15])
        if valor_cents <= 0:
            return {}
        valor = f"{valor_cents / 100:.2f}".replace(".", ",")
    except ValueError:
        return {}
    return {"valor": valor, "vencimento": "", "codigo": code}


def _parse_boleto_44(code: str) -> dict:
    """Decodifica código de barras bancário de 44 dígitos (FEBRABAN)."""
    if len(code) < 44 or code.startswith("000201"):
        return {}
    # Códigos de arrecadação (energia, água, etc.) começam com '8' —
    # têm formato diferente e são tratados em _parse_arrecadacao_44.
    if code[0] == '8':
        return {}
    # Dígito de moeda (posição 3) deve ser '9' para boleto bancário Real.
    # Chaves de acesso NF-e também têm 44 dígitos mas position 3 ≠ '9'.
    if code[3] != '9':
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
            # Base antiga: 07/10/1997. Fator 9999 = 21/02/2025 (último dia).
            # A partir de 22/02/2025 o FEBRABAN resetou o contador para 1000.
            # Nova base: 29/05/2022 (= 22/02/2025 - 1000 dias).
            _BASE_OLD  = date(1997, 10, 7)
            _BASE_NOVA = date(2022, 5, 29)
            venc_date = _BASE_OLD + timedelta(days=fator)
            # Se a data calculada com a base antiga for mais de 6 meses atrás,
            # o boleto pertence ao novo ciclo (pós-reset) → recalcula com base nova.
            if venc_date < date.today() - timedelta(days=180):
                venc_date = _BASE_NOVA + timedelta(days=fator)
            vencimento = venc_date.strftime("%d/%m/%Y")
    except (ValueError, OverflowError):
        pass

    return {"valor": valor, "vencimento": vencimento, "codigo": code}


def _tentar_todos_codigos(pil_img: Image.Image) -> list:
    """Retorna TODOS os códigos (QR/barras) únicos encontrados na imagem."""
    if not PYZBAR_OK:
        return []

    tentativas = [pil_img]

    if CV2_OK:
        img_np = np.array(pil_img.convert("RGB"))
        gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)
        _, thresh1 = cv2.threshold(gray, 128, 255, cv2.THRESH_BINARY)
        tentativas.append(Image.fromarray(thresh1))
        h, w = gray.shape
        big = cv2.resize(gray, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
        _, thresh2 = cv2.threshold(big, 128, 255, cv2.THRESH_BINARY)
        tentativas.append(Image.fromarray(thresh2))
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        _, thresh3 = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        tentativas.append(Image.fromarray(thresh3))

    seen: set = set()
    todos: list = []
    for img in tentativas:
        for c in pyzbar_decode(img):
            raw = c.data.decode("utf-8", errors="ignore").strip()
            if raw and raw not in seen:
                seen.add(raw)
                todos.append(raw)
    return todos


# Mantém compatibilidade com chamadas antigas
def _tentar_qr(pil_img: Image.Image) -> str:
    result = _tentar_todos_codigos(pil_img)
    return result[0] if result else ""


def _tentar_zxing(pil_img: Image.Image) -> str:
    """
    Tenta ler código de barras / QR com zxingcpp.
    Muito melhor que pyzbar para fotos comprimidas pelo celular.
    Suporta: ITF (boleto), QR Code (PIX), Code128, EAN-13.
    """
    if not ZXINGCPP_OK:
        return ""

    tentativas = [pil_img.convert("RGB")]

    # Versão upscalada — ajuda quando o boleto foi fotografado de longe
    w, h = pil_img.size
    if max(w, h) < 2500:
        scale = 2500 / max(w, h)
        big = pil_img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        tentativas.append(big.convert("RGB"))

    # Versão com contraste adaptativo (CLAHE) — ajuda em fotos escuras
    if CV2_OK:
        img_np = np.array(pil_img.convert("RGB"))
        gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        enhanced_rgb = cv2.cvtColor(enhanced, cv2.COLOR_GRAY2RGB)
        tentativas.append(Image.fromarray(enhanced_rgb))

    for img in tentativas:
        try:
            barcodes = zxingcpp.read_barcodes(img)
            for barcode in barcodes:
                if barcode.valid and barcode.text.strip():
                    return barcode.text.strip()
        except Exception:
            pass

    return ""


def extract_boleto(pil_img: Image.Image) -> dict:
    """Extrai dados de boleto: zxingcpp > pyzbar > OCR (fallback vazio)."""
    result = {"tipo": "Boleto", "beneficiario": "", "valor": "", "vencimento": "", "codigo": "", "observacoes": ""}

    # Coleta TODOS os códigos encontrados na imagem
    todos_raw: list = []
    raw_zxing = _tentar_zxing(pil_img)
    if raw_zxing:
        todos_raw.append(raw_zxing)
    for r in _tentar_todos_codigos(pil_img):
        if r not in todos_raw:
            todos_raw.append(r)

    # Ordena: QR PIX (EMV) > boleto bancário FEBRABAN válido (moeda=9) > outros
    def _prio(r: str) -> int:
        if r.startswith("000201"):
            return 0
        d = re.sub(r"\D", "", r)
        return 1 if len(d) >= 44 and d[3] == '9' else 2

    todos_raw.sort(key=_prio)

    for raw in todos_raw:
        # QR Code PIX (EMV)
        if raw.startswith("000201"):
            parsed = _parse_pix_emv(raw)
            if parsed.get("valor"):
                result.update(parsed)
                result["codigo"] = raw[:60] + "..." if len(raw) > 60 else raw
                # Tenta pegar vencimento via OCR (não vem no PIX)
                if TESSERACT_OK:
                    text = _ocr_text(pil_img)
                    m = re.search(r"Vencimento[\s\n:]+(\d{2}/\d{2}/\d{4})", text, re.IGNORECASE)
                    if m:
                        result["vencimento"] = m.group(1)
                    else:
                        dates = re.findall(r"\b(\d{2}/\d{2}/20[2-9]\d)\b", text)
                        if dates:
                            result["vencimento"] = dates[0]
                return result

        # Código de barras — tenta boleto bancário, depois arrecadação
        digits_only = re.sub(r"\D", "", raw)
        if len(digits_only) >= 44:
            parsed = _parse_boleto_44(digits_only[:44])
            if not parsed:
                parsed = _parse_arrecadacao_44(digits_only[:44])
            if parsed:
                result.update(parsed)
                result["codigo"] = digits_only[:44]
                # Arrecadação (energia, água, etc.) não carrega vencimento no
                # código de barras → extrai via OCR do texto impresso na fatura
                if not result.get("vencimento"):
                    result["vencimento"] = _ocr_vencimento(pil_img)
                return result

    # 2. Fallback OCR — só usa se conseguir dados razoáveis
    if TESSERACT_OK:
        text = _ocr_text(pil_img)
        dados = _extrair_dados_texto(text)
        # Valida: só aceita valor se parecer razoável
        valor = dados.get("valor", "")
        if valor:
            digits = re.sub(r"[.,]", "", valor)
            if len(digits) > 8:  # mais de R$ 999.999 = suspeito
                dados.pop("valor", None)
        result.update(dados)

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
