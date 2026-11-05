"""NOWPayments integration helpers.

Two things happen here:
  1. create_invoice() — ask NOWPayments to create a hosted checkout for a
     deposit, and return the URL to send the user to.
  2. verify_ipn_signature() — confirm an incoming payment callback genuinely
     came from NOWPayments before we credit anyone.

A deposit is credited ONLY when a callback's signature verifies (see
views.nowpayments_ipn). That is what ties a balance to a real payment.
"""
import hashlib
import hmac
import json

import requests

from .models import GatewaySettings


def _sorted_compact(data) -> str:
    """Serialize exactly how NOWPayments signs: keys recursively sorted,
    compact separators, no whitespace."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def create_invoice(amount, order_id, ipn_url, success_url, cancel_url, description=""):
    """Create a NOWPayments invoice. Returns (invoice_url, invoice_id) or
    raises RuntimeError with a readable message."""
    cfg = GatewaySettings.get()
    if not cfg.enabled or not cfg.api_key:
        raise RuntimeError("gateway_not_configured")

    payload = {
        "price_amount": float(amount),
        "price_currency": cfg.price_currency,
        "order_id": order_id,
        "order_description": description or f"Deposit {order_id}",
        "ipn_callback_url": ipn_url,
        "success_url": success_url,
        "cancel_url": cancel_url,
    }
    try:
        resp = requests.post(
            f"{cfg.api_base}/invoice",
            json=payload,
            headers={"x-api-key": cfg.api_key, "Content-Type": "application/json"},
            timeout=20,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"gateway_unreachable: {e}")

    if resp.status_code >= 400:
        raise RuntimeError(f"gateway_error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    url = data.get("invoice_url")
    if not url:
        raise RuntimeError(f"gateway_no_url: {data}")
    return url, str(data.get("id", ""))


def verify_ipn_signature(raw_body: bytes, received_sig: str) -> bool:
    """True iff the HMAC-SHA512 of the sorted/compact JSON body, keyed with the
    IPN secret, matches the x-nowpayments-sig header."""
    cfg = GatewaySettings.get()
    if not cfg.ipn_secret or not received_sig:
        return False
    try:
        parsed = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    message = _sorted_compact(parsed).encode("utf-8")
    expected = hmac.new(
        cfg.ipn_secret.encode("utf-8"), message, hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(expected.lower(), received_sig.strip().lower())
