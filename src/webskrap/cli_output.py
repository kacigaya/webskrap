"""How the CLI says things, shared by ``webskrap`` and ``webskrap browser``.

Success has always been printable two ways -- a Rich summary for a person, one
JSON line for everything else -- but failure was only ever printed for a
person. A script running ``--format json`` got a parseable line when the
command worked and Rich markup on stderr when it did not.

:func:`fail` closes that: the same ``{"ok", "error", "code", "hint"}`` envelope
the MCP tools report, on stdout, in JSON mode. The process exit status comes
from :data:`~webskrap.errors.EXIT_CODES`, so a caller can branch on the kind of
failure without reading the message.

Exit statuses describe *raised* failures. A command that ran to completion and
is reporting a negative result -- ``doctor`` on a host with no browser -- exits
1 and says so in its own payload.
"""

from __future__ import annotations

import json
from typing import Any, Literal, NoReturn

import typer
from rich.console import Console

from webskrap.errors import error_payload, exit_code

OutputFormat = Literal["human", "json"]

stderr_console = Console(stderr=True, highlight=False)


def parse_output_format(value: str) -> OutputFormat:
    """Validate a ``--format`` value.

    Raises:
        typer.BadParameter: If ``value`` is neither ``human`` nor ``json``.
    """
    if value not in ("human", "json"):
        raise typer.BadParameter("must be one of: human, json")
    return value


def print_json(payload: object) -> None:
    """Print one JSON line to stdout, leaving non-ASCII characters intact."""
    typer.echo(json.dumps(payload, ensure_ascii=False))


def fail(error: BaseException, output_format: OutputFormat = "human") -> NoReturn:
    """Report ``error`` in the requested format and exit with its status.

    Raises:
        typer.Exit: Always, with the status :data:`~webskrap.errors.EXIT_CODES`
            gives the error's classification.
    """
    payload: dict[str, Any] = dict(error_payload(error))
    if output_format == "json":
        print_json(payload)
    else:
        stderr_console.print(f"[red]{payload['error']}[/red]")
        stderr_console.print(str(payload["hint"]))
    raise typer.Exit(code=exit_code(error))
