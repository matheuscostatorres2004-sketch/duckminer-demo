"""Manual Bitcoin receipts. All credits are internal DCK, valued at USD 1 each."""
import hashlib
import hmac
import json
import os
import re
import secrets
from decimal import Decimal, ROUND_DOWN

UNIT = 1000000
SATOSHIS = 100000000


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS btc_deposits(
      id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created INTEGER NOT NULL,
      amount INTEGER NOT NULL, rate TEXT NOT NULL, credit INTEGER NOT NULL,
      address TEXT UNIQUE, txid TEXT, vout INTEGER, status TEXT NOT NULL,
      approved_at INTEGER, confirmations INTEGER)''')
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS btc_output ON btc_deposits(txid,vout) WHERE status='approved'")


def config():
    rate = os.environ.get('DUCKMINER_BTC_USD_RATE','')
    valid = bool(re.fullmatch(r'\d{1,9}(?:\.\d{1,6})?',rate)) and 0 < Decimal(rate) <= 100000000
    enabled = (valid and os.environ.get('DUCKMINER_MANUAL_DEPOSITS') == '1'
               and os.environ.get('DUCKMINER_PERSISTENT_STORAGE_CONFIRMED') == '1'
               and os.environ.get('DUCKMINER_DEMO') != '1'
               and bool(os.environ.get('DATABASE_URL') or os.environ.get('DUCKMINER_DB_PATH')))
    return dict(enabled=enabled, rate=rate if valid else None, dckUsd='1', source='Cotação definida pelo administrador')


def amount(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{1,8}(?:\.\d{1,8})?',value):
        raise ValueError('Quantidade BTC inválida.')
    sats = int(Decimal(value)*SATOSHIS)
    if not 0 < sats <= 21000000*SATOSHIS:
        raise ValueError('Quantidade BTC fora do limite.')
    return sats


def address_valid(address):
    if not isinstance(address,str):
        return False
    if address.startswith(('1','3')) and 26 <= len(address) <= 35:
        alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
        value = 0
        for char in address:
            if char not in alphabet:
                return False
            value = value*58 + alphabet.index(char)
        raw = b'\0'*(len(address)-len(address.lstrip('1'))) + value.to_bytes((value.bit_length()+7)//8,'big')
        return len(raw) == 25 and raw[0] in (0,5) and hmac.compare_digest(hashlib.sha256(hashlib.sha256(raw[:-4]).digest()).digest()[:4],raw[-4:])
    if not 14 <= len(address) <= 90 or (address.lower() != address and address.upper() != address):
        return False
    address = address.lower()
    if not address.startswith('bc1'):
        return False
    alphabet = 'qpzry9x8gf2tvdw0s3jn54khce6mua7l'
    data = [alphabet.find(char) for char in address[3:]]
    if len(data) < 7 or -1 in data or data[0] > 16:
        return False
    checksum = 1
    for value in [3,3,0,2,3] + data:  # HRP expansion for "bc".
        top = checksum >> 25
        checksum = ((checksum & 0x1ffffff) << 5) ^ value
        for i, generator in enumerate((0x3b6a57b2,0x26508e6d,0x1ea119fa,0x3d4233dd,0x2a1462b3)):
            if (top >> i) & 1:
                checksum ^= generator
    if checksum != (1 if data[0] == 0 else 0x2bc830a3):
        return False
    bits = accumulator = 0
    program = []
    for value in data[1:-6]:
        accumulator = (accumulator << 5) | value
        bits += 5
        while bits >= 8:
            bits -= 8
            program.append((accumulator >> bits) & 255)
    if bits >= 5 or ((accumulator << (8-bits)) & 255):
        return False
    return 2 <= len(program) <= 40 and (data[0] != 0 or len(program) in (20,32))


def player_data(db, uid):
    return [dict(row) for row in db.execute('SELECT * FROM btc_deposits WHERE user_id=? ORDER BY created DESC,rowid DESC LIMIT 50',(uid,))]


def history(db, uid, offset):
    count = db.execute('SELECT (SELECT count(*) FROM deposits WHERE user_id=?) + (SELECT count(*) FROM btc_deposits WHERE user_id=?)',(uid,uid)).fetchone()[0]
    rows = db.execute('''SELECT id,created,amount,rate,credit,address,txid,status,approved_at,'DOGE' AS currency
      FROM deposits WHERE user_id=? UNION ALL
      SELECT id,created,amount,rate,credit,address,txid,status,approved_at,'BTC' AS currency
      FROM btc_deposits WHERE user_id=? ORDER BY created DESC,id DESC LIMIT 20 OFFSET ?''',(uid,uid,offset))
    return dict(total=count,offset=offset,items=[dict(row) for row in rows])


def action(db, uid, data, now, administrative=False):
    cfg = config()
    if not cfg['enabled']:
        raise ValueError('Conversão BTC desativada. Configure armazenamento persistente e cotação.')
    name = data.get('action')
    if name == 'create' and not administrative:
        if db.execute("SELECT 1 FROM btc_deposits WHERE user_id=? AND status IN ('address','payment','review')",(uid,)).fetchone():
            return {'message':'Você já possui uma solicitação BTC em andamento.'}
        sats = amount(data.get('amount'))
        credit = int((Decimal(sats)*Decimal(cfg['rate'])*UNIT/SATOSHIS).to_integral_value(rounding=ROUND_DOWN))
        if not 0 < credit <= 1000000*UNIT:
            raise ValueError('Conversão fora do limite permitido.')
        db.execute('INSERT INTO btc_deposits(id,user_id,created,amount,rate,credit,status) VALUES(?,?,?,?,?,?,?)',
                   (secrets.token_hex(16),uid,now,sats,cfg['rate'],credit,'address'))
        return {'message':'Solicitação BTC criada. Aguarde um endereço exclusivo para este depósito.'}
    invoice = db.execute('SELECT * FROM btc_deposits WHERE id=?',(data.get('id'),)).fetchone()
    if not invoice or (not administrative and invoice['user_id'] != uid):
        raise ValueError('Solicitação indisponível.')
    if name == 'report' and not administrative:
        txid = data.get('txid')
        if not isinstance(txid,str) or not re.fullmatch('[a-fA-F0-9]{64}',txid) or invoice['status'] != 'payment':
            raise ValueError('TXID ou estado inválido.')
        db.execute("UPDATE btc_deposits SET txid=?,status='review' WHERE id=?",(txid.lower(),invoice['id']))
        return {'message':'Pagamento BTC enviado para conferência manual.'}
    if not administrative:
        raise ValueError('Ação indisponível.')
    if name == 'assign':
        address = data.get('address','')
        if invoice['status'] != 'address' or not address_valid(address):
            raise ValueError('Endereço Bitcoin inválido.')
        address = address.lower() if address.lower().startswith('bc1') else address
        if db.execute('SELECT 1 FROM btc_deposits WHERE address=?',(address,)).fetchone():
            raise ValueError('Use um endereço Bitcoin exclusivo novo.')
        db.execute("UPDATE btc_deposits SET address=?,status='payment' WHERE id=?",(address,invoice['id']))
    elif name == 'approve':
        if invoice['status'] == 'approved':
            return {'message':'Este depósito BTC já foi creditado.'}
        vout, confirmations = data.get('vout'),data.get('confirmations')
        if (invoice['status'] != 'review' or data.get('verified') is not True
                or type(vout) is not int or not 0 <= vout <= 10**9
                or type(confirmations) is not int or not 6 <= confirmations <= 10**9
                or amount(data.get('received')) != invoice['amount']):
            raise ValueError('Confira rede Bitcoin, endereço, valor, saída e confirmações.')
        if db.execute("SELECT 1 FROM btc_deposits WHERE txid=? AND vout=? AND status='approved'",(invoice['txid'],vout)).fetchone():
            raise ValueError('Saída BTC já creditada.')
        user = db.execute('SELECT state,blocked FROM users WHERE id=?',(invoice['user_id'],)).fetchone()
        if not user or user['blocked']:
            raise ValueError('Conta indisponível.')
        state = json.loads(user['state'])
        state['btcDepositedSats'] = state.get('btcDepositedSats', 0) + invoice['amount']
        state['history'].insert(0,'Depósito BTC aprovado: +'+str(invoice['amount']/SATOSHIS)+' BTC')
        state['history'] = state['history'][:45]
        db.execute('UPDATE users SET state=? WHERE id=?',(json.dumps(state),invoice['user_id']))
        db.execute("UPDATE btc_deposits SET status='approved',vout=?,confirmations=?,approved_at=? WHERE id=?",(vout,confirmations,now,invoice['id']))
    else:
        raise ValueError('Ação indisponível.')
    db.execute('INSERT INTO admin_audit(at,actor,target,action) VALUES(?,?,?,?)',(now,uid,invoice['user_id'],'btc:'+name+':'+invoice['id']))
    return {'message':'Solicitação BTC atualizada.'}
