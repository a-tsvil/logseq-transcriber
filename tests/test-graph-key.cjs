const { test } = require('node:test');
const assert = require('node:assert/strict');
const { webcrypto } = require('node:crypto');
const { subtle } = webcrypto;
const binary = value => '~b' + Buffer.from(value).toString('base64');

async function fixture(version) {
  const pair = await subtle.generateKey({ name: 'RSA-OAEP', modulusLength: 2048,
    publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' }, true, ['encrypt', 'decrypt']);
  const salt = webcrypto.getRandomValues(new Uint8Array(16));
  const iv = webcrypto.getRandomValues(new Uint8Array(12));
  const passwordKey = await subtle.importKey('raw', new TextEncoder().encode('test-password'), 'PBKDF2', false, ['deriveKey']);
  const derived = await subtle.deriveKey({ name: 'PBKDF2', salt, iterations: version ? 600000 : 100000, hash: 'SHA-256' },
    passwordKey, { name: 'AES-GCM', length: 256 }, false, ['encrypt']);
  const encryptedPrivate = await subtle.encrypt({ name: 'AES-GCM', iv }, derived, await subtle.exportKey('pkcs8', pair.privateKey));
  const expected = webcrypto.getRandomValues(new Uint8Array(32));
  const encryptedAES = await subtle.encrypt({ name: 'RSA-OAEP' }, pair.publicKey, expected);
  const parts = [binary(salt), binary(iv), binary(encryptedPrivate)];
  if (version) parts.unshift(version);
  return { expected: Buffer.from(expected).toString('base64'), bundle: {
    graph_id: 'test-graph', encrypted_private_key: JSON.stringify(parts),
    encrypted_aes_key: JSON.stringify(["~#'", binary(encryptedAES)]),
  } };
}

test('unlocks current and legacy Logseq key packages without exporting the account private key', async () => {
  const { unlockGraphKey } = await import('../scripts/unlock-graph-key.mjs');
  for (const version of ['20251210', null]) {
    const { bundle, expected } = await fixture(version);
    assert.deepEqual(await unlockGraphKey(bundle, 'test-password'), { graph_id: 'test-graph', key: expected });
    await assert.rejects(unlockGraphKey(bundle, 'wrong-password'));
    await assert.rejects(unlockGraphKey({ ...bundle, encrypted_aes_key: JSON.stringify(["~#'", binary(new Uint8Array(256))]) }, 'test-password'));
  }
});

test('rejects unsupported private-key versions', async () => {
  const { unlockGraphKey } = await import('../scripts/unlock-graph-key.mjs');
  await assert.rejects(unlockGraphKey({ encrypted_private_key: '["20990101","~b","~b","~b"]' }, 'test-password'), /Unsupported key version/);
});
