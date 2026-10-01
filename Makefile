# Unix PGXS build. Native Windows uses CMake with PostgreSQL's postgres.lib.
EXTENSION = echoo_pgmq
MODULE_big = echoo_pgmq
OBJS = src/echoo_pgmq.o src/storage.o src/message.o
DATA = $(wildcard sql/echoo_pgmq--*.sql)
PGFILEDESC = "echoo_pgmq - PostgreSQL-native durable AMQP 1.0 queues"
PG_CONFIG ?= pg_config
PKG_CONFIG ?= pkg-config
PG_CPPFLAGS += -I$(srcdir)/include $(shell $(PKG_CONFIG) --cflags libqpid-proton)
SHLIB_LINK += $(shell $(PKG_CONFIG) --libs libqpid-proton)
PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)
