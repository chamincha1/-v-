"""KOSPI index regression tests: offline / injected real-shaped vendor candles."""
from __future__ import annotations
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.request import urlopen
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import market_api
from market_data import MarketResult
from app_server import AppHandler
from http.server import ThreadingHTTPServer

KST = timezone(timedelta(hours=9))

def fake_index(days_back=0, label='FinanceDataReader KS11'):
    now = datetime.now(KST).date() - timedelta(days=days_back)
    frame = pd.DataFrame({'Date': pd.to_datetime([now - timedelta(days=3), now - timedelta(days=2), now]),
                          'Close': [2900.25, 2910.50, 2930.75], 'Volume': [0, 0, 0],
                          'Open': [2890, 2900, 2915], 'High': [2910, 2930, 2940], 'Low': [2875, 2885, 2900]})
    return MarketResult(frame, {'symbol': 'KS11', 'source': label, 'status': 'fresh',
                                'last_trade_date': now.isoformat(), 'realtime': False})

class IndexTests(unittest.TestCase):
    def setUp(self):
        market_api._kospi_cache.clear()

    def test_fdr_index_without_yahoo_for_fresh_data(self):
        with patch('market_data.fetch_historical', return_value=fake_index()) as primary, \
             patch('market_api._yahoo_kospi') as secondary:
            result = market_api.kospi_index()
        self.assertEqual(primary.call_args.kwargs['index'], True)
        secondary.assert_not_called()
        self.assertEqual(result['name'], 'KOSPI 종합주가지수')
        self.assertEqual(result['code'], 'KS11')
        self.assertEqual(result['unit'], 'pt')
        self.assertAlmostEqual(result['value'], 2930.75)
        self.assertAlmostEqual(result['change_pct'], (2930.75/2910.50 - 1)*100)
        self.assertFalse(result['meta']['realtime'])
        self.assertEqual(len(result['prices']), 3)

    def test_memory_cache_skips_second_vendor_request(self):
        with patch('market_data.fetch_historical', return_value=fake_index()) as primary:
            market_api.kospi_index()
            market_api.kospi_index()
        self.assertEqual(primary.call_count, 1)

    def test_stale_primary_prefers_newer_yahoo_and_saves_actual_data(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(market_api, 'DATA_CACHE', Path(td)), \
             patch('market_data.fetch_historical', return_value=fake_index(days_back=30)), \
             patch('market_api._yahoo_kospi', return_value=fake_index(days_back=1, label='Yahoo Finance')):
            result = market_api.kospi_index()
            self.assertEqual(result['meta']['source'], 'Yahoo Finance')
            self.assertTrue((Path(td)/'KS11.csv').is_file())
            cached = market_api.cached_kospi()
            self.assertEqual(cached['meta']['status'], 'cached')
            self.assertAlmostEqual(cached['value'], 2930.75)

    def test_stale_primary_marked_when_fallback_fails(self):
        with patch('market_data.fetch_historical', return_value=fake_index(days_back=31)), \
             patch('market_api._yahoo_kospi', side_effect=TimeoutError('timeout')):
            result = market_api.kospi_index()
        self.assertTrue(result['meta']['stale'])
        self.assertIn('최신 지수가 아닐', result['meta']['warning'])

    def test_both_providers_fail_without_cache(self):
        with patch('market_data.fetch_historical', side_effect=ValueError('down')), \
             patch('market_api._yahoo_kospi', side_effect=ValueError('down')):
            with self.assertRaises(market_api.MarketAPIError):
                market_api.kospi_index()

    def test_index_period_restricted(self):
        for invalid in (-1,0,119,1301):
            with self.assertRaises(market_api.MarketAPIError):
                market_api.kospi_index(days=invalid)

    def test_yahoo_secondary_daily_chart_parsing_with_missing_volume(self):
        from unittest.mock import MagicMock
        from datetime import time as clock_time
        today = datetime.now(KST).date()
        observed = [today - timedelta(days=4), today - timedelta(days=3), today - timedelta(days=2)]
        timestamps = [int(datetime.combine(d, clock_time(5,0),tzinfo=timezone.utc).timestamp()) for d in observed]
        response = MagicMock()
        response.json.return_value = {'chart': {'result': [{
            'timestamp': timestamps,
            'indicators': {'quote': [{'close': [3001.1, 3009.2, 3012.3],
                                       'open': [3000.0, 3005.0, 3011.0]}]}
        }]}}
        with patch('requests.get', return_value=response) as get:
            result = market_api._yahoo_kospi(today-timedelta(days=20), today)
        self.assertEqual(result.meta['symbol'], 'KS11')
        self.assertEqual(result.frame['Close'].tolist(), [3001.1, 3009.2, 3012.3])
        self.assertEqual(result.frame['Volume'].tolist(), [0, 0, 0])
        self.assertFalse(result.meta['realtime'])
        get.assert_called_once()

    def test_http_kospi_route_and_cache(self):
        server = ThreadingHTTPServer(('127.0.0.1',0),AppHandler)
        thread = Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            base=f'http://127.0.0.1:{server.server_port}'
            expected=market_api._kospi_payload(fake_index().frame, fake_index().meta)
            with patch('app_server.kospi_index',return_value=expected) as live, \
                 patch('app_server.cached_kospi',return_value=expected) as cached:
                with urlopen(base+'/api/index/kospi?days=460', timeout=3) as res:
                    body=json.loads(res.read());self.assertEqual(res.status,200)
                    self.assertEqual(body['code'],'KS11')
                with urlopen(base+'/api/index/kospi/cached?days=460', timeout=3) as res:
                    self.assertEqual(json.loads(res.read())['unit'],'pt')
                live.assert_called_once_with(460,force=False)
                cached.assert_called_once_with(460)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=3)

if __name__=='__main__':
    unittest.main()
