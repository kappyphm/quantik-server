import math

import pytest

from service.canonical_json import semantic_checksum


def test_numeric_hash_survives_jsonb_without_rounding_values():
    before = {"signed_zero": -0.0, "integer_exponent": 1e20, "fraction": 1.234567890123456e-17,
        "rules": {2: 0.1, 10: 0.2}, "boolean": False}
    after = {"boolean": False, "rules": {"10": 0.2, "2": 0.1}, "fraction": 0.00000000000000001234567890123456,
        "integer_exponent": 100000000000000000000, "signed_zero": 0.0}
    assert semantic_checksum(before) == semantic_checksum(after)
    assert semantic_checksum(before) != semantic_checksum(before | {"boolean": 0})
    assert semantic_checksum(before) != semantic_checksum(before | {"fraction": math.nextafter(before["fraction"], math.inf)})
    with pytest.raises(ValueError):
        semantic_checksum({"invalid": float("nan")})
    with pytest.raises(ValueError):
        semantic_checksum({1: "number key", "1": "string key"})
