import json
import os
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
    except (OSError, ValueError, AttributeError):
        return {"message": "O BTCPay não confirmou a criação. Confira a chave e o ID da loja."}
    return {"message": "Cobrança de demonstração criada. Confira em Invoices no BTCPay. Não envie BTC."}
