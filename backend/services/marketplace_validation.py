"""Human login/CAPTCHA stays on the store; release requires real page evidence."""
import json
import re
import time
from datetime import datetime,timezone
from urllib.parse import quote_plus,urlsplit
import requests


def search_url(store,sku):
    if store=='shopee':return 'https://shopee.com.br/search?keyword='+quote_plus(sku)
    return 'https://lista.mercadolivre.com.br/'+quote_plus(sku.lower())


def evidence(store,page,sku,required_url=None,since=None):
    if store=='shopee':
        from services.shopee_live_search import read_state,matching_keyword
        state=read_state(page)
        if urlsplit(state.get('url','')).hostname not in {'shopee.com.br','www.shopee.com.br'}:return False
        if since and state.get('observed_at',0)<datetime.fromisoformat(since).timestamp()*1000:return False
        from services.product_resolver import ProductIdentity,resolve_candidate
        def matching_row(row):
            item=row.get('item_basic') or row.get('item_data') or {}
            name=item.get('name') or (row.get('item_card_displayed_asset') or {}).get('name')
            return bool(name and resolve_candidate(ProductIdentity(sku=sku,model=sku),{'name':name}).accepted)
        relevant=any(matching_row(row) for row in state.get('rows') or [])
        return bool(not state.get('blocked') and matching_keyword(state.get('url',''),sku) and (relevant or state.get('empty')))
    result=page.call('Runtime.evaluate',expression="JSON.stringify({url:location.href,observed_at:performance.timeOrigin,html:document.documentElement.outerHTML,title:document.querySelector('h1.ui-pdp-title')?.innerText,cards:document.querySelectorAll('.poly-card,.ui-search-layout__item').length,text:document.body?.innerText||''})",returnByValue=True)
    state=json.loads(result.get('result',{}).get('value') or '{}')
    if since and state.get('observed_at',0)<datetime.fromisoformat(since).timestamp()*1000:return False
    from services.mercado_livre_browser import allowed_url,page_status
    if not allowed_url(state.get('url')) or page_status(state.get('html',''),state.get('url',''),200)[0]!='OK':return False
    normalized=lambda text:re.sub('[^a-z0-9]','',str(text).lower())
    search_matches=normalized(sku) in normalized(urlsplit(state.get('url','')).path)
    if search_matches and (state.get('cards') or re.search('n[aã]o encontramos|nenhum resultado',state.get('text',''),re.I)):return True
    if state.get('title') and required_url:
        from services.mercado_livre_pages import selected_item_id
        item=selected_item_id(required_url)
        return bool(item and selected_item_id(state['url'])==item)
    return bool(state.get('title') and normalized(sku) in normalized(state['title']))


def validate_account(store):
    from services.marketplace_session import configuration,launch_account_browser,set_status,_finish,_lock
    from services.marketplace_intervention import pending,enabled,release
    from services.shopee_page_transport import CDP
    from playwright.sync_api import sync_playwright
    profile,_,label,_=configuration(store);request=pending(store) or {};sku=request.get('sku') or 'BLF61AR';url=search_url(store,sku)
    if store=='mercado_livre' and request.get('url'):
        from services.mercado_livre_browser import allowed_url
        if allowed_url(request['url']):url=request['url']
    since=request.get('requested_at') or datetime.now(timezone.utc).isoformat()
    browser=page=None
    try:
        with sync_playwright() as runtime:endpoint=launch_account_browser(runtime,store,url)
        browser=CDP(requests.get(endpoint+'/json/version',timeout=3).json()['webSocketDebuggerUrl'])
        tabs=requests.get(endpoint+'/json/list',timeout=3).json()
        target=next((tab['id'] for tab in tabs if tab.get('type')=='page' and tab.get('url')==url),None)
        if not target:target=browser.call('Target.createTarget',url=url,background=False)['targetId']
        browser.call('Target.activateTarget',targetId=target)
        _show_window(profile.parent/(store+'-browser.json'))
        tab=next(tab for tab in requests.get(endpoint+'/json/list',timeout=3).json() if tab['id']==target)
        page=CDP(tab['webSocketDebuggerUrl'])
        if store=='mercado_livre':page.call('Page.navigate',url=url)
        set_status(store,'waiting',f'Conclua o login ou a verificação e pesquise {sku} na janela de {label}. A extração das outras lojas continua.')
        deadline=time.monotonic()+1800
        next_tab_check=time.monotonic()+3
        while time.monotonic()<deadline:
            if not enabled(store):set_status(store,'disabled','Uso da conta desativado.');return
            try:
                if evidence(store,page,sku,request.get('url'),since):
                    release(store,target);set_status(store,'connected','Acesso verificado. As pendências desta loja serão retomadas automaticamente.');return
                if time.monotonic()>=next_tab_check:
                    next_tab_check=time.monotonic()+3
                    other=_verified_other_tab(endpoint,target,store,sku,request.get('url'),since)
                    if other:
                        release(store,other);set_status(store,'connected','Acesso verificado. As pendências desta loja serão retomadas automaticamente.');return
            except Exception:
                # Login may replace the original tab. Only inspect tabs from
                # this store in its isolated browser, never the user's profile.
                try:
                    tabs=requests.get(endpoint+'/json/list',timeout=3).json()
                    candidates=[tab for tab in tabs if tab.get('type')=='page' and _store_host(store,urlsplit(tab.get('url','')).hostname)]
                    if not candidates:
                        set_status(store,'closed','A janela foi fechada. Abra a loja novamente para liberar as pendências.');return
                    if page:page.close()
                    tab=candidates[0];target=tab['id'];page=CDP(tab['webSocketDebuggerUrl'])
                except Exception:
                    set_status(store,'closed','Abra a loja novamente para concluir a verificação.');return
            confirmation=profile.parent/(store+'-confirm.request')
            if confirmation.exists():confirmation.unlink()
            with _lock:_finish[store]=False
            time.sleep(1)
        set_status(store,'closed','A validação continua pendente. Abra a loja novamente quando puder concluir.')
    except Exception as exc:set_status(store,'error',f'Não foi possível validar {label}: {type(exc).__name__}. A extração das outras lojas continua.')
    finally:
        if page:page.close()
        if browser:browser.close()


def _store_host(store,host):
    domain='mercadolivre.com.br' if store=='mercado_livre' else 'shopee.com.br'
    return host==domain or bool(host and host.endswith('.'+domain))


def _verified_other_tab(endpoint,current,store,sku,required_url,since):
    from services.shopee_page_transport import CDP
    try:tabs=requests.get(endpoint+'/json/list',timeout=3).json()
    except Exception:return None
    for tab in tabs:
        if tab.get('id')==current or tab.get('type')!='page' or not _store_host(store,urlsplit(tab.get('url','')).hostname):continue
        candidate=None
        try:
            candidate=CDP(tab['webSocketDebuggerUrl'])
            if evidence(store,candidate,sku,required_url,since):return tab['id']
        except Exception:pass
        finally:
            if candidate:candidate.close()
    return None


def _show_window(metadata):
    import os
    if os.name!='nt':return
    try:
        import ctypes
        from ctypes import wintypes
        owner=json.loads(metadata.read_text(encoding='utf-8'))['pid'];user=ctypes.windll.user32
        user.ShowWindow.argtypes=[wintypes.HWND,ctypes.c_int]
        user.SetForegroundWindow.argtypes=[wintypes.HWND]
        user.GetWindowRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
        user.SetWindowPos.argtypes=[wintypes.HWND,wintypes.HWND,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,wintypes.UINT]
        callback=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
        @callback
        def show(window,_):
            pid=wintypes.DWORD();user.GetWindowThreadProcessId(window,ctypes.byref(pid))
            if pid.value==owner:
                user.ShowWindow(window,9)
                bounds=wintypes.RECT();user.GetWindowRect(window,ctypes.byref(bounds))
                if bounds.left<-10000 or bounds.top<-10000:user.SetWindowPos(window,None,80,80,1200,820,0x0040)
                user.SetForegroundWindow(window)
            return True
        user.EnumWindows(show,0)
    except (OSError,ValueError,KeyError):pass
