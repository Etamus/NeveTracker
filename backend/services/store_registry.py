from scrapers.amazon import scrape_amazon
from scrapers.leroy import scrape_leroy
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
        "direct_scrapers": (search_mercado_livre,),
    },
    {
        "key": "shopee",
        "label": "Shopee",
        "direct_scrapers": (scrape_shopee,),
    },
    {
        "key": "leroy_merlin",
        "label": "Leroy Merlin",
        "direct_scrapers": (scrape_leroy,),
    },
    {
        "key": "amazon",
        "label": "Amazon Brasil",
        "aliases": ("Amazon",),
        "direct_scrapers": (scrape_amazon,),
    },
    {
        "key": "magalu",
        "label": "Magazine Luiza",
        "direct_scrapers": (),
    },
    {
        "key": "dufrio",
        "label": "Dufrio",
        "direct_scrapers": (scrape_dufrio,),
    },
    {
        "key": "friolar",
        "label": "Friolar",
        "direct_scrapers": (scrape_friolar,),
    },
    {
        "key": "refrigeracao_mota",
        "label": "Refrigeração Mota",
        "direct_scrapers": (scrape_refrigeracao_mota,),
    },
    {
        "key": "mg_parts",
        "label": "MG Parts",
        "direct_scrapers": (scrape_mg_parts,),
    },
    {
        "key": "gold_service",
        "label": "Gold Service",
        "direct_scrapers": (scrape_gold_service,),
    },
    {
        "key": "comclick",
        "label": "ComClick",
        "direct_scrapers": (scrape_comclick,),
    },
)

STORE_BY_LABEL = {store["label"]: store for store in TARGET_STORES}

