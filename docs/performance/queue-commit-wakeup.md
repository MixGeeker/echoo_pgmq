# Queue-directed post-commit wake experiment

Status: isolated candidate, not merged or proven faster. Baseline is immutable
`115dcfe67c1dc2ebd2bfc004ed9ce0700947ce40`. Published release assets are unchanged.

## Mechanism and scope

An empty AMQP claim remembers a next-poll deadline. A later successful AMQP
publish in the same worker now invalidates that negative cache only for active
consumer links on that queue. A coalesced worker latch also covers consumers
visited earlier in the connection traversal. No SQL is executed by the helper.
The normal sender pump still applies credit, in-flight, buffer and lease checks,
and still attempts one claim per link per scheduling turn. All matching links
become eligible; no particular consumer receives priority from this helper.
The existing scheduler is not claimed to guarantee strict fairness.

The call occurs only after the storage bridge returns successful durable commit.
A publish rejection does not invalidate consumers. WAL flush, commit-before-
accepted, fenced receipt settlement and at-least-once delivery remain unchanged.
A notification is only a hint; it is neither a delivery nor a receipt.

SQL publishes in other PostgreSQL backends do not notify this process. Their
visibility, and expiry/retry paths, still depend on periodic polling. Polling stays
at the default 50 ms. This does not promise exact 50 ms wall-clock latency because
scheduler, SQL, network and OS delays still apply.

## Alternatives deliberately separate

- Lower polling to 2 ms: increases idle database/CPU activity and changes all
  consumers, rather than invalidating a known-stale queue result
- Always shorten the event-loop wait to its nearest deadline: a broader timer
  scheduling change and does not by itself invalidate the empty-claim cache
- Shared-memory/NOTIFY cross-backend wake: can cover SQL publishers but requires
  a separate commit-aware signaling design and lost-notification analysis
- Flush socket output early: addresses a different phase and is not evidence
  that this candidate helps

This candidate scans active links after AMQP commits, bounded by configured
connection/link limits. That extra work can hurt publisher-heavy workloads;
performance is a question for the paired experiment, not an assumption.

## Bounded experiment

`wakeup-benchmark.yml` builds the immutable baseline and candidate with identical
Proton/PG dependencies. Each platform runs three ordered pairs: AB, BA, AB.
Every arm has a fresh PostgreSQL cluster, durability on, and no OS timer changes.
Windows Server 2022 PG18 is primary; Linux PG18 is supplementary. Neither is a
Windows 11 certification or a store hardware capacity estimate.

The synthetic rhea 3.0.5 / Node 22.17.0 client tests windows 1, 8, 32:
- Empty-to-sparse batches: eight bounded cohorts after an empty interval, with
  deterministic varied spacing to avoid aliasing to the 50 ms timer
- Already-backlogged drain: 256 durably accepted messages before consumer credit
- Credited empty idle: five seconds with no publish

Record each message's actual send, publisher Accepted, receive and consumer
remote-settlement timestamps. Missing/duplicate/incomplete cohorts fail instead
of being counted as completed. Backlog send-to-receive includes intentional
preloading; separate drain throughput starts when credit is granted. Sparse
throughput includes the deliberate idle intervals and is not capacity.

Record module hashes, full platform/settings, worker CPU seconds and one-core
CPU percentage, and PostgreSQL WAL/database/lock boundary snapshots. Snapshot
statistics can lag backend reporting; no per-message SQL instrumentation or
background business workload is mixed into these mechanism measurements.
Shared-runner jitter and baseline spread must be reported before benefit claims.
Every started run and partial failure remains in artifacts; credentials and
cluster databases are excluded. The experiment stops on a failed arm rather than
silently substituting, dropping or retrying it.

## Proof layers

The portable unit test compiles the actual C helper with scheduler stubs and
covers queue selection, closed/closing/failed links, publisher exclusion,
coalescing, multiple consumers, and already-active links. Source checks ensure
the single call remains inside successful publish and normal claim guards stay.
Ordinary integration tests exercise explicit empty-claim barriers and separate
publisher/consumer connections in both connection orders; existing commit,
redelivery and stale-receipt regressions remain in the ordinary CI allowlist.
Timing benchmarks do not replace those delivery-semantics tests. No adversarial,
security, deliberate crash or physical-power-loss tests are added or executed.
