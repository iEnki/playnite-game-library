"""Playnite Game Library – updater service.

Receives a Playnite HTML export (zip or tar), fills missing covers from Steam, builds
games.json + the modern single-page app, enriches games with Steam store data and
optional AI "Good to Know" texts (Ollama / OpenAI / Claude) and swaps the result into
the web root served by nginx.

Configuration via environment variables (see README / .env.example):
  UPDATE_TOKEN, ADMIN_PASSWORD, SESSION_DAYS, SITE_TITLE, SITE_ICON, OLLAMA_URL, OLLAMA_MODEL,
  SITE_DIR (/site), DATA_DIR (/data), CONF_DIR (/conf)
"""
import os, re, io, sys, json, time, glob, html, shutil, difflib, tarfile, zipfile, threading
import subprocess, hmac, urllib.request, urllib.parse, urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

SITE = os.environ.get('SITE_DIR', '/site').rstrip('/')
NEXT = os.path.join(SITE, '.next')
PREV = os.path.join(SITE, '.prev')
UPLOAD = os.path.join(SITE, '.upload.bin')
BACKUP = os.environ.get('DATA_DIR', '/data').rstrip('/')        # persistent data (caches, AI texts, config)
CODE = os.path.dirname(os.path.abspath(__file__))                   # updater.py, gen.py, app.html
SITE_TITLE = os.environ.get('SITE_TITLE', '').strip() or 'My Game Library'
SITE_ICON = os.environ.get('SITE_ICON', '').strip() or '🎮'
CONF = os.environ.get('CONF_DIR', '/conf').rstrip('/')
MAPPING = os.path.join(BACKUP, 'mapping.json')
TOKEN = os.environ.get('UPDATE_TOKEN', '')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '') or TOKEN   # web login; falls back to UPDATE_TOKEN
try: SESSION_DAYS = max(1, min(365, int(os.environ.get('SESSION_DAYS', '30'))))
except ValueError: SESSION_DAYS = 30
MAX_UPLOAD = 2 * 1024 ** 3
UA = {'User-Agent': 'Mozilla/5.0 playnite-game-library-updater'}

NGINX_CONF = r"""
server {
    listen 80;
    server_name _;
    root /usr/share/nginx/html;
    index index.html;
    charset utf-8;
    client_max_body_size 2048m;
    resolver 127.0.0.11 valid=30s ipv6=off;

    gzip on;
    gzip_types application/json text/css application/javascript;
    gzip_min_length 1024;

    location ~ /\. { deny all; return 404; }

    location /api/ {
        set $up http://updater:8000;
        proxy_pass $up;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;   # set by an outer HTTPS proxy (NPM)
        proxy_request_buffering off;
        proxy_read_timeout 900s;
        proxy_send_timeout 900s;
    }
    location = /index.html { add_header Cache-Control "no-cache"; }
    location = /games.json { add_header Cache-Control "no-cache"; }
    location = /sw.js { add_header Cache-Control "no-cache"; add_header Service-Worker-Allowed "/"; }
    location = /manifest.webmanifest { default_type application/manifest+json; add_header Cache-Control "no-cache"; }
    location ~* \.(jpg|jpeg|png|webp|gif)$ { expires 7d; access_log off; }
}
"""

STATE = {'state': 'idle', 'log': [], 'started': None, 'finished': None, 'summary': None}
LOCK = threading.Lock()

def log(msg):
    line = time.strftime('%H:%M:%S ') + str(msg)
    print(line, flush=True)
    STATE['log'].append(line)
    del STATE['log'][:-400]

# ---------------------------------------------------------------- covers
def get(url, method='GET'):
    req = urllib.request.Request(url, headers=UA, method=method)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, (r.read() if method == 'GET' else b'')

def norm(s):
    s = html.unescape(s).lower()
    s = re.sub(r'[™®©]', '', s).replace('&', 'and')
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', s).split())

SUFFIX = re.compile(r'\b(game of the year|goty|definitive|deluxe|complete|enhanced|remastered|ultimate|gold|standard|digital|anniversary|special|collector s|director s cut|edition|playtest|demo|for windows 10|windows 10|pc)\b')
def core(s): return ' '.join(SUFFIX.sub(' ', norm(s)).split())
NUM = re.compile(r'\b(\d+|ii|iii|iv|v|vi|vii|viii|ix|x)\b')
def nums(s): return sorted(n for n in NUM.findall(norm(s)) if not re.fullmatch(r'(19|20)\d\d', n))

def search(term):
    q = urllib.parse.quote(term)
    for attempt in range(4):
        try:
            _, b = get(f'https://store.steampowered.com/api/storesearch/?term={q}&l=english&cc=AT')
            return json.loads(b).get('items', [])
        except Exception:
            time.sleep(5 * (attempt + 1))
    return []

def best(name):
    n, c = norm(name), core(name)
    terms = [name]
    if c and c != n: terms.append(c)
    stripped = re.split(r'\s[-:–]\s', name)[0]
    if stripped != name: terms.append(stripped)
    for t in terms:
        items = search(t); time.sleep(0.6)
        scored = []
        for it in items:
            iname = it.get('name', '')
            if norm(iname) == n: return it['id']
            if nums(iname) != nums(name): continue          # never mix up sequels
            r = max(difflib.SequenceMatcher(None, n, norm(iname)).ratio(),
                    difflib.SequenceMatcher(None, c, core(iname)).ratio() if c else 0)
            if c and core(iname) == c: r = 0.99
            scored.append((r, it['id']))
        if scored:
            scored.sort(reverse=True)
            if scored[0][0] >= 0.88: return scored[0][1]
    return None

def cover_url(appid):
    for u in (f'https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/library_600x900.jpg',
              f'https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/library_600x900_2x.jpg'):
        try:
            if get(u, 'HEAD')[0] == 200: return u
        except Exception: pass
    try:
        _, b = get(f'https://store.steampowered.com/api/appdetails?appids={appid}&filters=basic')
        d = json.loads(b)[str(appid)]
        if d.get('success'): return d['data']['header_image']
    except Exception: pass
    return ''

def fill_covers(root):
    mapping = json.load(open(MAPPING)) if os.path.exists(MAPPING) else {}
    files = glob.glob(os.path.join(root, '*.html'))
    pat = re.compile(r'<a href="([0-9a-f-]{36})\.html">\s*<img class="lozad" data-src=""/>\s*<figcaption>(.*?)</figcaption>')
    missing = {}
    for f in files:
        for gid, name in pat.findall(open(f, encoding='utf-8-sig').read()):
            missing[gid] = html.unescape(name)
    new = [g for g in missing if g not in mapping]
    log(f'Ohne Cover: {len(missing)} (davon neu zu suchen: {len(new)})')
    for i, gid in enumerate(new, 1):
        appid = best(missing[gid])
        mapping[gid] = cover_url(appid) if appid else ''
        if i % 25 == 0: log(f'  Steam-Suche {i}/{len(new)}')
    json.dump(mapping, open(MAPPING, 'w'), indent=0)
    use = {g: mapping[g] for g in missing if mapping.get(g)}
    def rep(m):
        gid = m.group(1); url = use.get(gid)
        if not url: return m.group(0)
        style = '' if 'library_600x900' in url else ' style="object-fit:cover;height:100%"'
        return m.group(0).replace('data-src=""', f'data-src="{url}"{style}', 1)
    for f in files:
        s = open(f, encoding='utf-8-sig').read(); o = s
        s = re.sub(r'<a href="([0-9a-f-]{36})\.html">\s*<img class="lozad" data-src=""/>', rep, s)
        gid = os.path.basename(f)[:-5]
        if gid in use:
            s = s.replace('data-src=""', f'data-src="{use[gid]}"').replace("url('')", f"url('{use[gid]}')", 1)
        if s != o: open(f, 'w', encoding='utf-8-sig').write(s)
    log(f'Cover ergänzt: {len(use)} von {len(missing)}')
    return len(use), len(missing)

# ---------------------------------------------------------------- steam details
STEAM_DIR = os.path.join(BACKUP, 'steam')
APPIDS = os.path.join(BACKUP, 'appids.json')
GENRE_DE = {'Adventure': 'Abenteuer', 'Strategy': 'Strategie', 'Simulator': 'Simulation',
            'Role-playing (RPG)': 'Rollenspiel', 'Racing': 'Rennspiele', 'Sport': 'Sport', 'Indie': 'Indie',
            'Real Time Strategy (RTS)': 'Strategie', 'Turn-based strategy (TBS)': 'Strategie',
            'Arcade': 'Gelegenheitsspiele', 'Shooter': 'Shooter', 'Puzzle': 'Puzzle', 'Platform': 'Plattformer',
            'Fighting': 'Kampfspiel', 'Music': 'Musik', 'Tactical': 'Taktik', 'Hack and slash/Beat \'em up': 'Action',
            'Point-and-click': 'Abenteuer', 'Card & Board Game': 'Karten & Brettspiele', 'Quiz/Trivia': 'Quiz',
            'Visual Novel': 'Visual Novel', 'MOBA': 'MOBA', 'Pinball': 'Flipper', 'Action': 'Action'}
FEAT_DE = {'Single Player': 'Einzelspieler', 'Multiplayer': 'Mehrspieler', 'Co-Operative': 'Koop',
           'Split Screen': 'Geteilter Bildschirm', 'Massively Multiplayer Online (MMO)': 'MMO',
           'Online Co-Op': 'Online-Koop', 'Local Co-Op': 'Lokaler Koop', 'Battle Royale': 'Battle Royale',
           'VR': 'VR', 'Cloud Saves': None, 'Controller Support': 'Controller', 'Full Controller Support': 'Controller'}
FEAT_KEEP = re.compile(r'Einzelspieler|Mehrspieler|Koop|PvP|Bildschirm|MMO|VR')

MON = {'jan': 1, 'feb': 2, 'mär': 3, 'mar': 3, 'apr': 4, 'mai': 5, 'may': 5, 'jun': 6, 'jul': 7, 'aug': 8,
       'sep': 9, 'okt': 10, 'oct': 10, 'nov': 11, 'dez': 12, 'dec': 12}
def iso_date(s):
    m = re.search(r'(\d{1,2})\.?\s*([A-Za-zÄÖÜäöü]+)\.?,?\s*(\d{4})', s or '')
    if m and m.group(2)[:3].lower() in MON:
        return f'{m.group(3)}-{MON[m.group(2)[:3].lower()]:02d}-{int(m.group(1)):02d}'
    m = re.search(r'(\d{4})', s or '')
    return m.group(1) if m else ''

def strip_tags(s): return ' '.join(html.unescape(re.sub(r'<[^>]+>', ' ', s or '')).split())

def appdetails(appid):
    os.makedirs(STEAM_DIR, exist_ok=True)
    p = os.path.join(STEAM_DIR, f'{appid}.json')
    if os.path.exists(p):
        return json.load(open(p, encoding='utf-8'))
    for attempt in range(5):
        try:
            _, b = get(f'https://store.steampowered.com/api/appdetails?appids={appid}&l=german&cc=AT')
            d = json.loads(b) or {}
            d = d.get(str(appid)) or {}
            data = d.get('data') if d.get('success') else None
            json.dump(data, open(p, 'w', encoding='utf-8'), ensure_ascii=False)
            time.sleep(1.6)
            return data
        except urllib.error.HTTPError as e:
            if e.code in (429, 403): log('  Steam-Limit erreicht – warte 60 s'); time.sleep(60)
            else: time.sleep(5)
        except Exception:
            time.sleep(5)
    return None

def resolve_appid(g, mapping, cache):
    for l in g.get('links', []):
        m = re.search(r'store\.steampowered\.com/app/(\d+)', l['u'])
        if m: return int(m.group(1))
    for u in (g.get('cover', ''), mapping.get(g['id']) or ''):
        m = re.search(r'/steam/apps/(\d+)/', u)
        if m: return int(m.group(1))
    if g['id'] in cache: return cache[g['id']]
    if g['id'] in mapping and not mapping[g['id']]:       # bereits erfolglos gesucht
        return None
    a = best(g['name']); cache[g['id']] = a
    return a

def video(mv):
    out = {'name': mv.get('name', ''), 'thumb': mv.get('thumbnail', '')}
    for k in ('mp4', 'webm'):
        v = mv.get(k) or {}
        if v.get('max') or v.get('480'): out[k] = v.get('max') or v.get('480')
    if mv.get('hls_h264'): out['hls'] = mv['hls_h264']
    return out

def enrich(root):
    gp = os.path.join(root, 'games.json')
    games = json.load(open(gp, encoding='utf-8'))
    mapping = json.load(open(MAPPING)) if os.path.exists(MAPPING) else {}
    cache = {k: v for k, v in (json.load(open(APPIDS)) if os.path.exists(APPIDS) else {}).items()}
    ddir = os.path.join(root, 'details'); os.makedirs(ddir, exist_ok=True)
    todo = sum(1 for g in games if True)
    fetched = found = 0
    for i, g in enumerate(games, 1):
        g['genres'] = list(dict.fromkeys(GENRE_DE.get(x, x) for x in g['genres']))
        g['features'] = list(dict.fromkeys(f for f in (FEAT_DE.get(x, x) for x in g['features']) if f))
        appid = resolve_appid(g, mapping, cache)
        if not appid: continue
        new = not os.path.exists(os.path.join(STEAM_DIR, f'{appid}.json'))
        d = appdetails(appid)
        if new:
            fetched += 1
            if fetched % 25 == 0:
                log(f'  Steam-Details {fetched} geladen ({i}/{todo})')
                json.dump(cache, open(APPIDS, 'w'))
        if not d: continue
        found += 1
        g['appid'] = appid
        if not g['genres']: g['genres'] = [x['description'] for x in d.get('genres', [])]
        if not g['developers']: g['developers'] = d.get('developers', [])
        if not g['publishers']: g['publishers'] = d.get('publishers', [])
        cats = [c['description'] for c in d.get('categories', [])]
        if not g['features']: g['features'] = [c for c in cats if FEAT_KEEP.search(c)]
        rd = (d.get('release_date') or {}).get('date', '')
        if not g['release'] and rd: g['release'] = iso_date(rd)
        m = re.search(r'(\d{4})', rd or '')
        if m: g['year'] = int(m.group(1))
        mc = d.get('metacritic') or {}
        if mc.get('score'): g['metacritic'] = mc['score']
        g['details'] = True
        req = d.get('pc_requirements') or {}
        if isinstance(req, list): req = {}
        det = {
            'appid': appid,
            'short': strip_tags(d.get('short_description', '')),
            'about': d.get('about_the_game') or d.get('detailed_description') or '',
            'screens': [{'t': s['path_thumbnail'], 'f': s['path_full']} for s in (d.get('screenshots') or [])][:16],
            'movies': [video(mv) for mv in (d.get('movies') or [])][:4],
            'metacritic': mc or None,
            'release': rd, 'website': d.get('website') or '',
            'languages': strip_tags((d.get('supported_languages') or '').replace('<strong>*</strong>', '*').split('<br>')[0]),
            'languages_note': strip_tags(' '.join((d.get('supported_languages') or '').split('<br>')[1:]).replace('<strong>*</strong>', '*')),
            'req_min': req.get('minimum', ''), 'req_rec': req.get('recommended', ''),
            'categories': cats, 'genres': [x['description'] for x in d.get('genres', [])],
            'developers': d.get('developers', []), 'publishers': d.get('publishers', []),
            'controller': d.get('controller_support', ''),
            'achievements': (d.get('achievements') or {}).get('total', 0),
            'age': d.get('required_age', 0), 'background': d.get('background_raw') or d.get('background') or '',
            'header': d.get('header_image', ''), 'free': d.get('is_free', False),
            'store': f'https://store.steampowered.com/app/{appid}',
        }
        json.dump(det, open(os.path.join(ddir, g['id'] + '.json'), 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    json.dump(cache, open(APPIDS, 'w'))
    json.dump(games, open(gp, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    log(f'Steam-Details: {found} von {len(games)} Spielen (neu geladen: {fetched})')
    return found

# ---------------------------------------------------------------- KI-Texte (Ollama)
AI_DIR = os.path.join(BACKUP, 'ai')
AI_CONF = os.path.join(BACKUP, 'ai_config.json')
AI_VERSION = 2
AI_SCHEMA = {
    'type': 'object',
    'properties': {
        'bekannt': {'type': 'boolean'},
        'pitch': {'type': 'string'},
        'gameplay': {'type': 'string'},
        'highlights': {'type': 'array', 'items': {'type': 'string'}},
        'hinweise': {'type': 'array', 'items': {'type': 'string'}},
        'fuer_wen': {'type': 'string'},
    },
    'required': ['bekannt', 'pitch', 'gameplay', 'highlights', 'hinweise', 'fuer_wen'],
}
AI_SYSTEM = (
    'Du bist ein erfahrener deutschsprachiger Games-Redakteur und schreibst kurze, ehrliche Spiel-Steckbriefe '
    'aus Gamer-Sicht für eine private Spielesammlung. Locker, direkt, ohne Marketing-Floskeln, ohne Superlative, '
    'ohne Emojis. Duze den Leser. Nutze die gelieferten Fakten; erfinde keine Zahlen, Preise, Awards oder Features. '
    'Wenn du etwas nicht sicher weißt, lass es weg.')
AI_PROMPT = '''Schreibe einen Steckbrief zu diesem Spiel als JSON mit diesen Feldern:
- "bekannt": {known_rule}
- "pitch": 1–2 Sätze: Worum geht es, was macht das Spiel aus?
- "gameplay": 2–4 Sätze: Wie spielt es sich konkret (Perspektive, Kern-Loop, Kampf/Erkundung/Bauen, Solo/Koop)?
- "highlights": 3–5 kurze Stichpunkte mit den Stärken bzw. dem, worauf man sich freuen kann.
- "hinweise": 0–3 kurze Stichpunkte, die man vorher wissen sollte (z. B. Online-Pflicht, Early Access, Schwierigkeit, Länge, technische Eigenheiten) – nur wenn sicher, sonst leere Liste.
- "fuer_wen": 1 Satz: Für welche Spieler ist das was?
{extra}
FAKTEN
Titel: {name}
Genres: {genres}
Entwickler: {dev} | Publisher: {pub} | Erschienen: {release}
Eigenschaften: {cats}
Metacritic: {mc}
Beschreibung (Shop):
{text}'''
KNOWN_STEAM = 'immer true.'
KNOWN_OWN = ('true, wenn du dieses konkrete Spiel sicher kennst; false, wenn nicht oder nur vage '
             '(dann alle anderen Felder leer lassen).')
EXTRA_OWN = ('\nHINWEIS: Für dieses Spiel gibt es keine Shop-Beschreibung. Stütze dich auf dein eigenes Wissen über '
             'genau dieses Spiel. Rate nicht – im Zweifel "bekannt": false.\n')

ANTHROPIC_URL = os.environ.get('ANTHROPIC_URL', 'https://api.anthropic.com').rstrip('/')
PROVIDERS = ('ollama', 'openai', 'anthropic')
PROV_NAME = {'ollama': 'Ollama', 'openai': 'OpenAI', 'anthropic': 'Claude'}
SECRETS = ('openai_key', 'anthropic_key')
AI_DEFAULTS = {
    'provider': 'ollama',
    'url': os.environ.get('OLLAMA_URL', 'http://host.docker.internal:11434').rstrip('/'),
    'model': os.environ.get('OLLAMA_MODEL', 'gemma4:12b-it-qat'),
    'openai_url': 'https://api.openai.com/v1', 'openai_key': '', 'openai_model': 'gpt-6-luna',
    'anthropic_key': '', 'anthropic_model': 'claude-haiku-4-5-20251001',
    'temperature': 0.5, 'num_ctx': 8192, 'timeout': 300, 'parallel': 1,
    'system': AI_SYSTEM,
    'auto_import': True, 'import_scope': 'missing', 'import_limit': 0, 'nosteam': False,
}
URL_RE = r'https?://[\w.\-]+(:\d+)?(/[\w.\-/~%]*)?'

def active_model(c):
    return {'ollama': c['model'], 'openai': c['openai_model'], 'anthropic': c['anthropic_model']}[c['provider']]

def ai_cfg():
    c = dict(AI_DEFAULTS)
    try: c.update(json.load(open(AI_CONF, encoding='utf-8')))
    except Exception: pass
    if c.get('provider') not in PROVIDERS: c['provider'] = 'ollama'
    return c

def mask(k): return (k[:3] + '…' + k[-4:]) if len(k) > 12 else ('••••' if k else '')

def ai_cfg_public(c):
    p = {k: v for k, v in c.items() if k not in SECRETS}
    for k in SECRETS:
        p[k + '_set'] = bool(c.get(k)); p[k + '_hint'] = mask(c.get(k) or '')
    p['active_model'] = active_model(c); p['provider_name'] = PROV_NAME[c['provider']]
    return p

def ai_cfg_save(new):
    c = ai_cfg(); err = []
    def num(k, lo, hi, typ):
        if k in new:
            try:
                v = typ(new[k])
                if not lo <= v <= hi: raise ValueError
                c[k] = v
            except Exception: err.append(f'{k}: {lo}–{hi}')
    if 'provider' in new:
        if new['provider'] in PROVIDERS: c['provider'] = new['provider']
        else: err.append('Anbieter unbekannt')
    if 'url' in new:
        u = str(new['url']).strip().rstrip('/')
        if not re.fullmatch(URL_RE, u): err.append('Ollama-URL ungültig (http://host:port)')
        else: c['url'] = re.sub(r'/(v1|api)(/.*)?$', '', u)
    if 'openai_url' in new:
        u = str(new['openai_url']).strip().rstrip('/') or AI_DEFAULTS['openai_url']
        if not re.fullmatch(URL_RE, u): err.append('OpenAI-URL ungültig')
        else: c['openai_url'] = re.sub(r'/chat/completions$', '', u)
    for k in ('model', 'openai_model', 'anthropic_model'):
        if k in new:
            m = str(new[k]).strip()
            if not m or len(m) > 120: err.append('Modell fehlt')
            else: c[k] = m
    for k in SECRETS:
        if new.get(k + '_clear'): c[k] = ''
        elif isinstance(new.get(k), str) and new[k].strip():
            v = new[k].strip()
            if len(v) > 400 or re.search(r'\s', v): err.append('API-Key ungültig')
            else: c[k] = v
    num('temperature', 0, 2, float); num('num_ctx', 1024, 131072, int)
    num('timeout', 30, 1800, int); num('import_limit', 0, 5000, int); num('parallel', 1, 8, int)
    if 'system' in new:
        s = str(new['system']).strip()
        c['system'] = s[:6000] if s else AI_SYSTEM
    for k in ('auto_import', 'nosteam'):
        if k in new: c[k] = bool(new[k])
    if 'import_scope' in new and new['import_scope'] in ('missing', 'model'): c['import_scope'] = new['import_scope']
    if err: raise ValueError(', '.join(err))
    tmp = AI_CONF + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f: json.dump(c, f, ensure_ascii=False, indent=1)
    os.replace(tmp, AI_CONF)
    return c

class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(f'HTTP {code}: {msg}'); self.code = code; self.msg = str(msg)

def http_json(url, body=None, headers=None, timeout=60):
    h = {'Content-Type': 'application/json', **UA, **(headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(4):
        req = urllib.request.Request(url, data=data, headers=h, method='POST' if data else 'GET')
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raw = e.read().decode('utf-8', 'replace')[:1500]
            try:
                j = json.loads(raw); er = j.get('error')
                msg = er.get('message') if isinstance(er, dict) else (er or j.get('message') or raw)
            except Exception: msg = raw
            if e.code in (429, 500, 502, 503, 529) and attempt < 3:
                try: wait = float(e.headers.get('retry-after') or 0)
                except ValueError: wait = 0
                time.sleep(min(wait or 3 * 2 ** attempt, 60)); continue
            raise ApiError(e.code, str(msg)[:300])

def oa_headers(c): return {'Authorization': 'Bearer ' + c['openai_key']} if c.get('openai_key') else {}
def an_headers(c): return {'x-api-key': c['anthropic_key'], 'anthropic-version': '2023-06-01'}

def ollama_info(url):
    _, b = get(url + '/api/version'); ver = json.loads(b).get('version')
    _, b = get(url + '/api/tags')
    models = sorted(m['name'] for m in json.loads(b).get('models', []))
    return {'version': ver, 'models': models}

SKIP_OA = re.compile(r'audio|realtime|tts|transcri|whisper|image|dall|embed|moderation|search|sora|computer|codex')
def provider_info(c):
    p = c['provider']
    if p == 'ollama': return ollama_info(c['url'])
    if p == 'openai':
        out = http_json(c['openai_url'] + '/models', headers=oa_headers(c), timeout=30)
        ids = [m.get('id') for m in out.get('data', []) if m.get('id')]
        if 'api.openai.com' in c['openai_url']:
            ids = [i for i in ids if re.match(r'(gpt|o\d|chatgpt)', i) and not SKIP_OA.search(i)]
        return {'version': None, 'models': sorted(ids)}
    if not c.get('anthropic_key'): raise RuntimeError('Kein Claude-API-Key hinterlegt')
    out = http_json(ANTHROPIC_URL + '/v1/models?limit=100', headers=an_headers(c), timeout=30)
    return {'version': None, 'models': [m['id'] for m in out.get('data', [])]}

def provider_check(c):
    p = c['provider']
    if p == 'openai' and not c.get('openai_key') and 'api.openai.com' in c['openai_url']:
        raise RuntimeError('Kein OpenAI-API-Key hinterlegt (KI-Einstellungen)')
    if p == 'anthropic' and not c.get('anthropic_key'):
        raise RuntimeError('Kein Claude-API-Key hinterlegt (KI-Einstellungen)')
    try:
        provider_info(c)
    except ApiError as e:
        if e.code in (401, 403): raise RuntimeError(f'{PROV_NAME[p]}: API-Key ungültig oder ohne Berechtigung ({e.msg})')
        raise RuntimeError(f'{PROV_NAME[p]} nicht erreichbar: {e}')
    except RuntimeError: raise
    except Exception as e:
        where = c['url'] if p == 'ollama' else PROV_NAME[p]
        raise RuntimeError(f'{PROV_NAME[p]} nicht erreichbar ({where}): {e}' + (' – läuft der PC / Ollama?' if p == 'ollama' else ''))

def provider_ok(c):
    try: provider_check(c); return True
    except Exception: return False

def parse_json(t):
    t = re.sub(r'^```(?:json)?|```$', '', (t or '').strip(), flags=re.M).strip()
    a, b = t.find('{'), t.rfind('}')
    return json.loads(t[a:b + 1] if a >= 0 and b > a else t)

def gen_ollama(c, prompt):
    out = http_json(c['url'] + '/api/chat', {'model': c['model'], 'stream': False, 'think': False, 'format': AI_SCHEMA,
                    'options': {'temperature': c['temperature'], 'num_ctx': c['num_ctx']},
                    'messages': [{'role': 'system', 'content': c['system']}, {'role': 'user', 'content': prompt}]},
                    timeout=c['timeout'])
    return parse_json(out['message']['content'])

def gen_openai(c, prompt):
    body = {'model': c['openai_model'], 'temperature': c['temperature'],
            'messages': [{'role': 'system', 'content': c['system']}, {'role': 'user', 'content': prompt}],
            'response_format': {'type': 'json_schema', 'json_schema': {'name': 'steckbrief', 'strict': True,
                                'schema': {**AI_SCHEMA, 'additionalProperties': False}}}}
    for _ in range(4):
        try:
            out = http_json(c['openai_url'] + '/chat/completions', body, oa_headers(c), c['timeout']); break
        except ApiError as e:
            m = e.msg.lower()
            if e.code == 400 and 'temperature' in body and 'temperature' in m: body.pop('temperature'); continue
            if e.code == 400 and body['response_format']['type'] == 'json_schema' and ('response_format' in m or 'json_schema' in m):
                body['response_format'] = {'type': 'json_object'}; continue
            raise
    return parse_json(out['choices'][0]['message']['content'])

def gen_anthropic(c, prompt):
    body = {'model': c['anthropic_model'], 'max_tokens': 4000, 'system': c['system'], 'temperature': c['temperature'],
            'messages': [{'role': 'user', 'content': prompt + '\n\nGib das Ergebnis ausschließlich über das Tool „steckbrief“ zurück.'}],
            'tools': [{'name': 'steckbrief', 'description': 'Speichert den fertigen Spiel-Steckbrief.', 'input_schema': AI_SCHEMA}],
            'tool_choice': {'type': 'tool', 'name': 'steckbrief'}}
    for _ in range(4):
        try:
            out = http_json(ANTHROPIC_URL + '/v1/messages', body, an_headers(c), c['timeout']); break
        except ApiError as e:
            m = e.msg.lower()
            if e.code == 400 and 'temperature' in body and 'temperature' in m: body.pop('temperature'); continue
            if e.code == 400 and body['tool_choice']['type'] == 'tool' and ('tool_choice' in m or 'thinking' in m):
                body['tool_choice'] = {'type': 'auto'}; continue
            raise
    for blk in out.get('content', []):
        if blk.get('type') == 'tool_use': return blk['input']
    return parse_json(''.join(b.get('text', '') for b in out.get('content', []) if b.get('type') == 'text'))

GEN = {'ollama': gen_ollama, 'openai': gen_openai, 'anthropic': gen_anthropic}

def ai_generate(g, d, cfg):
    own = not d
    d = d or {}
    text = strip_tags(d.get('about') or '') or strip_tags(g.get('desc') or '')
    text = (strip_tags(d.get('short') or '') + '\n' + text).strip()[:3500]
    prompt = AI_PROMPT.format(known_rule=KNOWN_OWN if own else KNOWN_STEAM, extra=EXTRA_OWN if own else '',
                              name=g['name'], genres=', '.join(g.get('genres') or d.get('genres') or []) or '–',
                              dev=', '.join(d.get('developers') or g.get('developers') or []) or '–',
                              pub=', '.join(d.get('publishers') or g.get('publishers') or []) or '–',
                              release=d.get('release') or g.get('release') or '–',
                              cats=(', '.join(d.get('categories') or g.get('features') or []))[:400] or '–',
                              mc=(d.get('metacritic') or {}).get('score') or g.get('metacritic') or '–', text=text or '–')
    res = GEN[cfg['provider']](cfg, prompt)
    clean = lambda s: ' '.join(str(s).split())
    lst = lambda v: v if isinstance(v, list) else ([v] if v else [])
    base = {'v': AI_VERSION, 'model': active_model(cfg), 'provider': cfg['provider'],
            'src': 'wissen' if own else 'steam', 'ts': int(time.time())}
    if own and not res.get('bekannt'):
        return {**base, 'unknown': True}
    out = {**base, 'pitch': clean(res.get('pitch', '')), 'gameplay': clean(res.get('gameplay', '')),
           'highlights': [clean(x) for x in lst(res.get('highlights')) if str(x).strip()][:6],
           'hinweise': [clean(x) for x in lst(res.get('hinweise')) if str(x).strip()][:4],
           'fuer_wen': clean(res.get('fuer_wen', ''))}
    if not out['pitch']: raise ValueError('Leere Antwort der KI')
    return out

def ai_load(gid):
    p = os.path.join(AI_DIR, gid + '.json')
    try: return json.load(open(p, encoding='utf-8'))
    except Exception: return None

def ai_publish(root, gid, ai):
    """Put a cached text into the site: Steam games -> details/<id>.json, others -> ai/<id>.json."""
    dp = os.path.join(root, 'details', gid + '.json')
    show = ai if ai and not ai.get('unknown') and ai.get('pitch') else None
    if os.path.exists(dp):
        d = json.load(open(dp, encoding='utf-8'))
        if d.get('ai') != show:
            if show: d['ai'] = show
            else: d.pop('ai', None)
            json.dump(d, open(dp, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
        return
    ap = os.path.join(root, 'ai', gid + '.json')
    if show:
        os.makedirs(os.path.dirname(ap), exist_ok=True)
        json.dump(show, open(ap, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    elif os.path.exists(ap):
        os.remove(ap)

def ai_apply_cached(root):
    games = json.load(open(os.path.join(root, 'games.json'), encoding='utf-8'))
    n = 0
    for g in games:
        ai = ai_load(g['id'])
        if ai: ai_publish(root, g['id'], ai); n += ai.get('pitch') is not None
    log(f'KI-Texte übernommen: {n} gespeicherte Texte')
    return n

def ai_overview(root=SITE):
    have = {}
    for p in glob.glob(os.path.join(AI_DIR, '*.json')):
        try: a = json.load(open(p, encoding='utf-8'))
        except Exception: continue
        have[os.path.basename(p)[:-5]] = {'m': a.get('model', ''), 'u': 1 if a.get('unknown') else 0, 's': a.get('src', 'steam')}
    return have

AI_JOB = {'state': 'idle', 'total': 0, 'done': 0, 'made': 0, 'fails': 0, 'skipped': 0, 'current': '',
          'started': None, 'finished': None, 'eta': None, 'rate': None, 'log': [], 'stop': False, 'opts': None}

def ailog(msg):
    line = time.strftime('%H:%M:%S ') + str(msg)
    print('[ai] ' + line, flush=True)
    AI_JOB['log'].append(line); del AI_JOB['log'][:-300]

def ai_select(root, opts, cfg):
    games = json.load(open(os.path.join(root, 'games.json'), encoding='utf-8'))
    ids = set(opts.get('ids') or []) if opts.get('scope') != 'all' else None
    mode = opts.get('mode', 'missing'); nosteam = bool(opts.get('nosteam'))
    todo = []
    for g in games:
        if ids is not None and g['id'] not in ids: continue
        steam = os.path.exists(os.path.join(root, 'details', g['id'] + '.json'))
        if not steam and not nosteam: continue
        c = ai_load(g['id'])
        if mode == 'missing' and c: continue
        if mode == 'model' and c and c.get('model') == active_model(cfg): continue
        todo.append((g, steam))
    lim = int(opts.get('limit') or 0)
    return todo[:lim] if lim > 0 else todo

def ai_run(root, opts):
    """Generate texts for the selection. Caller must hold LOCK."""
    cfg = ai_cfg()
    AI_JOB.update(state='running', total=0, done=0, made=0, fails=0, skipped=0, current='', started=time.time(),
                  finished=None, eta=None, rate=None, log=[], stop=False,
                  opts={k: opts.get(k) for k in ('scope', 'mode', 'nosteam', 'limit', 'label')})
    try:
        os.makedirs(AI_DIR, exist_ok=True)
        todo = ai_select(root, opts, cfg)
        AI_JOB['total'] = len(todo)
        par = max(1, min(8, int(cfg.get('parallel') or 1)))
        where = cfg['url'] if cfg['provider'] == 'ollama' else cfg['openai_url'] if cfg['provider'] == 'openai' else 'api.anthropic.com'
        ailog(f'{len(todo)} Spiele ausgewählt · {PROV_NAME[cfg["provider"]]} · {active_model(cfg)} · {where}'
              + (f' · {par} parallel' if par > 1 else ''))
        if not todo: AI_JOB['state'] = 'done'; ailog('Nichts zu tun ✔'); return
        provider_check(cfg)
        t0 = time.time(); streak = 0; pending = {}; it = iter(todo)

        def work(g, steam):
            d = None
            if steam:
                try: d = json.load(open(os.path.join(root, 'details', g['id'] + '.json'), encoding='utf-8'))
                except Exception: d = {}
            return ai_generate(g, d, cfg)

        with ThreadPoolExecutor(par) as ex:
            def fill():
                while len(pending) < par and not AI_JOB['stop']:
                    nxt = next(it, None)
                    if nxt is None: return
                    pending[ex.submit(work, *nxt)] = nxt[0]
            fill()
            while pending:
                AI_JOB['current'] = ', '.join(g['name'] for g in pending.values())[:240]
                done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                for f in done:
                    g = pending.pop(f)
                    try:
                        ai = f.result()
                        json.dump(ai, open(os.path.join(AI_DIR, g['id'] + '.json'), 'w', encoding='utf-8'), ensure_ascii=False)
                        ai_publish(root, g['id'], ai)
                        if ai.get('unknown'): AI_JOB['skipped'] += 1; ailog(f'  ? {g["name"]}: Spiel der KI unbekannt – übersprungen')
                        else: AI_JOB['made'] += 1
                        streak = 0
                    except Exception as e:
                        AI_JOB['fails'] += 1; streak += 1
                        ailog(f'  ✗ {g["name"]}: {e}')
                    AI_JOB['done'] += 1
                    AI_JOB['rate'] = (time.time() - t0) / AI_JOB['done']
                    AI_JOB['eta'] = int(AI_JOB['rate'] * (AI_JOB['total'] - AI_JOB['done']))
                    if AI_JOB['done'] % 10 == 0 or AI_JOB['done'] == AI_JOB['total']:
                        ailog(f'  {AI_JOB["done"]}/{AI_JOB["total"]} · ~{AI_JOB["rate"]:.1f} s/Spiel')
                if streak >= 5 and not provider_ok(cfg):
                    AI_JOB['stop'] = True
                    raise RuntimeError(f'{PROV_NAME[cfg["provider"]]} nicht mehr erreichbar – Lauf gestoppt. Später mit „nur fehlende“ fortsetzen.')
                if streak >= 15: AI_JOB['stop'] = True; raise RuntimeError('15 Fehler in Folge – Lauf gestoppt.')
                fill()
        if AI_JOB['stop']:
            ailog('Abgebrochen durch Benutzer'); AI_JOB['state'] = 'stopped'; return
        AI_JOB['state'] = 'done'
        ailog(f'Fertig ✔ – {AI_JOB["made"]} Texte erzeugt, {AI_JOB["skipped"]} unbekannt, {AI_JOB["fails"]} Fehler')
    except Exception as e:
        AI_JOB['state'] = 'error'; ailog('FEHLER: ' + str(e))
    finally:
        AI_JOB['current'] = ''; AI_JOB['finished'] = time.time()

def ai_start(opts):
    def run():
        try: ai_run(SITE, opts)
        finally: LOCK.release()
    threading.Thread(target=run, daemon=True).start()

# ---------------------------------------------------------------- PWA (installable app)
import zlib, struct, math
SW_JS = r"""// Service worker: makes the library installable and usable offline (last loaded state).
const V='gl-__VER__',IMG='gl-img';
self.addEventListener('install',e=>{self.skipWaiting();e.waitUntil(caches.open(V).then(c=>c.addAll(['./','games.json','manifest.webmanifest']).catch(()=>{})))});
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(k=>Promise.all(k.filter(n=>n!==V&&n!==IMG).map(n=>caches.delete(n)))).then(()=>self.clients.claim())));
async function trim(c){const k=await c.keys();if(k.length>2500)for(const r of k.slice(0,k.length-2000))await c.delete(r)}
self.addEventListener('fetch',e=>{
  const r=e.request;if(r.method!=='GET')return;
  const u=new URL(r.url);if(u.origin!==location.origin||u.pathname.includes('/api/'))return;
  if(/\.(jpe?g|png|webp|gif)$/i.test(u.pathname)){
    e.respondWith(caches.open(IMG).then(c=>c.match(r).then(m=>m||fetch(r).then(res=>{if(res.ok){c.put(r,res.clone()).then(()=>trim(c))}return res}))));return}
  e.respondWith(fetch(r).then(res=>{if(res.ok){const cl=res.clone();caches.open(V).then(c=>c.put(r,cl))}return res})
    .catch(()=>caches.match(r,{ignoreSearch:true}).then(m=>m||(r.mode==='navigate'?caches.match('./'):Response.error()))));
});
"""

def _png(w, h, rows):
    raw = b''.join(b'\x00' + bytes(r) for r in rows)
    ch = lambda t, d: struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    return b'\x89PNG\r\n\x1a\n' + ch(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)) + ch(b'IDAT', zlib.compress(raw, 9)) + ch(b'IEND', b'')

def _icon(size, scale=0.86):
    """Gradient square with a white gamepad (signed distance fields, anti-aliased)."""
    hyp = math.hypot
    def rrect(x, y, cx, cy, hw, hh, r):
        qx, qy = abs(x - cx) - hw + r, abs(y - cy) - hh + r
        return hyp(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - r
    def smin(a, b, k=0.05):
        h = max(k - abs(a - b), 0) / k
        return min(a, b) - h * h * k / 4
    btn = ((0.665, 0.425), (0.665, 0.555), (0.60, 0.49), (0.73, 0.49))
    def glyph(x, y):
        x, y = 0.5 + (x - 0.5) / scale, 0.5 + (y - 0.5) / scale
        body = smin(smin(rrect(x, y, 0.5, 0.46, 0.31, 0.13, 0.12), hyp(x - 0.285, y - 0.585) - 0.11), hyp(x - 0.715, y - 0.585) - 0.11)
        dpad = min(rrect(x, y, 0.335, 0.49, 0.075, 0.024, 0.01), rrect(x, y, 0.335, 0.49, 0.024, 0.075, 0.01))
        bt = min(hyp(x - bx, y - by) - 0.029 for bx, by in btn)
        return max(body, -dpad, -bt) * scale
    c1, c2 = (0x7c, 0x5c, 0xff), (0x22, 0xd3, 0xee)
    rows = []
    for j in range(size):
        y = (j + 0.5) / size; row = []
        for i in range(size):
            x = (i + 0.5) / size
            t = min(1, max(0, (x + y) / 2)); bg = [c1[k] + (c2[k] - c1[k]) * t for k in range(3)]
            d = glyph(x, y)
            a = 1.0 if d < -2 / size else 0.0 if d > 2 / size else min(1, max(0, 0.5 - d * size))
            row += [int(bg[k] + (255 - bg[k]) * a + 0.5) for k in range(3)]
        rows.append(row)
    return _png(size, size, rows)

def _png_size(b):
    return struct.unpack('>II', b[16:24]) if b[:8] == b'\x89PNG\r\n\x1a\n' else (512, 512)

def write_pwa(dst):
    """manifest.webmanifest, service worker and home-screen icons next to index.html."""
    custom = os.path.join(BACKUP, 'icon.png')                     # optional own icon (square PNG, >=512px)
    cache = os.path.join(BACKUP, 'pwa'); os.makedirs(cache, exist_ok=True)
    names = {'app-icon-192.png': 192, 'app-icon-512.png': 512, 'apple-touch-icon.png': 180}
    for n, sz in names.items():
        out = os.path.join(dst, n)
        if os.path.exists(custom):
            shutil.copyfile(custom, out); continue
        c = os.path.join(cache, f'v1-{n}')
        if not os.path.exists(c):
            with open(c + '.tmp', 'wb') as f: f.write(_icon(sz))
            os.replace(c + '.tmp', c)
        shutil.copyfile(c, out)
    if os.path.exists(custom):
        w, h = _png_size(open(custom, 'rb').read(32)); icons = [{'src': 'app-icon-512.png', 'sizes': f'{w}x{h}', 'type': 'image/png', 'purpose': 'any'}]
    else:
        icons = [{'src': 'app-icon-192.png', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
                 {'src': 'app-icon-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
                 {'src': 'app-icon-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'}]
    short = SITE_TITLE if len(SITE_TITLE) <= 15 else SITE_TITLE[:14].rstrip() + '…'
    man = {'name': SITE_TITLE, 'short_name': short, 'description': 'Spielebibliothek', 'lang': 'de',
           'id': './', 'start_url': './', 'scope': './', 'display': 'standalone', 'orientation': 'any',
           'background_color': '#0d0f14', 'theme_color': '#0d0f14', 'icons': icons}
    with open(os.path.join(dst, 'manifest.webmanifest'), 'w', encoding='utf-8') as f: json.dump(man, f, ensure_ascii=False, indent=1)
    with open(os.path.join(dst, 'sw.js'), 'w', encoding='utf-8') as f: f.write(SW_JS.replace('__VER__', str(int(time.time()))))

# ---------------------------------------------------------------- app page
def render_app(dst):
    """Write the single-page app with the configured title/icon to dst."""
    s = open(os.path.join(CODE, 'app.html'), encoding='utf-8').read()
    s = s.replace('__SITE_TITLE__', html.escape(SITE_TITLE)).replace('__SITE_ICON__', html.escape(SITE_ICON))
    tmp = os.path.join(os.path.dirname(dst), '.index.tmp')
    with open(tmp, 'w', encoding='utf-8') as f: f.write(s)
    os.replace(tmp, dst)
    try: write_pwa(os.path.dirname(dst))
    except Exception as e: print('PWA-Dateien fehlgeschlagen:', e, flush=True)

def init_site():
    """First start: empty library with import button. Later starts: refresh title/icon/app version."""
    os.makedirs(SITE, exist_ok=True); os.makedirs(BACKUP, exist_ok=True)
    gj = os.path.join(SITE, 'games.json')
    if not os.path.exists(gj):
        with open(gj, 'w', encoding='utf-8') as f: f.write('[]')
        print('Neue Installation: leere Bibliothek angelegt – Export über den Import-Button hochladen.', flush=True)
    render_app(os.path.join(SITE, 'index.html'))
    print(f'Seite bereit: "{SITE_TITLE}"', flush=True)

# ---------------------------------------------------------------- pipeline
def safe_extract(archive, dest):
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            for m in z.infolist():
                name = m.filename.replace('\\', '/')          # Windows-ZIPs mit Backslashes
                p = os.path.normpath(os.path.join(dest, name))
                if not p.startswith(dest + os.sep): raise ValueError('Ungültiger Pfad im ZIP: ' + m.filename)
                if name.endswith('/'): os.makedirs(p, exist_ok=True); continue
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with z.open(m) as src, open(p, 'wb') as dst: shutil.copyfileobj(src, dst)
        return 'zip'
    try:
        with tarfile.open(archive) as t:
            t.extractall(dest, filter='data')
    except tarfile.ReadError:
        raise ValueError('Datei ist kein gültiges ZIP- oder TAR-Archiv.')
    return 'tar'

def find_root(base):
    for dirpath, dirs, files in os.walk(base):
        if 'index.html' in files and any(re.fullmatch(r'[0-9a-f-]{36}\.html', f) for f in files):
            return dirpath
    return None

def swap(root):
    shutil.rmtree(PREV, ignore_errors=True); os.makedirs(PREV)
    for e in os.listdir(SITE):
        if e in ('.next', '.prev', '.upload.bin'): continue
        os.rename(os.path.join(SITE, e), os.path.join(PREV, e))
    for e in os.listdir(root):
        os.rename(os.path.join(root, e), os.path.join(SITE, e))
    shutil.rmtree(PREV, ignore_errors=True)
    shutil.rmtree(NEXT, ignore_errors=True)

def run_import(archive):
    try:
        shutil.rmtree(NEXT, ignore_errors=True); os.makedirs(NEXT)
        log(f'Entpacke Upload ({os.path.getsize(archive) / 1e6:.1f} MB) …')
        kind = safe_extract(archive, NEXT)
        os.remove(archive)
        root = find_root(NEXT)
        if not root: raise ValueError('Kein Playnite-Export gefunden (index.html + Spiel-Detailseiten fehlen).')
        n = len([f for f in os.listdir(root) if re.fullmatch(r'[0-9a-f-]{36}\.html', f)])
        log(f'{kind.upper()} ok – {n} Spiele-Detailseiten gefunden')
        if os.path.exists(os.path.join(root, 'index-classic.html')) or 'games.json' in os.listdir(root):
            # someone re-uploaded the already processed site -> restore classic index first
            if os.path.exists(os.path.join(root, 'index-classic.html')):
                os.replace(os.path.join(root, 'index-classic.html'), os.path.join(root, 'index.html'))
        ok, miss = fill_covers(root)
        os.replace(os.path.join(root, 'index.html'), os.path.join(root, 'index-classic.html'))
        r = subprocess.run([sys.executable, os.path.join(CODE, 'gen.py'), root], capture_output=True, text=True)
        if r.returncode: raise RuntimeError('games.json fehlgeschlagen: ' + r.stderr[-500:])
        log(r.stdout.strip())
        enrich(root)
        ai_apply_cached(root)
        render_app(os.path.join(root, 'index.html'))
        log('Neue Seite wird aktiviert …')
        swap(root)
        STATE['summary'] = {'games': n, 'covers_filled': ok, 'covers_missing': miss - ok}
        STATE['state'] = 'done'; log('Fertig ✔')
        cfg = ai_cfg()
        if cfg.get('auto_import'):
            log('KI-Texte für neue Spiele werden im Hintergrund erzeugt (siehe ✨ KI-Texte)')
            ai_run(SITE, {'scope': 'all', 'mode': cfg.get('import_scope', 'missing'), 'nosteam': cfg.get('nosteam'),
                          'limit': cfg.get('import_limit') or 0, 'label': 'Auto nach Import'})
    except Exception as e:
        STATE['state'] = 'error'; log('FEHLER: ' + str(e))
        shutil.rmtree(NEXT, ignore_errors=True)
        if os.path.exists(archive): os.remove(archive)
    finally:
        STATE['finished'] = time.time()
        LOCK.release()

def start_job(archive):
    STATE.update(state='running', log=[], started=time.time(), finished=None, summary=None)
    threading.Thread(target=run_import, args=(archive,), daemon=True).start()

# ---------------------------------------------------------------- admin login
import base64, hashlib, secrets
COOKIE = 'gl_admin'
_FAILS = {}            # ip -> [timestamps of failed logins]
_FAIL_LOCK = threading.Lock()

def _session_key():
    """Secret for signing session cookies. Persisted in the data volume; mixed with the
    password so that changing ADMIN_PASSWORD logs out every session."""
    p = os.path.join(BACKUP, 'session_secret')
    try:
        sec = open(p, 'rb').read()
        if len(sec) < 32: raise ValueError
    except Exception:
        os.makedirs(BACKUP, exist_ok=True)
        sec = secrets.token_bytes(48)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as f: f.write(sec)
    return hmac.new(sec, ('pw:' + ADMIN_PASSWORD).encode(), hashlib.sha256).digest()

def make_session(days):
    exp = int(time.time() + days * 86400)
    nonce = secrets.token_hex(8)
    msg = f'{exp}.{nonce}'
    sig = hmac.new(_session_key(), msg.encode(), hashlib.sha256).hexdigest()
    return f'{msg}.{sig}', exp

def check_session(value):
    try:
        exp, nonce, sig = value.split('.')
        good = hmac.new(_session_key(), f'{exp}.{nonce}'.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and int(exp) > time.time()
    except Exception:
        return False

def check_password(pw):
    if not ADMIN_PASSWORD: return False
    a = hashlib.sha256(pw.encode()).digest(); b = hashlib.sha256(ADMIN_PASSWORD.encode()).digest()
    return hmac.compare_digest(a, b)

def login_blocked(ip):
    now = time.time()
    with _FAIL_LOCK:
        lst = [t for t in _FAILS.get(ip, []) if now - t < 900]
        _FAILS[ip] = lst
        return len(lst) >= 5

def login_failed(ip):
    with _FAIL_LOCK: _FAILS.setdefault(ip, []).append(time.time())

# ---------------------------------------------------------------- http
class H(BaseHTTPRequestHandler):
    def _json(self, code, obj, cookie=None):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store'); self.send_header('Content-Length', str(len(b)))
        if cookie: self.send_header('Set-Cookie', cookie)
        self.end_headers(); self.wfile.write(b)

    def _ip(self):
        # nginx appends the address of the outer proxy last; the entry before it was added by that proxy
        xff = [x.strip() for x in self.headers.get('X-Forwarded-For', '').split(',') if x.strip()]
        return (xff[-2] if len(xff) >= 2 else xff[0] if xff else '') or self.headers.get('X-Real-IP', '') or self.client_address[0]

    def _cookie(self, name):
        for part in (self.headers.get('Cookie') or '').split(';'):
            k, _, v = part.strip().partition('=')
            if k == name: return v
        return ''

    def _token_ok(self):
        return bool(TOKEN) and hmac.compare_digest(self.headers.get('X-Token', ''), TOKEN)

    def _session_ok(self):
        v = self._cookie(COOKIE)
        return bool(v) and check_session(v)

    def _same_origin(self):
        origin = self.headers.get('Origin')
        if not origin: return True                      # same-origin GET/old browsers; SameSite=Strict covers CSRF
        host = (self.headers.get('X-Forwarded-Host') or self.headers.get('Host') or '').split(',')[0].strip()
        return urllib.parse.urlparse(origin).netloc == host

    def _authed(self, write=False):
        """Admin = valid session cookie (web UI) or X-Token header (update scripts)."""
        if self._token_ok(): return True
        return self._session_ok() and (not write or self._same_origin())

    def _set_cookie(self, value, max_age):
        secure = '; Secure' if (self.headers.get('X-Forwarded-Proto', '').split(',')[0].strip() == 'https') else ''
        return f'{COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict{secure}'

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n > 1 << 20: raise ValueError('Anfrage zu groß')
        return json.loads(self.rfile.read(n) or b'{}') if n else {}

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p == '/api/session':
            return self._json(200, {'admin': self._authed(), 'login': bool(ADMIN_PASSWORD)})
        if p == '/api/status':
            if not self._authed():                      # visitors only learn whether an import is running
                return self._json(200, {'state': STATE['state'], 'admin': False})
            job = {k: v for k, v in AI_JOB.items() if k != 'stop'}; job['log'] = AI_JOB['log'][-150:]
            return self._json(200, {**STATE, 'log': STATE['log'][-200:], 'auth': bool(TOKEN), 'admin': True, 'ai': job})
        if p.startswith('/api/ai/') and not self._authed():
            return self._json(401, {'error': 'Nicht angemeldet'})
        if p == '/api/ai/config':
            return self._json(200, {'config': ai_cfg_public(ai_cfg()), 'default_system': AI_SYSTEM, 'defaults': ai_cfg_public(AI_DEFAULTS)})
        if p == '/api/ai/overview':
            return self._json(200, {'have': ai_overview()})
        self._json(404, {'error': 'not found'})

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        if p == '/api/login':
            ip = self._ip()
            if not self._same_origin(): return self._json(403, {'error': 'Ungültige Herkunft'})
            if login_blocked(ip): return self._json(429, {'error': 'Zu viele Fehlversuche – bitte 15 Minuten warten'})
            try: body = self._body()
            except Exception: body = {}
            if not check_password(str(body.get('password') or '')):
                login_failed(ip); time.sleep(1)
                print(f'login failed from {ip}', flush=True)
                return self._json(401, {'error': 'Passwort falsch'})
            days = SESSION_DAYS if body.get('remember') else 1
            val, _ = make_session(days)
            print(f'login ok from {ip}', flush=True)
            return self._json(200, {'admin': True}, cookie=self._set_cookie(val, days * 86400))
        if p == '/api/logout':
            return self._json(200, {'admin': False}, cookie=self._set_cookie('', 0))
        if p.startswith('/api/ai/'):
            if not self._authed(write=True): return self._json(401, {'error': 'Nicht angemeldet'})
            try: body = self._body()
            except Exception as e: return self._json(400, {'error': str(e)})
            if p == '/api/ai/config':
                try: return self._json(200, {'config': ai_cfg_public(ai_cfg_save(body))})
                except ValueError as e: return self._json(400, {'error': str(e)})
            if p == '/api/ai/test':
                c = ai_cfg()
                if body.get('provider') in PROVIDERS: c['provider'] = body['provider']
                for k in ('url', 'openai_url', 'openai_key', 'anthropic_key'):
                    v = str(body.get(k) or '').strip().rstrip('/')
                    if v: c[k] = v
                c['url'] = re.sub(r'/(v1|api)(/.*)?$', '', c['url']); c['openai_url'] = re.sub(r'/chat/completions$', '', c['openai_url'])
                for k in ('url', 'openai_url'):
                    if not re.fullmatch(URL_RE, c[k]): return self._json(400, {'error': 'URL ungültig'})
                try: return self._json(200, {'ok': True, 'url': c['url'], 'openai_url': c['openai_url'], **provider_info(c)})
                except ApiError as e:
                    msg = 'API-Key ungültig' if e.code in (401, 403) else str(e)
                    return self._json(502, {'ok': False, 'error': f'{PROV_NAME[c["provider"]]}: {msg}'})
                except Exception as e: return self._json(502, {'ok': False, 'error': f'Keine Verbindung zu {PROV_NAME[c["provider"]]}: {e}'})
            if p == '/api/ai/run':
                if body.get('scope') not in ('all', 'ids'): return self._json(400, {'error': 'scope fehlt'})
                if body.get('mode') not in ('missing', 'model', 'all'): return self._json(400, {'error': 'mode fehlt'})
                if body['scope'] == 'ids' and not body.get('ids'): return self._json(400, {'error': 'Keine Spiele ausgewählt'})
                if not LOCK.acquire(blocking=False):
                    return self._json(409, {'error': 'Es läuft bereits ein Import oder KI-Lauf'})
                ai_start(body); return self._json(202, {'ok': True})
            if p == '/api/ai/stop':
                if AI_JOB['state'] == 'running': AI_JOB['stop'] = True
                return self._json(200, {'ok': True})
            return self._json(404, {'error': 'not found'})
        if not p.startswith('/api/import'): return self._json(404, {'error': 'not found'})
        if not self._authed(write=True):
            return self._json(401, {'error': 'Nicht angemeldet oder Token ungültig'})
        length = int(self.headers.get('Content-Length') or 0)
        if not length: return self._json(411, {'error': 'Content-Length fehlt'})
        if length > MAX_UPLOAD: return self._json(413, {'error': 'Upload zu groß'})
        if not LOCK.acquire(blocking=False): return self._json(409, {'error': 'Es läuft bereits ein Import oder KI-Lauf'})
        try:
            with open(UPLOAD, 'wb') as f:
                left = length
                while left:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk: raise IOError('Upload abgebrochen')
                    f.write(chunk); left -= len(chunk)
        except Exception as e:
            LOCK.release(); return self._json(400, {'error': str(e)})
        start_job(UPLOAD)
        self._json(202, {'ok': True})

    def log_message(self, fmt, *args):
        if '/api/status' not in (str(args[0]) if args else ''): print('http', fmt % args, flush=True)

def selftest():
    """Re-import the currently published export (offline test of the whole pipeline)."""
    tmp = os.path.join(SITE, '.upload.bin')
    with tarfile.open(tmp, 'w') as t:
        for e in os.listdir(SITE):
            if e.startswith('.') or e in ('games.json',): continue
            arc = 'export/' + ('index.html' if e == 'index-classic.html' else e)
            if e == 'index.html' and os.path.exists(os.path.join(SITE, 'index-classic.html')): continue
            t.add(os.path.join(SITE, e), arc)
    LOCK.acquire(); start_job(tmp)
    while STATE['state'] == 'running': time.sleep(1)
    print('SELFTEST', STATE['state'], STATE['summary'], flush=True)

if __name__ == '__main__':
    os.makedirs(CONF, exist_ok=True)
    open(os.path.join(CONF, 'default.conf'), 'w').write(NGINX_CONF)
    for d in (NEXT, PREV): shutil.rmtree(d, ignore_errors=True)
    if os.path.exists(UPLOAD): os.remove(UPLOAD)
    if '--selftest' in sys.argv: selftest(); sys.exit(0)
    if '--enrich' in sys.argv:
        enrich(SITE); sys.exit(0)
    if '--ai' in sys.argv:
        lim = int(sys.argv[sys.argv.index('--limit') + 1]) if '--limit' in sys.argv else 0
        LOCK.acquire(); ai_run(SITE, {'scope': 'all', 'mode': 'missing', 'limit': lim, 'nosteam': '--nosteam' in sys.argv}); sys.exit(0)
    init_site()
    _session_key()
    print('updater listening on :8000, token set:', bool(TOKEN), '| admin login:', 'ADMIN_PASSWORD' if os.environ.get('ADMIN_PASSWORD') else ('UPDATE_TOKEN' if TOKEN else 'disabled'), flush=True)
    ThreadingHTTPServer(('0.0.0.0', 8000), H).serve_forever()
