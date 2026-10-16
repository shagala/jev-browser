"""Resolve a recorded descriptor to exactly one live observed action.

Ambiguity is never resolved by guessing. Two candidates where one was recorded means the
page changed, and picking the first is how a recipe silently drifts onto a wrong path.
"""

import re
import unicodedata

INVISIBLE = dict.fromkeys(map(ord, "​‌‍﻿"))
# Google appends live state to accessible names: "$745, Oct 3 to Oct 10, selected".
STATE_SUFFIX = re.compile(r",\s*(selected|checked|expanded|collapsed|current)\s*$", re.I)


class MatchError(ValueError):
    """A descriptor did not resolve. Carries the failure class the ledger records."""

    kind = "no_match"


class NoMatch(MatchError):
    kind = "no_match"


class Ambiguous(MatchError):
    kind = "ambiguous"


class Unreliable(MatchError):
    """The observation was truncated, so a miss proves nothing."""

    kind = "unreliable"


def normalize(label):
    text = unicodedata.normalize("NFKC", label or "").translate(INVISIBLE)
    text = " ".join(text.replace(" ", " ").split())
    return STATE_SUFFIX.sub("", text).strip()


def labels_match(observed, wanted, mode):
    if wanted is None:
        return True
    if mode == "exact":
        return observed == wanted
    observed, wanted = normalize(observed), normalize(wanted)
    return wanted in observed if mode == "contains" else observed == wanted


def candidates(page, descriptor):
    found = []
    for action in page["actions"]:
        if action["kind"] != descriptor.kind:
            continue
        if descriptor.role is not None and action.get("role") != descriptor.role:
            continue
        if labels_match(action.get("label", ""), descriptor.label, descriptor.mode):
            found.append(action)
    return found


def resolve(page, descriptor):
    """Return the one live action this descriptor names, or raise with a failure class."""
    found = candidates(page, descriptor)
    if not found:
        if page.get("omitted_actions"):
            raise Unreliable(
                f"{descriptor.label!r} not found, but {page['omitted_actions']} actions were truncated"
            )
        raise NoMatch(f"no action matches {descriptor.label!r} ({descriptor.kind})")
    if len(found) != descriptor.expected_candidates:
        raise Ambiguous(
            f"ambiguous:{len(found)} for {descriptor.label!r}; recorded {descriptor.expected_candidates}"
        )
    return found[descriptor.ordinal]
