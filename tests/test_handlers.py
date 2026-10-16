"""A recipe names code; it never carries any. Offline."""

import pytest

from usecases import handlers


def test_resolves_a_function_in_the_package():
    assert handlers.resolve("usecases.flights_grid:param_sets") is not None


def test_a_reference_without_a_function_is_rejected():
    with pytest.raises(ValueError, match="module>:<function"):
        handlers.resolve("usecases.flights_grid")


def test_a_module_outside_the_package_is_rejected():
    # Structural, not conventional: a recipe must not be able to name arbitrary code.
    with pytest.raises(ValueError, match="under the 'usecases' package"):
        handlers.resolve("os:system")


def test_a_lookalike_prefix_is_rejected():
    with pytest.raises(ValueError, match="under the 'usecases' package"):
        handlers.resolve("usecases_evil:run")
