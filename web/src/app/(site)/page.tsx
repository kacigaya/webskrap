import Image from "next/image";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { CodeBlock } from "@/components/code-block";
import { ThemeToggle } from "@/components/theme-toggle";
import { asset } from "@/lib/asset";
import { DEFAULT_DESCRIPTION, SITE_URL } from "@/lib/seo";

const DOCS_URL = "/docs";
const GITHUB_URL = "https://github.com/kacigaya/webskrap";
const PYPI_URL = "https://pypi.org/project/webskrap/";

const FEATURES = [
  {
    title: "Async-first",
    description:
      "Built on Playwright with an async API for concurrent data collection workflows.",
  },
  {
    title: "Browser profiles",
    description:
      "Coherent desktop and mobile profiles keep viewport, user agent, and locale consistent.",
  },
  {
    title: "Persistent sessions",
    description:
      "Reuse cookies, storage, and a user data dir across fetches to stay logged in between runs.",
  },
  {
    title: "Resource routing",
    description:
      "A resource policy blocks images, fonts, and trackers to make pages load faster and leaner.",
  },
  {
    title: "Patchright stealth",
    description:
      "Patchright ships with WebSkrap for stealth-oriented sessions and CLI fetches.",
  },
  {
    title: "Human-like clicks",
    description:
      "human_click moves the cursor along a Bezier curve with eased spacing before the click.",
  },
  {
    title: "MCP server",
    description:
      "fetch and stealth_fetch tools for Claude, Codex, and any MCP agent. They return clean page text, not raw HTML.",
  },
  {
    title: "Built for LLMs",
    description:
      "Clean-text output by default, 5-10x fewer tokens than raw HTML, plus JSON and bounded CLI output.",
  },
];

const QUICKSTART = `import asyncio

from webskrap import WebSkrapClient


async def main() -> None:
    async with WebSkrapClient() as client:
        result = await client.fetch("https://example.com")
        print(result.status)
        print(result.title)
        print(result.text[:200])


asyncio.run(main())`;

const INSTALL = `pip install webskrap
webskrap install
webskrap fetch https://example.com --format json --max-chars 12000`;

const SOFTWARE_SCHEMA = {
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  name: "WebSkrap",
  applicationCategory: "DeveloperApplication",
  operatingSystem: "Linux, macOS, Windows",
  programmingLanguage: "Python",
  description: DEFAULT_DESCRIPTION,
  url: SITE_URL,
  codeRepository: GITHUB_URL,
  downloadUrl: PYPI_URL,
  author: {
    "@type": "Person",
    name: "Gaya KACI",
    url: "https://github.com/kacigaya",
  },
  offers: {
    "@type": "Offer",
    price: "0",
    priceCurrency: "USD",
  },
};

export default function Home() {
  return (
    <main className="flex flex-1 flex-col">
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(SOFTWARE_SCHEMA) }}
      />
      {/* Nav */}
      <header className="sticky top-0 z-20 px-4 pt-4">
        <div className="mx-auto flex w-full max-w-6xl items-center justify-between rounded-2xl border bg-background/70 px-5 py-3 shadow-sm backdrop-blur-md">
          <div className="flex items-center gap-2.5">
            <Image
              src={asset("/webskrap-logo.png")}
              alt="WebSkrap"
              width={642}
              height={686}
              className="h-7 w-auto"
            />
            <span className="font-semibold tracking-tight">WebSkrap</span>
          </div>
          <nav className="flex items-center gap-2">
            <ThemeToggle />
            <Button variant="ghost" size="sm" render={<Link href={DOCS_URL} />}>
              Docs
            </Button>
            <Button variant="outline" size="sm" render={<a href={GITHUB_URL} />}>
              GitHub
            </Button>
          </nav>
        </div>
      </header>

      {/* Hero */}
      <section className="mx-auto flex w-full max-w-5xl flex-col items-center px-6 py-24 text-center">
        <Image
          src={asset("/webskrap-logo.png")}
          alt="WebSkrap logo"
          width={642}
          height={686}
          className="mb-8 h-24 w-auto"
          loading="eager"
        />
        <Badge variant="secondary" className="mb-6">
          Async-first · Playwright · Patchright · MCP
        </Badge>
        <h1 className="font-heading text-5xl font-bold tracking-tight sm:text-6xl">
          Python Web Scraping with Playwright and Patchright
        </h1>
        <p className="mt-6 max-w-2xl text-lg text-muted-foreground">
          WebSkrap is an async-first Python web scraping and browser automation toolkit for
          scraping JavaScript-heavy pages, managing persistent browser sessions, and exposing
          clean page text to LLM agents through MCP.
        </p>
        <p className="mt-4 max-w-2xl text-base text-muted-foreground">
          Scrape the web like a real browser with Playwright, Patchright stealth sessions,
          resource routing, browser profiles, and clean CLI output for crawlers and agents.
        </p>
        <div className="mt-10 flex flex-col gap-3 sm:flex-row">
          <Button size="xl" render={<Link href={DOCS_URL} />}>
            View Documentation
          </Button>
          <Button size="xl" variant="outline" render={<a href={GITHUB_URL} />}>
            Star on GitHub
          </Button>
        </div>
      </section>

      {/* Install */}
      <section className="mx-auto w-full max-w-3xl px-6 pb-24">
        <CodeBlock code={INSTALL} lang="bash" />
      </section>

      {/* Features */}
      <section className="mx-auto w-full max-w-5xl px-6 pb-24">
        <h2 className="mb-10 text-center font-heading text-3xl font-bold tracking-tight">
          Browser automation ready for agents and scripts
        </h2>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {FEATURES.map((feature) => (
            <Card key={feature.title}>
              <CardHeader>
                <CardTitle>{feature.title}</CardTitle>
                <CardDescription>{feature.description}</CardDescription>
              </CardHeader>
            </Card>
          ))}
        </div>
      </section>

      {/* Quickstart */}
      <section className="mx-auto w-full max-w-3xl px-6 pb-24">
        <h2 className="mb-6 text-center font-heading text-3xl font-bold tracking-tight">
          Quickstart
        </h2>
        <CodeBlock code={QUICKSTART} lang="python" />
      </section>

      {/* Footer */}
      <footer className="mt-auto border-t">
        <div className="mx-auto flex w-full max-w-5xl flex-col items-center justify-between gap-4 px-6 py-8 text-sm text-muted-foreground sm:flex-row">
          <span>© {new Date().getFullYear()} WebSkrap</span>
          <nav className="flex items-center gap-5">
            <Link href={DOCS_URL} className="hover:text-foreground">
              Documentation
            </Link>
            <a href={GITHUB_URL} className="hover:text-foreground">
              GitHub
            </a>
            <a href={PYPI_URL} className="hover:text-foreground">
              PyPI
            </a>
          </nav>
        </div>
      </footer>
    </main>
  );
}
