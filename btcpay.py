import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import build_opener, HTTPRedirectHandler, Request

import btc


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def preview_invoice(amount):
    base = os.environ.get("BTCPAY_URL", "").rstrip("/")
    store = os.environ.get("BTCPAY_STORE_ID")
    key = os.environ.get("BTCPAY_API_KEY")
    if base != "https://mainnet.demo.btcpayserver.org" or not store or not key:
        return {"message": "Configure as três variáveis BTCPay no Render."}
    sats = btc.amount(amount)
    whole, fractional = divmod(sats, btc.SATOSHIS)
    payload = {"amount": f"{whole}.{fractional:08d}", "currency": "BTC"}
    req = Request(f"{base}/api/v1/stores/{quote(store, safe='')}/invoices",
                  data=json.dumps(payload).encode(),
                  headers={"Authorization": "token " + key, "Content-Type": "application/json"})
    try:
        with build_opener(NoRedirect()).open(req, timeout=12) as response:
            invoice = json.load(response)
        if not invoice.get("id") or invoice.get("currency") != "BTC":
            raise ValueError()
    except HTTPError as error:
        hints = {
            400: "O BTCPay recusou os dados da cobrança. Confira a configuração de Bitcoin on-chain na loja.",
            401: "Confira se BTCPAY_API_KEY contém a chave válida, sem aspas nem prefixo token.",
            403: "Confira a permissão Create an invoice para a loja DuckMiner.",
            404: "Confira se a URL e o Store ID pertencem à mesma instância.",
        }
        detail = ""
        if error.code in (400, 422):
            try:
                body = json.loads(error.read(8192))
                entries = body if isinstance(body, list) else [body]
                parts = []
                for entry in entries[:4]:
                    if not isinstance(entry, dict):
                        continue
                    field = entry.get("path") or entry.get("field")
                    code = entry.get("code")
                    for value in (field, code):
                        if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", value):
                            if not any(secret and secret in value for secret in (key, store)):
                                parts.append(value)
                if parts:
                    detail = " Campos ou códigos da resposta: " + ", ".join(dict.fromkeys(parts)) + "."
            except (ValueError, OSError):
                pass
        return {"message": f"BTCPay HTTP {error.code}. " + hints.get(error.code, "Confira o painel da loja e tente novamente.") + detail}
    except (URLError, OSError):
        return {"message": "Sem resposta do BTCPay. Confira se a instância está disponível."}
    except (ValueError, AttributeError):
        return {"message": "O BTCPay retornou uma resposta inesperada. Confira Invoices antes de repetir."}
    return {"message": "Cobrança de demonstração criada. Confira em Invoices no BTCPay. Não envie BTC."}


def verify_webhook(body, signature):
    import hashlib
    import hmac

    secret = os.environ.get("BTCPAY_WEBHOOK_SECRET", "")
    if len(secret) < 32 or not re.fullmatch(r"sha256=[0-9a-f]{64}", signature):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)

def production_base():
    from urllib.parse import urlsplit
    base = os.environ.get("BTCPAY_URL", "").rstrip("/")
    url = urlsplit(base)
    if url.hostname and url.hostname.endswith(".demo.btcpayserver.org"):
        raise ValueError("A instância de demonstração do BTCPay não aceita depósitos automáticos no DuckMiner.")
    if os.environ.get("BTCPAY_AUTO_DEPOSITS") != "1":
        raise ValueError("Configure BTCPAY_AUTO_DEPOSITS=1 no Render para ativar a integração de produção.")
    if not os.environ.get("DATABASE_URL"):
        raise ValueError("DATABASE_URL ausente no Render.")
    if (url.scheme != "https" or not url.hostname or url.username
            or url.path or url.query or url.fragment):
        raise ValueError("BTCPAY_URL deve conter a URL base HTTPS da instância de produção.")
    if not os.environ.get("BTCPAY_STORE_ID") or not os.environ.get("BTCPAY_API_KEY"):
        raise ValueError("Confira BTCPAY_STORE_ID e BTCPAY_API_KEY no Render.")
    return base


def invoice_api(suffix="", payload=None):
    base = production_base()
    store = quote(os.environ["BTCPAY_STORE_ID"], safe="")
    req = Request(f"{base}/api/v1/stores/{store}/invoices{suffix}",
                  data=json.dumps(payload).encode() if payload is not None else None,
                  headers={"Authorization": "token " + os.environ["BTCPAY_API_KEY"],
                           "Content-Type": "application/json"})
    with build_opener(NoRedirect()).open(req, timeout=12) as response:
        return json.load(response)


def create_deposit(db, uid, amount):
    sats = btc.amount(amount)
    invoice = invoice_api(payload={"amount": f"{sats // btc.SATOSHIS}.{sats % btc.SATOSHIS:08d}",
                                   "currency": "BTC", "checkout": {"paymentMethods": ["BTC-CHAIN"],
                                   "speedPolicy": "LowSpeed", "paymentTolerance": 0}})
    invoice_id = invoice["id"]
    link = invoice["checkoutLink"]
    if not link.startswith(production_base() + "/"):
        raise ValueError("Link da cobrança inválido.")
    db.execute("INSERT INTO btcpay_deposits(invoice_id,user_id,sats) VALUES(?,?,?)", (invoice_id, uid, sats))
    return {"message": "Cobrança criada.", "checkoutLink": link}


def credit_deposit(db, event):
    from decimal import Decimal
    invoice_id = event.get("invoiceId", "")
    if not isinstance(invoice_id, str) or not re.fullmatch(r"[A-Za-z0-9]{1,100}", invoice_id):
        return False
    deposit = db.execute("SELECT * FROM btcpay_deposits WHERE invoice_id=?", (invoice_id,)).fetchone()
    if not deposit or deposit["credited"]:
        return False
    path = "/" + invoice_id
    invoice = invoice_api(path)
    expected = Decimal(deposit["sats"]) / btc.SATOSHIS
    if (invoice.get("id") != invoice_id or invoice.get("storeId") != event["storeId"]
            or invoice.get("status") != "Settled" or invoice.get("additionalStatus") != "None"
            or invoice.get("currency") != "BTC" or Decimal(str(invoice.get("amount"))) != expected):
        return False
    methods = invoice_api(path + "/payment-methods")
    if not any(m.get("paymentMethodId") == "BTC-CHAIN" and m.get("currency") == "BTC"
               and Decimal(str(m["totalPaid"])) == expected for m in methods):
        return False
    row = db.execute("SELECT state FROM users WHERE id=?", (deposit["user_id"],)).fetchone()
    state = json.loads(row["state"])
    state["btcDepositedSats"] = state.get("btcDepositedSats", 0) + deposit["sats"]
    db.execute("UPDATE users SET state=? WHERE id=?", (json.dumps(state), deposit["user_id"]))
    db.execute("UPDATE btcpay_deposits SET credited=1 WHERE invoice_id=?", (invoice_id,))
    return True
