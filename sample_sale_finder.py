#!/usr/bin/env python3
"""
ASOS Sample Sale — stock finder.

Same filters the sale gives you (product type, size, brand, price), plus the
one it withholds: whether the thing is actually in stock.

    python3 sample_sale_finder.py

Opens in your browser. Ctrl+C to stop. Standard library only, no installs.

The store's product feed sends no CORS headers, so a page opened straight from
your hard drive isn't allowed to read it. This serves the page and does the
fetching itself, which gets around that.
"""

import json
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, HTTPServer

ssl._create_default_https_context = ssl._create_unverified_context

BASE = "https://www.asossamplesale.com"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
PORT = 8765
TTL = 300
MAX_WORKERS = 1
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_BACKOFF = 3

SCOPES = [
    ("men", "Menswear"),
    ("women", "Womenswear"),
    ("mens-last-chance-items", "Men's last chance"),
    ("womens-last-chance-items", "Women's last chance"),
    ("mens-latest-drop", "Men's latest drop"),
    ("womens-latest-drop", "Women's latest drop"),
    ("drop-3", "Everything (slow)"),
]

_cache = {}
_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)


# ---------------------------------------------------------------- data layer

def fetch_json(url, attempt=1):
    """Fetch JSON with retry logic and exponential backoff."""
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.load(resp)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        if attempt < MAX_RETRIES:
            wait = RETRY_BACKOFF ** (attempt - 1)
            time.sleep(wait)
            return fetch_json(url, attempt + 1)
        raise exc


def fetch_page(handle, page):
    """Fetch a single page of products. Returns (page_num, products, error)."""
    url = f"{BASE}/collections/{handle}/products.json?limit=250&page={page}"
    try:
        data = fetch_json(url)
        return (page, data.get("products", []), None)
    except Exception as exc:
        return (page, [], str(exc))


def get_collection(handle):
    """Every product in a collection, flattened to one row per variant."""
    with _lock:
        hit = _cache.get(handle)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]

    rows, seen = [], set()
    pages_done = [0]
    errors = []

    def process_batch(future):
        page, products, error = future.result()
        pages_done[0] += 1
        status = f"  {handle}: page {page}/{pages_done[0]}"
        if error:
            status += f" (error: {error})"
            errors.append(error)
        else:
            status += f" ({len(products)} items)"
        print(status, file=sys.stderr)

        for p in products:
            if p["handle"] in seen:
                continue
            seen.add(p["handle"])
            image = (p.get("images") or [{}])[0].get("src", "")
            for v in p.get("variants", []):
                size = (v.get("option1") or "").strip()
                if not size:
                    continue
                rows.append({
                    "t": p["title"],
                    "b": p.get("vendor", "") or "Unbranded",
                    "y": p.get("product_type", "") or "Other",
                    "h": p["handle"],
                    "img": image,
                    "s": size,
                    "p": float(v.get("price") or 0),
                    "a": bool(v.get("available")),
                    "id": v["id"],
                })

    futures = {}
    page = 1
    batch_size = MAX_WORKERS * 2
    fetching = True

    while fetching:
        while len(futures) < batch_size and fetching:
            future = _executor.submit(fetch_page, handle, page)
            futures[future] = page
            page += 1
            time.sleep(0.5)

        for future in as_completed(futures):
            process_batch(future)
            del futures[future]
            page_num, products, error = future.result()

            if not products:
                fetching = False
                break

    with _lock:
        _cache[handle] = (time.time(), rows)
    return rows


# ------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _send(self, body, ctype="application/json", code=200):
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path == "/":
                self._send(PAGE, "text/html")
            elif parsed.path == "/api/scopes":
                self._send(json.dumps({"scopes": [{"h": h, "t": t} for h, t in SCOPES]}))
            elif parsed.path == "/api/scan":
                handle = query.get("c", [""])[0]
                if not any(handle == h for h, _ in SCOPES):
                    self._send(json.dumps({"error": "Unknown section."}), code=400)
                    return
                print(f"  scanning {handle} ...")
                rows = get_collection(handle)
                print(f"  {len(rows)} variants found")
                self._send(json.dumps({"rows": rows}))
            else:
                self._send(json.dumps({"error": "Not found"}), code=404)
        except Exception as exc:
            error_msg = "Network error" if "urlopen" in str(type(exc)) else str(exc)
            self._send(json.dumps({"error": error_msg}), code=500)


# --------------------------------------------------------------------- page

PAGE = r"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sample sale — in stock only</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600&family=Archivo+Narrow:wght@600;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{
  --paper:#E4E4DE; --paper-2:#EDEDE8; --card:#F7F7F4;
  --ink:#16181C; --ink-3:#6E7169; --rule:#C8C9C1;
  --live:#1B4DFF; --dead:#B23A2E;
}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
  font:400 15px/1.5 Archivo,system-ui,sans-serif}
button,input,select{font:inherit;color:inherit}
.mono{font-family:'IBM Plex Mono',ui-monospace,monospace}

header{border-bottom:2px solid var(--ink);background:var(--paper-2)}
.head-in{max-width:1200px;margin:0 auto;padding:16px 22px;
  display:flex;align-items:center;gap:16px;flex-wrap:wrap}
h1{font-family:'Archivo Narrow',sans-serif;font-weight:700;font-size:24px;
  letter-spacing:.02em;text-transform:uppercase;margin:0}
#scope{padding:7px 10px;border:1px solid var(--rule);background:var(--card);border-radius:2px}
.rescan{background:none;border:1px solid var(--rule);border-radius:2px;
  padding:7px 13px;font-size:12px;cursor:pointer;color:var(--ink-3)}
.rescan:hover{background:var(--card);color:var(--ink)}
.rescan:disabled{opacity:0.5;cursor:not-allowed}
.stamp-time{margin-left:auto;font-size:11.5px;color:var(--ink-3)}

.shell{max-width:1200px;margin:0 auto;padding:20px 22px;
  display:grid;grid-template-columns:265px 1fr;gap:26px;align-items:start}
@media(max-width:900px){.shell{grid-template-columns:1fr}}

.rail{position:sticky;top:16px;max-height:calc(100vh - 32px);overflow:auto;padding-right:4px}
@media(max-width:900px){.rail{position:static;max-height:none}}
.facet{border:none;border-top:1px solid var(--rule);margin:0 0 4px;padding:12px 0 14px}
.facet:first-child{border-top:none;padding-top:0}
.facet legend{font-family:'Archivo Narrow',sans-serif;font-weight:700;font-size:11px;
  letter-spacing:.14em;text-transform:uppercase;color:var(--ink-3);padding:0;
  display:flex;width:100%;justify-content:space-between;align-items:baseline}
.facet legend .n{font-family:'IBM Plex Mono',monospace;font-size:10.5px;letter-spacing:0}
.box{max-height:190px;overflow:auto;border:1px solid var(--rule);
  background:var(--card);border-radius:2px;margin-top:9px}
.box::-webkit-scrollbar{width:9px}
.box::-webkit-scrollbar-thumb{background:var(--rule)}
label.row{display:flex;gap:9px;align-items:baseline;padding:6px 9px;cursor:pointer;
  font-size:13.5px;border-bottom:1px solid rgba(200,201,193,.5)}
label.row:last-child{border-bottom:none}
label.row[hidden]{display:none}
label.row:hover{background:var(--paper-2)}
label.row input{margin:0;accent-color:var(--live);flex:none;position:relative;top:1px}
label.row.dim{opacity:.45}
.row .tally{margin-left:auto;font-family:'IBM Plex Mono',monospace;
  font-size:11px;color:var(--ink-3);white-space:nowrap}
.row .tally b{color:var(--live);font-weight:500}
.row .tally.zero b{color:var(--ink-3)}
.ffind{width:100%;padding:6px 9px;border:1px solid var(--rule);
  background:var(--card);border-radius:2px;margin-top:8px;font-size:13px}
.prices{display:flex;gap:8px;align-items:center;margin-top:10px}
.prices input{width:100%;padding:7px 9px;border:1px solid var(--rule);
  background:var(--card);border-radius:2px;font-family:'IBM Plex Mono',monospace;font-size:13px}
.prices span{color:var(--ink-3);font-size:12px}
.reset{width:100%;margin-top:14px;padding:9px;border:1px solid var(--rule);
  background:none;border-radius:2px;font-size:11.5px;letter-spacing:.09em;
  text-transform:uppercase;font-family:'Archivo Narrow',sans-serif;font-weight:600;
  color:var(--ink-3);cursor:pointer}
.reset:hover{background:var(--card);color:var(--ink)}

.ratio{border:2px solid var(--ink);background:var(--card);padding:13px 15px;margin-bottom:16px}
.ratio-top{display:flex;justify-content:space-between;align-items:baseline;
  gap:12px;flex-wrap:wrap;margin-bottom:10px}
.ratio-n{font-family:'Archivo Narrow',sans-serif;font-size:29px;font-weight:700;line-height:1}
.ratio-n b{color:var(--live)}
.ratio-note{font-size:12.5px;color:var(--ink-3);max-width:46ch}
.bar{height:11px;display:flex;border:1px solid var(--ink);overflow:hidden}
.bar i{display:block;height:100%}
.b-live{background:var(--live)}
.b-dead{background:repeating-linear-gradient(45deg,var(--dead) 0 3px,transparent 3px 6px);
  background-color:rgba(178,58,46,.16)}
.bar-key{display:flex;gap:16px;margin-top:7px;font-size:11px;color:var(--ink-3)}
.bar-key span{display:flex;align-items:center;gap:6px}
.dot{width:9px;height:9px;flex:none;border:1px solid var(--ink)}
.dot.live{background:var(--live)}
.dot.dead{background:rgba(178,58,46,.2)}

.toolbar{display:flex;gap:16px;align-items:center;flex-wrap:wrap;
  padding-bottom:10px;border-bottom:1px solid var(--rule)}
.toolbar label{font-size:12.5px;color:var(--ink-3);display:flex;align-items:center;gap:7px}
.toolbar select{padding:5px 8px;font-size:12.5px;border:1px solid var(--rule);
  background:var(--card);border-radius:2px}

.item{display:grid;grid-template-columns:60px 1fr auto;gap:14px;align-items:center;
  padding:12px 3px;border-bottom:1px solid var(--rule)}
@media(max-width:560px){.item{grid-template-columns:50px 1fr;row-gap:9px}
  .item .act{grid-column:2;flex-direction:row;align-items:center;gap:12px}}
.item img{width:60px;height:75px;object-fit:cover;background:var(--paper-2);
  border:1px solid var(--rule)}
@media(max-width:560px){.item img{width:50px;height:63px}}
.vendor{font-size:11.5px;color:var(--ink-3);text-transform:uppercase;letter-spacing:.07em}
.name{font-weight:500;line-height:1.35}
.sizes{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}
.chip{font-family:'IBM Plex Mono',monospace;font-size:11px;padding:2px 7px;
  border:1px solid var(--live);color:var(--live);border-radius:2px}
.chip.out{border-color:var(--rule);color:var(--ink-3);
  text-decoration:line-through;text-decoration-color:var(--dead)}
.price{font-family:'Archivo Narrow',sans-serif;font-size:20px;font-weight:700;
  text-align:right;white-space:nowrap}
.act{display:flex;flex-direction:column;gap:6px;align-items:flex-end}
.act a{font-size:11.5px;text-decoration:none;letter-spacing:.06em;text-transform:uppercase;
  font-family:'Archivo Narrow',sans-serif;font-weight:600}
.act a.cart{background:var(--live);color:#fff;padding:6px 12px;border-radius:2px}
.act a.cart:hover{background:#0B37D6}
.act a.view{color:var(--ink-3)}
.act a.view:hover{color:var(--ink)}
.item.ghost{opacity:.5}
.item.ghost .name{text-decoration:line-through;text-decoration-color:var(--dead)}
.item.ghost img{filter:grayscale(1)}
.stamp{font-family:'Archivo Narrow',sans-serif;font-size:10.5px;font-weight:700;
  letter-spacing:.1em;text-transform:uppercase;color:var(--dead);
  border:1.5px solid var(--dead);padding:3px 8px;transform:rotate(-3deg)}

.empty{padding:44px 10px;text-align:center;color:var(--ink-3)}
.empty strong{display:block;font-family:'Archivo Narrow',sans-serif;font-size:19px;
  text-transform:uppercase;letter-spacing:.05em;color:var(--ink);margin-bottom:7px}
.err{border-left:3px solid var(--dead);background:rgba(178,58,46,.07);
  padding:12px 15px;font-size:13.5px}
.spin{display:inline-block;width:12px;height:12px;border:2px solid var(--rule);
  border-top-color:var(--live);border-radius:50%;animation:sp .7s linear infinite;
  vertical-align:-2px;margin-right:7px}
@keyframes sp{to{transform:rotate(360deg)}}
@media(prefers-reduced-motion:reduce){.spin{animation:none}}
:focus-visible{outline:2px solid var(--live);outline-offset:2px}
</style>

<header><div class="head-in">
  <h1>In stock only</h1>
  <select id="scope" aria-label="Section to scan"></select>
  <button class="rescan" id="rescan" type="button">Refresh stock</button>
  <span class="stamp-time" id="stamp"></span>
</div></header>

<div class="shell">
  <form class="rail" id="rail" onsubmit="return false">
    <fieldset class="facet"><legend>Search <span class="n" id="n-search"></span></legend>
      <input class="ffind" id="search-prod" type="search" placeholder="e.g. oxford shirt…" aria-label="Search products">
    </fieldset>

    <fieldset class="facet"><legend>Product type <span class="n" id="n-type"></span></legend>
      <div class="box" id="f-type"></div></fieldset>

    <fieldset class="facet"><legend>Size <span class="n" id="n-size"></span></legend>
      <input class="ffind" id="find-size" type="search" placeholder="Find a size…" aria-label="Find a size">
      <div class="box" id="f-size"></div></fieldset>

    <fieldset class="facet"><legend>Brand <span class="n" id="n-brand"></span></legend>
      <input class="ffind" id="find-brand" type="search" placeholder="Find a brand…" aria-label="Find a brand">
      <div class="box" id="f-brand"></div></fieldset>

    <fieldset class="facet"><legend>Price</legend>
      <div class="prices">
        <input id="pmin" type="number" min="0" placeholder="Min" aria-label="Minimum price">
        <span>to</span>
        <input id="pmax" type="number" min="0" placeholder="Max" aria-label="Maximum price">
      </div></fieldset>

    <button class="reset" id="reset" type="button">Clear all filters</button>
  </form>

  <main id="out"><div class="empty"><span class="spin"></span>Loading the sale…</div></main>
</div>

<script>
const $ = s => document.querySelector(s);
const el = (t,c,x) => { const n=document.createElement(t);
  if(c)n.className=c; if(x!=null)n.textContent=x; return n; };

let ROWS = [];
let PICK = { y:new Set(), s:new Set(), b:new Set() };
let PMIN = null, PMAX = null;
let INSTOCK = true, SORT = 'price-asc';
let LOADING = false;
let SEARCH = '';

fetch('/api/scopes').then(r=>r.json()).then(d=>{
  const sel = $('#scope');
  d.scopes.forEach(s => { const o = el('option',null,s.t); o.value = s.h; sel.appendChild(o); });
  scan(sel.value);
});
$('#scope').addEventListener('change', e => scan(e.target.value));
$('#rescan').addEventListener('click', () => {
  if (!LOADING) scan($('#scope').value, true);
});

async function scan(handle, force) {
  LOADING = true;
  $('#rescan').disabled = true;
  $('#out').innerHTML = '<div class="empty"><span class="spin"></span>Reading the sale…</div>';
  $('#stamp').textContent = '';
  try {
    const r = await fetch('/api/scan?c=' + encodeURIComponent(handle) + (force?'&_='+Date.now():''));
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    ROWS = d.rows;
    PICK = { y:new Set(), s:new Set(), b:new Set() };
    PMIN = PMAX = null; $('#pmin').value = ''; $('#pmax').value = '';
    $('#stamp').textContent = 'Stock read ' + new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
    draw();
  } catch (err) {
    $('#out').innerHTML = '';
    $('#out').appendChild(el('div','err','Could not read the sale: ' + err.message +
      '. Check your connection, then hit refresh stock.'));
  } finally {
    LOADING = false;
    $('#rescan').disabled = false;
  }
}

function pass(r, skip) {
  if (skip !== 'y' && PICK.y.size && !PICK.y.has(r.y)) return false;
  if (skip !== 's' && PICK.s.size && !PICK.s.has(r.s)) return false;
  if (skip !== 'b' && PICK.b.size && !PICK.b.has(r.b)) return false;
  if (PMIN != null && r.p < PMIN) return false;
  if (PMAX != null && r.p > PMAX) return false;
  if (SEARCH) {
    const hay = (r.t + ' ' + r.b + ' ' + r.y).toLowerCase();
    if (!SEARCH.toLowerCase().split(/\s+/).filter(Boolean).every(w => hay.includes(w))) return false;
  }
  return true;
}
const subset = skip => ROWS.filter(r => pass(r, skip));

function counts(rows, key) {
  const m = new Map();
  rows.forEach(r => {
    const c = m.get(r[key]) || {live:0,total:0};
    c.total++; if (r.a) c.live++;
    m.set(r[key], c);
  });
  return m;
}

function facet(boxId, nId, key, numeric) {
  const box = $(boxId); box.textContent = '';
  const m = counts(subset(key), key);
  $(nId).textContent = PICK[key].size ? PICK[key].size + ' picked' : m.size;

  [...m.entries()].sort((a,b) =>
      b[1].live - a[1].live ||
      a[0].localeCompare(b[0], undefined, numeric ? {numeric:true} : {}))
    .forEach(([val, c], i) => {
      const row = el('label','row' + (c.live ? '' : ' dim'));
      row.dataset.name = val.toLowerCase();
      row.dataset.i = i;
      const cb = el('input'); cb.type='checkbox'; cb.value=val;
      cb.checked = PICK[key].has(val);
      cb.addEventListener('change', () => {
        cb.checked ? PICK[key].add(val) : PICK[key].delete(val);
        draw();
      });
      const tally = el('span','tally' + (c.live ? '' : ' zero'));
      tally.innerHTML = '<b>' + c.live + '</b>/' + c.total;
      row.append(cb, el('span', numeric ? 'mono' : null, val), tally);
      box.appendChild(row);
    });
  applyFind(boxId);
}

function applyFind(boxId) {
  const input = boxId === '#f-size' ? $('#find-size') : boxId === '#f-brand' ? $('#find-brand') : null;
  if (!input) return;
  const q = norm(input.value);
  const box = $(boxId);
  const rows = Array.from(box.querySelectorAll('label.row'));
  rows.sort((a,b) => a.dataset.i - b.dataset.i);
  if (!q) { rows.forEach(r => { r.hidden = false; box.appendChild(r); }); return; }
  const prefix = [], contains = [];
  rows.forEach(r => {
    const name = norm(r.dataset.name);
    r.hidden = true;
    if (name.startsWith(q)) prefix.push(r);
    else if (name.includes(q)) contains.push(r);
  });
  [...prefix, ...contains].forEach(r => { r.hidden = false; box.appendChild(r); });
}
const norm = s => (s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
$('#search-prod').addEventListener('input', e => { SEARCH = e.target.value; draw(); });
$('#find-size').addEventListener('input', () => applyFind('#f-size'));
$('#find-brand').addEventListener('input', () => applyFind('#f-brand'));

$('#pmin').addEventListener('input', e => { PMIN = e.target.value === '' ? null : +e.target.value; draw(); });
$('#pmax').addEventListener('input', e => { PMAX = e.target.value === '' ? null : +e.target.value; draw(); });
$('#reset').addEventListener('click', () => {
  PICK = { y:new Set(), s:new Set(), b:new Set() };
  PMIN = PMAX = null; SEARCH = '';
  $('#pmin').value=''; $('#pmax').value='';
  $('#search-prod').value=''; $('#find-size').value=''; $('#find-brand').value=''; draw();
});

function draw() {
  facet('#f-type','#n-type','y',false);
  facet('#f-size','#n-size','s',true);
  facet('#f-brand','#n-brand','b',false);
  render();
}

function render() {
  const out = $('#out'); out.textContent = '';
  if (!ROWS.length) return;

  const rows = subset(null);
  const live = rows.filter(r => r.a).length;
  const pct = rows.length ? Math.round(live / rows.length * 100) : 0;

  const box = el('div','ratio');
  const top = el('div','ratio-top');
  const n = el('div','ratio-n');
  n.innerHTML = '<b>' + live + '</b> <span style="color:var(--ink-3)">/ ' + rows.length + '</span>';
  const note = el('div','ratio-note', rows.length
    ? 'in stock out of everything matching your filters — ' + pct + '% of what the sale would show you.'
    : 'Nothing matches those filters.');
  top.append(n, note); box.appendChild(top);
  const bar = el('div','bar');
  const bl = el('i','b-live'); bl.style.width = pct + '%';
  const bd = el('i','b-dead'); bd.style.width = (100-pct) + '%';
  bar.append(bl,bd); box.appendChild(bar);
  const key = el('div','bar-key');
  const k1 = el('span'); k1.append(el('i','dot live'), el('span',null,'buyable now'));
  const k2 = el('span'); k2.append(el('i','dot dead'), el('span',null,'listed but gone'));
  key.append(k1,k2); box.appendChild(key);
  out.appendChild(box);

  const tb = el('div','toolbar');
  const sortLab = el('label',null,'Sort by');
  const sel = el('select');
  [['price-asc','Price low to high'],['price-desc','Price high to low'],
   ['name','Name A–Z'],['brand','Brand']].forEach(([v,t]) => {
    const o = el('option',null,t); o.value=v; if(v===SORT) o.selected=true; sel.appendChild(o); });
  sel.addEventListener('change', () => { SORT = sel.value; render(); });
  sortLab.appendChild(sel);
  const stockLab = el('label');
  const scb = el('input'); scb.type='checkbox'; scb.checked = INSTOCK;
  scb.addEventListener('change', () => { INSTOCK = scb.checked; render(); });
  stockLab.append(scb, el('span',null,'In stock only'));
  tb.append(sortLab, stockLab); out.appendChild(tb);

  const byProduct = new Map();
  rows.forEach(r => {
    const g = byProduct.get(r.h) || { r, live:[], all:[] };
    g.all.push(r); if (r.a) g.live.push(r);
    byProduct.set(r.h, g);
  });
  let groups = [...byProduct.values()];
  if (INSTOCK) groups = groups.filter(g => g.live.length);
  groups.forEach(g => g.cheap = Math.min(...(g.live.length ? g.live : g.all).map(v => v.p)));

  const cmp = {
    'price-asc':  (a,b) => a.cheap - b.cheap,
    'price-desc': (a,b) => b.cheap - a.cheap,
    'name':       (a,b) => a.r.t.localeCompare(b.r.t),
    'brand':      (a,b) => a.r.b.localeCompare(b.r.b) || a.cheap - b.cheap,
  }[SORT];

  if (!groups.length) {
    const e = el('div','empty');
    e.appendChild(el('strong',null, INSTOCK ? 'All gone' : 'Nothing matches'));
    e.appendChild(document.createTextNode(INSTOCK
      ? 'Everything matching those filters is sold out. Untick "in stock only" to see what you missed, or widen the filters.'
      : 'Try loosening a filter, or scan a different section.'));
    out.appendChild(e); return;
  }
  groups.sort(cmp).forEach(g => out.appendChild(card(g)));
}

function card(g) {
  const dead = !g.live.length;
  const wrap = el('div','item' + (dead ? ' ghost' : ''));
  const img = el('img'); img.src = g.r.img; img.alt=''; img.loading='lazy';
  wrap.appendChild(img);

  const mid = el('div');
  mid.appendChild(el('div','vendor', g.r.b));
  mid.appendChild(el('div','name', g.r.t));
  const chips = el('div','sizes');
  (dead ? g.all : g.live).slice(0,16).forEach(v =>
    chips.appendChild(el('span','chip' + (v.a ? '' : ' out'), v.s)));
  mid.appendChild(chips);
  wrap.appendChild(mid);

  const act = el('div','act');
  if (dead) {
    act.appendChild(el('span','stamp','Sold out'));
  } else {
    act.appendChild(el('div','price','£' + g.cheap.toFixed(2)));
    const best = g.live.slice().sort((a,b) => a.p - b.p)[0];
    const cart = el('a','cart','Add to cart');
    cart.href = 'https://www.asossamplesale.com/cart/' + best.id + ':1';
    cart.target='_blank'; cart.rel='noopener';
    const view = el('a','view','View item');
    view.href = 'https://www.asossamplesale.com/products/' + g.r.h + '?variant=' + best.id;
    view.target='_blank'; view.rel='noopener';
    act.append(cart, view);
  }
  wrap.appendChild(act);
  return wrap;
}
</script>
</html>
"""

# --------------------------------------------------------------------- main

def main():
    port = PORT
    for _ in range(20):
        try:
            server = HTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    else:
        print("Couldn't find a free port. Close some things and retry.")
        return

    url = f"http://127.0.0.1:{port}/"
    print(f"\n  Sample sale stock finder running at {url}")
    print("  Leave this window open. Press Ctrl+C to stop.\n")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        _executor.shutdown(wait=True)


if __name__ == "__main__":
    main()
