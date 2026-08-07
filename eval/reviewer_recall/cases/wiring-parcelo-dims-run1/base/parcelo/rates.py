"""Quoting: what a shipment costs."""

from parcelo.zones import surcharge_for, zone_for

BASE_FEE = 3.20
PER_KG = 1.10
EXPRESS_MULTIPLIER = 1.5


def quote(weight_kg, origin, destination, express=False):
    """The all-in price for a shipment."""
    origin_zone = zone_for(origin)
    dest_zone = zone_for(destination)
    weight_cost = round(PER_KG * weight_kg, 2)
    surcharge = surcharge_for(origin_zone, dest_zone)
    total = BASE_FEE + weight_cost + surcharge
    if express:
        total = total * EXPRESS_MULTIPLIER + surcharge
    return round(total, 2)
