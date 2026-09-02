DEFAULT_PROFILE = {
    "title": (
        'meta[property="og:title"]::attr(content)',
        'h1::text',
    ),
    "price": (
        'meta[property="product:price:amount"]::attr(content)',
        '[itemprop="price"]::attr(content)',
        '[data-testid*="price"]::text',
    ),
    "seller": (
        '[itemprop="seller"] [itemprop="name"]::attr(content)',
        '[data-testid*="seller"]::text',
    ),
}

STORE_PROFILES = {
    "amazon": {
        **DEFAULT_PROFILE,
        "title": ("#productTitle::text", *DEFAULT_PROFILE["title"]),
        "price": (
            ".a-price .a-offscreen::text",
            "#corePrice_feature_div .a-offscreen::text",
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
    "comclick": DEFAULT_PROFILE,
}
