import { defineConfig, globalIgnores } from "eslint/config";
import { fixupPluginRules } from "@eslint/compat";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  // Remove this shim when eslint-plugin-react supports ESLint 10.
  ...nextVitals.map((config) =>
    config.plugins?.react
      ? { ...config, plugins: { ...config.plugins, react: fixupPluginRules(config.plugins.react) } }
      : config,
  ),
  ...nextTs,
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
  ]),
]);

export default eslintConfig;
