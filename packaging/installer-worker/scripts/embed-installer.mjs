import { readFile, writeFile } from "node:fs/promises";

const installerPath = new URL("../src/install.sh", import.meta.url);
const windowsInstallerPath = new URL("../src/install.ps1", import.meta.url);
const canonicalWindowsInstallerPath = new URL("../../install.ps1", import.meta.url);
const generatedModulePath = new URL("../src/install.generated.js", import.meta.url);
const windowsGeneratedModulePath = new URL("../src/install-ps1.generated.js", import.meta.url);
const installer = await readFile(installerPath);
let windowsInstaller;
try {
  windowsInstaller = await readFile(windowsInstallerPath);
} catch (error) {
  if (error.code !== "ENOENT") throw error;
  windowsInstaller = await readFile(canonicalWindowsInstallerPath);
}
const generatedModule = `export default ${JSON.stringify(installer.toString("base64"))};\n`;
const windowsGeneratedModule = `export default ${JSON.stringify(windowsInstaller.toString("base64"))};\n`;

await writeFile(generatedModulePath, generatedModule, { mode: 0o600 });
await writeFile(windowsGeneratedModulePath, windowsGeneratedModule, { mode: 0o600 });
