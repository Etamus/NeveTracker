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
        "scope": "retail",
        "cadence": "3x_daily",
        "direct_scrapers": (search_mercado_livre, scrape_mercadoLivre),
    },
    {
        "key": "shopee",
        "label": "Shopee",
        "scope": "retail",
        "cadence": "3x_daily",
        "direct_scrapers": (scrape_shopee,),
    },
    {
        "key": "leroy_merlin",
        "label": "Leroy Merlin",
        "scope": "retail",
        "cadence": "3x_daily",
        "direct_scrapers": (scrape_leroy,),
    },
    {
        "key": "amazon",
        "label": "Amazon Brasil",
        "aliases": ("Amazon",),
        "scope": "retail",
        "cadence": "3x_daily",
        "direct_scrapers": (scrape_amazon,),
    },
    {
        "key": "magalu",
        "label": "Magazine Luiza",
        "scope": "retail",
        "cadence": "3x_daily",
        "direct_scrapers": (),
    },
    {
        "key": "dufrio",
        "label": "Dufrio",
        "scope": "sites",
        "cadence": "hourly",
        "direct_scrapers": (scrape_dufrio,),
    },
    {
        "key": "friolar",
        "label": "Friolar",
        "scope": "sites",
        "cadence": "hourly",
        "direct_scrapers": (scrape_friolar,),
    },
    {
        "key": "refrigeracao_mota",
        "label": "Refrigeração Mota",
        "scope": "sites",
        "cadence": "hourly",
        "direct_scrapers": (scrape_refrigeracao_mota,),
    },
    {
        "key": "mg_parts",
        "label": "MG Parts",
        "scope": "sites",
        "cadence": "hourly",
        "direct_scrapers": (scrape_mg_parts,),
    },
    {
        "key": "gold_service",
        "label": "Gold Service",
        "scope": "sites",
        "cadence": "hourly",
        "direct_scrapers": (scrape_gold_service,),
    },
    {
        "key": "comclick",
        "label": "ComClick",
        "scope": "sites",
        "cadence": "hourly",
        "direct_scrapers": (scrape_comclick,),
    },
)

STORE_BY_LABEL = {store["label"]: store for store in TARGET_STORES}
