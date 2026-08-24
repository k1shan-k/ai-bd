import { existsSync } from "node:fs";
import { loadEnvFile } from "node:process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const localEnvironment = resolve(frontendRoot, ".env.local");
if (existsSync(localEnvironment)) loadEnvFile(localEnvironment);
process.chdir(frontendRoot);

await import("../.next/standalone/server.js");
