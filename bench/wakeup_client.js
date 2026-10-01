'use strict';
// Synthetic same-worker AMQP traffic. No TLS shortcuts, timer changes or fault hooks.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const rhea = require('../tests/interop/node_modules/rhea');
const [queue, kind, rawWindow, output] = process.argv.slice(2);
const window = Number(rawWindow);
assert(['sparse', 'backlog', 'idle'].includes(kind));
assert([1, 8, 32].includes(window));
const url = new URL(process.env.ECHOO_TEST_AMQP_URL);
const certs = process.env.ECHOO_TEST_CERT_DIR;
const now = () => Number(process.hrtime.bigint()) / 1e6;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const container = rhea.create_container();
let failure, closing = false;
const samples = new Map(), completed = new Set();
let sender, receiver, connection, lastSendable;
const record = {schema: 1, queue, kind, window, client: 'rhea', client_version: require('../tests/interop/node_modules/rhea/package.json').version,
    samples: [], status: 'running', tls_authorized: false};
function save() { record.samples = [...samples.values()]; fs.writeFileSync(output, JSON.stringify(record, null, 2)); }
function fail(error) { if (!failure) failure = new Error(String(error?.description || error?.message || error)); }
for (const event of ['connection_error','session_error','sender_error','receiver_error','protocol_error','error']) {
    container.on(event, context => fail(context.error || context.receiver?.error || context.sender?.error || event));
}
container.on('disconnected', context => { if (!closing) fail(context.error || 'unexpected disconnect'); });
async function until(predicate, label, timeout = 10000) {
    const end = now() + timeout;
    while (!predicate()) { if (failure) throw failure; if (now() > end) throw new Error('timeout: ' + label); await sleep(1); }
}
container.on('sendable', () => { lastSendable = now(); });
container.on('accepted', context => {
    const sample = samples.get(context.delivery.tag.toString());
    if (!sample) return fail('unknown accepted delivery');
    sample.accepted_ms = now();
});
container.on('rejected', () => fail('publish rejected'));
container.on('released', () => fail('publish released'));
container.on('message', context => {
    const id = String(context.message.message_id), sample = samples.get(id);
    if (!sample || sample.receive_ms !== undefined) return fail('missing or duplicate message');
    assert.equal(context.message.body.content.toString(), 'synthetic-' + id);
    sample.receive_ms = now();
    context.delivery.accept();
    context.delivery.context = id;
});
container.on('settled', context => {
    if (!context.receiver) return;
    const id = context.delivery.context, sample = samples.get(id);
    if (!sample) return fail('unknown consumer settlement');
    sample.settled_ms = now();
    completed.add(id);
    if (kind === 'backlog') receiver.add_credit(1);
});
function send(id) {
    const start = now();
    samples.set(id, {id, send_ms: start});
    sender.send({message_id:id, body:rhea.message.data_section(Buffer.from('synthetic-' + id)), durable:true}, Buffer.from(id));
}
async function batch(count, prefix) {
    let sent = 0;
    while (sent < count) {
        await until(() => sender.sendable(), 'sender credit');
        send(prefix + sent++);
    }
}
(async () => {
    connection = container.connect({transport:'tls', host:url.hostname, port:Number(url.port), servername:url.hostname,
        rejectUnauthorized:true, minVersion:'TLSv1.2', reconnect:false,
        ca:fs.readFileSync(path.join(certs,'ca.pem')),cert:fs.readFileSync(path.join(certs,'client.pem')),key:fs.readFileSync(path.join(certs,'client.key'))});
    await until(() => connection.is_open(), 'connection');
    assert.equal(connection.socket.authorized,true); record.tls_authorized=true;
    sender=connection.open_sender(queue);
    receiver=connection.open_receiver({source:{address:queue},credit_window:0,autoaccept:false,autosettle:false,rcv_settle_mode:1});
    await until(() => sender.sendable() && receiver.is_open(), 'links');
    record.start_ms=now();
    if (kind==='idle') { receiver.add_credit(window); await sleep(5000); }
    else if (kind==='backlog') {
        await batch(256,'backlog-');
        await until(() => [...samples.values()].every(x => x.accepted_ms!==undefined), 'durable preload');
        record.consume_start_ms=now(); receiver.add_credit(window);
        await until(() => completed.size===256,'backlog settlement',30000);
    } else {
        for (let round=0;round<8;round++) {
            // Give credit while empty, then allow at least one ordinary poll.
            // This timing is measurement setup, not the regression's proof barrier.
            receiver.add_credit(window); await sleep(125 + (round * 17) % 40);
            await batch(window,'sparse-'+round+'-');
            const expected=(round+1)*window;
            await until(() => completed.size===expected && [...samples.values()].every(x => x.accepted_ms!==undefined),'sparse cohort');
        }
    }
    record.end_ms=now(); record.complete_count=completed.size;
    record.status='passed'; save();
    closing=true; connection.close();
    await until(() => connection.is_closed(),'close');
})().catch(error => { record.status='failed'; record.error=String(error.stack || error); save(); console.error(error); process.exitCode=1; closing=true; if(connection)connection.close(); }).finally(() => { setTimeout(() => process.exit(process.exitCode || 0),1000).unref(); });
