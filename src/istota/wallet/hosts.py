"""Exact HTTPS origins admitted for card fields."""

from istota.credentials.broker.bindings import credential_host

PAYMENT_FRAME_HOSTS = frozenset({
    "js.stripe.com", "checkout.stripe.com", "assets.braintreegateway.com",
    "checkoutshopper-live.adyen.com",
})


def normalize_merchant(url_or_host: str) -> str:
    if not isinstance(url_or_host, str):
        raise ValueError("invalid merchant host")
    if "://" not in url_or_host:
        if any(mark in url_or_host for mark in "/?#@"):
            raise ValueError("invalid merchant host")
        url_or_host = "https://" + url_or_host
    return credential_host(url_or_host)
