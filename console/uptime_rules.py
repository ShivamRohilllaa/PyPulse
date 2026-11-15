"""Response assertions. Reports use fixed messages, never values from a response."""
import json
import re

from django.core.exceptions import ValidationError
from .benchconfig import statuses

MISSING = object()
PATH = re.compile(r"(?:[\w-]+|\[\d+\])(?:\.[\w-]+|\[\d+\])*")
OPERATORS = ("equals", "not_equals", "contains", "exists")


def status_codes(expression):
    normalized = re.sub(r"\b([1-5])xx\b", lambda m: f"{m[1]}00-{m[1]}99", expression, flags=re.I)
    return statuses(re.sub(r"\s*-\s*", "-", normalized))


def valid_path(path):
    return isinstance(path, str) and len(path) <= 200 and (not path or PATH.fullmatch(path) is not None)


def lookup(doc, path):
    if not valid_path(path):
        return MISSING
    value = doc
    for key in re.findall(r"[\w-]+",path):
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value,list) and key.isdecimal() and int(key) < len(value):
            value = value[int(key)]
        else:
            return MISSING
    return value


def check_rules(monitor, options, result):
    if result["error"] or not result["status"]:
        return "Request failed"
    accepted = status_codes(monitor.status_expression) if monitor.status_expression else options.get("statuses", [monitor.expected_status])
    if result["status"] not in accepted:
        return "Unexpected HTTP status"
    if monitor.contains and monitor.contains.encode() not in result["body"]:
        return "Body text mismatch"
    exact = options.get("json_equals", {})
    rules = options.get("json_checks", [])
    if not exact and not rules:
        return ""
    try:
        doc = json.loads(result["body"])
    except (ValueError, RecursionError):
        return "Response is not valid JSON"
    for path, expected in exact.items():
        actual = lookup(doc,path)
        if type(actual) is not type(expected) or actual != expected:
            return "JSON value mismatch"
    for rule in rules:
        actual = lookup(doc,rule["path"])
        operator, expected = rule["op"], rule.get("value", "")
        if operator == "exists":
            if actual is MISSING or actual is None:
                return "JSON field is missing"
            continue
        observed = actual if isinstance(actual,str) else None if actual is MISSING else json.dumps(actual,separators=(",",":"),ensure_ascii=False)
        if operator == "equals" and observed != expected:
            return "JSON value mismatch"
        if operator == "not_equals" and observed == expected:
            return "JSON exclusion failed"
        if operator == "contains" and (observed is None or expected not in observed):
            return "JSON text mismatch"
    return ""


def validate_json_rules(value):
    if not isinstance(value, list) or len(value) > 24:
        raise ValidationError("Use up to 24 JSON assertions.")
    for rule in value:
        if not isinstance(rule,dict) or set(rule) - {"path","op","value"} or not valid_path(rule.get("path")):
            raise ValidationError("Use an object or array path such as dependencies[0].ready.")
        if rule.get("op") not in OPERATORS or (rule["op"] != "exists" and not isinstance(rule.get("value"),str)):
            raise ValidationError("Choose equals, not_equals, contains, or exists and enter the comparison text.")
