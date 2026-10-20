"""The public API is fully annotated: every parameter and every result of what the package exports (see ``__all__``)
and of the helpers of ``cypher_guardrail.utils``. The linter cannot say this: the modules also hold private code that
is not annotated yet, and the linter can only be told to leave out a whole file."""

import inspect
import typing

import pytest

import cypher_guardrail
from cypher_guardrail import utils

DUNDERS = {"__init__", "__enter__", "__exit__", "__aenter__", "__aexit__"}


def _callables():
    for name in cypher_guardrail.__all__:
        exported = getattr(cypher_guardrail, name)
        if not inspect.isclass(exported):
            continue
        for member, value in inspect.getmembers(exported):
            if member.startswith("_") and member not in DUNDERS:
                continue
            if isinstance(value, property):
                value = value.fget
            if inspect.isfunction(value) or inspect.ismethod(value):
                yield pytest.param(value, id=f"{name}.{member}")
    for helper in (
        utils.is_write,
        utils.is_write_async,
        utils.get_paths_after_inference,
        utils.get_paths_after_inference_async,
    ):
        yield pytest.param(helper, id=f"utils.{helper.__name__}")


@pytest.mark.parametrize("function", list(_callables()))
def test_every_parameter_and_the_result_are_annotated(function):
    signature = inspect.signature(function)
    unannotated = [
        name
        for name, parameter in signature.parameters.items()
        if name not in ("self", "cls")
        and parameter.annotation is inspect.Parameter.empty
    ]

    assert unannotated == [], f"{function.__qualname__}: parameters without annotation"
    assert signature.return_annotation is not inspect.Signature.empty, (
        f"{function.__qualname__}: no return annotation"
    )
    assert typing.get_type_hints(function)  # the annotations can be resolved
