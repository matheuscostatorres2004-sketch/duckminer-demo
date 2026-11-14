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
