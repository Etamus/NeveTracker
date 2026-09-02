from urllib.parse import quote, urljoin

from scrapy.http import HtmlResponse

from crawlers.base import collect_http
from crawlers.profiles import STORE_PROFILES
from services.http_client import create_resilient_session


def collect(source, identity, postal_code=None, timeout=15):
    return collect_http(source, identity, postal_code, STORE_PROFILES["magalu"], timeout)


def discover(query, timeout=15):
    url = f"https://www.magazineluiza.com.br/busca/{quote(query.strip())}/"
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
    except Exception as exc:
        print(f"[DISCOVERY][MAGALU] TIMEOUT: {exc}")
        return []
    finally:
        session.close()
    if response.status_code != 200:
        print(f"[DISCOVERY][MAGALU] HTTP {response.status_code}")
        return []
    page = HtmlResponse(
        url=response.url,
        body=response.content,
        encoding=response.encoding or "utf-8",
    )
    items = []
    seen = set()
    for link in page.css('a[href*="/p/"]'):
        href = link.attrib.get("href")
        title = " ".join(link.css("::text").getall()).strip()
        absolute = urljoin(response.url, href or "")
        if not title or absolute in seen:
            continue
        seen.add(absolute)
        items.append(
            {
                "name": title,
                "url": absolute,
                "store": "Magazine Luiza",
                "seller": "Magazine Luiza",
                "source": "Magazine Luiza",
                "brand": "Magazine Luiza",
            }
        )
        if len(items) >= 30:
            break
    return items
