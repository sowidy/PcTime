import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";

// Load the same browser ES module without changing the repository's package mode.
const code = await readFile(new URL("./protocol.js", import.meta.url), "utf8");
const {originOf, trimQueue} = await import("data:text/javascript;base64," + Buffer.from(code).toString("base64"));
assert.deepEqual(originOf({url: "https://example.com/private?secret=1#token"}),
  {scheme: "https", host: "example.com"});
for (const tab of [{url: "https://example.com", incognito: true}, {url: "chrome://settings"},
                   {url: "file:///secret"}, {url: "not a url"}]) assert.equal(originOf(tab), null);
const now = Date.now();
assert.equal(trimQueue([{started_at_ms: now - 700000}], now).length, 0);
assert.equal(trimQueue(Array.from({length: 1001}, () => ({started_at_ms: now})), now).length, 1000);
console.log("Extension protocol checks passed");
