/* SQL-friendly lossless binary payload envelope, suitable for enqueue inside
 * an application's existing business transaction. */
#include "postgres.h"
#include "fmgr.h"
#include "varatt.h"
#include "utils/builtins.h"
#include "echoo_pgmq.h"
#include <string.h>
#include <proton/codec.h>
#include <proton/message.h>

PG_FUNCTION_INFO_V1(echoo_pgmq_message_binary);

Datum
echoo_pgmq_message_binary(PG_FUNCTION_ARGS)
{
    bytea *input = PG_GETARG_BYTEA_PP(0);
    size_t body_size = VARSIZE_ANY_EXHDR(input);
    size_t encoded_size;
    bytea *result;
    pn_message_t *message;
    int status;

    /* IMMUTABLE: a fixed format limit, independent of per-worker settings.
     * Queue and listener limits still apply to the resulting encoded bytes. */
    if (body_size > 16 * 1024 * 1024 - 256)
        ereport(ERROR, (errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
                        errmsg("binary AMQP body exceeds the 16 MiB format limit")));
    encoded_size = body_size + 256;
    result = palloc(VARHDRSZ + encoded_size);
    message = pn_message();
    if (!message)
        ereport(ERROR, (errcode(ERRCODE_OUT_OF_MEMORY), errmsg("could not allocate AMQP message")));
    status = pn_message_set_durable(message, true);
    if (!status) status = pn_message_set_content_type(message, "application/octet-stream");
    if (!status) status = pn_message_set_inferred(message, true);
    if (!status) status = pn_data_put_binary(pn_message_body(message),
                                            pn_bytes(body_size, VARDATA_ANY(input)));
    if (!status) status = pn_message_encode(message, VARDATA(result), &encoded_size);
    pn_message_free(message);
    if (status)
        ereport(ERROR, (errcode(ERRCODE_DATA_EXCEPTION), errmsg("could not encode AMQP binary message")));
    SET_VARSIZE(result, VARHDRSZ + encoded_size);
    PG_FREE_IF_COPY(input, 0);
    PG_RETURN_BYTEA_P(result);
}
