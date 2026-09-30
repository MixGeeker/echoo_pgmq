/* Diagnostic-only Linux instrumentation. Never installed or shipped in a product.
 * Included after the private Echoo structures by instrument_native.py only.
 * Events are buffered without I/O and written only on the normal shutdown path.
 * No body, identity, certificate, queue, SQL text, or credential is retained.
 */
#ifndef ECHOO_DIAGNOSTIC_NATIVE_TRACE_H
#define ECHOO_DIAGNOSTIC_NATIVE_TRACE_H
#ifndef __linux__
#error "Native diagnostic instrumentation is Linux-only"
#endif
#ifdef ECHOO_ENABLE_TEST_HOOKS
#error "Native diagnostic instrumentation requires test hooks OFF"
#endif
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <netinet/tcp.h>
#include <stdio.h>
#include <time.h>
#include <sys/stat.h>

#define ECHOO_DIAG_CAPACITY 1048576U
#define ECHOO_DIAG_ID_BYTES 128U

typedef struct EchooDiagEvent
{
    const char *event;
    uint64_t seq, t_ns, loop, connection, link, delivery, operation;
    int64_t db_id, generation, value1, value2, result;
    int error, tcp_nodelay, observation_error;
    unsigned int data_size;
    unsigned char data[ECHOO_DIAG_ID_BYTES];
} EchooDiagEvent;

static EchooDiagEvent *echoo_diag_events;
static size_t echoo_diag_count;
static uint64_t echoo_diag_seq, echoo_diag_loop, echoo_diag_connection;
static uint64_t echoo_diag_link, echoo_diag_delivery, echoo_diag_operation;
static uint64_t echoo_diag_dropped, echoo_diag_clock_failures;
static uint64_t echoo_diag_identity_failures, echoo_diag_observation_failures;
static unsigned int echoo_diag_visit;
static char echoo_diag_path[PATH_MAX];
static uint64_t echoo_diag_resolution_ns;

static uint64_t
echoo_diag_now(void)
{
    struct timespec value;
    int saved_errno = errno;
    uint64_t result = 0;
    if (clock_gettime(CLOCK_MONOTONIC, &value) != 0)
        ++echoo_diag_clock_failures;
    else
        result = (uint64_t) value.tv_sec * UINT64_C(1000000000) + value.tv_nsec;
    errno = saved_errno;
    return result;
}

static EchooDiagEvent *
echoo_diag_record(const char *event, uint64_t connection, uint64_t link,
                  uint64_t delivery, uint64_t operation,
                  int64_t db_id, int64_t generation,
                  int64_t value1, int64_t value2, int64_t result)
{
    EchooDiagEvent *item;
    int saved_errno = errno;
    if (!echoo_diag_events)
        return NULL;
    ++echoo_diag_seq;
    if (echoo_diag_count >= ECHOO_DIAG_CAPACITY)
    {
        ++echoo_diag_dropped;
        return NULL;
    }
    item = &echoo_diag_events[echoo_diag_count++];
    item->event = event;
    item->seq = echoo_diag_seq;
    item->t_ns = echoo_diag_now();
    item->loop = echoo_diag_loop;
    item->connection = connection;
    item->link = link;
    item->delivery = delivery;
    item->operation = operation;
    item->db_id = db_id;
    item->generation = generation;
    item->value1 = value1;
    item->value2 = value2;
    item->result = result;
    item->error = 0;
    item->tcp_nodelay = -1;
    item->observation_error = 0;
    item->data_size = 0;
    errno = saved_errno;
    return item;
}

static void
echoo_diag_bytes(EchooDiagEvent *item, const char *data, size_t size)
{
    if (!item)
        return;
    if (!data || size > ECHOO_DIAG_ID_BYTES)
    {
        ++echoo_diag_identity_failures;
        item->result = -1;
        return;
    }
    item->data_size = (unsigned int) size;
    memcpy(item->data, data, size);
}

static void
echoo_diag_init(void)
{
    const char *directory = getenv("ECHOO_DIAGNOSTIC_TRACE_DIR");
    int saved_errno = errno;
    struct timespec resolution;
    int length;
    if (!directory || !directory[0])
        return; /* A missing trace is insufficient evidence, never success. */
    length = snprintf(echoo_diag_path, sizeof(echoo_diag_path),
                      "%s/native-trace-%ld.jsonl", directory, (long) getpid());
    if (length < 0 || (size_t) length >= sizeof(echoo_diag_path))
    {
        echoo_diag_path[0] = 0;
        errno = saved_errno;
        return;
    }
    echoo_diag_events = calloc(ECHOO_DIAG_CAPACITY, sizeof(EchooDiagEvent));
    if (echoo_diag_events)
    {
        /* Pre-touch the bounded allocation before the listener starts. Avoid
         * demand-zero page faults first occurring inside timed stage records. */
        volatile unsigned char *memory = (volatile unsigned char *) echoo_diag_events;
        size_t offset, bytes = ECHOO_DIAG_CAPACITY * sizeof(EchooDiagEvent);
        for (offset = 0; offset < bytes; offset += 4096)
            memory[offset] = 0;
        memory[bytes - 1] = 0;
    }
    if (clock_getres(CLOCK_MONOTONIC, &resolution) != 0)
        ++echoo_diag_clock_failures;
    else
        echoo_diag_resolution_ns = (uint64_t) resolution.tv_sec * UINT64_C(1000000000) + resolution.tv_nsec;
    echoo_diag_record("worker_start", 0, 0, 0, 0, 0, 0, 0, 0, 0);
    errno = saved_errno;
}

static int
echoo_diag_nodelay(int socket_fd, int *observation_error)
{
    int saved_errno = errno;
    int value = -1;
    socklen_t size = sizeof(value);
    *observation_error = 0;
    if (getsockopt(socket_fd, IPPROTO_TCP, TCP_NODELAY, &value, &size) != 0 || size != sizeof(value))
    {
        *observation_error = errno ? errno : EINVAL;
        value = -1;
        ++echoo_diag_observation_failures;
    }
    errno = saved_errno;
    return value;
}

static void
echoo_diag_connection_open(EchooConnection *connection)
{
    int saved_errno = errno;
    struct sockaddr_storage local, peer;
    socklen_t local_size = sizeof(local), peer_size = sizeof(peer);
    int local_port = -1, peer_port = -1, observation_error = 0;
    int nodelay = echoo_diag_nodelay(connection->socket, &observation_error);
    EchooDiagEvent *item;
    connection->diag_id = ++echoo_diag_connection;
    if (getsockname(connection->socket, (struct sockaddr *) &local, &local_size) == 0 &&
        getpeername(connection->socket, (struct sockaddr *) &peer, &peer_size) == 0)
    {
        if (local.ss_family == AF_INET && peer.ss_family == AF_INET)
        {
            local_port = ntohs(((struct sockaddr_in *) &local)->sin_port);
            peer_port = ntohs(((struct sockaddr_in *) &peer)->sin_port);
        }
        else if (local.ss_family == AF_INET6 && peer.ss_family == AF_INET6)
        {
            local_port = ntohs(((struct sockaddr_in6 *) &local)->sin6_port);
            peer_port = ntohs(((struct sockaddr_in6 *) &peer)->sin6_port);
        }
    }
    if (local_port < 0 || peer_port < 0)
        ++echoo_diag_observation_failures;
    item = echoo_diag_record("connection_open", connection->diag_id, 0, 0, 0,
                             0, 0, local_port, peer_port, local_port >= 0 && peer_port >= 0);
    if (item)
    {
        item->tcp_nodelay = nodelay;
        item->observation_error = observation_error;
    }
    errno = saved_errno;
}

static void
echoo_diag_link_open(EchooConnection *connection, EchooLink *link)
{
    int saved_errno = errno;
    const char *name = pn_link_name(link->link);
    EchooDiagEvent *item;
    link->diag_id = ++echoo_diag_link;
    item = echoo_diag_record("link_open", connection->diag_id, link->diag_id, 0,
                             0, 0, 0, pn_link_is_sender(link->link), 0, 1);
    echoo_diag_bytes(item, name, name ? strlen(name) : 0);
    errno = saved_errno;
}

static void
echoo_diag_delivery_map(EchooConnection *connection, EchooLink *link,
                        pn_delivery_t *delivery, uint64_t delivery_id,
                        uint64_t operation, int64_t db_id, int64_t generation)
{
    int saved_errno = errno;
    pn_delivery_tag_t tag = pn_delivery_tag(delivery);
    EchooDiagEvent *item = echoo_diag_record("delivery_map", connection->diag_id,
                                             link->diag_id, delivery_id, operation,
                                             db_id, generation, 0, 0, 1);
    echoo_diag_bytes(item, tag.start, tag.size);
    errno = saved_errno;
}

static bool
echoo_diag_publish(EchooConnection *connection, EchooLink *link, pn_delivery_t *delivery)
{
    uint64_t operation = ++echoo_diag_operation;
    bool result;
    link->diag_publish_delivery = ++echoo_diag_delivery;
    link->diag_publish_operation = operation;
    echoo_diag_delivery_map(connection, link, delivery, link->diag_publish_delivery, operation, 0, 0);
    echoo_diag_record("publish_start", connection->diag_id, link->diag_id,
                      link->diag_publish_delivery, operation, 0, 0, 0, 0, 0);
    result = echoo_db_publish(link->queue, connection->identity, link->incoming, link->incoming_size);
    echoo_diag_record("publish_return", connection->diag_id, link->diag_id,
                      link->diag_publish_delivery, operation, 0, 0, 0, 0, result);
    return result;
}

static int
echoo_diag_claim(EchooConnection *connection, EchooLink *link, EchooMessage *message)
{
    int result;
    link->diag_claim_operation = ++echoo_diag_operation;
    echoo_diag_record("claim_start", connection->diag_id, link->diag_id, 0,
                      link->diag_claim_operation, 0, 0, 0, 0, 0);
    result = echoo_db_claim(link->queue, connection->identity, &link->owner,
                            visibility_seconds, message);
    echoo_diag_record("claim_return", connection->diag_id, link->diag_id, 0,
                      link->diag_claim_operation, result == 1 ? message->id : 0,
                      result == 1 ? message->generation : 0, 0, 0, result);
    return result;
}

static bool
echoo_diag_settle(EchooConnection *connection, EchooLink *link,
                  EchooReceipt *receipt, const char *outcome)
{
    uint64_t operation = ++echoo_diag_operation;
    bool result;
    echoo_diag_record("settle_start", connection->diag_id, link->diag_id,
                      receipt->diag_delivery, operation, receipt->id, receipt->generation, 0, 0, 0);
    result = echoo_db_settle(link->queue, connection->identity, &link->owner,
                             receipt->id, receipt->generation, outcome);
    echoo_diag_record("settle_return", connection->diag_id, link->diag_id,
                      receipt->diag_delivery, operation, receipt->id, receipt->generation, 0, 0, result);
    return result;
}

static ssize_t
echoo_diag_send(EchooConnection *connection, const void *buffer, size_t length, int flags)
{
    uint64_t sequence = ++connection->diag_send_sequence;
    int observation_error = 0;
    int nodelay = echoo_diag_nodelay(connection->socket, &observation_error);
    EchooDiagEvent *item = echoo_diag_record("socket_send_start", connection->diag_id,
                                             0, 0, 0, 0, 0, sequence, length, 0);
    ssize_t result;
    int saved_errno;
    if (item)
    {
        item->tcp_nodelay = nodelay;
        item->observation_error = observation_error;
    }
    result = send(connection->socket, buffer, length, flags);
    saved_errno = errno;
    item = echoo_diag_record("socket_send_return", connection->diag_id,
                             0, 0, 0, 0, 0, sequence, length, result);
    if (item)
    {
        item->error = result < 0 ? saved_errno : 0;
        item->tcp_nodelay = nodelay;
        item->observation_error = observation_error;
    }
    errno = saved_errno;
    return result;
}

static void
echoo_diag_loop_start(void)
{
    ++echoo_diag_loop;
    echoo_diag_visit = 0;
    echoo_diag_record("loop_start", 0, 0, 0, 0, 0, 0, connection_count, 0, 0);
}

static void
echoo_diag_flush_normal_shutdown(void)
{
    int saved_errno = errno;
    FILE *file;
    int fd, failed = 0;
    size_t i;
    if (!echoo_diag_events || !echoo_diag_path[0])
        return;
    echoo_diag_record("normal_shutdown", 0, 0, 0, 0, 0, 0, 0, 0, 0);
    fd = open(echoo_diag_path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
    if (fd < 0)
        goto done;
    file = fdopen(fd, "w");
    if (!file)
    {
        close(fd);
        unlink(echoo_diag_path);
        goto done;
    }
    fprintf(file, "{\"record\":\"metadata\",\"schema\":1,\"pid\":%ld,"
            "\"clock\":\"CLOCK_MONOTONIC\",\"unit\":\"ns\",\"clock_resolution_ns\":%" PRIu64 ","
            "\"capacity\":%u,\"record_bytes\":%zu,\"buffer_bytes\":%zu,\"identity_limit_bytes\":%u,"
            "\"buffer_allocation\":\"calloc, pre-touched before listener\","
            "\"message_identity\":\"join client exact message ID via link name and delivery tag\","
            "\"accepted_semantics\":\"queued locally, not wire completion\","
            "\"send_semantics\":\"owned socket syscall; TLS bytes, no per-message wire attribution\","
            "\"clock_scope\":\"server process elapsed only; never subtract client clocks\"}\n",
            (long) getpid(), echoo_diag_resolution_ns, ECHOO_DIAG_CAPACITY,
            sizeof(EchooDiagEvent), ECHOO_DIAG_CAPACITY * sizeof(EchooDiagEvent), ECHOO_DIAG_ID_BYTES);
    for (i = 0; i < echoo_diag_count; ++i)
    {
        EchooDiagEvent *item = &echoo_diag_events[i];
        unsigned int j;
        fprintf(file, "{\"record\":\"event\",\"event\":\"%s\",\"seq\":%" PRIu64
                ",\"t_ns\":%" PRIu64 ",\"loop\":%" PRIu64 ",\"connection\":%" PRIu64
                ",\"link\":%" PRIu64 ",\"delivery\":%" PRIu64 ",\"operation\":%" PRIu64
                ",\"db_id\":%" PRId64 ",\"generation\":%" PRId64
                ",\"value1\":%" PRId64 ",\"value2\":%" PRId64 ",\"result\":%" PRId64
                ",\"errno\":%d,\"tcp_nodelay\":%d,\"observation_errno\":%d,\"data_hex\":\"",
                item->event, item->seq, item->t_ns, item->loop, item->connection,
                item->link, item->delivery, item->operation, item->db_id, item->generation,
                item->value1, item->value2, item->result, item->error,
                item->tcp_nodelay, item->observation_error);
        for (j = 0; j < item->data_size; ++j)
            fprintf(file, "%02x", item->data[j]);
        fputs("\"}\n", file);
    }
    fprintf(file, "{\"record\":\"footer\",\"normal_shutdown\":true,\"events\":%zu,"
            "\"attempted_events\":%" PRIu64 ",\"dropped_events\":%" PRIu64
            ",\"clock_failures\":%" PRIu64 ",\"identity_failures\":%" PRIu64
            ",\"observation_failures\":%" PRIu64 ",\"complete\":%s}\n",
            echoo_diag_count, echoo_diag_seq, echoo_diag_dropped, echoo_diag_clock_failures,
            echoo_diag_identity_failures, echoo_diag_observation_failures,
            !echoo_diag_dropped && !echoo_diag_clock_failures && !echoo_diag_identity_failures &&
            !echoo_diag_observation_failures ? "true" : "false");
    if (ferror(file) || fflush(file) != 0 || fsync(fd) != 0)
        failed = 1;
    if (fclose(file) != 0)
        failed = 1;
    if (failed)
        unlink(echoo_diag_path); /* Missing/incomplete output is insufficient. */
done:
    free(echoo_diag_events);
    echoo_diag_events = NULL;
    errno = saved_errno;
}
#endif
