import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests",
  // CI runners start the demo API cold and run slower than a workstation.
  expect: { timeout: process.env.CI ? 15_000 : 5_000 },
  retries: process.env.CI ? 1 : 0,
  use: { baseURL: "http://127.0.0.1:5173", headless: true },
  webServer: {
    command: "npm run dev",
    url: "http://127.0.0.1:5173",
    reuseExistingServer: true,
  },
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 1000 } } },
    { name: "mobile", use: { viewport: { width: 390, height: 844 } } },
  ],
});
