DEFAULT_PROFILE = {
    "title": (
        'h1::text',
        'meta[property="og:title"]::attr(content)',
    ),
    "price": (
        'meta[property="product:price:amount"]::attr(content)',
        '[itemtype*="Product"] > [itemprop="offers"] [itemprop="price"]::attr(content)',
        '.product-info-main [itemprop="price"]::attr(content)',
        '[data-testid="product-price"]::text',
        '[data-testid="product-detail"] [data-testid*="price"]::text',
    ),
    "seller": (
        '[itemprop="seller"] [itemprop="name"]::attr(content)',
        '[data-testid="product-seller"]::text',
        '[data-testid="product-detail"] [data-testid*="seller"]::text',
    ),
    "availability": (
        '.product-info-main [itemprop="availability"]::attr(content)',
        '.summary [itemprop="availability"]::attr(href)',
        '.summary .stock::text',
        '.product-info-main .stock::text',
        'meta[property="product:availability"]::attr(content)',
    ),
}

STORE_PROFILES = {
    "mercado_livre": {
        **DEFAULT_PROFILE,
        "title": ("h1.ui-pdp-title::text", *DEFAULT_PROFILE["title"]),
        "seller": (
            ".ui-pdp-seller__header__info-container a::text",
            ".ui-pdp-seller__link-trigger::text",
            *DEFAULT_PROFILE["seller"],
        ),
    },
    "amazon": {
        **DEFAULT_PROFILE,
        "title": ("#productTitle::text", *DEFAULT_PROFILE["title"]),
        "price": (
            "#corePrice_feature_div .a-offscreen::text",
            "#corePriceDisplay_desktop_feature_div .a-offscreen::text",
            "#apex_desktop .a-price .a-offscreen::text",
            *DEFAULT_PROFILE["price"],
        ),
        "seller": ("#sellerProfileTriggerId::text", "#merchant-info a::text"),
    },
    "magalu": DEFAULT_PROFILE,
    "leroy_merlin": DEFAULT_PROFILE,
    "dufrio": DEFAULT_PROFILE,
    "friolar": DEFAULT_PROFILE,
    "refrigeracao_mota": DEFAULT_PROFILE,
    "mg_parts": DEFAULT_PROFILE,
    "gold_service": DEFAULT_PROFILE,
    "comclick": {
        **DEFAULT_PROFILE,
        "title": ("h1.product-name::text", *DEFAULT_PROFILE["title"]),
        "availability": ("#produto_nao_disp #nao_disp .color::text", *DEFAULT_PROFILE["availability"]),
    },
}
