'use strict';
// npm ci performs tarball integrity verification. This second check records the
// actual installed dependency closure and refuses unreviewed packages/versions.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const lockBytes = fs.readFileSync(path.join(__dirname, 'package-lock.json'));
const lock = JSON.parse(lockBytes);
const expected = {rhea: '3.0.5', debug: '4.4.3', ms: '2.1.3'};
assert.equal(lock.lockfileVersion, 3);
assert.equal(lock.packages[''].dependencies.rhea, expected.rhea);
assert.deepEqual(Object.keys(lock.packages).sort(), ['', ...Object.keys(expected).map(name => 'node_modules/' + name)].sort());
const dependencies = {};
for (const [name, version] of Object.entries(expected)) {
    const pinned = lock.packages['node_modules/' + name];
    assert.equal(pinned.version, version);
    assert.equal(pinned.resolved, `https://registry.npmjs.org/${name}/-/${name}-${version}.tgz`);
    assert.match(pinned.integrity, /^sha512-[A-Za-z0-9+/]+={0,2}$/);
    const actual = JSON.parse(fs.readFileSync(path.join(__dirname, 'node_modules', name, 'package.json')));
    assert.equal(actual.name, name);
    assert.equal(actual.version, version);
    assert.equal(actual.license, pinned.license);
    assert.deepEqual(actual.dependencies || {}, pinned.dependencies || {});
    dependencies[name] = {version, license: actual.license, resolved: pinned.resolved, integrity: pinned.integrity};
}
assert.equal(require.resolve('rhea'), path.join(__dirname, 'node_modules', 'rhea', 'lib', 'container.js'));
console.log(JSON.stringify({implementation: 'independent JavaScript AMQP codec; no Proton client bindings',
    node: process.version, dependencies,
    lock_sha256: crypto.createHash('sha256').update(lockBytes).digest('hex')}));
