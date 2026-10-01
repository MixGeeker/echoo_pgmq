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

/* Proton's high-level decoder tolerates/skips some non-message values. A
 * durable broker must require a complete sequence of recognized AMQP message
 * sections instead. Preserve the original bytes; this is validation only.
 */
bool
echoo_message_valid(const unsigned char *bytes, size_t size)
{
    static const char *symbols[] = {
        "amqp:header:list", "amqp:delivery-annotations:map",
        "amqp:message-annotations:map", "amqp:properties:list",
        "amqp:application-properties:map", "amqp:data:binary",
        "amqp:amqp-sequence:list", "amqp:amqp-value:*", "amqp:footer:map"
    };
    static const pn_type_t types[] = {
        PN_LIST, PN_MAP, PN_MAP, PN_LIST, PN_MAP,
        PN_BINARY, PN_LIST, PN_INVALID, PN_MAP
    };
    pn_data_t *data;
    pn_message_t *message;
    size_t offset = 0;
    int previous = -1;
    int body_kind = -1;
    bool valid = false;
    if (!bytes || !size)
        return false;
    data = pn_data(0);
    if (!data)
        return false;
    while (offset < size)
    {
        ssize_t consumed;
        int section = -1;
        pn_data_clear(data);
        consumed = pn_data_decode(data, (const char *) bytes + offset, size - offset);
        if (consumed <= 0 || (size_t) consumed > size - offset)
            goto done;
        pn_data_rewind(data);
        if (!pn_data_next(data) || pn_data_type(data) != PN_DESCRIBED ||
            !pn_data_enter(data) || !pn_data_next(data))
            goto done;
        if (pn_data_type(data) == PN_ULONG)
        {
            uint64_t code = pn_data_get_ulong(data);
            if (code >= 0x70 && code <= 0x78)
                section = (int) (code - 0x70);
        }
        else if (pn_data_type(data) == PN_SYMBOL)
        {
            pn_bytes_t symbol = pn_data_get_symbol(data);
            int i;
            for (i = 0; i < 9; ++i)
                if (strlen(symbols[i]) == symbol.size &&
                    memcmp(symbols[i], symbol.start, symbol.size) == 0)
                    section = i;
        }
        if (section < 0 || !pn_data_next(data) ||
            (types[section] != PN_INVALID && pn_data_type(data) != types[section]))
            goto done;
        /* Sections are ordered. Only data and sequence sections may repeat,
         * and the three body encodings may never be mixed. */
        if (section < previous || (section == previous && section != 5 && section != 6))
            goto done;
        if (section >= 5 && section <= 7)
        {
            if (body_kind != -1 && body_kind != section)
                goto done;
            body_kind = section;
        }
        if (pn_data_next(data) || !pn_data_exit(data) || pn_data_next(data))
            goto done;
        previous = section;
        offset += consumed;
    }
    message = pn_message();
    if (message)
    {
        valid = pn_message_decode(message, (const char *) bytes, size) == 0;
        pn_message_free(message);
    }
done:
    pn_data_free(data);
    return valid;
}
