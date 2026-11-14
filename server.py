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
import btcpay
from postgres_db import connect_postgres
import threading
import time
import webbrowser
import btc
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socket import timeout as SocketTimeout
from urllib.parse import urlsplit, parse_qs

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get('DUCKMINER_DB_PATH', str(ROOT / 'data' / 'duckminer.sqlite3')))
UNIT = 1_000_000
POOL_SECONDS = 600
POOL_REWARD_UNITS = 50_000
DEFAULT_SPLIT = dict(DCK=100, BTC=0, DOGE=0)
DAY = 86400
SLOTS = 4
MODELS = json.loads((ROOT / 'catalog.json').read_text(encoding='utf-8')) if (ROOT / 'catalog.json').exists() else []
MISSION = {
    'daily': [('machine', 1, 75), ('install', 1, 50)],
    'weekly': [('machine', 3, 250), ('install', 3, 200), ('crate', 2, 250)],
}
TZ = timezone(timedelta(hours=-3))
SESSION_SECONDS = DAY
HASH_SLOTS = threading.BoundedSemaphore(3)


class Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect():
    if os.environ.get("DATABASE_URL"):
        return connect_postgres(os.environ["DATABASE_URL"])
    db = sqlite3.connect(DB, timeout=30, factory=Connection)
    db.row_factory = sqlite3.Row
    return db


def initialize():
    if not os.environ.get("DATABASE_URL"):
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
        CREATE TABLE IF NOT EXISTS limits(key TEXT PRIMARY KEY, start INTEGER, count INTEGER);
        CREATE TABLE IF NOT EXISTS admin_audit(
          id INTEGER PRIMARY KEY, at INTEGER, actor INTEGER, target INTEGER, action TEXT);
        CREATE TABLE IF NOT EXISTS deposits(
          id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created INTEGER NOT NULL,
          amount INTEGER NOT NULL, rate TEXT NOT NULL, credit INTEGER NOT NULL,
          address TEXT UNIQUE, txid TEXT, vout INTEGER, status TEXT NOT NULL,
          approved_at INTEGER, confirmations INTEGER);
        CREATE UNIQUE INDEX IF NOT EXISTS deposit_outpoint ON deposits(txid,vout) WHERE status='approved';
        ''')
        columns = {row['name'] for row in db.execute('PRAGMA table_info(users)')}
        if 'blocked' not in columns:
            db.execute('ALTER TABLE users ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0')
        if 'created_at' not in columns:
            db.execute('ALTER TABLE users ADD COLUMN created_at INTEGER NOT NULL DEFAULT 0')
        if 'email' not in columns:
            db.execute('ALTER TABLE users ADD COLUMN email TEXT')
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS user_email ON users(email) WHERE email IS NOT NULL')
        columns = {row['name'] for row in db.execute('PRAGMA table_info(sessions)')}
        if 'csrf' not in columns:
            db.execute("ALTER TABLE sessions ADD COLUMN csrf TEXT NOT NULL DEFAULT ''")
        if 'admin_until' not in columns:
            db.execute('ALTER TABLE sessions ADD COLUMN admin_until INTEGER NOT NULL DEFAULT 0')
        if 'last_seen' not in columns:
            db.execute('ALTER TABLE sessions ADD COLUMN last_seen INTEGER NOT NULL DEFAULT 0')
        columns = {row['name'] for row in db.execute('PRAGMA table_info(requests)')}
        if 'body_hash' not in columns:
            db.execute("ALTER TABLE requests ADD COLUMN body_hash TEXT NOT NULL DEFAULT ''")
        db.execute("CREATE TABLE IF NOT EXISTS btcpay_deposits (invoice_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, sats INTEGER NOT NULL, credited INTEGER NOT NULL DEFAULT 0)")
        btc.initialize(db)
        now = int(time.time())
        db.execute('INSERT OR IGNORE INTO pool VALUES(1,?,?)', (now, now - now % POOL_SECONDS))


def fresh():
    return dict(coins=0, mined=0, racks=[[None]*4 for _ in range(3)],
                inventory=[], rackStock=0, rackPositions=[0, 1, 2],
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
    """Settle global rounds in the caller's write transaction."""
    pool = db.execute('SELECT * FROM pool WHERE id=1').fetchone()
    last, start = pool['last'], pool['start']
    if now <= last:
        return
    states = {r['id']: json.loads(r['state']) for r in db.execute('SELECT id,state FROM users WHERE blocked=0')}
    while last < now:
        # Empty pools can skip whole intervals after downtime.
        if not any(power(s) for s in states.values()) and not any(s['work'] or any(s.get('poolWork', {}).values()) for s in states.values()):
            start += max(0, (now - start) // POOL_SECONDS) * POOL_SECONDS
            last = now
            break
        end = min(now, start + POOL_SECONDS)
        duration = end - last
        for s in states.values():
            installed_power = power(s)
            if installed_power:
                work = s.setdefault('poolWork', {})
                for currency, percent in s.get('poolSplit', DEFAULT_SPLIT).items():
                    work[currency] = work.get(currency, 0) + installed_power * duration * percent
                s['seconds'] += duration
        last = end
        if end == start + POOL_SECONDS:
            for currency in DEFAULT_SPLIT:
                work = {uid: state.get('poolWork', {}).get(currency, 0)
                        + (state['work'] * 100 if currency == 'DCK' else 0)
                        for uid, state in states.items()}
                total = sum(work.values())
                shares = {uid: (POOL_REWARD_UNITS * value) // total for uid, value in work.items() if value} if total else {}
                if shares:
                    # Distribute rounding remainder exactly, without creating extra DCK.
                    ordered = sorted(shares, key=lambda uid: (-(POOL_REWARD_UNITS * work[uid] % total), uid))
                    for uid in ordered[:POOL_REWARD_UNITS - sum(shares.values())]:
                        shares[uid] += 1
                for uid, amount in shares.items():
                    state = states[uid]
                    if currency == 'DCK':
                        db.execute('INSERT INTO payouts VALUES(?,?,?)', (end, uid, amount))
                        state['coins'] += amount
                        state['mined'] += amount
                        state['lastReward'] = amount
                    else:
                        wallet = state.setdefault('miningWallet', {})
                        wallet[currency] = wallet.get(currency, 0) + amount
            for state in states.values():
                state['work'] = 0
                state['poolWork'] = {}
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
    units = int(cost * UNIT)
    if s['coins'] < units:
        raise ValueError('Saldo insuficiente.')
    s['coins'] -= units


def new_machine(index):
    return {**MODELS[index], 'id': secrets.randbelow(2**48), 'level': 0,
            'baseCost': MODELS[index]['cost'], 'rarity': 'Basic'}


def slot(s, data, prefix=''):
    rack = integer(data, prefix+'r', high=len(s['racks'])-1)
    position = integer(data, prefix+'s', high=len(s['racks'][rack])-1)
    return rack, position


def act(s, data):
    name = data.get('action')
    if name in ('daily', 'claim', 'mission', 'crate'):
        raise ValueError('Ação desativada.')
    message = 'Operação concluída.'
    if name == 'convert_mined_balance':
        currency = data.get('currency')
        if currency not in ('BTC', 'DOGE'):
            raise ValueError('Moeda inválida.')
        wallet = s.setdefault('miningWallet', {})
        credit = wallet.get(currency, 0)
        if credit <= 0:
            raise ValueError('Saldo minerado insuficiente.')
        wallet[currency] = 0
        s['coins'] += credit
        message = f'Conversão de mineração concluída: +{credit / UNIT:.6f} DCK.'
    elif name == 'convert_btc_balance':
        cfg = btc.config()
        if not cfg['enabled']:
            raise ValueError('Conversão BTC indisponível.')
        credit = data.get('dckUnits')
        if type(credit) is not int or not 0 < credit <= 1000000 * UNIT or data.get('rate') != cfg['rate']:
            raise ValueError('Quantidade ou cotação inválida.')
        numerator, denominator = Decimal(cfg['rate']).as_integer_ratio()
        needed = credit * btc.SATOSHIS * denominator
        divisor = UNIT * numerator
        sats = (needed + divisor - 1) // divisor
        if sats > s.get('btcDepositedSats', 0):
            raise ValueError('Saldo BTC depositado insuficiente.')
        s['btcDepositedSats'] -= sats
        s['coins'] += credit
        message = f'Conversão concluída: +{credit / UNIT:.6f} DCK.'
    elif name == 'pool_split':
        split = data.get('split')
        if (not isinstance(split, dict) or set(split) != set(DEFAULT_SPLIT)
                or any(type(v) is not int or not 0 <= v <= 100 for v in split.values())
                or sum(split.values()) > 100):
            raise ValueError('Divisão inválida.')
        s['poolSplit'] = split
        message = 'Percentuais salvos.'
    elif name == 'settings':
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
        for period in MISSION:
            p = progress(s, period)
            installed = p.setdefault('installedMachines', [])
            if machine['id'] not in installed:
                installed.append(machine['id'])
                p['counts']['install'] = len(installed)
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
    elif name == 'clear_balance':
        s['coins'] = 0
    elif name == 'expand':
        stock = s.get('rackStock', 0)
        owned = len(s['racks']) + stock
        if owned >= 12:
            raise ValueError('Limite de 12 racks, incluindo o estoque.')
        size = data.get('size', SLOTS)
        if type(size) is not int or size != SLOTS:
            raise ValueError('Tamanho de rack inválido.')
        cost = Decimal('0.05')
        spend(s, cost)
        s['rackStock'] = stock + 1
        message = 'Rack enviado ao estoque · 0,05 DCK'
    elif name == 'install_rack':
        position = integer(data, 'position', high=11)
        positions = s.setdefault('rackPositions', list(range(len(s['racks']))))
        if position in positions or len(s['racks']) >= 12 or s.get('rackStock', 0) < 1:
            raise ValueError('Lugar ocupado ou sem rack no estoque.')
        saved_sizes = s.get('returnedRackSizes', [])
        size = saved_sizes.pop(0) if saved_sizes else SLOTS
        s['racks'].append([None] * size)
        positions.append(position)
        s['rackStock'] -= 1
        message = f'Rack instalado no lugar {position + 1}.'
    elif name == 'remove_rack':
        position = integer(data, 'position', high=11)
        positions = s.setdefault('rackPositions', list(range(len(s['racks']))))
        if position not in positions:
            raise ValueError('Não há rack neste lugar.')
        index = positions.index(position)
        rack = s['racks'].pop(index)
        positions.pop(index)
        s['inventory'].extend(machine for machine in rack if machine)
        s['rackStock'] = s.get('rackStock', 0) + 1
        s.setdefault('returnedRackSizes', []).append(len(rack))
        message = 'Rack e máquinas devolvidos ao estoque.'
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
        s['inventory'].append(m)
        s['crateCount'] += 1
        count(s, 'crate')
        message = 'Caixa aberta: ' + m['name']
    else:
        raise ValueError('Ação indisponível.')
    event(s, message)
    return message


def snapshot(db, uid):
    users = list(db.execute('SELECT id,name,state FROM users WHERE blocked=0'))
    s = next(json.loads(u['state']) for u in users if u['id'] == uid)
    for period in MISSION:
        progress(s, period)
    active = power
    network = dict.fromkeys(DEFAULT_SPLIT, 0)
    for user in users:
        state = json.loads(user['state'])
        for currency, percent in state.get('poolSplit', DEFAULT_SPLIT).items():
            network[currency] += active(state) * percent / 100
    elapsed = db.execute('SELECT last-start FROM pool WHERE id=1').fetchone()[0]
    return {'game': {**s, 'balance': s['coins']/UNIT, 'total': s['mined']/UNIT,
                     'running': bool(active(s)), 'metrics': [], 'poolSeconds': elapsed,
                     'poolLastReward': s['lastReward']/UNIT},
            'pool': {'power': network['DCK'], 'powers': network, 'elapsed': elapsed, 'duration': POOL_SECONDS, 'reward': POOL_REWARD_UNITS / UNIT},
            'ranking': sorted([{'name': json.loads(u['state']).get('settings', {}).get('displayName', u['name']), 'power': active(json.loads(u['state'])),
                                'mine': u['id'] == uid} for u in users], key=lambda u: -u['power']),
            'user': next(u['name'] for u in users if u['id'] == uid),
            'email': db.execute('SELECT email FROM users WHERE id=?',(uid,)).fetchone()[0],
            'adminEligible': admin_eligible(db, uid),
            'depositConfig': deposit_config(),
            'btcConfig':btc.config(), 'btcDeposits':btc.player_data(db,uid),
            'deposits': [dict(row) for row in db.execute('SELECT * FROM deposits WHERE user_id=? ORDER BY created DESC,rowid DESC LIMIT 50',(uid,))]}


def deposit_config():
    rate = os.environ.get('DUCKMINER_DOGE_DCK_RATE','')
    valid = bool(re.fullmatch(r'\d{1,7}(?:\.\d{1,6})?',rate)) and 0 < Decimal(rate) <= 1000000
    # Acknowledgement is an explicit operator action after durable storage setup.
    enabled = (valid and os.environ.get('DUCKMINER_MANUAL_DEPOSITS') == '1'
               and os.environ.get('DUCKMINER_PERSISTENT_STORAGE_CONFIRMED') == '1'
               and os.environ.get('DUCKMINER_DEMO') != '1'
               and bool(os.environ.get('DUCKMINER_DB_PATH')))
    return {'enabled':enabled, 'rate':rate if valid else None, 'network':'Dogecoin mainnet'}


def doge_amount(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{1,7}(?:\.\d{1,8})?',value):
        raise ValueError('Valor DOGE inválido.')
    amount = int(Decimal(value)*100000000)
    if not 0 < amount <= 1000000*100000000:
        raise ValueError('Valor DOGE fora do limite.')
    return amount


def doge_address(value):
    """Validate a mainnet Base58Check P2PKH or P2SH public address."""
    alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    if not isinstance(value,str) or not 26 <= len(value) <= 35:
        return False
    number = 0
    for char in value:
        if char not in alphabet:
            return False
        number = number*58 + alphabet.index(char)
    raw = b'\0'*(len(value)-len(value.lstrip('1'))) + number.to_bytes((number.bit_length()+7)//8,'big')
    return (len(raw) == 25 and raw[0] in (0x1e,0x16)
            and hmac.compare_digest(hashlib.sha256(hashlib.sha256(raw[:-4]).digest()).digest()[:4],raw[-4:]))


def deposit_action(db, uid, data, now, administrative=False):
    if not deposit_config()['enabled']:
        raise ValueError('Depósitos desativados.')
    action = data.get('action')
    if action == 'create' and not administrative:
        existing = db.execute("SELECT * FROM deposits WHERE user_id=? AND status IN ('address','payment','review')",(uid,)).fetchone()
        if existing:
            return {'message':'Você já tem uma solicitação em andamento.'}
        amount = doge_amount(data.get('amount'))
        rate = deposit_config()['rate']
        credit = int((Decimal(amount)*Decimal(rate)*UNIT/100000000).to_integral_value(rounding=ROUND_DOWN))
        if not 0 < credit <= 1000000*UNIT:
            raise ValueError('Crédito fora do limite permitido.')
        db.execute('INSERT INTO deposits(id,user_id,created,amount,rate,credit,status) VALUES(?,?,?,?,?,?,?)',
                   (secrets.token_hex(16),uid,now,amount,rate,credit,'address'))
        return {'message':'Solicitação criada. Aguarde o endereço exclusivo do administrador.'}
    invoice = db.execute('SELECT * FROM deposits WHERE id=?',(data.get('id'),)).fetchone()
    if not invoice or (not administrative and invoice['user_id'] != uid):
        raise ValueError('Solicitação indisponível.')
    if action == 'report' and not administrative:
        txid = data.get('txid','')
        if not isinstance(txid,str) or not re.fullmatch('[a-fA-F0-9]{64}',txid) or invoice['status'] != 'payment':
            raise ValueError('Transação ou estado inválido.')
        db.execute("UPDATE deposits SET txid=?,status='review' WHERE id=?",(txid.lower(),invoice['id']))
        return {'message':'Transação enviada para conferência manual.'}
    if not administrative:
        raise ValueError('Ação inválida.')
    if action == 'assign':
        address = data.get('address','')
        if invoice['status'] != 'address' or not doge_address(address):
            raise ValueError('Endereço Dogecoin mainnet inválido ou solicitação indisponível.')
        if db.execute('SELECT 1 FROM deposits WHERE address=?',(address,)).fetchone():
            raise ValueError('Use um endereço exclusivo novo para cada solicitação.')
        db.execute("UPDATE deposits SET address=?,status='payment' WHERE id=?",(address,invoice['id']))
    elif action == 'approve':
        if invoice['status'] == 'approved':
            return {'message':'Este depósito já foi creditado.'}
        vout = integer(data,'vout')
        if invoice['status'] != 'review' or data.get('verified') is not True or doge_amount(data.get('received')) != invoice['amount']:
            raise ValueError('Confira o endereço, saída, valor e confirmações antes de aprovar.')
        confirmations = integer(data,'confirmations',low=6)
        if db.execute("SELECT 1 FROM deposits WHERE txid=? AND vout=? AND status='approved'",(invoice['txid'],vout)).fetchone():
            raise ValueError('Esta saída de transação já foi creditada.')
        target = db.execute('SELECT state,blocked FROM users WHERE id=?',(invoice['user_id'],)).fetchone()
        if not target or target['blocked']:
            raise ValueError('Conta indisponível.')
        state = json.loads(target['state'])
        state['coins'] += invoice['credit']
        event(state,'Depósito DOGE aprovado: +'+str(invoice['credit']/UNIT)+' DCK')
        db.execute('UPDATE users SET state=? WHERE id=?',(json.dumps(state),invoice['user_id']))
        db.execute("UPDATE deposits SET status='approved',vout=?,approved_at=?,confirmations=? WHERE id=?",(vout,now,confirmations,invoice['id']))
    else:
        raise ValueError('Ação indisponível.')
    db.execute('INSERT INTO admin_audit(at,actor,target,action) VALUES(?,?,?,?)',
               (now,uid,invoice['user_id'],'deposit:'+action+':'+invoice['id']))
    return {'message':'Solicitação atualizada.'}


def admin_eligible(db, uid):
    expected = os.environ.get('DUCKMINER_ADMIN_USERNAME', '').strip().lower()
    row = db.execute('SELECT name FROM users WHERE id=? AND blocked=0', (uid,)).fetchone()
    return bool(expected and row and row['name'] == expected)


def admin_snapshot(db, now, offset):
    total = db.execute('SELECT count(*) FROM users').fetchone()[0]
    recent = db.execute('SELECT count(DISTINCT user_id) FROM sessions WHERE expires>? AND last_seen>?', (now, now-60)).fetchone()[0]
    players = []
    for user in db.execute('SELECT id,name,state,blocked,created_at FROM users ORDER BY id LIMIT 50 OFFSET ?', (offset,)):
        state = json.loads(user['state'])
        session = db.execute('SELECT count(*) AS count,max(last_seen) AS seen FROM sessions WHERE user_id=? AND expires>?', (user['id'],now)).fetchone()
        players.append(dict(id=user['id'], name=user['name'], displayName=state.get('settings',{}).get('displayName',user['name']),
                            blocked=bool(user['blocked']), createdAt=user['created_at'],
                            balance=state['coins']/UNIT, totalMined=state['mined']/UNIT,
                            miningWallet={currency: state.get('miningWallet', {}).get(currency, 0) / UNIT
                                          for currency in ('BTC', 'DOGE')},
                            installed=len(machines(state)), inventory=len(state['inventory']),
                            roomMachines=[[{'name': m['name'], 'power': m['power']} if m else None
                                           for m in rack] for rack in state['racks']],
                            stockMachines=[{'name': m['name'], 'power': m['power']} for m in state['inventory']],
                            rackPositions=state.get('rackPositions') or list(range(len(state['racks']))),
                            power=power(state), racks=len(state['racks']),
                            rackStock=state.get('rackStock', 0), activeSessions=session['count'],
                            lastSeen=session['seen'] or 0, admin=user['name'] == os.environ.get('DUCKMINER_ADMIN_USERNAME','').strip().lower()))
    return dict(total=total, recent=recent, players=players, offset=offset, limit=50)


def consume_limits(rules, now):
    """Independent transaction so rejected attempts cannot roll back their counters."""
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM limits WHERE start < ?', (now - 3600,))
        allowed = True
        for key, maximum, duration in rules:
            row = db.execute('SELECT start,count FROM limits WHERE key=?', (key,)).fetchone()
            start, count = (row['start'], row['count']) if row and now-row['start'] < duration else (now, 0)
            allowed = allowed and count < maximum
            db.execute('INSERT OR REPLACE INTO limits VALUES(?,?,?)', (key, start, min(count+1, maximum+1)))
        return allowed


def invitation_valid(value):
    expected = os.environ.get('DUCKMINER_INVITE_CODE', '')
    return (len(expected) >= 16 and isinstance(value, str)
            and hmac.compare_digest(value.encode(), expected.encode()))


def normalize_email(value):
    if not isinstance(value,str):
        raise ValueError('E-mail inválido.')
    email = value.strip().lower()
    if not 3 <= len(email) <= 254 or email.count('@') != 1:
        raise ValueError('E-mail inválido.')
    local, domain = email.split('@')
    if (len(local) > 64 or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+",local)
            or local.startswith('.') or local.endswith('.') or '..' in local):
        raise ValueError('E-mail inválido.')
    labels = domain.split('.')
    if len(labels) < 2 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',label) for label in labels):
        raise ValueError('E-mail inválido.')
    return email


class LimitedServer(ThreadingHTTPServer):
    """Bound sockets and threads, including clients that stop sending a request."""
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(24)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b'HTTP/1.0 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = 'DuckMiner'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def end_headers(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
        # The retained interface uses inline handlers. Scripts cannot connect to third parties.
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if os.environ.get('DUCKMINER_SECURE_COOKIE') == '1':
            self.send_header('Strict-Transport-Security', 'max-age=31536000')
        super().end_headers()

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
        if urlsplit(self.path).path in ('/api/state', '/api/admin', '/api/admin/deposits', '/api/admin/btc', '/api/deposits/history'):
            return self.api()
        files = {'/': ('index.html', 'text/html; charset=utf-8'),
                 '/online.js': ('online.js', 'application/javascript; charset=utf-8'),
                 '/deposits.js': ('deposits.js', 'application/javascript; charset=utf-8'),
                 '/bitcoin.js': ('bitcoin.js', 'application/javascript; charset=utf-8')}
        if self.path not in files:
            return self.reply(404, {'error': 'Página indisponível.'})
        name, mime = files[self.path]
        try:
            body = (ROOT / 'public' / name).read_bytes()
        except OSError:
            return self.reply(503, {'error': 'Arquivos do jogo ausentes. Confira a pasta public no GitHub.'})
        if name == 'index.html' and os.environ.get('DUCKMINER_DEMO') == '1':
            notice = '<div style="padding:12px;background:#713f12;color:white;font-weight:bold">Demonstração gratuita: contas e progresso podem ser apagados quando o serviço reiniciar. DCK fictício.</div>'
            body = body.replace(b'<body>', b'<body>' + notice.encode())
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def btcpay_webhook(self):
        if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            return self.reply(400, {'error': 'Formato inválido.'})
        length = self.headers.get('Content-Length', '')
        if not re.fullmatch(r'[0-9]{1,5}', length) or not 0 < int(length) <= 65536:
            return self.reply(400, {'error': 'Tamanho inválido.'})
        body = self.rfile.read(int(length))
        if not btcpay.verify_webhook(body, self.headers.get('BTCPay-Sig', '')):
            return self.reply(403, {'error': 'Assinatura inválida.'})
        try:
            event = json.loads(body)
        except (ValueError, UnicodeError):
            return self.reply(400, {'error': 'Evento inválido.'})
        store = os.environ.get('BTCPAY_STORE_ID')
        if not isinstance(event, dict) or not store or event.get('storeId') != store:
            return self.reply(403, {'error': 'Loja inválida.'})
        if event.get("type") == "InvoiceSettled" and os.environ.get("BTCPAY_AUTO_DEPOSITS") == "1":
            try:
                btcpay.production_base()
                with connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    credited = btcpay.credit_deposit(db, event)
                return self.reply(200, {"received": True, "credited": credited})
            except Exception:
                return self.reply(503, {"error": "Confirmação indisponível."})
        # Demo invoices have no player association and never affect balances.
        return self.reply(200, {'received': True, 'credited': False})

    def do_POST(self):
        if self.path == '/api/btcpay/webhook':
            return self.btcpay_webhook()
        origin = self.headers.get('Origin')
        host = self.headers.get('Host', '')
        try:
            parsed = urlsplit(origin) if origin else None
        except ValueError:
            return self.reply(403, {'error': 'Origem inválida.'})
        if (self.headers.get('Sec-Fetch-Site') == 'cross-site'
                or (origin and (not parsed or parsed.scheme not in ('http', 'https') or parsed.netloc != host or parsed.path or parsed.query or parsed.fragment))):
            return self.reply(403, {'error': 'Origem inválida.'})
        if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            return self.reply(415, {'error': 'Use uma requisição JSON.'})
        if self.headers.get('X-DuckMiner-Request') != '1':
            return self.reply(403, {'error': 'Recarregue a página do jogo.'})
        if self.headers.get('Transfer-Encoding'):
            return self.reply(400, {'error': 'Formato de requisição inválido.'})
        self.api()

    def api(self):
        db = connect()
        try:
            data = {}
            now = int(time.time())
            path = urlsplit(self.path).path
            network_key = hashlib.sha256(self.client_address[0].encode()).hexdigest()
            # Do not trust a client-supplied X-Forwarded-For header. Account and
            # session limits below isolate players even when Render shares a proxy IP.
            if not consume_limits([('network:'+network_key, 3000, 60)], now):
                return self.reply(429, {'error': 'Muitas requisições. Aguarde um minuto.'})
            if self.command == 'POST':
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= 4096:
                    raise ValueError('Pedido inválido.')
                data = json.loads(self.rfile.read(length), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if not isinstance(data, dict):
                    raise ValueError('Pedido inválido.')
            if self.path in ('/api/register', '/api/login') and self.command == 'POST':
                name, password = data.get('name', ''), data.get('password', '')
                identifier = data.get('email', name)
                # Count even malformed credentials and incorrect invitation codes.
                account_key = hashlib.sha256(str(identifier).strip().lower().encode()).hexdigest()
                if not consume_limits([('auth-network:'+network_key, 100, 600),
                                       ('auth-account:'+account_key, 10, 600)], now):
                    return self.reply(429, {'error': 'Muitas tentativas. Aguarde dez minutos.'})
                registering = self.path == '/api/register'
                if registering and not invitation_valid(data.get('invite')):
                    return self.reply(403, {'error': 'Cadastro restrito. Solicite um convite válido ao responsável pelo jogo.'})
                if not isinstance(password, str) or not 10 <= len(password) <= 128:
                    raise ValueError('Senha inválida.')
                email = None
                if registering:
                    if not isinstance(name,str) or not re.fullmatch(r'[a-zA-Z0-9_]{3,24}',name):
                        raise ValueError('Nome de jogador inválido.')
                    email = normalize_email(data.get('email'))
                    name = name.lower()
                    user = db.execute('SELECT * FROM users WHERE name=? OR email=?',(name,email)).fetchone()
                elif isinstance(identifier,str) and '@' in identifier:
                    email = normalize_email(identifier)
                    user = db.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
                elif isinstance(identifier,str):
                    # Migration path for accounts created before email login.
                    user = db.execute('SELECT * FROM users WHERE name=? AND email IS NULL',(identifier.strip().lower(),)).fetchone()
                else:
                    raise ValueError('Credenciais inválidas.')
                salt = user['salt'] if user else secrets.token_bytes(16)
                if not HASH_SLOTS.acquire(blocking=False):
                    return self.reply(429, {'error': 'Servidor ocupado. Aguarde alguns segundos.'})
                try:
                    hashed = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
                finally:
                    HASH_SLOTS.release()
                if (registering and user) or (not registering and (not user or user['blocked'] or not hmac.compare_digest(hashed, user['password']))):
                    return self.reply(400, {'error': 'Nome indisponível ou credenciais inválidas.'})
                db.execute('BEGIN IMMEDIATE')
                advance(db, now)
                if registering:
                    if db.execute('SELECT 1 FROM users WHERE name=? OR email=?', (name,email)).fetchone():
                        db.rollback()
                        return self.reply(400, {'error': 'Nome indisponível ou credenciais inválidas.'})
                    uid = db.execute('INSERT INTO users(name,salt,password,state,created_at,email) VALUES(?,?,?,?,?,?)', (name, salt, hashed, json.dumps(fresh()),now,email)).lastrowid
                else:
                    uid = user['id']
                    if db.execute('SELECT blocked FROM users WHERE id=?', (uid,)).fetchone()[0]:
                        db.rollback()
                        return self.reply(403, {'error': 'Conta indisponível.'})
                token = secrets.token_urlsafe(32)
                csrf = secrets.token_urlsafe(32)
                db.execute('INSERT INTO sessions(token,user_id,expires,csrf,last_seen) VALUES(?,?,?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), uid, now+SESSION_SECONDS, csrf,now))
                db.execute('DELETE FROM sessions WHERE expires<=?', (now,))
                result = snapshot(db, uid)
                result['csrf'] = csrf
                db.commit()
                secure = '; Secure' if os.environ.get('DUCKMINER_SECURE_COOKIE') == '1' else ''
                return self.reply(200, result, f'duck_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}{secure}')
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            token = cookie['duck_session'].value if 'duck_session' in cookie else ''
            digest = hashlib.sha256(token.encode()).hexdigest()
            session = db.execute('SELECT user_id,csrf,admin_until FROM sessions JOIN users ON users.id=sessions.user_id WHERE token=? AND expires>? AND blocked=0', (digest, now)).fetchone()
            if not session:
                db.commit()
                return self.reply(401, {'error': 'Entre na sua conta.'})
            uid = session['user_id']
            rules = [('session:'+digest, 120, 60)]
            if self.command == 'POST':
                rules.append(('action:'+str(uid), 30, 60))
                csrf = self.headers.get('X-CSRF-Token', '')
                if not session['csrf'] or not hmac.compare_digest(csrf.encode(), session['csrf'].encode()):
                    return self.reply(403, {'error': 'Sessão desatualizada. Recarregue o jogo.'})
            if not consume_limits(rules, now):
                return self.reply(429, {'error': 'Muitas ações. Aguarde um minuto.'})
            if path == '/api/admin/unlock':
                if not consume_limits([('admin-unlock:'+str(uid), 5, 600)], now):
                    return self.reply(429, {'error': 'Muitas tentativas de acesso administrativo. Aguarde dez minutos.'})
            if path == '/api/account/email' and not consume_limits([('link-email:'+str(uid),5,600)],now):
                return self.reply(429, {'error':'Muitas tentativas. Aguarde dez minutos.'})
            db.execute('BEGIN IMMEDIATE')
            # Recheck after acquiring the write lock, so logout/revocation wins races.
            session = db.execute('SELECT user_id,csrf,admin_until FROM sessions JOIN users ON users.id=sessions.user_id WHERE token=? AND expires>? AND blocked=0', (digest, now)).fetchone()
            if not session:
                db.rollback()
                return self.reply(401, {'error': 'Entre na sua conta.'})
            if not session['csrf']:
                csrf = secrets.token_urlsafe(32)
                db.execute('UPDATE sessions SET csrf=? WHERE token=?', (csrf, digest))
            else:
                csrf = session['csrf']
            advance(db, now)
            db.execute('UPDATE sessions SET last_seen=? WHERE token=?', (now,digest))
            if path == '/api/deposits/history' and self.command == 'GET':
                offset = int(parse_qs(urlsplit(self.path).query).get('offset',['0'])[0])
                if offset < 0:
                    raise ValueError('Página inválida.')
                result = btc.history(db,uid,offset)
                db.commit()
                return self.reply(200,result)
            if path == '/api/account/email' and self.command == 'POST':
                email = normalize_email(data.get('email'))
                user = db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
                password = data.get('password')
                if user['email'] or not isinstance(password,str) or not 10 <= len(password) <= 128:
                    raise ValueError('Não foi possível vincular o e-mail.')
                if not HASH_SLOTS.acquire(blocking=False):
                    db.rollback()
                    return self.reply(429,{'error':'Servidor ocupado. Tente novamente.'})
                try:
                    hashed = hashlib.scrypt(password.encode(),salt=user['salt'],n=16384,r=8,p=1)
                finally:
                    HASH_SLOTS.release()
                if not hmac.compare_digest(hashed,user['password']) or db.execute('SELECT 1 FROM users WHERE email=?',(email,)).fetchone():
                    raise ValueError('Não foi possível vincular o e-mail.')
                db.execute('UPDATE users SET email=? WHERE id=?',(email,uid))
                db.execute('DELETE FROM sessions WHERE user_id=? AND token<>?',(uid,digest))
                result = snapshot(db,uid)
                result.update(csrf=csrf,message='E-mail cadastrado. Use-o no próximo acesso.')
                db.commit()
                return self.reply(200,result)
            if path.startswith('/api/admin'):
                if not admin_eligible(db, uid):
                    db.rollback()
                    return self.reply(403, {'error': 'Acesso exclusivo do administrador.'})
                if path == '/api/admin/unlock' and self.command == 'POST':
                    expected = os.environ.get('DUCKMINER_ADMIN_KEY', '')
                    supplied = data.get('key')
                    if len(expected) < 24 or not isinstance(supplied,str) or not hmac.compare_digest(expected.encode(), supplied.encode()):
                        db.rollback()
                        return self.reply(403, {'error': 'Chave administrativa inválida ou não configurada.'})
                    db.execute('UPDATE sessions SET admin_until=? WHERE token=?', (now+900,digest))
                    db.commit()
                    return self.reply(200, {'message':'Administração liberada por 15 minutos.'})
                if session['admin_until'] <= now:
                    db.rollback()
                    return self.reply(403, {'error': 'Digite a chave administrativa para liberar o painel.'})
                if path == '/api/admin/btc' and self.command == 'GET':
                    offset = int(parse_qs(urlsplit(self.path).query).get('offset',['0'])[0])
                    if offset < 0:
                        raise ValueError('Página inválida.')
                    rows = db.execute('SELECT btc_deposits.*,users.name FROM btc_deposits JOIN users ON users.id=btc_deposits.user_id ORDER BY created DESC,btc_deposits.rowid DESC LIMIT 50 OFFSET ?',(offset,))
                    result = {'deposits':[dict(row) for row in rows], 'offset':offset,
                              'total':db.execute('SELECT count(*) FROM btc_deposits').fetchone()[0]}
                    db.commit()
                    return self.reply(200,result)
                if path == '/api/admin/btc' and self.command == 'POST':
                    result = btc.action(db,uid,data,now,administrative=True)
                    db.commit()
                    return self.reply(200,result)
                if path == '/api/admin/deposits' and self.command == 'GET':
                    offset = int(parse_qs(urlsplit(self.path).query).get('offset',['0'])[0])
                    if offset < 0:
                        raise ValueError('Página inválida.')
                    rows = db.execute('SELECT deposits.*,users.name FROM deposits JOIN users ON users.id=deposits.user_id ORDER BY created DESC,deposits.rowid DESC LIMIT 50 OFFSET ?',(offset,))
                    result = {'deposits':[dict(r) for r in rows], 'offset':offset,
                              'total':db.execute('SELECT count(*) FROM deposits').fetchone()[0]}
                    db.commit()
                    return self.reply(200,result)
                if path == '/api/admin/deposit' and self.command == 'POST':
                    result = deposit_action(db,uid,data,now,administrative=True)
                    db.commit()
                    return self.reply(200,result)
                if path == '/api/admin' and self.command == 'GET':
                    offset = int(parse_qs(urlsplit(self.path).query).get('offset',['0'])[0])
                    if offset < 0:
                        raise ValueError('Página inválida.')
                    result = admin_snapshot(db,now,offset)
                    db.commit()
                    return self.reply(200,result)
                if path == '/api/admin/action' and self.command == 'POST':
                    target = integer(data,'userId')
                    target_user = db.execute('SELECT name FROM users WHERE id=?',(target,)).fetchone()
                    if not target_user:
                        raise ValueError('Conta indisponível.')
                    if target == uid or admin_eligible(db,target):
                        db.rollback()
                        return self.reply(400,{'error':'Esta operação não se aplica à conta administradora.'})
                    action = data.get('action')
                    if action in ('block','unblock'):
                        db.execute('UPDATE users SET blocked=? WHERE id=?',(int(action == 'block'),target))
                        if action == 'block':
                            state = json.loads(db.execute('SELECT state FROM users WHERE id=?',(target,)).fetchone()[0])
                            state['work'] = 0
                            db.execute('UPDATE users SET state=? WHERE id=?',(json.dumps(state),target))
                    elif action != 'revoke':
                        raise ValueError('Ação inválida.')
                    if action in ('block','revoke'):
                        db.execute('DELETE FROM sessions WHERE user_id=?',(target,))
                    db.execute('INSERT INTO admin_audit(at,actor,target,action) VALUES(?,?,?,?)',(now,uid,target,action))
                    db.commit()
                    return self.reply(200,{'message':'Operação administrativa concluída.'})
                raise ValueError('Rota administrativa inválida.')
            message = None
            if path == '/api/btc' and self.command == 'POST':
                if data.get('action') == 'create_btcpay':
                    try:
                        try:
                            btcpay.production_base()
                        except ValueError as error:
                            db.rollback()
                            message = str(error) if str(error).startswith(("A instância", "Configure", "DATABASE_URL", "BTCPAY_URL", "Confira")) else "Confira BTCPAY_URL no Render."
                            return self.reply(400, {'error': message})
                        result = btcpay.create_deposit(db, uid, data.get('amount'))
                    except OSError:
                        db.rollback()
                        return self.reply(502, {'error': 'BTCPay indisponível.'})
                    db.commit()
                    return self.reply(200, result)
                if data.get('action') == 'preview_btcpay':
                    if not admin_eligible(db, uid) or session['admin_until'] <= now:
                        db.rollback()
                        return self.reply(403, {'error': 'Desbloqueie o painel de administração primeiro.'})
                    db.commit()
                    result = btcpay.preview_invoice(data.get('amount'))
                    return self.reply(200, result)
                result = btc.action(db,uid,data,now)
                db.commit()
                return self.reply(200,result)
            if path == '/api/deposit' and self.command == 'POST':
                result = deposit_action(db,uid,data,now)
                db.commit()
                return self.reply(200,result)
            if self.path == '/api/logout' and self.command == 'POST':
                db.execute('DELETE FROM sessions WHERE token=?', (digest,))
                db.commit()
                return self.reply(200, {}, 'duck_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
            actor = uid
            support = self.headers.get('X-DuckMiner-Support')
            if support is not None:
                if not admin_eligible(db, actor) or session['admin_until'] <= now:
                    db.rollback()
                    return self.reply(403, {'error': 'Desbloqueie a sessão administrativa.'})
                if (path, self.command) not in (('/api/state', 'GET'), ('/api/action', 'POST')):
                    raise ValueError('Operação indisponível no suporte.')
                if not re.fullmatch(r'[0-9]{1,18}', support):
                    raise ValueError('Conta inválida.')
                uid = int(support)
                target = db.execute('SELECT name FROM users WHERE id=? AND blocked=0', (uid,)).fetchone()
                if not target or uid == actor:
                    raise ValueError('Conta indisponível.')
                if self.command == 'POST' and data.get('action') not in (
                    'buy', 'install', 'remove', 'move', 'expand', 'install_rack', 'remove_rack',
                    'pool_split', 'settings', 'cooling', 'upgrade', 'daily', 'claim', 'mission', 'crate'
                ):
                    raise ValueError('Ação indisponível no suporte.')
            if self.path == '/api/action' and self.command == 'POST':
                key = self.headers.get('Idempotency-Key', '')
                if not re.fullmatch(r'[a-zA-Z0-9-]{16,80}', key):
                    raise ValueError('Identificador da operação inválido.')
                body_hash = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                exists = db.execute('SELECT body_hash FROM requests WHERE user_id=? AND key=?', (uid, key)).fetchone()
                if exists and exists['body_hash'] != body_hash:
                    db.rollback()
                    return self.reply(409, {'error': 'Identificador já usado para outra operação.'})
                if not exists:
                    s = json.loads(db.execute('SELECT state FROM users WHERE id=?', (uid,)).fetchone()[0])
                    message = act(s, data)
                    if support is not None:
                        db.execute('CREATE TABLE IF NOT EXISTS support_audit (actor INTEGER, target INTEGER, action TEXT, created INTEGER, request_key TEXT)')
                        db.execute('INSERT INTO support_audit VALUES(?,?,?,?,?)', (actor, uid, data['action'], now, key))
                    db.execute('UPDATE users SET state=? WHERE id=?', (json.dumps(s), uid))
                    db.execute('INSERT INTO requests(user_id,key,body_hash) VALUES(?,?,?)', (uid, key, body_hash))
            elif self.path != '/api/state' or self.command != 'GET':
                raise ValueError('Pedido indisponível.')
            result = snapshot(db, uid)
            if support is not None:
                result.update(email=None, adminEligible=False, deposits=[], btcDeposits=[])
            result['message'] = message
            result['csrf'] = csrf
            db.commit()
            self.reply(200, result)
        except (ValueError, TypeError, KeyError, IndexError, UnicodeError):
            db.rollback()
            self.reply(400, {'error': 'Operação inválida ou indisponível. Confira saldo, carga e requisitos.'})
        except (SocketTimeout, ConnectionError):
            db.rollback()
        except Exception as error:
            db.rollback()
            print('Falha na API:', type(error).__name__, flush=True)
            self.reply(500, {'error': 'Não foi possível concluir. Tente novamente.'})
        finally:
            db.close()


def worker(stop):
    # API requests settle all elapsed rounds before reading or changing state.
    # Avoid competing for the PostgreSQL write lock once per second.
    if os.environ.get("DATABASE_URL"):
        return
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
    if len(os.environ.get('DUCKMINER_INVITE_CODE', '')) < 16:
        print('Cadastro fechado: configure DUCKMINER_INVITE_CODE com pelo menos 16 caracteres para liberar convites.', flush=True)
    try:
        http = LimitedServer((args.host, args.port), Handler)
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
