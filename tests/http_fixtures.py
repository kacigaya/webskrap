"""HTTP handlers shared by browser and CDP integration tests."""

from __future__ import annotations

import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

# A OneTrust-shaped notice: matched by an exact CMP selector.
CMP_BANNER_PAGE = b"""<html><title>Notice</title><body>
<p>Article body</p>
<div id="onetrust-banner-sdk">
  <button id="onetrust-accept-btn-handler">Accept All Cookies</button>
  <button id="onetrust-reject-all-handler">Reject All Cookies</button>
</div>
<script>
document.getElementById('onetrust-reject-all-handler').addEventListener('click', () => {
  document.getElementById('onetrust-banner-sdk').remove();
});
</script>
</body></html>"""

# Relative, duplicate, and javascript: anchors: link collection must resolve,
# deduplicate, and drop them respectively.
LINKS_PAGE = b"""<html><title>Links</title><body>
<a href="/one">  One\n  link  </a>
<a href="/two">Two</a>
<a href="/one">One again</a>
<a href="javascript:void(0)">Script</a>
<a href="https://example.test/three">Three</a>
</body></html>"""

# An unbranded notice injected after load: only the text strategy can find it.
LATE_TEXT_BANNER_PAGE = b"""<html><title>Notice</title><body>
<p>Article body</p>
<button id="decoy">Decline invitation</button>
<script>
setTimeout(() => {
  const bar = document.createElement('div');
  bar.className = 'cookie-consent-bar';
  bar.innerHTML = '<button>Accept</button><button>Continue without accepting</button>';
  bar.querySelectorAll('button')[1].addEventListener('click', () => bar.remove());
  document.body.appendChild(bar);
}, 400);
</script>
</body></html>"""


# Clears itself as Akamai's sec-cpt page does: its script sets a cookie and reloads.
INTERSTITIAL_PAGE = b"""<html><body><div id="sec-if-cpt-container" style="display:none"></div>
<script>setTimeout(() => { document.cookie = 'passed=1; path=/'; location.reload(); }, 300);
</script></body></html>"""

# Reloads before DOMContentLoaded: the slow script holds the parser until the
# reload has replaced the document.
EARLY_RELOAD_PAGE = b"""<html><head><script>
document.cookie = 'early=1; path=/'; location.reload();
</script><script src="/slow.js"></script></head><body>First</body></html>"""

BLOCKS = Path(__file__).parent / "fixtures" / "blocks"


class BrowserHandler(BaseHTTPRequestHandler):
    server_name = "BaseHTTP"

    def version_string(self) -> str:
        return self.server_name

    def _passed_page(self, cookie: str, title: bytes) -> bool:
        if cookie not in self.headers.get("Cookie", ""):
            return False
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><title>" + title + b"</title><body>Real page</body></html>")
        return True

    def do_GET(self) -> None:
        if self.path == "/resource-probe":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<html><body><div id="asset-result"></div>
            <img src="/asset.svg"
              onload="document.querySelector('div').textContent='image loaded'"
              onerror="document.querySelector('div').textContent='image blocked'">
            <script>new MutationObserver(() => {
              document.querySelector('div').setAttribute('data-ready', 'yes');
            }).observe(document.querySelector('div'), {childList:true});</script>
            </body></html>""")
            return
        if self.path == "/asset.svg":
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.end_headers()
            self.wfile.write(b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"/>')
            return
        if self.path in ("/empty-404", "/empty-500"):
            self.send_response(int(self.path.rsplit("-", 1)[1]))
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if self.path == "/interstitial":
            if not self._passed_page("passed=1", b"Passed"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(INTERSTITIAL_PAGE)
            return

        if self.path == "/waf-challenge":
            if not self._passed_page("passed=1", b"Passed"):
                self.send_response(202)
                self.send_header("Content-Type", "text/html")
                self.send_header("x-amzn-waf-action", "challenge")
                self.end_headers()
                self.wfile.write(INTERSTITIAL_PAGE.replace(b"sec-if-cpt-container", b"x"))
            return

        if self.path == "/early-reload":
            if not self._passed_page("early=1", b"Reloaded"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(EARLY_RELOAD_PAGE)
            return

        if self.path == "/slow.js":
            time.sleep(3)
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript")
            self.end_headers()
            return

        if self.path == "/cf-block":
            self.server_name = "cloudflare"
            self.send_response(403)
            self.send_header("Content-Type", "text/html")
            self.send_header("cf-ray", "0123456789abcdef-CDG")
            self.end_headers()
            self.wfile.write((BLOCKS / "cloudflare-custom-block.html").read_bytes())
            return

        if self.path == "/cf-challenge":
            self.send_response(200)
            self.send_header("cf-mitigated", "challenge")
            self.end_headers()
            self.wfile.write(b"<html><body>Verify your browser</body></html>")
            return

        if self.path == "/datadome-challenge":
            self.send_response(403)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b'<html><body><script src="https://ct.captcha-delivery.com/c.js"></script>'
                b"</body></html>"
            )
            return

        if self.path == "/forbidden":
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"<html><body>Forbidden</body></html>")
            return

        if self.path == "/ready":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b'<html><body><div id="quote" hidden>Quote form ready</div>'
                b'<script>addEventListener("load", async () => {'
                b'await fetch("/ready-data"); document.getElementById("quote").hidden = false;'
                b"});</script></body></html>"
            )
            return

        if self.path == "/set-cookie":
            self.send_response(200)
            self.send_header("Set-Cookie", "webskrap_test=1; Path=/")
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><title>Cookie</title><body>ok</body></html>")
            return

        if self.path == "/echo-cookie":
            cookie = self.headers.get("Cookie", "")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(f"<html><title>Echo</title><body>{cookie}</body></html>".encode())
            return

        if self.path == "/cmp-banner":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(CMP_BANNER_PAGE)
            return

        if self.path == "/links":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(LINKS_PAGE)
            return

        if self.path == "/late-text-banner":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(LATE_TEXT_BANNER_PAGE)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><title>Hello</title><body>WebSkrap</body></html>")

    def log_message(self, format: str, *args: object) -> None:
        return


# Logs an Error whose `stack` getter records access. A driver that enabled the
# Runtime or Console domain serializes logged errors and trips it.
CONSOLE_PROBE_PAGE = b"""<html><title>Probe</title><body><p id="out"></p><script>
let touched = false;
const bait = new Error('bait');
Object.defineProperty(bait, 'stack', { get() { touched = true; return ''; } });
console.debug(bait);
setTimeout(() => {
  document.getElementById('out').textContent = JSON.stringify({
    touched, webdriver: navigator.webdriver,
  });
}, 200);
</script></body></html>"""

# Records the registration outcome in the DOM, where an isolated world reads it.
SW_PAGE = b"""<html><body><script>
navigator.serviceWorker.register('/sw.js').then(
  () => { document.body.dataset.sw = 'registered'; },
  () => { document.body.dataset.sw = 'refused'; });
</script></body></html>"""

IMAGE_PAGE = b'<html><title>Images</title><body><img src="/pixel.png">text</body></html>'


class CDPHandler(BrowserHandler):
    image_requests = 0

    def do_GET(self) -> None:
        pages = {"/console-probe": CONSOLE_PROBE_PAGE, "/image-page": IMAGE_PAGE, "/sw": SW_PAGE}
        if self.path == "/sw.js":
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript")
            self.end_headers()
            self.wfile.write(b"self.addEventListener('fetch', () => {});")
            return
        if self.path == "/csp":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'none'")
            self.end_headers()
            self.wfile.write(b"<html><title>CSP</title><body>locked</body></html>")
            return
        if self.path in pages:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(pages[self.path])
            return
        if self.path == "/pixel.png":
            type(self).image_requests += 1
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            return
        super().do_GET()
