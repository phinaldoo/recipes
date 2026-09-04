import { createHash } from "node:crypto";
import {
  copyFile,
  mkdir,
  readFile,
  readdir,
  writeFile,
} from "node:fs/promises";
import { basename, extname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = fileURLToPath(new URL("..", import.meta.url));
const staticRoot = resolve(projectRoot, "app/static");
const distRoot = resolve(staticRoot, "dist");
const outputRoot = resolve(distRoot, "assets");
const viteManifestPath = resolve(distRoot, ".vite/manifest.json");
const outputManifestPath = resolve(distRoot, "asset-manifest.json");
const serviceWorkerSourcePath = resolve(staticRoot, "service-worker.js");
const serviceWorkerOutputPath = resolve(distRoot, "service-worker.js");
const offlineTemplatePath = resolve(projectRoot, "app/templates/offline.html");

const hash = (content) => createHash("sha256").update(content).digest("hex");
const fingerprintPattern = /-[A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+$/;

async function filesBelow(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const paths = await Promise.all(
    entries.map(async (entry) => {
      const path = resolve(directory, entry.name);
      return entry.isDirectory() ? filesBelow(path) : [path];
    }),
  );
  return paths.flat().sort();
}

async function sourceEntries() {
  const logicalPaths = [];
  for (const directory of ["css", "js"]) {
    const entries = await readdir(resolve(staticRoot, directory), { withFileTypes: true });
    for (const entry of entries) {
      if (entry.isFile() && entry.name.endsWith(`.${directory}`)) {
        logicalPaths.push(`${directory}/${entry.name}`);
      }
    }
  }
  return logicalPaths.sort();
}

async function fingerprintPwaAssets(assets) {
  const pwaRoot = resolve(staticRoot, "pwa");
  const destinationRoot = resolve(outputRoot, "pwa");
  await mkdir(destinationRoot, { recursive: true });

  for (const sourcePath of await filesBelow(pwaRoot)) {
    const content = await readFile(sourcePath);
    const extension = extname(sourcePath);
    const stem = basename(sourcePath, extension);
    const outputName = `${stem}-${hash(content).slice(0, 12)}${extension}`;
    const destination = resolve(destinationRoot, outputName);
    await copyFile(sourcePath, destination);
    assets[`pwa/${basename(sourcePath)}`] = `/static/assets/pwa/${outputName}`;
  }
}

async function validateTemplateAssetReferences(assets) {
  const templatesRoot = resolve(projectRoot, "app/templates");
  for (const templatePath of await filesBelow(templatesRoot)) {
    if (!templatePath.endsWith(".html")) continue;
    const template = await readFile(templatePath, "utf8");
    const references = template.matchAll(/asset\(\s*["']([^"']+)["']\s*\)/g);
    for (const reference of references) {
      if (!(reference[1] in assets)) {
        throw new Error(
          `Template ${relative(projectRoot, templatePath)} references unknown asset ${reference[1]}`,
        );
      }
    }
  }
}

const viteManifest = JSON.parse(await readFile(viteManifestPath, "utf8"));
const assets = {};

for (const logicalPath of await sourceEntries()) {
  const entry = Object.values(viteManifest).find(
    (candidate) => candidate.isEntry === true && candidate.src === logicalPath,
  );
  if (!entry) {
    throw new Error(`Vite manifest is missing the frontend entry ${logicalPath}`);
  }
  assets[logicalPath] = `/static/${entry.file}`;
}

await fingerprintPwaAssets(assets);
await validateTemplateAssetReferences(assets);

const outputFiles = await filesBelow(outputRoot);
for (const outputFile of outputFiles) {
  if (!fingerprintPattern.test(basename(outputFile))) {
    throw new Error(`Frontend build emitted a non-fingerprinted asset: ${outputFile}`);
  }
}

const serviceWorkerTemplate = await readFile(serviceWorkerSourcePath, "utf8");
const offlineTemplate = await readFile(offlineTemplatePath);
const sourceIdentity = createHash("sha256");
const sourceFiles = [
  ...(await filesBelow(resolve(staticRoot, "css"))),
  ...(await filesBelow(resolve(staticRoot, "js"))),
  ...(await filesBelow(resolve(staticRoot, "pwa"))),
  serviceWorkerSourcePath,
  offlineTemplatePath,
].sort();

for (const sourceFile of sourceFiles) {
  sourceIdentity.update(relative(projectRoot, sourceFile).split("\\").join("/"));
  sourceIdentity.update(await readFile(sourceFile));
}
const sourceDigest = sourceIdentity.digest("hex");
const buildIdentity = createHash("sha256");
const staticUrls = outputFiles.map(
  (outputFile) => `/static/${relative(distRoot, outputFile).split("\\").join("/")}`,
);
const precacheStaticUrls = staticUrls
  .filter((url) => url !== assets["pwa/og.png"])
  .sort();

for (const outputFile of outputFiles) {
  buildIdentity.update(relative(distRoot, outputFile).split("\\").join("/"));
  buildIdentity.update(await readFile(outputFile));
}
buildIdentity.update(serviceWorkerTemplate);
buildIdentity.update(offlineTemplate);
buildIdentity.update(JSON.stringify(precacheStaticUrls));

const buildId = buildIdentity.digest("hex").slice(0, 16);
const offlineUrl = `/offline?v=${buildId}`;
const manifestUrl = `/manifest.webmanifest?v=${buildId}`;
const precache = [...precacheStaticUrls, offlineUrl];
const cacheName = `rezepte-static-${buildId}`;

const serviceWorker = serviceWorkerTemplate
  .replace('"__CACHE_NAME__"', JSON.stringify(cacheName))
  .replace('"__OFFLINE_URL__"', JSON.stringify(offlineUrl))
  .replace("__STATIC_ASSETS__", JSON.stringify(precache, null, 2));

if (serviceWorker.includes("__CACHE_NAME__") || serviceWorker.includes("__STATIC_ASSETS__")) {
  throw new Error("Service-worker template contains an unresolved build placeholder");
}

const sortedAssets = Object.fromEntries(
  Object.entries(assets).sort(([left], [right]) => left.localeCompare(right)),
);
const manifest = {
  schema: 1,
  build_id: buildId,
  source_digest: sourceDigest,
  assets: sortedAssets,
  precache,
  offline_url: offlineUrl,
  manifest_url: manifestUrl,
};

await writeFile(outputManifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
await writeFile(serviceWorkerOutputPath, serviceWorker);
