/* Bounded parser regression/fuzz target. No network, database or credentials.
 * This tests the Proton dependency boundary, not complete broker semantics. */
#include <proton/connection_driver.h>
#include <proton/connection.h>
#include <proton/transport.h>
#include <proton/message.h>
#include <proton/event.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    pn_message_t *message;
    pn_connection_driver_t driver;
    size_t offset = 0;
    unsigned iterations = 0;
    if (size > 65536) return 0;
    message = pn_message();
    if (message) {
        /* Proton explicitly requires nonempty input; production checks this too. */
        if (size) (void) pn_message_decode(message, (const char *) data, size);
        pn_message_free(message);
    }
    if (pn_connection_driver_init(&driver, NULL, NULL)) return 0;
    pn_transport_set_server(driver.transport);
    pn_transport_set_max_frame(driver.transport, 65536);
    pn_transport_set_channel_max(driver.transport, 7);
    while (offset < size && iterations++ < 256) {
        pn_rwbytes_t buffer = pn_connection_driver_read_buffer(&driver);
        size_t chunk = size - offset;
        unsigned events = 0;
        if (!buffer.size) break;
        if (chunk > buffer.size) chunk = buffer.size;
        if (chunk > 4096) chunk = 4096;
        memcpy(buffer.start, data + offset, chunk);
        pn_connection_driver_read_done(&driver, chunk);
        offset += chunk;
        while (pn_connection_driver_next_event(&driver) && events++ < 256) {}
        if (pn_connection_driver_finished(&driver)) break;
    }
    pn_connection_driver_destroy(&driver);
    return 0;
}
