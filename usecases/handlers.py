"""Resolve the code a recipe hands work back to.

A recipe never carries executable code — AGENTS.md forbids the model emitting any. It
carries the dotted name of a function in this package, supplied at record time by whoever
recorded it. Restricting the module prefix makes that structural rather than a convention.
"""

import importlib

PACKAGE = __package__


def resolve(reference):
    module_name, separator, attribute = reference.partition(":")
    if not separator or not attribute:
        raise ValueError(f"handler {reference!r} must be '<module>:<function>'")
    if module_name != PACKAGE and not module_name.startswith(f"{PACKAGE}."):
        raise ValueError(f"handler {reference!r} must live under the {PACKAGE!r} package")
    return getattr(importlib.import_module(module_name), attribute)
