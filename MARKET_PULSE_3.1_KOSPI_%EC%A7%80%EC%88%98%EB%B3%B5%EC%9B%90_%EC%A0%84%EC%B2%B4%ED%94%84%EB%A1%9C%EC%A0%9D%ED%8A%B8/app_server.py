"""MARKET PULSE: same UI and features on local Windows or a hosted web service.

No Node/npm/AI Studio. Environment controls host, port and optional site password.
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import os
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
from threading import BoundedSemaphore
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import time
import webbrowser
from threading import Timer

from groq_client import configured, analyze, GroqError, remaining_today
from article_link import fetch_public_article, ArticleFetchError
from market_api import recent_prices, cached_prices, predictor, search_stocks, cached_kospi, kospi_index, MarketAPIError
from stock_catalog import CATALOG

ROOT = Path(__file__).resolve().parent
LOCAL_HOST = '127.0.0.1'
DEFAULT_PORT = 8765
MAX_POST_BYTES = 12_000
MAX_API_CONCURRENCY = 3
fetch_slots = BoundedSemaphore(MAX_API_CONCURRENCY)
index_slots = BoundedSemaphore(1)
AI_COOLDOWN_SEC = 3
last_ai_call = 0.0

ALLOWED = {
    '/': 'index.html', '/index.html': 'index.html', '/styles.css': 'styles.css',
    '/src/app.js': 'src/app.js', '/src/analytics.js': 'src/analytics.js',
    '/assets/catalog.json': 'assets/catalog.json', '/assets/favicon.svg': 'assets/favicon.svg',
    '/assets/logo.png': 'assets/logo.png',
}


class UserInputError(ValueError):
    pass


class AppHandler(SimpleHTTPRequestHandler):
    server_version = 'MarketPulse/2.2'
    sys_version = ''
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        # Avoid logging full URL query values and submitted article/AI prompts.
        logging.info('%s %s', self.client_address[0], (fmt % args).split('?')[0][:250])

    def _authorized(self) -> bool:
        """Optional browser-native Basic authentication; never persist passwords in JS.

        Render health checks must remain accessible even if the whole UI is protected.
        The optional password is configured ONLY in the hosting environment.
        """
        if urlsplit(self.path).path == '/api/health':
            return True
        password = os.environ.get('MARKET_PULSE_SITE_PASSWORD', '')
        if not password:
            return True
        supplied = self.headers.get('Authorization', '')
        if not supplied.startswith('Basic '):
            return False
        try:
            raw = base64.b64decode(supplied[6:], validate=True).decode('utf-8')
            username, candidate = raw.split(':', 1)
        except (ValueError, UnicodeDecodeError):
            return False
        return (hmac.compare_digest(username, 'marketpulse')
                and hmac.compare_digest(candidate, password))

    def _need_auth(self) -> bool:
        if self._authorized():
            return False
        body = '로그인이 필요합니다.'.encode('utf-8')
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="MARKET PULSE", charset="UTF-8"')
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)
        return True

    def _headers(self, code: int, kind: str, length: int) -> None:
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(length))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cache-Control', 'no-store' if kind.startswith('application/json') else 'public, max-age=120')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()

    def _json(self, data: object, code: int = 200) -> None:
        blob = json.dumps(data, ensure_ascii=False, allow_nan=False, default=str).encode('utf-8')
        self._headers(code, 'application/json; charset=utf-8', len(blob))
        if self.command != 'HEAD': self.wfile.write(blob)

    def _error(self, exc: Exception, code: int = 400) -> None:
        # Only send sanitized messages; never expose API credentials or tracebacks.
        self._json({'ok': False, 'error': str(exc)[:320]}, code=code)

    def _static(self, pathname: str) -> None:
        if pathname not in ALLOWED:
            return self._error(UserInputError('요청한 파일이 없습니다.'), 404)
        filepath = (ROOT / ALLOWED[pathname]).resolve()
        if not filepath.is_file() or ROOT not in filepath.parents or filepath.stat().st_size > 5_000_000:
            return self._error(UserInputError('파일을 찾을 수 없습니다.'), 404)
        kind = {
            '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
            '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml',
            '.json': 'application/json; charset=utf-8', '.png': 'image/png',
        }.get(filepath.suffix, 'application/octet-stream')
        content = filepath.read_bytes()
        self._headers(200, kind, len(content))
        if self.command != 'HEAD': self.wfile.write(content)

    def do_HEAD(self):
        return self.do_GET()

    def do_GET(self):
        if self._need_auth():
            return
        try:
            uri = urlsplit(self.path)
            if uri.path in ('/api/health', '/api/catalog', '/api/stock', '/api/stock/cached', '/api/search', '/api/index/kospi', '/api/index/kospi/cached'):
                if uri.path == '/api/health':
                    return self._json({'ok': True, 'app': 'MARKET PULSE', 'engine': 'python-local',
                                       'market': 'historical_daily', 'realtime': False,
                                       'ai_provider': 'GroqCloud', 'ai_configured': configured(),
                                       'quality_modes': ['standard', 'deep'], 'ai_calls_remaining_local': remaining_today()})
                if uri.path == '/api/catalog':
                    return self._json([{'name': n, 'code': c, 'sector': s} for n, c, s in CATALOG])
                params = parse_qs(uri.query)
                if uri.path == '/api/search':
                    return self._json({'ok': True, 'results': search_stocks(params.get('q', [''])[0])})
                if uri.path in ('/api/index/kospi', '/api/index/kospi/cached'):
                    days = int(params.get('days', ['460'])[0])
                    if uri.path.endswith('/cached'):
                        try: return self._json({'ok': True, **cached_kospi(days)})
                        except MarketAPIError as exc: return self._error(exc, 404)
                    if not index_slots.acquire(blocking=False):
                        return self._error(UserInputError('KOSPI 지수 조회가 이미 진행 중입니다.'), 503)
                    try: return self._json({'ok': True, **kospi_index(days, force=params.get('refresh',['0'])[0]=='1')})
                    finally: index_slots.release()
                code = params.get('code', [''])[0]
                days = int(params.get('days', ['450'])[0])
                if uri.path == '/api/stock/cached':
                    try: return self._json({'ok': True, **cached_prices(code, days=days)})
                    except MarketAPIError as exc: return self._error(exc, 404)
                force = params.get('refresh', ['0'])[0] == '1'
                if not fetch_slots.acquire(blocking=False):
                    return self._error(UserInputError('다른 자료 조회가 진행 중입니다. 잠시 후 다시 시도하세요.'), 503)
                try: return self._json({'ok': True, **recent_prices(code, days=days, force=force)})
                finally: fetch_slots.release()
            if uri.path.startswith('/api/'):
                return self._error(UserInputError('존재하지 않는 API입니다.'), 404)
            return self._static(uri.path)
        except (UserInputError, MarketAPIError, ValueError, ArticleFetchError, GroqError) as exc:
            return self._error(exc)
        except (ConnectionError, TimeoutError) as exc:
            return self._error(UserInputError('외부 서버 응답이 지연되었습니다.'), 502)
        except Exception:
            logging.exception('GET failed')
            return self._error(UserInputError('서버 처리 중 오류가 발생했습니다.'), 500)

    def do_POST(self):
        if self._need_auth():
            return
        try:
            uri = urlsplit(self.path)
            if uri.path not in ('/api/news/analyze', '/api/predict'):
                return self._error(UserInputError('지원하지 않는 API입니다.'), 404)
            try: length = int(self.headers.get('Content-Length', '0'))
            except ValueError: length = -1
            if length <= 0 or length > MAX_POST_BYTES:
                return self._error(UserInputError('요청 데이터 크기가 잘못되었습니다.'), 413)
            if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                return self._error(UserInputError('JSON 요청만 지원합니다.'), 415)
            try: data = json.loads(self.rfile.read(length))
            except ValueError:
                return self._error(UserInputError('JSON 형식이 올바르지 않습니다.'))
            if not isinstance(data, dict):
                return self._error(UserInputError('요청 형식이 올바르지 않습니다.'))
            if not fetch_slots.acquire(blocking=False):
                return self._error(UserInputError('분석 요청이 실행 중입니다. 중복 호출을 피하기 위해 잠시 제한합니다.'), 503)
            try:
                if uri.path == '/api/predict':
                    code = str(data.get('code', ''))
                    return self._json({'ok': True, **predictor(code)})
                # The public internet must never be allowed to consume an owner's AI key freely.
                if os.environ.get('MARKET_PULSE_CLOUD') == '1' and not os.environ.get('MARKET_PULSE_SITE_PASSWORD', ''):
                    return self._error(GroqError('클라우드 AI 분석을 사용하려면 MARKET_PULSE_SITE_PASSWORD를 서버 환경변수에 설정하세요.'), 503)
                if not configured():
                    return self._error(GroqError('GROQ_API_KEY가 등록되지 않았습니다. 서버 환경변수에 추가하세요.'), 503)
                urls = data.get('urls', [])
                if not isinstance(urls, list) or not 1 <= len(urls) <= 4:
                    return self._error(UserInputError('공개 기사 URL을 1~4개 입력하세요.'))
                if any(not isinstance(url, str) or len(url) > 2048 for url in urls):
                    return self._error(UserInputError('URL 형식 또는 길이가 올바르지 않습니다.'))
                if len(set(urls)) != len(urls):
                    return self._error(UserInputError('중복된 기사 URL은 제외해 주세요.'))
                quality = str(data.get('quality', 'deep'))
                if quality not in ('standard', 'deep'):
                    return self._error(UserInputError('분석 모드가 올바르지 않습니다.'))
                articles = [fetch_public_article(url) for url in urls]
                # Content is fetched only when a human presses the analyze button.
                result = analyze(articles, quality=quality)
                return self._json({'ok': True, 'analysis': result})
            finally:
                fetch_slots.release()
        except (UserInputError, MarketAPIError, ValueError, ArticleFetchError, GroqError) as exc:
            return self._error(exc)
        except Exception:
            logging.exception('POST failed')
            return self._error(UserInputError('서버 처리 중 오류가 발생했습니다.'), 500)


def main():
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(message)s')
    raw = os.environ.get('PORT') or os.environ.get('MARKET_PULSE_PORT', str(DEFAULT_PORT))
    try:
        port = int(raw)
        if not 1 <= port <= 65535: raise ValueError()
    except ValueError:
        raise SystemExit('PORT 또는 MARKET_PULSE_PORT는 1~65535 사이 숫자여야 합니다.')
    cloud = os.environ.get('MARKET_PULSE_CLOUD') == '1' or bool(os.environ.get('PORT'))
    host = '0.0.0.0' if cloud else os.environ.get('MARKET_PULSE_HOST', LOCAL_HOST)
    httpd = ThreadingHTTPServer((host, port), AppHandler)
    httpd.daemon_threads = True
    print(f'MARKET PULSE started: http://{host}:{port}', flush=True)
    if cloud:
        print('클라우드 배포 모드: 외부 접속 가능 · HTTPS는 호스팅 제공자가 처리', flush=True)
        if not os.environ.get('MARKET_PULSE_SITE_PASSWORD'):
            print('주의: 비밀번호 미설정으로 공개 페이지입니다. GroqCloud API는 사용이 제한됩니다.', flush=True)
    else:
        print('로컬 실행: 종료하려면 Ctrl+C를 누르세요.', flush=True)
        if os.environ.get('MARKET_PULSE_AUTO_OPEN', '1') != '0':
            Timer(1.0, lambda: webbrowser.open(f'http://{LOCAL_HOST}:{port}')).start()
    try: httpd.serve_forever(poll_interval=.25)
    except KeyboardInterrupt: pass
    finally: httpd.server_close()


if __name__ == '__main__':
    main()
