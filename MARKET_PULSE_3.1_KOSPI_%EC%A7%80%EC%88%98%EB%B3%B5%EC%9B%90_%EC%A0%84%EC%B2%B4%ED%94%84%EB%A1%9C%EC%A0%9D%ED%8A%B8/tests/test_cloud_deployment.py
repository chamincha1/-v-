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
            for route in ('/.env','/app_server.py','/data_cache/ai_usage.json','/render.yaml','/Dockerfile','/.dockerignore'):
                with self.subTest(path=route):
                    self.assertEqual(self.request(route,'new-private-secret')[0],404)

class CloudConfigTests(unittest.TestCase):
    def test_unchanged_foundation_hashes(self):
        # Verify the immutable original design, analysis and AI modules.
        # app_server.py and market_api.py intentionally gain only KOSPI endpoints
        # in 3.1; dedicated index tests cover their new behavior instead.
        from hashlib import sha256
        expected = {'index.html': 'd42cac36ab6ca24d94dda966aea7eeb4c28da8dfa68104f4f2da2fc0ed71bf1e', 'assets/catalog.json': '22d2875633f9f59f853276d6ed9161e09efa822c9d7edff4dd2656e858fc79f5', 'assets/favicon.svg': 'deb845142099a104271abb7e9c91d5f61d91a6f8ce74aadfb9677a264ad8afce', 'assets/logo.png': 'ac93938375de2d26fcdc6e7bc7f2408e1fbeb328866dd262400750e9d37f99ca', 'article_link.py': '1f18d2a8b8567a56dc2f83cb9aebd2543299009d4605e68fc311178ab371315e', 'backtesting.py': '0165aef6de6ea3f52b39322152f5377b951634b83fe7b4f178cc78b5ad1fc0e0', 'config.py': '8fc2a6850cd404d6eb0f8ded5eacf906aeda52f7b8827722562933a797061d5f', 'feature_engineering.py': 'c783dac35856ca21f083837146bff07d2129d574ea8a8aabd2457f0c0fe03371', 'groq_client.py': '21020a76e3545f2ba7c9347024521ba9af9479f41c272c59cee22ac30f31721e', 'market_data.py': '4fc8dd30a2ad1d2c4fac2e9b0d4b43324b1b9dd1a57b8107bd79efcf4dd6398f', 'news_analyzer.py': '54413def8dd868d1ba98cc3f4a1881f267d027af372a9b7fd75ffb7f6e8da826', 'news_collector.py': '4575a08b9456e11b6c24476cf067e7ca8f1e0efda70499054a5f3a6c5a75170f', 'prediction_model.py': 'a768ab2b5cb977b4d3bcb28b9e5c2e0744e65dbb50cd3a83b786440924dd00e1', 'stock_catalog.py': 'f37051d2367ed9cc234f16cb2ef1b07df4f173f07510868e848fbc808300d32c', 'stock_core.py': '64a5fc4b6b9d96129d8d113b3541b6ef2a146e663367df422cde9f8ea8fd57b9', 'technical_indicators.py': 'cab2b4c801967a64660127bd0122fcbf18a60a0b03d84aa64a084c99216cdf76'}
        for name,digest in expected.items():
            with self.subTest(name=name):
                self.assertEqual(sha256((ROOT/name).read_bytes()).hexdigest(),digest)

    def test_nocard_container_deployment_config(self):
        dockerfile=(ROOT/'Dockerfile').read_text('utf-8')
        self.assertIn('CMD ["python", "app_server.py"]', dockerfile)
        self.assertIn('MARKET_PULSE_CLOUD=1', dockerfile)
        self.assertIn('EXPOSE 8765', dockerfile)
        self.assertNotIn('gsk_', dockerfile)
        self.assertFalse((ROOT/'render.yaml').exists())

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
