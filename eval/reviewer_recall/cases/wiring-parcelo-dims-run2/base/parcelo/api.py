"""Dispatch layer: dict in, dict out. The HTTP shim only calls handle()."""

from parcelo.orders import OrderStore
from parcelo.rates import quote
from parcelo.zones import UnknownPostcode

STORE = OrderStore()


def handle(request):
    """Route one request dict to a response dict."""
    op = request.get("op")
    try:
        if op == "quote":
            total = quote(request["weight_kg"], request["origin"],
                          request["destination"],
                          express=bool(request.get("express", False)))
            return {"ok": True, "total": total}
        if op == "create_order":
            total = quote(request["weight_kg"], request["origin"],
                          request["destination"],
                          express=bool(request.get("express", False)))
            order = STORE.create_order(request["destination"],
                                       request["weight_kg"], total)
            return {"ok": True, "order": order}
    except UnknownPostcode as exc:
        return {"ok": False, "error": "unknown postcode: %s" % exc}
    except KeyError as exc:
        return {"ok": False, "error": "missing field: %s" % exc}
    return {"ok": False, "error": "unknown op: %s" % op}
