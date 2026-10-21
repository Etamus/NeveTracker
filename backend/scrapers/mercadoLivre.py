"""Quick mode shares public Mercado Livre discovery with precise extraction."""


def scrape_mercadoLivre(product_name):
    from crawlers.paged_discovery import fetch_search_page, search_url

    url = search_url("mercado_livre", product_name.strip())
    diagnostics = {}
    items, _, success = fetch_search_page("mercado_livre", url, diagnostics=diagnostics)
    if not success:
        items, _, success = fetch_search_page("mercado_livre", url, diagnostics=diagnostics, use_browser=True)
    if not success:
        print(f"[SEARCH][MERCADO_LIVRE] {diagnostics.get('status')}: {diagnostics.get('reason')}")
        return []
    return [dict(item, brand=item.get("brand") or "Mercado Livre", category=product_name,
                 image_url=item.get("image_url"))
            for item in items if item.get("seller") and item.get("price") is not None]
