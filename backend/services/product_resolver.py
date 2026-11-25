from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import urlparse


IDENTIFIER_FIELDS = ("ean", "mpn", "model", "sku")
GENERIC_TOKENS = {
    "a", "as", "com", "da", "das", "de", "do", "dos", "e", "em", "para",
    "peca", "produto", "unidade", "original", "novo",
}
SPARE_PART_HEADING = r'^(?:\d+\s+)?(?:kit|jogo|conjunto|pecas?|capa|refil|puxador|gaveta|prateleira|placa|sensor|motor|filtro|suporte|bandeja|compressor|resistencia|borracha|gaxeta|rotor|impeller|impelidor|cabo|vara|vareta|botao|queimador|pe|pes|bocal|condensador|rodizio|protetor|grade|dobradica|termostato|tampa|porta|dispenser|vedacao|anel|trava|rede|chicote|atuador|suspensao|agitador|painel)\b'


def normalize_text(value):
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return " ".join(
        "brastemp" if token == "brastem" else token
        for token in re.findall(r"[a-z0-9]+", ascii_text)
    )


def canonical_brand(value):
    return "Brastemp" if normalize_text(value) == "brastemp" else value


def _voltage_options(value):
    voltages = re.findall(r"\b(110|115|120|127|208|220|230|240)\s*v\b", normalize_text(value))
    return {127 if int(voltage) <= 127 else 220 for voltage in voltages}


def appliance_kind(value):
    text=normalize_text(value)
    if re.match(SPARE_PART_HEADING,text):return None
    found=[]
    for kind,pattern in (
        ('dishwasher',r'\blava loucas?\b'),
        ('washer',r'\b(?:maquina (?:de )?lavar|lavadoras?|lava ?roupas?)\b'),
        ('refrigerator',r'\b(?:geladeira|refrigerador)\b'),
        ('freezer',r'\bfreezer\b'),
        ('stove',r'\bfogao\b'),
        ('beer_cooler',r'\bcervejeira\b'),
    ):
        if match:=re.search(pattern,text):found.append((match.start(),kind))
    return min(found)[1] if found else None


def appliance_match_error(identity, candidate):
    """A compatible spare part must not be mistaken for a complete appliance."""
    expected=appliance_kind(identity.name)
    if not expected:
        code=str(identity.model or identity.sku or '').upper()
        for kind,pattern in (
            ('dishwasher',r'(?:BLF\d+[A-Z0-9]*|LS\d+[A-Z0-9]*)'),
            ('washer',r'(?:(?:BWF|BWJ|CWB|CWH|CWN|LEE|LEP|LED|LDA)\d+[A-Z0-9]*|MA512W\d+[A-Z0-9]*)'),
            ('refrigerator',r'(?:(?:BRE|BRM|BRO|CRA|CRD|CRE|CRM|IB|IF|IM|TF|RE)\d+[A-Z0-9]*|MD-RT[A-Z0-9]*)'),
            ('freezer',r'(?:CHA|CHB|CVU|HI|FEI)\d+[A-Z0-9]*'),
            ('stove',r'CFO\d+[A-Z0-9]*'),
            ('beer_cooler',r'(?:CZD|MDRX)\d+[A-Z0-9]*'),
        ):
            if re.fullmatch(pattern,code):expected=kind;break
    if not expected:return None
    title=normalize_text(candidate.get('name') or candidate.get('title'))
    if not title:return None
    if re.match(SPARE_PART_HEADING,title):
        return 'Peça ou acessório compatível não é o eletrodoméstico solicitado.'
    actual=appliance_kind(title)
    if actual and actual!=expected:return 'Tipo de eletrodoméstico diferente do produto cadastrado.'
    if not actual:return 'O título não confirma o eletrodoméstico completo solicitado.'
    return None


def normalize_identifier(value):
    return "".join(re.findall(r"[a-z0-9]+", normalize_text(value)))


def normalized_tokens(value):
    return {
        token
        for token in normalize_text(value).split()
        if len(token) > 1 and token not in GENERIC_TOKENS
    }


@dataclass(frozen=True)
class ProductIdentity:
    sku: str
    ean: str | None = None
    mpn: str | None = None
    model: str | None = None
    brand: str | None = None
    name: str | None = None

    @property
    def search_terms(self):
        values = (self.ean, self.mpn, self.model, self.sku, self.name)
        return tuple(dict.fromkeys(str(value).strip() for value in values if value))


@dataclass(frozen=True)
class MatchResult:
    accepted: bool
    confidence: float
    method: str
    reason: str


def _candidate_value(candidate, key):
    value = candidate.get(key)
    if value:
        return str(value)
    identifiers = candidate.get("identifiers") or {}
    return str(identifiers.get(key) or "")


def _identifier_in_text(identifier, *values):
    expected = normalize_identifier(identifier)
    if not expected:
        return False
    # This manufacturer publishes the requested family with voltage suffixes
    # 1/2. The explicit pair also lets saved listings retain a correct match.
    if expected == 'mdrt411fgf01' and any(_identifier_in_text(value, *values) for value in ('MD-RT411FGF011', 'MD-RT411FGF012')):
        return True
    if expected == 'mdrt611evk46' and _identifier_in_text('MD-RT611EVK463', *values):
        return True
    for value in values:
        tokens = re.findall(r"[a-z0-9]+", normalize_text(value))
        for start in range(len(tokens)):
            combined = ""
            for token in tokens[start:]:
                combined += token
                if combined == expected:
                    return True
                if len(combined) >= len(expected):
                    break
    return False


def _pack_quantity(name):
    text = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    patterns = (r"\bkit\s+(?:com\s+|de\s+)?(\d+)\b(?![.,]\d)", r"\b(\d+)\s*(?:unidades|unidade|un|pecas|sensores)\b")
    for pattern in patterns:
        match = re.search(pattern, text)
        if match and 1 < int(match.group(1)) <= 1000:
            return int(match.group(1))
    return 1


def _component(name):
    prefix = re.split(r"\b(?:para|compativel|aplicacao)\b", normalize_text(name), maxsplit=1)[0]
    prefix = re.sub(r'\b(?:controle|modulo) eletronico\b','placa',prefix)
    groups = {
        "sensor": {"sensor", "sensores"}, "placa": {"placa", "placas"},
        "cabo": {"cabo", "cabos"}, "adaptador": {"adaptador", "adaptadores"},
        "motor": {"motor", "motores"}, "capacitor": {"capacitor", "capacitores"},
        "resistencia": {"resistencia", "resistencias"}, "filtro": {"filtro", "filtros"},
        "tampa": {"tampa", "tampas"}, "gaveta": {"gaveta", "gavetas"},
        "termostato": {"termostato", "termostatos"},
        "agitador": {"agitador", "agitadores"},
        "suspensao": {"vareta", "varetas", "suspensao"},
        "correia": {"correia", "correias"},
        "rolamento": {"rolamento", "rolamentos"},
        "valvula": {"valvula", "valvulas"},
    }
    for token in prefix.split():
        for component, aliases in groups.items():
            if token in aliases:
                return component
    return None


def appliance_variant(sku):
    return re.fullmatch(r'((?:BRM|BRE|BRO|CRM|CWH|BWF|BWJ)\d{2}[A-Z0-9]{2})(ANA|BNA|XNA)', sku.upper())


def _appliance_variant_match(identity, candidate, title):
    variant = appliance_variant(identity.sku)
    if not variant:
        return None
    base, suffix = variant.groups()
    structured = [_candidate_value(candidate, field) for field in ('structured_sku', 'mpn', 'model')]
    text = ' '.join([title, *structured]).upper()
    codes = set(re.findall(r'(?<![A-Z0-9])' + base + r'(?:ANA|BNA|XNA)(?![A-Z0-9])', text))
    tension = normalize_text(candidate.get('voltage')) or title
    expected = {'ANA': '127', 'BNA': '220', 'XNA': 'bivolt'}[suffix]
    observed = {'bivolt'} if re.search(r'\bbivolt\b', tension) else {
        '127' if int(value) <= 127 else '220'
        for value in re.findall(r'\b(110|115|120|127|208|220|230|240)\s*v(?:olts?)?\b', tension)
    }
    if tension in {'110', '127', '220'}:
        observed = {'220' if tension == '220' else '127'}
    if (observed and observed != {expected}) or (codes and codes != {variant.group(0)}):
        return MatchResult(False, 0.0, 'invalid_match', 'Código ou tensão da variante divergente.')
    if codes or any(normalize_identifier(value) == normalize_identifier(identity.sku) for value in structured):
        return MatchResult(True, .98, 'variant_code_exact', 'Código completo da variante confirmado.')
    if observed == {expected} and _identifier_in_text(base, title, *structured):
        return MatchResult(True, .95, 'variant_model_voltage', 'Modelo e tensão da variante confirmados.')
    if identity.ean and normalize_identifier(_candidate_value(candidate, 'ean')) == normalize_identifier(identity.ean):
        return MatchResult(True, 1.0, 'ean_exact', 'EAN da variante confirmado.')
    return MatchResult(False, 0.0, 'invalid_match', 'A oferta não confirma o código completo nem a tensão da variante.')


def resolve_candidate(identity: ProductIdentity, candidate) -> MatchResult:
    if error:=appliance_match_error(identity,candidate):
        return MatchResult(False,0.0,'invalid_match',error)
    searchable = " ".join(str(candidate.get(key) or "") for key in ("name", "title"))
    heading = normalize_text(searchable)
    if re.search(r'\b(?:garantia estendida|seguro garantia|servico de instalacao)\b', heading):
        return MatchResult(False, 0.0, 'invalid_match', 'Servico ou garantia nao corresponde ao produto')
    appliance_model = re.fullmatch(r'(?:BWF|BRO|CRD|CRM|BRM|BWJ|LEE|IM|IF)\d+[A-Z]*', str(identity.sku).upper()) or str(identity.sku).upper().startswith('MD-RT')
    appliance_heading = re.match(r'^(?:geladeira|refrigerador|lavadora|maquina|lava roupa)\b', heading)
    if appliance_model and not appliance_heading and _component(searchable) in {'placa', 'sensor', 'cabo', 'motor', 'filtro', 'gaveta', 'tampa', 'resistencia', 'termostato', 'agitador', 'suspensao', 'correia', 'rolamento', 'valvula'}:
        return MatchResult(False, 0.0, 'invalid_match', 'Peca de reposicao nao corresponde ao eletrodomestico')
    if _pack_quantity(searchable) != _pack_quantity(identity.name):
        return MatchResult(False, 0.0, "invalid_match", "Quantidade do kit diferente da unidade pesquisada")
    expected_component, observed_component = _component(identity.name), _component(searchable)
    if not appliance_kind(identity.name) and expected_component and observed_component and expected_component != observed_component:
        return MatchResult(False, 0.0, "invalid_match", "Componente anunciado diferente da peca pesquisada")
    url_path = urlparse(str(candidate.get("url") or "")).path
    identifier_text = (searchable, url_path)
    expected_voltage = _voltage_options(identity.name)
    observed_voltage = _voltage_options(f"{searchable} {url_path} {candidate.get('voltage') or ''}")
    if len(expected_voltage) == len(observed_voltage) == 1 and expected_voltage != observed_voltage:
        return MatchResult(False, 0.0, "invalid_match", "Tensão diferente da especificação pesquisada")

    identity_fields = IDENTIFIER_FIELDS[:2] if appliance_variant(identity.sku) else IDENTIFIER_FIELDS[:3]
    for field in identity_fields:
        expected = normalize_identifier(getattr(identity, field))
        observed = _candidate_value(candidate, field)
        if expected and observed and not _identifier_in_text(expected, observed):
            return MatchResult(False, 0.0, "invalid_match", f"{field.upper()} divergente")

    if variant := _appliance_variant_match(identity, candidate, heading):
        return variant

    for field, method in (("ean", "ean_exact"), ("mpn", "mpn_exact")):
        expected = normalize_identifier(getattr(identity, field))
        candidate_id = normalize_identifier(_candidate_value(candidate, field))
        if expected and (candidate_id == expected or _identifier_in_text(expected, *identifier_text)):
            return MatchResult(True, 1.0, method, f"{field.upper()} exato")

    expected_sku = normalize_identifier(identity.sku)
    if expected_sku and normalize_identifier(_candidate_value(candidate, "structured_sku")) == expected_sku:
        return MatchResult(True, 0.98, "sku_exact", "SKU estruturado exato")

    model = normalize_identifier(identity.model or identity.sku)
    distinct_model = bool(model and model != expected_sku)
    if distinct_model and _identifier_in_text(expected_sku, *identifier_text):
        return MatchResult(True, 0.95, "sku_exact", "SKU exato no título ou URL")
    candidate_model = normalize_identifier(_candidate_value(candidate, "model"))
    model_exact = bool(model and (
        candidate_model == model
        or _identifier_in_text(model, _candidate_value(candidate, "model"), *identifier_text)
    ))
    if model_exact and distinct_model and len(expected_voltage) == 1 and expected_voltage != observed_voltage:
        return MatchResult(False, 0.0, "invalid_match", "Modelo encontrado sem confirmação da tensão")
    brand = normalize_text(identity.brand)
    candidate_brand = normalize_text(
        _candidate_value(candidate, "brand") or searchable
    )
    brand_exact = bool(brand and set(brand.split()).issubset(candidate_brand.split()))
    if model_exact and brand_exact:
        return MatchResult(True, 0.95, "brand_model_exact", "Marca e modelo exatos")
    if model_exact:
        return MatchResult(True, 0.90, "model_exact", "Modelo exato")

    expected_name = normalize_text(identity.name)
    candidate_name = normalize_text(candidate.get("name") or candidate.get("title"))
    has_catalog_identifier = any(char.isdigit() for char in str(identity.model or identity.sku or ""))
    if expected_name and candidate_name and not has_catalog_identifier:
        left = normalized_tokens(expected_name)
        right = normalized_tokens(candidate_name)
        overlap = len(left & right) / max(1, len(left))
        sequence = SequenceMatcher(None, expected_name, candidate_name).ratio()
        numeric_left = {token for token in left if any(char.isdigit() for char in token)}
        numeric_match = not numeric_left or numeric_left.issubset(right)
        confidence = round(0.65 * overlap + 0.35 * sequence, 3)
        if brand_exact:
            confidence = min(0.89, confidence + 0.05)
        if numeric_match and confidence >= 0.75:
            return MatchResult(True, confidence, "name_specs", "Nome e especificacoes compativeis")

    return MatchResult(
        False,
        0.0,
        "invalid_match",
        "Nenhum identificador forte confirmou o produto",
    )


def identity_from_models(known_sku, products) -> ProductIdentity:
    return ProductIdentity(
        sku=known_sku.sku,
        ean=getattr(known_sku, "ean", None),
        mpn=getattr(known_sku, "mpn", None),
        model=getattr(known_sku, "model", None) or known_sku.sku,
        brand=canonical_brand(getattr(known_sku, "brand", None)),
        name=known_sku.description,
    )
