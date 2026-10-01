# Verified queue-wake / TCP_NODELAY factorial

Decision: a supported sparse-latency candidate with a measurable CPU tradeoff.
Keep PR #12 in draft for review; no merge or general throughput/capacity claim.

- Draft PR: https://github.com/MixGeeker/echoo_pgmq/pull/12
- Server code: `82adc0d0712c10cb3450e1ad6c594ede18a517e5`
- Identity-enforced harness: `8a1347c6f0826a29ed2aa59d60328b9c3f267d23`
- Verified experiment: https://github.com/MixGeeker/echoo_pgmq/actions/runs/36914844920
- Ordinary regression: https://github.com/MixGeeker/echoo_pgmq/actions/runs/36913163692

## What passed

All six ordinary Windows/Linux PG16/17/18 jobs passed, including installation,
ordinary semantics, and candidate-archive reinstall. Both corrected PG18
factorial jobs passed. Combined and NODELAY-only each passed a 51-test ordinary
regression gate on each platform before timed arms began, without skips/failures.

An independent raw audit verifies 24 fresh arms, 168 cells, and 26,304 message
completions. All expected IDs are unique and complete; send/Accepted/receive/
remote-settled timestamps are ordered; recalculated receive P95 values match;
queues retain zero messages. Four distinct expected build-module hashes match
staged and installed modules for every arm, including the NODELAY-only gate.
Every preceding owned cluster was confirmed stopped before replacement.

The harness and server code differ only in benchmark/docs files. The measured
modules are exact build bytes, with the same verified Proton installation and
build RPATH; ordinary package/reinstall tests separately cover installed packages.

## Conditions and limits

PG18.6, Node22.17.0, rhea3.0.5, loopback mutual TLS, default50ms poll, fsync,
full_page_writes and synchronous_commit ON. Windows Server2022 and Ubuntu shared
CI runners each had four logical CPUs and about16GiB RAM. These are not Windows11
certification, actual ERP workloads, store-equivalent storage, or capacity tests.
No system timer changes, new fault hooks, security/crash/physical-power-loss tests,
or durability weakening. No private user-host data appears in these artifacts.

Arms: baseline115dcfe, wake-only332c6afe, NODELAY-only (baseline plus checked
socket patch), combined. Three fresh-cluster blocks are ordered ABCD/DCBA/BADC.
One AMQP connection carries the producer and consumer. Sparse cells use8 spaced
batches: W1 has8 samples per arm, W8 has64, W32 has256. Short small-N cells are
mechanism evidence, not production tail guarantees. Reported P95 uses sorted
index floor((N−1)×0.95); table entries are medians of three arm-specific P95s.

## Receive P95 (ms)

| Platform | Window | Baseline | Wake only | NODELAY only | Combined |
|---|---:|---:|---:|---:|---:|
| Windows |1|63.93|2.89|64.10|4.94|
| Windows |8|65.97|14.15|65.84|13.13|
| Windows |32|95.71|50.12|98.75|51.73|
| Linux |1|52.20|2.15|52.10|1.87|
| Linux |8|60.79|129.27|59.09|11.24|
| Linux |32|118.03|594.59|87.66|38.43|

Windows combined improvements appear in every pair: W1−91.0% to−95.7%,
W8−78.2% to−81.2%, W32−45.9% to−48.0%.

Linux W1/W8 improvements also appear in every pair. Linux W32 combined arm
P95s are153.31,38.17,38.43ms against baselines118.03,155.37,109.39ms.
The first pair is29.9% worse; it is retained, not filtered. Its first batch starts
slowly (first two receives around28.7 and108.6ms after first send), while its
remaining seven batches top out around37–41ms. Cause is not established.

The factorial supports that server TCP_NODELAY removes the repeatable Linux
wake-only burst penalty in this tested setup. It does not directly prove a
particular packet-level Nagle/delayed-ACK mechanism: no packet/syscall trace was
captured. The socket service function, sender pump, bounded event/IO work quanta,
lease checks and receipt-fencing code were not changed.

## CPU tradeoff

Use CPU seconds for an equal message cohort, not CPU percentage alone: faster
work can raise percent simply by shortening elapsed time. All CPU figures are
worker user+system, including the cell's TLS/link lifecycle; they exclude other
PostgreSQL processes. One-core percent is not whole-machine percent.

- Windows sparseW32 (256 messages): median0.1875→0.2969 CPU seconds; paired
  increases30.8%,58.3%,75.0%. This is a real measured cost to weigh against the
  roughly halved receive tail
- Linux sparseW8 (64 messages):0.07→0.08 CPU seconds; paired+12.5% to+16.7%
- Linux sparseW32 (256 messages):0.23→0.24 CPU seconds; paired−7.7%,+4.3%,+8.7%
- Five-second credited-idle medians: Windows0.015625 CPU seconds for both;
  Linux0.02→0.03 seconds. OS accounting granularity and short duration are too
  coarse to claim zero idle overhead or energy savings

## Already-backlogged drain

Each cell preloads256 durably accepted messages, then grants receive credit.
Median completed drain messages/sec (baseline→combined):

| Platform | W1 | W8 | W32 |
|---|---:|---:|---:|
| Windows |753.3→757.4|1180.0→1179.7|1159.0→1157.8|
| Linux |973.0→934.1|1436.5→1485.9|1517.9→1555.6|

Windows W32 individual changes span−7.4% to+6.9%; Linux W1 includes a−12.1%
arm. There is no universal backlog throughput gain. Sparse throughput includes
intentional quiet intervals and must not be advertised as maximum throughput.

## WAL and diagnostics

Generated WAL is now measured from synchronous insert-LSN boundaries. Sparse
cells differ by at most tiny record/alignment amounts; backlogW1 is390,744 bytes
in every arm. Combined backlogW32 has approximately0.5–1.4% more generated WAL
than its paired baselines. That is not an fsync latency measurement.

pg_stat_wal remains explicitly unusable for per-cell attribution because worker
reporting can lag. Boundary lock counts cannot exclude transient locks. No claim
that storage cost is identical, or that the first-batch outlier is harmless, is
supported. Server scan cost at many connections/links and actual store workloads
remain unmeasured.

## Preserved failures and provenance

The wake-only run36909344362 is preserved as an unsuccessful cross-platform
candidate. Its two installed identities were distinct and each switch logged a
new installation, but pre-install build hashes were not recorded then.

Linux factorial36913155367 is invalid: three arms shared an installed hash due
to CMake installation freshness behavior. It is not used for factor attribution.
Its Windows side had four distinct stable installed hashes, but the corrected
run above is the primary evidence. No failed run or slow arm was deleted.

Corrected artifact checksums:
- Windows artifact11189218058:7623164a0a1e833368f6d47288efdc748a61448b34293ca334413e2f337095b8
- Linux artifact11189317779:fb369ba07155424849a78f27f63feb24da459127b7476b78d8ea136b97ce507a

The detailed audit JSON contains all three raw arm-level metric values, paired
changes, module hashes and completion counts. All data is newly generated
synthetic CI evidence. Published release assets and main remain unchanged.

## Review recommendation

The combined change is promising for small idle-to-active queues when lower
latency justifies more CPU. Reject wake-only for cross-platform use. Do not call
this a throughput, energy, or store-capacity optimization. Before merging, review
the Windows burst CPU increase and the retained Linux first-batch tail; if lower
CPU is a stronger requirement, keep this draft and evaluate bounded wake/batch
coalescing separately. No further performance run or merge is implied by this
report.
