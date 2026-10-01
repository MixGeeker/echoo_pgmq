/*
 * echoo_pgmq: a PostgreSQL-managed AMQP 1.0 endpoint.
 *
 * All PostgreSQL and Proton calls execute on this background worker's main
 * thread. No protocol thread, external broker or sidecar accesses PG memory.
 * This is independently written code; it contains no Kafgres implementation.
 */
#include "postgres.h"
#include "fmgr.h"
#include "libpq/pqsignal.h"
#include "miscadmin.h"
#include "postmaster/bgworker.h"
#include "storage/ipc.h"
#include "storage/latch.h"
#include "storage/proc.h"
#include "utils/guc.h"
#include "utils/memutils.h"
#include "utils/wait_event.h"
#include "portability/instr_time.h"
#include "echoo_pgmq.h"

#include <proton/condition.h>
#include <proton/codec.h>
#include <proton/connection.h>
#include <proton/connection_driver.h>
#include <proton/delivery.h>
#include <proton/disposition.h>
#include <proton/event.h>
#include <proton/link.h>
#include <proton/message.h>
#include <proton/sasl.h>
#include <proton/session.h>
#include <proton/ssl.h>
#include <proton/terminus.h>
#include <proton/transport.h>

#include <stdlib.h>
#include <string.h>
#ifndef WIN32
#include <arpa/inet.h>
#include <netdb.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

PG_MODULE_MAGIC;
PGDLLEXPORT void _PG_init(void);
PGDLLEXPORT void echoo_pgmq_main(Datum main_arg);

static bool enabled = false;
static char *database = NULL;
static char *worker_role = NULL;
static char *listen_address = NULL;
static char *tls_certificate = NULL;
static char *tls_private_key = NULL;
static char *tls_ca_file = NULL;
static int port = 5671;
#ifdef WIN32
/* WaitForMultipleObjects permits 64 handles. Reserve signal/latch/parent/listener. */
#define ECHOO_DEFAULT_CONNECTIONS 32
#define ECHOO_CONNECTION_LIMIT 60
#else
#define ECHOO_DEFAULT_CONNECTIONS 64
#define ECHOO_CONNECTION_LIMIT 1024
#endif
static int max_connections = ECHOO_DEFAULT_CONNECTIONS;
static int max_links = 16;
static int max_inflight = 32;
static int max_buffer_bytes = 64 * 1024 * 1024;
static int visibility_seconds = 60;
static int poll_interval_ms = 50;
static int idle_timeout_ms = 30000;
int echoo_statement_timeout_ms = 5000;
int echoo_max_message_bytes = 1024 * 1024;
#ifdef ECHOO_ENABLE_TEST_HOOKS
bool echoo_test_fail_settle_before_commit = false;
#endif

static volatile sig_atomic_t stop_requested = false;
static volatile sig_atomic_t reload_requested = false;
static size_t inbound_bytes = 0;
static pn_ssl_domain_t *tls_domain = NULL;

#define ECHOO_MAX_SESSIONS 8
#define ECHOO_FRAME_BYTES 65536
#define ECHOO_EVENTS_PER_TURN 256
#define ECHOO_IO_PER_TURN 65536
#define ECHOO_CLOSE_GRACE_MS 5000

typedef struct EchooReceipt
{
    pn_delivery_t *delivery;
    int64 id;
    int64 generation;
    int64 deadline;
    struct EchooReceipt *next;
} EchooReceipt;

typedef struct EchooLink
{
    pn_link_t *link;
    char *queue;
    pg_uuid_t owner;
    unsigned char *incoming;
    size_t incoming_size;
    size_t incoming_capacity;
    int inflight;
    EchooReceipt *receipts;
    int64 next_poll;
    bool closed;
    struct EchooLink *next;
} EchooLink;

typedef struct EchooConnection
{
    pgsocket socket;
    pn_connection_driver_t driver;
    char identity[NAMEDATALEN];
    bool authenticated;
    bool failed;
    int link_count;
    int session_count;
    int64 created_at;
    int64 closing_at;
    EchooLink *links;
    struct EchooConnection *next;
} EchooConnection;

static EchooConnection *connections = NULL;
static int connection_count = 0;

static int64
now_ms(void)
{
    instr_time value;
    INSTR_TIME_SET_CURRENT(value);
    return (int64) INSTR_TIME_GET_MILLISEC(value);
}

static void
signal_stop(SIGNAL_ARGS)
{
    int save_errno = errno;
    (void) postgres_signal_arg;
    stop_requested = true;
    SetLatch(MyLatch);
    errno = save_errno;
}

static void
signal_reload(SIGNAL_ARGS)
{
    int save_errno = errno;
    (void) postgres_signal_arg;
    reload_requested = true;
    SetLatch(MyLatch);
    errno = save_errno;
}

/* A deliberately narrow certificate identity contract. The dedicated CA must
 * issue one CN containing the exact PostgreSQL principal; no SASL username or
 * AMQP Open field can override it. Queue ACLs are rechecked in every SQL call. */
static bool
valid_identity(const char *value)
{
    size_t i, n;
    if (!value || !(n = strlen(value)) || n >= NAMEDATALEN)
        return false;
    for (i = 0; i < n; ++i)
    {
        unsigned char c = (unsigned char) value[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '_' || c == '-'))
            return false;
    }
    return true;
}

static bool
valid_queue(const char *value)
{
    size_t i, n;
    if (!value || !(n = strlen(value)) || n > 128)
        return false;
    for (i = 0; i < n; ++i)
    {
        unsigned char c = (unsigned char) value[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '_' || c == '-' || c == '.' || c == '/'))
            return false;
    }
    return true;
}

static void
connection_error(EchooConnection *connection, const char *name, const char *message)
{
    pn_condition_t *condition = pn_connection_condition(connection->driver.connection);
    if (!connection->closing_at)
    {
        pn_condition_set_name(condition, name);
        pn_condition_set_description(condition, message);
        pn_connection_close(connection->driver.connection);
        connection->closing_at = now_ms();
    }
}

static void
link_error(EchooLink *link, const char *name, const char *message)
{
    pn_condition_set_name(pn_link_condition(link->link), name);
    pn_condition_set_description(pn_link_condition(link->link), message);
    pn_link_close(link->link);
    link->closed = true;
}

static void
free_incoming(EchooLink *link)
{
    inbound_bytes -= link->incoming_capacity;
    free(link->incoming);
    link->incoming = NULL;
    link->incoming_size = 0;
    link->incoming_capacity = 0;
}

static void
free_link(EchooConnection *connection, EchooLink *link)
{
    EchooLink **cursor = &connection->links;
    EchooReceipt *receipt = link->receipts;
    while (*cursor && *cursor != link)
        cursor = &(*cursor)->next;
    if (*cursor)
        *cursor = link->next;
    while (receipt)
    {
        EchooReceipt *next = receipt->next;
        pn_delivery_set_context(receipt->delivery, NULL);
        free(receipt);
        receipt = next;
    }
    free_incoming(link);
    pn_link_set_context(link->link, NULL);
    free(link->queue);
    free(link);
    --connection->link_count;
    /* Outstanding durable claims intentionally expire. Disconnect is not an
     * acknowledgement and must never delete a message. */
}

static void
free_connection(EchooConnection *connection)
{
    while (connection->links)
        free_link(connection, connection->links);
    closesocket(connection->socket);
    pn_connection_driver_destroy(&connection->driver);
    free(connection);
    --connection_count;
}

static size_t
outgoing_bytes(EchooConnection *connection)
{
    pn_session_t *session;
    size_t size = 0;
    for (session = pn_session_head(connection->driver.connection, 0); session;
         session = pn_session_next(session, 0))
        size += pn_session_outgoing_bytes(session);
    return size;
}

static size_t
total_buffered_bytes(void)
{
    EchooConnection *connection;
    size_t size = inbound_bytes;
    for (connection = connections; connection; connection = connection->next)
        size += outgoing_bytes(connection);
    return size;
}

/* Only the AMQP worker calls this, after its enqueue transaction committed.
 * An empty claim is a negative cache entry, not a reason to defer new work.
 * Do not claim here: normal pump_sender preserves credit, memory, in-flight
 * and lease checks, and the existing one-message-per-link scheduling turn.
 * SQL publishes in other backends still use the ordinary polling path.
 */
static void
invalidate_empty_consumers(const char *queue)
{
    EchooConnection *connection;
    bool invalidated = false;
    for (connection = connections; connection; connection = connection->next)
    {
        EchooLink *consumer;
        if (connection->failed || connection->closing_at)
            continue;
        for (consumer = connection->links; consumer; consumer = consumer->next)
            if (!consumer->closed && pn_link_is_sender(consumer->link) &&
                consumer->next_poll != 0 && strcmp(consumer->queue, queue) == 0)
            {
                consumer->next_poll = 0;
                invalidated = true;
            }
    }
    /* A matching connection may already have had its turn this iteration.
     * The latch makes the upcoming wait return, independent of list order.
     * Coalesce notifications; idle queues produce no extra wakeups.
     */
    if (invalidated)
        SetLatch(MyLatch);
}

static void
receive_message(EchooConnection *connection, EchooLink *link, pn_delivery_t *delivery)
{
    size_t pending;
    ssize_t received;
    unsigned char *grown;
    bool valid;

    if (link->closed || !pn_delivery_readable(delivery))
        return;
    if (pn_link_credit(link->link) < 0)
    {
        link_error(link, "amqp:link:transfer-limit-exceeded", "Publisher exceeded granted credit");
        return;
    }
    if (pn_delivery_settled(delivery))
    {
        link_error(link, "amqp:precondition-failed", "Pre-settled publishing is not supported");
        return;
    }
    if (pn_delivery_aborted(delivery))
    {
        free_incoming(link);
        pn_delivery_settle(delivery);
        pn_link_flow(link->link, 1);
        return;
    }
    pending = pn_delivery_pending(delivery);
    if (pending > (size_t) echoo_max_message_bytes - link->incoming_size ||
        pending > (size_t) max_buffer_bytes - Min(total_buffered_bytes(), (size_t) max_buffer_bytes))
    {
        free_incoming(link);
        link_error(link, "amqp:resource-limit-exceeded", "Message or buffer limit exceeded");
        return;
    }
    if (pending)
    {
        grown = realloc(link->incoming, link->incoming_size + pending);
        if (!grown)
        {
            link_error(link, "amqp:resource-limit-exceeded", "Message allocation failed");
            return;
        }
        link->incoming = grown;
        inbound_bytes -= link->incoming_capacity;
        link->incoming_capacity = link->incoming_size + pending;
        inbound_bytes += link->incoming_capacity;
        received = pn_link_recv(link->link, (char *) grown + link->incoming_size, pending);
        if (received < 0)
        {
            link_error(link, "amqp:internal-error", "Message read failed");
            return;
        }
        link->incoming_size += received;
    }
    if (pn_delivery_partial(delivery) || pn_delivery_pending(delivery))
        return;
    valid = echoo_message_valid(link->incoming, link->incoming_size);
    if (!valid)
    {
        pn_condition_t *condition = pn_disposition_condition(pn_delivery_local(delivery));
        pn_condition_set_name(condition, "amqp:decode-error");
        pn_condition_set_description(condition, "Invalid encoded AMQP message");
        pn_delivery_update(delivery, PN_REJECTED);
    }
    else if (echoo_db_publish(link->queue, connection->identity,
                              link->incoming, link->incoming_size))
    {
        /* The SQL bridge has returned only after CommitTransactionCommand. */
        invalidate_empty_consumers(link->queue);
        pn_delivery_update(delivery, PN_ACCEPTED);
    }
    else
    {
        pn_condition_t *condition = pn_disposition_condition(pn_delivery_local(delivery));
        pn_condition_set_name(condition, "amqp:resource-limit-exceeded");
        pn_condition_set_description(condition, "Queue rejected the publish; retry after checking authorization and capacity");
        pn_delivery_update(delivery, PN_REJECTED);
    }
    free_incoming(link);
    pn_delivery_settle(delivery);
    pn_link_flow(link->link, 1);
}

static void
settle_message(EchooConnection *connection, EchooLink *link, pn_delivery_t *delivery)
{
    EchooReceipt *receipt = pn_delivery_get_context(delivery);
    EchooReceipt **cursor;
    const char *outcome = NULL;
    uint64 state;
    if (!receipt || link->closed || !pn_delivery_updated(delivery))
        return;
    state = pn_delivery_remote_state(delivery);
    switch (state)
    {
        case PN_ACCEPTED: outcome = "accepted"; break;
        case PN_REJECTED: outcome = "rejected"; break;
        case PN_RELEASED: outcome = "released"; break;
        case PN_MODIFIED: outcome = "modified"; break;
        case 0:
            if (pn_delivery_settled(delivery))
                link_error(link, "amqp:precondition-failed", "Settlement requires an explicit outcome");
            pn_delivery_clear(delivery);
            return;
        default:
            link_error(link, "amqp:not-implemented", "Transactional and non-terminal outcomes are not supported");
            return;
    }
    if (!echoo_db_settle(link->queue, connection->identity, &link->owner,
                         receipt->id, receipt->generation, outcome))
    {
        link_error(link, "amqp:precondition-failed", "Receipt expired or settlement was not committed");
        return;
    }
    /* In receiver-settle-mode SECOND the peer receives this only after WAL
     * flush; FIRST is also accepted, but lost acknowledgements can redeliver. */
    pn_delivery_update(delivery, state);
    pn_delivery_set_context(delivery, NULL);
    cursor = &link->receipts;
    while (*cursor && *cursor != receipt)
        cursor = &(*cursor)->next;
    if (*cursor)
        *cursor = receipt->next;
    --link->inflight;
    free(receipt);
    pn_delivery_settle(delivery);
}

static void
open_link(EchooConnection *connection, pn_link_t *pnlink)
{
    bool publishing = pn_link_is_receiver(pnlink);
    pn_terminus_t *remote = publishing ? pn_link_remote_target(pnlink) : pn_link_remote_source(pnlink);
    const char *queue = pn_terminus_get_address(remote);
    EchooLink *link;
    if (connection->link_count >= max_links || !valid_queue(queue) ||
        pn_terminus_is_dynamic(remote) || pn_terminus_get_type(remote) == PN_COORDINATOR ||
        pn_terminus_get_durability(remote) != PN_NONDURABLE ||
        pn_data_size(pn_terminus_filter(remote)) != 0 ||
        pn_terminus_get_distribution_mode(remote) == PN_DIST_MODE_COPY ||
        pn_link_remote_snd_settle_mode(pnlink) == PN_SND_SETTLED)
    {
        pn_condition_set_name(pn_link_condition(pnlink), "amqp:invalid-field");
        pn_condition_set_description(pn_link_condition(pnlink), "A named queue and unsettled delivery are required; link limit may also be reached");
        pn_link_close(pnlink);
        /* Do not free before the peer's detach: Proton can still emit remote
         * close events for this endpoint. Freeing here and again on detach
         * double-frees its engine state. Close the connection to bound refused
         * endpoints even when a hostile peer never acknowledges the detach. */
        connection_error(connection, "amqp:invalid-field", "Attach rejected");
        return;
    }
    if (!echoo_db_authorize(queue, connection->identity, publishing))
    {
        pn_condition_set_name(pn_link_condition(pnlink), "amqp:unauthorized-access");
        pn_condition_set_description(pn_link_condition(pnlink), "Queue not available to this certificate principal");
        pn_link_close(pnlink);
        connection_error(connection, "amqp:unauthorized-access", "Queue access denied");
        return;
    }
    link = calloc(1, sizeof(*link));
    if (!link)
    {
        connection_error(connection, "amqp:resource-limit-exceeded", "Link allocation failed");
        return;
    }
    link->queue = strdup(queue);
    if (!link->queue || !pg_strong_random(link->owner.data, UUID_LEN))
    {
        free(link->queue);
        free(link);
        connection_error(connection, "amqp:internal-error", "Receipt identity allocation failed");
        return;
    }
    link->owner.data[6] = (link->owner.data[6] & 0x0f) | 0x40;
    link->owner.data[8] = (link->owner.data[8] & 0x3f) | 0x80;
    link->link = pnlink;
    link->next = connection->links;
    connection->links = link;
    ++connection->link_count;
    pn_link_set_context(pnlink, link);
    pn_terminus_copy(pn_link_source(pnlink), pn_link_remote_source(pnlink));
    pn_terminus_copy(pn_link_target(pnlink), pn_link_remote_target(pnlink));
    pn_link_set_snd_settle_mode(pnlink, PN_SND_UNSETTLED);
    pn_link_set_rcv_settle_mode(pnlink, publishing ? PN_RCV_FIRST : pn_link_remote_rcv_settle_mode(pnlink));
    pn_link_set_max_message_size(pnlink, echoo_max_message_bytes);
    pn_link_open(pnlink);
    if (publishing)
        pn_link_flow(pnlink, 1);
}

static void
process_event(EchooConnection *connection, pn_event_t *event)
{
    pn_link_t *pnlink = pn_event_link(event);
    EchooLink *link = pnlink ? pn_link_get_context(pnlink) : NULL;
    switch (pn_event_type(event))
    {
        case PN_CONNECTION_INIT:
            pn_connection_set_container(connection->driver.connection, "echoo_pgmq");
            break;
        case PN_CONNECTION_REMOTE_OPEN:
        {
            pn_ssl_t *ssl = pn_ssl(connection->driver.transport);
            const char *identity = pn_ssl_get_remote_subject_subfield(ssl, PN_SSL_CERT_SUBJECT_COMMON_NAME);
            if (!pn_transport_is_encrypted(connection->driver.transport) ||
                pn_ssl_get_ssf(ssl) < 128 || !valid_identity(identity))
            {
                connection_error(connection, "amqp:unauthorized-access", "Verified client certificate with a principal CN is required");
                break;
            }
            strlcpy(connection->identity, identity, sizeof(connection->identity));
            connection->authenticated = true;
            pn_connection_open(connection->driver.connection);
            break;
        }
        case PN_SESSION_REMOTE_OPEN:
            if (!connection->authenticated || ++connection->session_count > ECHOO_MAX_SESSIONS)
            {
                connection_error(connection, "amqp:resource-limit-exceeded", "Connection authentication or session limit failed");
                break;
            }
            pn_session_set_incoming_capacity(pn_event_session(event), ECHOO_FRAME_BYTES * 2);
            pn_session_set_outgoing_window(pn_event_session(event), 16);
            pn_session_open(pn_event_session(event));
            break;
        case PN_LINK_REMOTE_OPEN:
            if (connection->authenticated && !connection->closing_at)
                open_link(connection, pnlink);
            break;
        case PN_DELIVERY:
            if (link && connection->authenticated && !connection->closing_at)
            {
                if (pn_link_is_receiver(pnlink))
                    receive_message(connection, link, pn_event_delivery(event));
                else
                    settle_message(connection, link, pn_event_delivery(event));
            }
            break;
        case PN_LINK_REMOTE_CLOSE:
        case PN_LINK_REMOTE_DETACH:
            pn_link_close(pnlink);
            if (link) free_link(connection, link);
            pn_link_free(pnlink);
            break;
        case PN_SESSION_REMOTE_CLOSE:
        {
            EchooLink *item = connection->links;
            while (item)
            {
                EchooLink *next = item->next;
                if (pn_link_session(item->link) == pn_event_session(event))
                    free_link(connection, item);
                item = next;
            }
            pn_session_close(pn_event_session(event));
            pn_session_free(pn_event_session(event));
            if (connection->session_count > 0) --connection->session_count;
            break;
        }
        case PN_CONNECTION_REMOTE_CLOSE:
            pn_connection_close(connection->driver.connection);
            connection->closing_at = now_ms();
            break;
        case PN_TRANSPORT_ERROR:
            connection->failed = true;
            break;
        default:
            break;
    }
}

static void
pump_sender(EchooConnection *connection, EchooLink *link, int64 now)
{
    EchooMessage message;
    EchooReceipt *receipt;
    int result;
    ssize_t sent;
    uint64 remote_max;
    char tag[48];
    int tag_size;
    if (link->closed || !pn_link_is_sender(link->link))
        return;
    for (receipt = link->receipts; receipt; receipt = receipt->next)
        if (now >= receipt->deadline)
        {
            link_error(link, "amqp:resource-limit-exceeded", "Delivery lease expired; reconnect to receive again");
            return;
        }
    if (now < link->next_poll || pn_link_credit(link->link) <= 0 ||
        link->inflight >= max_inflight ||
        outgoing_bytes(connection) >= (size_t) echoo_max_message_bytes ||
        total_buffered_bytes() > (size_t) max_buffer_bytes - echoo_max_message_bytes)
        return;
    result = echoo_db_claim(link->queue, connection->identity, &link->owner,
                            visibility_seconds, &message);
    if (result < 0)
    {
        link_error(link, "amqp:internal-error", "Queue claim failed");
        return;
    }
    if (!result)
    {
        link->next_poll = now + poll_interval_ms;
        if (pn_link_get_drain(link->link)) pn_link_drained(link->link);
        return;
    }
    remote_max = pn_link_remote_max_message_size(link->link);
    if (remote_max && message.size > remote_max)
    {
        free(message.body);
        link_error(link, "amqp:link:message-size-exceeded", "Stored message exceeds receiver's maximum");
        return;
    }
    receipt = calloc(1, sizeof(*receipt));
    if (!receipt)
    {
        free(message.body);
        link_error(link, "amqp:resource-limit-exceeded", "Receipt allocation failed");
        return;
    }
    tag_size = snprintf(tag, sizeof(tag), INT64_FORMAT ":" INT64_FORMAT, message.id, message.generation);
    receipt->delivery = pn_delivery(link->link, pn_dtag(tag, tag_size));
    if (!receipt->delivery)
    {
        free(receipt);
        free(message.body);
        link_error(link, "amqp:resource-limit-exceeded", "Delivery allocation failed");
        return;
    }
    receipt->id = message.id;
    receipt->generation = message.generation;
    receipt->deadline = now + (int64) visibility_seconds * 1000;
    receipt->next = link->receipts;
    link->receipts = receipt;
    ++link->inflight;
    pn_delivery_set_context(receipt->delivery, receipt);
    sent = pn_link_send(link->link, (const char *) message.body, message.size);
    free(message.body);
    if (sent < 0 || (size_t) sent != message.size)
    {
        link_error(link, "amqp:internal-error", "Message transfer could not be buffered");
        return;
    }
    pn_link_advance(link->link);
    link->next_poll = 0;
}

static bool
would_block(void)
{
#ifdef WIN32
    /* PostgreSQL's pgwin32 socket wrappers map WSA errors to errno. */
#endif
    return errno == EWOULDBLOCK || errno == EAGAIN || errno == EINTR;
}

static void
service_io(EchooConnection *connection, uint32 events)
{
    int size;
    if (events & WL_SOCKET_READABLE)
    {
        pn_rwbytes_t buffer = pn_connection_driver_read_buffer(&connection->driver);
        if (buffer.size)
        {
            size = recv(connection->socket, buffer.start, (int) Min(buffer.size, (size_t) ECHOO_IO_PER_TURN), 0);
            if (size > 0) pn_connection_driver_read_done(&connection->driver, size);
            else if (size == 0) pn_connection_driver_read_close(&connection->driver);
            else if (!would_block()) connection->failed = true;
        }
    }
    if (events & WL_SOCKET_WRITEABLE)
    {
        pn_bytes_t buffer = pn_connection_driver_write_buffer(&connection->driver);
        if (buffer.size)
        {
            size = send(connection->socket, buffer.start, (int) Min(buffer.size, (size_t) ECHOO_IO_PER_TURN), 0);
            if (size > 0) pn_connection_driver_write_done(&connection->driver, size);
            else if (size < 0 && !would_block()) connection->failed = true;
        }
    }
}

static void
accept_connections(pgsocket listener)
{
    int accepted;
    for (accepted = 0; accepted < 16; ++accepted)
    {
        pgsocket socket_fd = accept(listener, NULL, NULL);
        EchooConnection *connection;
        if (socket_fd == PGINVALID_SOCKET)
            return;
        if (connection_count >= max_connections || !pg_set_noblock(socket_fd))
        {
            closesocket(socket_fd);
            continue;
        }
        connection = calloc(1, sizeof(*connection));
        if (!connection)
        {
            closesocket(socket_fd);
            continue;
        }
        connection->socket = socket_fd;
        connection->created_at = now_ms();
        if (pn_connection_driver_init(&connection->driver, NULL, NULL) != 0)
        {
            closesocket(socket_fd);
            free(connection);
            continue;
        }
        pn_transport_set_server(connection->driver.transport);
        pn_transport_require_encryption(connection->driver.transport, true);
        pn_transport_set_max_frame(connection->driver.transport, ECHOO_FRAME_BYTES);
        pn_transport_set_channel_max(connection->driver.transport, ECHOO_MAX_SESSIONS - 1);
        pn_transport_set_idle_timeout(connection->driver.transport, idle_timeout_ms);
        /* TLS authenticates identity. SASL ANONYMOUS is merely a supported
         * protocol negotiation, not permission to bypass the required cert. */
        pn_sasl_allowed_mechs(pn_sasl(connection->driver.transport), "ANONYMOUS EXTERNAL");
        pn_sasl_set_allow_insecure_mechs(pn_sasl(connection->driver.transport), false);
        if (pn_ssl_init(pn_ssl(connection->driver.transport), tls_domain, NULL) != 0)
        {
            closesocket(socket_fd);
            pn_connection_driver_destroy(&connection->driver);
            free(connection);
            continue;
        }
        connection->next = connections;
        connections = connection;
        ++connection_count;
    }
}

static pgsocket
create_listener(void)
{
    struct addrinfo hints, *addresses = NULL;
    pgsocket listener;
    char service[16];
    int one = 1;
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_flags = AI_NUMERICHOST | AI_NUMERICSERV;
    snprintf(service, sizeof(service), "%d", port);
    if (getaddrinfo(listen_address, service, &hints, &addresses) != 0 || !addresses)
        ereport(FATAL, (errmsg("echoo_pgmq.listen_address must be a numeric IPv4 or IPv6 address")));
    listener = socket(addresses->ai_family, addresses->ai_socktype, addresses->ai_protocol);
    if (listener == PGINVALID_SOCKET)
        ereport(FATAL, (errmsg("echoo_pgmq: cannot create listener socket")));
#ifndef WIN32
    setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, (const char *) &one, sizeof(one));
#else
    /* Prevent a second process from hijacking the same listening port. */
    setsockopt(listener, SOL_SOCKET, SO_EXCLUSIVEADDRUSE, (const char *) &one, sizeof(one));
#endif
    if (addresses->ai_family == AF_INET6)
        setsockopt(listener, IPPROTO_IPV6, IPV6_V6ONLY, (const char *) &one, sizeof(one));
    if (!pg_set_noblock(listener) ||
        bind(listener, addresses->ai_addr, addresses->ai_addrlen) != 0 ||
        listen(listener, max_connections) != 0)
    {
        closesocket(listener);
        freeaddrinfo(addresses);
        ereport(FATAL, (errmsg("echoo_pgmq: cannot bind listener at %s:%d", listen_address, port)));
    }
    freeaddrinfo(addresses);
    return listener;
}

static void
initialize_tls(void)
{
    if (!tls_certificate[0] || !tls_private_key[0] || !tls_ca_file[0])
        ereport(FATAL, (errmsg("echoo_pgmq: enabled listener requires TLS certificate, private key and client CA"),
                        errhint("Configure echoo_pgmq.tls_certificate, tls_private_key and tls_ca_file.")));
    tls_domain = pn_ssl_domain(PN_SSL_MODE_SERVER);
    if (!tls_domain ||
        pn_ssl_domain_set_credentials(tls_domain, tls_certificate, tls_private_key, NULL) != 0 ||
        pn_ssl_domain_set_trusted_ca_db(tls_domain, tls_ca_file) != 0 ||
        pn_ssl_domain_set_peer_authentication(tls_domain, PN_SSL_VERIFY_PEER, tls_ca_file) != 0 ||
        pn_ssl_domain_set_protocols(tls_domain, "TLSv1.2 TLSv1.3") != 0)
        ereport(FATAL, (errmsg("echoo_pgmq: mandatory mutual TLS configuration failed"),
                        errhint("Use a Proton OpenSSL build, PEM files, and a dedicated client CA. No plaintext fallback exists.")));
}

void
echoo_pgmq_main(Datum main_arg)
{
    pgsocket listener;
    WaitEvent *events;
    (void) main_arg;
    pqsignal(SIGTERM, signal_stop);
    pqsignal(SIGHUP, signal_reload);
#ifndef WIN32
    pqsignal(SIGPIPE, SIG_IGN);
#endif
    BackgroundWorkerUnblockSignals();
#if PG_VERSION_NUM >= 170000
    BackgroundWorkerInitializeConnection(database, worker_role, BGWORKER_BYPASS_ROLELOGINCHECK);
#else
    /* PostgreSQL 16 requires a LOGIN role even for managed workers. Configure
     * no password and reject this role explicitly in pg_hba.conf. */
    BackgroundWorkerInitializeConnection(database, worker_role, 0);
#endif
    initialize_tls();
#ifdef WIN32
    /* PG's Winsock wrappers otherwise emulate blocking even on an OS
     * nonblocking socket. This worker never uses blocking network calls. */
    pgwin32_noblock = 1;
#endif
    listener = create_listener();
    events = palloc0(sizeof(*events) * (max_connections + 2));
    ereport(LOG, (errmsg("echoo_pgmq: AMQP 1.0 mutual-TLS listener started on %s:%d", listen_address, port)));
    while (!stop_requested)
    {
        EchooConnection **cursor;
        EchooConnection *connection;
        WaitEventSet *waitset;
        int64 now = now_ms();
        long timeout = poll_interval_ms;
        int count, i;
        ResetLatch(MyLatch);
        CHECK_FOR_INTERRUPTS();
        if (reload_requested)
        {
            reload_requested = false;
            ProcessConfigFile(PGC_SIGHUP);
        }
        cursor = &connections;
        while ((connection = *cursor) != NULL)
        {
            pn_event_t *event;
            EchooLink *link;
            int handled = 0;
            while (handled++ < ECHOO_EVENTS_PER_TURN &&
                   (event = pn_connection_driver_next_event(&connection->driver)) != NULL)
                process_event(connection, event);
            if (pn_connection_driver_has_event(&connection->driver)) timeout = 0;
            pn_transport_tick(connection->driver.transport, now);
            if (!connection->authenticated && now - connection->created_at >= idle_timeout_ms)
                connection->failed = true;
            if (connection->closing_at && now - connection->closing_at >= ECHOO_CLOSE_GRACE_MS)
                connection->failed = true;
            if (connection->failed || pn_connection_driver_finished(&connection->driver))
            {
                *cursor = connection->next;
                free_connection(connection);
                continue;
            }
            if (!connection->closing_at)
                for (link = connection->links; link; link = link->next)
                {
                    pump_sender(connection, link, now);
                    if (!link->closed && pn_link_is_sender(link->link) &&
                        link->next_poll == 0 && pn_link_credit(link->link) > 0 &&
                        link->inflight < max_inflight &&
                        outgoing_bytes(connection) < (size_t) echoo_max_message_bytes &&
                        total_buffered_bytes() <= (size_t) max_buffer_bytes - echoo_max_message_bytes)
                        timeout = 0;
                }
            cursor = &connection->next;
        }
#if PG_VERSION_NUM >= 170000
        waitset = CreateWaitEventSet(NULL, max_connections + 3);
#else
        waitset = CreateWaitEventSet(CurrentMemoryContext, max_connections + 3);
#endif
        AddWaitEventToSet(waitset, WL_LATCH_SET, PGINVALID_SOCKET, MyLatch, NULL);
        AddWaitEventToSet(waitset, WL_EXIT_ON_PM_DEATH, PGINVALID_SOCKET, NULL, NULL);
        AddWaitEventToSet(waitset, WL_SOCKET_ACCEPT, listener, NULL, NULL);
        for (connection = connections; connection; connection = connection->next)
        {
            uint32 flags = 0;
            if (!pn_connection_driver_read_closed(&connection->driver) &&
                !pn_connection_driver_has_event(&connection->driver) &&
                pn_connection_driver_read_buffer(&connection->driver).size)
                flags |= WL_SOCKET_READABLE;
            if (pn_connection_driver_write_buffer(&connection->driver).size)
                flags |= WL_SOCKET_WRITEABLE;
            if (flags)
                AddWaitEventToSet(waitset, flags, connection->socket, NULL, connection);
        }
        count = WaitEventSetWait(waitset, timeout, events, max_connections + 2, PG_WAIT_EXTENSION);
        FreeWaitEventSet(waitset);
        for (i = 0; i < count; ++i)
        {
            if (events[i].events & WL_LATCH_SET) continue;
            if (events[i].user_data)
                service_io((EchooConnection *) events[i].user_data, events[i].events);
            else if (events[i].events & WL_SOCKET_ACCEPT)
                accept_connections(listener);
        }
    }
    closesocket(listener);
    while (connections)
    {
        EchooConnection *next = connections->next;
        free_connection(connections);
        connections = next;
    }
    pn_ssl_domain_free(tls_domain);
    proc_exit(0);
}

void
_PG_init(void)
{
    BackgroundWorker worker;
    /* SQL envelope helpers also load this module in ordinary backends.
     * PGC_POSTMASTER settings may only be registered during shared preload. */
    if (!process_shared_preload_libraries_in_progress)
        return;
    DefineCustomBoolVariable("echoo_pgmq.enabled", "Enable the native AMQP listener.", NULL,
                             &enabled, false, PGC_POSTMASTER, 0, NULL, NULL, NULL);
#define STRING_GUC(name, description, pointer, default_value) \
    DefineCustomStringVariable("echoo_pgmq." name, description, NULL, pointer, default_value, PGC_POSTMASTER, 0, NULL, NULL, NULL)
#define INT_GUC(name, description, pointer, default_value, minimum, maximum) \
    DefineCustomIntVariable("echoo_pgmq." name, description, NULL, pointer, default_value, minimum, maximum, PGC_POSTMASTER, 0, NULL, NULL, NULL)
    STRING_GUC("database", "Database containing the echoo_pgmq extension.", &database, "postgres");
    STRING_GUC("role", "Restricted database role executing the worker API.", &worker_role, "echoo_pgmq_worker");
    STRING_GUC("listen_address", "Numeric listener address; defaults to loopback.", &listen_address, "127.0.0.1");
    STRING_GUC("tls_certificate", "Server certificate PEM path (Proton OpenSSL backend).", &tls_certificate, "");
    STRING_GUC("tls_private_key", "Unencrypted server private-key PEM path; protect with OS permissions.", &tls_private_key, "");
    STRING_GUC("tls_ca_file", "Dedicated CA PEM file for mandatory client-certificate verification.", &tls_ca_file, "");
    INT_GUC("port", "AMQP TLS listener port.", &port, 5671, 1, 65535);
    INT_GUC("max_connections", "Maximum simultaneous network connections.", &max_connections, ECHOO_DEFAULT_CONNECTIONS, 1, ECHOO_CONNECTION_LIMIT);
    INT_GUC("max_links_per_connection", "Maximum active links per connection.", &max_links, 16, 1, 128);
    INT_GUC("max_message_bytes", "Maximum encoded AMQP message size.", &echoo_max_message_bytes, 1048576, 1024, 16777216);
    INT_GUC("max_inflight_per_link", "Maximum unsettled deliveries per consumer link.", &max_inflight, 32, 1, 1024);
    INT_GUC("max_buffer_bytes", "Global application and queued-output byte budget.", &max_buffer_bytes, 67108864, 16777216, 1073741824);
    INT_GUC("visibility_seconds", "Lease timeout; unacknowledged messages become available again.", &visibility_seconds, 60, 1, 86400);
    INT_GUC("poll_interval_ms", "Maximum delay before polling credited empty consumers.", &poll_interval_ms, 50, 1, 10000);
    INT_GUC("idle_timeout_ms", "AMQP idle and initial TLS handshake deadline.", &idle_timeout_ms, 30000, 1000, 3600000);
    INT_GUC("statement_timeout_ms", "Maximum time for each queue SQL operation.", &echoo_statement_timeout_ms, 5000, 100, 60000);
#undef STRING_GUC
#undef INT_GUC
#ifdef ECHOO_ENABLE_TEST_HOOKS
    /* Compiled out of normal builds. PGC_SIGHUP settings can only be changed
     * through administrator-controlled server configuration. */
    DefineCustomBoolVariable("echoo_pgmq.test_fail_settle_before_commit",
        "TEST BUILD ONLY: abort settlement immediately before transaction commit.", NULL,
        &echoo_test_fail_settle_before_commit, false, PGC_SIGHUP,
        GUC_NOT_IN_SAMPLE | GUC_SUPERUSER_ONLY, NULL, NULL, NULL);
#endif
    MarkGUCPrefixReserved("echoo_pgmq");
    if (!process_shared_preload_libraries_in_progress || !enabled)
        return;
    memset(&worker, 0, sizeof(worker));
    worker.bgw_flags = BGWORKER_SHMEM_ACCESS | BGWORKER_BACKEND_DATABASE_CONNECTION;
    worker.bgw_start_time = BgWorkerStart_RecoveryFinished;
    worker.bgw_restart_time = 10;
    snprintf(worker.bgw_library_name, BGW_MAXLEN, "echoo_pgmq");
    snprintf(worker.bgw_function_name, BGW_MAXLEN, "echoo_pgmq_main");
    snprintf(worker.bgw_name, BGW_MAXLEN, "echoo_pgmq AMQP listener");
    snprintf(worker.bgw_type, BGW_MAXLEN, "echoo_pgmq AMQP listener");
    RegisterBackgroundWorker(&worker);
}
