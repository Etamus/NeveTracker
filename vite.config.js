import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");

  return {
    base: env.VITE_BASE_PATH || "/",
    plugins: [react(), tailwindcss()],
    build: {
      rollupOptions: {
        output: {
          manualChunks(id) {
            if (!id.includes("node_modules")) return undefined;
            if (id.includes("recharts") || id.includes("d3-") || id.includes("victory-vendor")) {
              return "charts";
            }
            if (id.includes("lucide-react")) return "icons";
            return undefined;
          },
        },
      },
    },
    server: {
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
      allowedHosts: ["localhost", "127.0.0.1"],
      watch: {
        // Chromium locks its session databases while scraping. These runtime
        // files and backend outputs are not frontend sources or HMR inputs.
        ignored: [/(?:^|[/\\])(?:\.tools|\.logs|backend|\.artifacts|\.iniciar\.lock|\.install\.lock|playwright-report|test-results)(?:[/\\]|$)/],
      },
    },
  };
});
