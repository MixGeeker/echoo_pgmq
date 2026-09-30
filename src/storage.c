/* Independent PostgreSQL transaction bridge for the AMQP worker. */
#include "postgres.h"
#include "access/xact.h"
#include "access/xlog.h"
#include "catalog/pg_type_d.h"
#include "executor/spi.h"
#include "miscadmin.h"
#include "utils/builtins.h"
#include "utils/memutils.h"
#include "utils/snapmgr.h"
#include "utils/timeout.h"
#include "echoo_pgmq.h"

#include <stdlib.h>
#include <string.h>

static void
begin_operation(void)
{
    SetCurrentStatementStartTimestamp();
    StartTransactionCommand();
    if (!enableFsync || !fullPageWrites)
        elog(ERROR, "echoo_pgmq requires fsync and full_page_writes enabled");
    PushActiveSnapshot(GetTransactionSnapshot());
    if (SPI_connect() != SPI_OK_CONNECT)
        elog(ERROR, "echoo_pgmq: SPI connection failed");
    /* Publisher acceptance and consumer settlement are never sent before
     * the WAL commit record has been synchronously flushed. */
    SPI_execute("SET LOCAL synchronous_commit = on", false, 0);
    SPI_execute("SET LOCAL search_path = pg_catalog", false, 0);
    SPI_execute("SET LOCAL lock_timeout = '1000ms'", false, 0);
    enable_timeout_after(STATEMENT_TIMEOUT, echoo_statement_timeout_ms);
}

static void
commit_operation(void)
{
    disable_timeout(STATEMENT_TIMEOUT, false);
    SPI_finish();
    PopActiveSnapshot();
    CommitTransactionCommand();
}

static void
abort_operation(MemoryContext context)
{
    ErrorData *error;
    MemoryContextSwitchTo(context);
    error = CopyErrorData();
    FlushErrorState();
    disable_timeout(STATEMENT_TIMEOUT, false);
    AbortCurrentTransaction();
    /* Never include message content, certificate details or SQL parameter
     * values in protocol errors. A server-side SQLSTATE is enough to triage. */
    ereport(LOG, (errmsg("echoo_pgmq: database operation failed (SQLSTATE %s)",
                        unpack_sql_state(error->sqlerrcode))));
    FreeErrorData(error);
}

bool
echoo_db_authorize(const char *queue, const char *identity, bool publish)
{
    MemoryContext context = CurrentMemoryContext;
    volatile bool allowed = false;
    PG_TRY();
    {
        Oid types[] = {TEXTOID, TEXTOID, TEXTOID};
        Datum args[3];
        bool isnull;
        begin_operation();
        args[0] = CStringGetTextDatum(queue);
        args[1] = CStringGetTextDatum(identity);
        args[2] = CStringGetTextDatum(publish ? "publish" : "consume");
        if (SPI_execute_with_args("SELECT echoo_pgmq.authorize($1,$2,$3)",
                                  3, types, args, NULL, false, 1) != SPI_OK_SELECT ||
            SPI_processed != 1)
            elog(ERROR, "echoo_pgmq: authorization returned no result");
        allowed = DatumGetBool(SPI_getbinval(SPI_tuptable->vals[0],
                                             SPI_tuptable->tupdesc, 1, &isnull));
        allowed = allowed && !isnull;
        commit_operation();
    }
    PG_CATCH();
    {
        abort_operation(context);
        allowed = false;
    }
    PG_END_TRY();
    return allowed;
}

bool
echoo_db_publish(const char *queue, const char *identity,
                 const unsigned char *body, size_t size)
{
    MemoryContext context = CurrentMemoryContext;
    volatile bool success = false;
    PG_TRY();
    {
        Oid types[] = {TEXTOID, BYTEAOID, TEXTOID};
        Datum args[3];
        bytea *payload;
        begin_operation();
        if (size == 0 || size > (size_t) echoo_max_message_bytes)
            elog(ERROR, "echoo_pgmq: invalid message size");
        payload = palloc(VARHDRSZ + size);
        SET_VARSIZE(payload, VARHDRSZ + size);
        memcpy(VARDATA(payload), body, size);
        args[0] = CStringGetTextDatum(queue);
        args[1] = PointerGetDatum(payload);
        args[2] = CStringGetTextDatum(identity);
        if (SPI_execute_with_args("SELECT echoo_pgmq.publish($1,$2,$3)",
                                  3, types, args, NULL, false, 1) != SPI_OK_SELECT ||
            SPI_processed != 1)
            elog(ERROR, "echoo_pgmq: publish returned no result");
        commit_operation();
        success = true;
    }
    PG_CATCH();
    {
        abort_operation(context);
    }
    PG_END_TRY();
    return success;
}

/* Called only inside the active claim transaction. Poison rows remain durable
 * dead letters, rather than rolling their claim back and starving the queue. */
static void
reject_claimed_message(const char *queue, const char *identity,
                       const pg_uuid_t *owner, int64 id, int64 generation)
{
    Oid types[] = {TEXTOID, INT8OID, INT8OID, UUIDOID, TEXTOID, TEXTOID};
    Datum args[6];
    args[0] = CStringGetTextDatum(queue);
    args[1] = Int64GetDatum(id);
    args[2] = Int64GetDatum(generation);
    args[3] = PointerGetDatum(owner);
    args[4] = CStringGetTextDatum("rejected");
    args[5] = CStringGetTextDatum(identity);
    if (SPI_execute_with_args("SELECT echoo_pgmq.settle($1,$2,$3,$4,$5,$6)",
                              6, types, args, NULL, false, 1) != SPI_OK_SELECT ||
        SPI_processed != 1)
        elog(ERROR, "echoo_pgmq: poison-message settlement failed");
}

int
echoo_db_claim(const char *queue, const char *identity,
               const pg_uuid_t *owner, int visibility_seconds,
               EchooMessage *message)
{
    MemoryContext context = CurrentMemoryContext;
    volatile int result = -1;
    memset(message, 0, sizeof(*message));
    PG_TRY();
    {
        Oid types[] = {TEXTOID, TEXTOID, UUIDOID, INT4OID};
        Datum args[4];
        int inspected;
        begin_operation();
        args[0] = CStringGetTextDatum(queue);
        args[1] = CStringGetTextDatum(identity);
        args[2] = PointerGetDatum(owner);
        args[3] = Int32GetDatum(visibility_seconds);
        result = 0;
        /* Bound poison scanning per transaction to preserve event-loop
         * fairness and statement timeout, even for a large corrupt backlog. */
        for (inspected = 0; inspected < 8; ++inspected)
        {
            bool isnull;
            bytea *payload;
            Datum body_datum;
            HeapTuple tuple;
            TupleDesc desc;
            if (SPI_execute_with_args("SELECT id,generation,body FROM echoo_pgmq.claim($1,$2,$3,$4)",
                                      4, types, args, NULL, false, 1) != SPI_OK_SELECT)
                elog(ERROR, "echoo_pgmq: claim query failed");
            if (SPI_processed == 0)
                break;
            tuple = SPI_tuptable->vals[0];
            desc = SPI_tuptable->tupdesc;
            message->id = DatumGetInt64(SPI_getbinval(tuple, desc, 1, &isnull));
            if (isnull) elog(ERROR, "echoo_pgmq: null message id");
            message->generation = DatumGetInt64(SPI_getbinval(tuple, desc, 2, &isnull));
            if (isnull) elog(ERROR, "echoo_pgmq: null receipt generation");
            body_datum = SPI_getbinval(tuple, desc, 3, &isnull);
            if (isnull) elog(ERROR, "echoo_pgmq: null message body");
            payload = DatumGetByteaPP(body_datum);
            message->size = VARSIZE_ANY_EXHDR(payload);
            if (message->size == 0 || message->size > (size_t) echoo_max_message_bytes ||
                !echoo_message_valid((const unsigned char *) VARDATA_ANY(payload), message->size))
            {
                reject_claimed_message(queue, identity, owner, message->id, message->generation);
                memset(message, 0, sizeof(*message));
                continue;
            }
            message->body = malloc(message->size);
            if (!message->body) elog(ERROR, "echoo_pgmq: message allocation failed");
            memcpy(message->body, VARDATA_ANY(payload), message->size);
            result = 1;
            break;
        }
        commit_operation();
    }
    PG_CATCH();
    {
        abort_operation(context);
        free(message->body);
        memset(message, 0, sizeof(*message));
        result = -1;
    }
    PG_END_TRY();
    return result;
}

bool
echoo_db_settle(const char *queue, const char *identity,
                const pg_uuid_t *owner, int64 id, int64 generation,
                const char *outcome)
{
    MemoryContext context = CurrentMemoryContext;
    volatile bool success = false;
    PG_TRY();
    {
        Oid types[] = {TEXTOID, INT8OID, INT8OID, UUIDOID, TEXTOID, TEXTOID};
        Datum args[6];
        bool isnull;
        bool applied;
        begin_operation();
        args[0] = CStringGetTextDatum(queue);
        args[1] = Int64GetDatum(id);
        args[2] = Int64GetDatum(generation);
        args[3] = PointerGetDatum(owner);
        args[4] = CStringGetTextDatum(outcome);
        args[5] = CStringGetTextDatum(identity);
        if (SPI_execute_with_args("SELECT echoo_pgmq.settle($1,$2,$3,$4,$5,$6)",
                                  6, types, args, NULL, false, 1) != SPI_OK_SELECT ||
            SPI_processed != 1)
            elog(ERROR, "echoo_pgmq: settlement returned no result");
        applied = DatumGetBool(SPI_getbinval(SPI_tuptable->vals[0],
                                            SPI_tuptable->tupdesc, 1, &isnull));
        applied = applied && !isnull;
#ifdef ECHOO_ENABLE_TEST_HOOKS
        if (echoo_test_fail_settle_before_commit)
            elog(ERROR, "echoo_pgmq: test-injected failure before settlement commit");
#endif
        commit_operation();
        success = applied;
    }
    PG_CATCH();
    {
        abort_operation(context);
        success = false;
    }
    PG_END_TRY();
    return success;
}
