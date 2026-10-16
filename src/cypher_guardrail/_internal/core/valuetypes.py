"""The types of values, as FalkorDB's ``typeof`` names them, and when a literal in a query can match one of them.

FalkorDB does not fail when a property is compared to a value of another type (``n.year = '2020'`` for an integer
``year``): it finds nothing. The type check of the properties validator reports it, with the rule of this module.
"""

# FalkorDB compares an integer and a float as numbers (`2020 = 2020.0` is true), so they are one group
NUMERIC = frozenset({"Integer", "Float"})


def fits(literal_type: str, observed) -> bool:
    """Whether a value of ``literal_type`` can be equal to a value of one of the ``observed`` types.

    ``observed`` is what ``typeof`` found for the property; with mixed data it has several. No observed type at all
    (the property has no value) fits nothing.
    """
    if literal_type in NUMERIC:
        return not NUMERIC.isdisjoint(observed)
    return literal_type in observed
