"""Read visible search cards from the logged-in site's current interface."""
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from services.shopee_public import item_ids
from services.structured_products import number


def search_cards(html):
    soup=BeautifulSoup(html,'lxml');items={}
    for link in soup.select('a[href*="-i."],a[href*="/product/"]'):
        url=urljoin('https://shopee.com.br/',link.get('href') or '')
        shop,item=item_ids(url)
        if not shop or not item:continue
        image=next((img for img in link.select('img[alt]') if img.get('alt') not in {'flag-label','icon',''}),None)
        heading=link.select_one('[data-sqe="name"],.line-clamp-2')
        name=image.get('alt') if image else heading.get_text(' ',strip=True) if heading else None
        if not name:continue
        for node in link.select('del,s,strike'):node.decompose()
        text=link.get_text(' ',strip=True)
        prices=re.findall(r'R\$\s*([\d.,]+)',text)
        price=number(prices[0]) if prices else None
        unavailable=bool(re.search(r'\b(?:sem estoque|esgotado)\b',text,re.I))
        value=dict(name=name,url=url,shop_id=shop,item_id=item,seller=f'Shopee Loja {shop}',store=f'Shopee Loja {shop}',source='Shopee',
            price=None if unavailable else price,cash_price=None if unavailable else price,
            image_url=image.get('src') if image else None,discovery_source='shopee_authenticated_listing')
        if unavailable:value['availability']='out_of_stock'
        items[(shop,item)]=value
    return list(items.values())
