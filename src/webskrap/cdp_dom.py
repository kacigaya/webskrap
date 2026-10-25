"""Snapshot refs and element actions for sessions on the CDP driver.

:func:`snapshot` walks the DOM in an isolated world, as Playwright's injected
script does for its aria snapshot, and lists elements by ARIA role and
accessible name. Each listed element gets a ref ``e<backendNodeId>``:
Chromium's id for the node, which stays the same across connections while
the node lives and is never written into the page. Iframe content is listed
under its ``iframe`` line, with refs ``f<key>e<backendNodeId>``: ``key`` is
:func:`frame_key` of the frame id, so a ref names its frame on any later
connection too. The Accessibility domain is not used; see
docs/design/cdp-driver.md for the measurement behind that.

The element actions resolve a ref or a CSS selector to one visible element
and drive it with trusted input: the human cursor path, button hold and
keystroke timing of :mod:`webskrap.human`, sent as ``Input.dispatch*``
events. Element reads and writes run in an isolated world.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import zlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from webskrap.cdp import CDPError, CDPPage
from webskrap.cdp_session import key_events
from webskrap.errors import ErrorCode, WebSkrapError
from webskrap.human import MAX_WHEEL_BATCHES, VIEW_MARGIN, bezier_path, click_point, uniform

#: What a ref looks like: an optional ``f`` and frame key for iframe content,
#: then ``e`` and Chromium's backend node id.
REF_PATTERN = re.compile(r"^(?:f(\d+))?e(\d+)$")
_POLL_S = 0.1

# Builds the snapshot text with "[ref=#N]" placeholders and returns it with
# the listed elements, so the caller can swap each placeholder for the
# element's backend node id. Roles and names follow the ARIA implicit-role
# table and the accessible-name rules for the elements pages commonly use.
_SNAPSHOT_SCRIPT = r"""(maxDepth) => {
  const elements = [];
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'HEAD', 'META', 'LINK']);
  const LEAF = new Set(['button', 'link', 'heading', 'textbox', 'searchbox', 'checkbox',
    'radio', 'switch', 'img', 'option', 'combobox', 'slider', 'spinbutton', 'tab',
    'menuitem', 'menuitemcheckbox', 'menuitemradio', 'treeitem', 'progressbar', 'meter',
    'separator']);
  const FROM_CONTENT = new Set(['button', 'link', 'heading', 'cell', 'columnheader',
    'rowheader', 'option', 'tab', 'menuitem', 'menuitemcheckbox', 'menuitemradio',
    'treeitem', 'checkbox', 'radio', 'switch', 'tooltip']);
  const norm = (text) => (text || '').replace(/\s+/g, ' ').trim();
  const clip = (text) => text.length > 200 ? text.slice(0, 199) + '…' : text;
  const quote = (text) => JSON.stringify(text);
  const inLandmark = (e) => !!e.parentElement && !!e.parentElement.closest(
    'article, aside, main, nav, section');
  const inputRole = (e) => {
    const type = (e.getAttribute('type') || 'text').toLowerCase();
    if (type === 'hidden') return null;
    if (['button', 'submit', 'reset', 'image', 'file'].includes(type)) return 'button';
    if (type === 'checkbox') return 'checkbox';
    if (type === 'radio') return 'radio';
    if (type === 'range') return 'slider';
    if (type === 'number') return 'spinbutton';
    if (type === 'search') return e.hasAttribute('list') ? 'combobox' : 'searchbox';
    return e.hasAttribute('list') ? 'combobox' : 'textbox';
  };
  const role = (e) => {
    const explicit = norm(e.getAttribute('role')).split(' ')[0];
    if (explicit) return explicit;
    const tag = e.tagName;
    switch (tag) {
      case 'A': case 'AREA': return e.hasAttribute('href') ? 'link' : null;
      case 'ARTICLE': return 'article';
      case 'ASIDE': return 'complementary';
      case 'BLOCKQUOTE': return 'blockquote';
      case 'BUTTON': case 'SUMMARY': return 'button';
      case 'CODE': return 'code';
      case 'DD': return 'definition';
      case 'DT': return 'term';
      case 'DETAILS': case 'FIELDSET': return 'group';
      case 'DIALOG': return 'dialog';
      case 'FIGURE': return 'figure';
      case 'FOOTER': return inLandmark(e) ? null : 'contentinfo';
      case 'FORM': return name(e, 'form') ? 'form' : null;
      case 'H1': case 'H2': case 'H3': case 'H4': case 'H5': case 'H6': return 'heading';
      case 'HEADER': return inLandmark(e) ? null : 'banner';
      case 'HR': return 'separator';
      case 'IFRAME': return 'iframe';
      case 'IMG': return e.getAttribute('alt') === '' ? null : 'img';
      case 'INPUT': return inputRole(e);
      case 'LI': return 'listitem';
      case 'MAIN': return 'main';
      case 'METER': return 'meter';
      case 'NAV': return 'navigation';
      case 'OL': case 'UL': case 'MENU': return 'list';
      case 'OPTION': return 'option';
      case 'OUTPUT': return 'status';
      case 'P': return 'paragraph';
      case 'PROGRESS': return 'progressbar';
      case 'SECTION': return name(e, 'region') ? 'region' : null;
      case 'SELECT': return e.multiple || e.size > 1 ? 'listbox' : 'combobox';
      case 'TABLE': return 'table';
      case 'TBODY': case 'THEAD': case 'TFOOT': return 'rowgroup';
      case 'TD': return 'cell';
      case 'TH': return e.getAttribute('scope') === 'row' ? 'rowheader' : 'columnheader';
      case 'TEXTAREA': return 'textbox';
      case 'TR': return 'row';
      default: return null;
    }
  };
  // Name from content: visible text, plus image alt text and aria-labels of
  // descendants, which innerText leaves out (a link wrapping a logo).
  const contentText = (node) => {
    if (node.nodeType === Node.TEXT_NODE) return node.textContent;
    if (node.nodeType !== Node.ELEMENT_NODE || SKIP.has(node.tagName) || hidden(node)) return '';
    const label = norm(node.getAttribute('aria-label'));
    if (label) return ' ' + label + ' ';
    if (node.tagName === 'IMG') return ' ' + norm(node.getAttribute('alt')) + ' ';
    const inner = childrenOf(node).map(contentText).join('');
    const display = getComputedStyle(node).display;
    return display === 'inline' ? inner : ' ' + inner + ' ';
  };
  const visibleText = (e) => norm(contentText(e));
  const name = (e, r) => {
    const labelledBy = norm(e.getAttribute('aria-labelledby'));
    if (labelledBy) {
      const text = labelledBy.split(' ')
        .map((id) => document.getElementById(id))
        .filter(Boolean).map((el) => norm(el.textContent)).join(' ');
      if (text) return clip(text);
    }
    const label = norm(e.getAttribute('aria-label'));
    if (label) return clip(label);
    const tag = e.tagName;
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') {
      const type = (e.getAttribute('type') || '').toLowerCase();
      if (tag === 'INPUT' && ['button', 'submit', 'reset'].includes(type))
        return clip(e.value || (type === 'submit' ? 'Submit' : type === 'reset' ? 'Reset' : ''));
      if (tag === 'INPUT' && type === 'image') return clip(norm(e.getAttribute('alt')));
      const labels = e.labels ? [...e.labels].map((l) => norm(l.innerText)).join(' ') : '';
      if (labels) return clip(labels);
    }
    if (tag === 'IMG' || tag === 'AREA') {
      const alt = norm(e.getAttribute('alt'));
      if (alt) return clip(alt);
    }
    if (tag === 'FIELDSET') {
      const legend = e.querySelector(':scope > legend');
      if (legend) return clip(norm(legend.innerText));
    }
    if (tag === 'TABLE') {
      const caption = e.querySelector(':scope > caption');
      if (caption) return clip(norm(caption.innerText));
    }
    if (FROM_CONTENT.has(r)) {
      const text = visibleText(e);
      if (text) return clip(text);
    }
    const title = norm(e.getAttribute('title'));
    if (title) return clip(title);
    return clip(norm(e.getAttribute('placeholder')));
  };
  const states = (e, r) => {
    const out = [];
    const ariaChecked = e.getAttribute('aria-checked');
    if (['checkbox', 'radio', 'switch', 'menuitemcheckbox', 'menuitemradio'].includes(r)) {
      if (ariaChecked === 'mixed' || e.indeterminate) out.push('checked=mixed');
      else if (ariaChecked === 'true' || (ariaChecked === null && e.checked)) out.push('checked');
    }
    if (e.disabled || e.getAttribute('aria-disabled') === 'true') out.push('disabled');
    const expanded = e.getAttribute('aria-expanded');
    if (expanded === 'true') out.push('expanded');
    if (e.tagName === 'DETAILS' && e.open) out.push('expanded');
    if (e.getAttribute('aria-pressed') === 'true') out.push('pressed');
    if (r === 'option' ? e.selected : e.getAttribute('aria-selected') === 'true')
      out.push('selected');
    if (r === 'heading') {
      const level = Number(e.getAttribute('aria-level')) || Number(e.tagName.slice(1)) || 2;
      out.push('level=' + level);
    }
    if (e === document.activeElement && e !== document.body) out.push('active');
    return out.map((s) => ' [' + s + ']').join('');
  };
  const value = (e, r) => {
    if (['textbox', 'searchbox', 'spinbutton', 'slider'].includes(r) && 'value' in e)
      return norm(e.value);
    if (r === 'combobox' && e.tagName === 'SELECT')
      return norm(e.selectedOptions[0] ? e.selectedOptions[0].text : '');
    if (r === 'combobox' && 'value' in e) return norm(e.value);
    return '';
  };
  const childrenOf = (node) => {
    if (node.shadowRoot) return [...node.shadowRoot.childNodes];
    if (node.tagName === 'SLOT') {
      const assigned = node.assignedNodes({ flatten: true });
      if (assigned.length) return assigned;
    }
    return [...node.childNodes];
  };
  const hidden = (e) => {
    if (e.hidden || e.getAttribute('aria-hidden') === 'true') return true;
    const style = getComputedStyle(e);
    return style.display === 'none';
  };
  // Adjacent text pieces at one depth read as one line, as on screen.
  const merge = (items) => {
    const out = [];
    for (const item of items) {
      const last = out[out.length - 1];
      if (item.text !== undefined && last && last.text !== undefined && last.depth === item.depth) {
        last.text += ' ' + item.text;
      } else {
        out.push({ ...item });
      }
    }
    return out;
  };
  // Returns lines for the node's subtree at the given depth.
  const walk = (node, depth) => {
    if (node.nodeType === Node.TEXT_NODE) {
      // visibility is inherited and a child may set it back to visible, so
      // text counts only where its own parent is visible.
      const parent = node.parentElement;
      if (parent && getComputedStyle(parent).visibility !== 'visible') return [];
      const text = norm(node.textContent);
      return text ? [{ text, depth }] : [];
    }
    if (node.nodeType !== Node.ELEMENT_NODE || SKIP.has(node.tagName) || hidden(node)) return [];
    const r = role(node);
    const invisible = getComputedStyle(node).visibility === 'hidden';
    if (!r || r === 'presentation' || r === 'none' || r === 'generic' || invisible) {
      return childrenOf(node).flatMap((child) => walk(child, depth));
    }
    if (maxDepth !== null && depth >= maxDepth) return [];
    const index = elements.push(node) - 1;
    const label = name(node, r);
    let line = '- ' + r + (label ? ' ' + quote(label) : '') + states(node, r);
    line += ' [ref=#' + index + ']';
    const own = value(node, r);
    if (LEAF.has(r) && r !== 'link') {
      return [{ line: own ? line + ': ' + quote(own) : line, depth }];
    }
    let children = merge(childrenOf(node).flatMap((child) => walk(child, depth + 1)));
    if (r === 'link') {
      // As in Playwright's snapshot: where the link goes, then any content
      // that says more than its name.
      children = children.filter((child) => child.text === undefined || child.text !== label);
      const href = node.getAttribute('href');
      const room = maxDepth === null || depth + 1 < maxDepth;
      if (href !== null && room) children.unshift({ line: '- /url: ' + href, depth: depth + 1 });
    }
    if (children.length === 1 && children[0].text !== undefined) {
      if (children[0].text !== label) line += ': ' + children[0].text;
      return [{ line, depth }];
    }
    if (children.length) line += ':';
    return [{ line, depth }, ...children];
  };
  const root = document.body || document.documentElement;
  const lines = merge(root ? childrenOf(root).flatMap((child) => walk(child, 0)) : []);
  const text = lines.map((item) => '  '.repeat(item.depth)
    + (item.text !== undefined ? '- text: ' + item.text : item.line)).join('\n');
  return [text, ...elements];
}"""

# Visibility as Playwright's actionability check has it: a non-empty box and
# not visibility:hidden. Also reports whether the node is still connected.
_STATE_FUNCTION = """function () {
  if (!this.isConnected) return 'detached';
  const box = this.getBoundingClientRect();
  const visible = box.width > 0 && box.height > 0
    && getComputedStyle(this).visibility !== 'hidden';
  return visible ? 'visible' : 'hidden';
}"""
_BOX_FUNCTION = """function () {
  const r = this.getBoundingClientRect();
  return { x: r.left, y: r.top, width: r.width, height: r.height,
           viewportHeight: innerHeight };
}"""
# Same hit test as human._HIT_TEST: dispatches nothing, unlike a trial click.
_HIT_TEST_FUNCTION = """function (dx, dy) {
  const rect = this.getBoundingClientRect();
  const root = this.getRootNode();
  const scope = typeof root.elementFromPoint === 'function' ? root : document;
  const hit = scope.elementFromPoint(rect.left + dx, rect.top + dy);
  return !!hit && (hit === this || this.contains(hit));
}"""
# Playwright's enabled check: not :disabled (which covers disabled fieldsets)
# and not inside an aria-disabled subtree.
_ENABLED_FUNCTION = """function () {
  return !this.matches(':disabled') && !this.closest('[aria-disabled="true"]');
}"""
_SCROLL_FUNCTION = """function () { this.scrollIntoView({ block: 'center', inline: 'center' }); }"""
# Playwright's check/uncheck refuse anything but a checkbox or radio, before
# any click: a native input, or an ARIA checkbox, radio or switch.
_CHECKED_FUNCTION = """function () {
  const type = (this.getAttribute('type') || '').toLowerCase();
  if (this.tagName === 'INPUT' && (type === 'checkbox' || type === 'radio')) return this.checked;
  const role = (this.getAttribute('role') || '').trim().split(/\\s+/)[0];
  if (['checkbox', 'radio', 'switch', 'menuitemcheckbox', 'menuitemradio'].includes(role))
    return this.getAttribute('aria-checked') === 'true';
  return 'not-checkable';
}"""
# Playwright's fill: focus the field and select what it holds, so the text
# that follows replaces it. Non-editable targets are refused.
_PREPARE_FILL_FUNCTION = """function () {
  const tag = this.tagName;
  const types = ['', 'text', 'search', 'email', 'url', 'tel', 'password', 'number'];
  if (tag === 'INPUT' && !types.includes((this.getAttribute('type') || '').toLowerCase()))
    return 'not-fillable';
  if (tag !== 'INPUT' && tag !== 'TEXTAREA' && !this.isContentEditable) return 'not-fillable';
  if (this.disabled || this.readOnly) return 'not-editable';
  this.focus();
  if (tag === 'INPUT' || tag === 'TEXTAREA') this.select();
  else {
    const range = document.createRange();
    range.selectNodeContents(this);
    const selection = getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }
  return 'ok';
}"""
# Playwright's select_option: match by value or label, then fire input and
# change, which is all a real selection would dispatch.
_SELECT_FUNCTION = """function (wanted) {
  if (this.tagName !== 'SELECT') return { error: 'not-select' };
  const options = [...this.options];
  const picked = wanted.map((w) => options.find((o) => o.value === w || o.label === w));
  if (picked.some((o) => !o)) return { error: 'no-option' };
  if (!this.multiple && picked.length > 1) return { error: 'single' };
  for (const option of options) option.selected = picked.includes(option);
  this.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
  this.dispatchEvent(new Event('change', { bubbles: true }));
  return { values: picked.map((o) => o.value) };
}"""


async def snapshot(page: CDPPage, *, depth: int | None = None) -> str:
    """Return the snapshot text, with iframe content under each ``iframe`` line.

    Each frame is walked in its own isolated world. A frame is listed when its
    ``<iframe>`` element is: frames under a hidden owner, or below ``depth``,
    are left out. ``depth`` counts levels across frames.
    """
    # Like Playwright, a depth of 0 means no limit.
    limit = int(depth) if depth else None
    page_frames = await frames(page)
    main, *children = page_frames
    lines, refs = await _frame_snapshot(main, limit, "")
    for frame in children:
        owner = refs.get(frame.owner) if frame.owner else None
        line = next((i for i, text in enumerate(lines) if f"[ref={owner}]" in text), None)
        if owner is None or line is None:
            continue
        indent = len(lines[line]) - len(lines[line].lstrip(" "))
        level = indent // 2 + 1
        if limit is not None and level >= limit:
            continue
        prefix = f"f{frame_key(frame.page.target_id)}"
        try:
            inner, inner_refs = await _frame_snapshot(
                frame, None if limit is None else limit - level, prefix
            )
        except WebSkrapError:
            continue  # the frame navigated or closed during the walk
        refs.update(inner_refs)
        if inner:
            if not lines[line].endswith(":"):
                lines[line] += ":"
            pad = " " * (indent + 2)
            lines[line + 1 : line + 1] = [pad + text for text in inner]
    return "\n".join(lines)


def frame_key(frame_id: str) -> int:
    """Return the short number that names ``frame_id`` in refs."""
    return zlib.crc32(frame_id.encode()) % 100_000


async def _frame_snapshot(
    frame: Frame, depth: int | None, prefix: str
) -> tuple[list[str], dict[tuple[str, int], str]]:
    """Walk one frame; return its lines and each listed node's ref.

    One isolated-world evaluation builds the text and returns the listed
    elements as remote objects; ``DOM.describeNode`` maps each to its backend
    node id. The refs are keyed by session and backend node id, the way
    :attr:`Frame.owner` names an ``<iframe>`` element. The objects are
    released afterwards.
    """
    page = frame.page
    world = await page.isolated_world()
    async with _object_group(page) as group:
        result = await page.send(
            "Runtime.evaluate",
            {
                "expression": f"({_SNAPSHOT_SCRIPT})({'null' if depth is None else depth})",
                "contextId": world,
                "returnByValue": False,
                "objectGroup": group,
            },
        )
        if "exceptionDetails" in result:
            raise CDPError("snapshot script failed")
        properties = await page.send(
            "Runtime.getProperties",
            {"objectId": result["result"]["objectId"], "ownProperties": True},
        )
        by_index = {
            int(item["name"]): item["value"]
            for item in properties["result"]
            if item["name"].isdigit() and "value" in item
        }
        text = str(by_index[0]["value"])
        object_ids = [by_index[index]["objectId"] for index in range(1, len(by_index))]
        nodes = await asyncio.gather(
            *(page.send("DOM.describeNode", {"objectId": object_id}) for object_id in object_ids)
        )
    refs: dict[tuple[str, int], str] = {}
    for index, node in enumerate(nodes):
        backend_id = int(node["node"]["backendNodeId"])
        ref = f"{prefix}e{backend_id}"
        refs[(page.session_id, backend_id)] = ref
        text = text.replace(f"[ref=#{index}]", f"[ref={ref}]")
    return (text.split("\n") if text else []), refs


@dataclass(frozen=True)
class Frame:
    """One frame of a page, ready for isolated-world scripts.

    ``page`` evaluates in this frame: its session (the page's, or an
    out-of-process iframe's) and the frame id. ``offset_x`` and ``offset_y``
    place the frame's viewport in the top-level viewport, so element boxes
    from inside it can be turned into mouse coordinates for ``top``.
    ``owner`` is the ``<iframe>`` element: the session that reported it and
    its backend node id. It is None for the main frame.
    """

    page: CDPPage
    top: CDPPage
    url: str
    offset_x: float = 0.0
    offset_y: float = 0.0
    owner: tuple[str, int] | None = None


async def frames(page: CDPPage) -> list[Frame]:
    """Return the page's frames: the main frame first, then every iframe.

    Same-site iframes come from ``Page.getFrameTree``. Cross-site iframes run
    in other processes, so the page auto-attaches to them (as Playwright
    always does); each gets its own flat session. Offsets come from the
    ``<iframe>`` element's content box (``DOM.getFrameOwner`` and
    ``DOM.getBoxModel``), which Chromium reports in top-level viewport
    coordinates. Frames whose owner cannot be measured (detached, hidden)
    are left out, since nothing in them can be clicked.
    """
    found: list[Frame] = []
    tree = await page.send("Page.getFrameTree")
    found.append(Frame(page, page, tree["frameTree"]["frame"].get("url", "")))
    await _add_child_frames(page, page, tree["frameTree"], 0.0, 0.0, found)
    targets = page.iframe_targets
    if targets is None:
        targets = await _start_auto_attach(page)
    for event in list(targets.values()):
        info = event.get("targetInfo", {})
        if info.get("type") != "iframe":
            continue
        owner = await _owner_box(page, info["targetId"])
        if owner is None:
            continue
        x, y, backend_id = owner
        frame_page = CDPPage(page.connection, event["sessionId"], info["targetId"])
        frame_page.default_timeout_s = page.default_timeout_s
        found.append(
            Frame(frame_page, page, info.get("url", ""), x, y, (page.session_id, backend_id))
        )
        with suppress(WebSkrapError):
            subtree = await frame_page.send("Page.getFrameTree")
            await _add_child_frames(frame_page, page, subtree["frameTree"], x, y, found)
    return found


async def _start_auto_attach(page: CDPPage) -> dict[str, dict[str, Any]]:
    """Track ``page``'s out-of-process iframes from now on.

    Chromium reports each iframe target once, when auto-attach starts or when
    the iframe appears, so the registry outlives one :func:`frames` call: a
    consent notice that renders into an existing iframe later is still found.
    """
    targets: dict[str, dict[str, Any]] = {}

    def attached(params: dict[str, Any]) -> None:
        info = params.get("targetInfo", {})
        if info.get("type") == "iframe":
            targets[info["targetId"]] = params

    def detached(params: dict[str, Any]) -> None:
        for target_id, event in list(targets.items()):
            if event.get("sessionId") == params.get("sessionId"):
                del targets[target_id]

    page.connection.on("Target.attachedToTarget", attached, session_id=page.session_id)
    page.connection.on("Target.detachedFromTarget", detached, session_id=page.session_id)
    page.iframe_targets = targets
    await page.send(
        "Target.setAutoAttach",
        {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
    )
    # Existing iframe targets are reported right after the command.
    await asyncio.sleep(0.05)
    return targets


async def _add_child_frames(
    session_page: CDPPage,
    top: CDPPage,
    node: dict[str, Any],
    base_x: float,
    base_y: float,
    found: list[Frame],
) -> None:
    """Add in-process child frames of ``node``; ``base`` is the session's own offset."""
    for child in node.get("childFrames", []):
        frame_id = child["frame"]["id"]
        owner = await _owner_box(session_page, frame_id)
        if owner is None:
            continue
        frame_page = CDPPage(session_page.connection, session_page.session_id, frame_id)
        frame_page.default_timeout_s = session_page.default_timeout_s
        x, y = base_x + owner[0], base_y + owner[1]
        url = child["frame"].get("url", "")
        found.append(Frame(frame_page, top, url, x, y, (session_page.session_id, owner[2])))
        await _add_child_frames(session_page, top, child, base_x, base_y, found)


async def _owner_box(session_page: CDPPage, frame_id: str) -> tuple[float, float, int] | None:
    """Return the content-box origin and backend node id of ``frame_id``'s ``<iframe>``."""
    try:
        owner = await session_page.send("DOM.getFrameOwner", {"frameId": frame_id})
        box = await session_page.send("DOM.getBoxModel", {"backendNodeId": owner["backendNodeId"]})
    except WebSkrapError:
        return None
    content = box["model"]["content"]
    return float(content[0]), float(content[1]), int(owner["backendNodeId"])


class _Element:
    """One resolved element: a remote object in an isolated world.

    ``input_page`` receives mouse events, and ``offset`` turns the element's
    frame-relative box into its coordinates there; both differ from ``page``
    only for elements inside iframes. ``frame_id`` is set for elements a
    frame ref resolved; see :func:`_scroll_into_view`.
    """

    def __init__(
        self,
        page: CDPPage,
        object_id: str,
        description: str,
        *,
        input_page: CDPPage | None = None,
        offset: tuple[float, float] = (0.0, 0.0),
        frame_id: str | None = None,
    ) -> None:
        self.page = page
        self.object_id = object_id
        self.description = description
        self.input_page = input_page or page
        self.offset = offset
        self.frame_id = frame_id

    async def call(self, declaration: str, *args: Any) -> Any:
        result = await self.page.send(
            "Runtime.callFunctionOn",
            {
                "functionDeclaration": declaration,
                "objectId": self.object_id,
                "arguments": [{"value": arg} for arg in args],
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        if "exceptionDetails" in result:
            raise CDPError(f"element script failed on {self.description}")
        return result["result"].get("value")


def _stale(target: str) -> WebSkrapError:
    msg = f"ref {target} no longer resolves (aria-ref stale); take a fresh snapshot"
    return WebSkrapError(msg, code=ErrorCode.STALE_REF)


@asynccontextmanager
async def _object_group(page: CDPPage) -> AsyncIterator[str]:
    """Name a group for one lookup's remote objects and release it afterwards."""
    group = f"webskrap-{uuid4().hex[:8]}"
    try:
        yield group
    finally:
        with suppress(WebSkrapError):
            await page.send("Runtime.releaseObjectGroup", {"objectGroup": group})


def _context_gone(exc: CDPError) -> bool:
    return "context" in str(exc).lower()


async def _locate(page: CDPPage, target: str) -> Frame:
    """Return the frame ``target`` lives in: the main frame, or a frame ref's frame.

    CSS selectors match in the main frame only.

    Raises:
        WebSkrapError: ``stale_ref`` when no current frame has the ref's key.
    """
    match = REF_PATTERN.fullmatch(target)
    if not match or match[1] is None:
        return Frame(page, page, "")
    key = int(match[1])
    for frame in (await frames(page))[1:]:
        if frame_key(frame.page.target_id) == key:
            return frame
    raise _stale(target)


async def _find(frame: Frame, target: str, group: str) -> list[_Element]:
    """Resolve ``target`` to its current elements: one for a ref, all CSS matches.

    Runs in the frame's shared isolated world and retries once in a new world
    when a navigation destroyed the old one.
    """
    page = frame.page
    for attempt in range(2):
        world = await page.shared_world()
        try:
            elements = await _find_in(page, target, group, world)
        except CDPError as exc:
            if attempt == 0 and _context_gone(exc):
                page.forget_world()
                continue
            raise
        if frame.owner is not None:
            for element in elements:
                element.input_page = frame.top
                element.offset = (frame.offset_x, frame.offset_y)
                element.frame_id = page.target_id
        return elements
    raise AssertionError("unreachable")


async def _find_in(page: CDPPage, target: str, group: str, world: int) -> list[_Element]:
    match = REF_PATTERN.fullmatch(target)
    if match:
        try:
            resolved = await page.send(
                "DOM.resolveNode",
                {
                    "backendNodeId": int(match[2]),
                    "executionContextId": world,
                    "objectGroup": group,
                },
            )
        except CDPError as exc:
            if _context_gone(exc):
                raise
            raise _stale(target) from exc
        element = _Element(page, resolved["object"]["objectId"], target)
        # In-process frames share one session: a node of another frame resolves
        # too, so a ref whose node left its frame's document is stale.
        if not await element.call("function () { return this.ownerDocument === document; }"):
            raise _stale(target)
        return [element]
    result = await page.send(
        "Runtime.evaluate",
        {
            "expression": f"[...document.querySelectorAll({json.dumps(target)})]",
            "contextId": world,
            "returnByValue": False,
            "objectGroup": group,
        },
    )
    if "exceptionDetails" in result:
        msg = f"'{target}' is not a valid CSS selector (targets are CSS selectors or snapshot refs)"
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    # Properties of a grouped object belong to the same group.
    properties = await page.send(
        "Runtime.getProperties", {"objectId": result["result"]["objectId"], "ownProperties": True}
    )
    return [
        _Element(page, item["value"]["objectId"], target)
        for item in properties["result"]
        if item["name"].isdigit() and "objectId" in item.get("value", {})
    ]


async def element_state(page: CDPPage, target: str) -> str:
    """Return ``visible``, ``hidden`` or ``detached`` for the first match.

    The first match, not a strict single one, as Patchright waits on
    ``locator.first``.
    """
    async with _object_group(page) as group:
        try:
            elements = await _find(await _locate(page, target), target, group)
        except WebSkrapError as exc:
            if exc.code is ErrorCode.STALE_REF:
                return "detached"
            raise
        if not elements:
            return "detached"
        return str(await elements[0].call(_STATE_FUNCTION))


async def _actionable(
    page: CDPPage, target: str, group: str, timeout_s: float, *, enabled: bool
) -> _Element:
    """Wait for ``target`` to be one visible element, as Playwright's strict mode.

    With ``enabled``, also wait until it is enabled, as Playwright does before
    clicking, checking, filling, typing or selecting.
    """
    deadline = time.monotonic() + timeout_s
    frame = await _locate(page, target)
    while True:
        elements = await _find(frame, target, group)
        if len(elements) > 1:
            msg = f"strict mode: '{target}' matches {len(elements)} elements; use a ref"
            raise WebSkrapError(msg, code=ErrorCode.USAGE)
        state = await elements[0].call(_STATE_FUNCTION) if elements else "detached"
        if state == "visible" and (not enabled or await elements[0].call(_ENABLED_FUNCTION)):
            return elements[0]
        if state == "detached" and REF_PATTERN.fullmatch(target):
            # A removed node can outlive the DOM in memory; it is stale all the same.
            raise _stale(target)
        if time.monotonic() >= deadline:
            wanted = "visible and enabled" if enabled else "visible"
            msg = f"timed out after {timeout_s * 1000:.0f}ms waiting for {target} to be {wanted}"
            raise WebSkrapError(msg, code=ErrorCode.TIMEOUT)
        await asyncio.sleep(_POLL_S)


async def _box(element: _Element) -> dict[str, float]:
    box = await element.call(_BOX_FUNCTION)
    if not box or box["width"] <= 0 or box["height"] <= 0:
        msg = f"could not find a visible bounding box for selector: {element.description}"
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    return box


async def _move(page: CDPPage, x: float, y: float) -> None:
    await page.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})


async def _scroll_into_view(element: _Element) -> None:
    """Port of :func:`webskrap.human.scroll_into_view`: wheel notches, then a jump.

    The wheel goes where a fresh connection's mouse is, the viewport origin,
    as Patchright's ``mouse.wheel`` does after reattaching. An element from a
    frame ref can sit in an iframe that is out of view itself, so it gets the
    jump at once, which scrolls every frame above it too.
    """
    if element.frame_id is not None:
        await _scroll_frame_element(element)
        return
    page = element.input_page
    # Over a frame, the wheel must sit inside it to scroll its document.
    wheel_x, wheel_y = element.offset[0] + 1, element.offset[1] + 1
    previous_top: float | None = None
    for _ in range(MAX_WHEEL_BATCHES):
        box = await _box(element)
        top, bottom, height = box["y"], box["y"] + box["height"], box["viewportHeight"]
        if top >= VIEW_MARGIN and bottom <= height - VIEW_MARGIN:
            return
        if previous_top is not None and abs(top - previous_top) < 1:
            break
        previous_top = top
        remaining = top - height * uniform(0.25, 0.45)
        while abs(remaining) > 20:
            notch = min(abs(remaining), uniform(80, 120))
            step = notch if remaining > 0 else -notch
            await page.send(
                "Input.dispatchMouseEvent",
                {"type": "mouseWheel", "x": wheel_x, "y": wheel_y, "deltaX": 0, "deltaY": step},
            )
            remaining -= step
            await asyncio.sleep(uniform(16, 45) / 1000)
        await asyncio.sleep(uniform(60, 160) / 1000)
    await element.call(_SCROLL_FUNCTION)


async def _scroll_frame_element(element: _Element) -> None:
    """Scroll a frame element into view, then measure its frame's offset again."""
    await element.call(_SCROLL_FUNCTION)
    for frame in await frames(element.input_page):
        if frame.page.target_id == element.frame_id:
            element.offset = (frame.offset_x, frame.offset_y)
            return
    raise _stale(element.description)


async def _human_pointer(element: _Element, *, click_count: int = 0) -> None:
    """Move along a curved path to ``element`` and click ``click_count`` times.

    Port of :func:`webskrap.human.click`: scroll by wheel, refuse a covered
    point, pause, teleport near the target, follow a Bezier path, pause, then
    hold the button 60 to 140 ms per click. ``click_count=0`` only hovers.
    """
    page = element.input_page
    await _scroll_into_view(element)
    box = await _box(element)
    frame_x, frame_y = click_point(
        {"x": box["x"], "y": box["y"], "width": box["width"], "height": box["height"]}, None
    )
    if not await element.call(_HIT_TEST_FUNCTION, frame_x - box["x"], frame_y - box["y"]):
        msg = f"another element covers the click point of: {element.description}"
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    x, y = frame_x + element.offset[0], frame_y + element.offset[1]
    await asyncio.sleep(uniform(80, 220) / 1000)
    start_x, start_y = x + uniform(-160, 160), y + uniform(-90, 90)
    await _move(page, start_x, start_y)
    end_x, end_y = x + uniform(-8, 8), y + uniform(-6, 6)
    distance = ((end_x - start_x) ** 2 + (end_y - start_y) ** 2) ** 0.5
    steps = max(12, min(48, int(distance / 6)))
    for px, py in bezier_path((start_x, start_y), (end_x, end_y), steps):
        await _move(page, px, py)
        await asyncio.sleep(uniform(2, 9) / 1000)
    await asyncio.sleep(uniform(40, 140) / 1000)
    # Playwright's mouse.click(x, y) moves to the exact point before pressing.
    if click_count:
        await _move(page, x, y)
    for count in range(1, click_count + 1):
        press = {"x": x, "y": y, "button": "left", "clickCount": count}
        await page.send("Input.dispatchMouseEvent", {"type": "mousePressed", "buttons": 1, **press})
        await asyncio.sleep(uniform(60, 140) / 1000)
        await page.send("Input.dispatchMouseEvent", {"type": "mouseReleased", **press})


async def _type(page: CDPPage, text: str) -> None:
    """Port of :func:`webskrap.human.type_text`'s keystrokes, one key at a time.

    Each key is held 30 to 90 ms, keys are 40 to 160 ms apart, and about one
    in twelve is followed by a 250 to 600 ms pause. Characters a US keyboard
    cannot type are inserted as text, as Playwright's ``keyboard.type`` does.
    """
    for character in text:
        name = "Enter" if character == "\n" else character
        try:
            events = key_events(name)
        except WebSkrapError:
            events = []
        if events and len(events) == 2:
            await page.send("Input.dispatchKeyEvent", events[0])
            await asyncio.sleep(uniform(30, 90) / 1000)
            await page.send("Input.dispatchKeyEvent", events[1])
        else:
            await page.send("Input.insertText", {"text": character})
        pause = uniform(250, 600) if uniform(0, 1) < 1 / 12 else uniform(40, 160)
        await asyncio.sleep(pause / 1000)


async def element_action(
    page: CDPPage,
    action: str,
    target: str,
    arguments: list[Any],
) -> None:
    """Run one ``browser_session.ELEMENT_ACTIONS`` action on a ref or CSS target.

    ``arguments`` were validated by ``browser_session.element_arguments``.

    Raises:
        WebSkrapError: ``stale_ref`` for a ref whose node is gone, ``usage``
            for a covered, ambiguous or unsuitable element, ``timeout`` when
            no visible element appears in time.
    """
    async with _object_group(page) as group:
        element = await _actionable(
            page, target, group, page.default_timeout_s, enabled=action != "hover"
        )
        await _act(page, element, action, target, arguments)


async def _act(
    page: CDPPage, element: _Element, action: str, target: str, arguments: list[Any]
) -> None:
    if action in ("click", "dblclick"):
        await _human_pointer(element, click_count=2 if action == "dblclick" else 1)
    elif action == "hover":
        await _human_pointer(element)
    elif action in ("check", "uncheck"):
        wanted = action == "check"
        current = await element.call(_CHECKED_FUNCTION)
        if current == "not-checkable":
            msg = f"{target} is not a checkbox or radio button"
            raise WebSkrapError(msg, code=ErrorCode.USAGE)
        if current != wanted:
            await _human_pointer(element, click_count=1)
            if await element.call(_CHECKED_FUNCTION) != wanted:
                msg = f"clicking {target} did not change its checked state"
                raise WebSkrapError(msg, code=ErrorCode.USAGE)
    elif action == "fill":
        prepared = await element.call(_PREPARE_FILL_FUNCTION)
        if prepared != "ok":
            msg = f"{target} cannot be filled: {prepared}"
            raise WebSkrapError(msg, code=ErrorCode.USAGE)
        if arguments[0]:
            await page.send("Input.insertText", {"text": arguments[0]})
        else:
            for event in key_events("Delete"):
                await page.send("Input.dispatchKeyEvent", event)
    elif action == "type":
        await _human_pointer(element, click_count=1)
        await _type(page, arguments[0])
    elif action == "select":
        outcome = await element.call(_SELECT_FUNCTION, arguments[0])
        if "error" in outcome:
            reasons = {
                "not-select": "is not a <select>",
                "no-option": "has no option with that value or label",
                "single": "accepts one option only",
            }
            msg = f"{target} {reasons[outcome['error']]}"
            raise WebSkrapError(msg, code=ErrorCode.USAGE)


@asynccontextmanager
async def query_elements(
    frame: Frame, script: str, arg: Any = None
) -> AsyncIterator[list[_Element]]:
    """Run ``script`` (a function returning an array of elements) in ``frame``.

    Yields the elements, ready for :func:`is_visible` and :func:`human_click`;
    their remote objects are released when the block ends. The script runs in
    a fresh isolated world, so the page cannot see or patch it.
    """
    async with _object_group(frame.page) as group:
        world = await frame.page.isolated_world()
        result = await frame.page.send(
            "Runtime.evaluate",
            {
                "expression": f"({script})({json.dumps(arg)})",
                "contextId": world,
                "returnByValue": False,
                "objectGroup": group,
            },
        )
        if "exceptionDetails" in result or "objectId" not in result.get("result", {}):
            yield []
            return
        properties = await frame.page.send(
            "Runtime.getProperties",
            {"objectId": result["result"]["objectId"], "ownProperties": True},
        )
        yield [
            _Element(
                frame.page,
                item["value"]["objectId"],
                "element",
                input_page=frame.top,
                offset=(frame.offset_x, frame.offset_y),
            )
            for item in properties["result"]
            if item["name"].isdigit() and "objectId" in item.get("value", {})
        ]


async def is_visible(element: _Element) -> bool:
    """True when ``element`` has a non-empty box and is not ``visibility:hidden``."""
    return await element.call(_STATE_FUNCTION) == "visible"


async def human_click(element: _Element, *, description: str, timeout_s: float) -> None:
    """Click ``element`` along the human cursor path within ``timeout_s``.

    Raises:
        WebSkrapError: ``usage`` when another element covers it, ``timeout``
            when the gesture does not finish in time.
    """
    element.description = description
    try:
        await asyncio.wait_for(_human_pointer(element, click_count=1), timeout_s)
    except TimeoutError as exc:
        msg = f"timed out clicking {description}"
        raise WebSkrapError(msg, code=ErrorCode.TIMEOUT) from exc


async def instant_click(page: CDPPage, target: str, *, click_count: int = 1) -> None:
    """Click the center of ``target`` at once, with no cursor path or hold.

    The same strict, visible and enabled checks as :func:`element_action`
    apply; only the gesture is mechanical. Prefer the human click on pages
    that score input.
    """
    async with _object_group(page) as group:
        element = await _actionable(page, target, group, page.default_timeout_s, enabled=True)
        if element.frame_id is not None:
            await _scroll_frame_element(element)
        else:
            await element.call(_SCROLL_FUNCTION)
        box = await _box(element)
        x = box["x"] + box["width"] / 2 + element.offset[0]
        y = box["y"] + box["height"] / 2 + element.offset[1]
        await _move(element.input_page, x, y)
        for count in range(1, click_count + 1):
            press = {"x": x, "y": y, "button": "left", "clickCount": count}
            await element.input_page.send(
                "Input.dispatchMouseEvent", {"type": "mousePressed", "buttons": 1, **press}
            )
            await element.input_page.send(
                "Input.dispatchMouseEvent", {"type": "mouseReleased", **press}
            )
