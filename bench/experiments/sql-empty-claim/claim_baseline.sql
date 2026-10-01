CREATE OR REPLACE FUNCTION echoo_pgmq.claim_baseline(p_queue text, p_identity text, p_owner uuid, p_visibility_seconds integer)
 RETURNS TABLE(id bigint, generation bigint, body bytea)
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'echoo_pgmq', 'pg_temp'
AS $function$
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
END $function$;

REVOKE ALL ON FUNCTION echoo_pgmq.claim_baseline(text,text,uuid,integer) FROM PUBLIC;
