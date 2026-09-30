'use strict';

// Independent, ordinary AMQP traffic only. No raw frames, process termination,
// deliberate capacity exhaustion or TLS verification bypasses are used here.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const rhea = require('rhea');

const [operation, address, profile] = process.argv.slice(2);
assert(['publish', 'roundtrip', 'release', 'reconnect'].includes(operation));
assert(['default-first', 'manual-second'].includes(profile));
assert(address);
const url = new URL(process.env.ECHOO_TEST_AMQP_URL);
assert.equal(url.protocol, 'amqps:');
const certs = process.env.ECHOO_TEST_CERT_DIR;
const second = profile === 'manual-second';
const connections = new Set();
const tlsSessions = [];
let failure;

const original = {
    body: rhea.message.data_section(Buffer.from([...Array(256).keys(), 0, 255, 0])),
    message_id: 'rhea-' + address,
    correlation_id: 'correlation-' + address,
    durable: true,
    priority: 7,
    ttl: 60000,
    subject: '门店/二进制',
    to: 'payload-address',
    reply_to: 'reply/queue',
    content_type: 'application/octet-stream',
    content_encoding: 'identity',
    creation_time: 1700000000000,
    absolute_expiry_time: 1800000000000,
    group_id: 'store-17',
    group_sequence: 42,
    reply_to_group_id: 'reply-group',
    application_properties: {store: 'shop-17', sequence: 42, flag: true},
    message_annotations: {'x-echoo-origin': 'independent-rhea'}
};
const wire = rhea.message.encode(original);
const expected = rhea.message.decode(wire);

// Resolve/reject each awaited event explicitly. Connection failures remain
// failures; they are never mistaken for settlement or a successful reconnect.
function event(emitter, name, action, predicate = () => true) {
    return new Promise((resolve, reject) => {
        const timer = setTimeout(() => finish(new Error('timed out waiting for ' + name)), 10000);
        const onEvent = context => {
            if (predicate(context)) finish(null, context);
        };
        const onFailure = error => finish(error);
        function finish(error, result) {
            clearTimeout(timer);
            emitter.removeListener(name, onEvent);
            failures.removeListener('failure', onFailure);
            if (error) reject(error); else resolve(result);
        }
        emitter.on(name, onEvent);
        failures.on('failure', onFailure);
        if (failure) return finish(failure);
        try { if (action) action(); } catch (error) { finish(error); }
    });
}
const {EventEmitter} = require('node:events');
const failures = new EventEmitter();
function fail(error) {
    if (!failure) failure = error instanceof Error ? error : new Error(String(error));
    failures.emit('failure', failure);
}

async function connect(sasl = 'direct') {
    const container = rhea.create_container();
    for (const name of ['connection_error', 'session_error', 'sender_error', 'receiver_error', 'protocol_error', 'error']) {
        container.on(name, context => {
            const error = context.error || context.receiver?.error || context.sender?.error ||
                context.session?.error || context.connection?.error;
            fail(new Error(name + ': ' + (error?.description || error?.message || String(error || name))));
        });
    }
    container.on('disconnected', context => {
        if (!context.connection.expectedClose) fail(context.error || new Error('unexpected disconnect'));
    });
    const options = {
        transport: 'tls', host: url.hostname, port: Number(url.port),
        servername: url.hostname, rejectUnauthorized: true, minVersion: 'TLSv1.2',
        ca: fs.readFileSync(path.join(certs, 'ca.pem')),
        cert: fs.readFileSync(path.join(certs, 'client.pem')),
        key: fs.readFileSync(path.join(certs, 'client.key')),
        reconnect: false
    };
    if (sasl === 'anonymous') {
        options.sasl_mechanisms = rhea.sasl.client_mechanisms();
        options.sasl_mechanisms.enable_anonymous();
    } else if (sasl === 'external') {
        options.enable_sasl_external = true;
    }
    const connection = container.connect(options);
    connections.add(connection);
    await event(connection, 'connection_open');
    assert.equal(connection.socket.authorized, true, 'TLS certificate must be authorized');
    assert.equal(connection.socket.servername, url.hostname, 'TLS server-name validation stays enabled');
    const protocol = connection.socket.getProtocol();
    assert(['TLSv1.2', 'TLSv1.3'].includes(protocol));
    const negotiatedSasl = connection.sasl_transport?.mechanism_name || 'direct';
    assert.equal(negotiatedSasl, sasl === 'direct' ? 'direct' : sasl.toUpperCase());
    tlsSessions.push({protocol, servername: url.hostname, authorized: true, sasl,
        negotiated_sasl: negotiatedSasl});
    return connection;
}

async function close(connection) {
    connection.expectedClose = true;
    await event(connection, 'connection_close', () => connection.close());
    connections.delete(connection);
}

function checkMessage(context) {
    assert.equal(context.delivery.remote_settled, false, 'server must deliver unsettled');
    assert.deepEqual(context.message, expected, 'binary body and every metadata field must survive');
}

async function publish() {
    const connection = await connect();
    // Keep all ordinary rhea sender defaults, including mixed sender settlement.
    const sender = connection.open_sender(address);
    await event(sender, 'sendable');
    assert.equal(sender.remote.attach.snd_settle_mode, 0, 'server negotiates unsettled sending');
    const accepted = await event(sender, 'accepted', () => sender.send(original));
    assert.equal(accepted.delivery.remote_state.constructor.composite_type, 'accepted');
    assert.equal(accepted.delivery.remote_settled, true);
    await close(connection);
    return {published: 1, accepted: 1, wire_base64: wire.toString('base64')};
}

function receiver(connection, manual) {
    if (!manual && !second) return connection.open_receiver(address);
    // One-credit manual control prevents the same consumer from taking another
    // lease while the graceful disconnect scenario opens its replacement.
    return connection.open_receiver({source: address, autoaccept: false,
        credit_window: 0, ...(second ? {rcv_settle_mode: 1} : {})});
}

async function receiveOne(connection, manual) {
    const link = receiver(connection, manual);
    const context = await event(link, 'message', () => {
        if (manual || second) link.add_credit(1);
    });
    assert.equal(link.remote.attach.rcv_settle_mode, second ? 1 : 0);
    checkMessage(context);
    return context;
}

async function accept(context) {
    if (second) {
        const settled = await event(context.receiver, 'settled', () => context.delivery.accept(),
            candidate => candidate.delivery === context.delivery);
        assert.equal(settled.delivery.remote_settled, true);
        assert.equal(settled.delivery.remote_state.constructor.composite_type, 'accepted');
    } else {
        context.delivery.accept();
    }
}

async function consume() {
    let connection = await connect(operation === 'release' ? 'anonymous' : operation === 'reconnect' ? 'external' : 'direct');
    const context = await receiveOne(connection, operation !== 'roundtrip');
    let received = 1;
    if (operation === 'release') {
        if (second) {
            const settled = await event(context.receiver, 'settled', () => context.delivery.release(),
                candidate => candidate.delivery === context.delivery);
            assert.equal(settled.delivery.remote_settled, true);
            assert.equal(settled.delivery.remote_state.constructor.composite_type, 'released');
        } else {
            context.delivery.release();
        }
        const retry = await event(context.receiver, 'message', () => context.receiver.add_credit(1));
        checkMessage(retry);
        assert.notEqual(retry.delivery.id, context.delivery.id);
        received += 1;
        await accept(retry);
    } else if (operation === 'reconnect') {
        // AMQP close handshake, then a fresh connection and link. No socket
        // abort/kill, automatic transport recovery or durable link resumption.
        await close(connection);
        connection = await connect('external');
        const retry = await receiveOne(connection, true);
        checkMessage(retry);
        received += 1;
        await accept(retry);
    } else if (second) {
        await accept(context);
    }
    await close(connection);
    return {received, released: operation === 'release' ? 1 : 0,
        reconnected: operation === 'reconnect', accepted: 1,
        remote_settlement_confirmed: second, metadata_verified: true};
}

(async () => {
    try {
        const result = operation === 'publish' ? await publish() : await consume();
        if (failure) throw failure;
        console.log(JSON.stringify({client: 'rhea', version: require('rhea/package.json').version,
            node: process.version, operation, address, profile, tls_sessions: tlsSessions, ...result}));
    } catch (error) {
        console.error(error.stack || String(error));
        process.exitCode = 1;
    } finally {
        // Normal close is also used on assertion failure; never kill the server
        // or intentionally sever its transport to manufacture a retry outcome.
        for (const connection of connections) {
            connection.expectedClose = true;
            connection.close();
        }
    }
})();
