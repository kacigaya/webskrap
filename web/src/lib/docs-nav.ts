interface NavItem {
  title: string;
  href: string;
}

interface NavSection {
  title?: string;
  items: NavItem[];
}

export const NAV: NavSection[] = [
  { items: [{ title: "Home", href: "/docs" }] },
  {
    title: "Getting Started",
    items: [
      { title: "Installation", href: "/docs/getting-started/installation" },
      { title: "Quickstart", href: "/docs/getting-started/quickstart" },
    ],
  },
  {
    title: "User Guide",
    items: [
      { title: "Client", href: "/docs/user-guide/client" },
      { title: "Sessions", href: "/docs/user-guide/sessions" },
      { title: "Humanizer", href: "/docs/user-guide/humanizer" },
      { title: "Profiles", href: "/docs/user-guide/profiles" },
      { title: "Stealth", href: "/docs/user-guide/stealth" },
      { title: "Resource Policy", href: "/docs/user-guide/resource-policy" },
      { title: "CLI", href: "/docs/user-guide/cli" },
      { title: "MCP Server", href: "/docs/user-guide/mcp" },
    ],
  },
  {
    title: "Guides",
    items: [
      { title: "Python Web Scraping", href: "/docs/guides/python-web-scraping" },
      { title: "Scrape JavaScript Pages", href: "/docs/guides/scrape-javascript-pages" },
      { title: "Web Crawling with Python", href: "/docs/guides/web-crawling-with-python" },
      { title: "LLM Web Scraping", href: "/docs/guides/llm-web-scraping" },
      { title: "WebSkrap vs Playwright", href: "/docs/comparisons/webskrap-vs-playwright" },
    ],
  },
  {
    title: "Reference",
    items: [
      { title: "API Reference", href: "/docs/api-reference" },
      { title: "Benchmarks", href: "/docs/benchmarks" },
      { title: "Development", href: "/docs/development" },
    ],
  },
];

// Nav hrefs served by a dedicated React route, not the markdown catch-all.
const CUSTOM_ROUTES = new Set(["/docs/benchmarks"]);

// Every markdown doc slug derived from the nav (["/docs"] -> []).
export function getDocSlugs(): string[][] {
  return NAV.flatMap((section) =>
    section.items
      .filter((item) => !CUSTOM_ROUTES.has(item.href))
      .map((item) =>
        item.href.replace(/^\/docs\/?/, "").split("/").filter(Boolean),
      ),
  );
}
