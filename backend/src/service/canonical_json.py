"""Storage-stable JSON hashing without rounding scientific values.

JSONB may turn 1e20 into an integer and -0.0 into 0.0. Canonical numeric tokens
represent the exact decimal JSON value, independently of Python numeric type/notation.
"""
from decimal import Decimal
from hashlib import sha256
import json
import math


def semantic_checksum(value: object) -> str:
    digest = sha256(b"quantik-json-number-v1:")

    def visit(item):
        if item is None:
            digest.update(b"null")
        elif isinstance(item, bool):
            digest.update(b"true" if item else b"false")
        elif isinstance(item, (int, float)):
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("Nonfinite JSON number")
            number = Decimal(str(item))
            if number == 0:
                digest.update(b"0")
            else:
                sign, digits, exponent = number.as_tuple()
                digits = list(digits)
                while digits[-1] == 0:
                    digits.pop()
                    exponent += 1
                digest.update((('-' if sign else '')+''.join(map(str, digits))+"e"+str(exponent)).encode("ascii"))
        elif isinstance(item, str):
            digest.update(json.dumps(item, ensure_ascii=False).encode("utf-8"))
        elif isinstance(item, (list, tuple)):
            digest.update(b"[")
            for child in item:
                visit(child)
                digest.update(b",")
            digest.update(b"]")
        elif isinstance(item, dict):
            keys = {}
            for key in item:
                if not isinstance(key, (str, int)) or isinstance(key, bool):
                    raise TypeError("JSON object keys must be strings or integers")
                rendered = str(key)
                if rendered in keys:
                    raise ValueError("Ambiguous JSON object keys")
                keys[rendered] = key
            digest.update(b"{")
            for rendered in sorted(keys):
                visit(rendered)
                digest.update(b":")
                visit(item[keys[rendered]])
                digest.update(b",")
            digest.update(b"}")
        else:
            raise TypeError("Unsupported JSON value")

    visit(value)
    return digest.hexdigest()
