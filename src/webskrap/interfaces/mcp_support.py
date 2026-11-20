"""SDK imports, annotations, and error handling shared by MCP tools."""

from collections.abc import Iterator
from contextlib import contextmanager

from webskrap.errors import ErrorCode, WebSkrapError, classify, tool_message

try:
    from mcp.server.mcpserver import Context as Context
    from mcp.server.mcpserver import MCPServer as MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import CallToolResult as CallToolResult
    from mcp.types import ImageContent as ImageContent
    from mcp.types import TextContent as TextContent
    from mcp.types import ToolAnnotations
except ImportError as exc:
    msg = "the MCP server requires mcp. Run: pip install webskrap"
    raise WebSkrapError(msg, code=ErrorCode.BROWSER_LAUNCH) from exc


class _AnnotatedError(WebSkrapError, ToolError):
    """A failure whose message already carries its code and recovery hint.

    Marks an error as handled so a tool that guards several steps does not
    append the same hint twice. ToolError preserves the message over MCP;
    the SDK hides messages from other exception types as unexpected failures.
    """


@contextmanager
def _tool_errors() -> Iterator[None]:
    """Re-raise whatever fails inside as a one-line, code-tagged message.

    Browser errors can arrive with a log dozens of lines long, and none of
    those lines says what to do next. What reaches the caller is the first line
    plus the classification and its hint.
    """
    try:
        yield
    except _AnnotatedError:
        raise
    except Exception as exc:
        raise _AnnotatedError(tool_message(exc), code=classify(exc)) from exc


def _hints(
    *,
    read_only: bool,
    destructive: bool = False,
    idempotent: bool = False,
    open_world: bool = False,
) -> ToolAnnotations:
    """Describe a tool's effects so a client can decide what to confirm.

    Without these, closing a session with delete_data (which throws away
    cookies and logins) looks exactly like listing sessions. Interaction tools
    are marked destructive because a click or an Enter submits forms on a site
    WebSkrap does not own, and that is not undoable from here.

    These are hints, not enforcement: the confinement and sandbox rules that
    actually constrain the tools live in :mod:`webskrap.security.paths` and
    :mod:`webskrap.browser.browser_session`.
    """
    return ToolAnnotations(
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        open_world_hint=open_world,
    )
