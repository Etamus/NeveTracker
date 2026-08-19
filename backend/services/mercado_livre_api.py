import os

import requests
from cachetools import TTLCache, cached

BASE_URL = "https://api.mercadolibre.com/sites/MLB/search"
cache = TTLCache(maxsize=100, ttl=900)


def extract_brand(item):
    attributes = item.get("attributes", [])

    for attr in attributes:
        if attr.get("id") == "BRAND":
            return attr.get("value_name")

    return None


@cached(cache)
def search_mercado_livre(product_query: str):
    try:
        params = {
            "q": product_query,
            "limit": 50,
            "sort": "price_asc",
        }
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Accept-Language": "pt-BR,pt;q=0.9",
        }
        access_token = os.getenv("MERCADO_LIVRE_ACCESS_TOKEN", "").strip()
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"

        response = requests.get(BASE_URL, params=params, headers=headers, timeout=10)
        if response.status_code != 200:
            token_hint = (
                " Configure MERCADO_LIVRE_ACCESS_TOKEN para usar a API oficial."
                if response.status_code in {401, 403} and not access_token
                else ""
            )
            print(
                "API do Mercado Livre indisponivel ou bloqueada: "
                f"HTTP {response.status_code}.{token_hint}"
            )
            return []

        data = response.json()
        results = []

        for item in data.get("results", []):
            seller = item.get("seller") or {}
            seller_name = (
                seller.get("nickname")
                or seller.get("eshop", {}).get("nick_name")
                or (f"Mercado Livre Loja {seller.get('id')}" if seller.get("id") else None)
                or "Mercado Livre"
            )
            results.append(
                {
                    "name": item.get("title"),
                    "price": float(item.get("price", 0)),
                    "store": seller_name,
                    "source": "Mercado Livre",
                    "seller": seller_name,
                    "url": item.get("permalink"),
                    "brand": extract_brand(item) or "Mercado Livre",
                    "category": product_query,
                    "image_url": item.get("thumbnail"),
                }
            )

        return results

    except Exception as e:
        print(f"Erro ao buscar na API do Mercado Livre: {e}")
        return []
