import re
import unicodedata

from scrapers.amazon import scrape_amazon
from scrapers.leroy import scrape_leroy
from scrapers.mercadoLivre import scrape_mercadoLivre
from scrapers.shopee import scrape_shopee
from scrapers.specialty_stores import (
    scrape_comclick,
    scrape_dufrio,
    scrape_friolar,
    scrape_gold_service,
    scrape_mg_parts,
    scrape_refrigeracao_mota,
)
from services.mercado_livre_api import search_mercado_livre


# One direct extractor per storefront keeps the normal path predictable. Offers
# exposed by Buscape/Zoom are collected once, separately, and merged by source.
TARGET_STORES = (
    {
        "key": "mercado_livre",
        "label": "Mercado Livre",
        "aliases": ("mercadolivre", "mercado livre", "mercadolivre.com.br"),
        "scope": "retail",
        "cadence": "3x_daily",
        "direct_scrapers": (search_mercado_livre, scrape_mercadoLivre),
    },
    {
        "key": "shopee",
        "label": "Shopee",
        "aliases": ("shopee.com.br",),
        "scope": "retail",
        "cadence": "3x_daily",
        "direct_scrapers": (scrape_shopee,),
    },
    {
        "key": "leroy_merlin",
        "label": "Leroy Merlin",
        "aliases": ("leroy", "leroymerlin", "leroymerlin.com.br"),
        "scope": "retail",
        "cadence": "3x_daily",
        "direct_scrapers": (scrape_leroy,),
    },
    {
        "key": "amazon",
        "label": "Amazon Brasil",
        "aliases": ("Amazon", "amazon.com.br"),
        "scope": "retail",
        "cadence": "3x_daily",
        "direct_scrapers": (scrape_amazon,),
    },
    {
        "key": "magalu",
        "label": "Magazine Luiza",
        "aliases": ("Magalu", "magazineluiza", "magazineluiza.com.br"),
        "scope": "retail",
        "cadence": "3x_daily",
        "direct_scrapers": (),
    },
    {
        "key": "dufrio",
        "label": "Dufrio",
        "aliases": ("Duofrio", "dufrio.com.br"),
        "scope": "sites",
        "cadence": "hourly",
        "direct_scrapers": (scrape_dufrio,),
    },
    {
        "key": "friolar",
        "label": "Friolar",
        "aliases": ("Friolar Peças", "friolar pecas", "friolarpecas.com.br"),
        "scope": "sites",
        "cadence": "hourly",
        "direct_scrapers": (scrape_friolar,),
    },
    {
        "key": "refrigeracao_mota",
        "label": "Refrigeração Mota",
        "aliases": ("Refrigeracao Mota", "refrigeracaomota.com.br"),
        "scope": "sites",
        "cadence": "hourly",
        "direct_scrapers": (scrape_refrigeracao_mota,),
    },
    {
        "key": "mg_parts",
        "label": "MG Parts",
        "aliases": ("MGParts", "mgparts.com.br"),
        "scope": "sites",
        "cadence": "hourly",
        "direct_scrapers": (scrape_mg_parts,),
    },
    {
        "key": "gold_service",
        "label": "Gold Service",
        "aliases": ("GoldService", "goldservice.com.br"),
        "scope": "sites",
        "cadence": "hourly",
        "direct_scrapers": (scrape_gold_service,),
    },
    {
        "key": "comclick",
        "label": "ComClick",
        "aliases": ("Com Click", "comclick.com.br"),
        "scope": "sites",
        "cadence": "hourly",
        "direct_scrapers": (scrape_comclick,),
    },
)

STORE_BY_LABEL = {store["label"]: store for store in TARGET_STORES}


def _normalize_store_name(value):
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return " ".join(re.findall(r"[a-z0-9]+", ascii_text))


def find_target_store(*values):
    """Return the configured storefront represented by a merchant name or URL."""
    haystack = f" {' '.join(_normalize_store_name(value) for value in values)} "
    if not haystack.strip():
        return None

    for store in TARGET_STORES:
        names = (store["label"], *store.get("aliases", ()))
        for name in names:
            needle = _normalize_store_name(name)
            if needle and f" {needle} " in haystack:
                return store
    return None


def target_store_label(*values):
    store = find_target_store(*values)
    return store["label"] if store else None
