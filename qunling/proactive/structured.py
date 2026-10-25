"""Bounded local protocol recovery; never replay a paid request."""
import json
import re


def decode_object(text):
    if not isinstance(text, str) or len(text) > 6000:
        raise ValueError('Invalid structured reply')
    text = text.strip()
    fenced = re.fullmatch(r'```(?:json)?\s*\n(.*?)\n```', text, re.S | re.I)
    if fenced:
        text = fenced[1]
    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate key')
            result[key] = value
        return result
    def invalid(_):
        raise ValueError('Invalid constant')
    value = json.loads(text, object_pairs_hook=unique, parse_constant=invalid)
    if not isinstance(value, dict):
        raise ValueError('Expected an object')
    return value
