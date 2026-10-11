import assert from "node:assert/strict";
import test from "node:test";

import { handleRequest } from "../src/handler.js";

const installer = "#!/usr/bin/env bash\nset -euo pipefail\n";
const installers = { "/install.sh": installer, "/install.ps1": "# PowerShell\n" };

test("serves the exact installer with security headers", async () => {
  const response = handleRequest(
    new Request("https://ravenstash.com/install.sh?release=current"),
    installers,
  );

  assert.equal(response.status, 200);
  assert.equal(await response.text(), installer);
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.equal(response.headers.get("x-content-type-options"), "nosniff");
  assert.equal(
    response.headers.get("strict-transport-security"),
    "max-age=31536000; includeSubDomains",
  );
});

test("HEAD returns the same headers without a body", async () => {
  const response = handleRequest(
    new Request("https://ravenstash.com/install.sh", { method: "HEAD" }),
    installers,
  );

  assert.equal(response.status, 200);
  assert.equal(await response.text(), "");
  assert.equal(response.headers.get("content-type"), "text/plain; charset=utf-8");
});

test("rejects methods and routes outside the exact binding", () => {
  const post = handleRequest(
    new Request("https://ravenstash.com/install.sh", { method: "POST" }),
    installers,
  );
  const other = handleRequest(
    new Request("https://ravenstash.com/other"),
    installers,
  );
  const insecure = handleRequest(
    new Request("http://ravenstash.com/install.sh"),
    installers,
  );

  assert.equal(post.status, 405);
  assert.equal(post.headers.get("allow"), "GET, HEAD");
  assert.equal(other.status, 404);
  assert.equal(insecure.status, 404);
});

test("serves the Windows installer", async () => {
  const response = handleRequest(
    new Request("https://ravenstash.com/install.ps1"),
    installers,
  );

  assert.equal(response.status, 200);
  assert.equal(await response.text(), "# PowerShell\n");
});
