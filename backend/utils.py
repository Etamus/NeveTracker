import time
from urllib.parse import quote_plus

import requests

session = requests.Session()

MARKETPLACE_CONFIGS = {
    "mercado_livre": {
        "base_url": "https://lista.mercadolivre.com.br/",
        "sort_param": "_OrderId_PRICE_ASC",
    },
    "amazon": {
        "base_url": "https://www.amazon.com.br/s?k=",
        "sort_param": "&s=price-asc-rank",
    },
}


def build_url(marketplace, product_name):
    config = MARKETPLACE_CONFIGS.get(marketplace)
    return f"{config['base_url']}{quote_plus(product_name)}{config['sort_param']}"


def get_random_headers():
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.google.com/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Upgrade-Insecure-Requests": "1",
    }


def fetch_marketplace_data(marketplace, product_name):
    url = build_url(marketplace, product_name)
    for attempt in range(2):
        try:
            response = session.get(url, headers=get_random_headers(), timeout=12)

            if response.status_code == 200:
                first_chunk = response.text[:2000]
                if (
                    "account-verification" in response.url
                    or "suspicious-traffic" in first_chunk
                ):
                    print(f"{marketplace} bloqueou a consulta por verificacao de trafego.")
                    return None
                return response.text

            if response.status_code not in {429, 500, 502, 503, 504} or attempt == 1:
                print(f"Erro {response.status_code} ao acessar {marketplace}")
                return None

            time.sleep(0.4)
        except Exception as e:
            if attempt == 1:
                print(f"Falha na requisicao para {marketplace}: {e}")
                return None
            time.sleep(0.4)

    return None
