// Offline browser-state regression for KOSPI index, no external network access.
import {readFileSync} from 'node:fs';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {dirname,resolve} from 'node:path';
const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const pageTags=['#nav','#content','#notice','#current-page','#page-heading','#page-lead','#ai-tag','#watch-count','#watch-items','#watch-suggestions','#watch-search','#board-search','#board-sector','#board-sort','#refresh-three'];
const items=new Map(pageTags.map(k=>[k,{innerHTML:'',textContent:'',value:'',disabled:false,addEventListener(){},querySelectorAll(){return[];},focus(){},setSelectionRange(){}}]));
globalThis.document={querySelector:k=>items.get(k)||null,querySelectorAll:()=>[],addEventListener(){}};
globalThis.window={scrollTo(){}};
const data=[{date:'2026-11-16',close:2875.5,volume:0},{date:'2026-11-17',close:2889.0,volume:0},{date:'2026-11-18',close:2904.25,volume:0}];
const payload={ok:true,code:'KS11',name:'KOSPI 종합주가지수',unit:'pt',value:2904.25,change:15.25,change_pct:(2904.25/2889-1)*100,
  last_trade_date:'2026-11-18',prices:data,meta:{status:'fresh',source:'KS11 test data',realtime:false}};
const calls=[];
globalThis.fetch=async path=>{
  calls.push(path);
  if(path==='/api/catalog')return{ok:true,json:async()=>JSON.parse(readFileSync(resolve(root,'assets/catalog.json'),'utf8'))};
  if(path==='/api/health')return{ok:true,json:async()=>({ok:true,ai_configured:false,ai_calls_remaining_local:0})};
  if(path.startsWith('/api/index/kospi/cached'))return{ok:false,status:404,json:async()=>({error:'cache missing',ok:false})};
  if(path.startsWith('/api/index/kospi'))return{ok:true,json:async()=>payload};
  if(path.startsWith('/api/stock'))return{ok:false,status:503,json:async()=>({error:'offline fixture',ok:false})};
  throw Error('Unexpected: '+path);
};
const {nav}=await import(pathToFileURL(resolve(root,'src/app.js')).href);
await new Promise(r=>setTimeout(r,200));
nav('home');
let checks=0;
function assert(ok,name){if(!ok)throw Error(name);checks++;}
const home=items.get('#content').innerHTML;
assert(calls.some(x=>x.startsWith('/api/index/kospi?')),'KOSPI network request missing');
assert(home.includes('KOSPI 종합주가지수'),'KOSPI home widget missing');
assert(home.includes('2,904.25 pt'),'KOSPI observed index value missing');
assert(home.includes('2026-11-18'),'KOSPI date missing');
assert(home.includes('kospi-period'),'KOSPI chart selector missing');
assert(home.includes('svg'),'KOSPI chart missing');
nav('board');
assert(items.get('#content').innerHTML.includes('market-index-board'),'KOSPI board bar missing');
assert(items.get('#content').innerHTML.includes('2,904.25 pt'),'KOSPI board price missing');
console.log(`KOSPI UI state smoke: ${checks} passed`);
