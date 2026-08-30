"""Typer entry point for the ``webskrap`` command.

Subcommands cover installing browsers, listing profiles, checking the
install, and fetching pages; persistent-session commands live in
:mod:`webskrap.browser_cli` under ``webskrap browser``. This layer parses
arguments, formats output, and turns failures into one-line errors with a
non-zero exit code.
"""

from __future__ import annotations

import asyncio
import subprocess  # nosec B404  # noqa: S404 - fixed argv for browser installs, no shell
import sys
from pathlib import Path
from typing import Annotated, Any, NoReturn, TypedDict

import click
import typer
from rich.console import Console
from rich.table import Table

from webskrap.browser_cli import browser_app
from webskrap.cli_output import (
    OutputFormat,
    fail,
    parse_output_format,
    print_json,
    stderr_console,
)
from webskrap.client import WebSkrapClient
from webskrap.diagnostics import diagnose
from webskrap.errors import ErrorCode, WebSkrapError, first_line
from webskrap.models import (
    FetchResult,
    ResourcePolicy,
    SessionConfig,
    WaitUntil,
    WebRtcIPHandlingPolicy,
    shape_fetch_result,
)
from webskrap.parsing import (
    parse_wait_until,
    parse_webrtc_ip_handling_policy,
)
from webskrap.profiles import get_profile, list_profiles

app = typer.Typer(help="WebSkrap browser scraping toolkit.")
app.add_typer(browser_app, name="browser")
console = Console()


class InstallResult(TypedDict):
    """Outcome of one browser-install step."""

    ok: bool
    command: list[str]
    message: str


INSTALL_COMMANDS = (
    (sys.executable, "-m", "playwright", "install", "chromium"),
    (sys.executable, "-m", "patchright", "install", "chromium"),
)


@app.command("install")
def install_command(
    format: Annotated[
        str,
        typer.Option("--format", help="Output format: human or json."),
    ] = "human",
) -> None:
    """Download the Chromium builds Playwright and Patchright need."""
    output_format = parse_output_format(format)
    results = [_run_install_command(command) for command in INSTALL_COMMANDS]
    ok = all(result["ok"] for result in results)
    payload = {"ok": ok, "steps": results}
    if output_format == "json":
        print_json(payload)
    else:
        _print_install_result(results)
    if not ok:
        raise typer.Exit(code=1)


@app.command("profiles")
def profiles_command(
    format: Annotated[
        str,
        typer.Option("--format", help="Output format: human or json."),
    ] = "human",
) -> None:
    """List the bundled browser profiles."""
    output_format = parse_output_format(format)
    profiles = list_profiles()
    if output_format == "json":
        print_json({"profiles": [profile.model_dump(mode="json") for profile in profiles]})
        return

    table = Table(title="WebSkrap Profiles")
    table.add_column("Name")
    table.add_column("Viewport")
    table.add_column("Locale")
    table.add_column("Timezone")
    table.add_column("Mobile")

    for profile in profiles:
        table.add_row(
            profile.name,
            f"{profile.viewport.width}x{profile.viewport.height}",
            profile.locale,
            profile.timezone_id,
            "yes" if profile.is_mobile else "no",
        )

    console.print(table)


@app.command("schema")
def schema_command(
    format: Annotated[
        str,
        typer.Option("--format", help="Output format: json or human."),
    ] = "json",
) -> None:
    """Describe every command, option and default as one JSON document.

    Written for callers that generate invocations rather than read help text:
    one payload instead of a `--help` run per subcommand.
    """
    output_format = parse_output_format(format)
    schema = describe_command(typer.main.get_command(app), "webskrap")
    if output_format == "json":
        print_json(schema)
        return

    table = Table(title="WebSkrap Commands")
    table.add_column("Command")
    table.add_column("Summary")
    for path, summary in _walk_command_summaries(schema, ()):
        table.add_row(path, summary)
    console.print(table)


def _walk_command_summaries(
    schema: dict[str, Any], prefix: tuple[str, ...]
) -> list[tuple[str, str]]:
    """Flatten a schema into ``("browser open", "Start ...")`` rows."""
    path = (*prefix, str(schema["name"]))
    rows = [] if schema.get("commands") else [(" ".join(path[1:]) or path[0], str(schema["help"]))]
    for child in schema.get("commands", []):
        rows.extend(_walk_command_summaries(child, path))
    return rows


@app.command("doctor")
def doctor_command(
    format: Annotated[
        str,
        typer.Option("--format", help="Output format: human or json."),
    ] = "human",
) -> None:
    """Check that Patchright and Chromium are installed and can launch."""
    output_format = parse_output_format(format)
    result = asyncio.run(_doctor())
    if output_format == "json":
        print_json(result)
    else:
        _print_doctor_result(result)
    if not result["ok"]:
        raise typer.Exit(code=1)


async def _doctor() -> dict[str, Any]:
    return await diagnose()


@app.command("fetch")
def fetch_command(
    url: Annotated[str, typer.Argument(help="URL to fetch.")],
    profile: Annotated[
        str,
        typer.Option(
            "--profile",
            "-p",
            help="Bundled profile metadata (requires --patchright-context-profile).",
        ),
    ] = "desktop-chrome",
    channel: Annotated[
        str | None,
        typer.Option("--channel", help="Browser channel for headless Patchright stealth."),
    ] = "chrome",
    user_data_dir: Annotated[
        Path | None,
        typer.Option("--user-data-dir", help="Persistent browser profile directory."),
    ] = None,
    screenshot: Annotated[
        Path | None,
        typer.Option("--screenshot", help="Write a full-page screenshot to this path."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write fetched content to this file."),
    ] = None,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: human or json."),
    ] = "human",
    max_chars: Annotated[
        int,
        typer.Option("--max-chars", min=0, help="Maximum JSON text characters."),
    ] = 20_000,
    offset: Annotated[
        int,
        typer.Option(
            "--offset",
            min=0,
            help="Start JSON text at this character; pass back next_text_offset to continue.",
        ),
    ] = 0,
    stdout: Annotated[
        bool,
        typer.Option("--stdout", help="Write fetched content to stdout."),
    ] = False,
    text_only: Annotated[
        bool,
        typer.Option("--text-only", help="Return readable body text instead of HTML."),
    ] = False,
    links: Annotated[
        bool,
        typer.Option("--links", help="Also collect the page's outbound links."),
    ] = False,
    max_links: Annotated[
        int,
        typer.Option("--max-links", min=0, help="Maximum links to collect."),
    ] = 50,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", help="Suppress human summary output."),
    ] = False,
    wait_until: Annotated[
        str,
        typer.Option("--wait-until", help="commit, domcontentloaded, load, or networkidle."),
    ] = "domcontentloaded",
    timeout_ms: Annotated[
        float,
        typer.Option("--timeout-ms", min=1, help="Navigation timeout."),
    ] = 30_000,
    resource_policy: Annotated[
        ResourcePolicy,
        typer.Option("--resource-policy", help="Resource routing preset."),
    ] = ResourcePolicy.ALL,
    decline_cookies: Annotated[
        bool,
        typer.Option(
            "--decline-cookies/--no-decline-cookies",
            help="Click the reject button of a cookie consent notice after load.",
        ),
    ] = True,
    decline_cookies_timeout_ms: Annotated[
        float,
        typer.Option(
            "--decline-cookies-timeout-ms",
            min=0,
            help="How long to wait for a cookie consent notice to appear.",
        ),
    ] = 2_000,
    patchright_context_profile: Annotated[
        bool,
        typer.Option(
            "--patchright-context-profile",
            help="Apply locale/timezone/media profile metadata in Patchright contexts.",
        ),
    ] = False,
    reduce_fingerprint_surface: Annotated[
        bool,
        typer.Option(
            "--reduce-fingerprint-surface",
            help="Disable Chromium WebGL and canvas readback with native browser flags.",
        ),
    ] = False,
    mask_headless_user_agent: Annotated[
        bool,
        typer.Option(
            "--mask-headless-user-agent",
            help="Rewrite HeadlessChrome to Chrome via Chromium's user-agent flag.",
        ),
    ] = False,
    launch_args: Annotated[
        list[str] | None,
        typer.Option(
            "--launch-arg",
            help="Additional browser launch argument. Repeat for multiple args.",
        ),
    ] = None,
    webrtc_ip_handling_policy: Annotated[
        str | None,
        typer.Option(
            "--webrtc-ip-handling-policy",
            help=(
                "Chromium WebRTC IP policy: default, default_public_and_private_interfaces, "
                "default_public_interface_only, or disable_non_proxied_udp."
            ),
        ),
    ] = None,
) -> None:
    """Fetch a URL with the Patchright stealth driver and report the result."""
    asyncio.run(
        _fetch(
            url=url,
            profile=profile,
            channel=channel,
            user_data_dir=user_data_dir,
            screenshot=screenshot,
            output=output,
            output_format=output_format,
            max_chars=max_chars,
            offset=offset,
            stdout=stdout,
            text_only=text_only,
            links=links,
            max_links=max_links,
            quiet=quiet,
            wait_until=wait_until,
            timeout_ms=timeout_ms,
            resource_policy=resource_policy,
            decline_cookies=decline_cookies,
            decline_cookies_timeout_ms=decline_cookies_timeout_ms,
            patchright_context_profile=patchright_context_profile,
            reduce_fingerprint_surface=reduce_fingerprint_surface,
            mask_headless_user_agent=mask_headless_user_agent,
            launch_args=launch_args or [],
            webrtc_ip_handling_policy=webrtc_ip_handling_policy,
        )
    )


async def _fetch(
    *,
    url: str,
    profile: str,
    channel: str | None,
    user_data_dir: Path | None,
    screenshot: Path | None,
    output: Path | None,
    output_format: str,
    max_chars: int,
    offset: int,
    stdout: bool,
    text_only: bool,
    links: bool,
    max_links: int,
    quiet: bool,
    wait_until: str,
    timeout_ms: float,
    resource_policy: ResourcePolicy,
    decline_cookies: bool,
    decline_cookies_timeout_ms: float,
    patchright_context_profile: bool,
    reduce_fingerprint_surface: bool,
    mask_headless_user_agent: bool,
    launch_args: list[str],
    webrtc_ip_handling_policy: str | None,
) -> None:
    parsed_output_format = parse_output_format(output_format)
    selected_profile = get_profile(profile)
    config = SessionConfig(
        driver="patchright",
        headless=True,
        channel=channel,
        user_data_dir=user_data_dir,
        navigation_timeout_ms=timeout_ms,
        resource_policy=resource_policy,
        decline_cookies=decline_cookies,
        decline_cookies_timeout_ms=decline_cookies_timeout_ms,
        patchright_context_profile=patchright_context_profile,
        reduce_fingerprint_surface=reduce_fingerprint_surface,
        mask_headless_user_agent=mask_headless_user_agent,
        launch_args=launch_args,
        webrtc_ip_handling_policy=_parse_webrtc_ip_handling_policy(webrtc_ip_handling_policy),
    )

    try:
        result = await _fetch_with_channel_fallback(
            config,
            parsed_output_format,
            url=url,
            profile=selected_profile,
            wait_until=_parse_wait_until(wait_until),
            screenshot=screenshot or False,
            timeout_ms=timeout_ms,
            text_only=text_only,
            include_links=links,
            max_links=max_links,
        )
    except (typer.Exit, typer.Abort, typer.BadParameter):
        raise
    except Exception as exc:
        # A timeout, a refused host or an unwritable path leaves through the
        # same envelope as a launch failure, so `--format json` never has to
        # answer with Rich markup on stderr.
        fail(exc, parsed_output_format)

    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(result.text, encoding="utf-8")

    if parsed_output_format == "json":
        print_json(shape_fetch_result(result, max_chars, offset))
        return

    if stdout:
        typer.echo(result.text, nl=False)
        return

    if quiet:
        return

    console.print(f"[bold]Status:[/bold] {result.status}")
    console.print(f"[bold]Final URL:[/bold] {result.final_url}")
    console.print(f"[bold]Title:[/bold] {result.title}")
    if result.cookie_notice_declined:
        console.print(f"[bold]Cookie notice:[/bold] declined ({result.cookie_notice_declined})")
    if links:
        console.print(f"[bold]Links:[/bold] {len(result.links)} of {result.links_total}")
    if result.screenshot_path:
        console.print(f"[bold]Screenshot:[/bold] {result.screenshot_path}")
    if output:
        label = "Text" if text_only else "HTML"
        console.print(f"[bold]{label}:[/bold] {output}")


LAUNCH_FAILURE_MARKERS = (
    "executable doesn't exist",
    "is not found at",
    "playwright install",
    "failed to launch",
    "browsertype.launch",
)


def _is_launch_failure(exc: Exception) -> bool:
    return any(marker in str(exc).lower() for marker in LAUNCH_FAILURE_MARKERS)


def _fail_launch(exc: Exception, output_format: OutputFormat) -> NoReturn:
    """Report an unlaunchable browser the way `doctor` does, not as a traceback."""
    message = f"Browser did not launch: {first_line(exc)}"
    fail(WebSkrapError(message, code=ErrorCode.BROWSER_LAUNCH), output_format)


async def _run_fetch(config: SessionConfig, **kwargs: Any) -> FetchResult:
    async with WebSkrapClient() as client:
        return await client.fetch(config=config, **kwargs)


async def _fetch_with_channel_fallback(
    config: SessionConfig, output_format: OutputFormat, **kwargs: Any
) -> FetchResult:
    """Fetch, retrying on bundled chromium when the chosen channel cannot launch.

    The default channel is `chrome`, which does not exist on every platform
    (Linux ARM64 has no Chrome build). Falling back keeps `webskrap fetch`
    working there instead of dumping a Playwright traceback.
    """
    try:
        return await _run_fetch(config, **kwargs)
    except Exception as exc:
        if not _is_launch_failure(exc):
            raise
        if config.channel is None:
            _fail_launch(exc, output_format)
        stderr_console.print(
            f"[yellow]channel '{config.channel}' did not launch; retrying with chromium[/yellow]"
        )
        try:
            return await _run_fetch(config.model_copy(update={"channel": None}), **kwargs)
        except Exception as retry_exc:
            if not _is_launch_failure(retry_exc):
                raise
            _fail_launch(retry_exc, output_format)


def _parse_wait_until(value: str) -> WaitUntil:
    try:
        return parse_wait_until(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc).partition(" must ")[2]) from exc


def _parse_webrtc_ip_handling_policy(
    value: str | None,
) -> WebRtcIPHandlingPolicy | None:
    try:
        return parse_webrtc_ip_handling_policy(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc).partition(" must ")[2]) from exc


def _run_install_command(command: tuple[str, ...]) -> InstallResult:
    try:
        # argv is one of the INSTALL_COMMANDS constants, built from
        # sys.executable. No shell, and no caller-supplied argument.
        completed = subprocess.run(  # nosec B603  # noqa: S603
            command, capture_output=True, text=True, check=False
        )
    except OSError as exc:
        return {
            "ok": False,
            "command": list(command),
            "message": str(exc),
        }
    output = (completed.stdout or completed.stderr).strip()
    return {
        "ok": completed.returncode == 0,
        "command": list(command),
        "message": output,
    }


def _print_install_result(results: list[InstallResult]) -> None:
    for result in results:
        command = " ".join(str(part) for part in result["command"])
        if result["ok"]:
            console.print(f"[green]OK:[/green] {command}")
        else:
            console.print(f"[red]FAILED:[/red] {command}")
        if result["message"]:
            console.print(str(result["message"]))


def _print_doctor_result(result: dict[str, Any]) -> None:
    message = str(result["message"])
    if result["ok"]:
        console.print(f"[green]{message}[/green]")
    else:
        console.print("[yellow]Patchright is unavailable.[/yellow]")
        console.print(message)
        if hint := result.get("hint"):
            console.print(str(hint))
    _print_doctor_details(result)


def _print_doctor_details(result: dict[str, Any]) -> None:
    """Print the surrounding facts, skipping any a caller stubbed out."""
    if versions := result.get("versions"):
        installed = ", ".join(f"{name} {value or 'missing'}" for name, value in versions.items())
        console.print(f"[bold]Versions:[/bold] {installed}")
    if executable := result.get("executable_path"):
        console.print(f"[bold]Chromium:[/bold] {executable}")
    if paths := result.get("paths"):
        for label, path in paths.items():
            console.print(f"[bold]{label}:[/bold] {path}")
    if environment := result.get("environment"):
        overrides = {name: value for name, value in environment.items() if value is not None}
        console.print(f"[bold]Environment:[/bold] {overrides or 'defaults'}")
    if (sessions := result.get("sessions")) is not None:
        running = sum(1 for entry in sessions if entry["running"])
        console.print(f"[bold]Sessions:[/bold] {len(sessions)} ({running} running)")


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


def _describe_parameter(parameter: click.Parameter) -> dict[str, Any]:
    """Describe one option or positional argument.

    Read structurally rather than by class: Typer builds its commands from its
    own Click-compatible classes, which do not subclass ``click.Argument`` or
    ``click.Option``, so an isinstance check would silently call every
    parameter an option.
    """
    choices = getattr(parameter.type, "choices", None)
    return {
        "name": parameter.name,
        "kind": parameter.param_type_name,
        "flags": list(parameter.opts),
        "type": parameter.type.name,
        "choices": [str(choice) for choice in choices] if choices else None,
        "required": bool(parameter.required),
        "multiple": bool(parameter.multiple),
        "default": _json_safe(parameter.default),
        "help": getattr(parameter, "help", None),
    }


def describe_command(command: click.Command, name: str) -> dict[str, Any]:
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
