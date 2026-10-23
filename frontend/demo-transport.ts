// Development only. Exercises the FastAPI app without a second listening socket.
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { resolve } from "node:path";
import type { Plugin } from "vite";
export function demoTransport(): Plugin {
  return {
    name: "brickpulse-offline-demo",
    apply: "serve",
    configureServer(server) {
      const root = resolve(server.config.root, "..");
      const worker = spawn(
        resolve(root, ".venv/bin/python"),
        [resolve(root, "scripts/demo_api.py")],
        {
          cwd: root,
          env: { ...process.env, PYTHONPATH: resolve(root, "src") },
          stdio: ["pipe", "pipe", "inherit"],
        },
      );
      let sequence = 0;
      const pending = new Map<
        number,
        (value: {
          status: number;
          headers: Record<string, string>;
          body: string;
        }) => void
      >();
      createInterface({ input: worker.stdout }).on("line", (line) => {
        try {
          const value = JSON.parse(line);
          pending.get(value.id)?.(value);
          pending.delete(value.id);
        } catch {
          /* Python logs are emitted on stderr. */
        }
      });
      worker.on("exit", () => {
        for (const respond of pending.values())
          respond({
            status: 503,
            headers: { "content-type": "application/json" },
            body: '{"code":"loadError"}',
          });
        pending.clear();
      });
      server.httpServer?.once("close", () => worker.kill("SIGTERM"));
      server.middlewares.use((req, res, next) => {
        if (!req.url?.startsWith("/api/") && req.url !== "/health")
          return next();
        if (!worker.stdin.writable) {
          res.statusCode = 503;
          res.end();
          return;
        }
        const id = ++sequence;
        let body = "";
        const timer = setTimeout(() => {
          pending.delete(id);
          res.statusCode = 504;
          res.end();
        }, 30_000);
        req.on("data", (chunk) => (body += chunk));
        req.on("end", () => {
          pending.set(id, (response) => {
            clearTimeout(timer);
            res.statusCode = response.status;
            for (const [k, v] of Object.entries(response.headers))
              res.setHeader(k, v);
            res.end(response.body);
          });
          worker.stdin.write(
            JSON.stringify({
              id,
              path: req.url,
              method: req.method,
              headers: {
                "accept-language": String(
                  req.headers["accept-language"] ?? "en",
                ),
              },
              body,
            }) + "\n",
          );
        });
      });
    },
  };
}
