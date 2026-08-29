import os, json, sqlite3, hashlib, secrets, mimetypes
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib import request as urlrequest, parse as urlparse
from pathlib import Path
from email.parser import BytesParser
from email.policy import default

BASE = Path(__file__).resolve().parent
STATIC = BASE / 'static'
DB = BASE / 'data.db'

HOST = os.getenv('HOST', '0.0.0.0')
PORT = int(os.getenv('PORT', '5000'))
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'change-me')
SECRET_KEY = os.getenv('SECRET_KEY', 'change-this-secret')
DATABASE_URL = os.getenv('DATABASE_URL', '').strip()
TG_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TG_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
TG_CHANNEL_ID = os.getenv('TELEGRAM_CHANNEL_ID', '')
AUTH_TOKEN = hashlib.sha256(f'{SECRET_KEY}:{ADMIN_PASSWORD}'.encode()).hexdigest()

STATUSES = ['new','contacted','measure','estimate','contract','production','install','done','lost']
ALLOWED = {'.jpg','.jpeg','.png','.webp'}
MAX_FILE = 8 * 1024 * 1024

if DATABASE_URL:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except Exception as e:
        raise RuntimeError('DATABASE_URL задан, но пакет psycopg не установлен') from e


def now(): return datetime.now().isoformat(timespec='seconds')

def is_pg(): return bool(DATABASE_URL)

def db():
    if is_pg():
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def sql(s): return s.replace('?', '%s') if is_pg() else s

def fetchall(query, args=()):
    c=db()
    try:
        cur=c.execute(sql(query), args)
        return [dict(r) for r in cur.fetchall()]
    finally: c.close()

def fetchone(query, args=()):
    c=db()
    try:
        r=c.execute(sql(query), args).fetchone()
        return dict(r) if r else None
    finally: c.close()

def execute(query, args=()):
    c=db()
    try:
        c.execute(sql(query), args); c.commit()
    finally: c.close()

def insert_returning_id(query, args=()):
    c=db()
    try:
        if is_pg():
            cur=c.execute(sql(query) + ' RETURNING id', args); rid=cur.fetchone()['id']
        else:
            cur=c.execute(query, args); rid=cur.lastrowid
        c.commit(); return rid
    finally: c.close()

def init_db():
    c=db()
    try:
        if is_pg():
            c.execute('''CREATE TABLE IF NOT EXISTS leads(
              id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL,
              product TEXT NOT NULL, dimensions TEXT, budget TEXT, city TEXT, comment TEXT,
              source TEXT DEFAULT 'site', status TEXT DEFAULT 'new', photos TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL, followup_due TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS portfolio(
              id BIGSERIAL PRIMARY KEY, title TEXT NOT NULL, description TEXT,
              material TEXT, price TEXT, city TEXT, photos TEXT, tg_caption TEXT,
              vk_caption TEXT, ig_caption TEXT, created_at TEXT NOT NULL, published_tg INTEGER DEFAULT 0)''')
            c.execute('''CREATE TABLE IF NOT EXISTS media(
              id BIGSERIAL PRIMARY KEY, filename TEXT, content_type TEXT, data BYTEA NOT NULL, created_at TEXT NOT NULL)''')
        else:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS leads(
              id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, phone TEXT NOT NULL,
              product TEXT NOT NULL, dimensions TEXT, budget TEXT, city TEXT, comment TEXT,
              source TEXT DEFAULT 'site', status TEXT DEFAULT 'new', photos TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL, followup_due TEXT);
            CREATE TABLE IF NOT EXISTS portfolio(
              id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, description TEXT,
              material TEXT, price TEXT, city TEXT, photos TEXT, tg_caption TEXT,
              vk_caption TEXT, ig_caption TEXT, created_at TEXT NOT NULL, published_tg INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS media(
              id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT, content_type TEXT, data BLOB NOT NULL, created_at TEXT NOT NULL);
            ''')
        c.commit()
    finally: c.close()


def telegram(method, payload):
    if not TG_BOT_TOKEN: return False, 'Telegram не настроен'
    try:
        data=urlparse.urlencode(payload).encode()
        with urlrequest.urlopen(f'https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}', data=data, timeout=10) as r:
            return r.status==200, r.read().decode(errors='ignore')
    except Exception as e: return False, str(e)


def captions(title, description='', material='', price='', city=''):
    bits=[title.strip()]
    if description.strip(): bits += ['', description.strip()]
    if material.strip(): bits += ['', f'Материал: {material.strip()}']
    if price.strip(): bits += [f'Стоимость проекта: {price.strip()}']
    if city.strip(): bits += [f'Город: {city.strip()}']
    bits += ['', 'Хотите похожий проект? Оставьте заявку на замер.']
    tg='\n'.join(bits)
    vk=f"{title.strip()}. {description.strip()}".strip()
    if material.strip(): vk += f" Материал: {material.strip()}."
    if price.strip(): vk += f" Стоимость: {price.strip()}."
    vk += ' Напишите нам — рассчитаем проект и подберём удобное время замера.'
    ig=f"{title.strip()}\n\n{description.strip()}".strip()
    if material.strip(): ig += f"\nМатериал: {material.strip()}"
    if price.strip(): ig += f"\nСтоимость: {price.strip()}"
    ig += '\n\nЗапись на замер — по ссылке в профиле.\n\n#кухняназаказ #мебельназаказ #дизайнинтерьера'
    return tg,vk,ig


def parse_multipart(headers, body):
    ctype=headers.get('Content-Type','')
    raw=(f'Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n').encode()+body
    msg=BytesParser(policy=default).parsebytes(raw)
    fields={}; files=[]
    if not msg.is_multipart(): return fields,files
    for part in msg.iter_parts():
        name=part.get_param('name', header='content-disposition')
        filename=part.get_filename()
        payload=part.get_payload(decode=True) or b''
        if filename:
            ext=Path(filename).suffix.lower()
            if ext in ALLOWED and len(payload)<=MAX_FILE:
                files.append({'filename': filename, 'content_type': part.get_content_type() or mimetypes.guess_type(filename)[0] or 'application/octet-stream', 'data': payload})
        elif name:
            fields[name]=payload.decode('utf-8', errors='replace')
    return fields,files


def save_files(files):
    ids=[]
    for f in files:
        rid=insert_returning_id('INSERT INTO media(filename,content_type,data,created_at) VALUES(?,?,?,?)',
                                (f['filename'],f['content_type'],f['data'],now()))
        ids.append(str(rid))
    return ids


class App(BaseHTTPRequestHandler):
    server_version='MebelFlow/1.1'

    def log_message(self, fmt, *args): print('[web]', fmt%args)
    def send_bytes(self, data, ctype='text/html; charset=utf-8', status=200, extra=None):
        self.send_response(status); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store')
        if extra:
            for k,v in extra.items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(data)
    def send_json(self, obj, status=200, extra=None): self.send_bytes(json.dumps(obj,ensure_ascii=False).encode(), 'application/json; charset=utf-8', status, extra)
    def body(self):
        n=int(self.headers.get('Content-Length','0')); return self.rfile.read(n)
    def authed(self):
        cookie=self.headers.get('Cookie','')
        return f'mf_auth={AUTH_TOKEN}' in cookie
    def need_auth(self):
        if self.authed(): return True
        self.send_json({'ok':False,'error':'auth'},401); return False
    def serve(self, path, ctype=None):
        p=(BASE/path).resolve()
        if BASE not in p.parents and p!=BASE: return self.send_bytes(b'Forbidden', status=403)
        if not p.exists() or not p.is_file(): return self.send_bytes(b'Not found',status=404)
        return self.send_bytes(p.read_bytes(), ctype or mimetypes.guess_type(str(p))[0] or 'application/octet-stream')

    def do_GET(self):
        path=self.path.split('?',1)[0]
        if path=='/': return self.serve('static/index.html','text/html; charset=utf-8')
        if path=='/crm': return self.serve('static/crm.html','text/html; charset=utf-8')
        if path=='/content': return self.serve('static/content.html','text/html; charset=utf-8')
        if path=='/login': return self.serve('static/login.html','text/html; charset=utf-8')
        if path.startswith('/static/'): return self.serve(path.lstrip('/'))
        if path.startswith('/uploads/'):
            try: mid=int(path.rsplit('/',1)[-1])
            except: return self.send_bytes(b'Not found',status=404)
            row=fetchone('SELECT content_type,data FROM media WHERE id=?',(mid,))
            if not row: return self.send_bytes(b'Not found',status=404)
            data=bytes(row['data']) if not isinstance(row['data'],bytes) else row['data']
            return self.send_bytes(data,row.get('content_type') or 'application/octet-stream')
        if path=='/api/health': return self.send_json({'ok':True,'time':now(),'database':'postgres' if is_pg() else 'sqlite'})
        if path=='/api/me': return self.send_json({'authenticated':self.authed()})
        if path=='/api/leads':
            if not self.need_auth(): return
            return self.send_json({'ok':True,'leads':fetchall('SELECT * FROM leads ORDER BY id DESC')})
        if path=='/api/portfolio':
            return self.send_json({'ok':True,'items':fetchall('SELECT * FROM portfolio ORDER BY id DESC')})
        if path=='/api/content':
            if not self.need_auth(): return
            return self.send_json({'ok':True,'items':fetchall('SELECT * FROM portfolio ORDER BY id DESC'),'telegram_ready':bool(TG_BOT_TOKEN and TG_CHANNEL_ID)})
        return self.send_bytes(b'Not found',status=404)

    def do_POST(self):
        path=self.path.split('?',1)[0]
        if path=='/api/login':
            try: data=json.loads(self.body() or b'{}')
            except: data={}
            if secrets.compare_digest(str(data.get('password','')), ADMIN_PASSWORD):
                return self.send_json({'ok':True}, extra={'Set-Cookie':f'mf_auth={AUTH_TOKEN}; Path=/; HttpOnly; SameSite=Lax; Secure'})
            return self.send_json({'ok':False,'error':'Неверный пароль'},403)
        if path=='/api/logout':
            return self.send_json({'ok':True}, extra={'Set-Cookie':'mf_auth=; Path=/; Max-Age=0; Secure'})
        if path=='/api/lead':
            body=self.body(); fields,files=parse_multipart(self.headers,body)
            for k in ('name','phone','product'):
                if not fields.get(k,'').strip(): return self.send_json({'ok':False,'error':'Заполните имя, телефон и тип мебели'},400)
            media_ids=save_files(files)
            t=now(); due=(datetime.now()+timedelta(days=2)).isoformat(timespec='seconds')
            lid=insert_returning_id('''INSERT INTO leads(name,phone,product,dimensions,budget,city,comment,source,status,photos,created_at,updated_at,followup_due) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',(
                fields['name'].strip(),fields['phone'].strip(),fields['product'].strip(),fields.get('dimensions','').strip(),fields.get('budget','').strip(),fields.get('city','').strip(),fields.get('comment','').strip(),fields.get('source','site').strip() or 'site','new',','.join(media_ids),t,t,due))
            lead=fetchone('SELECT * FROM leads WHERE id=?',(lid,))
            if TG_CHAT_ID:
                text=f"🔥 Новая заявка\n\nКлиент: {lead['name']}\nТелефон: {lead['phone']}\nЧто нужно: {lead['product']}\nРазмеры: {lead['dimensions'] or 'не указаны'}\nБюджет: {lead['budget'] or 'не указан'}\nГород: {lead['city'] or 'не указан'}\nКомментарий: {lead['comment'] or '—'}"
                telegram('sendMessage',{'chat_id':TG_CHAT_ID,'text':text})
            return self.send_json({'ok':True,'lead_id':lid})
        if path=='/api/content/add':
            if not self.need_auth(): return
            body=self.body(); fields,files=parse_multipart(self.headers,body)
            title=fields.get('title','').strip()
            if not title: return self.send_json({'ok':False,'error':'Нужно название'},400)
            media_ids=save_files(files)
            tg,vk,ig=captions(title,fields.get('description',''),fields.get('material',''),fields.get('price',''),fields.get('city',''))
            iid=insert_returning_id('''INSERT INTO portfolio(title,description,material,price,city,photos,tg_caption,vk_caption,ig_caption,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',(
                title,fields.get('description','').strip(),fields.get('material','').strip(),fields.get('price','').strip(),fields.get('city','').strip(),','.join(media_ids),tg,vk,ig,now()))
            return self.send_json({'ok':True,'id':iid})
        if path.startswith('/api/lead/') and path.endswith('/status'):
            if not self.need_auth(): return
            try: lid=int(path.split('/')[3]); data=json.loads(self.body() or b'{}'); status=data.get('status')
            except: return self.send_json({'ok':False},400)
            if status not in STATUSES: return self.send_json({'ok':False,'error':'bad status'},400)
            due=None if status in ('done','lost') else (datetime.now()+timedelta(days=2)).isoformat(timespec='seconds')
            execute('UPDATE leads SET status=?,updated_at=?,followup_due=? WHERE id=?',(status,now(),due,lid)); return self.send_json({'ok':True})
        if path.startswith('/api/content/') and path.endswith('/publish-telegram'):
            if not self.need_auth(): return
            try: iid=int(path.split('/')[3])
            except: return self.send_json({'ok':False},400)
            if not TG_CHANNEL_ID: return self.send_json({'ok':False,'error':'TELEGRAM_CHANNEL_ID не настроен'},400)
            row=fetchone('SELECT * FROM portfolio WHERE id=?',(iid,))
            if not row: return self.send_json({'ok':False,'error':'not found'},404)
            ok,detail=telegram('sendMessage',{'chat_id':TG_CHANNEL_ID,'text':row['tg_caption']})
            if ok:
                execute('UPDATE portfolio SET published_tg=1 WHERE id=?',(iid,)); return self.send_json({'ok':True})
            return self.send_json({'ok':False,'error':detail[:200]},500)
        if path.startswith('/api/lead/') and path.endswith('/delete'):
            if not self.need_auth(): return
            try: lid=int(path.split('/')[3])
            except: return self.send_json({'ok':False},400)
            execute('DELETE FROM leads WHERE id=?',(lid,)); return self.send_json({'ok':True})
        if path.startswith('/api/content/') and path.endswith('/delete'):
            if not self.need_auth(): return
            try: iid=int(path.split('/')[3])
            except: return self.send_json({'ok':False},400)
            execute('DELETE FROM portfolio WHERE id=?',(iid,)); return self.send_json({'ok':True})
        return self.send_json({'ok':False,'error':'not found'},404)

if __name__=='__main__':
    init_db(); print(f'MEBEL FLOW: http://127.0.0.1:{PORT}'); print('CRM: /crm  |  Content: /content')
    ThreadingHTTPServer((HOST,PORT),App).serve_forever()
