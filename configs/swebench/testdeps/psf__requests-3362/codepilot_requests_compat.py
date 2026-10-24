"""Compatibility shim for the requests-2.10-era test suite under modern pytest.

``tests/test_utils.py`` contains a parametrize entry of the form::

    pytest.mark.skipif('cStringIO is None')((cStringIO, 'Test'))

Modern pytest treats the returned ``MarkDecorator`` as one parameter set and
raises ``TypeError: object of type 'MarkDecorator' has no len()``.  The intent
is unambiguous: the tuple is the parameter set and the skipif mark applies to
it.  This plugin rewrites *only* that malformed shape before pytest validates
parameter lengths.  It does not change any test body, assertion or parameter
value, and it is scoped to the instance image that needs it.
"""

from __future__ import annotations

from typing import Any

import pytest
from _pytest.mark.structures import MarkDecorator, ParameterSet

_ORIGINAL = ParameterSet._for_parametrize


def _normalize_argvalues(argvalues: Any) -> Any:
    if not isinstance(argvalues, (list, tuple)):
        return argvalues
    fixed: list[Any] = []
    changed = False
    for item in argvalues:
        if isinstance(item, MarkDecorator):
            mark = item.mark
            if mark.name == "skipif" and len(mark.args) == 2 and isinstance(
                mark.args[1], (tuple, list)
            ):
                expression, values = mark.args
                fixed.append(
                    pytest.param(*values, marks=[pytest.mark.skipif(expression)])
                )
                changed = True
                continue
            raise TypeError(
                f"unsupported parametrize MarkDecorator value: {mark.name}"
            )
        fixed.append(item)
    return fixed if changed else argvalues


def _patched(argnames: Any, argvalues: Any, *args: Any, **kwargs: Any) -> Any:
    return _ORIGINAL(argnames, _normalize_argvalues(argvalues), *args, **kwargs)


ParameterSet._for_parametrize = staticmethod(_patched)
