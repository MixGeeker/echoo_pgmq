# Transactional storage and SQL API

Target: PostgreSQL 16 and 17. All payloads, receipts, ACLs, idempotency records,
and counters reside in ordinary **logged PostgreSQL tables**. There is no segment
file, custom WAL, or second durability domain. `bytea` stores the complete AMQP
1.0 encoded message bytes without conversion to JSON or text; PostgreSQL TOAST
may compress the physical representation transparently. Transfer/session frames
are transport state and are not included in an AMQP message body.

## Durability and delivery contract

- An enqueue is part of the caller's PostgreSQL transaction. Committing a business
  update and `enqueue` together makes them atomic; rolling back removes both.
- The network worker must commit `publish` successfully with
  `synchronous_commit=on` before sending an AMQP `Accepted` disposition. A lost
  connection before an acknowledgement makes the publish outcome uncertain;
  publishers may retry and create duplicates.
- A claim commits an incremented generation, random connection owner UUID, and
  expiry before the worker sends the message. The full receipt is
  `(queue, id, generation, owner)`. Message IDs and receipt generations never
  substitute for one another.
- ACK, release, and reject only affect the current, unexpired, inflight receipt.
  A wrong owner, stale generation, duplicate ACK, or expired ACK returns `false`.
  Even an expired receipt that has not yet been reclaimed cannot acknowledge.
- Delivery is **at least once**. Visibility expiry and a crash can redeliver a
  message; consumers must make business side effects idempotent. PostgreSQL
  transactions alone do not make external side effects exactly once.
- PostgreSQL's normal durability prerequisites still apply: `fsync=on`,
  `full_page_writes=on`, reliable storage that honors flushes, and backups.
  `synchronous_commit=on` means the primary's configured commit durability, not
  automatic cross-region replication. Network-worker crash tests and database
  process restart tests do not prove physical power-loss behavior on store disks.
- A SQL caller may choose unsafe database commit settings independently. The
  SQL function does not silently override that transaction's durability policy.

## Installation and trust boundary

Install the extension as a controlled administrative role. Its objects are owned
by the installer. The schema is not relocatable. `PUBLIC` has schema usage and
only the explicitly listed public API functions; it has no table, sequence,
worker-helper, or administration privileges.

Every `SECURITY DEFINER` function fixes its search path to
`pg_catalog, echoo_pgmq, pg_temp` and explicitly qualifies storage objects. Ordinary
SQL wrappers use `session_user`, not a caller-supplied principal or `current_user`
inside a definer function. `SET ROLE` cannot impersonate a different queue
principal. Shared-login database pools therefore share one queue principal;
use separate authenticated logins for separate trust boundaries.

For example, as the extension owner:

```sql
CREATE ROLE store_042 LOGIN;
CREATE ROLE echoo_pgmq_worker NOLOGIN;
CREATE EXTENSION echoo_pgmq;
SELECT echoo_pgmq.create_queue('store/042/orders');
SELECT echoo_pgmq.grant_queue('store/042/orders', 'store_042', true, true);
GRANT EXECUTE ON FUNCTION
    echoo_pgmq.publish(text,bytea,text),
    echoo_pgmq.claim(text,text,uuid,integer),
    echoo_pgmq.settle(text,bigint,bigint,uuid,text,text),
    echoo_pgmq.authorize(text,text,text)
TO echoo_pgmq_worker;
```

The worker role is a **trusted identity gateway**. Its four private functions
accept an explicit principal because certificate authentication takes place in
the native AMQP worker. The worker must verify client certificates against the
configured CA and derive the exact principal from the authenticated certificate;
an AMQP message, address, or property is never an identity source. This role can
act for ACL-listed principals and must not be shared with untrusted SQL clients.
Do not grant membership in it to application users. Role names used in ACLs must
already exist in PostgreSQL; role membership is not implicitly inherited by the
queue ACL. Revocation affects subsequent SQL operations, including settlement.
ACL entries bind both the principal name and PostgreSQL role identity (`regrole`).
Dropping or renaming that role immediately invalidates subsequent authorization;
recreating its old name does not resurrect the prior grant. Administrators must
explicitly re-grant it. PostgreSQL dumps `regrole` as a role name, preserving
proper role resolution when restoring into another cluster; create the expected
roles before restoring extension data.

`create_queue`, `grant_queue`, `revoke_queue`, `purge_dead`, and `retry_dead` are
administrator-only by default. No public function creates a queue implicitly.
The installer can delegate exact administrative functions explicitly if desired.

## Public SQL interface

| Function | Result and behavior |
| --- | --- |
| `message_binary(body bytea)` | Encodes application bytes as a durable AMQP 1.0 data-section message with `application/octet-stream` content type |
| `enqueue_binary(queue text, body bytea, idempotency_key text DEFAULT NULL)` | Encodes arbitrary application bytes with `message_binary`, then enqueues atomically |
| `enqueue(queue text, body bytea, idempotency_key text DEFAULT NULL)` | `bigint` message ID; requires produce ACL |
| `read(queue text, owner uuid, visibility_seconds int DEFAULT 30)` | Zero or one row `(id bigint, generation bigint, body bytea)`; requires consume ACL |
| `ack(queue text, id bigint, generation bigint, owner uuid)` | `boolean`; current receipt deletes body and decrements capacity |
| `release(queue text, id bigint, generation bigint, owner uuid)` | `boolean`; current receipt immediately retries unless attempt limit is reached |
| `reject(queue text, id bigint, generation bigint, owner uuid)` | `boolean`; current receipt becomes a retained dead letter |

Visibility must be 1–86,400 seconds. Generate a new owner UUID for each independent
consumer session. Store every returned generation with its delivery; do not look
up the newest generation to settle an older delivery. Empty `bytea` is permitted
by the SQL storage layer, which deliberately does not parse AMQP; a network
client must send a valid AMQP encoded message.

```sql
BEGIN;
UPDATE orders SET state = 'confirmed' WHERE id = 123;
SELECT echoo_pgmq.enqueue_binary(
    'store/042/orders', $1::bytea, 'order:123:confirmed:v1');
COMMIT;
```

`$1` illustrates driver-bound application bytes, not SQL string interpolation.
Use `enqueue` directly only for an already encoded AMQP message. The binary
helper has a fixed input format ceiling of 16 MiB minus 256 bytes; queue limits
apply to the resulting encoded message, including its header/property overhead. All network
worker SPI calls must use parameters for queue names, identities, payloads,
message IDs, generations, and owner UUIDs.

In 0.1.1, `echoo_pgmq.queue_stats` exposes retained message/byte counters and
configured limits for queues with any ACL granted to `session_user`. A superuser
session without an ACL sees no rows in that view and can instead query the base
tables administratively. Dead letters count toward retained totals.

## Private native-worker interface

These signatures are a stable bridge for the first native worker:

```text
publish(queue text, body bytea, identity text) -> bigint
claim(queue text, identity text, owner uuid, visibility_seconds integer)
    -> TABLE(id bigint, generation bigint, body bytea)
settle(queue text, id bigint, generation bigint, owner uuid,
       outcome text, identity text) -> boolean
authorize(queue text, identity text, operation text) -> boolean
```

`authorize` accepts publish/enqueue/produce or consume/claim/read/settle and returns
false for unknown operations, missing queues, or absent permissions. Each actual
operation rechecks ACLs; attach-time authorization is only an early rejection.
Settlement outcomes are `accepted`, `released`, `rejected`, and `modified`.
`modified` has the same retry policy as `released`; this minimal queue does not
implement AMQP distribution-mode filtering based on modified fields.

`claim` locks one eligible row using `FOR UPDATE SKIP LOCKED` and returns no row
if no eligible unlocked message exists. It also moves at most 64 expired final
attempts to dead-letter state. Separate partial indexes cover deliverable rows,
exhausted leases, and dead messages. Claims never lock the global capacity row.
Capacity-mutating operations lock global counters, then queue counters, then
message rows consistently. This intentionally serializes enqueues and accepted
settlements at the counter row to enforce a hard database-wide bound; it is a
known throughput trade-off, not a performance claim. Keep transactions short and
configure lock/statement timeouts for network-worker operations.

## State machine and poison messages

```text
ready -> inflight                 claim; attempts++, generation++, set lease
inflight -> ready                 current release/modified before attempt limit
inflight -> deleted               current accepted before lease expiry
inflight -> dead                  current rejected, or final-attempt release
expired inflight -> inflight      another claim, if attempts remain
expired final-attempt -> dead     bounded housekeeping on subsequent claims
 dead -> ready                    administrator retry_dead; attempts reset
 dead -> deleted                  administrator purge_dead
```

A dead-letter queue is a logical state within the original queue's message table,
not a separately routed address. Its raw payload and reason remain available to
the administrator. Dead-letter bodies retain their capacity reservation.
`retry_dead(queue, id)` resets attempts and increments the generation;
`purge_dead(queue, limit DEFAULT 100)` removes at most 10,000 dead messages per
call and releases their capacity. Both are transactional. No timer discards a
message automatically. Exhausted expired messages become dead during future
claims; an idle queue may still show them as inflight in raw administrative data.

Each enqueued message captures the queue's `max_attempts` as `attempt_limit`.
Changing the queue setting affects newly enqueued messages; existing messages
retain their captured retry budget, including after administrator retry.

## Bounds and operational configuration

`echoo_pgmq.limits` contains one global counter/configuration row, initialized
transactionally when the first queue is created (the newly installed extension
leaves data tables empty so logical restore can insert their contents):

- Default maximum retained messages: 100,000
- Default maximum retained raw payload bytes: 1,073,741,824
- Default maximum single payload: 1,048,576 bytes

`create_queue(queue, max_messages DEFAULT 10000, max_bytes DEFAULT 67108864,
max_message_bytes DEFAULT 1048576, max_attempts DEFAULT 5)` configures queue
bounds. Global and queue bounds both apply. Only an administrator may change
configuration columns on `limits` or `queues`. Never mutate counter columns or
message/receipt/idempotency tables manually; doing so breaks invariants. Do not
reduce a maximum below the current retained total; enqueues will be refused
until usage drops below the new limit. These limits count payload bytes, not
PostgreSQL row/index overhead, WAL, TOAST overhead, dead tuples, or backups.
Monitor database disk usage separately and budget autovacuum/WAL/disk headroom.
Acknowledgements free logical capacity immediately; PostgreSQL vacuum controls
physical space reuse. Table growth under heavy churn needs real workload tests.

Capacity checks are atomic, use subtraction to avoid addition overflow, and fail
with SQLSTATE `54000` without retaining a partial message or counter update.
Unauthorized operations fail `42501`. Invalid bounds/parameters fail a check
constraint or `22023`; unknown settlement outcomes fail `22023`.

Optional SQL idempotency keys are 1–128 bytes and scoped to a queue. An existing,
unexpired key returns its original message ID even after that message was ACKed;
it does not compare bodies. Reusing a key with different bytes is an application
error and deliberately returns the original ID. Per queue defaults are at most
10,000 keys and a 24-hour window (configurable 1 second–7 days). Keyed enqueues
remove their own expired key and at most 64 other expired records under the queue
lock. A full key ledger rejects new keyed enqueues with `54000`. This ledger is
bounded metadata, not permanent exactly-once deduplication. It is not enabled
implicitly for AMQP message-id values; the native publish API currently has no
idempotency-key parameter.

Queue names are 1–128 ASCII characters matching
`^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$`. Queue creation is administrative and there is
no arbitrary tenant-driven queue creation surface. Schema privileges and queue
ACLs must be backed up together with data.

## Schema versions, backup, and verification

The checked-in `0.1.0` install script is the initial implemented schema.
`0.1.0--0.1.1` is the real additive `queue_stats` migration; fresh `0.1.1` installs
contain the same schema plus that view. `ALTER EXTENSION ... UPDATE` is
transactional. A failed surrounding transaction rolls back both extension
version and added objects, preserving messages, generation, and ownership.
This exercises the actual initial release path, not a fabricated legacy schema.
No downgrade script is supplied.

Tables are registered with `pg_extension_config_dump`, so logical extension
backups include configuration, ACLs, idempotency ledger, and retained messages.
The integration suite checks payloads, ACL-authorized access, deduplication keys,
inflight receipts, counters, and identity-sequence continuation after a logical
dump/restore. Test logical restore with the exact deployed PostgreSQL/backup
versions before relying on it.
PostgreSQL physical backups include all extension data normally. Restore requires
the compatible installed native extension and SQL scripts.

Run `psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -f tests/sql/core.sql` as an
administrative role against an installed extension. It rolls back fixtures and
checks business/enqueue rollback, opaque byte preservation, deduplication,
private-API/ACL isolation, byte/message capacity, current/stale/expired receipts,
maximum-attempt dead letters, search-path hardening, and counter consistency.
Separate integration tests cover concurrent sessions, version migration,
network outcomes, and process restart. Passing a functional test is not a
benchmark or production certification.
