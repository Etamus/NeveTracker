"""Interactive, persistent marketplace sessions; passwords stay in the store's page."""
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Thread, BoundedSemaphore
from urllib.parse import urlsplit
import json
import time
import os
from contextlib import contextmanager

ROOT=Path(__file__).resolve().parents[2]
SHOPEE_SLOT=BoundedSemaphore(1)
_lock=Lock()
_states={}
_finish={}


def configuration(store):
    if store=='mercado_livre':
        from services.mercado_livre_browser import PROFILE_DIR,BROWSER_SLOT
        return PROFILE_DIR,BROWSER_SLOT,'Mercado Livre','https://lista.mercadolivre.com.br/blf61ar'
    if store=='shopee':return ROOT/'.tools/browser/shopee',SHOPEE_SLOT,'Shopee','https://shopee.com.br/buyer/login'
    raise ValueError('Loja não suporta conexão pelo navegador.')


def metadata_file(store):
    profile,_,_,_=configuration(store)
    return profile.parent/(store.replace('_','-')+'-validation.json')


def _lease_file(store):
    profile,_,_,_=configuration(store)
    return profile.parent/(store+'-interactive-session.json')


def _process_alive(pid):
    if not isinstance(pid,int) or pid<=0:return False
    if pid==os.getpid():return True
    if os.name=='nt':
        import ctypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.restype=ctypes.c_void_p
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:return False
        try:
            code=ctypes.c_ulong()
            kernel.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong)]
            return bool(kernel.GetExitCodeProcess(handle,ctypes.byref(code))) and code.value==259
        finally:kernel.CloseHandle(handle)
    return Path(f'/proc/{pid}').exists()


def _interactive_state(store):
    try:
        state=json.loads(_lease_file(store).read_text(encoding='utf-8'))
        if _process_alive(state.get('pid')):return {'status':state['status'],'message':state['message']}
    except (OSError,ValueError,KeyError,TypeError):pass
    return None


def session_status(store):
    configuration(store)
    from services.marketplace_intervention import enabled,pending
    if not enabled(store):return {'status':'disabled','message':'Uso da conta do navegador desativado.'}
    external=_interactive_state(store)
    if external and external['status'] in {'opening','waiting'}:return external
    access=pending(store)
    if access:return {'status':'access_required','message':f"Aguardando verificação de acesso. Pesquise {access['sku']} na janela da loja."}
    if external:return external
    if metadata_file(store).exists() and browser_endpoint(store):
        from services.marketplace_access import limited
        blocked=limited(store)
        if blocked:return {'status':'connected','message':blocked['reason']}
        return {'status':'connected','message':'Acesso validado. O navegador da conta está disponível para as extrações.'}
    with _lock:
        result=dict(_states.get(store) or {'status':'saved' if metadata_file(store).exists() else 'idle','message':'Sessão local preservada. Você pode verificar o acesso ou entrar novamente.' if metadata_file(store).exists() else 'Conecte sua conta para usar a sessão nas extrações.'})
    return result


def set_status(store,status,message):
    with _lock:_states[store]={'status':status,'message':message}
    lease=_lease_file(store);lease.parent.mkdir(parents=True,exist_ok=True)
    lease.write_text(json.dumps({'pid':os.getpid(),'status':status,'message':message}),encoding='utf-8')


def validated_after(store,when):
    try:
        stamp=json.loads(metadata_file(store).read_text(encoding='utf-8'))['verified_at']
        verified=datetime.fromisoformat(stamp).astimezone(timezone.utc).replace(tzinfo=None)
        return when is not None and verified>when
    except (OSError,ValueError,KeyError,TypeError):return False


def browser_options(visible=False):
    options=dict(headless=not visible,locale='pt-BR',timezone_id='America/Sao_Paulo',no_viewport=True,timeout=15000)
    # Prefer the installed browser; never attach to or copy the user's normal profile.
    import os
    for relative in ['Google/Chrome/Application/chrome.exe','Microsoft/Edge/Application/msedge.exe']:
        for base in [os.environ.get('PROGRAMFILES',''),os.environ.get('PROGRAMFILES(X86)',''),os.environ.get('LOCALAPPDATA','')]:
            candidate=Path(base)/relative
            if base and candidate.is_file():options['executable_path']=str(candidate);return options
    # Account profiles may have been created by the newer Patchright browser.
    # Opening them with Playwright's older bundled revision can exit silently.
    cache=Path(os.environ.get('PLAYWRIGHT_BROWSERS_PATH') or str(Path(os.environ.get('LOCALAPPDATA',''))/'ms-playwright'))
    revisions=sorted((path for path in cache.glob('chromium-*') if path.name.rsplit('-',1)[-1].isdigit()),
                     key=lambda path:int(path.name.rsplit('-',1)[-1]),reverse=True)
    for revision in revisions:
        for relative in ['chrome-win64/chrome.exe','chrome-win/chrome.exe']:
            candidate=revision/relative
            if candidate.is_file():options['executable_path']=str(candidate);return options
    return options


def browser_endpoint(store):
    """Only attach to the dedicated browser started for this store."""
    profile,_,_,_=configuration(store)
    port=9471 if store=='mercado_livre' else 9472
    try:
        owner=json.loads((profile.parent/(store+'-browser.json')).read_text(encoding='utf-8'))
        if owner.get('port')!=port or not _process_alive(owner.get('pid')):return None
        import requests
        response=requests.get(f'http://127.0.0.1:{port}/json/version',timeout=1)
        response.raise_for_status()
        websocket=response.json().get('webSocketDebuggerUrl','')
        if urlsplit(websocket).hostname not in {'127.0.0.1','localhost'}:return None
        return f'http://127.0.0.1:{port}'
    except (OSError,ValueError,KeyError):return None
    except Exception:return None


def ensure_account_browser(store):
    from services.marketplace_intervention import enabled
    if not enabled(store):return None
    endpoint=browser_endpoint(store)
    if endpoint or not metadata_file(store).exists():return endpoint
    from playwright.sync_api import sync_playwright
    with sync_playwright() as runtime:
        return launch_account_browser(runtime,store,'about:blank',background=True)


def launch_account_browser(runtime,store,url,background=False):
    endpoint=browser_endpoint(store)
    if endpoint:return endpoint
    import subprocess
    profile,_,_,_=configuration(store);profile.mkdir(parents=True,exist_ok=True)
    port=9471 if store=='mercado_livre' else 9472
    executable=browser_options().get('executable_path') or runtime.chromium.executable_path
    args=[executable,f'--user-data-dir={profile}',f'--remote-debugging-port={port}',
        '--remote-debugging-address=127.0.0.1','--no-first-run','--no-default-browser-check']
    startup=None
    if background and os.name=='nt':
        startup=subprocess.STARTUPINFO();startup.dwFlags|=subprocess.STARTF_USESHOWWINDOW;startup.wShowWindow=0
        args.append('--start-minimized')
    process=subprocess.Popen([*args,url],startupinfo=startup,
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    (profile.parent/(store+'-browser.json')).write_text(json.dumps({'pid':process.pid,'port':port}),encoding='utf-8')
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        endpoint=browser_endpoint(store)
        if endpoint:return endpoint
        time.sleep(.25)
    raise ValueError('O navegador de conta não ficou disponível. Feche a janela de validação anterior e tente novamente.')


@contextmanager
def account_context(runtime,store,visible=False):
    from services.marketplace_intervention import enabled
    if not enabled(store):
        options=browser_options(visible)
        browser=runtime.chromium.launch(**{key:value for key,value in options.items() if key in {'headless','executable_path','timeout'}})
        context=browser.new_context(locale='pt-BR',timezone_id='America/Sao_Paulo')
        try:yield context
        finally:context.close();browser.close()
        return
    endpoint=browser_endpoint(store)
    if endpoint:
        browser=runtime.chromium.connect_over_cdp(endpoint,timeout=10000)
        domain='https://www.mercadolivre.com.br/' if store=='mercado_livre' else 'https://shopee.com.br/'
        context=browser.contexts[0]
        for candidate in browser.contexts:
            cookies=candidate.cookies(domain)
            if any(cookie['name']==('ssid' if store=='mercado_livre' else 'SPC_U') and cookie.get('value') not in {'','-','-1','0'} for cookie in cookies):
                context=candidate;break
        yield context
        # The user's browser and tabs remain open when this connection ends.
    else:
        profile,_,_,_=configuration(store);profile.mkdir(parents=True,exist_ok=True)
        options=browser_options(visible)
        if visible:options['args']=['--window-position=-32000,-32000']
        context=runtime.chromium.launch_persistent_context(str(profile),**options)
        try:yield context
        finally:context.close()


def start_validation(store):
    from services.marketplace_intervention import enabled
    if not enabled(store):raise ValueError('Ative o uso da conta antes de conectar.')
    _,slot,label,_=configuration(store)
    opening_message='' if store=='shopee' else f'Abrindo a janela de {label}.'
    external=_interactive_state(store)
    if external and external['status'] in {'opening','waiting'}:return external
    with _lock:
        if (_states.get(store) or {}).get('status') in {'opening','waiting'}:return dict(_states[store])
        if not slot.acquire(blocking=False):raise ValueError(f'Uma coleta de {label} está usando a sessão. Aguarde para conectar.')
        _finish[store]=False
        _states[store]={'status':'opening','message':opening_message}
    set_status(store,'opening',opening_message)
    Thread(target=_validate,args=(store,),name=store+'-login',daemon=True).start()
    return session_status(store)


def finish_validation(store):
    configuration(store)
    external=_interactive_state(store)
    if external and external['status'] in {'opening','waiting'}:
        profile,_,_,_=configuration(store)
        (profile.parent/(store+'-confirm.request')).write_text(datetime.now(timezone.utc).isoformat(),encoding='utf-8')
        return {'status':'waiting','message':'Verificando o acesso na janela aberta.'}
    from services.marketplace_intervention import pending
    if pending(store):return start_validation(store)
    with _lock:
        if (_states.get(store) or {}).get('status') not in {'opening','waiting'}:return dict(_states.get(store) or {'status':'idle','message':'Abra a janela de login primeiro.'})
        _finish[store]=True
    return {'status':'waiting','message':'Verificando o acesso e preservando a sessão local.'}


def _ready(store,page):
    host=urlsplit(page.url).hostname or ''
    if store=='mercado_livre':
        authenticated=any(c['name']=='ssid' and bool(c.get('value')) for c in page.context.cookies('https://www.mercadolivre.com.br/'))
        return authenticated and host.endswith('mercadolivre.com.br') and page.locator('.poly-card, .ui-search-layout__item, .ui-pdp-title').count()>0
    if host not in {'shopee.com.br','www.shopee.com.br'} or '/buyer/login' in page.url or '/verify/' in page.url:return False
    authenticated=any(c['name']=='SPC_U' and c.get('value') not in {'','-','-1','0'} for c in page.context.cookies('https://shopee.com.br/'))
    return authenticated and page.locator('.navbar__username, [data-sqe="item"]').count()>0


def _validate(store):
    from services.marketplace_validation import validate_account
    _,slot,_,_=configuration(store)
    try:validate_account(store)
    finally:slot.release()


def render_page(store,url,timeout=15,handler=None,observer=None):
    """Read a page in the same isolated profile used for the interactive login."""
    from patchright.sync_api import sync_playwright
    profile,slot,label,_=configuration(store)
    host=urlsplit(url).hostname or ''
    valid=urlsplit(url).scheme=='https' and (host=='mercadolivre.com.br' or host.endswith('.mercadolivre.com.br')) if store=='mercado_livre' else urlsplit(url).scheme=='https' and host in {'shopee.com.br','www.shopee.com.br'}
    if not valid:return {'status':'INVALID_MATCH','reason':'URL fora da loja selecionada.'}
    if session_status(store)['status'] in {'opening','waiting'}:return {'status':'LOGIN_PENDING','reason':f'Conclua o login de {label} na janela aberta.'}
    with slot:
        try:
            profile.mkdir(parents=True,exist_ok=True)
            with sync_playwright() as runtime:
                manager=account_context(runtime,store)
                context=manager.__enter__()
                try:
                    page=context.new_page()
                    if observer:page.on('response',observer)
                    response=page.goto(url,wait_until='domcontentloaded',timeout=timeout*1000)
                    try:
                        page.wait_for_function("""() => !!document.querySelector('.poly-card,.ui-pdp-title,[data-sqe="item"],.navbar__username,input[type="password"]') || /nenhum resultado|n[aã]o encontramos|verifique sua identidade/i.test(document.body?.innerText||'')""",timeout=min(timeout*1000,8000))
                    except Exception:pass
                    if handler:return handler(page,context)
                    return {'status':'OK','html':page.content(),'url':page.url,'http_status':response.status if response else None}
                finally:
                    if not page.is_closed():page.close()
                    manager.__exit__(None,None,None)
        except Exception as exc:return {'status':'BROWSER_ERROR','reason':str(exc)[:200]}
