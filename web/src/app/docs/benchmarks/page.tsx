import type { Metadata } from "next";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Frame, FrameHeader, FrameTitle, FrameDescription } from "@/components/ui/frame";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

export const metadata: Metadata = {
  title: "Benchmarks — WebSkrap",
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
  "WebSkrap patchright",
];

const FEATURE_ROWS: { feature: string; values: string[] }[] = [
  {
    feature: "reCAPTCHA v3 score",
    values: ["0.1", "0.3-0.5", "0.3-0.7", "0.7-0.9", "0.9", "Pass in headed mode (>=0.7 gate)"],
  },
  {
    feature: "Cloudflare Turnstile",
    values: ["Fail", "Sometimes", "Sometimes", "Pass", "Pass", "Renders challenge surface"],
  },
  {
    feature: "Patch level",
    values: [
      "None",
      "JS injection",
      "Config patches",
      "C++ (Firefox)",
      "C++ (Chromium)",
      "Patchright driver + native Chrome options",
    ],
  },
  {
    feature: "Survives Chrome updates",
    values: ["N/A", "Breaks often", "Breaks often", "Yes", "Yes", "Depends on Chrome + Patchright"],
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
    values: ["Native", "Native", "No (Selenium)", "No", "Native", "Native-compatible"],
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
    notes: "Public demo renders the challenge surface",
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
    notes: "0 abnormal checks in headed run",
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
    webskrap: "TIMEOUT",
    notes: "Latest run timed out waiting for networkidle",
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

const RESOURCE_ROUTING = [
  { policy: "DOCUMENTS", time: "140.87", vs: "0.43x", best: true },
  { policy: "LITE", time: "260.49", vs: "0.80x", best: false },
  { policy: "ALL", time: "325.58", vs: "1.0x", best: false },
];

const SESSION_REUSE = [
  { mode: "Warm session reuse", time: "180.62", vs: "1.0x", best: true },
  { mode: "Cold launch per fetch", time: "315.28", vs: "1.75x", best: false },
];

export default function BenchmarksPage() {
  return (
    <div className="flex flex-col gap-12">
      <header className="flex flex-col gap-4">
        <h1 className="font-heading text-4xl font-bold tracking-tight">Benchmarks</h1>
        <p className="max-w-2xl text-muted-foreground">
          Stealth comparison against other anti-detect stacks, plus performance benchmarks
          for what WebSkrap actually does: resource routing, session reuse, and concurrent
          fetching.
        </p>
      </header>

      {/* Stealth comparison */}
      <section className="flex flex-col gap-4">
        <div className="flex flex-col gap-1">
          <h2 className="font-heading text-2xl font-bold tracking-tight">Comparison</h2>
          <p className="max-w-2xl text-sm text-muted-foreground">
            CloakBrowser values are copied from its{" "}
            <a
              href="https://github.com/CloakHQ/CloakBrowser/blob/main/README.md"
              className="text-primary underline"
            >
              upstream README
            </a>
            . WebSkrap values are a snapshot from the local live report generated on
            2026-06-26 with{" "}
            <code className="rounded bg-muted px-1.5 py-0.5 text-sm">
              python scripts/live_stealth_report.py --no-open --report-only
            </code>
            .
          </p>
        </div>

        <Frame className="gap-3 bg-transparent p-0">
          <FrameHeader className="p-0">
            <FrameTitle>Feature comparison</FrameTitle>
            <FrameDescription>WebSkrap patchright vs other stacks</FrameDescription>
          </FrameHeader>
          <Table
            variant="card"
            className="w-full text-xs [&_td]:whitespace-normal [&_td]:px-2.5 [&_td]:py-3 [&_td]:align-top [&_th]:whitespace-normal [&_th]:break-words [&_th]:px-2.5 [&_th]:py-3 [&_th]:align-bottom"
          >
            <TableHeader>
              <TableRow>
                <TableHead>Feature</TableHead>
                {FEATURE_COLUMNS.map((col) => (
                  <TableHead
                    key={col}
                    className={
                      col === "WebSkrap patchright" ? "text-foreground" : undefined
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
            <FrameDescription>Stock Playwright vs CloakBrowser vs WebSkrap patchright headed</FrameDescription>
          </FrameHeader>
          <Table
            variant="card"
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
              Latest WebSkrap live summary: 24 passed, 2 failed, 1 skipped. Headed: 17
              passed, 1 failed. Headless: 7 passed, 1 failed, 1 skipped. The two failures
              were Sannysoft headed/headless networkidle timeouts; the headless skip was
              reCAPTCHA v3 not returning a score from Google&apos;s public demo.
            </FrameDescription>
          </FrameHeader>
        </Frame>
      </section>

      {/* Performance */}
      <section className="flex flex-col gap-4">
        <div className="flex flex-col gap-1">
          <h2 className="font-heading text-2xl font-bold tracking-tight">Performance</h2>
          <p className="max-w-2xl text-sm text-muted-foreground">
            Run against a local HTTP server serving a synthetic page with many delayed
            sub-resources. No external sites are contacted, so results are deterministic.
            This snapshot was generated on 2026-07-30 on a single ARM64 machine — run
            it yourself with{" "}
            <code className="rounded bg-muted px-1.5 py-0.5 text-sm">python benchmarks.py</code>.
          </p>
        </div>

        <Frame className="gap-3 bg-transparent p-0">
          <FrameHeader className="p-0">
            <FrameTitle>Resource routing</FrameTitle>
            <FrameDescription>Full page load with delayed assets</FrameDescription>
          </FrameHeader>
          <Table variant="card">
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
                    <Badge variant={row.best ? "success" : "secondary"}>{row.vs}</Badge>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <FrameHeader>
            <FrameDescription>
              Blocking images, fonts, and media (LITE) cuts load time ~20%; also dropping
              stylesheets (DOCUMENTS) reaches ~57%.
            </FrameDescription>
          </FrameHeader>
        </Frame>

        <Frame className="gap-3 bg-transparent p-0">
          <FrameHeader className="p-0">
            <FrameTitle>Session reuse</FrameTitle>
            <FrameDescription>Warm persistent session vs cold launch per fetch</FrameDescription>
          </FrameHeader>
          <Table variant="card">
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
              Reusing a persistent session avoids per-fetch browser/context startup, roughly
              1.75x faster than launching cold each time.
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
              <span className="font-heading text-4xl font-bold tabular-nums">~151 ms</span>
              <FrameDescription>average per page</FrameDescription>
            </FrameHeader>
          </Frame>
        </Frame>
      </section>

      <p className="text-sm text-muted-foreground">
        Benchmarks average 20+ navigations after warm-up. See{" "}
        <a
          href="https://github.com/kacigaya/webskrap/blob/main/benchmarks.py"
          className="text-primary underline"
        >
          benchmarks.py
        </a>{" "}
        for methodology.
      </p>
    </div>
  );
}
