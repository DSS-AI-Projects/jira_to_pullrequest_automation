import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { compile } from "json-schema-to-typescript";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const root = path.resolve(__dirname, "..", "..");
const schemaPath = path.join(root, "schema", "plan.schema.json");
const outputPath = path.join(root, "frontend", "src", "lib", "plan.gen.ts");

const schemaText = await readFile(schemaPath, "utf8");
const schema = JSON.parse(schemaText);
const generated = await compile(schema, "Plan", {
  bannerComment: "// Generated from schema/plan.schema.json. Do not edit by hand.\n",
  style: {
    singleQuote: false,
  },
});

await mkdir(path.dirname(outputPath), { recursive: true });
await writeFile(outputPath, generated, "utf8");
