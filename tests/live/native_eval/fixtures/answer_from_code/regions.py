"""Routing codes, one per region."""

ROUTING_CODES = {
    "north-1": "KV-1180",
    "north-3": "KV-7724",
    "south-2": "KV-3391",
    "west-4": "KV-5062",
}


def code_for(region):
    return ROUTING_CODES[region]
