#!/usr/bin/env python3
"""
Taynalysis upload service. Caddy forwards https://taynalysis.com/api/* here; everything else Caddy serves
straight from the site folder.

  PUT /api/files/<path>   save one site file (needs the upload token)     -> {"ok": true, "path": ..., "bytes": n}
  GET /api/check          confirm an upload token works                    -> {"ok": true}
  GET /api/health         is the service running (no token needed)         -> {"ok": true}

Settings come from environment variables (set in the systemd unit):
  TAYNALYSIS_SITE        site folder Caddy serves                 (default /srv/taynalysis/site)
  TAYNALYSIS_TOKEN_FILE  file holding the upload token             (default /etc/taynalysis/upload-token)
  TAYNALYSIS_PORT        local port Caddy forwards to              (default 8787)
"""
import hmac, json, os, re, sys, tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

SITE = os.path.realpath(os.environ.get('TAYNALYSIS_SITE', '/srv/taynalysis/site'))
TOKEN_FILE = os.environ.get('TAYNALYSIS_TOKEN_FILE', '/etc/taynalysis/upload-token')
PORT = int(os.environ.get('TAYNALYSIS_PORT', '8787'))
MAX_BYTES = 25 * 1024 * 1024
ALLOWED = {'.html', '.json', '.txt', '.css', '.js', '.png', '.jpg', '.jpeg', '.webp', '.svg', '.ico', '.woff2'}
SEGMENT = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$')     # no leading dot, no slashes, no spaces


def load_token():
    with open(TOKEN_FILE, encoding='utf-8') as f:
        tok = f.read().strip()
    if len(tok) < 32:
        sys.exit(f'Upload token in {TOKEN_FILE} is missing or too short.')
    return tok

TOKEN = load_token()


def safe_path(raw):
    """Site-relative path from the URL -> absolute path inside SITE, or None if it isn't allowed."""
    rel = unquote(raw).strip('/')
    parts = rel.split('/')
    if not rel or len(parts) > 6 or not all(SEGMENT.match(p) for p in parts):
        return None
    if os.path.splitext(parts[-1])[1].lower() not in ALLOWED:
        return None
    full = os.path.realpath(os.path.join(SITE, *parts))
    return full if full.startswith(SITE + os.sep) else None


class Handler(BaseHTTPRequestHandler):
    server_version = 'taynalysis-upload/1'

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        got = self.headers.get('Authorization', '')
        return got.startswith('Bearer ') and hmac.compare_digest(got[7:].strip(), TOKEN)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/api/health':
            return self.reply(200, {'ok': True})
        if path == '/api/check':
            return self.reply(200, {'ok': True}) if self.authorized() else self.reply(401, {'ok': False, 'error': 'bad token'})
        self.reply(404, {'ok': False, 'error': 'not found'})

    def do_PUT(self):
        path = urlsplit(self.path).path
        if not path.startswith('/api/files/'):
            return self.reply(404, {'ok': False, 'error': 'not found'})
        if not self.authorized():
            return self.reply(401, {'ok': False, 'error': 'bad token'})
        dest = safe_path(path[len('/api/files/'):])
        if not dest:
            return self.reply(400, {'ok': False, 'error': 'path not allowed'})
        try:
            n = int(self.headers.get('Content-Length', ''))
        except ValueError:
            return self.reply(411, {'ok': False, 'error': 'Content-Length required'})
        if n < 0 or n > MAX_BYTES:
            return self.reply(413, {'ok': False, 'error': f'file over {MAX_BYTES // 1048576} MB'})
        data = self.rfile.read(n)
        if len(data) != n:
            return self.reply(400, {'ok': False, 'error': 'upload cut short'})
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), prefix='.up-')
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
            os.chmod(tmp, 0o644)
            os.replace(tmp, dest)          # readers see the old file or the new one, never half of one
        except Exception:
            if os.path.exists(tmp): os.unlink(tmp)
            raise
        self.reply(200, {'ok': True, 'path': os.path.relpath(dest, SITE), 'bytes': n})

    def log_message(self, fmt, *args):
        sys.stderr.write('%s %s\n' % (self.command, fmt % args))


if __name__ == '__main__':
    os.makedirs(SITE, exist_ok=True)
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    print(f'Taynalysis upload service on 127.0.0.1:{PORT}, saving into {SITE}', flush=True)
    srv.serve_forever()
