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


def normalize_text(value):
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return " ".join(
        "brastemp" if token == "brastem" else token
        for token in re.findall(r"[a-z0-9]+", ascii_text)
    )


def canonical_brand(value):
    return "Brastemp" if normalize_text(value) == "brastemp" else value


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
    groups = {
        "sensor": {"sensor", "sensores"}, "placa": {"placa", "placas"},
        "cabo": {"cabo", "cabos"}, "adaptador": {"adaptador", "adaptadores"},
        "motor": {"motor", "motores"}, "capacitor": {"capacitor", "capacitores"},
        "resistencia": {"resistencia", "resistencias"}, "filtro": {"filtro", "filtros"},
        "tampa": {"tampa", "tampas"}, "gaveta": {"gaveta", "gavetas"},
        "termostato": {"termostato", "termostatos"},
    }
    for token in prefix.split():
        for component, aliases in groups.items():
            if token in aliases:
                return component
    return None


def resolve_candidate(identity: ProductIdentity, candidate) -> MatchResult:
    searchable = " ".join(str(candidate.get(key) or "") for key in ("name", "title"))
    if _pack_quantity(searchable) != _pack_quantity(identity.name):
        return MatchResult(False, 0.0, "invalid_match", "Quantidade do kit diferente da unidade pesquisada")
    expected_component, observed_component = _component(identity.name), _component(searchable)
    if expected_component and observed_component and expected_component != observed_component:
        return MatchResult(False, 0.0, "invalid_match", "Componente anunciado diferente da peca pesquisada")
    url_path = urlparse(str(candidate.get("url") or "")).path
    identifier_text = (searchable, url_path)

    for field in IDENTIFIER_FIELDS[:3]:
        expected = normalize_identifier(getattr(identity, field))
        observed = _candidate_value(candidate, field)
        if expected and observed and not _identifier_in_text(expected, observed):
            return MatchResult(False, 0.0, "invalid_match", f"{field.upper()} divergente")

    for field, method in (("ean", "ean_exact"), ("mpn", "mpn_exact")):
        expected = normalize_identifier(getattr(identity, field))
        candidate_id = normalize_identifier(_candidate_value(candidate, field))
        if expected and (candidate_id == expected or _identifier_in_text(expected, *identifier_text)):
            return MatchResult(True, 1.0, method, f"{field.upper()} exato")

    expected_sku = normalize_identifier(identity.sku)
    if expected_sku and normalize_identifier(_candidate_value(candidate, "structured_sku")) == expected_sku:
        return MatchResult(True, 0.98, "sku_exact", "SKU estruturado exato")

    model = normalize_identifier(identity.model or identity.sku)
    candidate_model = normalize_identifier(_candidate_value(candidate, "model"))
    model_exact = bool(model and (
        candidate_model == model
        or _identifier_in_text(model, _candidate_value(candidate, "model"), *identifier_text)
    ))
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
