"""PriceLabs connector: read-only vacancy and pricing inventory.

There is deliberately no write path in this package. See `models.NightState`
for how a night's sellability is represented and `normalise` for the only
module that knows PriceLabs field names.
"""
