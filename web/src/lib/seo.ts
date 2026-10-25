export const SITE_URL = "https://kacigaya.github.io/webskrap";
export const SITE_NAME = "WebSkrap";
export const SOCIAL_IMAGE_URL = `${SITE_URL}/webskrap-og.png`;
export const DEFAULT_TITLE = "WebSkrap: Python Web Scraping and Browser Automation";
export const DEFAULT_DESCRIPTION =
  "Python web scraping and browser automation on real Chromium, with its own CDP client, persistent sessions, and an MCP server for LLM agents.";

export const SEO_KEYWORDS = [
  "web scraping",
  "web crawling",
  "Python web scraping",
  "Python web crawler",
  "Chrome DevTools Protocol scraping",
  "browser automation",
  "headless browser scraping",
  "scrape JavaScript pages",
  "AI web scraping",
  "LLM web scraping",
  "MCP scraping server",
  "Playwright alternative",
  "WebSkrap",
];

export function canonicalUrl(path = "/"): string {
  const normalized = path.startsWith("/") ? path : `/${path}`;
  return `${SITE_URL}${normalized}`;
}
