# Benchmark infrastructure only. End users run the native PostgreSQL extension.
# Official multi-platform image digest resolved 2026-09-30. compare_ci.py also
# records the built image ID; all test cells use that ONE immutable image ID.
FROM rabbitmq:4.0.5@sha256:82ee1d53d63c646b2fbaacfe4c5fb0e01a447047df585d16836ae236637bb15a

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake pkg-config libssl-dev zlib1g-dev bison flex \
    python3 ca-certificates curl xz-utils && rm -rf /var/lib/apt/lists/*

# Official PostgreSQL source, checksum also verified against upstream and the
# archive used for the original native PG18 build. Never disable WAL/fsync.
RUN curl --fail --location --retry 3 \
      https://ftp.postgresql.org/pub/source/v18.6/postgresql-18.6.tar.bz2 -o /tmp/postgres.tar.bz2 \
    && echo '555610c24d53e4316da5b7d3fc25c279d96856d5e0e23ee308c328c5fa881d9f  /tmp/postgres.tar.bz2' | sha256sum -c - \
    && mkdir /tmp/pg-src && tar -xjf /tmp/postgres.tar.bz2 -C /tmp/pg-src --strip-components=1 \
    && cd /tmp/pg-src && ./configure --prefix=/opt/pg18 --without-readline --without-icu --with-openssl \
    && make -j2 && make install && rm -rf /tmp/pg-src /tmp/postgres.tar.bz2

COPY scripts/fetch_dependency.py /opt/build/fetch_dependency.py
RUN python3 /opt/build/fetch_dependency.py proton /tmp/proton-src \
    && cmake -S /tmp/proton-src -B /tmp/proton-build \
      -DCMAKE_BUILD_TYPE=RelWithDebInfo -DCMAKE_INSTALL_PREFIX=/opt/proton \
      -DSSL_IMPL=openssl -DSASL_IMPL=none -DBUILD_CPP=OFF -DBUILD_PYTHON=OFF \
      -DBUILD_RUBY=OFF -DBUILD_GO=OFF -DBUILD_TESTING=OFF -DBUILD_EXAMPLES=OFF -DBUILD_TOOLS=OFF \
    && cmake --build /tmp/proton-build --parallel 2 \
    && cmake --install /tmp/proton-build && rm -rf /tmp/proton-src /tmp/proton-build

# Both arms are compiled in this one image with identical flags/instrumentation.
COPY bench/diagnostic/generated/A /opt/arms-source/A
COPY bench/diagnostic/generated/B /opt/arms-source/B
COPY bench/diagnostic/generated/manifest.json /opt/source-manifest.json
RUN for arm in A B; do \
      cmake -S /opt/arms-source/$arm -B /tmp/build-$arm -DCMAKE_BUILD_TYPE=RelWithDebInfo \
        -DPG_CONFIG=/opt/pg18/bin/pg_config -DPROTON_ROOT=/opt/proton -DECHOO_ENABLE_TEST_HOOKS=OFF \
        -DECHOO_EXTENSION_LIBDIR=/opt/arms/$arm; \
      cmake --build /tmp/build-$arm --parallel 2 && cmake --install /tmp/build-$arm || exit 1; \
    done \
    && cp -L /opt/proton/lib/libqpid-proton*.so* /opt/pg18/lib/postgresql/ \
    && ln -s /state/selected/echoo_pgmq.so /opt/pg18/lib/postgresql/echoo_pgmq.so \
    && sha256sum /opt/arms/A/echoo_pgmq.so /opt/arms/B/echoo_pgmq.so > /opt/native-binaries.sha256 \
    && cp /opt/pg18/include/postgresql/server/portability/instr_time.h /opt/instr_time.h \
    && rm -rf /tmp/build-A /tmp/build-B

COPY scripts/make_test_certs.py /opt/echoo/scripts/make_test_certs.py
COPY bench/diagnostic/server.py /opt/echoo/bench/diagnostic/server.py
RUN dpkg-query -W -f='${Package}\t${Version}\n' > /opt/build-packages.txt \
    && mkdir -p /state && chown rabbitmq:rabbitmq /state
ENV PATH="/opt/pg18/bin:${PATH}"
ENV HOME=/state
ENV LD_LIBRARY_PATH="/opt/proton/lib"
USER rabbitmq
ENTRYPOINT ["python3", "/opt/echoo/bench/diagnostic/server.py"]
