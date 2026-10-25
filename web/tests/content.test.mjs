import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import test from "node:test";

const contentDir = new URL("../content/", import.meta.url);

// The site exports with trailingSlash: true, so a link without the slash
// costs every reader and crawler a 301 redirect.
test("internal Markdown links end with a trailing slash", () => {
  const files = readdirSync(contentDir, { recursive: true }).filter((file) => file.endsWith(".md"));
  const offenders = files.flatMap((file) => {
    const text = readFileSync(new URL(file, contentDir), "utf8");
    return [...text.matchAll(/\]\((\/[^)#\s]*)(?:#[^)]*)?\)/g)]
      .map((match) => match[1])
      .filter((href) => !href.endsWith("/") && !/\.[a-z0-9]+$/i.test(href))
      .map((href) => `${file}: ${href}`);
  });
  assert.deepEqual(offenders, []);
});

// Pages without frontmatter fall back to the site-wide title and description,
// so search results show duplicate snippets.
test("every doc page sets its own title and description", () => {
  const files = readdirSync(contentDir, { recursive: true }).filter((file) => file.endsWith(".md"));
  const missing = files.filter((file) => {
    const text = readFileSync(new URL(file, contentDir), "utf8");
    const frontmatter = text.match(/^---\n([\s\S]*?)\n---/)?.[1] ?? "";
    return !/^title:\s*\S/m.test(frontmatter) || !/^description:\s*\S/m.test(frontmatter);
  });
  assert.deepEqual(missing, []);
});
