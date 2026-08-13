import { cloudflare } from "@cloudflare/vite-plugin";
import { defineConfig } from "vite";

// Dev only: one server runs the viewer and the tile Worker (in workerd) on one
// port. `vite build` stays a plain client build — the plugin would restructure
// the output for `wrangler deploy`-less hosting, but the Worker deploys
// separately (worker/) and the viewer goes to Pages as static files.
export default defineConfig(({ command }) => ({
  plugins:
    // vitest (style/ inherits this config) also resolves as "serve"
    command === "serve" && !process.env.VITEST
      ? [
          cloudflare({
            configPath: "./worker/wrangler.toml",
            // seed.sh writes the local R2 sim here (wrangler resolves state
            // next to its config file)
            persistState: { path: "./worker/.wrangler/state" },
          }),
        ]
      : [],
}));
