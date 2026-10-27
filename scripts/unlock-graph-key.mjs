import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { pathToFileURL } from 'node:url';

const { subtle } = webcrypto;

// These are the binary packages emitted by the pinned Logseq Transit writer.
function binary(value) {
  if (Array.isArray(value)) {
    assert.equal(value[0], "~#'");
    assert.equal(value.length, 2);
    value = value[1];
  }
  assert.equal(typeof value, 'string');
  assert.ok(value.startsWith('~b'));
  const bytes = Buffer.from(value.slice(2), 'base64');
  assert.equal(bytes.toString('base64'), value.slice(2));
  return bytes;
}

export async function unlockGraphKey(bundle, password) {
  const parts = JSON.parse(bundle.encrypted_private_key);
  assert.ok(Array.isArray(parts) && [3, 4].includes(parts.length));
  const version = parts.length === 4 ? parts.shift() : null;
  assert.ok(version === null || version === '20251210', 'Unsupported key version');
  const [salt, iv, encryptedPrivate] = parts.map(binary);
  assert.equal(salt.length, 16);
  assert.equal(iv.length, 12);
  const passwordKey = await subtle.importKey('raw', new TextEncoder().encode(password), 'PBKDF2', false, ['deriveKey']);
  const derived = await subtle.deriveKey(
    { name: 'PBKDF2', salt, iterations: version ? 600000 : 100000, hash: 'SHA-256' },
    passwordKey, { name: 'AES-GCM', length: 256 }, false, ['decrypt']);
  const pkcs8 = await subtle.decrypt({ name: 'AES-GCM', iv }, derived, encryptedPrivate);
  const privateKey = await subtle.importKey('pkcs8', pkcs8, { name: 'RSA-OAEP', hash: 'SHA-256' }, false, ['decrypt']);
  const raw = await subtle.decrypt({ name: 'RSA-OAEP' }, privateKey, binary(JSON.parse(bundle.encrypted_aes_key)));
  assert.equal(raw.byteLength, 32);
  return { graph_id: bundle.graph_id, key: Buffer.from(raw).toString('base64') };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    let input = '';
    for await (const chunk of process.stdin) input += chunk;
    const { bundle, password } = JSON.parse(input);
    process.stdout.write(JSON.stringify(await unlockGraphKey(bundle, password)));
  } catch {
    console.error('Could not unlock the graph key. Check the encryption password and key format.');
    process.exitCode = 1;
  }
}
