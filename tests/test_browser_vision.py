from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import ImageContent
from typer.testing import CliRunner

from webskrap import browser_session, cli, mcp_server
from webskrap.errors import EXIT_CODES, ErrorCode, WebSkrapError


@pytest.mark.parametrize(
    ("action", "x", "y", "options", "message"),
    [
        ("bad", 1, 2, {}, "mouse action"),
        ("click", -1, 2, {}, "non-negative"),
        ("click", float("nan"), 2, {}, "finite"),
        ("click", 1, float("inf"), {}, "finite"),
        ("click", 1, 2, {"button": "bad"}, "button"),
        ("move", 1, 2, {"button": "right"}, "only used"),
        ("scroll", 1, 2, {}, "non-zero"),
        ("scroll", 1, 2, {"delta_y": float("inf")}, "finite"),
        ("click", 1, 2, {"delta_y": 4}, "only used"),
        ("drag", 1, 2, {}, "requires"),
        ("drag", 1, 2, {"end_x": 3}, "requires"),
        ("drag", 1, 2, {"end_x": 3, "end_y": float("nan")}, "finite"),
        ("click", 1, 2, {"end_x": 3, "end_y": 4}, "only used"),
    ],
)
async def test_mcp_mouse_rejects_invalid_arguments_before_connecting(
    monkeypatch, action, x, y, options, message
) -> None:
    connect = AsyncMock()
    monkeypatch.setattr(browser_session, "run_page_action", connect)
    with pytest.raises(WebSkrapError, match=message) as caught:
        await mcp_server.browser_mouse(action, x, y, **options)
    assert caught.value.code is ErrorCode.USAGE
    connect.assert_not_called()


@pytest.mark.parametrize("point", [(100, 0), (0, 50), (-1, 0)])
async def test_outside_coordinates_send_no_mouse_input(point) -> None:
    mouse = SimpleNamespace(move=AsyncMock(), down=AsyncMock())
    page = SimpleNamespace(
        evaluate=AsyncMock(return_value={"width": 100, "height": 50}), mouse=mouse
    )
    with pytest.raises(WebSkrapError, match="outside the viewport"):
        await browser_session.mouse_action(page, "drag", 1, 2, end_x=point[0], end_y=point[1])
    mouse.move.assert_not_called()
    mouse.down.assert_not_called()


async def test_failed_drag_releases_mouse_button() -> None:
    mouse = SimpleNamespace(
        move=AsyncMock(side_effect=[None, RuntimeError("disconnected")]),
        down=AsyncMock(),
        up=AsyncMock(),
    )
    page = SimpleNamespace(
        evaluate=AsyncMock(return_value={"width": 100, "height": 50}), mouse=mouse
    )
    with pytest.raises(RuntimeError, match="disconnected"):
        await browser_session.mouse_action(page, "drag", 1, 2, end_x=3, end_y=4, button="right")
    mouse.up.assert_awaited_once_with(button="right")


@pytest.mark.parametrize(
    "arguments",
    [
        ["click", "nan", "2"],
        ["scroll", "1", "2"],
        ["drag", "1", "2", "--end-x", "4"],
    ],
)
def test_cli_mouse_reports_usage_as_json(arguments, tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli.app,
        ["browser", "mouse", *arguments, "--format", "json"],
        env={"WEBSKRAP_BROWSER_DIR": str(tmp_path)},
    )
    assert result.exit_code == EXIT_CODES[ErrorCode.USAGE], result.output
    assert json.loads(result.stdout)["code"] == "usage"


async def test_vision_tools_publish_effect_annotations() -> None:
    tools = {tool.name: tool for tool in await mcp_server.mcp.list_tools()}
    assert tools["browser_view"].annotations.read_only_hint is True
    assert tools["browser_view"].annotations.destructive_hint is False
    for name in ("browser_mouse", "browser_insert_text"):
        assert tools[name].annotations.read_only_hint is False
        assert tools[name].annotations.destructive_hint is True
        assert tools[name].annotations.open_world_hint is True


PAGE = """<html><title>Vision</title><body style='margin:0;height:3000px'>
<input id='input' style='position:absolute;left:20px;top:20px;width:200px;height:40px'>
<canvas id='canvas' width='200' height='100' style='position:absolute;left:20px;top:100px'></canvas>
<script>
window.events=[];
const canvas=document.querySelector('canvas');
canvas.getContext('2d').fillRect(0,0,200,100);
for (const type of ['click','dblclick','mousemove','mousedown','mouseup','wheel','input'])
  addEventListener(type,e=>events.push({type:e.type,trusted:e.isTrusted,buttons:e.buttons}),true);
</script></body></html>"""


@pytest.mark.browser
async def test_mcp_vision_protocol_and_persistent_interactions(
    persistent_session_env, monkeypatch
) -> None:
    name = "vision-protocol"
    output_root = persistent_session_env / "output"
    monkeypatch.setenv("WEBSKRAP_OUTPUT_DIR", str(output_root))
    await browser_session.open_session(name)

    async def prepare(page) -> None:
        await page.set_viewport_size({"width": 640, "height": 480})
        await page.set_content(PAGE)

    await browser_session.run_page_action(name, prepare)
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        server = asyncio.create_task(
            mcp_server.mcp._lowlevel_server.run(
                *server_streams, mcp_server.mcp._lowlevel_server.create_initialization_options()
            )
        )
        try:
            async with ClientSession(*client_streams) as client:
                await client.initialize()
                result = await client.call_tool("browser_view", {"session": name})
                assert not result.is_error
                images = [block for block in result.content if isinstance(block, ImageContent)]
                assert len(images) == 1
                assert images[0].mime_type == "image/png"
                png = base64.b64decode(images[0].data)
                assert png.startswith(b"\x89PNG\r\n\x1a\n")
                assert struct.unpack(">II", png[16:24]) == (640, 480)
                assert result.structured_content["width"] == 640
                assert result.structured_content["height"] == 480
                assert result.structured_content["coordinate_system"] == "viewport-css-pixels"
                assert not output_root.exists()
                for tool, args in (
                    ("browser_mouse", {"action": "click", "x": 40, "y": 40}),
                    ("browser_insert_text", {"text": "héllo 世界"}),
                    ("browser_mouse", {"action": "dblclick", "x": 50, "y": 130}),
                    ("browser_mouse", {"action": "move", "x": 70, "y": 140}),
                    (
                        "browser_mouse",
                        {"action": "drag", "x": 60, "y": 140, "end_x": 100, "end_y": 160},
                    ),
                    ("browser_mouse", {"action": "scroll", "x": 300, "y": 300, "delta_y": 500}),
                ):
                    result = await client.call_tool(tool, {"session": name, **args})
                    assert not result.is_error, result

                async def verify(page):
                    await page.wait_for_function("scrollY > 0")
                    assert await page.locator("#input").input_value() == "héllo 世界"
                    events = await browser_session.evaluate(page, "window.events")
                    assert all(event["trusted"] for event in events)
                    assert {"click", "dblclick", "input", "wheel"} <= {e["type"] for e in events}
                    assert any(e["type"] == "mousemove" and e["buttons"] == 1 for e in events)

                await browser_session.run_page_action(name, verify)
                result = await client.call_tool("browser_view", {"session": name})
                assert result.structured_content["scroll_y"] > 0
                error = await client.call_tool(
                    "browser_mouse", {"session": name, "action": "click", "x": 640, "y": 0}
                )
                assert error.is_error
                assert "outside the viewport" in error.content[0].text
        finally:
            server.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server
            browser_session.close_session(name, delete_data=True)


@pytest.mark.browser
def test_cli_view_and_focused_input(persistent_session_env, tmp_path) -> None:
    name = "vision-cli"

    async def prepare():
        await browser_session.open_session(name)

        async def content(page):
            await page.set_viewport_size({"width": 640, "height": 480})
            await page.set_content(PAGE)

        await browser_session.run_page_action(name, content)

    asyncio.run(prepare())
    runner = CliRunner()
    try:
        path = tmp_path / "frames" / "viewport.png"
        result = runner.invoke(
            cli.app, ["browser", "view", str(path), "-s", name, "--format", "json"]
        )
        assert result.exit_code == 0, result.output
        metadata = json.loads(result.stdout)
        assert Path(metadata["path"]) == path
        assert struct.unpack(">II", path.read_bytes()[16:24]) == (
            metadata["width"],
            metadata["height"],
        )
        for command in (["mouse", "click", "40", "40"], ["insert-text", "Enter 世界"]):
            result = runner.invoke(cli.app, ["browser", *command, "-s", name, "--format", "json"])
            assert result.exit_code == 0, result.output
            assert json.loads(result.stdout)["title"] == "Vision"

        async def value(page):
            return await page.locator("#input").input_value()

        assert asyncio.run(browser_session.run_page_action(name, value)) == "Enter 世界"
    finally:
        browser_session.close_session(name, delete_data=True)


@pytest.mark.browser
async def test_view_uses_css_pixels_on_a_high_dpi_page(sandbox_supported) -> None:
    from patchright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(chromium_sandbox=sandbox_supported)
        try:
            context = await browser.new_context(
                viewport={"width": 320, "height": 240}, device_scale_factor=2
            )
            page = await context.new_page()
            await page.set_content(PAGE)
            captured = await browser_session.view(page)
            assert await page.evaluate("devicePixelRatio") == 2
            assert (captured.width, captured.height) == (320, 240)
            assert struct.unpack(">II", captured.image[16:24]) == (320, 240)
            await browser_session.mouse_action(page, "click", 40, 40)
            await browser_session.insert_text(page, "css pixels")
            assert await page.locator("#input").input_value() == "css pixels"
        finally:
            await browser.close()
