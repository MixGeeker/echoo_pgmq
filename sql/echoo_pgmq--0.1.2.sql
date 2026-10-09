\echo Use "CREATE EXTENSION echoo_pgmq" to load this file. \quit

-- All durable data uses ordinary WAL-logged PostgreSQL tables. No external log.
CREATE SCHEMA echoo_pgmq;
REVOKE ALL ON SCHEMA echoo_pgmq FROM PUBLIC;
GRANT USAGE ON SCHEMA echoo_pgmq TO PUBLIC;

CREATE TABLE echoo_pgmq.limits (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    max_messages bigint NOT NULL DEFAULT 100000 CHECK (max_messages > 0),
    max_bytes bigint NOT NULL DEFAULT 1073741824 CHECK (max_bytes > 0),
    max_message_bytes integer NOT NULL DEFAULT 1048576 CHECK (max_message_bytes > 0),
    message_count bigint NOT NULL DEFAULT 0 CHECK (message_count >= 0),
    total_bytes bigint NOT NULL DEFAULT 0 CHECK (total_bytes >= 0)
);
CREATE TABLE echoo_pgmq.queues (
    queue_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL UNIQUE CHECK (name ~ '^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$'),
    max_messages bigint NOT NULL CHECK (max_messages > 0),
    max_bytes bigint NOT NULL CHECK (max_bytes > 0),
    max_message_bytes integer NOT NULL CHECK (max_message_bytes > 0),
    max_attempts integer NOT NULL CHECK (max_attempts BETWEEN 1 AND 1000000),
    message_count bigint NOT NULL DEFAULT 0 CHECK (message_count >= 0),
    total_bytes bigint NOT NULL DEFAULT 0 CHECK (total_bytes >= 0),
    max_idempotency_keys integer NOT NULL DEFAULT 10000 CHECK (max_idempotency_keys > 0),
    idempotency_keys integer NOT NULL DEFAULT 0 CHECK (idempotency_keys >= 0),
    idempotency_window_seconds integer NOT NULL DEFAULT 86400
        CHECK (idempotency_window_seconds BETWEEN 1 AND 604800),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE echoo_pgmq.queue_acl (
    queue_id bigint NOT NULL REFERENCES echoo_pgmq.queues(queue_id) ON DELETE CASCADE,
    principal text NOT NULL CHECK (octet_length(principal) BETWEEN 1 AND 63),
    principal_role regrole NOT NULL,
    can_produce boolean NOT NULL,
    can_consume boolean NOT NULL,
    PRIMARY KEY (queue_id, principal)
);
CREATE TABLE echoo_pgmq.messages (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    queue_id bigint NOT NULL REFERENCES echoo_pgmq.queues(queue_id),
    body bytea NOT NULL,
    state text NOT NULL DEFAULT 'ready' CHECK (state IN ('ready', 'inflight', 'dead')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_until timestamptz,
    owner uuid,
    generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    attempt_limit integer NOT NULL CHECK (attempt_limit BETWEEN 1 AND 1000000),
    exhausted boolean NOT NULL DEFAULT false,
    CHECK (exhausted = (attempts >= attempt_limit)),
    last_error text,
    CHECK ((state = 'inflight' AND lease_until IS NOT NULL AND owner IS NOT NULL)
        OR (state <> 'inflight' AND lease_until IS NULL AND owner IS NULL))
);
CREATE INDEX messages_visible ON echoo_pgmq.messages(queue_id, available_at, id)
    WHERE state IN ('ready', 'inflight') AND NOT exhausted;
CREATE INDEX messages_exhausted ON echoo_pgmq.messages(queue_id, available_at, id)
    WHERE state = 'inflight' AND exhausted;
CREATE INDEX messages_dead ON echoo_pgmq.messages(queue_id, id) WHERE state = 'dead';
CREATE TABLE echoo_pgmq.idempotency (
    queue_id bigint NOT NULL REFERENCES echoo_pgmq.queues(queue_id) ON DELETE CASCADE,
    key text NOT NULL CHECK (octet_length(key) BETWEEN 1 AND 128),
    message_id bigint NOT NULL,
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (queue_id, key)
);
CREATE INDEX idempotency_expiration ON echoo_pgmq.idempotency(queue_id, expires_at);

-- No caller receives direct table/sequence privileges. Every definer function
-- fixes search_path with pg_temp last, and qualifies application objects.
REVOKE ALL ON ALL TABLES IN SCHEMA echoo_pgmq FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA echoo_pgmq FROM PUBLIC;

CREATE FUNCTION echoo_pgmq.create_queue(
    p_queue text, p_max_messages bigint DEFAULT 10000,
    p_max_bytes bigint DEFAULT 67108864, p_max_message_bytes integer DEFAULT 1048576,
    p_max_attempts integer DEFAULT 5
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, echoo_pgmq, pg_temp AS $$
BEGIN
    -- Leave extension config tables empty at installation so pg_dump data can
    -- restore without colliding with a seeded singleton row.
    INSERT INTO echoo_pgmq.limits DEFAULT VALUES ON CONFLICT(singleton) DO NOTHING;
    INSERT INTO echoo_pgmq.queues(name,max_messages,max_bytes,max_message_bytes,max_attempts)
    VALUES (p_queue,p_max_messages,p_max_bytes,p_max_message_bytes,p_max_attempts);
END $$;

CREATE FUNCTION echoo_pgmq.grant_queue(
    p_queue text, p_principal text,
    p_produce boolean DEFAULT true, p_consume boolean DEFAULT true
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, echoo_pgmq, pg_temp AS $$
DECLARE v_queue_id bigint;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = p_principal) THEN
        RAISE EXCEPTION 'queue principal must be an existing PostgreSQL role' USING ERRCODE='42704';
    END IF;
    SELECT queue_id INTO STRICT v_queue_id FROM echoo_pgmq.queues WHERE name=p_queue;
    INSERT INTO echoo_pgmq.queue_acl(queue_id,principal,principal_role,can_produce,can_consume)
    VALUES(v_queue_id,p_principal,
           (SELECT oid::regrole FROM pg_catalog.pg_roles WHERE rolname=p_principal),
           p_produce,p_consume)
    ON CONFLICT(queue_id,principal) DO UPDATE
        SET principal_role=excluded.principal_role,
            can_produce=excluded.can_produce, can_consume=excluded.can_consume;
END $$;

CREATE FUNCTION echoo_pgmq.revoke_queue(p_queue text,p_principal text)
RETURNS void LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, echoo_pgmq, pg_temp AS $$
    DELETE FROM echoo_pgmq.queue_acl a USING echoo_pgmq.queues q
    WHERE a.queue_id=q.queue_id AND q.name=p_queue AND a.principal=p_principal;
$$;

CREATE FUNCTION echoo_pgmq.authorize(p_queue text,p_identity text,p_operation text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, echoo_pgmq, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM echoo_pgmq.queues q JOIN echoo_pgmq.queue_acl a USING(queue_id)
        JOIN pg_catalog.pg_roles r ON r.oid=a.principal_role AND r.rolname=a.principal
        WHERE q.name=p_queue AND a.principal=p_identity
        AND CASE WHEN p_operation IN ('publish','enqueue','produce') THEN a.can_produce
                 WHEN p_operation IN ('consume','claim','read','settle') THEN a.can_consume
                 ELSE false END
    );
$$;

CREATE FUNCTION echoo_pgmq._require_access(p_queue text,p_identity text,p_operation text)
RETURNS bigint LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, echoo_pgmq, pg_temp AS $$
DECLARE v_queue_id bigint;
BEGIN
    SELECT q.queue_id INTO v_queue_id
      FROM echoo_pgmq.queues q JOIN echoo_pgmq.queue_acl a USING(queue_id)
        JOIN pg_catalog.pg_roles r ON r.oid=a.principal_role AND r.rolname=a.principal
     WHERE q.name=p_queue AND a.principal=p_identity
       AND CASE WHEN p_operation='produce' THEN a.can_produce
                WHEN p_operation='consume' THEN a.can_consume ELSE false END;
    IF v_queue_id IS NULL THEN
        RAISE EXCEPTION 'queue access denied' USING ERRCODE='42501';
    END IF;
    RETURN v_queue_id;
END $$;

CREATE FUNCTION echoo_pgmq._enqueue(
    p_queue text,p_body bytea,p_identity text,p_idempotency_key text
) RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, echoo_pgmq, pg_temp AS $$
DECLARE
    v_queue_id bigint; v_queue echoo_pgmq.queues%ROWTYPE;
    v_limits echoo_pgmq.limits%ROWTYPE; v_size bigint;
    v_id bigint; v_deleted integer; v_now timestamptz;
BEGIN
    v_queue_id := echoo_pgmq._require_access(p_queue,p_identity,'produce');
    IF p_body IS NULL THEN
        RAISE EXCEPTION 'message body must not be null' USING ERRCODE='22004';
    END IF;
    IF p_idempotency_key IS NOT NULL AND
        octet_length(p_idempotency_key) NOT BETWEEN 1 AND 128 THEN
        RAISE EXCEPTION 'idempotency key must be 1..128 bytes' USING ERRCODE='22023';
    END IF;
    v_size := octet_length(p_body);
    -- All paths changing capacity acquire global, queue, then message locks.
    SELECT * INTO STRICT v_limits FROM echoo_pgmq.limits WHERE singleton FOR UPDATE;
    SELECT * INTO STRICT v_queue FROM echoo_pgmq.queues WHERE queue_id=v_queue_id FOR NO KEY UPDATE;
    v_now := clock_timestamp();
    IF p_idempotency_key IS NOT NULL THEN
        -- Targeted expiration ensures this key can be reused even if the bounded
        -- housekeeping pass has not reached it yet. No unbounded delete or scan.
        DELETE FROM echoo_pgmq.idempotency
         WHERE queue_id=v_queue_id AND key=p_idempotency_key AND expires_at<=v_now;
        GET DIAGNOSTICS v_deleted = ROW_COUNT;
        WITH expired AS (
            SELECT key FROM echoo_pgmq.idempotency
             WHERE queue_id=v_queue_id AND expires_at<=v_now
             ORDER BY expires_at LIMIT 64
        ), removed AS (
            DELETE FROM echoo_pgmq.idempotency d USING expired e
             WHERE d.queue_id=v_queue_id AND d.key=e.key RETURNING 1
        ) SELECT v_deleted+count(*) INTO v_deleted FROM removed;
        IF v_deleted>0 THEN
            UPDATE echoo_pgmq.queues SET idempotency_keys=idempotency_keys-v_deleted
             WHERE queue_id=v_queue_id;
            v_queue.idempotency_keys := v_queue.idempotency_keys-v_deleted;
        END IF;
        SELECT message_id INTO v_id FROM echoo_pgmq.idempotency
         WHERE queue_id=v_queue_id AND key=p_idempotency_key;
        IF FOUND THEN RETURN v_id; END IF;
        IF v_queue.idempotency_keys>=v_queue.max_idempotency_keys THEN
            RAISE EXCEPTION 'queue idempotency capacity exceeded' USING ERRCODE='54000';
        END IF;
    END IF;
    IF v_size>v_limits.max_message_bytes OR v_size>v_queue.max_message_bytes THEN
        RAISE EXCEPTION 'message size limit exceeded' USING ERRCODE='54000';
    END IF;
    IF v_limits.message_count>=v_limits.max_messages
       OR v_size>v_limits.max_bytes-v_limits.total_bytes
       OR v_queue.message_count>=v_queue.max_messages
       OR v_size>v_queue.max_bytes-v_queue.total_bytes THEN
        RAISE EXCEPTION 'queue capacity exceeded' USING ERRCODE='54000';
    END IF;
    INSERT INTO echoo_pgmq.messages(queue_id,body,created_at,available_at,attempt_limit)
      VALUES(v_queue_id,p_body,v_now,v_now,v_queue.max_attempts) RETURNING id INTO v_id;
    UPDATE echoo_pgmq.limits SET message_count=message_count+1,total_bytes=total_bytes+v_size
     WHERE singleton;
    UPDATE echoo_pgmq.queues SET message_count=message_count+1,total_bytes=total_bytes+v_size
     WHERE queue_id=v_queue_id;
    IF p_idempotency_key IS NOT NULL THEN
        INSERT INTO echoo_pgmq.idempotency(queue_id,key,message_id,expires_at)
        VALUES(v_queue_id,p_idempotency_key,v_id,
            v_now+make_interval(secs=>v_queue.idempotency_window_seconds));
        UPDATE echoo_pgmq.queues SET idempotency_keys=idempotency_keys+1
         WHERE queue_id=v_queue_id;
    END IF;
    RETURN v_id;
END $$;

CREATE FUNCTION echoo_pgmq.publish(p_queue text,p_body bytea,p_identity text)
RETURNS bigint LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, echoo_pgmq, pg_temp AS $$
    SELECT echoo_pgmq._enqueue(p_queue,p_body,p_identity,NULL);
$$;

CREATE FUNCTION echoo_pgmq.enqueue(p_queue text,p_body bytea,p_idempotency_key text DEFAULT NULL)
RETURNS bigint LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, echoo_pgmq, pg_temp AS $$
    SELECT echoo_pgmq._enqueue(p_queue,p_body,session_user::text,p_idempotency_key);
$$;

CREATE FUNCTION echoo_pgmq.claim(
    p_queue text,p_identity text,p_owner uuid,p_visibility_seconds integer
) RETURNS TABLE(id bigint,generation bigint,body bytea)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, echoo_pgmq, pg_temp AS $$
DECLARE v_queue_id bigint; v_now timestamptz;
BEGIN
    v_queue_id := echoo_pgmq._require_access(p_queue,p_identity,'consume');
    IF p_owner IS NULL OR p_visibility_seconds IS NULL
       OR p_visibility_seconds NOT BETWEEN 1 AND 86400 THEN
        RAISE EXCEPTION 'owner and visibility timeout (1..86400 seconds) required' USING ERRCODE='22023';
    END IF;
    v_now := clock_timestamp();
    -- Exhausted leases are moved to the same queue's explicit dead-letter state
    -- in a bounded batch; retained body and counters remain unchanged.
    WITH expired AS (
        SELECT m.id FROM echoo_pgmq.messages m
         WHERE m.queue_id=v_queue_id AND m.state='inflight' AND m.exhausted
           AND m.available_at<=v_now
         ORDER BY m.available_at,m.id FOR UPDATE SKIP LOCKED LIMIT 64
    ) UPDATE echoo_pgmq.messages m SET state='dead',owner=NULL,lease_until=NULL,
          last_error='maximum delivery attempts exceeded'
       FROM expired e WHERE m.id=e.id;
    RETURN QUERY
    WITH candidate AS (
        SELECT m.id FROM echoo_pgmq.messages m
         WHERE m.queue_id=v_queue_id AND m.state IN ('ready','inflight')
           AND m.available_at<=v_now AND NOT m.exhausted
         ORDER BY m.available_at,m.id FOR UPDATE SKIP LOCKED LIMIT 1
    ) UPDATE echoo_pgmq.messages m
         SET state='inflight',owner=p_owner,generation=m.generation+1,attempts=m.attempts+1,
             exhausted=(m.attempts+1>=m.attempt_limit),
             lease_until=v_now+make_interval(secs=>p_visibility_seconds),
             available_at=v_now+make_interval(secs=>p_visibility_seconds)
        FROM candidate c WHERE m.id=c.id RETURNING m.id,m.generation,m.body;
END $$;

CREATE FUNCTION echoo_pgmq.read(p_queue text,p_owner uuid,p_visibility_seconds integer DEFAULT 30)
RETURNS TABLE(id bigint,generation bigint,body bytea)
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, echoo_pgmq, pg_temp AS $$
    SELECT * FROM echoo_pgmq.claim(p_queue,session_user::text,p_owner,p_visibility_seconds);
$$;

CREATE FUNCTION echoo_pgmq.settle(
    p_queue text,p_id bigint,p_generation bigint,p_owner uuid,p_outcome text,p_identity text
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, echoo_pgmq, pg_temp AS $$
DECLARE v_queue_id bigint; v_message echoo_pgmq.messages%ROWTYPE;
    v_now timestamptz;
BEGIN
    v_queue_id := echoo_pgmq._require_access(p_queue,p_identity,'consume');
    IF p_outcome IS NULL OR p_outcome NOT IN ('accepted','released','rejected','modified') THEN
        RAISE EXCEPTION 'unknown settlement outcome' USING ERRCODE='22023';
    END IF;
    -- Queue counters never change queue_id. NO KEY UPDATE remains exclusive
    -- against quota writers while permitting FK KEY SHARE during quarantine.
    -- Only ACK deletes payload and changes quota counters. Retry/reject must
    -- not acquire quota locks while a caller already holds the claimed row.
    IF p_outcome='accepted' THEN
        PERFORM 1 FROM echoo_pgmq.limits WHERE singleton FOR UPDATE;
        PERFORM 1 FROM echoo_pgmq.queues WHERE queue_id=v_queue_id FOR NO KEY UPDATE;
    END IF;
    SELECT * INTO v_message FROM echoo_pgmq.messages
     WHERE messages.id=p_id AND queue_id=v_queue_id FOR UPDATE;
    v_now := clock_timestamp();
    IF NOT FOUND OR v_message.state<>'inflight'
       OR v_message.owner IS DISTINCT FROM p_owner
       OR v_message.generation IS DISTINCT FROM p_generation
       OR v_message.lease_until<=v_now THEN
        RETURN false;
    END IF;
    IF p_outcome='accepted' THEN
        DELETE FROM echoo_pgmq.messages WHERE messages.id=p_id;
        UPDATE echoo_pgmq.queues SET message_count=message_count-1,
            total_bytes=total_bytes-octet_length(v_message.body) WHERE queue_id=v_queue_id;
        UPDATE echoo_pgmq.limits SET message_count=message_count-1,
            total_bytes=total_bytes-octet_length(v_message.body) WHERE singleton;
    ELSIF p_outcome='rejected' OR v_message.exhausted THEN
        UPDATE echoo_pgmq.messages SET state='dead',owner=NULL,lease_until=NULL,
            last_error=CASE WHEN p_outcome='rejected' THEN 'consumer rejected'
                            ELSE 'maximum delivery attempts exceeded' END
         WHERE messages.id=p_id;
    ELSE
        UPDATE echoo_pgmq.messages SET state='ready',owner=NULL,lease_until=NULL,
            available_at=v_now,last_error=CASE WHEN p_outcome='modified' THEN 'consumer modified' ELSE NULL END
         WHERE messages.id=p_id;
    END IF;
    RETURN true;
END $$;

CREATE FUNCTION echoo_pgmq.ack(p_queue text,p_id bigint,p_generation bigint,p_owner uuid)
RETURNS boolean LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, echoo_pgmq, pg_temp AS $$
    SELECT echoo_pgmq.settle(p_queue,p_id,p_generation,p_owner,'accepted',session_user::text);
$$;
CREATE FUNCTION echoo_pgmq.release(p_queue text,p_id bigint,p_generation bigint,p_owner uuid)
RETURNS boolean LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, echoo_pgmq, pg_temp AS $$
    SELECT echoo_pgmq.settle(p_queue,p_id,p_generation,p_owner,'released',session_user::text);
$$;
CREATE FUNCTION echoo_pgmq.reject(p_queue text,p_id bigint,p_generation bigint,p_owner uuid)
RETURNS boolean LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, echoo_pgmq, pg_temp AS $$
    SELECT echoo_pgmq.settle(p_queue,p_id,p_generation,p_owner,'rejected',session_user::text);
$$;

-- Administrator APIs. A dead message continues consuming storage until purged.
CREATE FUNCTION echoo_pgmq.purge_dead(p_queue text,p_limit integer DEFAULT 100)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, echoo_pgmq, pg_temp AS $$
DECLARE v_queue_id bigint; v_count bigint; v_bytes bigint;
BEGIN
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 10000 THEN
        RAISE EXCEPTION 'purge batch must be 1..10000' USING ERRCODE='22023';
    END IF;
    PERFORM 1 FROM echoo_pgmq.limits WHERE singleton FOR UPDATE;
    SELECT queue_id INTO STRICT v_queue_id FROM echoo_pgmq.queues WHERE name=p_queue FOR NO KEY UPDATE;
    WITH candidates AS (
        SELECT id FROM echoo_pgmq.messages WHERE queue_id=v_queue_id AND state='dead'
         ORDER BY id FOR UPDATE SKIP LOCKED LIMIT p_limit
    ), removed AS (
        DELETE FROM echoo_pgmq.messages m USING candidates c WHERE m.id=c.id
        RETURNING octet_length(m.body) AS bytes
    ) SELECT count(*),coalesce(sum(bytes),0) INTO v_count,v_bytes FROM removed;
    UPDATE echoo_pgmq.queues SET message_count=message_count-v_count,total_bytes=total_bytes-v_bytes
     WHERE queue_id=v_queue_id;
    UPDATE echoo_pgmq.limits SET message_count=message_count-v_count,total_bytes=total_bytes-v_bytes
     WHERE singleton;
    RETURN v_count;
END $$;

CREATE FUNCTION echoo_pgmq.retry_dead(p_queue text,p_id bigint)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, echoo_pgmq, pg_temp AS $$
DECLARE v_changed integer;
BEGIN
    UPDATE echoo_pgmq.messages m SET state='ready',attempts=0,exhausted=false,owner=NULL,lease_until=NULL,
        generation=generation+1,available_at=clock_timestamp(),last_error=NULL
     FROM echoo_pgmq.queues q WHERE m.queue_id=q.queue_id AND q.name=p_queue
        AND m.id=p_id AND m.state='dead';
    GET DIAGNOSTICS v_changed=ROW_COUNT;
    RETURN v_changed=1;
END $$;

-- Administrator API. Removes a queue together with every retained message
-- (ready, inflight and dead), its ACL and idempotency keys, and returns the
-- reclaimed global capacity in the same transaction. Message row locks are
-- taken NOWAIT after the global and queue locks, so a concurrently open
-- claim/settle transaction makes this call fail with 55P03 for the caller to
-- retry instead of waiting into a deadlock with a later ACK.
CREATE FUNCTION echoo_pgmq.drop_queue(p_queue text,p_missing_ok boolean DEFAULT false)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, echoo_pgmq, pg_temp AS $$
DECLARE v_queue_id bigint; v_count bigint; v_bytes bigint;
BEGIN
    PERFORM 1 FROM echoo_pgmq.limits WHERE singleton FOR UPDATE;
    SELECT queue_id INTO v_queue_id FROM echoo_pgmq.queues WHERE name=p_queue FOR UPDATE;
    IF NOT FOUND THEN
        IF coalesce(p_missing_ok,false) THEN RETURN NULL; END IF;
        RAISE EXCEPTION 'queue "%" does not exist',p_queue USING ERRCODE='42704';
    END IF;
    PERFORM 1 FROM echoo_pgmq.messages WHERE queue_id=v_queue_id FOR UPDATE NOWAIT;
    WITH removed AS (
        DELETE FROM echoo_pgmq.messages WHERE queue_id=v_queue_id
        RETURNING octet_length(body) AS bytes
    ) SELECT count(*),coalesce(sum(bytes),0) INTO v_count,v_bytes FROM removed;
    -- queue_acl and idempotency rows cascade from the queue.
    DELETE FROM echoo_pgmq.queues WHERE queue_id=v_queue_id;
    UPDATE echoo_pgmq.limits SET message_count=message_count-v_count,total_bytes=total_bytes-v_bytes
     WHERE singleton;
    RETURN v_count;
END $$;

-- Construct a valid AMQP 1.0 data-section message from arbitrary application bytes.
CREATE FUNCTION echoo_pgmq.message_binary(bytea) RETURNS bytea
AS 'MODULE_PATHNAME','echoo_pgmq_message_binary'
LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;

CREATE FUNCTION echoo_pgmq.enqueue_binary(
    p_queue text,p_body bytea,p_idempotency_key text DEFAULT NULL
) RETURNS bigint LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, echoo_pgmq, pg_temp AS $$
    SELECT echoo_pgmq._enqueue(p_queue,echoo_pgmq.message_binary(p_body),
                              session_user::text,p_idempotency_key);
$$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA echoo_pgmq FROM PUBLIC;
GRANT EXECUTE ON FUNCTION echoo_pgmq.message_binary(bytea),
    echoo_pgmq.enqueue_binary(text,bytea,text),echoo_pgmq.enqueue(text,bytea,text),
    echoo_pgmq.read(text,uuid,integer),echoo_pgmq.ack(text,bigint,bigint,uuid),
    echoo_pgmq.release(text,bigint,bigint,uuid),echoo_pgmq.reject(text,bigint,bigint,uuid) TO PUBLIC;

-- Tell pg_dump to preserve queue data and configuration (including counters).
SELECT pg_catalog.pg_extension_config_dump('echoo_pgmq.limits','');
SELECT pg_catalog.pg_extension_config_dump('echoo_pgmq.queues','');
SELECT pg_catalog.pg_extension_config_dump('echoo_pgmq.queue_acl','');
SELECT pg_catalog.pg_extension_config_dump('echoo_pgmq.messages','');
SELECT pg_catalog.pg_extension_config_dump('echoo_pgmq.idempotency','');
-- Identity sequences are extension-owned too; dumping table rows alone does not
-- advance them on restore and could reuse accepted/deleted message IDs.
SELECT pg_catalog.pg_extension_config_dump('echoo_pgmq.queues_queue_id_seq','');
SELECT pg_catalog.pg_extension_config_dump('echoo_pgmq.messages_id_seq','');

-- First additive migration. No message rewrite and no protocol/storage format change.
CREATE VIEW echoo_pgmq.queue_stats WITH (security_barrier=true) AS
    SELECT q.name AS queue_name,q.message_count,q.total_bytes,
           q.max_messages,q.max_bytes,q.max_message_bytes,q.max_attempts,
           q.idempotency_keys,q.max_idempotency_keys
      FROM echoo_pgmq.queues q
     WHERE EXISTS (
         SELECT 1 FROM echoo_pgmq.queue_acl a
          JOIN pg_catalog.pg_roles r ON r.oid=a.principal_role AND r.rolname=a.principal
          WHERE a.queue_id=q.queue_id AND a.principal=session_user::text
            AND (a.can_produce OR a.can_consume)
     );
GRANT SELECT ON echoo_pgmq.queue_stats TO PUBLIC;
COMMENT ON VIEW echoo_pgmq.queue_stats IS
    'Current retained payload counters for queues authorized to session_user; includes dead letters.';
