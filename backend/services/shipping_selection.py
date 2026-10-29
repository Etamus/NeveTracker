"""Select one quoted delivery service and keep its own transit time."""
import math


def shipping_sort_key(cost, maximum_days=None):
    # Prefer an actual paid delivery over promotional zero-price services.
    # Confirmed free delivery remains valid when no paid alternative exists.
    valid = isinstance(cost, (float, int)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0
    return (0 if valid and cost > 0 else 1 if valid else 2,
            cost if valid else math.inf,
            maximum_days if maximum_days is not None else math.inf)
