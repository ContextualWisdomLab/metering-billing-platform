import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

test("packed package imports in a clean Node process", () => {
  const packageRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
  const temporaryDirectory = mkdtempSync(join(tmpdir(), "cwl-typescript-package-"));
  try {
    execFileSync("npm", ["pack", "--ignore-scripts", "--pack-destination", temporaryDirectory], {
      cwd: packageRoot,
      stdio: "pipe",
    });
    const tarball = readdirSync(temporaryDirectory).find((name) => name.endsWith(".tgz"));
    assert.ok(tarball);
    const consumerDirectory = join(temporaryDirectory, "consumer");
    mkdirSync(consumerDirectory);
    writeFileSync(join(consumerDirectory, "package.json"), JSON.stringify({ type: "module" }));
    execFileSync("npm", ["install", "--ignore-scripts", "--no-package-lock", join(temporaryDirectory, tarball)], {
      cwd: consumerDirectory,
      stdio: "ignore",
    });
    writeFileSync(join(consumerDirectory, "import.mjs"), [
      'import { ProducerContractError, buildUsageEvent } from "@contextualwisdomlab/metering-producer";',
      "if (typeof buildUsageEvent !== \"function\" || typeof ProducerContractError !== \"function\") process.exit(1);",
    ].join("\n"));
    execFileSync(process.execPath, ["import.mjs"], { cwd: consumerDirectory, stdio: "pipe" });
  } finally {
    rmSync(temporaryDirectory, { recursive: true, force: true });
  }
});
