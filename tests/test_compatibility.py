from importlib import import_module

import pytest


@pytest.mark.parametrize(
    ("legacy", "current", "name"),
    [
        ("webskrap", "webskrap.scraping.client", "WebSkrapClient"),
        ("webskrap.client", "webskrap.scraping.session", "WebSkrapSession"),
        ("webskrap.models", "webskrap.config.models", "SessionConfig"),
        ("webskrap.models", "webskrap.scraping.results", "FetchResult"),
        ("webskrap.models", "webskrap.driver.text_search", "text_window"),
        ("webskrap.cdp", "webskrap.driver.cdp", "CDPPage"),
        ("webskrap.cdp", "webskrap.driver.transport", "WebSocketTransport"),
        ("webskrap.cdp", "webskrap.browser.executable", "find_executable"),
        ("webskrap.cdp_dom", "webskrap.driver.snapshot", "snapshot"),
        ("webskrap.cdp_session", "webskrap.browser.navigation", "goto"),
        ("webskrap.browser_session", "webskrap.browser.actions", "BrowserView"),
        ("webskrap.cli", "webskrap.interfaces.cli", "app"),
        ("webskrap.mcp_server", "webskrap.interfaces.mcp_server", "mcp"),
        ("webskrap.mcp_server", "webskrap.interfaces.mcp_browser", "browser_open"),
        ("webskrap.paths", "webskrap.security.paths", "resolve_output_path"),
        ("webskrap.urls", "webskrap.security.urls", "validate_mcp_url"),
    ],
)
def test_legacy_imports_resolve_to_current_objects(legacy, current, name):
    assert getattr(import_module(legacy), name) is getattr(import_module(current), name)
