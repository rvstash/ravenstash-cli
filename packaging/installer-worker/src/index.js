import installerBase64 from "./install.generated.js";
import windowsInstallerBase64 from "./install-ps1.generated.js";
import { handleRequest } from "./handler.js";

const installer = Uint8Array.from(atob(installerBase64), (character) =>
  character.charCodeAt(0),
);
const windowsInstaller = Uint8Array.from(atob(windowsInstallerBase64), (character) =>
  character.charCodeAt(0),
);

export default {
  fetch(request) {
    return handleRequest(request, {
      "/install.sh": installer,
      "/install.ps1": windowsInstaller,
    });
  },
};
