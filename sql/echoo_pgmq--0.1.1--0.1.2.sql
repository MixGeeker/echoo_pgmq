\echo Use "ALTER EXTENSION echoo_pgmq UPDATE TO '0.1.2'" to load this file. \quit

-- Replace only the private enqueue function. Preserve its identity and ACL.
-- No table, durable format, protocol, native worker or public grant change.
CREATE OR REPLACE FUNCTION echoo_pgmq._enqueue(
    p_queue text,p_body bytea,p_identity text,p_idempotency_key text
) RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, echoo_pgmq, pg_temp AS $$
DECLARE
    v_queue_id bigint; v_queue echoo_pgmq.queues%ROWTYPE;
    v_limits echoo_pgmq.limits%ROWTYPE; v_size bigint;
    v_id bigint; v_deleted integer; v_now timestamptz;
    v_global_reserved boolean := false; v_queue_reserved boolean := false;
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
    -- EXPERIMENT ONLY: normal NULL-key publishes reserve counters with the
    -- UPDATE itself. A no-match is only a hint: use the original locking read
    -- so a full old snapshot still waits for an uncommitted capacity release.
    IF p_idempotency_key IS NULL THEN
        UPDATE echoo_pgmq.limits
           SET message_count=message_count+1,total_bytes=total_bytes+v_size
         WHERE singleton AND v_size<=max_message_bytes
           AND message_count<max_messages AND v_size<=max_bytes-total_bytes
         RETURNING * INTO v_limits;
        v_global_reserved := FOUND;
        IF v_global_reserved THEN
            -- Preserve the original later checks against pre-reservation values.
            v_limits.message_count := v_limits.message_count-1;
            v_limits.total_bytes := v_limits.total_bytes-v_size;
        END IF;
    END IF;
    IF NOT v_global_reserved THEN
        SELECT * INTO STRICT v_limits FROM echoo_pgmq.limits WHERE singleton FOR UPDATE;
    END IF;
    -- Keep the whole original queue path after a global fallback. This avoids
    -- a new queue UPDATE when the global capacity check will later reject.
    IF v_global_reserved THEN
        UPDATE echoo_pgmq.queues
           SET message_count=message_count+1,total_bytes=total_bytes+v_size
         WHERE queue_id=v_queue_id AND v_size<=max_message_bytes
           AND message_count<max_messages AND v_size<=max_bytes-total_bytes
         RETURNING * INTO v_queue;
        v_queue_reserved := FOUND;
        IF v_queue_reserved THEN
            v_queue.message_count := v_queue.message_count-1;
            v_queue.total_bytes := v_queue.total_bytes-v_size;
        END IF;
    END IF;
    IF NOT v_queue_reserved THEN
        SELECT * INTO STRICT v_queue FROM echoo_pgmq.queues WHERE queue_id=v_queue_id FOR NO KEY UPDATE;
    END IF;
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
    IF NOT v_global_reserved THEN
        UPDATE echoo_pgmq.limits SET message_count=message_count+1,total_bytes=total_bytes+v_size
         WHERE singleton;
    END IF;
    IF NOT v_queue_reserved THEN
        UPDATE echoo_pgmq.queues SET message_count=message_count+1,total_bytes=total_bytes+v_size
         WHERE queue_id=v_queue_id;
    END IF;
    IF p_idempotency_key IS NOT NULL THEN
        INSERT INTO echoo_pgmq.idempotency(queue_id,key,message_id,expires_at)
        VALUES(v_queue_id,p_idempotency_key,v_id,
            v_now+make_interval(secs=>v_queue.idempotency_window_seconds));
        UPDATE echoo_pgmq.queues SET idempotency_keys=idempotency_keys+1
         WHERE queue_id=v_queue_id;
    END IF;
    RETURN v_id;
END $$;
