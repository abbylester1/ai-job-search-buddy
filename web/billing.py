"""Stripe Checkout for credit packs. Disabled unless STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET are set."""
from __future__ import annotations

import os

PACKS = {
    "starter": {"credits": 30, "price_cents": 500, "label": "30 credits"},
    "pro": {"credits": 100, "price_cents": 1500, "label": "100 credits"},
}
CURRENCY = os.environ.get("STRIPE_CURRENCY", "usd")


class WebhookError(Exception):
    pass


def enabled() -> bool:
    return bool(os.environ.get("STRIPE_SECRET_KEY") and os.environ.get("STRIPE_WEBHOOK_SECRET"))


def _stripe():
    import stripe

    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
    return stripe


def checkout_url(pack_id: str, user_id: int, host_url: str) -> str:
    pack = PACKS[pack_id]
    base = os.environ.get("PUBLIC_URL", host_url).rstrip("/")
    s = _stripe().checkout.Session.create(
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": CURRENCY,
                "product_data": {"name": f"AI Job Search Buddy - {pack['label']}"},
                "unit_amount": pack["price_cents"],
            },
            "quantity": 1,
        }],
        client_reference_id=str(user_id),
        metadata={"user_id": str(user_id), "pack": pack_id},
        success_url=f"{base}/?paid=1",
        cancel_url=f"{base}/?paid=0",
    )
    return s.url


def parse_webhook(payload: bytes, signature: str) -> dict | None:
    """Verify the Stripe signature; return a credit grant for completed, paid checkouts."""
    stripe = _stripe()
    try:
        event = stripe.Webhook.construct_event(payload, signature, os.environ["STRIPE_WEBHOOK_SECRET"])
    except Exception as e:  # invalid payload or signature
        raise WebhookError("Invalid webhook") from e
    if event["type"] != "checkout.session.completed":
        return None
    obj = event["data"]["object"]
    if obj.get("payment_status") != "paid":
        return None
    pack = PACKS.get((obj.get("metadata") or {}).get("pack", ""))
    user_id = (obj.get("metadata") or {}).get("user_id")
    if not pack or not user_id:
        return None
    # Trust the amount Stripe charged, not just the metadata.
    if obj.get("amount_total") != pack["price_cents"]:
        return None
    return {"user_id": int(user_id), "credits": pack["credits"], "ref": obj["id"]}
