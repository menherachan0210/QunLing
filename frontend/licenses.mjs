// Bundle the licenses of runtime dependencies beside the production assets.
import fs from "node:fs";
import path from "node:path";
const lock = JSON.parse(fs.readFileSync("package-lock.json", "utf8"));
let output = "QunLing console: third-party runtime notices\n\n";
for (const [folder, info] of Object.entries(lock.packages).sort(([a], [b]) =>
  a.localeCompare(b, "en"),
)) {
  if (!folder || info.dev) continue;
  const manifest = JSON.parse(
    fs.readFileSync(path.join(folder, "package.json"), "utf8"),
  );
  const license = fs
    .readdirSync(folder)
    .find((name) => /^licen[cs]e(?:\..*)?$/i.test(name));
  const licensePath = license ? path.join(folder,license) : path.join('licenses',manifest.name+'.txt');
  if (!fs.existsSync(licensePath)) throw new Error(`Missing license for ${manifest.name}`);
  output += `${manifest.name} ${manifest.version}\n${"=".repeat(64)}\n${fs.readFileSync(licensePath, "utf8").trim()}\n\n`;
}
fs.mkdirSync("public", { recursive: true });
fs.writeFileSync("public/licenses.txt", output.trimEnd() + "\n");
