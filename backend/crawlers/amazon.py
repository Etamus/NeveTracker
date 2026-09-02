import re
from urllib.parse import parse_qs, urlencode, urlparse

from scrapy.http import HtmlResponse

from crawlers.base import collect_http
from crawlers.profiles import STORE_PROFILES
from services.http_client import create_resilient_session


def collect(source, identity, postal_code=None, timeout=15):
    return collect_http(source, identity, postal_code, STORE_PROFILES["amazon"], timeout)


def _asin(value):
    match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", str(value or ""), re.I)
    return match.group(1).upper() if match else None


def discover_sellers(candidate, timeout=15):
    asin = candidate.get("asin") or _asin(candidate.get("url"))
    if not asin:
        return []
    url = f"https://www.amazon.com.br/gp/offer-listing/{asin}"
    session = create_resilient_session(pool_size=2)
    try:
        response = session.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
                "Accept-Language": "pt-BR,pt;q=0.9",
            },
            timeout=(5, timeout),
        )
    except Exception:
        return []
    finally:
        session.close()
    if response.status_code != 200:
        return []
    page = HtmlResponse(url=response.url, body=response.content, encoding=response.encoding or "utf-8")
    offers = []
    cards = page.css(".olpOffer, .aod-offer, [id^='aod-offer']")
    for card in cards:
        seller = " ".join(
            card.css(".olpSellerName ::text, #aod-offer-soldBy .a-size-small::text, [aria-label*='Vendido por']::text").getall()
        ).strip()
        seller_link = card.css(".olpSellerName a::attr(href), #aod-offer-soldBy a::attr(href)").get()
        seller_id = None
        if seller_link:
            seller_id = (parse_qs(urlparse(seller_link).query).get("seller") or [None])[0]
        if not seller:
            seller = "Amazon Brasil" if card.css("i.a-icon-prime").get() else None
        if not seller:
            continue
        query = urlencode({"m": seller_id}) if seller_id else ""
        offers.append(
            {
                **candidate,
                "asin": asin,
                "seller": seller,
                "store": seller,
                "source": "Amazon Brasil",
                "url": f"https://www.amazon.com.br/dp/{asin}" + (f"?{query}" if query else ""),
            }
        )
    return offers
