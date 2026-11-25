"""Reuse the manually validated Shopee tab and read public product card data."""
import json
import time
from urllib.parse import parse_qs,urlsplit

STATE_SCRIPT=r'''(() => {
 const rows=[],seen=new Set();let visits=0;
 const take=v=>{const item=v.item_basic||v.item_data;if(!item||!item.itemid||!item.shopid)return;
 const key=item.shopid+':'+item.itemid;if(seen.has(key))return;seen.add(key);
 const asset=v.item_card_displayed_asset||{},price=item.item_card_display_price||{};
 rows.push({item_basic:item.name?{name:item.name,itemid:item.itemid,shopid:item.shopid,price:item.price,price_min:item.price_min,price_max:item.price_max,stock:item.stock,image:item.image}:null,
 item_data:{itemid:item.itemid,shopid:item.shopid,item_card_display_price:{applied_product_promo_price:price.applied_product_promo_price}},
 item_card_displayed_asset:{name:asset.name,image:asset.image}});};
 const walk=(v,depth,visited)=>{if(!v||typeof v!=='object'||depth>6||visits++>100000||visited.has(v))return;visited.add(v);
 if(v.item_basic||v.item_data){take(v);return;}if(v.itemid&&v.shopid&&v.name){take({item_basic:v});return;}
 for(const [key,child] of Object.entries(v))if(!['_owner','stateNode','return','child','sibling'].includes(key))walk(child,depth+1,visited);};
 for(const node of document.querySelectorAll('a[href*="-i."],a[href*="/product/"]')){
 let fiber=node[Object.keys(node).find(key=>key.startsWith('__reactFiber$')||key.startsWith('__reactInternalInstance$'))];
 for(let up=0;fiber&&up<15;up++,fiber=fiber.return)walk(fiber.memoizedProps,0,new Set());}
 const text=document.body?.innerText||'';
 const searches=performance.getEntriesByType('resource').filter(e=>/\/api\/v4\/(search|recommend)/.test(e.name));
 return JSON.stringify({rows,url:location.href,observed_at:Math.max(performance.timeOrigin,...searches.map(e=>performance.timeOrigin+e.responseEnd)),empty:/n[aã]o encontramos(?: nenhum)? resultados?|nenhum resultado(?: foi)? encontrado|n[aã]o foram encontrados resultados/i.test(text),
 blocked:/\/verify\/|\/buyer\/login/.test(location.href)||/verifique sua identidade|atividade incomum|captcha/i.test(text),
 current:document.querySelector('.shopee-mini-page-controller__current')?.innerText,
 total:document.querySelector('.shopee-mini-page-controller__total')?.innerText});
})()'''


def read_state(page):
    value=page.call('Runtime.evaluate',expression=STATE_SCRIPT,returnByValue=True)
    return json.loads(value.get('result',{}).get('value') or '{}')


def matching_keyword(url,term):
    actual=(parse_qs(urlsplit(url).query).get('keyword') or [''])[0]
    return actual.strip().casefold()==term.strip().casefold()


def search_tab(page,url,timeout):
    term=(parse_qs(urlsplit(url).query).get('keyword') or [''])[0]
    wanted=int((parse_qs(urlsplit(url).query).get('page') or ['0'])[0])+1
    state=read_state(page)
    submitted=not matching_keyword(state.get('url',''),term) or int(state.get('current') or 1)>wanted
    previous_observation=state.get('observed_at',0)
    def row_ids(value):
        return {(str((row.get('item_basic') or row.get('item_data') or {}).get('shopid')),str((row.get('item_basic') or row.get('item_data') or {}).get('itemid'))) for row in value.get('rows',[])}
    previous_rows=row_ids(state)
    if submitted:
        page.call('Runtime.evaluate',expression="[...document.querySelectorAll('input.shopee-searchbar-input__input,input[type=search]')].find(e=>e.getClientRects().length)?.focus()")
        page.call('Input.dispatchKeyEvent',type='keyDown',key='a',code='KeyA',modifiers=2,windowsVirtualKeyCode=65)
        page.call('Input.dispatchKeyEvent',type='keyUp',key='a',code='KeyA',modifiers=2,windowsVirtualKeyCode=65)
        page.call('Input.insertText',text=term)
        page.call('Input.dispatchKeyEvent',type='keyDown',key='Enter',code='Enter',windowsVirtualKeyCode=13)
        page.call('Input.dispatchKeyEvent',type='keyUp',key='Enter',code='Enter',windowsVirtualKeyCode=13)
    deadline=time.monotonic()+max(4,min(timeout,20))
    while time.monotonic()<deadline:
        state=read_state(page)
        if state.get('blocked'):return state
        fresh=not submitted or state.get('observed_at',0)>previous_observation and (state.get('empty') or row_ids(state)!=previous_rows)
        if matching_keyword(state.get('url',''),term) and fresh and (state.get('rows') or state.get('empty')):break
        time.sleep(.5)
    else:return {'status':'TIMEOUT','reason':'A loja não confirmou uma nova busca para o SKU solicitado.'}
    if not matching_keyword(state.get('url',''),term):return {'status':'PARSER_ERROR','reason':'A busca não avançou para o SKU solicitado.'}
    if state.get('empty'):return {**state,'rows':[],'more':False}
    current=int(state.get('current') or 1)
    while current<wanted and time.monotonic()<deadline:
        page.call('Runtime.evaluate',expression="document.querySelector('.shopee-mini-page-controller__next-btn,.shopee-icon-button--right')?.click()")
        time.sleep(.7);state=read_state(page);advanced=int(state.get('current') or current)
        if advanced==current:return {'status':'PARSER_ERROR','reason':'A página de vendedores não avançou.'}
        current=advanced
    if current!=wanted:return {'status':'PARSER_ERROR','reason':'A loja retornou outra página de resultados.'}
    rows={}
    # Load the visible search page progressively; item filtering happens later.
    for fraction in [0,.2,.4,.6,.8,1]:
        page.call('Runtime.evaluate',expression=f'window.scrollTo(0,document.documentElement.scrollHeight*{fraction})')
        time.sleep(.15);state=read_state(page)
        for row in state.get('rows',[]):
            item=row.get('item_basic') or row.get('item_data') or {};rows[(item.get('shopid'),item.get('itemid'))]=row
    state['rows']=list(rows.values())
    state['more']=int(state.get('current') or 1)<int(state.get('total') or 1)
    return state
