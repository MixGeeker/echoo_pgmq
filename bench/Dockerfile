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

COPY CMakeLists.txt echoo_pgmq.control /opt/echoo/
COPY include /opt/echoo/include
COPY src /opt/echoo/src
COPY sql /opt/echoo/sql
RUN cmake -S /opt/echoo -B /tmp/echoo-build -DCMAKE_BUILD_TYPE=RelWithDebInfo \
      -DPG_CONFIG=/opt/pg18/bin/pg_config -DPROTON_ROOT=/opt/proton -DECHOO_ENABLE_TEST_HOOKS=OFF \
    && cmake --build /tmp/echoo-build --parallel 2 && cmake --install /tmp/echoo-build \
    && cp -L /opt/proton/lib/libqpid-proton*.so* "$(/opt/pg18/bin/pg_config --pkglibdir)/" \
    && rm -rf /tmp/echoo-build

COPY scripts/make_test_certs.py /opt/echoo/scripts/make_test_certs.py
COPY bench/ci_server.py /opt/echoo/bench/ci_server.py
RUN dpkg-query -W -f='${Package}\t${Version}\n' > /opt/build-packages.txt \
    && mkdir -p /state && chown rabbitmq:rabbitmq /state
ENV PATH="/opt/pg18/bin:${PATH}"
ENV HOME=/state
USER rabbitmq
ENTRYPOINT ["python3", "/opt/echoo/bench/ci_server.py"]
