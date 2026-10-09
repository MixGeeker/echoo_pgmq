#ifndef ECHOO_MESSAGE_VALID_H
#define ECHOO_MESSAGE_VALID_H

#include <stdbool.h>
#include <stddef.h>

/* True only for a complete, ordered sequence of AMQP 1.0 message sections
 * whose nesting stays within the validator's fixed depth limit. */
bool echoo_message_valid(const unsigned char *bytes, size_t size);

#endif
