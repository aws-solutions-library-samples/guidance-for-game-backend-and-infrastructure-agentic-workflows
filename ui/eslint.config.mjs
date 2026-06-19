import nextCoreWebVitals from 'eslint-config-next/core-web-vitals';
import nextTypeScript from 'eslint-config-next/typescript';

// ESLint 10 flat config. eslint-config-next 16 ships native flat-config arrays
// (no FlatCompat / @eslint/eslintrc shim needed). The two custom rules below
// were previously in the legacy .eslintrc.json; they are ported here so they
// keep applying after the flat-config migration.
const eslintConfig = [
  // Replaces .eslintignore — build output and deps are not linted.
  {
    ignores: ['.next/**', 'node_modules/**', 'coverage/**', 'playwright-report/**', 'test-results/**'],
  },
  ...nextCoreWebVitals,
  ...nextTypeScript,
  {
    rules: {
      '@typescript-eslint/no-explicit-any': 'warn',
      '@typescript-eslint/no-unused-vars': 'warn',
    },
  },
];

export default eslintConfig;
