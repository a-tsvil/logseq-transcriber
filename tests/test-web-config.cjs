const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "../deploy/training-config.js"), "utf8");

function load(origin, existing) {
  const values = new Map(existing ? [["sync-server-url", existing]] : []);
  vm.runInNewContext(script, {
    URL,
    window: {
      location: { origin },
      localStorage: {
        getItem: (key) => values.get(key) ?? null,
        setItem: (key, value) => values.set(key, value),
      },
    },
  });
  return values.get("sync-server-url");
}

test("public browser uses HTTPS sync on the same IP", () => {
  assert.equal(load("https://203.0.113.10"), "https://203.0.113.10:8443");
});

test("SSH browser uses the forwarded sync port", () => {
  assert.equal(load("http://localhost:3001"), "http://localhost:8787");
});

test("an explicitly configured server is preserved", () => {
  assert.equal(load("https://203.0.113.10", "https://sync.example.com"), "https://sync.example.com");
});
