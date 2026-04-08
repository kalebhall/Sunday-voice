import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
  build: {
    outDir: "dist",
    // Sourcemaps expose the full source tree in production; disable them.
    // Enable locally via VITE_SOURCEMAP=true if needed for debugging.
    sourcemap: process.env.VITE_SOURCEMAP === "true",
  },
});
