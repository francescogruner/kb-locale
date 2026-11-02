#!/usr/bin/env python3
"""kb-locale: la tua base di conoscenza privata da video, audio, PDF e slide. Tutto in locale.

  kb.py process <cartella> --raccolta NOME     trascrive, fotogrammi+OCR, documenti->Markdown, indicizza
  kb.py search "domanda" [--raccolta NOME] [-k 8]   ricerca semantica con citazioni (minuto o slide)
  kb.py list                                   raccolte indicizzate e numero di file
  kb.py remove --raccolta NOME                 toglie una raccolta dall'indice
  kb.py serve                                  piccola API HTTP di ricerca (token obbligatorio)

Servizi locali usati (configurabili con variabili d'ambiente, vedi README):
  - whisper.cpp server (trascrizione)   KB_WHISPER_URL   default http://127.0.0.1:8080/inference
  - Ollama con bge-m3 (embedding)       KB_OLLAMA_URL    default http://127.0.0.1:11434/api/embed
  - Qdrant (indice vettoriale)          KB_QDRANT_URL    default http://127.0.0.1:6333
"""
import argparse, hashlib, json, os, re, shutil, sqlite3, subprocess, sys, time, uuid
from pathlib import Path

HOME = Path(os.environ.get('KB_HOME', os.path.expanduser('~/kb-locale')))
DERIVED = HOME / 'derived'          # trascrizioni, markdown, fotogrammi (restano in locale)
LEDGER = HOME / 'ledger.sqlite'
QDRANT = os.environ.get('KB_QDRANT_URL', 'http://127.0.0.1:6333')
COLL = os.environ.get('KB_COLLECTION', 'kb')
OLLAMA = os.environ.get('KB_OLLAMA_URL', 'http://127.0.0.1:11434/api/embed')
EMB_MODEL = os.environ.get('KB_EMBED_MODEL', 'bge-m3')
EMB_DIM = int(os.environ.get('KB_EMBED_DIM', '1024'))
WHISPER = os.environ.get('KB_WHISPER_URL', 'http://127.0.0.1:8080/inference')
OCR_LANG = os.environ.get('KB_OCR_LANG', 'ita+eng')
# Facoltativo: un comando che restituisce 0 quando la GPU e' occupata da qualcosa di piu' importante
# (es. un assistente vocale in chiamata). Finche' risponde 0, la trascrizione aspetta.
GPU_BUSY_CMD = os.environ.get('KB_GPU_BUSY_CMD', '')
VIDEO = {'.mp4', '.mkv', '.mov', '.webm', '.avi', '.m4v', '.mpg', '.mpeg', '.wmv'}
AUDIO = {'.mp3', '.m4a', '.wav', '.aac', '.ogg', '.flac'}
DOCS = {'.pdf', '.docx', '.pptx', '.xlsx', '.html', '.htm', '.md', '.txt', '.epub'}

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


def sh(cmd, timeout=None, check=True):
    r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL)
    if check and r.returncode != 0:
        raise RuntimeError('%s -> %s' % (cmd if isinstance(cmd, str) else ' '.join(cmd), (r.stderr or r.stdout)[-600:]))
    return r.stdout


def db():
    HOME.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(LEDGER)
    c.execute('create table if not exists files(sha text primary key, raccolta text, path text, tipo text, '
              'stato text, chunks int, durata real, ts real)')
    return c


def sha256(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def slug(s):
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')[:80] or 'x'


# ---------------------------------------------------------------- media
def gpu_busy():
    return bool(GPU_BUSY_CMD) and subprocess.run(GPU_BUSY_CMD, shell=True).returncode == 0


def transcribe(media, workdir):
    wav = workdir / 'audio16k.wav'
    sh(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(media), '-vn', '-ac', '1', '-ar', '16000', str(wav)])
    while gpu_busy():
        time.sleep(20)
    with open(wav, 'rb') as f:
        r = requests.post(WHISPER, files={'file': ('a.wav', f, 'audio/wav')},
                          data={'response_format': 'verbose_json', 'language': 'auto', 'temperature': '0'},
                          timeout=4 * 3600)
    r.raise_for_status()
    js = r.json()
    segs = [{'start': float(s.get('start', 0)), 'end': float(s.get('end', 0)), 'text': s.get('text', '').strip()}
            for s in js.get('segments', []) if s.get('text', '').strip()]
    if not segs and js.get('text'):
        segs = [{'start': 0.0, 'end': 0.0, 'text': js['text'].strip()}]
    wav.unlink(missing_ok=True)
    (workdir / 'transcript.json').write_text(json.dumps({'language': js.get('language'), 'segments': segs},
                                                        ensure_ascii=False, indent=1))
    return segs, js.get('language')


def keyframes(media, workdir):
    fdir = workdir / 'frames'
    fdir.mkdir(exist_ok=True)
    # cambio scena (slide) + almeno un fotogramma al minuto
    sh(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(media), '-vf',
        "select='gt(scene,0.30)+not(mod(n\\,1500))',scale=1280:-2", '-vsync', 'vfr', '-q:v', '4',
        str(fdir / 'f_%05d.jpg')], check=False)
    out = []
    for jpg in sorted(fdir.glob('f_*.jpg')):
        txt = sh(['tesseract', str(jpg), 'stdout', '-l', OCR_LANG, '--psm', '3'], check=False).strip()
        txt = re.sub(r'\s+', ' ', txt)
        if len(txt) >= 25:
            out.append({'frame': jpg.name, 'text': txt[:1500]})
    (workdir / 'slides.json').write_text(json.dumps(out, ensure_ascii=False, indent=1))
    return out


def duration(media):
    try:
        return float(sh(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(media)]).strip())
    except Exception:
        return 0.0


# ---------------------------------------------------------------- documents
def doc_to_md(path, workdir):
    from docling.document_converter import DocumentConverter
    res = DocumentConverter().convert(str(path))
    md = res.document.export_to_markdown()
    (workdir / 'doc.md').write_text(md)
    return md


# ---------------------------------------------------------------- chunks + index
def mmss(t):
    t = int(t or 0)
    return '%d:%02d:%02d' % (t // 3600, t % 3600 // 60, t % 60) if t >= 3600 else '%d:%02d' % (t // 60, t % 60)


def chunks_from_segments(segs, target=1100):
    cur, out = [], []
    for s in segs:
        cur.append(s)
        if sum(len(x['text']) for x in cur) >= target:
            out.append(cur)
            cur = cur[-2:]  # piccola sovrapposizione
    if cur and (not out or cur is not out[-1]):
        out.append(cur)
    return [{'text': ' '.join(x['text'] for x in c), 'start': c[0]['start'], 'end': c[-1]['end']} for c in out]


def chunks_from_md(md, target=1200, overlap=150):
    parts, out = re.split(r'\n(?=#+ )', md), []
    for p in parts:
        p = p.strip()
        while len(p) > target:
            cut = p.rfind('\n', 0, target)
            cut = cut if cut > target // 2 else target
            out.append(p[:cut])
            p = p[max(0, cut - overlap):]
        if p:
            out.append(p)
    return [{'text': c} for c in out if len(c.strip()) > 40]


def embed(texts):
    vecs = []
    for i in range(0, len(texts), 32):
        r = requests.post(OLLAMA, json={'model': EMB_MODEL, 'input': texts[i:i + 32]}, timeout=600)
        r.raise_for_status()
        vecs += r.json()['embeddings']
    return vecs


def ensure_collection():
    r = requests.get('%s/collections/%s' % (QDRANT, COLL), timeout=10)
    if r.status_code == 200:
        return
    requests.put('%s/collections/%s' % (QDRANT, COLL), json={'vectors': {'size': EMB_DIM, 'distance': 'Cosine'}},
                 timeout=30).raise_for_status()
    for f in ('raccolta', 'tipo', 'file_sha'):
        requests.put('%s/collections/%s/index' % (QDRANT, COLL), json={'field_name': f, 'field_schema': 'keyword'},
                     timeout=30)


def upsert(chunks, base):
    if not chunks:
        return 0
    ensure_collection()
    vecs = embed([c['text'] for c in chunks])
    pts = []
    for c, v in zip(chunks, vecs):
        pts.append({'id': str(uuid.uuid5(uuid.NAMESPACE_URL, base['file_sha'] + str(c.get('start', '')) + c['text'][:80])),
                    'vector': v, 'payload': dict(base, **c)})
    for i in range(0, len(pts), 128):
        requests.put('%s/collections/%s/points?wait=true' % (QDRANT, COLL), json={'points': pts[i:i + 128]},
                     timeout=300).raise_for_status()
    return len(pts)


def item_meta(path, root, raccolta):
    rel = path.relative_to(root)
    parts = rel.parts
    sezione = parts[-2] if len(parts) > 1 else ''
    titolo = re.sub(r'\.[^.]+$', '', parts[-1])
    return {'raccolta': raccolta, 'sezione': sezione, 'titolo': titolo, 'file': str(rel)}


def process(folder, raccolta):
    folder = Path(folder).resolve()
    c = db()
    done = n_new = 0
    for p in sorted(folder.rglob('*')):
        ext = p.suffix.lower()
        if not p.is_file() or ext not in VIDEO | AUDIO | DOCS:
            continue
        h = sha256(p)
        row = c.execute('select stato from files where sha=?', (h,)).fetchone()
        if row and row[0] == 'indicizzato':        # stesso contenuto gia' indicizzato: niente doppioni
            done += 1
            continue
        meta = item_meta(p, folder, raccolta)
        work = DERIVED / slug(raccolta) / h[:16]
        work.mkdir(parents=True, exist_ok=True)
        base = dict(meta, file_sha=h)
        try:
            if ext in VIDEO | AUDIO:
                segs, lang = transcribe(p, work)
                ch = [dict(x, tipo='parlato', lingua=lang) for x in chunks_from_segments(segs)]
                if ext in VIDEO:
                    ch += [{'text': s['text'], 'tipo': 'slide', 'frame': s['frame']} for s in keyframes(p, work)]
                dur, tipo = duration(p), 'video' if ext in VIDEO else 'audio'
            else:
                md = p.read_text(errors='ignore') if ext in ('.md', '.txt') else doc_to_md(p, work)
                ch = [dict(x, tipo='documento') for x in chunks_from_md(md)]
                dur, tipo = 0.0, 'documento'
            k = upsert(ch, base)
            c.execute('insert or replace into files values(?,?,?,?,?,?,?,?)',
                      (h, raccolta, str(p), tipo, 'indicizzato', k, dur, time.time()))
            c.commit()
            n_new += 1
            print('ok %s  (%d pezzi)' % (meta['file'], k), flush=True)
        except Exception as e:
            c.execute('insert or replace into files values(?,?,?,?,?,?,?,?)',
                      (h, raccolta, str(p), ext, 'errore: %s' % str(e)[:200], 0, 0, time.time()))
            c.commit()
            print('ERRORE %s: %s' % (meta['file'], str(e)[:200]), flush=True)
    print('fatto: %d nuovi, %d gia\' presenti' % (n_new, done))


# ---------------------------------------------------------------- search / list / remove / serve
def search(q, raccolta=None, k=8):
    vec = embed([q])[0]
    body = {'vector': vec, 'limit': k, 'with_payload': True}
    if raccolta:
        body['filter'] = {'must': [{'key': 'raccolta', 'match': {'value': raccolta}}]}
    r = requests.post('%s/collections/%s/points/search' % (QDRANT, COLL), json=body, timeout=60)
    r.raise_for_status()
    out = []
    for h in r.json()['result']:
        p = h['payload']
        dove = ''
        if p.get('tipo') == 'parlato':
            dove = '%s–%s' % (mmss(p.get('start')), mmss(p.get('end')))
        elif p.get('tipo') == 'slide':
            dove = 'slide %s' % p.get('frame', '')
        cit = ' › '.join(x for x in (p.get('raccolta'), p.get('sezione'), p.get('titolo'), dove) if x)
        out.append({'score': round(h['score'], 3), 'citazione': cit, 'tipo': p.get('tipo'), 'testo': p.get('text')})
    return out


def list_collections():
    c = db()
    for raccolta, n, ch, dur in c.execute("select raccolta, count(*), sum(chunks), sum(durata) from files "
                                          "where stato='indicizzato' group by raccolta order by raccolta"):
        print('%-50s %4d file  %6d pezzi  %5.1f h' % (raccolta, n, ch or 0, (dur or 0) / 3600))


def remove(raccolta):
    requests.post('%s/collections/%s/points/delete?wait=true' % (QDRANT, COLL),
                  json={'filter': {'must': [{'key': 'raccolta', 'match': {'value': raccolta}}]}},
                  timeout=120).raise_for_status()
    c = db()
    c.execute('delete from files where raccolta=?', (raccolta,))
    c.commit()
    shutil.rmtree(DERIVED / slug(raccolta), ignore_errors=True)
    print('rimossa', raccolta)


def serve():
    """API di sola ricerca: GET /search?q=...&raccolta=...&k=8 con header Authorization: Bearer <KB_API_TOKEN>.
    Ascolta su KB_API_HOST (default 127.0.0.1): esponila solo su una rete privata o dietro un proxy con TLS."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs
    token = os.environ.get('KB_API_TOKEN', '')
    if len(token) < 24:
        raise SystemExit('imposta KB_API_TOKEN (almeno 24 caratteri casuali) prima di avviare l\'API')
    host, port = os.environ.get('KB_API_HOST', '127.0.0.1'), int(os.environ.get('KB_API_PORT', '8791'))

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.headers.get('Authorization') != 'Bearer ' + token:
                self.send_response(403); self.end_headers(); return
            u = urlparse(self.path)
            if u.path != '/search':
                self.send_response(404); self.end_headers(); return
            q = parse_qs(u.query)
            try:
                res = search(q.get('q', [''])[0], (q.get('raccolta') or [None])[0], int((q.get('k') or ['8'])[0]))
                body, code = json.dumps({'risultati': res}, ensure_ascii=False).encode(), 200
            except Exception as e:
                body, code = json.dumps({'errore': str(e)[:300]}).encode(), 500
            self.send_response(code); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(body)

    print('API di ricerca su http://%s:%d/search' % (host, port))
    ThreadingHTTPServer((host, port), H).serve_forever()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest='cmd', required=True)
    a = sp.add_parser('process'); a.add_argument('folder'); a.add_argument('--raccolta', required=True)
    a = sp.add_parser('search'); a.add_argument('q'); a.add_argument('--raccolta'); a.add_argument('-k', type=int, default=8)
    a = sp.add_parser('remove'); a.add_argument('--raccolta', required=True)
    sp.add_parser('list')
    sp.add_parser('serve')
    x = ap.parse_args()
    if requests is None:
        raise SystemExit('manca il pacchetto requests: pip install -r requirements.txt')
    if x.cmd == 'process':
        process(x.folder, x.raccolta)
    elif x.cmd == 'search':
        print(json.dumps(search(x.q, x.raccolta, x.k), ensure_ascii=False, indent=1))
    elif x.cmd == 'list':
        list_collections()
    elif x.cmd == 'remove':
        remove(x.raccolta)
    elif x.cmd == 'serve':
        serve()


if __name__ == '__main__':
    main()
