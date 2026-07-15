// P1 (docs/UI_AUDIT.md): a blank Stats page shipped past 197 green tests
// because vite happily bundles an undeclared identifier (burnOf called from
// another function's scope). Nothing here renders components, so the ONLY
// cheap guard for that class is no-undef at lint time. Scope: exactly that
// rule — this is a tripwire, not a style regime.
import globals from "globals";

export default [
  {
    files: ["src/**/*.{js,jsx,mjs}"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: { ...globals.browser, ...globals.node },
    },
    rules: { "no-undef": "error" },
  },
];
