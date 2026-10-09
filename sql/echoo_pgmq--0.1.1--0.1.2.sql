\echo Use "ALTER EXTENSION echoo_pgmq UPDATE TO '0.1.2'" to load this file. \quit

-- Additive migration: new administrator-only drop_queue. No message rewrite and
-- no protocol/storage format change.
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
REVOKE ALL ON FUNCTION echoo_pgmq.drop_queue(text,boolean) FROM PUBLIC;
