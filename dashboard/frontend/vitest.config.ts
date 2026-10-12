import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// A file of its own instead of a `test` block in `vite.config.ts`: that file holds the dev proxy,
// which has nothing to do with the test run, and `vite build` should not carry the test
// configuration along.
export default defineConfig({
  plugins: [react()],
  test: {
    // `jsdom` globally, although much of the coverage is pure logic: `lib/theme.ts` needs
    // `localStorage` and `matchMedia`, and the component tests need a DOM.
    environment: "jsdom",
    globals: true,
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    restoreMocks: true,
    setupFiles: ["src/test-setup.ts"],
    // Many component tests run the whole app against a fake backend and take a few seconds on an
    // idle machine; with every core busy (default workers on a loaded host or CI runner) they
    // outlast vitest's default of 5 s per test. Each wait inside a test has its own limit
    // (`asyncUtilTimeout` in src/test-setup.ts); this is only the outer one.
    testTimeout: 60000,
  },
});
