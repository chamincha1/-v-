"""Cloud server regression tests without public network or secret accounts."""
from __future__ import annotations
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app_server import AppHandler

class CloudAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),AppHandler)
        cls.thread=Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.url=f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def request(self, path, password=None, post=None):
        headers={}
        if password is not None:
            token=base64.b64encode(f'marketpulse:{password}'.encode()).decode()
            headers['Authorization']='Basic '+token
        if post is not None: headers['Content-Type']='application/json'
        req=Request(self.url+path,data=json.dumps(post).encode() if post is not None else None,headers=headers)
        try:
            with urlopen(req,timeout=5) as r: return r.status, dict(r.headers),r.read()
        except HTTPError as e: return e.code,dict(e.headers),e.read()

    def test_health_even_when_private(self):
        with patch.dict(os.environ,{'MARKET_PULSE_SITE_PASSWORD':'new-private-secret'}):
            code,_,data=self.request('/api/health')
        self.assertEqual(code,200)
        self.assertTrue(json.loads(data)['ok'])

    def test_password_enforced_on_all_ui_and_api(self):
        with patch.dict(os.environ,{'MARKET_PULSE_SITE_PASSWORD':'new-private-secret'}):
            for route in ('/','/src/app.js','/api/catalog','/api/stock?code=005930'):
                with self.subTest(path=route):
                    code,headers,_=self.request(route)
                    self.assertEqual(code,401)
                    self.assertIn('Basic',headers['WWW-Authenticate'])

    def test_correct_password_reveals_unchanged_ui(self):
        with patch.dict(os.environ,{'MARKET_PULSE_SITE_PASSWORD':'new-private-secret'}):
            code,_,data=self.request('/',password='new-private-secret')
        self.assertEqual(code,200)
        self.assertIn(b'MARKET PULSE',data)

    def test_wrong_user_is_rejected(self):
        token=base64.b64encode(b'wrong:new-private-secret').decode()
        req=Request(self.url+'/',headers={'Authorization':'Basic '+token})
        with patch.dict(os.environ,{'MARKET_PULSE_SITE_PASSWORD':'new-private-secret'}):
            with self.assertRaises(HTTPError) as error:urlopen(req,timeout=4)
        self.assertEqual(error.exception.code,401)

    def test_cloud_ai_requires_password(self):
        with patch.dict(os.environ,{'MARKET_PULSE_CLOUD':'1','GROQ_API_KEY':'fake_key'},clear=False):
            with patch.dict(os.environ,{'MARKET_PULSE_SITE_PASSWORD':''}):
                code,_,data=self.request('/api/news/analyze',post={'urls':['https://example.com/1']})
        self.assertEqual(code,503)
        self.assertIn('MARKET_PULSE_SITE_PASSWORD',data.decode())

    def test_no_password_public_ui_unchanged(self):
        with patch.dict(os.environ,{'MARKET_PULSE_SITE_PASSWORD':''}):
            code,_,data=self.request('/')
        self.assertEqual(code,200)
        self.assertIn(b'MARKET PULSE',data)

    def test_private_files_not_exposed_with_password(self):
        with patch.dict(os.environ,{'MARKET_PULSE_SITE_PASSWORD':'new-private-secret'}):
            for route in ('/.env','/app_server.py','/data_cache/ai_usage.json','/render.yaml'):
                with self.subTest(path=route):
                    self.assertEqual(self.request(route,'new-private-secret')[0],404)

class CloudConfigTests(unittest.TestCase):
    def test_original_frontend_assets_preserved(self):
        import zipfile
        orig=Path('/mnt/data/MARKET_PULSE_2.1_자동주가_관심종목복원_전체.zip')
        if not orig.exists():self.skipTest('Prior archive unavailable')
        with zipfile.ZipFile(orig) as z:
            for name in ['index.html','styles.css','src/app.js','src/analytics.js','assets/catalog.json','assets/favicon.svg','assets/logo.png']:
                with self.subTest(name=name):self.assertEqual((ROOT/name).read_bytes(),z.read(name))

    def test_render_deployment_config(self):
        text=(ROOT/'render.yaml').read_text('utf-8')
        self.assertIn('startCommand: python app_server.py',text)
        self.assertIn('healthCheckPath: /api/health',text)
        self.assertIn('MARKET_PULSE_SITE_PASSWORD',text)
        self.assertNotIn('gsk_',text)

    def test_config_cache_dir_can_be_customized(self):
        with tempfile.TemporaryDirectory() as d:
            code='import config; print(config.DATA_CACHE)'
            env={**os.environ,'MARKET_PULSE_DATA_DIR':d}
            p=subprocess.run([sys.executable,'-c',code],cwd=ROOT,env=env,text=True,capture_output=True,timeout=10)
            self.assertEqual(p.returncode,0,p.stderr)
            self.assertEqual(p.stdout.strip(),d)

    def test_cloud_subprocess_binds_port_and_serves_http(self):
        import socket
        with socket.socket() as s:
            s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        env={**os.environ,'PORT':str(port),'MARKET_PULSE_CLOUD':'1','MARKET_PULSE_AUTO_OPEN':'0','MARKET_PULSE_SITE_PASSWORD':''}
        proc=subprocess.Popen([sys.executable,'app_server.py'],cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            for _ in range(50):
                if proc.poll() is not None:self.fail(f'server exited {proc.communicate()}')
                try:
                    with urlopen(f'http://127.0.0.1:{port}/api/health',timeout=1) as r:
                        self.assertEqual(r.status,200)
                        break
                except OSError:time.sleep(.12)
            else:self.fail('Cloud service never became ready')
            with urlopen(f'http://127.0.0.1:{port}/',timeout=3) as r:
                self.assertIn(b'MARKET PULSE',r.read())
        finally:
            proc.terminate()
            try:proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:proc.kill();proc.communicate(timeout=3)

if __name__=='__main__': unittest.main()
