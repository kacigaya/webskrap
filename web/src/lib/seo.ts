export const SITE_URL = "https://kacigaya.github.io/webskrap";
export const SITE_NAME = "WebSkrap";
export const SOCIAL_IMAGE_URL = `${SITE_URL}/webskrap-logo.png`;
export const DEFAULT_TITLE = "WebSkrap: Python Web Scraping and Browser Automation";
export const DEFAULT_DESCRIPTION =
  "WebSkrap is a Python web scraping and web crawling toolkit built on Playwright and Patchright. Scrape JavaScript pages, automate browsers, use persistent sessions, and expose scraping tools to LLM agents through MCP.";

export const SEO_KEYWORDS = [
  "web scraping",
  "web crawling",
  "Python web scraping",
  "Python web crawler",
  "Playwright scraping",
  "browser automation",
  "headless browser scraping",
  "scrape JavaScript pages",
  "AI web scraping",
  "LLM web scraping",
  "MCP scraping server",
  "Patchright",
  "WebSkrap",
];

export function canonicalUrl(path = "/"): string {
  const normalized = path.startsWith("/") ? path : `/${path}`;
  return `${SITE_URL}${normalized}`;
}
