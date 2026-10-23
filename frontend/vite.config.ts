import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { demoTransport } from "./demo-transport";
export default defineConfig({
  plugins: [react(), demoTransport()],
  server: {
    port: 5173,
  },
  build: {
    outDir: "../src/brickpulse/server/static",
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks: {
          charts: ["recharts"],
          react: ["react", "react-dom", "react-router-dom"],
        },
      },
    },
  },
});
