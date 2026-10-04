import re, os, glob, json, html, sys
SITE = sys.argv[1] if len(sys.argv) > 1 else '/site'
OUT = sys.argv[2] if len(sys.argv) > 2 else SITE
os.chdir(SITE)

def rd(f): return open(f, encoding='utf-8-sig').read()
def txt(s): return html.unescape(re.sub(r'<[^>]+>', '', s)).strip()

# index: cover per game (index.html may already be the new app -> use classic copy)
idx = 'index-classic.html' if os.path.exists('index-classic.html') else 'index.html'
covers = {}
for gid, src in re.findall(r'<a href="([0-9a-f-]{36})\.html">\s*<img class="lozad" data-src="([^"]*)"', rd(idx)):
    covers[gid] = src

# installation status groups
installed = set()
f = 'default list text_installationstatus_aufsteigend_name_aufsteigend.html'
if os.path.exists(f):
    for part in re.split(r'<TR id="quicklink-', rd(f))[1:]:
        grp = part.split('"', 1)[0]
        if grp == 'Installiert':
            installed |= set(re.findall(r'href="([0-9a-f-]{36})\.html"', part))
favs = set()
f = 'default list text_favorite_aufsteigend_name_aufsteigend.html'
if os.path.exists(f):
    for part in re.split(r'<TR id="quicklink-', rd(f))[1:]:
        if part.split('"', 1)[0] in ('Wahr', 'True', 'Ja'):
            favs |= set(re.findall(r'href="([0-9a-f-]{36})\.html"', part))

def secs(s):
    t = 0
    for n, u in re.findall(r'(\d+)\s*([dhms])\b', s):
        t += int(n) * {'d': 86400, 'h': 3600, 'm': 60, 's': 1}[u]
    return t

def date(s):
    m = re.search(r'(\d{2})\.(\d{2})\.(\d{4})', s)
    if not m or m.group(3) == '1970': return ''
    return f'{m.group(3)}-{m.group(2)}-{m.group(1)}'

def num(s):
    m = re.search(r'\d+', s); return int(m.group()) if m else None

games = []
for f in glob.glob('*.html'):
    gid = f[:-5]
    if not re.fullmatch(r'[0-9a-f-]{36}', gid): continue
    d = rd(f)
    name = txt(re.search(r'<title>(.*?)</title>', d, re.S).group(1))
    rows = dict((txt(k), v) for k, v in re.findall(r'<TR>\s*<TD>([^<]+)</TD>\s*<TD[^>]*>(.*?)</TD>\s*</TR>', d, re.S))
    def lst(k): return [txt(x) for x in re.findall(r'<a [^>]*>(.*?)</a>', rows.get(k, ''), re.S) if txt(x)]
    def one(k):
        l = lst(k); return l[0] if l else txt(rows.get(k, ''))
    imgs = re.findall(r'data-src="([^"]*)"', d)
    bg = re.search(r"url\('([^']*)'\)", d)
    desc = re.search(r'<div class="description">(.*?)</div>', d, re.S)
    links = [{'n': txt(n), 'u': u} for u, n in re.findall(r'<a href="(https?://[^"]+)"[^>]*>(.*?)</a>', rows.get('Links', ''), re.S)]
    games.append({
        'id': gid, 'name': name,
        'cover': covers.get(gid) or (imgs[1] if len(imgs) > 1 else ''),
        'icon': imgs[0] if imgs else '',
        'bg': bg.group(1) if bg else '',
        'playtime': secs(rows.get('Spielzeit', '')),
        'last': date(rows.get('Letzte Aktivität', '')),
        'added': date(rows.get('Hinzugefügt', '')),
        'status': one('Fertigstellungsstatus'),
        'library': one('Bibliothek'),
        'source': one('Quelle'),
        'platforms': lst('Plattform'),
        'release': date(rows.get('Veröffentlichungsdatum', '')) or one('Veröffentlichungsdatum'),
        'community': num(txt(rows.get('Community Bewertungen', ''))),
        'critic': num(txt(rows.get('Kritiker Punkte', ''))),
        'user': num(txt(rows.get('Benutzerwertung', ''))),
        'genres': lst('Genre'), 'developers': lst('Entwickler'), 'publishers': lst('Verleger'),
        'features': lst('Eigenschaft'), 'tags': lst('Tag'), 'links': links,
        'installed': gid in installed, 'favorite': gid in favs,
        'desc': desc.group(1).strip() if desc else '',
    })
games.sort(key=lambda g: g['name'].lower())
json.dump(games, open(os.path.join(OUT, 'games.json'), 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
print('games', len(games), 'with cover', sum(1 for g in games if g['cover']), 'installed', len(installed), 'favs', len(favs))
