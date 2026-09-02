from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher


IDENTIFIER_FIELDS = ("ean", "mpn", "model", "sku")
GENERIC_TOKENS = {
    "a", "as", "com", "da", "das", "de", "do", "dos", "e", "em", "para",
    "peca", "produto", "unidade", "original", "novo",
}


def normalize_text(value):
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return " ".join(re.findall(r"[a-z0-9]+", ascii_text))


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


def resolve_candidate(identity: ProductIdentity, candidate) -> MatchResult:
    searchable = " ".join(
        str(candidate.get(key) or "")
        for key in ("name", "title", "url", "external_product_id", "asin", "item_id")
    )
    searchable_id = normalize_identifier(searchable)

    for field, method in (("ean", "ean_exact"), ("mpn", "mpn_exact")):
        expected = normalize_identifier(getattr(identity, field))
        candidate_id = normalize_identifier(_candidate_value(candidate, field))
        if expected and (candidate_id == expected or expected in searchable_id):
            return MatchResult(True, 1.0, method, f"{field.upper()} exato")

    model = normalize_identifier(identity.model or identity.sku)
    candidate_model = normalize_identifier(_candidate_value(candidate, "model"))
    model_exact = bool(model and (candidate_model == model or model in searchable_id))
    brand = normalize_text(identity.brand)
    candidate_brand = normalize_text(
        _candidate_value(candidate, "brand") or searchable
    )
    brand_exact = bool(brand and brand in candidate_brand)
    if model_exact and brand_exact:
        return MatchResult(True, 0.95, "brand_model_exact", "Marca e modelo exatos")
    if model_exact:
        return MatchResult(True, 0.90, "model_exact", "Modelo exato")

    expected_name = normalize_text(identity.name)
    candidate_name = normalize_text(candidate.get("name") or candidate.get("title"))
    if expected_name and candidate_name:
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
    products = list(products or [])
    representative = next((product for product in products if product.name), None)
    return ProductIdentity(
        sku=known_sku.sku,
        ean=getattr(known_sku, "ean", None),
        mpn=getattr(known_sku, "mpn", None),
        model=getattr(known_sku, "model", None) or known_sku.sku,
        brand=getattr(known_sku, "brand", None) or (
            representative.brand if representative else None
        ),
        name=(representative.name if representative else None) or known_sku.description,
    )
