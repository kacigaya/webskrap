import type { Metadata } from "next";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { DocsPager } from "@/components/docs-pager";
import { Frame, FrameHeader, FrameTitle, FrameDescription } from "@/components/ui/frame";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  canonicalUrl,
  SITE_NAME,
  SOCIAL_IMAGE_URL,
} from "@/lib/seo";

const DESCRIPTION =
  "WebSkrap stealth comparisons and repeatable browser automation performance benchmarks for resource routing, session reuse, and concurrent fetching.";

export const metadata: Metadata = {
  title: "Benchmarks",
  description: DESCRIPTION,
  alternates: {
    canonical: canonicalUrl("/docs/benchmarks/"),
  },
  openGraph: {
    type: "article",
    url: canonicalUrl("/docs/benchmarks/"),
    title: `Benchmarks | ${SITE_NAME}`,
    description: DESCRIPTION,
    siteName: SITE_NAME,
    images: [
      {
        url: SOCIAL_IMAGE_URL,
        width: 1200,
        height: 630,
        alt: "WebSkrap documentation",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: `Benchmarks | ${SITE_NAME}`,
    description: DESCRIPTION,
    images: [SOCIAL_IMAGE_URL],
  },
};

// Map a status string to a badge variant by keyword. ponytail: keyword match, add cases if new statuses appear.
function statusVariant(value: string): BadgeProps["variant"] {
  const v = value.toLowerCase();
  if (/(pass|normal|human|identical|^0 |^0$|active|native|yes)/.test(v)) return "success";
  if (/(fail|detected|bot|mismatch|breaks|stale|unstable)/.test(v)) return "error";
  if (/(timeout|sometimes|depends)/.test(v)) return "warning";
  return "secondary";
}

const FEATURE_COLUMNS = [
  "Playwright",
  "playwright-stealth",
  "undetected-chromedriver",
  "Camoufox",
  "CloakBrowser",
  "WebSkrap",
];

const FEATURE_ROWS: { feature: string; values: string[] }[] = [
  {
    feature: "reCAPTCHA v3 score",
    values: ["0.1", "0.3-0.5", "0.3-0.7", "0.7-0.9", "0.9", "Pass headed (>=0.7 gate); scored in virtual display"],
  },
  {
    feature: "Cloudflare Turnstile",
    values: ["Fail", "Sometimes", "Sometimes", "Pass", "Pass", "Pass (non-interactive token)"],
  },
  {
    feature: "Patch level",
    values: [
      "None",
      "JS injection",
      "Config patches",
      "C++ (Firefox)",
      "C++ (Chromium)",
      "Own CDP client + native Chrome options",
    ],
  },
  {
    feature: "Survives Chrome updates",
    values: ["N/A", "Breaks often", "Breaks often", "Yes", "Yes", "Yes (stock Chrome, no patched driver)"],
  },
  {
    feature: "Maintained",
    values: ["Yes", "Stale", "Stale", "Unstable", "Active", "Active project tests"],
  },
  {
    feature: "Browser engine",
    values: ["Chromium", "Chromium", "Chrome", "Firefox", "Chromium", "Chrome/Chromium"],
  },
  {
    feature: "Playwright API",
    values: ["Native", "Native", "No (Selenium)", "No", "Native", "No (own Python API)"],
  },
];

const DETECTION_ROWS: {
  service: string;
  stock: string;
  cloak: string;
  webskrap: string;
  notes: string;
}[] = [
  {
    service: "reCAPTCHA v3",
    stock: "0.1 (bot)",
    cloak: "0.9 (human)",
    webskrap: "PASS",
    notes: "WebSkrap asserts score >=0.7 when Google's demo returns one",
  },
  {
    service: "Cloudflare Turnstile (non-interactive)",
    stock: "FAIL",
    cloak: "PASS",
    webskrap: "PASS",
    notes: "Production sitekey issued a real token without interaction, headed and on the virtual display",
  },
  {
    service: "FingerprintJS bot detection",
    stock: "DETECTED",
    cloak: "PASS",
    webskrap: "PASS",
    notes: "demo.fingerprint.com/web-scraping returns demo data",
  },
  {
    service: "BrowserScan bot detection",
    stock: "DETECTED",
    cloak: "NORMAL (4/4)",
    webskrap: "PASS",
    notes: "0 abnormal checks, headed and on the virtual display",
  },
  {
    service: "bot.incolumitas.com",
    stock: "13 fails",
    cloak: "1 fail",
    webskrap: "PASS",
    notes: "Only tolerated network/spec false positives",
  },
  {
    service: "deviceandbrowserinfo.com",
    stock: "6 true flags",
    cloak: "0 true flags",
    webskrap: "PASS",
    notes: "isBot: false",
  },
  {
    service: "bot.sannysoft.com",
    stock: "DETECTED",
    cloak: "Not listed",
    webskrap: "PASS",
    notes: "No rows flagged; WebGL rendered by Mesa lavapipe (gpu=\"mesa\") on a GPU-less host",
  },
  {
    service: "BrowserLeaks WebRTC",
    stock: "Not listed",
    cloak: "Not listed",
    webskrap: "PASS",
    notes: "No private ICE candidate IPs exposed",
  },
  {
    service: "BrowserLeaks Client Hints",
    stock: "Not listed",
    cloak: "Not listed",
    webskrap: "PASS",
    notes: "No HeadlessChrome token",
  },
  {
    service: "TLS / JA3 visibility",
    stock: "Mismatch",
    cloak: "Identical to Chrome",
    webskrap: "PASS",
    notes: "TLS/JA3/JA4 surface is visible; no proxy mismatch without proxy",
  },
  {
    service: "DNS leak standard test",
    stock: "Not listed",
    cloak: "Not listed",
    webskrap: "PASS",
    notes: "Resolver rows are public; optional proxy country/IP expectations supported",
  },
];

// Medians of three virtual-display passes.
const RESOURCE_ROUTING = [
  { policy: "DOCUMENTS", time: "174.40", vs: "0.53x" },
  { policy: "LITE", time: "204.32", vs: "0.62x" },
  { policy: "ALL", time: "328.44", vs: "1.00x" },
];

const SESSION_REUSE = [
  { mode: "Warm session reuse", time: "209.66", vs: "1.00x", best: true },
  { mode: "Cold launch per fetch", time: "1,154.79", vs: "5.51x", best: false },
];

// Median of three alternating passes per mode; ranges show run-to-run spread.
const MODE_COMPARISON = [
  {
    benchmark: "ALL policy",
    virtual: { median: "328.44", range: "301.09-351.45" },
    headed: { median: "307.55", range: "304.13-309.37" },
  },
  {
    benchmark: "LITE policy",
    virtual: { median: "204.32", range: "199.26-233.81" },
    headed: { median: "209.53", range: "195.46-211.47" },
  },
  {
    benchmark: "DOCUMENTS policy",
    virtual: { median: "174.40", range: "169.59-190.99" },
    headed: { median: "169.05", range: "165.37-174.14" },
  },
  {
    benchmark: "Warm session reuse",
    virtual: { median: "209.66", range: "200.82-226.29" },
    headed: { median: "208.64", range: "203.08-220.83" },
  },
  {
    benchmark: "Cold launch per fetch",
    virtual: { median: "1,154.79", range: "1,137.44-1,216.78" },
    headed: { median: "1,021.96", range: "947.38-1,190.56" },
  },
  {
    benchmark: "Concurrency, per page",
    virtual: { median: "160.22", range: "153.17-176.12" },
    headed: { median: "160.61", range: "157.73-167.13" },
  },
];

export default function BenchmarksPage() {
  return (
    <div className="flex flex-col gap-12">
      <header className="flex flex-col gap-4">
        <h1 className="text-balance font-heading text-4xl font-bold tracking-tight">Benchmarks</h1>
        <p className="max-w-2xl text-pretty text-muted-foreground">
          A dated stealth comparison and local performance measurements for resource
          routing, session reuse, and concurrent fetching.
        </p>
      </header>

      {/* Stealth comparison */}
      <section className="flex flex-col gap-4">
        <div className="flex flex-col gap-1">
          <h2 className="text-balance font-heading text-2xl font-bold tracking-tight">Comparison</h2>
          <p className="max-w-2xl text-sm text-muted-foreground">
            WebSkrap values are an October 25, 2026 snapshot. Stock Playwright and
            CloakBrowser values came from CloakBrowser&apos;s{" "}
            <a
              href="https://github.com/CloakHQ/CloakBrowser/blob/main/README.md"
              className="text-primary underline"
            >
              upstream README
            </a>
            , read on September 29, 2026. WebSkrap values came from a live report generated with{" "}
            <code className="rounded bg-muted px-1.5 py-0.5 text-sm">
              WEBSKRAP_BROWSER_CHANNEL=chromium WEBSKRAP_LIVE_VIRTUAL_DISPLAY=1
              WEBSKRAP_LIVE_GPU=mesa WEBSKRAP_CHROMIUM_SANDBOX=0 xvfb-run -a python
              scripts/live_stealth_report.py --no-open --report-only
            </code>
            . Both suites ran WebSkrap 3.0.0 on its own CDP client with Chrome for Testing
            156.0.8078.47 on ARM64 Linux, on a datacenter IP with no GPU and no proxy,
            rendering WebGL through Mesa lavapipe. The headed suite ran under{" "}
            <code className="rounded bg-muted px-1.5 py-0.5 text-sm">xvfb-run</code>; the
            headless suite used{" "}
            <code className="rounded bg-muted px-1.5 py-0.5 text-sm">virtual_display=True</code>
            , so both drove a headed browser on an Xvfb screen. The projects were not tested side by side. Detection sites and browser
            versions change, so these results do not predict a current score.
          </p>
        </div>

        <Frame className="gap-3 bg-transparent p-0">
          <FrameHeader className="p-0">
            <FrameTitle>Feature comparison</FrameTitle>
            <FrameDescription>WebSkrap vs other stacks</FrameDescription>
          </FrameHeader>
          <Table
            variant="card"
            scrollLabel="Feature comparison table"
            className="w-full text-xs [&_td]:whitespace-normal [&_td]:px-2.5 [&_td]:py-3 [&_td]:align-top [&_th]:whitespace-normal [&_th]:break-words [&_th]:px-2.5 [&_th]:py-3 [&_th]:align-bottom"
          >
            <TableHeader>
              <TableRow>
                <TableHead>Feature</TableHead>
                {FEATURE_COLUMNS.map((col) => (
                  <TableHead
                    key={col}
                    className={
                      col === "WebSkrap" ? "text-foreground" : undefined
                    }
                  >
                    {col}
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {FEATURE_ROWS.map((row) => (
                <TableRow key={row.feature}>
                  <TableCell className="font-medium">{row.feature}</TableCell>
                  {row.values.map((value, i) => (
                    <TableCell
                      key={i}
                      className={
                        i === FEATURE_COLUMNS.length - 1
                          ? "font-medium text-foreground"
                          : "text-muted-foreground"
                      }
                    >
                      {value}
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Frame>

        <Frame className="gap-3 bg-transparent p-0">
          <FrameHeader className="p-0">
            <FrameTitle>Detection services</FrameTitle>
            <FrameDescription>Stock Playwright vs CloakBrowser vs WebSkrap 3.0 headed on Xvfb</FrameDescription>
          </FrameHeader>
          <Table
            variant="card"
            scrollLabel="Detection services table"
            className="w-full text-xs [&_td]:whitespace-normal [&_td]:px-2.5 [&_td]:py-3 [&_td]:align-top [&_th]:whitespace-normal [&_th]:px-2.5 [&_th]:py-3 [&_th]:align-bottom"
          >
            <TableHeader>
              <TableRow>
                <TableHead>Detection service</TableHead>
                <TableHead>Stock Playwright</TableHead>
                <TableHead>CloakBrowser</TableHead>
                <TableHead>WebSkrap headed</TableHead>
                <TableHead>Notes</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {DETECTION_ROWS.map((row) => (
                <TableRow key={row.service}>
                  <TableCell className="font-medium">{row.service}</TableCell>
                  <TableCell>
                    <Badge variant={statusVariant(row.stock)}>{row.stock}</Badge>
                  </TableCell>
                  <TableCell>
                    <Badge variant={statusVariant(row.cloak)}>{row.cloak}</Badge>
                  </TableCell>
                  <TableCell>
                    <Badge variant={statusVariant(row.webskrap)}>{row.webskrap}</Badge>
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {row.notes}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Frame>

        <Frame className="gap-3 bg-transparent p-0">
          <FrameHeader className="p-0">
            <FrameDescription>
              October 25 WebSkrap live summary: 25 passed, 2 skipped, 0 failed. Headed:
              17 passed, 1 skipped. Virtual display: 8 passed, 1 skipped. Both skips were
              Are You Headless, which returned 502 Bad Gateway; the suites skip a site
              that answers with a 5xx status. reCAPTCHA v3 returned a score in both
              suites. The Turnstile demo uses a production sitekey, and the suite rejects
              the dummy token that a Cloudflare test sitekey returns.
            </FrameDescription>
          </FrameHeader>
        </Frame>
      </section>

      {/* Performance */}
      <section className="flex flex-col gap-4">
        <div className="flex flex-col gap-1">
          <h2 className="text-balance font-heading text-2xl font-bold tracking-tight">Performance</h2>
          <p className="max-w-2xl text-sm text-muted-foreground">
            Measured on 2026-10-25 with WebSkrap 3.0.0 against a local HTTP server with
            delayed assets. No external sites were contacted. Every session used the
            stealth setup:{" "}
            <code className="rounded bg-muted px-1.5 py-0.5 text-sm">headless=True</code>{" "}
            and{" "}
            <code className="rounded bg-muted px-1.5 py-0.5 text-sm">virtual_display=True</code>
            , so Chromium ran headed on a private 1920x1080 Xvfb screen. The host was
            ARM64, running Python 3.14.7 and Chrome for Testing 156.0.8078.47. Its
            Chromium sandbox was unavailable, so these runs used{" "}
            <code className="rounded bg-muted px-1.5 py-0.5 text-sm">
              WEBSKRAP_CHROMIUM_SANDBOX=0 python benchmarks.py
            </code>
            . Each figure is the median of three passes. Timings vary with hardware,
            browser version, and host load.
          </p>
        </div>

        <Frame className="gap-3 bg-transparent p-0">
          <FrameHeader className="p-0">
            <FrameTitle>Resource routing</FrameTitle>
            <FrameDescription>Full page load with delayed assets</FrameDescription>
          </FrameHeader>
          <Table variant="card" scrollLabel="Resource routing benchmark table">
            <TableHeader>
              <TableRow>
                <TableHead>Policy</TableHead>
                <TableHead className="text-right">Time (ms)</TableHead>
                <TableHead className="text-right">vs ALL</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {RESOURCE_ROUTING.map((row) => (
                <TableRow key={row.policy}>
                  <TableCell className="font-medium">
                    <code>{row.policy}</code>
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{row.time}</TableCell>
                  <TableCell className="text-right">
                    <Badge variant="secondary">{row.vs}</Badge>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <FrameHeader>
            <FrameDescription>
              DOCUMENTS took 47% less time than ALL and LITE took 38% less. All three
              passes kept the same order.
            </FrameDescription>
          </FrameHeader>
        </Frame>

        <Frame className="gap-3 bg-transparent p-0">
          <FrameHeader className="p-0">
            <FrameTitle>Session reuse</FrameTitle>
            <FrameDescription>Warm persistent session vs cold launch per fetch</FrameDescription>
          </FrameHeader>
          <Table variant="card" scrollLabel="Session reuse benchmark table">
            <TableHeader>
              <TableRow>
                <TableHead>Mode</TableHead>
                <TableHead className="text-right">Time (ms)</TableHead>
                <TableHead className="text-right">vs warm</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {SESSION_REUSE.map((row) => (
                <TableRow key={row.mode}>
                  <TableCell className="font-medium">{row.mode}</TableCell>
                  <TableCell className="text-right tabular-nums">{row.time}</TableCell>
                  <TableCell className="text-right">
                    <Badge variant={row.best ? "success" : "secondary"}>{row.vs}</Badge>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <FrameHeader>
            <FrameDescription>
              Cold launch took 5.51 times as long as warm session reuse. Each cold
              fetch also starts its own Xvfb server, which adds to launch cost.
            </FrameDescription>
          </FrameHeader>
        </Frame>

        <Frame className="gap-3 bg-transparent p-0">
          <FrameHeader className="p-0">
            <FrameTitle>Concurrency</FrameTitle>
            <FrameDescription>8 pages per batch from one session</FrameDescription>
          </FrameHeader>
          <Frame className="bg-transparent p-0">
            <FrameHeader className="flex-row items-baseline gap-3">
              <span className="shrink-0 whitespace-nowrap font-heading text-3xl font-bold tabular-nums sm:text-4xl">160.22 ms</span>
              <FrameDescription>batch time divided by 8 pages</FrameDescription>
            </FrameHeader>
            <FrameDescription>
              Median batch time was 1,281.78 ms (1,225.39 to 1,408.99 ms across passes).
              The per-page figure is a throughput equivalent, not the latency of an
              individual page.
            </FrameDescription>
          </Frame>
        </Frame>

        <Frame className="gap-3 bg-transparent p-0">
          <FrameHeader className="p-0">
            <FrameTitle>Headed vs virtual display</FrameTitle>
            <FrameDescription>
              Median time in ms, with the range across three passes
            </FrameDescription>
          </FrameHeader>
          <Table variant="card" scrollLabel="Headed and virtual display benchmark table">
            <TableHeader>
              <TableRow>
                <TableHead>Benchmark</TableHead>
                <TableHead className="text-right">Virtual display</TableHead>
                <TableHead className="text-right">Headed</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {MODE_COMPARISON.map((row) => (
                <TableRow key={row.benchmark}>
                  <TableCell className="font-medium">{row.benchmark}</TableCell>
                  {[row.virtual, row.headed].map((result, index) => (
                    <TableCell key={index} className="text-right tabular-nums">
                      {result.median}
                      <span className="block text-xs text-muted-foreground">
                        {result.range}
                      </span>
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <FrameHeader>
            <FrameDescription>
              Measured on 2026-10-25 on the same host, alternating three passes of{" "}
              <code className="rounded bg-muted px-1.5 py-0.5 text-sm">
                python benchmarks.py
              </code>{" "}
              with three passes of{" "}
              <code className="rounded bg-muted px-1.5 py-0.5 text-sm">
                xvfb-run -a python benchmarks.py --mode headed
              </code>
              , both with{" "}
              <code className="rounded bg-muted px-1.5 py-0.5 text-sm">
                WEBSKRAP_CHROMIUM_SANDBOX=0
              </code>
              . Headed mode used{" "}
              <code className="rounded bg-muted px-1.5 py-0.5 text-sm">headless=False</code>{" "}
              on a 1920x1080 Xvfb screen started by xvfb-run instead of one per
              session. Once the browser is running, the two modes differ by less than
              the spread between passes. Cold launch varied the most.
            </FrameDescription>
          </FrameHeader>
        </Frame>
      </section>

      <p className="text-sm text-muted-foreground">
        Each result averages 20 measured runs after two warm-up runs. See{" "}
        <a
          href="https://github.com/kacigaya/webskrap/blob/main/benchmarks.py"
          className="text-primary underline"
        >
          benchmarks.py
        </a>{" "}
        for methodology.
      </p>
      <DocsPager href="/docs/benchmarks" />
    </div>
  );
}
