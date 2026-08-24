import { access, cp, mkdir, rm } from "node:fs/promises";
import { constants } from "node:fs";
import { resolve } from "node:path";

async function exists(path) {
  try {
    await access(path, constants.F_OK);
    return true;
  } catch {
    return false;
  }
}

const root = process.cwd();
const standalone = resolve(root, ".next", "standalone");
if (!(await exists(resolve(standalone, "server.js")))) {
  throw new Error("Next standalone server was not generated");
}

const staticSource = resolve(root, ".next", "static");
const staticTarget = resolve(standalone, ".next", "static");
await rm(staticTarget, { recursive: true, force: true });
await mkdir(resolve(standalone, ".next"), { recursive: true });
await cp(staticSource, staticTarget, { recursive: true });

const publicSource = resolve(root, "public");
const publicTarget = resolve(standalone, "public");
await rm(publicTarget, { recursive: true, force: true });
if (await exists(publicSource)) await cp(publicSource, publicTarget, { recursive: true });

console.log("Prepared standalone Next.js assets");
