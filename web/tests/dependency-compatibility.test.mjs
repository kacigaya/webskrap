import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join, relative, resolve } from "node:path";
import test from "node:test";

const require = createRequire(import.meta.url);
const pluginRequire = createRequire(require.resolve("@next/eslint-plugin-next"));
const { getRootDirs } = pluginRequire("./utils/get-root-dirs.js");

test("Next.js ESLint resolves its glob dependency to tinyglobby without braces", () => {
  const packageJson = pluginRequire("fast-glob/package.json");
  assert.equal(packageJson.name, "tinyglobby");

  const lockfile = readFileSync(new URL("../bun.lock", import.meta.url), "utf8");
  assert.doesNotMatch(lockfile, /"(?:[^"\n]+\/)?(?:braces|micromatch)":\s*\[/);
});

test("Next.js ESLint preserves root-directory settings with the glob replacement", (t) => {
  const fixture = mkdtempSync(join(tmpdir(), "webskrap-eslint-"));
  t.after(() => rmSync(fixture, { recursive: true, force: true }));
  for (const directory of ["apps/site/src/app", "apps/docs/pages", "apps/api"]) {
    mkdirSync(join(fixture, directory), { recursive: true });
  }
  writeFileSync(join(fixture, "apps", "file.txt"), "not a directory");

  const roots = (rootDir) => getRootDirs({
    cwd: fixture,
    settings: { next: { rootDir } },
  }).map((directory) => resolve(directory)).sort();
  const expected = (...directories) => directories.map((directory) => join(fixture, directory)).sort();

  assert.deepEqual(roots(undefined), [fixture]);
  assert.deepEqual(roots(join(fixture, "apps/site")), expected("apps/site"));
  assert.deepEqual(roots(`${join(fixture, "apps/site")}/`), expected("apps/site"));
  assert.deepEqual(roots(relative(process.cwd(), join(fixture, "apps/site"))), expected("apps/site"));
  assert.deepEqual(roots(join(fixture, "apps/*")), expected("apps/site", "apps/docs", "apps/api"));
  assert.deepEqual(roots(join(fixture, "apps/{site,{docs,missing}}")), expected("apps/site", "apps/docs"));
  assert.deepEqual(roots([join(fixture, "apps/site"), join(fixture, "apps/docs"), 42]), expected("apps/site", "apps/docs"));
  assert.deepEqual(roots(join(fixture, "apps/missing")), []);
  assert.deepEqual(roots(join(fixture, "apps/file.txt")), []);
  assert.deepEqual(roots(join(fixture, "apps/{site,docs}").replaceAll("/", "\\")), expected("apps/site", "apps/docs"));
});
