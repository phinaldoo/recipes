import { readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

const projectRoot = fileURLToPath(new URL(".", import.meta.url));
const staticRoot = fileURLToPath(new URL("./app/static/", import.meta.url));

const inputs = Object.fromEntries(
  ["css", "js"].flatMap((directory) =>
    readdirSync(new URL(`./app/static/${directory}/`, import.meta.url), {
      withFileTypes: true,
    })
      .filter(
        (entry) =>
          entry.isFile() && entry.name.endsWith(`.${directory === "css" ? "css" : "js"}`),
      )
      .map((entry) => {
        const filename = entry.name.slice(0, entry.name.lastIndexOf("."));
        return [
          `${directory}/${filename}`,
          fileURLToPath(new URL(`./app/static/${directory}/${entry.name}`, import.meta.url)),
        ];
      }),
  ),
);

export default defineConfig({
  root: staticRoot,
  base: "/static/",
  publicDir: false,
  build: {
    outDir: `${projectRoot}/app/static/dist`,
    emptyOutDir: true,
    manifest: true,
    modulePreload: false,
    rollupOptions: {
      input: inputs,
      output: {
        entryFileNames: "assets/[name]-[hash].js",
        chunkFileNames: "assets/chunks/[name]-[hash].js",
        assetFileNames: "assets/[name]-[hash][extname]",
      },
    },
  },
});
