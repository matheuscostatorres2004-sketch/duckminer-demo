"""DuckMiner local multiplayer server. Python 3.11+, standard library only."""
import argparse
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import random
import re
import secrets
import sqlite3
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'data' / 'duckminer.sqlite3'
UNIT = 1_000_000
DAY = 86400
SLOTS = 4
MODELS = json.loads((ROOT / 'catalog.json').read_text(encoding='utf-8')) if (ROOT / 'catalog.json').exists() else []
MISSION = {
    'daily': [('machine', 1, 75), ('battery', 1, 50)],
    'weekly': [('machine', 3, 250), ('battery', 3, 200), ('crate', 2, 250)],
}
TZ = timezone(timedelta(hours=-3))


class Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect():
    db = sqlite3.connect(DB, timeout=30, factory=Connection)
    db.row_factory = sqlite3.Row
    return db


def initialize():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, salt BLOB NOT NULL,
          password BLOB NOT NULL, state TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(
          token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS pool(id INTEGER PRIMARY KEY CHECK(id=1), last INTEGER, start INTEGER);
        CREATE TABLE IF NOT EXISTS payouts(
          round_end INTEGER, user_id INTEGER, amount INTEGER,
          PRIMARY KEY(round_end,user_id));
        CREATE TABLE IF NOT EXISTS requests(
          user_id INTEGER, key TEXT, PRIMARY KEY(user_id,key));
        CREATE TABLE IF NOT EXISTS attempts(ip TEXT PRIMARY KEY, start INTEGER, count INTEGER);
        ''')
        now = int(time.time())
        db.execute('INSERT OR IGNORE INTO pool VALUES(1,?,?)', (now, now - now % 60))


def fresh():
    return dict(coins=1000 * UNIT, mined=0, racks=[[None]*4 for _ in range(3)],
                inventory=[], batterySeconds=DAY, batteryReserve=0, batteriesUsed=1,
                seconds=0, cooling=0, claimed=[], history=[], dailyLast='', dailyStreak=0,
                crateCount=0, timedMissions={}, work=0, lastReward=0)


def event(state, text):
    state['history'].insert(0, datetime.now(TZ).strftime('%H:%M:%S') + ' — ' + text)
    state['history'] = state['history'][:45]


def machines(state):
    return [m for rack in state['racks'] for m in rack if m]


def power(state):
    return sum(m['power'] for m in machines(state))


def advance(db, now):
    """Settle global rounds and battery usage in the caller's write transaction."""
    pool = db.execute('SELECT * FROM pool WHERE id=1').fetchone()
    last, start = pool['last'], pool['start']
    if now <= last:
        return
    states = {r['id']: json.loads(r['state']) for r in db.execute('SELECT id,state FROM users')}
    while last < now:
        # Empty pools can skip whole intervals after downtime.
        if not any(s['batterySeconds'] > 0 and power(s) for s in states.values()) and not any(s['work'] for s in states.values()):
            start += max(0, (now - start) // 60) * 60
            last = now
            break
        expirations = [last + s['batterySeconds'] for s in states.values()
                       if s['batterySeconds'] > 0 and power(s)]
        end = min([now, start + 60] + expirations)
        duration = end - last
        for s in states.values():
            if power(s) > 0:
                active = min(duration, s['batterySeconds'])
                s['work'] += power(s) * active
                s['seconds'] += active
                s['batterySeconds'] -= active
                # A depleted battery cancels unfinished personal contribution.
                if s['batterySeconds'] == 0 and end < start + 60:
                    s['work'] = 0
        last = end
        if end == start + 60:
            total = sum(s['work'] for s in states.values())
            shares = {uid: (10 * UNIT * s['work']) // total for uid, s in states.items() if s['work']} if total else {}
            if shares:
                # Distribute rounding remainder exactly, without creating extra DCK.
                ordered = sorted(shares, key=lambda uid: (-(10 * UNIT * states[uid]['work'] % total), uid))
                for uid in ordered[:10 * UNIT - sum(shares.values())]:
                    shares[uid] += 1
            for uid, amount in shares.items():
                db.execute('INSERT INTO payouts VALUES(?,?,?)', (end, uid, amount))
                s = states[uid]
                s['coins'] += amount
                s['mined'] += amount
                s['lastReward'] = amount
                event(s, f'Pool DCK: +{amount / UNIT:.6f} DCK')
            for s in states.values():
                s['work'] = 0
            start = end
    for uid, s in states.items():
        db.execute('UPDATE users SET state=? WHERE id=?', (json.dumps(s), uid))
    db.execute('UPDATE pool SET last=?,start=? WHERE id=1', (last, start))


def progress(s, period):
    today = datetime.now(TZ).date()
    if period == 'weekly':
        today -= timedelta(days=today.weekday())
    key = today.isoformat()
    if s['timedMissions'].get(period, {}).get('key') != key:
        s['timedMissions'][period] = dict(key=key, counts={}, claimed={})
    return s['timedMissions'][period]


def count(s, action):
    for period in MISSION:
        counts = progress(s, period)['counts']
        counts[action] = counts.get(action, 0) + 1


def integer(data, key, low=0, high=10**9):
    value = data.get(key)
    if type(value) is not int or not low <= value <= high:
        raise ValueError('Escolha inválida.')
    return value


def spend(s, cost):
    if s['coins'] < cost * UNIT:
        raise ValueError('Saldo insuficiente.')
    s['coins'] -= cost * UNIT


def new_machine(index):
    return {**MODELS[index], 'id': secrets.randbelow(2**48), 'level': 0,
            'baseCost': MODELS[index]['cost']}


def slot(s, data, prefix=''):
    return (integer(data, prefix+'r', high=len(s['racks'])-1), integer(data, prefix+'s', high=3))


def act(s, data):
    name = data.get('action')
    message = 'Operação concluída.'
    if name == 'settings':
        display = data.get('displayName')
        zoom = data.get('zoom')
        if not isinstance(display, str) or not 2 <= len(display.strip()) <= 24 or any(ord(c) < 32 for c in display):
            raise ValueError('Nome de exibição inválido.')
        if type(zoom) is not int or zoom not in (75, 90, 100, 110, 125):
            raise ValueError('Zoom inválido.')
        s['settings'] = {'displayName': display.strip(), 'zoom': zoom}
        message = 'Configurações salvas.'
    elif name == 'buy':
        index = integer(data, 'index', high=len(MODELS)-1)
        spend(s, MODELS[index]['cost'])
        s['inventory'].append(new_machine(index))
        count(s, 'machine')
        message = 'Compra confirmada: ' + MODELS[index]['name']
    elif name == 'install':
        r, pos = slot(s, data)
        machine = next((m for m in s['inventory'] if m['id'] == data.get('id')), None)
        if not machine or s['racks'][r][pos]:
            raise ValueError('Máquina indisponível ou espaço ocupado.')
        s['inventory'].remove(machine)
        s['racks'][r][pos] = machine
    elif name == 'remove':
        r, pos = slot(s, data)
        if not s['racks'][r][pos]:
            raise ValueError('Espaço já está vazio.')
        s['inventory'].append(s['racks'][r][pos])
        s['racks'][r][pos] = None
    elif name == 'move':
        r, pos = slot(s, data)
        r2, pos2 = slot(s, data, 'to')
        if not s['racks'][r][pos]:
            raise ValueError('Máquina indisponível.')
        s['racks'][r][pos], s['racks'][r2][pos2] = s['racks'][r2][pos2], s['racks'][r][pos]
    elif name == 'battery':
        spend(s, 150)
        s['batteryReserve'] += 1
        s['batteriesUsed'] += 1
        message = 'Compra confirmada: bateria · 150 DCK'
    elif name == 'recharge':
        if s['batteryReserve'] < 1 or s['batterySeconds'] + DAY > 7 * DAY:
            raise ValueError('Sem bateria no estoque ou carga acima do limite de sete dias.')
        s['batteryReserve'] -= 1
        s['batterySeconds'] += DAY
        count(s, 'battery')
    elif name == 'clear_battery':
        s['batterySeconds'] = 0
        s['work'] = 0
    elif name == 'clear_balance':
        s['coins'] = 0
    elif name == 'expand':
        if len(s['racks']) >= 8:
            raise ValueError('Limite de oito racks.')
        spend(s, 2500 + max(0, len(s['racks']) - 3)*2000)
        s['racks'].append([None]*4)
        message = 'Compra confirmada: novo rack'
    elif name == 'cooling':
        spend(s, 1200 + s['cooling']*900)
        s['cooling'] += 1
        message = 'Compra confirmada: refrigeração'
    elif name == 'upgrade':
        m = next((m for m in machines(s) if m['id'] == data.get('id')), None)
        if not m:
            raise ValueError('Instale a máquina antes de melhorar.')
        cost = math.floor(m['baseCost']*(.55 + m['level']*.45) + .5)
        spend(s, cost)
        m['level'] += 1
        m['power'] = math.floor(m['power']*1.25 + .5)
        m['watts'] = max(40, math.floor(m['watts']*.96 + .5))
        message = 'Compra confirmada: melhoria'
    elif name == 'daily':
        today = datetime.now(TZ).date()
        if s['dailyLast'] == today.isoformat():
            raise ValueError('Recompensa de hoje já resgatada.')
        yesterday = (today - timedelta(days=1)).isoformat()
        s['dailyStreak'] = s['dailyStreak']+1 if s['dailyLast'] == yesterday else 1
        s['dailyLast'] = today.isoformat()
        s['coins'] += (200 + min(6, s['dailyStreak']-1)*100) * UNIT
    elif name == 'claim':
        index = integer(data, 'index', high=6)
        requirements = [len(machines(s)) >= 1, len(machines(s)) >= 4, power(s) >= 1000,
                        power(s) >= 5000, s['mined'] >= 100*UNIT, s['seconds'] >= 36000,
                        len(s['racks']) > 3]
        if index in s['claimed'] or not requirements[index]:
            raise ValueError('Conquista indisponível.')
        s['claimed'].append(index)
        s['coins'] += [300,800,700,1800,500,1200,1000][index]*UNIT
    elif name == 'mission':
        period = data.get('period')
        if period not in MISSION:
            raise ValueError('Período inválido.')
        action, target, reward = MISSION[period][integer(data, 'index', high=len(MISSION[period])-1)]
        p = progress(s, period)
        if p['claimed'].get(action) or p['counts'].get(action, 0) < target:
            raise ValueError('Missão indisponível.')
        p['claimed'][action] = True
        s['coins'] += reward*UNIT
    elif name == 'crate':
        premium = data.get('premium') is True
        spend(s, 2000 if premium else 250)
        rarity = random.choices(range(4), weights=[30,45,20,5] if premium else [75,20,4,1])[0]
        pools = [list(range(4)), [2,3,4], [5], [6]]
        m = new_machine(random.choice(pools[rarity]))
        m['rarity'] = ['Comum','Rara','Épica','Lendária'][rarity]
        s['inventory'].append(m)
        s['crateCount'] += 1
        count(s, 'crate')
        message = 'Caixa aberta: ' + m['name']
    else:
        raise ValueError('Ação indisponível.')
    event(s, message)
    return message


def snapshot(db, uid):
    users = list(db.execute('SELECT id,name,state FROM users'))
    s = next(json.loads(u['state']) for u in users if u['id'] == uid)
    for period in MISSION:
        progress(s, period)
    active = lambda state: power(state) if state['batterySeconds'] > 0 else 0
    network = sum(active(json.loads(u['state'])) for u in users)
    elapsed = db.execute('SELECT last-start FROM pool WHERE id=1').fetchone()[0]
    return {'game': {**s, 'balance': s['coins']/UNIT, 'total': s['mined']/UNIT,
                     'running': bool(active(s)), 'metrics': [], 'poolSeconds': elapsed,
                     'poolLastReward': s['lastReward']/UNIT},
            'pool': {'power': network, 'elapsed': elapsed, 'reward': 10},
            'ranking': sorted([{'name': json.loads(u['state']).get('settings', {}).get('displayName', u['name']), 'power': active(json.loads(u['state'])),
                                'mine': u['id'] == uid} for u in users], key=lambda u: -u['power']),
            'user': next(u['name'] for u in users if u['id'] == uid)}


class Handler(BaseHTTPRequestHandler):
    server_version = 'DuckMiner'

    def log_message(self, *_):
        pass

    def reply(self, code, payload, cookie=None):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/api/state':
            return self.api()
        files = {'/': ('index.html', 'text/html; charset=utf-8'),
                 '/online.js': ('online.js', 'application/javascript; charset=utf-8')}
        if self.path not in files:
            return self.reply(404, {'error': 'Página indisponível.'})
        name, mime = files[self.path]
        body = (ROOT / 'public' / name).read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        origin = self.headers.get('Origin')
        if origin and origin not in ('http://' + self.headers.get('Host', ''), 'https://' + self.headers.get('Host', '')):
            return self.reply(403, {'error': 'Origem inválida.'})
        self.api()

    def api(self):
        db = connect()
        try:
            data = {}
            if self.command == 'POST':
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= 4096:
                    raise ValueError('Pedido inválido.')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError('Pedido inválido.')
            now = int(time.time())
            db.execute('BEGIN IMMEDIATE')
            advance(db, now)
            if self.path in ('/api/register', '/api/login') and self.command == 'POST':
                name, password = data.get('name', ''), data.get('password', '')
                if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_]{3,24}', name) or not isinstance(password, str) or not 10 <= len(password) <= 128:
                    raise ValueError('Use um nome com 3–24 letras, números ou _ e senha com 10–128 caracteres.')
                name = name.lower()
                attempt = db.execute('SELECT * FROM attempts WHERE ip=?', (self.client_address[0],)).fetchone()
                attempts = attempt['count'] if attempt and now-attempt['start'] < 600 else 0
                start = attempt['start'] if attempts else now
                if attempts >= 30:
                    db.commit()
                    return self.reply(429, {'error': 'Muitas tentativas. Aguarde dez minutos.'})
                db.execute('INSERT OR REPLACE INTO attempts VALUES(?,?,?)', (self.client_address[0], start, attempts+1))
                user = db.execute('SELECT * FROM users WHERE name=?', (name,)).fetchone()
                registering = self.path == '/api/register'
                salt = user['salt'] if user else secrets.token_bytes(16)
                hashed = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
                if (registering and user) or (not registering and (not user or not hmac.compare_digest(hashed, user['password']))):
                    db.commit()
                    return self.reply(400, {'error': 'Nome indisponível ou credenciais inválidas.'})
                if registering:
                    uid = db.execute('INSERT INTO users(name,salt,password,state) VALUES(?,?,?,?)', (name, salt, hashed, json.dumps(fresh()))).lastrowid
                else:
                    uid = user['id']
                token = secrets.token_urlsafe(32)
                db.execute('INSERT INTO sessions VALUES(?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), uid, now+7*DAY))
                db.execute('DELETE FROM sessions WHERE expires<=?', (now,))
                result = snapshot(db, uid)
                db.commit()
                secure = '; Secure' if os.environ.get('DUCKMINER_SECURE_COOKIE') == '1' else ''
                return self.reply(200, result, f'duck_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={7*DAY}{secure}')
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            token = cookie['duck_session'].value if 'duck_session' in cookie else ''
            digest = hashlib.sha256(token.encode()).hexdigest()
            session = db.execute('SELECT user_id FROM sessions WHERE token=? AND expires>?', (digest, now)).fetchone()
            if not session:
                db.commit()
                return self.reply(401, {'error': 'Entre na sua conta.'})
            uid = session['user_id']
            message = None
            if self.path == '/api/logout' and self.command == 'POST':
                db.execute('DELETE FROM sessions WHERE token=?', (digest,))
                db.commit()
                return self.reply(200, {}, 'duck_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
            if self.path == '/api/action' and self.command == 'POST':
                key = self.headers.get('Idempotency-Key', '')
                if not re.fullmatch(r'[a-zA-Z0-9-]{16,80}', key):
                    raise ValueError('Identificador da operação inválido.')
                exists = db.execute('SELECT 1 FROM requests WHERE user_id=? AND key=?', (uid, key)).fetchone()
                if not exists:
                    s = json.loads(db.execute('SELECT state FROM users WHERE id=?', (uid,)).fetchone()[0])
                    message = act(s, data)
                    db.execute('UPDATE users SET state=? WHERE id=?', (json.dumps(s), uid))
                    db.execute('INSERT INTO requests VALUES(?,?)', (uid, key))
            elif self.path != '/api/state' or self.command != 'GET':
                raise ValueError('Pedido indisponível.')
            result = snapshot(db, uid)
            result['message'] = message
            db.commit()
            self.reply(200, result)
        except (ValueError, TypeError, KeyError, IndexError):
            db.rollback()
            self.reply(400, {'error': 'Operação inválida ou indisponível. Confira saldo, carga e requisitos.'})
        except Exception:
            db.rollback()
            self.reply(500, {'error': 'Não foi possível concluir. Tente novamente.'})
        finally:
            db.close()


def worker(stop):
    while not stop.wait(1):
        try:
            with connect() as db:
                db.execute('BEGIN IMMEDIATE')
                advance(db, int(time.time()))
        except Exception as error:
            print('Falha ao processar pool:', type(error).__name__, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--open', action='store_true', help='Abrir o jogo no navegador')
    args = parser.parse_args()
    initialize()
    try:
        http = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as error:
        print(f'Nao foi possivel abrir a porta {args.port}: {error}', flush=True)
        print('Feche outro DuckMiner aberto ou use: python server.py --port 8001 --open', flush=True)
        raise SystemExit(1)
    stop = threading.Event()
    thread = threading.Thread(target=worker, args=(stop,), daemon=True)
    thread.start()
    print(f'DuckMiner: http://{args.host}:{args.port}', flush=True)
    if args.open:
        threading.Thread(target=webbrowser.open, args=(f'http://127.0.0.1:{args.port}',), daemon=True).start()
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        stop.set()
    finally:
        http.server_close()
