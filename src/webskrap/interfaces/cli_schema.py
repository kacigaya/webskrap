"""Serialize the CLI command tree for agent clients."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol, get_args, get_origin


def _json_safe(value: Any) -> Any:
    """Reduce a click default to something json.dumps accepts.

    Defaults reach here as enum members, Paths and tuples as often as as plain
    scalars, and a schema that cannot be serialized is no schema at all.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    return str(value)


class _Parameter(Protocol):
    """The part of a Click parameter :func:`describe_command` reads."""

    @property
    def name(self) -> str | None: ...

    @property
    def opts(self) -> Sequence[str]: ...

    @property
    def param_type_name(self) -> str: ...

    @property
    def required(self) -> bool: ...

    @property
    def multiple(self) -> bool: ...

    @property
    def default(self) -> object: ...


class _Command(Protocol):
    """The part of a Click command :func:`describe_command` reads.

    A structural type, not ``click.Command``: Typer builds its commands from
    its own Click-compatible classes, which are private and do not subclass
    Click's. Naming what is actually read keeps the check honest without
    reaching into ``typer._click``. Every member is read-only, which is both
    true of this use and what keeps the sequences covariant.
    """

    @property
    def help(self) -> str | None: ...

    @property
    def short_help(self) -> str | None: ...

    @property
    def params(self) -> Sequence[_Parameter]: ...


def _describe_parameter(parameter: _Parameter) -> dict[str, Any]:
    """Describe one option or positional argument.

    ``param_type_name`` is read rather than the parameter's class, because
    Typer's parameters do not subclass ``click.Argument`` or ``click.Option``
    and an isinstance check would silently call every parameter an option.
    Support both legacy parameter types and Typer 0.29 type descriptors.
    """
    parameter_type = getattr(parameter, "type", None)
    if parameter_type is not None:
        choices = getattr(parameter_type, "choices", None)
        type_name = parameter_type.name
    else:
        descriptor = getattr(parameter, "type_descriptor", None)
        if descriptor is None:
            raise TypeError("CLI parameter has no type or type descriptor")
        choices = descriptor.choices
        annotation = descriptor.annotation
        if get_origin(annotation) is list:
            annotation = get_args(annotation)[0]
        type_name = (
            "choice"
            if choices
            else {
                str: "str",
                int: "int",
                bool: "boolean",
                float: "float",
                Path: "path",
            }.get(annotation, getattr(annotation, "__name__", "text"))
        )
        if annotation in (int, float) and any(
            getattr(parameter, bound, None) is not None for bound in ("min", "max")
        ):
            type_name += " range"
    return {
        "name": parameter.name,
        "kind": parameter.param_type_name,
        "flags": list(parameter.opts),
        "type": type_name,
        "choices": [str(choice) for choice in choices] if choices else None,
        "required": bool(parameter.required),
        "multiple": bool(parameter.multiple),
        "default": _json_safe(parameter.default),
        "help": getattr(parameter, "help", None),
    }


def describe_command(command: _Command, name: str) -> dict[str, Any]:
    """Describe ``command`` and, for a group, everything under it.

    The auto-generated ``--help`` flag is left out: it is on every command and
    tells a caller nothing about that one.
    """
    described: dict[str, Any] = {
        "name": name,
        "help": (command.help or command.short_help or "").strip().split("\n\n")[0].strip(),
        "parameters": [
            _describe_parameter(parameter)
            for parameter in command.params
            if parameter.name != "help"
        ],
    }
    if children := getattr(command, "commands", None):
        described["commands"] = [
            describe_command(child, child_name) for child_name, child in sorted(children.items())
        ]
    return described
