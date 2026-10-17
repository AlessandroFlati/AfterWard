"""Same-origin HTTP adapter for isolated demo and loopback personal workspaces."""
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit, unquote

from .discharge_flow import FlowError
from .discharge_service import WorkspaceStore

WEB = Path(__file__).parent / 'web'
COOKIE = 'afterward_workspace'
TAIL_HOST = os.environ.get('AFTERWARD_TAILSCALE_HOST', 'tailscale.invalid')
TAIL_BASE = '/lablab-amd/workspace'


def handle_http(handler, store, base='', *, allowed_hosts):
    host = handler.headers.get('Host', '')
    route = urlsplit(handler.path).path
    if not (route == base or route.startswith(base + '/')):
        return False
    route = route[len(base):] or '/'
    cookie_name = 'afterward_private_workspace' if store.personal else COOKIE
    def respond(status, value, content_type='application/json; charset=utf-8', extra=None):
        body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        handler.send_response(status)
        headers = {'Content-Type': content_type, 'Content-Length': str(len(body)), 'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}
        headers.update(extra or {})
        for key, value in headers.items():
            handler.send_header(key, value)
        handler.end_headers()
        if handler.command != 'HEAD':
            handler.wfile.write(body)
    try:
        if route == '/health' and handler.command in ('GET', 'HEAD') and host in allowed_hosts | {'healthcheck.railway.app'}:
            respond(200, {'status': 'ok', 'application': 'AfterWard'})
            return True
        if host not in allowed_hosts:
            respond(403, {'error': 'host', 'detail': 'Open AfterWard through its configured address.'})
            return True
        if base and urlsplit(handler.path).path == base and handler.command in ('GET', 'HEAD'):
            respond(308, b'', extra={'Location': base + '/'})
            return True
        if route == '/api/config' and handler.command == 'GET':
            respond(200, {'personal': store.personal, 'pdf_max_pages': 5, 'pdf_max_bytes': 12 * 1024 * 1024,
                'ocr_provider': 'amd_api' if store.page_transcriber else 'local' if store.local_ocr else 'disabled',
                'ai_enabled': store.ai is not None,
                'packaged_uploads_only': store.allowed_pdf_digests is not None,
                'share_base': 'https://' + TAIL_HOST + TAIL_BASE if getattr(handler.server, 'tailscale', False) else None})
            return True
        if route == '/staff' and store.personal:
            respond(403, {'error': 'role', 'detail': 'This personal workspace has no connected ward.'})
            return True
        if route in ('/judge-guide', '/presentation') and handler.command in ('GET', 'HEAD'):
            respond(200, (WEB / (route[1:] + '.html')).read_bytes(), 'text/html; charset=utf-8')
            return True
        sample = re.fullmatch(r'/examples/(letter|rehab|appointment|discharge-scan)\.pdf', route)
        if sample and not store.personal and handler.command in ('GET', 'HEAD'):
            respond(200, (WEB.parent / 'assets/demo' / (sample[1] + '.pdf')).read_bytes(), 'application/pdf',
                    extra={'Content-Disposition': 'attachment; filename="' + sample[1] + '.pdf"'})
            return True
        if route in ('/','/patient','/staff') and handler.command in ('GET','HEAD'):
            respond(200, (WEB / 'discharge.html').read_bytes(), 'text/html; charset=utf-8')
            return True
        asset = {'/discharge.js': ('discharge.js','text/javascript'), '/discharge.css': ('discharge.css','text/css')}.get(route)
        if asset and handler.command in ('GET','HEAD'):
            respond(200, (WEB / asset[0]).read_bytes(), asset[1] + '; charset=utf-8')
            return True
        if re.fullmatch(r'/font-[a-z0-9-]+\.woff2?', route) and handler.command in ('GET','HEAD'):
            path = WEB / route[1:]
            if path.is_file():
                respond(200, path.read_bytes(), 'font/woff2' if path.suffix == '.woff2' else 'font/woff')
                return True
        if handler.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise FlowError('Cross-site access is not allowed.')
        cookies = SimpleCookie()
        cookies.load(handler.headers.get('Cookie',''))
        token = cookies[cookie_name].value if cookie_name in cookies else ''
        document_match = re.fullmatch(r'/api/(patient|staff)/documents/(letter|rehab|appointment)/([01])', route)
        if handler.command == 'GET' and document_match:
            page = int(document_match[3])
            respond(200, store.document_asset(token, document_match[1], document_match[2], page),
                'image/png' if page else 'application/pdf', extra={'Content-Disposition':'inline; filename="document.pdf"'})
            return True
        asset_match = re.fullmatch(r'/api/(patient|staff)/uploads/([a-f0-9]{32})/([0-5])', route)
        if handler.command == 'GET' and asset_match:
            page = int(asset_match[3])
            respond(200, store.asset(token, asset_match[1], asset_match[2], page),
                'image/png' if page else 'application/pdf',
                extra={'Content-Disposition': 'inline' if page else 'inline; filename="document.pdf"'})
            return True
        match = re.fullmatch(r'/api/(patient|delegate|staff)(?:/([a-z_]+))?', route)
        if handler.command == 'GET' and match and not match[2]:
            respond(200, store.view(token, match[1]))
            return True
        if handler.command != 'POST':
            respond(404, {'error': 'not_found','detail': 'Page unavailable.'})
            return True
        if handler.headers.get('Origin') not in ('http://' + host, 'https://' + host) or handler.headers.get('X-AfterWard-Request') != '1':
            raise FlowError('Open AfterWard before continuing.')
        if handler.headers.get('Transfer-Encoding'):
            raise FlowError('Invalid request format.')
        length = int(handler.headers.get('Content-Length','0'))
        if match and match[2] == 'upload':
            if handler.headers.get('Content-Type') != 'application/pdf' or not 0 < length <= 12 * 1024 * 1024:
                raise FlowError('Choose a valid PDF, up to 12 MiB.')
            revision = int(handler.headers.get('X-AfterWard-Revision', '-1'))
            csrf = handler.headers.get('X-AfterWard-CSRF')
            store.validate_upload(token, match[1], csrf, revision)
            data = handler.rfile.read(length)
            if len(data) != length:
                raise FlowError('Upload interrupted. Try again.')
            respond(200, store.upload(token, match[1], csrf, revision, data,
                unquote(handler.headers.get('X-AfterWard-Filename', 'PDF document'))))
            return True
        if handler.headers.get('Content-Type','').split(';')[0] != 'application/json':
            raise FlowError('Invalid request format.')
        if not 0 < length <= 512000:
            raise FlowError('Request is empty or too large.')
        body = json.loads(handler.rfile.read(length))
        if type(body) is not dict:
            raise FlowError('Invalid request.')
        if route == '/api/join':
            token, joined_role = store.join(body.get('code'))
            secure = '; Secure' if handler.headers.get('Origin','').startswith('https:') else ''
            respond(200, {'role':joined_role}, extra={'Set-Cookie':f'{cookie_name}={token}; Path={base or "/"}; HttpOnly; SameSite=Strict; Max-Age=43200{secure}'})
        elif route == '/api/invite':
            respond(200, store.invite(token, handler.headers.get('X-AfterWard-CSRF'), body.get('role')))
        elif route == '/api/start':
            if body.get('acknowledged') is not True:
                raise FlowError('Read the demo conditions.')
            token = store.create(body.get('route','hospital'))
            secure = '; Secure' if handler.headers.get('Origin','').startswith('https:') else ''
            respond(201, {'started': True}, extra={'Set-Cookie': f'{cookie_name}={token}; Path={base or "/"}; HttpOnly; SameSite=Strict; Max-Age=43200{secure}'})
        elif route == '/api/delete':
            store.delete(token, handler.headers.get('X-AfterWard-CSRF'))
            respond(200, {'deleted': True}, extra={'Set-Cookie': f'{cookie_name}=; Path={base or "/"}; HttpOnly; SameSite=Strict; Max-Age=0'})
        elif match and match[2] == 'read_page':
            respond(200, store.read_page(token, match[1], handler.headers.get('X-AfterWard-CSRF'),
                body.get('revision'), body.get('upload_id'), body.get('page')))
        elif match and match[2] == 'start_ai':
            respond(202, store.start_ai(token, match[1], handler.headers.get('X-AfterWard-CSRF'), body))
        elif match and match[2]:
            respond(200, store.mutate(token, match[1], handler.headers.get('X-AfterWard-CSRF'), match[2], body))
        else:
            respond(404, {'error':'not_found','detail':'Action unavailable.'})
    except (FlowError, ValueError, KeyError, TypeError) as exc:
        handler.close_connection = True
        from .documents import DocumentError
        detail = str(exc) if isinstance(exc, (FlowError, DocumentError)) else 'Invalid request. Refresh and try again.'
        respond(409, {'error': 'request_rejected', 'detail': detail})
    return True


class WorkspaceServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, data, *, personal=False, tailscale=False, page_transcriber=None, public_host=None,
                 allowed_pdf_digests=None, local_ocr=True, ai=None):
        if personal and tailscale:
            raise ValueError('The personal workspace cannot be mounted on Tailscale.')
        if personal and address[0] not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError('The personal workspace must stay on loopback.')
        self.tailscale = tailscale
        if public_host and (personal or tailscale or not re.fullmatch(r'[a-zA-Z0-9.-]+(?::[0-9]{1,5})?', public_host)):
            raise ValueError('Configure one public hostname without a scheme or path.')
        self.public_host = public_host
        self.store = WorkspaceStore(data, personal=personal, page_transcriber=page_transcriber,
                                    allowed_pdf_digests=allowed_pdf_digests, local_ocr=local_ocr, ai=ai)
        super().__init__(address, WorkspaceHandler)


class WorkspaceHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def do_GET(self):
        if self.server.tailscale and handle_http(self, self.server.store, TAIL_BASE, allowed_hosts={TAIL_HOST}):
            return
        hosts = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        if self.server.public_host:
            hosts.add(self.server.public_host)
        handle_http(self, self.server.store, allowed_hosts=hosts)
    do_POST = do_GET
    do_HEAD = do_GET
    def log_message(self, *args):
        pass
