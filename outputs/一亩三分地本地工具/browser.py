"""One owned Chrome profile; local-only control and bounded website operations."""
import asyncio
import json
import re
from datetime import datetime, timezone
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import mycdp
import requests
from seleniumbase import sb_cdp
from contracts import (AccountRequestError, BrowserConnectionError, session_result, login_result, logout_result, unread_summary,
                       RunStatus, SessionState)
from settings import (ROOT, WORKSPACE, STATE, PROFILE, CHROME, SITE, SITE_HOST, AUTH_HOST,
                      RPC_HOST, API_HOST, AUTH_URL, RPC_URL, API_URL, USERNAME, ACCOUNT_UID, PAGE_TIMEOUT, REQUEST_TIMEOUT_MS,
                      SESSION_COOKIE_DOMAINS, UPLOADER_URL, UPLOAD_TIMEOUT_MS, MEDIA_HOSTS,
                      LOGIN_METHOD, LOGIN_VERIFICATION_TIMEOUT, READ_RETRY_LIMIT, READ_RETRY_DELAY, WINDOWS,
                      CDP_CALL_TIMEOUT, BROWSER_SHUTDOWN_TIMEOUT, LOGIN_METHODS, WECHAT_QR_PATH, WECHAT_QR_FRAME_PREFIX,
                      WECHAT_QR_FILE, WECHAT_LOGIN_TIMEOUT, WECHAT_LOGIN_MIN_WAIT, WECHAT_LOGIN_MAX_WAIT, WINDOW_ON_SCREEN,
                      MACOS, CHROME_BUNDLE_ID)

if WINDOWS:
    import ctypes
    import ctypes.wintypes
    import msvcrt
else:
    import fcntl

WECHAT_QR_SOURCE = 'official WeChat Open Platform QR embedded by the site login page ' + AUTH_URL + WECHAT_QR_PATH


def _lock(handle, acquire):
    """Non-blocking one-byte lock on Windows; whole-file flock elsewhere."""
    if WINDOWS:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), (fcntl.LOCK_EX | fcntl.LOCK_NB) if acquire else fcntl.LOCK_UN)


def _hidden_window():
    if not WINDOWS:
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {'startupinfo': startup, 'creationflags': subprocess.CREATE_NO_WINDOW}


def _process_windows(pid):
    """Top-level window handles of one process (Windows). CDP can move the owned Chrome but the window was
    created hidden, so showing it to a person takes the OS call as well."""
    user32 = ctypes.windll.user32
    handles = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def visit(handle, _):
        owner = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner.value == pid and user32.GetWindow(handle, 4) == 0:
            handles.append(handle)
        return True
    user32.EnumWindows(visit, 0)
    return handles


# Captured at import: the Chrome launch is what callers double, and these short OS queries must not be handed that double.
_SHELL = subprocess.Popen


def _query(*command):
    """Stdout of a short OS command, or '' on any failure. Best effort by design: focus handling never fails a run."""
    try:
        process = _SHELL(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except Exception:
        return ''
    try:
        output, _ = process.communicate(timeout=5)
    except Exception:
        process.kill()
        return ''
    return output.strip()


def _front_app():
    """macOS: bundle id of the app that has focus, read before Chrome is launched and takes it."""
    session = _query('lsappinfo', 'front')
    match = re.search(r'"CFBundleIdentifier"="([^"]+)"', _query('lsappinfo', 'info', '-only', 'bundleid', session)) if session else None
    return match.group(1) if match else ''


def _give_focus_back(bundle_id):
    """macOS: hand focus back to the app that had it. Only an app still running, and never Chrome itself, so nothing
    gets launched and the owned Chrome is not the one activated. Needs no accessibility or automation permission."""
    if not bundle_id or bundle_id == CHROME_BUNDLE_ID or not _query('lsappinfo', 'find', 'bundleid=' + bundle_id):
        return False
    _query('open', '-b', bundle_id)
    return True


class Browser:
    def __init__(self, *, recover_login=True, deadline=None):
        self.recover_login = recover_login
        self.entry_url = SITE + '/next/daily-checkin' if recover_login else SITE
        self.login_attempted = False
        self.wechat = None
        self.read_retries = 0
        # Seconds for the whole session, or None: the daily run sets it; collection tasks may legitimately run long.
        self.deadline = deadline
        self.expired = False
        self.closing = threading.Event()
        self.watchdog = None
        self.sb = None
        self.process = None
        self.front_app = ''

    def __enter__(self):
        if not USERNAME or not ACCOUNT_UID:
            raise RuntimeError('account_not_configured')
        STATE.mkdir(parents=True, exist_ok=True)
        PROFILE.mkdir(parents=True, exist_ok=True)
        self.lock = (STATE / 'browser.lock').open('a+b')
        if self.lock.tell() == 0:
            self.lock.write(b'0')
            self.lock.flush()
        try:
            _lock(self.lock, True)
        except OSError:
            self.lock.close()
            raise RuntimeError('another_task_is_using_the_browser') from None
        self.sb = None
        self.process = None
        self.responses = []
        self.ids = {}
        self.media_responses = {}
        self.solver_calls = 0
        if self.deadline is not None:
            self.watchdog = threading.Thread(target=self._watch, name='browser-deadline', daemon=True)
            self.watchdog.start()
        try:
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            arguments = [str(CHROME), '--remote-debugging-address=127.0.0.1', f'--remote-debugging-port={port}',
                         f'--user-data-dir={PROFILE}', '--no-first-run', '--no-default-browser-check', '--window-size=1280,900']
            if WINDOWS:
                arguments.append('--window-position=-20000,-20000')  # Honoured there; macOS clamps a window back on screen.
            self.front_app = _front_app() if MACOS else ''
            self.process = subprocess.Popen(arguments + ['about:blank'], **_hidden_window(),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 15
            with requests.Session() as local:
                local.trust_env = False
                while True:
                    try:
                        if local.get(f'http://127.0.0.1:{port}/json/version', timeout=1).ok:
                            break
                    except requests.RequestException:
                        pass
                    if time.monotonic() >= deadline:
                        raise RuntimeError('chrome_profile_busy_or_start_failed')
                    time.sleep(0.3)
            self.sb = sb_cdp.Chrome('about:blank', host='127.0.0.1', port=port,
                headless=False, user_data_dir=str(PROFILE), browser_executable_path=str(CHROME), lang='zh-CN')
            self.sb.bring_active_window_to_front = lambda: None
            self.sb.add_handler(mycdp.network.ResponseReceived, self._response)
            if MACOS:
                self._park_window()
            self.goto(self.entry_url)
            if self.recover_login:
                self.ensure_account()
            return self
        except BaseException as error:
            self.__exit__(None, None, None)
            if self.expired and isinstance(error, Exception) and not isinstance(error, BrowserConnectionError):
                # The deadline landed inside the driver's own start-up wait; report it under its name.
                raise BrowserConnectionError('daily_run_timeout') from None
            raise

    def __exit__(self, *_):
        self.closing.set()  # The watchdog stands down, or finishes the loop it is in.
        if self.sb and not self.expired:  # Past the deadline the loop may hold a queued stop: never run it again.
            try:
                self._send(mycdp.browser.close(), timeout=BROWSER_SHUTDOWN_TIMEOUT, connection=self.sb.driver.connection)
            except Exception:
                pass
        if self.process:
            for stop in (None, self.process.terminate, self.process.kill):
                if stop:
                    stop()
                try:
                    self.process.wait(timeout=BROWSER_SHUTDOWN_TIMEOUT)
                    break
                except subprocess.TimeoutExpired:
                    continue
        try:
            _lock(self.lock, False)
        finally:
            self.lock.close()

    def check_active(self):
        if self.expired:
            raise BrowserConnectionError('daily_run_timeout')

    def _send(self, command, *, timeout=None, connection=None):
        """Every CDP round trip. The driver never fails a pending command when its socket drops, so the bound
        lives here, and past the deadline nothing is sent. A None result is the driver's own error report
        (it reconnects on the next call) and is left to the caller: a page error, not a lost link."""
        self.check_active()
        target = self.sb.page if connection is None else connection
        try:
            return self.sb.loop.run_until_complete(
                asyncio.wait_for(target.send(command), CDP_CALL_TIMEOUT if timeout is None else timeout))
        except Exception:
            # The bound expired, the watchdog stopped the loop, or the driver could not reconnect.
            raise BrowserConnectionError('daily_run_timeout' if self.expired else 'browser_connection_lost') from None

    def _watch(self):
        """Daemon thread. Past the deadline it ends the session from outside and keeps doing so until __exit__
        stands it down: the driver retries some waits without a bound, so one stop would only move the block."""
        if self.closing.wait(self.deadline):
            return
        self.expired = True  # First, so a released caller reports the deadline rather than a lost link.
        while not self.closing.is_set():
            sb, process = self.sb, self.process
            if sb is not None:
                try:
                    sb.loop.call_soon_threadsafe(sb.loop.stop)
                except RuntimeError:
                    pass  # The loop is already closed.
            if process is not None and process.poll() is None:
                process.kill()
            self.closing.wait(1)

    def _response(self, event):
        parsed = urlsplit(event.response.url)
        if parsed.hostname in MEDIA_HOSTS and parsed.hostname != SITE_HOST:
            # Media served from the site's asset hosts: kept by URL so read_bytes can take the body via CDP.
            self.media_responses[event.response.url] = event
            return
        if parsed.hostname not in {SITE_HOST, AUTH_HOST, RPC_HOST, API_HOST}:
            return
        if parsed.path.startswith('/cdn-cgi/'):
            return
        kind = str(event.type_)
        if kind not in {'ResourceType.DOCUMENT', 'ResourceType.FETCH', 'ResourceType.XHR'}:
            return
        headers = {str(k).lower(): str(v) for k, v in event.response.headers.items()}
        item = {'path': parsed.path, 'status': event.response.status,
                'challenge': headers.get('cf-mitigated') == 'challenge', 'host': parsed.hostname}
        self.responses.append(item)
        if parsed.hostname == RPC_HOST:
            self.ids[parsed.path] = event.request_id

    def evaluate(self, expression):
        result = self._send(mycdp.runtime.evaluate(expression, await_promise=True, return_by_value=True))
        if result is None:
            raise RuntimeError('page_expression_failed')
        value, error = result
        if error:
            raise RuntimeError('page_expression_failed')
        return value.value

    def wait_for(self, expression, timeout=PAGE_TIMEOUT, allow_solver=True):
        deadline = time.monotonic() + timeout
        start = time.monotonic()
        attempted = False
        while time.monotonic() < deadline:
            try:
                result = self.evaluate(expression)
                if result:
                    return result
            except BrowserConnectionError:
                raise
            except Exception:
                pass
            if allow_solver and not attempted and time.monotonic() - start > 10:
                # This method selects the CDP path, not the OS mouse path.
                try:
                    self.sb.solve_captcha()
                    self.solver_calls += 1
                except Exception:
                    pass
                attempted = True
            self.sb.sleep(0.7)
        raise RuntimeError('automatic_page_or_verification_timeout')

    def goto(self, url):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname not in {SITE_HOST, AUTH_HOST}:
            raise ValueError('unsupported_navigation_target')
        self.check_active()
        try:
            self.sb.get(url)
        except Exception:
            # The driver swallows its own load timeouts; whatever escapes is the link or the stopped loop.
            raise BrowserConnectionError('daily_run_timeout' if self.expired else 'browser_connection_lost') from None
        try:
            self.wait_for("!window._cf_chl_opt && !document.querySelector('#challenge-form') && (document.body?.innerText.length||0)>100")
        except RuntimeError as error:
            if str(error) == 'automatic_page_or_verification_timeout':
                if self.has_access_challenge():
                    raise RuntimeError('page_challenge_not_resolved') from None
            raise
        self.sb.sleep(0.8)

    def has_access_challenge(self):
        try:
            return bool(self.evaluate("!!(window._cf_chl_opt || document.querySelector('#challenge-form'))"))
        except Exception:
            return False

    def _read_response(self, url, *, html=False):
        # Only the two GET readers use this boundary; form submissions never enter it.
        expression = r"""(async()=>{let metadata=null;try{
            const r=await fetch(__URL__,{credentials:'include',signal:AbortSignal.timeout(__TIMEOUT__)});
            metadata={status:r.status,challenge:r.headers.get('cf-mitigated')==='challenge',url:r.url,charset:null};
            if(metadata.challenge||r.status===401)return {...metadata,text:'',html:''};
            if(!__HTML__)return {...metadata,text:await r.text()};
            const buffer=await r.arrayBuffer();
            const prefix=new TextDecoder('windows-1252').decode(buffer.slice(0,4000));
            const charset=/charset\s*=\s*["']?([a-z0-9_-]+)/i.exec(r.headers.get('content-type')||'')?.[1] || /charset\s*=\s*["']?([a-z0-9_-]+)/i.exec(prefix)?.[1] || 'utf-8';
            return {...metadata,charset,html:new TextDecoder(charset).decode(buffer)};
        }catch(e){
            if(metadata&&metadata.status!==200)return {...metadata,body_error:true,text:'',html:''};
            return {transport_error:['TimeoutError','AbortError'].includes(e?.name)?'network_timeout':e?.name==='TypeError'?'network_unavailable':'browser_read_failed'};
        }})()""".replace('__TIMEOUT__', str(REQUEST_TIMEOUT_MS)).replace('__HTML__', json.dumps(html)).replace('__URL__', json.dumps(url))
        for attempt in range(READ_RETRY_LIMIT + 1):
            response = self.evaluate(expression)
            reason = response.get('transport_error')
            if reason is None:
                return response
            if reason not in {'network_timeout', 'network_unavailable'}:
                raise RuntimeError('browser_read_failed')
            if attempt == READ_RETRY_LIMIT:
                raise RuntimeError(reason)
            self.read_retries += 1
            self.sb.sleep(READ_RETRY_DELAY)

    # The Discuz form handlers this tool may post to; anything else is refused before a request is built.
    FORM_TARGETS = {('spacecp', 'favorite')}

    def submit_form(self, url, fields):
        """The one write boundary for Discuz forms: posted once, never retried, decoded as the site answers.

        A transport failure after the request left is reported as unconfirmed, because the site may have
        acted; the caller must read the real state back instead of posting again.
        """
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        if (parsed.scheme != 'https' or parsed.hostname != SITE_HOST or parsed.path != '/bbs/home.php'
                or (query.get('mod', [None])[0], query.get('ac', [None])[0]) not in self.FORM_TARGETS):
            raise ValueError('unsupported_form_target')
        if not isinstance(fields, dict) or not fields.get('formhash'):
            raise ValueError('form_token_missing')
        try:
            body = urlencode(fields, encoding='gbk', errors='strict')  # the site reads GBK form data
        except UnicodeEncodeError:
            raise ValueError('form_text_not_encodable') from None
        target = url + ('&' if parsed.query else '?') + 'inajax=1'
        expression = r"""(async()=>{try{
            const r=await fetch(__URL__,{method:'POST',credentials:'include',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:__BODY__,signal:AbortSignal.timeout(__TIMEOUT__)});
            const buffer=await r.arrayBuffer();
            const prefix=new TextDecoder('windows-1252').decode(buffer.slice(0,4000));
            const charset=/charset\s*=\s*["']?([a-z0-9_-]+)/i.exec(r.headers.get('content-type')||'')?.[1] || /charset\s*=\s*["']?([a-z0-9_-]+)/i.exec(prefix)?.[1] || 'utf-8';
            return {status:r.status,challenge:r.headers.get('cf-mitigated')==='challenge',url:r.url,text:new TextDecoder(charset).decode(buffer)};
        }catch(e){return {transport_error:true};}})()""".replace('__TIMEOUT__', str(REQUEST_TIMEOUT_MS)).replace('__URL__', json.dumps(target)).replace('__BODY__', json.dumps(body))
        response = self.evaluate(expression)
        if response.get('transport_error'):
            raise RuntimeError('form_submission_unconfirmed')
        if response['challenge']:
            raise RuntimeError('form_challenge_not_resolved')
        if response['status'] != 200:
            raise RuntimeError('form_http_' + str(response['status']))
        return response['text']

    # JSON writes to the site's API this tool may send; method and path are both part of the listing.
    API_WRITES = (('PUT', r'/api/posts/[0-9]+/reactions'), ('DELETE', r'/api/posts/[0-9]+/reactions'),
                  ('POST', r'/api/threads'), ('POST', r'/api/threads/[0-9]+/posts'),
                  ('POST', r'/api/v2/attachment/upload-init'), ('POST', r'/api/v2/attachment/upload-complete'),
                  ('DELETE', r'/api/user/unused-attachments/[0-9]+'),
                  ('POST', r'/api/videos/upload-url'), ('POST', r'/api/videos/upload'))
    # JSON reads from the site's API; nothing here changes site state.
    API_READS = (r'/api/threads/[0-9]+/reply-perm', r'/api/v3/threads/[0-9]+', r'/api/user/unused-attachments')

    def upload_file(self, token, name, mime, data, url=None):
        """Send one file's bytes to an uploader, once: the site's image uploader with the token
        upload-init issued, or the one-off https upload URL the site's video API just handed out (then
        no token field, as the site's own client does). Neither takes cookies; the bytes travel through
        the page as a Blob. A lost response is unconfirmed: the caller decides, nothing is re-sent."""
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise ValueError('upload_arguments_invalid')
        if url is None:
            if not isinstance(token, str) or not token:
                raise ValueError('upload_arguments_invalid')
            target, extra = UPLOADER_URL, "form.append('upload_token',__TOKEN__);"
        else:
            if not isinstance(url, str) or urlsplit(url).scheme != 'https':
                raise ValueError('upload_arguments_invalid')
            target, extra = url, ''
        import base64
        expression = (r"""(async()=>{try{
            const bytes=Uint8Array.from(atob(__DATA__),c=>c.charCodeAt(0));
            const form=new FormData();form.append('file',new Blob([bytes],{type:__MIME__}),__NAME__);""" + extra + r"""
            const r=await fetch(__URL__,{method:'POST',body:form,signal:AbortSignal.timeout(__TIMEOUT__)});
            return {status:r.status,text:await r.text()};
        }catch(e){return {transport_error:true};}})()""").replace('__TIMEOUT__', str(UPLOAD_TIMEOUT_MS)).replace(
            '__URL__', json.dumps(target)).replace('__DATA__', json.dumps(base64.b64encode(bytes(data)).decode('ascii'))).replace(
            '__MIME__', json.dumps(mime)).replace('__NAME__', json.dumps(name)).replace('__TOKEN__', json.dumps(token or ''))
        response = self.evaluate(expression)
        if response.get('transport_error'):
            raise RuntimeError('upload_unconfirmed')
        if not 200 <= response['status'] < 300:
            raise RuntimeError('upload_http_' + str(response['status']))
        try:
            return json.loads(response['text'])
        except ValueError:
            if url is not None:
                # A one-off upload address acknowledges with a plain body (seen live); the receipt that
                # matters is the registration call the caller makes next, so 2xx is enough here.
                return {}
            raise RuntimeError('upload_returned_non_json') from None

    def api_get(self, path):
        """A listed read-only API path, fetched with the signed-in cookies and returned as parsed JSON."""
        if not any(re.fullmatch(pattern, path) for pattern in self.API_READS):
            raise ValueError('unsupported_api_target')
        response = self._read_response(API_URL + path)
        if response['challenge']:
            raise RuntimeError('api_challenge_not_resolved')
        if response['status'] == 401:
            raise RuntimeError('login_required')
        try:
            return {'status': response['status'], 'body': json.loads(response['text'])}
        except ValueError:
            raise RuntimeError('api_returned_non_json') from None

    def api_request(self, method, path, body):
        """The one JSON write boundary to the site's API: a listed method and path, sent once with the
        signed-in cookies, never retried. The parsed body is returned with the HTTP status; what it means
        is the caller's business, and a lost request is unconfirmed rather than repeated."""
        if not any(method == allowed and re.fullmatch(pattern, path) for allowed, pattern in self.API_WRITES):
            raise ValueError('unsupported_api_target')
        expression = r"""(async()=>{try{
            const r=await fetch(__URL__,{method:__METHOD__,credentials:'include',headers:{'Content-Type':'application/json'},body:__BODY__,signal:AbortSignal.timeout(__TIMEOUT__)});
            return {status:r.status,challenge:r.headers.get('cf-mitigated')==='challenge',text:await r.text()};
        }catch(e){return {transport_error:true};}})()""".replace('__TIMEOUT__', str(REQUEST_TIMEOUT_MS)).replace(
            '__URL__', json.dumps(API_URL + path)).replace('__METHOD__', json.dumps(method)).replace('__BODY__', json.dumps(json.dumps(body)))
        response = self.evaluate(expression)
        if response.get('transport_error'):
            raise RuntimeError('api_submission_unconfirmed')
        if response['challenge']:
            raise RuntimeError('api_challenge_not_resolved')
        if response['status'] == 401:
            raise RuntimeError('login_required')
        try:
            parsed = json.loads(response['text'])
        except ValueError:
            raise RuntimeError('api_returned_non_json') from None
        return {'status': response['status'], 'body': parsed}

    def rpc(self, method, data=None):
        if method not in {'user.me', 'dailyQuestion.get', 'credit.getCreditLogs', 'favorite.getFavorites', 'forum.get',
                          'notificationV2.getNewPrompts', 'notificationV2.getNotifications'}:
            raise ValueError('unsupported_read_method')
        url = RPC_URL + '/trpc/' + method + '?batch=1&input=' + quote(json.dumps({'0': {'json': data}}, separators=(',', ':')))
        response = self._read_response(url)
        if response['challenge']:
            raise RuntimeError('api_challenge_not_resolved')
        if response['status'] == 401:
            raise RuntimeError('login_required')
        if response.get('body_error'):
            raise RuntimeError('account_http_error' if method == 'user.me' else 'api_http_error')
        try:
            body = json.loads(response['text'])
        except ValueError:
            raise RuntimeError('api_returned_non_json') from None
        item = body[0] if isinstance(body, list) else body
        if 'error' in item:
            detail = item['error'].get('json', {})
            if method == 'user.me' and detail.get('data', {}).get('code') == 'UNAUTHORIZED':
                raise RuntimeError('login_required')
            message = detail.get('message', 'api_rejected_request')
            if method == 'user.me':
                raise AccountRequestError('account_request_rejected')
            raise RuntimeError(str(message)[:200])
        if method == 'user.me' and response['status'] != 200:
            raise RuntimeError('account_http_error')
        return item['result']['data']['json']

    def profile(self):
        """The identity read, validated and returned whole; account() keeps the trimmed view."""
        user = self.rpc('user.me')
        if user is None:
            raise RuntimeError('login_required')
        if (not isinstance(user, dict) or type(user.get('uid')) is not int or user['uid'] <= 0
                or not isinstance(user.get('username'), str) or not user['username'].strip()):
            raise RuntimeError('account_response_invalid')
        if user.get('username') != USERNAME or user.get('uid') != ACCOUNT_UID:
            raise RuntimeError('unexpected_account')
        return user

    def account(self):
        user = self.profile()
        return {'uid': user['uid'], 'username': user['username'],
                'rice': user.get('user_count', {}).get('extcredits1'), 'app_status': user.get('app_status', {})}

    def ensure_account(self):
        try:
            return self.account()
        except RuntimeError as error:
            if isinstance(error, AccountRequestError) or str(error) != 'login_required':
                raise
        from secure import load_credentials
        credentials = values = None
        try:
            try:
                credentials = load_credentials()
            except ValueError:
                raise RuntimeError('saved_credentials_invalid') from None
            if (not isinstance(credentials, dict) or credentials.get('username') != USERNAME
                    or not isinstance(credentials.get('password'), str) or not credentials['password']):
                raise RuntimeError('saved_credentials_invalid')
            self.goto(AUTH_URL + '/login?url=' + quote(self.entry_url))
            self.wait_for("document.readyState==='complete' && !!document.querySelector('#username') && !!document.querySelector('#password')", allow_solver=False)
            values = json.dumps([['username', credentials['username']], ['password', credentials['password']]])
            filled = self.evaluate("(()=>{for(const [id,value] of " + values + "){const e=document.getElementById(id);Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,value);e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));}return document.forms[0].checkValidity()&&!!document.getElementById('password').value;})()")
            if not filled:
                raise RuntimeError('login_fields_not_accepted')
            if self.evaluate("!!document.querySelector('.cf-turnstile,input[name=\"cf-turnstile-response\"]')"):
                try:
                    self.wait_for("!!document.querySelector('input[name=\"cf-turnstile-response\"]')?.value", timeout=LOGIN_VERIFICATION_TIMEOUT)
                except BrowserConnectionError:
                    raise
                except RuntimeError:
                    raise RuntimeError('login_challenge_not_resolved') from None
            marker = 'window.__toolkitLoginDocument'
            self.login_attempted = True
            self.evaluate(marker + "=true;document.getElementById('submit').click()")
            try:
                self.wait_for("!window._cf_chl_opt && !document.querySelector('#challenge-form') && (location.hostname==="
                    + json.dumps(SITE_HOST) + " || (location.hostname===" + json.dumps(AUTH_HOST)
                    + " && !" + marker + " && !!document.querySelector('#username') && !!document.querySelector('#password')))",
                    timeout=PAGE_TIMEOUT)
            except BrowserConnectionError:
                raise
            except RuntimeError:
                reason = 'page_challenge_not_resolved' if self.has_access_challenge() else 'login_submission_unconfirmed'
                raise RuntimeError(reason) from None
            if self.evaluate('location.hostname===' + json.dumps(AUTH_HOST)):
                raise RuntimeError('login_rejected')
            return self.account()
        except RuntimeError:
            raise
        except Exception:
            raise RuntimeError('automatic_login_failed') from None
        finally:
            credentials = None
            values = None

    def _cdp(self, command):
        return self._send(command)

    def _park_window(self):
        """macOS keeps every window on screen and activates Chrome at launch, so the owned window is minimized and
        focus is handed back. Best effort: failing here leaves a visible window, it does not fail the run."""
        try:
            self._place_window(False)
        except Exception:
            pass

    def set_window_visible(self, visible):
        """Bring the owned Chrome on screen for a person, or put it back the way it started. Only scan login needs a
        visible window; every other flow keeps it out of sight."""
        self._place_window(visible)

    def _place_window(self, visible):
        """On screen and in front, or out of sight: off-screen and hidden on Windows, minimized on macOS with focus
        handed back to the app that had it."""
        window, _ = self._cdp(mycdp.browser.get_window_for_target())
        if visible:
            # Leaving the minimized state and moving are two calls: Chrome ignores coordinates sent with a state change.
            self._cdp(mycdp.browser.set_window_bounds(window, mycdp.browser.Bounds(window_state=mycdp.browser.WindowState.NORMAL)))
            bounds = mycdp.browser.Bounds(left=WINDOW_ON_SCREEN[0], top=WINDOW_ON_SCREEN[1], width=1280, height=900)
        elif WINDOWS:
            bounds = mycdp.browser.Bounds(left=-20000, top=-20000)
        else:
            bounds = mycdp.browser.Bounds(window_state=mycdp.browser.WindowState.MINIMIZED)
        self._cdp(mycdp.browser.set_window_bounds(window, bounds))
        if visible:
            self._cdp(mycdp.page.bring_to_front())
        if WINDOWS and self.process is not None:
            for handle in _process_windows(self.process.pid):
                ctypes.windll.user32.ShowWindow(handle, 5 if visible else 0)  # SW_SHOW / SW_HIDE
        elif MACOS and not visible:
            _give_focus_back(self.front_app)

    def capture_png(self):
        import base64
        return base64.b64decode(self._cdp(mycdp.page.capture_screenshot(format_='png')))

    def wechat_login(self, wait_seconds, qr_path):
        """Scan login on the site's own WeChat page (#51). A valid session is reused without showing anything.

        The official QR iframe must appear on the page; it is then shown in the owned Chrome window and as a
        screenshot file, and the call waits (bounded) for the page to come back to the site. The QR frame is
        cross-origin and its scanned/expired states are not observable, and the site states no lifetime, so
        neither is claimed. Success is only the usual identity read; a foreign identity is logged out again.
        The window goes back off-screen and the file is removed however the wait ends."""
        try:
            return self.account()
        except RuntimeError as error:
            if isinstance(error, AccountRequestError) or str(error) != 'login_required':
                raise
        self.wechat = {'qr_source': WECHAT_QR_SOURCE, 'displayed_in': 'toolkit_chrome_window',
                       'qr_path': str(qr_path), 'qr_file_removed': False, 'displayed_at': None, 'expires_at': None,
                       'wait_limit': wait_seconds, 'waited_seconds': None, 'foreign_session_cleared': False}
        self.goto(AUTH_URL + WECHAT_QR_PATH)
        try:
            self.wait_for("!!document.querySelector('iframe[src^=" + json.dumps(WECHAT_QR_FRAME_PREFIX) + "]')",
                          allow_solver=False)
        except BrowserConnectionError:
            raise
        except RuntimeError:
            raise RuntimeError('wechat_qr_not_shown') from None
        started = time.monotonic()
        try:
            try:
                self.set_window_visible(True)
                Path(qr_path).parent.mkdir(parents=True, exist_ok=True)
                Path(qr_path).write_bytes(self.capture_png())
            except BrowserConnectionError:
                raise
            except Exception:
                raise RuntimeError('wechat_display_failed') from None
            self.wechat['displayed_at'] = datetime.now(timezone.utc).isoformat()
            self.login_attempted = True
            try:
                while True:
                    try:
                        if self.evaluate('location.hostname') == SITE_HOST:
                            break
                    except BrowserConnectionError:
                        raise
                    except RuntimeError:
                        pass  # Mid-navigation the page has no answer; the next second has.
                    if time.monotonic() - started >= wait_seconds:
                        raise RuntimeError('wechat_login_timeout')
                    self.sb.sleep(1)
            except KeyboardInterrupt:
                raise RuntimeError('wechat_login_cancelled') from None
        finally:
            self.wechat['waited_seconds'] = round(time.monotonic() - started, 1)
            try:
                Path(qr_path).unlink()
                self.wechat['qr_file_removed'] = True
            except OSError:
                self.wechat['qr_file_removed'] = not Path(qr_path).exists()
            try:
                self.set_window_visible(False)
            except Exception:
                pass
        try:
            return self.account()
        except RuntimeError as error:
            if str(error) == 'unexpected_account':
                self.clear_site_cookies()
                self.wechat['foreign_session_cleared'] = True
            raise

    def clear_site_cookies(self):
        """Delete this profile's cookies for the site's own domains, one by one; every other cookie stays.

        The profile is the tool's own, but the reset is still scoped by domain so that nothing unrelated
        that may live in it is touched. The count returned is what was actually deleted.
        """
        removed = 0
        for cookie in self._cdp(mycdp.storage.get_cookies()):
            host = cookie.domain.lstrip('.')
            if any(host == domain or host.endswith('.' + domain) for domain in SESSION_COOKIE_DOMAINS):
                self._cdp(mycdp.network.delete_cookies(name=cookie.name, domain=cookie.domain, path=cookie.path))
                removed += 1
        return removed

    def credit_logs(self):
        self.goto(SITE + '/home/credit')
        self.wait_for("document.querySelectorAll('table tbody tr').length>0", allow_solver=False)
        paths = [path for path in self.ids if 'credit.getCreditLogs' in path]
        if not paths:
            raise RuntimeError('credit_log_response_missing')
        response = self._send(mycdp.network.get_response_body(self.ids[paths[-1]]))
        if response is None:
            raise RuntimeError('credit_log_response_missing')
        body, encoded = response
        if encoded:
            import base64
            body = base64.b64decode(body).decode('utf-8')
        batches = json.loads(body)
        for item in batches:
            data = item.get('result', {}).get('data', {}).get('json', {})
            if isinstance(data, dict) and isinstance(data.get('log'), list):
                return data['log']
        raise RuntimeError('credit_log_data_missing')

    def click_text(self, text):
        expression = "(()=>{const b=[...document.querySelectorAll('button')].find(e=>e.textContent.trim()===" + json.dumps(text) + ");if(!b||b.disabled)return false;b.click();return true;})()"
        if not self.evaluate(expression):
            raise RuntimeError('button_not_ready')

    def read_bytes(self, url, max_bytes):
        """One media file from the site's own hosts, fetched once with the session cookies and returned
        as bytes with the type the server declared. Anything larger than `max_bytes` is refused before
        it is read in full; any other host is refused before a request is built."""
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname not in MEDIA_HOSTS:
            raise ValueError('unsupported_media_host')
        import base64
        if parsed.hostname != SITE_HOST:
            return self._read_asset_via_cdp(url, max_bytes)
        expression = r"""(async()=>{try{
            const r=await fetch(__URL__,{credentials:'include',signal:AbortSignal.timeout(__TIMEOUT__)});
            const declared=Number(r.headers.get('content-length')||0);
            if(declared>__MAX__)return {status:r.status,too_large:true,declared};
            const buffer=await r.arrayBuffer();
            if(buffer.byteLength>__MAX__)return {status:r.status,too_large:true,declared:buffer.byteLength};
            const bytes=new Uint8Array(buffer);let binary='';
            for(let i=0;i<bytes.length;i+=0x8000)binary+=String.fromCharCode.apply(null,bytes.subarray(i,i+0x8000));
            return {status:r.status,challenge:r.headers.get('cf-mitigated')==='challenge',mime:(r.headers.get('content-type')||'').split(';')[0].trim(),data:btoa(binary)};
        }catch(e){return {transport_error:true};}})()""".replace('__URL__', json.dumps(url)).replace(
            '__TIMEOUT__', str(UPLOAD_TIMEOUT_MS)).replace('__MAX__', str(int(max_bytes)))
        response = self.evaluate(expression)
        if response.get('transport_error'):
            raise RuntimeError('media_read_failed')
        if response.get('too_large'):
            raise RuntimeError('media_too_large')
        if response.get('challenge'):
            raise RuntimeError('page_challenge_not_resolved')
        if response['status'] != 200:
            raise RuntimeError('media_http_' + str(response['status']))
        return {'mime': response['mime'], 'data': base64.b64decode(response['data'])}

    def _read_asset_via_cdp(self, url, max_bytes):
        """The site's asset hosts answer image loads but not cross-origin fetches (no CORS header), so the
        file is loaded the way the page loads it, as an image, and its bytes are taken from the browser's
        own network record through CDP; size is checked on the declared length before the body is read."""
        import base64
        self.media_responses.pop(url, None)
        self.evaluate("(async()=>await new Promise(done=>{const i=new Image();i.onload=()=>done('load');i.onerror=()=>done('error');i.src=" + json.dumps(url) + ";}))()")
        deadline = time.monotonic() + 10
        while url not in self.media_responses and time.monotonic() < deadline:
            self.sb.sleep(0.2)
        event = self.media_responses.get(url)
        if event is None:
            raise RuntimeError('media_read_failed')
        headers = {str(k).lower(): str(v) for k, v in event.response.headers.items()}
        if headers.get('cf-mitigated') == 'challenge':
            raise RuntimeError('page_challenge_not_resolved')
        if event.response.status != 200:
            raise RuntimeError('media_http_' + str(event.response.status))
        declared = int(headers.get('content-length') or 0)
        if declared > max_bytes:
            raise RuntimeError('media_too_large')
        body, encoded = self._cdp(mycdp.network.get_response_body(event.request_id))
        data = base64.b64decode(body) if encoded else body.encode('utf-8')
        if len(data) > max_bytes:
            raise RuntimeError('media_too_large')
        return {'mime': headers.get('content-type', '').split(';')[0].strip(), 'data': data}

    def read_html(self, url):
        if urlsplit(url).hostname != SITE_HOST:
            raise ValueError('unsupported_read_target')
        response = self._read_response(url, html=True)
        self.last_read = {key: response[key] for key in ['status', 'challenge', 'charset', 'url']}
        if response['challenge']:
            self.goto(url)
            self.last_read['url'] = self.evaluate('location.href')
            return self.evaluate('document.documentElement.outerHTML')
        if response['status'] != 200:
            raise RuntimeError('thread_http_' + str(response['status']))
        return response['html']


def session_status():
    """Inspect the configured account's existing session without login or daily actions."""
    try:
        with Browser(recover_login=False) as browser:
            browser.account()
        return session_result()
    except Exception as error:
        return session_result(error)


def get_unread_counts():
    """Unread counters the site already returns with the identity read; no list is opened, nothing is marked read.

    A login that has lapsed is a failed read with unknown counts, never a quiet zero.
    """
    read_at = datetime.now(timezone.utc).isoformat()
    try:
        with Browser() as browser:
            user = browser.profile()
    except Exception as error:
        session = session_result(error)
        return {'status': RunStatus.FAILED, 'session_state': session['session_state'],
                **unread_summary(None, read_at), 'scope': 'currently_visible_content', 'error': session['error']}
    return {'status': RunStatus.COMPLETE, 'session_state': SessionState.LOGGED_IN,
            **unread_summary(user, read_at), 'scope': 'currently_visible_content', 'error': None}


def session_logout():
    """Clear the dedicated profile's site login and verify the site no longer knows the account.

    Only the site's cookie domains inside the tool's own Chrome profile are touched: saved credentials,
    the account file, the database and any other browser profile stay. The verification is the same
    identity read as session_status with recovery disabled, so checking the result can never log back in.
    """
    scope = {'profile': str(PROFILE), 'domains': list(SESSION_COOKIE_DOMAINS)}
    removed = None
    verified = False
    error = None
    try:
        with Browser(recover_login=False) as browser:
            removed = browser.clear_site_cookies()
            try:
                browser.account()
            except RuntimeError as after:
                if str(after) != 'login_required':
                    raise
                verified = True
    except Exception as caught:
        error = caught
    return logout_result(error, cookies_removed=removed, verified=verified, scope=scope)


def session_login(method=LOGIN_METHOD, wait_seconds=WECHAT_LOGIN_TIMEOUT, qr_path=None):
    """Restore the configured account once: from locally protected credentials, or by a WeChat scan the
    account owner confirms in person. Arguments are checked before any browser starts."""
    browser = None
    error = None
    try:
        if method not in LOGIN_METHODS:
            raise RuntimeError('unsupported_login_method')
        if method == 'wechat':
            if type(wait_seconds) is not int or not WECHAT_LOGIN_MIN_WAIT <= wait_seconds <= WECHAT_LOGIN_MAX_WAIT:
                raise RuntimeError('invalid_wait_seconds')
            qr_file = Path(qr_path) if qr_path else WECHAT_QR_FILE
        browser = Browser(recover_login=False)
        with browser:
            if method == 'wechat':
                browser.wechat_login(wait_seconds, qr_file)
            else:
                browser.ensure_account()
    except Exception as failure:
        error = failure
    return login_result(error, configured=bool(USERNAME and ACCOUNT_UID),
                        login_attempted=bool(browser and browser.login_attempted),
                        wechat=browser.wechat if browser else None)
