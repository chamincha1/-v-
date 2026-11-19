"""On-demand, cache-aware historical daily prices; never label daily data realtime."""
from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import json
from threading import Lock
import re
import time
from stock_catalog import CATALOG
from config import DATA_CACHE

CODES = {code for _, code, _ in CATALOG}
CATALOG_BY_CODE = {code: (name, sector) for name, code, sector in CATALOG}
_stock_listing_cache: tuple[float, list[dict]] | None = None
KST = timezone(timedelta(hours=9))
_TTL = 15 * 60
_price_cache: dict[tuple, tuple[float, dict]] = {}
_prediction_cache: dict[tuple, tuple[float, dict]] = {}
_kospi_cache: dict[tuple, tuple[float, dict]] = {}
_lock = Lock()


class MarketAPIError(ValueError):
    pass


def _valid_code(code: str) -> None:
    if not isinstance(code, str) or not re.fullmatch(r'\d{6}', code) or code == '000000':
        raise MarketAPIError('종목코드는 정확한 6자리 국내 주식 코드여야 합니다.')


def _price_payload(code: str, df, meta: dict) -> dict:
    """Serialize actually observed OHLCV. Never synthesize prices."""
    import math
    def optional_number(value):
        try:
            n = float(value)
            return n if math.isfinite(n) else None
        except (TypeError, ValueError, OverflowError):
            return None

    prices = []
    for row in df.itertuples(index=False):
        day = row.Date.date().isoformat() if hasattr(row.Date, 'date') else str(row.Date)[:10]
        close = float(row.Close)
        if not math.isfinite(close) or close <= 0:
            continue
        volume = optional_number(row.Volume)
        if volume is None or volume < 0:
            continue
        prices.append({'date': day, 'close': close, 'volume': int(volume),
                       'open': optional_number(getattr(row, 'Open', None)),
                       'high': optional_number(getattr(row, 'High', None)),
                       'low': optional_number(getattr(row, 'Low', None))})
    if len(prices) < 2:
        raise MarketAPIError('거래일 데이터가 부족합니다.')
    return {'code': code, 'name': CATALOG_BY_CODE.get(code, (f'종목 {code}', '직접 추가'))[0],
            'prices': prices, 'meta': meta}


def cached_prices(code: str, *, days: int = 460) -> dict:
    """Serve previously downloaded real daily prices instantly; never trigger network."""
    _valid_code(code)
    if not isinstance(days, int) or not 120 <= days <= 1300:
        raise MarketAPIError('조회 기간은 120~1300일로 제한됩니다.')
    path = Path(DATA_CACHE) / f'{code}.csv'
    meta_path = Path(DATA_CACHE) / f'{code}.meta.json'
    if not path.is_file() or path.stat().st_size > 8_000_000:
        raise MarketAPIError('저장된 실제 종가 데이터가 없습니다. 온라인으로 자동 조회 중입니다.')
    try:
        from stock_core import read_csv_bytes
        from pandas import to_datetime
        frame = read_csv_bytes(path.read_bytes())
        today = datetime.now(KST).date()
        frame = frame[to_datetime(frame['Date']).dt.date >= today - timedelta(days=days)]
        meta = json.loads(meta_path.read_text('utf-8')) if meta_path.is_file() else {}
        meta = dict(meta, status='cached', data_kind='daily_historical_ohlcv', realtime=False,
                    last_trade_date=frame.iloc[-1]['Date'].date().isoformat(),
                    warning='저장된 실제 과거 일봉입니다. 온라인 최신 조회를 진행 중입니다.')
        return _price_payload(code, frame.tail(850), meta)
    except (ValueError, OSError, KeyError, IndexError, TypeError) as exc:
        raise MarketAPIError('저장된 가격 파일을 읽을 수 없습니다. 온라인으로 다시 조회합니다.') from exc


def recent_prices(code: str, *, days: int = 450, force: bool = False) -> dict:
    _valid_code(code)
    if not isinstance(days, int) or days < 120 or days > 1300:
        raise MarketAPIError('조회 기간은 120~1300일로 제한됩니다.')
    today = datetime.now(KST).date()
    key = (code, days, today.isoformat())
    with _lock:
        cached = _price_cache.get(key)
        if cached and not force and cached[0] > time.monotonic():
            return cached[1]
    try:
        from market_data import fetch_historical
        result = fetch_historical(code, today - timedelta(days=days), today)
        payload = _price_payload(code, result.frame.tail(850), result.meta)
        with _lock:
            _price_cache[key] = (time.monotonic() + _TTL, payload)
        return payload
    except ImportError as exc:
        raise MarketAPIError('실제 주가 조회용 FinanceDataReader가 없습니다. requirements.txt를 설치하세요.') from exc
    except Exception as exc:
        with _lock:
            stale = _price_cache.get(key)
            if stale:
                fallback = dict(stale[1]); fallback['meta'] = dict(fallback.get('meta') or {}, status='cached', warning='온라인 조회 실패. 이전 수집 실제 일봉 사용')
                return fallback
        raise MarketAPIError(f'주가 조회 실패: {str(exc)[:220]}') from exc


def search_stocks(query: str, *, limit: int = 15) -> list[dict]:
    """User-triggered full KRX listing lookup. No network request at app startup."""
    global _stock_listing_cache
    if not isinstance(query, str) or len(query.strip()) < 2 or len(query) > 40:
        raise MarketAPIError('검색어를 2~40자로 입력하세요.')
    q = query.casefold().strip()
    with _lock:
        cache = _stock_listing_cache
    if cache is None or cache[0] < time.monotonic():
        from market_data import list_domestic_stocks
        try:
            listing = list_domestic_stocks()
            records = [{'code': str(row.Code), 'name': str(row.Name), 'sector': 'KRX 검색'}
                       for row in listing.itertuples(index=False)]
        except Exception as exc:
            raise MarketAPIError('KRX 전체 종목 검색에 실패했습니다. 6자리 코드를 직접 입력할 수 있습니다. ' + str(exc)[:110]) from exc
        with _lock:
            _stock_listing_cache = (time.monotonic() + 12 * 3600, records)
    else:
        records = cache[1]
    return [r for r in records if q in r['code'] or q in r['name'].casefold()][:limit]


def predictor(code: str) -> dict:
    if code not in CODES:
        raise MarketAPIError('등록된 30개 종목 중에서 예측 대상을 선택하세요.')
    data = recent_prices(code, days=1180)
    cache_key = (code, data['meta'].get('last_trade_date'), data['prices'][-1]['close'], len(data['prices']))
    with _lock:
        cached = _prediction_cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
    from prediction_model import run_experiments
    from pandas import DataFrame, to_datetime
    frame = DataFrame(data['prices'])
    frame['Date'] = to_datetime(frame.pop('date'))
    frame = frame.rename(columns={'close':'Close','volume':'Volume','open':'Open','high':'High','low':'Low'})
    result = run_experiments(frame)
    def clean(value):
        if isinstance(value, (int, str, bool)) or value is None:
            return value
        if hasattr(value, 'item'):
            try: return clean(value.item())
            except (TypeError, ValueError): pass
        if isinstance(value, float):
            return round(value, 8) if value == value else None
        return str(value)
    payload = {'code': code, 'name': data['name'], 'last_trade_date': data['meta'].get('last_trade_date'),
            'forecasts': [{k:clean(v) for k,v in row.items()} for row in result.forecasts.to_dict(orient='records')],
            'summary': [{k:clean(v) for k,v in row.items()} for row in result.summary.to_dict(orient='records')],
            'note': result.news_note,
            'method': '과거 가격·거래량 기반 시간순 학습/검증·시험 분리; 탐색적·미보정 확률 추정',
            'realtime': False}
    with _lock:
        _prediction_cache[cache_key] = (time.monotonic() + 30 * 60, payload)
    return payload


# MARKET PULSE 3.1 - independent KOSPI market index (KS11), never a six-digit equity.
# Only actual observed close values are serialized; this is not an intraday quote.
def _kospi_payload(frame, meta: dict) -> dict:
    payload = _price_payload('KS11', frame, meta)
    prices = payload['prices']
    last, prev = prices[-1], prices[-2]
    payload['name'] = 'KOSPI 종합주가지수'
    payload['value'] = last['close']
    payload['change'] = last['close'] - prev['close']
    payload['change_pct'] = (last['close'] / prev['close'] - 1) * 100
    payload['last_trade_date'] = last['date']
    payload['unit'] = 'pt'
    return payload


def cached_kospi(days: int = 460) -> dict:
    """Show saved real index points quickly; no network on this endpoint."""
    if not isinstance(days, int) or not 120 <= days <= 1300:
        raise MarketAPIError('지수 조회 기간은 120~1300일로 제한됩니다.')
    try:
        from stock_core import read_csv_bytes
        from pandas import to_datetime
        path = Path(DATA_CACHE) / 'KS11.csv'
        meta_path = Path(DATA_CACHE) / 'KS11.meta.json'
        if not path.is_file() or path.stat().st_size > 8_000_000:
            raise MarketAPIError('저장된 KOSPI 실데이터가 없습니다.')
        frame = read_csv_bytes(path.read_bytes())
        today = datetime.now(KST).date()
        frame = frame[to_datetime(frame['Date']).dt.date >= today - timedelta(days=days)]
        meta = json.loads(meta_path.read_text('utf-8')) if meta_path.is_file() else {}
        if not isinstance(meta, dict):
            meta = {}
        meta.update(status='cached', data_kind='daily_index', realtime=False,
                    last_trade_date=frame.iloc[-1]['Date'].date().isoformat(),
                    warning='기존 실제 KOSPI 지수 기록입니다. 최신 지수 데이터를 확인 중입니다.')
        return _kospi_payload(frame.tail(850), meta)
    except MarketAPIError:
        raise
    except (OSError, ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise MarketAPIError('저장된 KOSPI 지수 데이터를 읽을 수 없습니다.') from exc


def _yahoo_kospi(start: date, end: date):
    """Secondary public daily-chart source used when primary KS11 data stops updating.

    The source may rate-limit or stop serving; failures remain visible rather than
    returning synthetic points. Never include keys or credentials in this request.
    """
    import pandas as pd
    import requests
    from market_data import MarketResult
    from stock_core import clean_data
    endpoint = 'https://query1.finance.yahoo.com/v8/finance/chart/%5EKS11'
    response = requests.get(endpoint, params={
        'period1': int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp()),
        'period2': int(datetime((end + timedelta(days=1)).year, (end + timedelta(days=1)).month,
                                (end + timedelta(days=1)).day, tzinfo=timezone.utc).timestamp()),
        'interval': '1d'}, headers={'User-Agent': 'Mozilla/5.0 (compatible; MarketPulse/3.1)'}, timeout=12)
    response.raise_for_status()
    data = response.json()
    result = ((data.get('chart') or {}).get('result') or [None])[0]
    if not isinstance(result, dict):
        raise MarketAPIError('보조 KOSPI 지수 제공처의 응답에 데이터가 없습니다.')
    timestamps = result.get('timestamp') or []
    quote = (((result.get('indicators') or {}).get('quote') or [None])[0])
    if not isinstance(quote, dict):
        raise MarketAPIError('보조 KOSPI 지수의 가격 열이 없습니다.')
    closes = quote.get('close') or []
    volumes = quote.get('volume') or []
    rows = []
    def quote_value(key, index):
        values = quote.get(key) or []
        return values[index] if index < len(values) else None
    for i, ts in enumerate(timestamps):
        if i >= len(closes) or closes[i] is None:
            continue
        local_day = datetime.fromtimestamp(int(ts), timezone.utc).astimezone(KST).date()
        if local_day < start or local_day > end:
            continue
        rows.append({'Date': local_day.isoformat(), 'Close': closes[i],
                     'Volume': volumes[i] if i < len(volumes) and volumes[i] is not None else 0,
                     **{key: quote_value(key, i) for key in ('open', 'high', 'low')}})
    frame = pd.DataFrame(rows).rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low'})
    frame = clean_data(frame)
    retrieved = datetime.now(KST).isoformat(timespec='seconds')
    meta = {'symbol': 'KS11', 'source': 'Yahoo Finance (^KS11), 일봉 차트',
            'status': 'fresh', 'retrieved_at': retrieved,
            'last_trade_date': frame.iloc[-1]['Date'].date().isoformat(),
            'data_kind': 'daily_index', 'realtime': False,
            'adjusted_price': 'unknown'}
    return MarketResult(frame=frame, meta=meta)


def kospi_index(days: int = 460, *, force: bool = False) -> dict:
    """KOSPI daily index: primary FDR KS11, optional fresher Yahoo daily fallback.

    Vendor KS11 may have stalled; prefer a demonstrably newer observation, never
    silently identify a weeks-old close as today's market level.
    """
    if not isinstance(days, int) or not 120 <= days <= 1300:
        raise MarketAPIError('지수 조회 기간은 120~1300일로 제한됩니다.')
    today = datetime.now(KST).date()
    key = ('KS11', days, today.isoformat())
    with _lock:
        value = _kospi_cache.get(key)
        if value and not force and value[0] > time.monotonic():
            return value[1]
    from market_data import fetch_historical
    primary = None
    errors = []
    start = today - timedelta(days=days)
    try:
        primary = fetch_historical('KS11', start, today, index=True)
    except Exception as exc:
        errors.append(f'FinanceDataReader: {type(exc).__name__}')
    selected = primary
    primary_date = (primary.frame.iloc[-1]['Date'].date() if primary is not None else None)
    if primary_date is None or (today - primary_date).days >= 7:
        try:
            secondary = _yahoo_kospi(start, today)
            if primary_date is None or secondary.frame.iloc[-1]['Date'].date() > primary_date:
                selected = secondary
                # Only save if the newer vendor returned real, validated values.
                cache_dir = Path(DATA_CACHE)
                cache_dir.mkdir(parents=True, exist_ok=True)
                secondary.frame.to_csv(cache_dir / 'KS11.csv', index=False, encoding='utf-8-sig')
                (cache_dir / 'KS11.meta.json').write_text(json.dumps(secondary.meta, ensure_ascii=False), encoding='utf-8')
        except Exception as exc:
            errors.append(f'보조 지수 제공처: {type(exc).__name__}')
    if selected is None:
        with _lock:
            old = _kospi_cache.get(key)
        if old:
            response = dict(old[1]); response['meta'] = dict(response['meta'], status='cached',
                warning='온라인 조회 실패로 이전에 수집한 실제 KOSPI 지수 사용')
            return response
        raise MarketAPIError('KOSPI 지수 온라인 조회에 실패했습니다. 데이터 제공처 접속 또는 네트워크 상태를 확인하세요. ' + ', '.join(errors))
    meta = dict(selected.meta, data_kind='daily_index', realtime=False)
    age = (today - selected.frame.iloc[-1]['Date'].date()).days
    if age >= 7:
        meta['warning'] = f'제공처의 마지막 거래일이 {age}일 전입니다. 최신 지수가 아닐 수 있습니다.'
        meta['stale'] = True
    elif selected.is_cached:
        meta['warning'] = '온라인 갱신에 실패해 저장된 실제 지수 기록을 사용합니다.'
    payload = _kospi_payload(selected.frame.tail(850), meta)
    with _lock:
        _kospi_cache[key] = (time.monotonic() + _TTL, payload)
    return payload
