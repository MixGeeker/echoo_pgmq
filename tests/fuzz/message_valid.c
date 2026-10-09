/* Fuzz target for the production message validator: the bounded nesting walk
 * followed by Proton decoding. No network, database or credentials. */
#include "echoo_message_valid.h"
#include <stddef.h>
#include <stdint.h>

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    (void) echoo_message_valid(data, size);
    return 0;
}
