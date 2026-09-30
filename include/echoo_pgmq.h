#ifndef ECHOO_PGMQ_H
#define ECHOO_PGMQ_H

#include "postgres.h"
#include "utils/uuid.h"

/* SQL bridge. Every call owns a top-level transaction and catches SQL ERROR.
 * Returned message bytes use malloc, never transaction-scoped PostgreSQL memory.
 */
typedef struct EchooMessage
{
    int64 id;
    int64 generation;
    unsigned char *body;
    size_t size;
} EchooMessage;

extern int echoo_statement_timeout_ms;
extern int echoo_max_message_bytes;
bool echoo_db_authorize(const char *queue, const char *identity, bool publish);
bool echoo_db_publish(const char *queue, const char *identity,
                      const unsigned char *body, size_t size);
/* 1 = claimed, 0 = empty, -1 = database error. */
int echoo_db_claim(const char *queue, const char *identity,
                   const pg_uuid_t *owner, int visibility_seconds,
                   EchooMessage *message);
bool echoo_db_settle(const char *queue, const char *identity,
                     const pg_uuid_t *owner, int64 id, int64 generation,
                     const char *outcome);

#endif
