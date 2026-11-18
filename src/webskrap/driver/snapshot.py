"""Accessibility snapshots and matching lines."""

from __future__ import annotations

import asyncio
import json
import re
from uuid import uuid4

from webskrap.driver import text_search
from webskrap.driver.cdp import CDPError, CDPPage
from webskrap.driver.cdp_dom import (
    _STATE_FUNCTION,
    Frame,
    _find,
    _locate,
    _object_group,
    _stale,
    frame_key,
    frames,
)
from webskrap.errors import ErrorCode, WebSkrapError

# Builds the snapshot text with "[ref=#N]" placeholders and returns it with
# the listed elements, so the caller can swap each placeholder for the
# element's backend node id. Roles and names follow the ARIA implicit-role
# table and the accessible-name rules for the elements pages commonly use.
_SNAPSHOT_SCRIPT = r"""({depth: maxDepth, boxes, offsetX, offsetY, refToken}, scope) => {
  const elements = [];
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'HEAD', 'META', 'LINK']);
  const LEAF = new Set(['button', 'link', 'heading', 'textbox', 'searchbox', 'checkbox',
    'radio', 'switch', 'img', 'option', 'combobox', 'slider', 'spinbutton', 'tab',
    'menuitem', 'menuitemcheckbox', 'menuitemradio', 'treeitem', 'progressbar', 'meter',
    'separator']);
  const FROM_CONTENT = new Set(['button', 'link', 'heading', 'cell', 'columnheader',
    'rowheader', 'option', 'tab', 'menuitem', 'menuitemcheckbox', 'menuitemradio',
    'treeitem', 'checkbox', 'radio', 'switch', 'tooltip']);
  const norm = (text) => (text || '').replace(/[\s\u001c-\u001e\u0085]+/g, ' ').trim();
  const clip = (text) => text.length > 200 ? text.slice(0, 199) + '…' : text;
  const quote = (text) => JSON.stringify(text).replace(/[\u0085\u2028\u2029]/g,
    (c) => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0'));
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
        .map((id) => e.getRootNode().getElementById(id))
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
    if (e.matches(':disabled') || e.closest('[aria-disabled="true"]')) out.push('disabled');
    const expanded = e.getAttribute('aria-expanded');
    if (expanded === 'true') out.push('expanded');
    if (expanded === 'false') out.push('expanded=false');
    for (const attr of ['pressed', 'selected', 'invalid', 'busy', 'required', 'readonly']) {
      const state = e.getAttribute('aria-' + attr);
      if (state !== null && !(state === 'true' && ['pressed', 'selected'].includes(attr)))
        out.push(state === 'true' && ['required', 'readonly'].includes(attr)
          ? attr : attr + '=' + norm(state));
    }
    if (e.required) out.push('required');
    if (e.readOnly) out.push('readonly');
    if (e.tagName === 'DETAILS' && e.open) out.push('expanded');
    if (e.getAttribute('aria-pressed') === 'true') out.push('pressed');
    if (r === 'option' ? e.selected : e.getAttribute('aria-selected') === 'true')
      out.push('selected');
    if (r === 'heading') {
      const level = Number(e.getAttribute('aria-level')) || Number(e.tagName.slice(1)) || 2;
      out.push('level=' + level);
    }
    if (e === document.activeElement && e !== document.body) out.push('active');
    return [...new Set(out)].map((s) => ' [' + s + ']').join('');
  };
  const value = (e, r) => {
    if (e.tagName === 'INPUT' && e.type === 'password') return '';
    if (['textbox', 'searchbox', 'spinbutton', 'slider'].includes(r) && 'value' in e)
      return norm(e.value);
    if (r === 'combobox' && e.tagName === 'SELECT')
      return norm(e.selectedOptions[0] ? e.selectedOptions[0].text : '');
    if (r === 'combobox' && 'value' in e) return norm(e.value);
    return '';
  };
  const childrenOf = (node) => {
    if (node.tagName === 'DETAILS' && !node.open) {
      const summary = node.querySelector(':scope > summary');
      return summary ? [summary] : [];
    }
    if (node.shadowRoot) return [...node.shadowRoot.childNodes];
    if (node.tagName === 'SLOT') {
      const assigned = node.assignedNodes({ flatten: true });
      if (assigned.length) return assigned;
    }
    return [...node.childNodes];
  };
  const hidden = (e) => {
    if (e.hidden || e.inert || e.getAttribute('aria-hidden') === 'true') return true;
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
    line += ' [ref=#' + refToken + '-' + index + ']';
    if (boxes) {
      const b = node.getBoundingClientRect();
      line += ' [box=' + [b.x + offsetX, b.y + offsetY, b.width, b.height]
        .map(Math.round).join(',') + ']';
    }
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
      if (href !== null && room)
        children.unshift({ line: '- /url: ' + quote(href), depth: depth + 1 });
    }
    if (children.length === 1 && children[0].text !== undefined) {
      if (children[0].text !== label) line += ': ' + children[0].text;
      return [{ line, depth }];
    }
    if (children.length) line += ':';
    return [{ line, depth }, ...children];
  };
  const modal = document.activeElement?.closest('dialog:modal')
    || [...document.querySelectorAll('dialog:modal')].at(-1) || null;
  const root = modal && (!scope || scope.contains(modal))
    ? modal : scope || document.body || document.documentElement;
  let ancestor = scope;
  let insideModal = !modal || (scope && scope.contains(modal));
  while (ancestor) {
    if (ancestor === modal) insideModal = true;
    if (hidden(ancestor)) return [''];
    ancestor = ancestor.parentElement || ancestor.getRootNode().host;
  }
  if (scope && !insideModal) return [''];
  const lines = merge(root ? (scope || modal ? walk(root, 0)
    : childrenOf(root).flatMap((child) => walk(child, 0))) : []);
  const text = lines.map((item) => '  '.repeat(item.depth)
    + (item.text !== undefined ? '- text: ' + item.text : item.line)).join('\n');
  return [text, ...elements];
}"""


async def snapshot(
    page: CDPPage,
    *,
    depth: int | None = None,
    target: str | None = None,
    boxes: bool = False,
    pattern: str | re.Pattern[str] | None = None,
) -> str:
    """Return the snapshot text, with iframe content under each ``iframe`` line.

    Each frame is walked in its own isolated world. A frame is listed when its
    ``<iframe>`` element is: frames under a hidden owner, or below ``depth``,
    are left out. ``depth`` counts levels across frames.
    """
    # Like Playwright, a depth of 0 means no limit.
    if depth is not None and depth < 0:
        raise WebSkrapError("depth must be non-negative or null", code=ErrorCode.USAGE)
    limit = int(depth) if depth else None
    page_frames = await frames(page)
    main, *children = page_frames
    if target:
        main = await _locate(page, target)
        children = [frame for frame in page_frames if frame.page.target_id != main.page.target_id]
    prefix = f"f{frame_key(main.page.target_id)}" if main.owner else ""
    lines, refs = await _frame_snapshot(main, limit, prefix, target, boxes, pattern)
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
                frame,
                None if limit is None else limit - level,
                prefix,
                None,
                boxes,
                pattern,
            )
        except WebSkrapError as exc:
            if pattern is not None and exc.code in (ErrorCode.USAGE, ErrorCode.INTERNAL):
                raise
            continue
        refs.update(inner_refs)
        if inner:
            if not lines[line].endswith(":"):
                lines[line] += ":"
            pad = " " * (indent + 2)
            lines[line + 1 : line + 1] = [pad + text for text in inner]
    return "\n".join(lines)


async def matching_snapshot(
    tree: str,
    pattern: str | re.Pattern[str],
    max_matches: int,
    *,
    keep_frames: bool = False,
) -> tuple[str, int]:
    """Select matching semantic lines and their ancestor paths, without siblings."""
    lines = tree.splitlines()
    searchable = [
        re.sub(
            r'^(?P<node>\s*- [a-z][a-z0-9-]*(?: "(?:[^"\\]|\\.)*")?'
            r"(?: \[(?!ref=)[^\]]*\])*) \[ref=(?:#[a-f0-9]+-\d+|(?:f\d+)?e\d+)\]"
            r"(?: \[box=[^]]+\])?(?P<text>:.*)?$",
            r"\g<node>\g<text>",
            line,
        ).strip()
        for line in lines
    ]
    if isinstance(pattern, re.Pattern):
        hits = [i for i, line in enumerate(searchable) if pattern.search(line)]
    else:
        hits = await text_search._matching_lines(pattern, searchable)
    hits = [i for i in hits if not searchable[i].startswith("- /url:")]
    selected = set(hits[:max_matches])
    if keep_frames:
        selected.update(i for i, line in enumerate(searchable) if line.startswith("- iframe"))
    stack: list[tuple[int, int]] = []
    kept: set[int] = set()
    for i, line in enumerate(lines):
        indent = len(line) - len(line.lstrip())
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if i in selected:
            kept.add(i)
            kept.update(index for _, index in stack)
        stack.append((indent, i))
    return "\n".join(line for i, line in enumerate(lines) if i in kept), len(hits)


async def _frame_snapshot(
    frame: Frame,
    depth: int | None,
    prefix: str,
    target: str | None = None,
    boxes: bool = False,
    pattern: str | re.Pattern[str] | None = None,
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
        ref_token = uuid4().hex
        placeholder = re.compile(r"\[ref=#" + ref_token + r"-(\d+)\]")
        options = {
            "depth": depth,
            "refToken": ref_token,
            "boxes": boxes,
            "offsetX": frame.offset_x,
            "offsetY": frame.offset_y,
        }
        if target:
            elements = await _find(frame, target, group)
            if len(elements) != 1:
                raise WebSkrapError("target must match one element", code=ErrorCode.USAGE)
            element = elements[0]
            if await element.call(_STATE_FUNCTION) == "detached":
                raise _stale(target)
            result = await page.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": element.object_id,
                    "functionDeclaration": "function (options) { return ("
                    + _SNAPSHOT_SCRIPT
                    + ")(options, this); }",
                    "arguments": [{"value": options}],
                    "objectGroup": group,
                },
            )
        else:
            result = await page.send(
                "Runtime.evaluate",
                {
                    "expression": f"({_SNAPSHOT_SCRIPT})({json.dumps(options)})",
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
        if pattern is not None:
            text, _ = await matching_snapshot(text, pattern, len(text), keep_frames=True)
        indexes = sorted({int(index) for index in placeholder.findall(text)})
        nodes = await asyncio.gather(
            *(
                page.send("DOM.describeNode", {"objectId": by_index[index + 1]["objectId"]})
                for index in indexes
            )
        )
    refs: dict[tuple[str, int], str] = {}
    replacements: dict[str, str] = {}
    for index, node in zip(indexes, nodes, strict=True):
        backend_id = int(node["node"]["backendNodeId"])
        ref = f"{prefix}e{backend_id}"
        refs[(page.session_id, backend_id)] = ref
        replacements[str(index)] = ref
    text = placeholder.sub(lambda match: f"[ref={replacements[match[1]]}]", text)
    return (text.split("\n") if text else []), refs
