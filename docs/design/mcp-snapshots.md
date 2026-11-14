# Targeted MCP accessibility reads

WebSkrap keeps its native CDP DOM walker. It does not enable Chromium's
Accessibility domain or Runtime event domain to generate snapshots.

## Existing behavior and changes

The starting implementation already had implicit ARIA roles, common accessible
name rules, states, wrapper elision, text merging, depth limits, open shadow
roots, iframe traversal, backend-node refs and character pagination. Actions
already returned page state without an automatic snapshot.

[Playwright MCP snapshots](https://playwright.dev/mcp/snapshots) describes
subtree targets, depth limits, optional boxes and matching nodes under ancestor
paths. WebSkrap adopts these features while keeping its selector and ref
contracts. Playwright locator expressions are not accepted. A CSS selector
must uniquely match an element in the main document. A frame ref selects that
frame; an element ref also works inside an open shadow root.

`browser_snapshot` adds optional `target` and `boxes` arguments after the
existing positional arguments. Its response fields remain unchanged. Depth is
relative to the selected root. Zero and null retain the unlimited-depth
meaning. Negative depth returns a usage error. Boxes are rounded CSS pixels
relative to the top-level viewport, including iframe offsets.

`browser_find` returns the same snapshot-window fields, plus match counts.
It searches semantic lines, including text and non-password values appended
after refs, and retains their ancestor paths. Only structural ref and box
tokens are removed for matching; ref-like text in names or content is kept.
It omits sibling subtrees and link URL metadata. Search takes either case-insensitive literal
text or Python regex. Literal text is escaped before compilation. Caller regex
uses the existing subprocess worker and two-second timeout. Regex never runs
in the page. Search has a match cap and independent character pagination.

## Ref and response cost

The DOM walk runs in an isolated world and returns semantic text plus remote
node objects. Search filters the text before issuing `DOM.describeNode`.
Only matching nodes, their ancestors and iframe owners need backend IDs.
Iframe owners remain available while child frames are searched. A final pass
removes nonmatching frame paths and applies the global match limit.

The walker still builds each available frame's semantic tree. This avoids a
second naming implementation and keeps semantics consistent with snapshots.
It does not make search traversal proportional only to the number of matches.
There is no persistent tree cache. Remote object groups are released on errors
and successful reads. Ref replacement uses one substitution pass. Per-call
random placeholders prevent page text that resembles a placeholder from being
used as a remote-object index.

Refs keep Chromium's backend-node identity. Repeated snapshots, find calls and
SPA updates preserve refs when they preserve the underlying node. A detached
node or a changed document returns `stale_ref`. There is no snapshot-generation
counter and no model-specific ref remapping. Existing interaction mechanisms
remain in use.

Responses do not repeat snapshots after actions. A caller can find, interact,
wait for a condition and request only the next needed control. A diff cache
was not added: this change measures explicit targeted reads, without adding a
second state lifetime or requiring clients to reconstruct trees.

## Semantics and limits

Link URL metadata uses quoted JSON strings, including escaped line separators.
A URL cannot add structural lines or refs to the snapshot. Search still omits
URL metadata. Dynamic ARIA state text is normalized to one line.

With stacked native modal dialogs, snapshots select the modal containing
`document.activeElement`. If focus does not identify a modal, the last modal in
document order is used. That fallback is an approximation of top-layer order.
Scope checks apply to the selected modal, and closing it restores the lower
modal's context.

The shared walker now also omits password input values, inert content, closed
details content and background content behind native modal dialogs. It resolves
`aria-labelledby` in the element's document or shadow root. Disabled fieldsets,
expanded=false, mixed pressed state, required, readonly, invalid and busy state
are represented. Existing compact role/name and common naming rules remain;
this is not a complete implementation of the browser accessibility algorithm.

Closed shadow roots and nested frames inside out-of-process iframes retain the
driver's existing limits. Frame navigation during traversal can omit that frame.
A snapshot is not an atomic DOM transaction. Continuation offsets are valid
only while the DOM remains unchanged. A character window can split a line or
ref. Match caps precede character caps. There are no sibling gap markers;
ancestor paths are intentionally partial trees.

Password redaction does not remove secrets from visible text, labels, URLs or
other field values. Snapshot and find output must receive the same privacy
handling as other browser reads.

See [measured workflows](../benchmarks/mcp-snapshots.md) for results and limits.
