import os

from cachetools import TTLCache, cached

from services.http_client import create_resilient_session
from services.integration_settings import get_runtime_provider_settings

BASE_URL = "https://api.mercadolibre.com/sites/MLB/search"
cache = TTLCache(maxsize=100, ttl=900)
session = create_resilient_session()


def extract_brand(item):
    attributes = item.get("attributes", [])

    for attr in attributes:
        if attr.get("id") == "BRAND":
            return attr.get("value_name")

    return None


@cached(cache)
def search_mercado_livre(product_query: str):
    try:
        integration = get_runtime_provider_settings("mercado_livre")
        max_results = min(500, max(10, int(integration["max_results"])))
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Accept-Language": "pt-BR,pt;q=0.9",
        }
        api_enabled = os.getenv("MERCADO_LIVRE_API_ENABLED", "true").strip().lower()
        access_token = os.getenv("MERCADO_LIVRE_ACCESS_TOKEN", "").strip()
        if api_enabled in {"0", "false", "no", "off"}:
            access_token = ""
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"

        results = []
        seen_ids = set()

        for offset in range(0, max_results, 50):
            response = session.get(
                BASE_URL,
                params={
                    "q": product_query,
                    "limit": 50,
                    "offset": offset,
                    "sort": "price_asc",
                },
                headers=headers,
                timeout=(5, 15),
            )
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
                break

            data = response.json()
            page_items = data.get("results", [])
            for item in page_items:
                item_id = item.get("id")
                if item_id and item_id in seen_ids:
                    continue
                if item_id:
                    seen_ids.add(item_id)

                seller = item.get("seller") or {}
                seller_name = (
                    seller.get("nickname")
                    or seller.get("eshop", {}).get("nick_name")
                    or (f"Mercado Livre Loja {seller.get('id')}" if seller.get("id") else None)
                    or "Mercado Livre"
                )
                price = float(item.get("price") or 0)
                installments = item.get("installments") or {}
                installment_count = installments.get("quantity")
                installment_value = installments.get("amount")
                installment_total = None
                if installment_count and installment_value:
                    installment_total = round(
                        float(installment_count) * float(installment_value), 2
                    )
                shipping = item.get("shipping") or {}
                available_quantity = item.get("available_quantity")

                results.append(
                    {
                        "name": item.get("title"),
                        "price": price,
                        "cash_price": price,
                        "installment_price": installment_total,
                        "installment_count": installment_count,
                        "installment_value": installment_value,
                        "payment_difference": (
                            round(max(0, installment_total - price), 2)
                            if installment_total and price
                            else None
                        ),
                        "shipping_cost": 0.0 if shipping.get("free_shipping") else None,
                        "shipping_method": "Frete gratis" if shipping.get("free_shipping") else None,
                        "availability": (
                            "Disponivel"
                            if available_quantity is None or available_quantity > 0
                            else "Indisponivel"
                        ),
                        "logistics_source": "mercado_livre_search",
                        "discovery_source": "mercado_livre_api",
                        "store": seller_name,
                        "source": "Mercado Livre",
                        "seller": seller_name,
                        "url": item.get("permalink"),
                        "brand": extract_brand(item) or "Mercado Livre",
                        "category": item.get("category_id") or product_query,
                        "image_url": item.get("thumbnail"),
                        "external_id": item_id,
                    }
                )

                if len(results) >= max_results:
                    break

            paging = data.get("paging") or {}
            if (
                len(results) >= max_results
                or not page_items
                or offset + len(page_items) >= int(paging.get("total") or 0)
            ):
                break

        return results

    except Exception as e:
        print(f"Erro ao buscar na API do Mercado Livre: {e}")
        return []
