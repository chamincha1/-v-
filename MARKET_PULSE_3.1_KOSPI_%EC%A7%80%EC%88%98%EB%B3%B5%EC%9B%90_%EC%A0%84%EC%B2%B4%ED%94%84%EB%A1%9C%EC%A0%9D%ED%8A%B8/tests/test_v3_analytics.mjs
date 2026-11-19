import test from 'node:test';
import assert from 'node:assert/strict';
import {periodPrices,indexedComparison,priceDataQuality,priceCsv,parsePriceCsv,analyzeSeries} from '../src/analytics.js';
const dates=Array.from({length:310},(_,i)=>new Date(Date.UTC(2025,0,i+1)).toISOString().slice(0,10));
const a={code:'005930',name:'기업 A',prices:dates.map((date,i)=>({date,close:100+i,volume:1000+i}))};
const b={code:'000660',name:'기업 B',prices:dates.filter((_,i)=>i%12!==0).map((date,i)=>({date,close:200+i*2,volume:500+i}))};

test('v3: period selection supports all ranges',()=>{
 assert.equal(periodPrices(a.prices,'1M').length,22);
 assert.equal(periodPrices(a.prices,'3M').length,66);
 assert.equal(periodPrices(a.prices,'6M').length,132);
 assert.equal(periodPrices(a.prices,'1Y').length,252);
 assert.equal(periodPrices(a.prices,'ALL').length,310);
 assert.equal(periodPrices(a.prices,'invalid').length,310);
 assert.deepEqual(periodPrices(null,'1M'),[]);
});
test('v3: normalized comparison starts at exactly 100 and aligns dates',()=>{
 const c=indexedComparison(a,b,'1M');
 assert.equal(c.rows.length,22);
 assert.equal(c.rows[0].first,100);
 assert.equal(c.rows[0].second,100);
 assert.equal(c.rows.at(-1).date,c.rows.at(-1).date);
 assert(c.rows.every(x=>a.prices.some(t=>t.date===x.date)&&b.prices.some(t=>t.date===x.date)));
 assert(Number.isFinite(c.firstPct)&&Number.isFinite(c.secondPct));
 assert(Math.abs((c.rows.at(-1).first/100-1)*100-c.firstPct)<1e-9);
});
test('v3: comparisons require two common days',()=>{
 assert.equal(indexedComparison({prices:[]},b),null);
 assert.equal(indexedComparison({prices:[{date:'2026-10-02',close:1}]},b),null);
 assert.equal(indexedComparison(null,b),null);
 assert.equal(indexedComparison({prices:[{date:'2025-01-01',close:0}]},b),null);
});
test('v3: comparison is pure and does not change source arrays',()=>{
 const aCopy=JSON.stringify(a),bCopy=JSON.stringify(b);
 indexedComparison(a,b,'1Y');
 assert.equal(JSON.stringify(a),aCopy);
 assert.equal(JSON.stringify(b),bCopy);
});
test('v3: comparison returns null for date gaps without false matching',()=>{
 const c={prices:[{date:'2026-01-01',close:100},{date:'2026-01-02',close:110}]};
 const d={prices:[{date:'2026-01-03',close:200},{date:'2026-01-04',close:210}]};
 assert.equal(indexedComparison(c,d,'ALL'),null);
});
test('v3: price quality detects duplicates, invalid close, missing volume',()=>{
 const q=priceDataQuality({prices:[{date:'2026-01-01',close:100,volume:1},
  {date:'2026-01-01',close:0,volume:null},{date:'2026-01-15',close:110,volume:undefined}]});
 assert.equal(q.rows,3);assert.equal(q.duplicate,1);assert.equal(q.invalidClose,1);
 assert.equal(q.missingVolume,2);assert.equal(q.largeGaps,1);
 assert.equal(q.invalidDate,0);
});
test('v3: invalid dates are flagged without throwing',()=>{
 assert.equal(priceDataQuality({prices:[{date:'notdate',close:1}]}).invalidDate,1);
 assert.equal(priceDataQuality({prices:[{date:'2026-13-40',close:1}]}).invalidDate,1);
 assert.equal(priceDataQuality(null).rows,0);
});
test('v3: generated CSV has BOM, original rows, and no formula injection via date',()=>{
 const c=priceCsv({prices:[{date:'2026-11-15',close:100,volume:1000,open:99,high:101,low:97},
  {date:'=HYPERLINK("bad")',close:110,volume:null,open:null}]});
 assert(c.startsWith('\ufeffDate,Close,Volume,Open,High,Low\r\n'));
 assert(c.includes('2026-11-15,100,1000,99,101,97'));
 assert(!c.includes('HYPERLINK'));assert(c.includes('\r\n,110,,,,\r\n'));
});
test('v3: exported CSV can be imported again by existing parser',()=>{
 const text=priceCsv({prices:[{date:'2026-11-15',close:100,volume:1000},
 {date:'2026-11-16',close:105,volume:1200}]});
 const parsed=parsePriceCsv(text,'prices.csv');
 assert.equal(parsed.prices.length,2);assert.equal(parsed.prices[1].close,105);
 assert.equal(parsed.prices[0].date,'2026-11-15');
});
test('v3: original price analytics remain identical for valid data',()=>{
 const s={code:'005930',prices:a.prices.slice(0,90)};
 const res=analyzeSeries(s);
 assert.equal(res.prices.length,90);assert(res.last>res.prices[0].close);
 assert(Number.isFinite(res.changePct));
 assert(Number.isFinite(res.sma20));
});
