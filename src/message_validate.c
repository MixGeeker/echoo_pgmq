/* Wire-level AMQP message validation. Free of PostgreSQL headers so the same
 * code is linked into the extension and the standalone fuzz target. */
#include "echoo_message_valid.h"
#include <stdint.h>
#include <string.h>
#include <proton/codec.h>
#include <proton/message.h>

/* Proton's codec decodes compound values recursively without a depth limit,
 * so a sub-megabyte message of nested lists exhausts the worker's C stack.
 * Walk the encoding first with recursion bounded by ECHOO_MAX_AMQP_NESTING
 * (described values, lists, maps and arrays each add a level) and reject
 * deeper input before any Proton decoder sees the bytes. Every loop consumes input
 * except zero-width array elements, which are skipped without iteration.
 */
#define ECHOO_MAX_AMQP_NESTING 64

static bool
amqp_read_width(const unsigned char *bytes, size_t size, size_t *offset, int width, size_t *value)
{
    size_t result = 0;
    int i;
    if (size - *offset < (size_t) width)
        return false;
    for (i = 0; i < width; ++i)
        result = (result << 8) | bytes[(*offset)++];
    *value = result;
    return true;
}

static bool amqp_skip_value(const unsigned char *bytes, size_t size, size_t *offset, int depth);

/* Skips the body of a value whose constructor code is already consumed. */
static bool
amqp_skip_body(const unsigned char *bytes, size_t size, size_t *offset, int depth, unsigned char code)
{
    static const signed char fixed[] = {0, 1, 2, 4, 8, 16}; /* 0x4_ .. 0x9_ */
    unsigned char category = code >> 4;
    size_t length;
    size_t count;
    size_t end;
    int width;
    if (depth > ECHOO_MAX_AMQP_NESTING)
        return false;
    if (category >= 0x4 && category <= 0x9)
    {
        if (size - *offset < (size_t) fixed[category - 0x4])
            return false;
        *offset += fixed[category - 0x4];
        return true;
    }
    width = (category == 0xa || category == 0xc || category == 0xe) ? 1 : 4;
    if (category < 0xa || !amqp_read_width(bytes, size, offset, width, &length) ||
        length > size - *offset)
        return false;
    end = *offset + length;
    if (category == 0xa || category == 0xb)
    {
        *offset = end;
        return true;
    }
    if (!amqp_read_width(bytes, end, offset, width, &count))
        return false;
    if (category == 0xc || category == 0xd)
    {
        for (; count > 0; --count)
            if (*offset >= end || !amqp_skip_value(bytes, end, offset, depth + 1))
                return false;
    }
    else
    {
        unsigned char element;
        if (*offset >= end)
            return count == 0;
        element = bytes[(*offset)++];
        if (element == 0x00)
        {
            if (!amqp_skip_value(bytes, end, offset, depth + 1) || *offset >= end)
                return false;
            element = bytes[(*offset)++];
        }
        if ((element >> 4) >= 0x4 && (element >> 4) <= 0x9)
        {
            size_t element_size = (size_t) fixed[(element >> 4) - 0x4];
            if (element_size && count > (end - *offset) / element_size)
                return false;
            *offset += count * element_size;
        }
        else
        {
            for (; count > 0; --count)
                if (!amqp_skip_body(bytes, end, offset, depth + 1, element))
                    return false;
        }
    }
    if (*offset > end)
        return false;
    *offset = end;
    return true;
}

static bool
amqp_skip_value(const unsigned char *bytes, size_t size, size_t *offset, int depth)
{
    unsigned char code;
    if (depth > ECHOO_MAX_AMQP_NESTING || *offset >= size)
        return false;
    code = bytes[(*offset)++];
    if (code == 0x00)
        return amqp_skip_value(bytes, size, offset, depth + 1) &&
               amqp_skip_value(bytes, size, offset, depth + 1);
    return amqp_skip_body(bytes, size, offset, depth, code);
}

static bool
amqp_nesting_bounded(const unsigned char *bytes, size_t size)
{
    size_t offset = 0;
    while (offset < size)
        if (!amqp_skip_value(bytes, size, &offset, 0))
            return false;
    return true;
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
    if (!bytes || !size || !amqp_nesting_bounded(bytes, size))
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
