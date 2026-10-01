\set ON_ERROR_STOP on
-- Execute as a superuser against an installed extension. All fixtures roll back.
BEGIN;
CREATE ROLE echoo_sql_alice;
CREATE ROLE echoo_sql_bob;
SELECT echoo_pgmq.create_queue('sql/binary',100,10000,1000,3);
SELECT echoo_pgmq.create_queue('sql/rollback',10,10000,1000,3);
SELECT echoo_pgmq.create_queue('sql/capacity',2,8,5,3);
SELECT echoo_pgmq.create_queue('sql/dead',10,10000,1000,2);
SELECT echoo_pgmq.create_queue('sql/private',10,10000,1000,3);
SELECT echoo_pgmq.grant_queue(name,'echoo_sql_alice')
  FROM echoo_pgmq.queues WHERE name LIKE 'sql/%' AND name<>'sql/private';
SELECT echoo_pgmq.grant_queue('sql/private','echoo_sql_bob');

SET SESSION AUTHORIZATION echoo_sql_alice;
DO $$
DECLARE v_id bigint; v_receipt record; v_owner uuid:=gen_random_uuid();
    v_body bytea:=decode('0053704500537345005375a00500ff80fe00','hex');
BEGIN
    v_id:=echoo_pgmq.enqueue('sql/binary',v_body,'stable-key');
    IF echoo_pgmq.enqueue('sql/binary',decode('aabb','hex'),'stable-key')<>v_id THEN
        RAISE EXCEPTION 'duplicate key did not return original message ID';
    END IF;
    SELECT * INTO STRICT v_receipt FROM echoo_pgmq.read('sql/binary',v_owner,30);
    IF v_receipt.body<>v_body OR v_receipt.id<>v_id OR v_receipt.generation<>1 THEN
        RAISE EXCEPTION 'binary payload or receipt changed';
    END IF;
    IF echoo_pgmq.ack('sql/binary',v_id,1,gen_random_uuid()) THEN
        RAISE EXCEPTION 'wrong owner acknowledged message';
    END IF;
    IF echoo_pgmq.ack('sql/binary',v_id,2,v_owner) THEN
        RAISE EXCEPTION 'wrong generation acknowledged message';
    END IF;
    IF NOT echoo_pgmq.ack('sql/binary',v_id,1,v_owner) THEN
        RAISE EXCEPTION 'current receipt rejected';
    END IF;
    IF echoo_pgmq.ack('sql/binary',v_id,1,v_owner) THEN
        RAISE EXCEPTION 'duplicate receipt acknowledged deleted message';
    END IF;
    IF echoo_pgmq.enqueue('sql/binary',v_body,'stable-key')<>v_id THEN
        RAISE EXCEPTION 'dedup receipt lost after accepted settlement';
    END IF;
    IF EXISTS(SELECT * FROM echoo_pgmq.read('sql/binary',v_owner)) THEN
        RAISE EXCEPTION 'dedup created another message';
    END IF;
END $$;

CREATE TEMP TABLE business_fixture(id integer);
SAVEPOINT atomic_business;
INSERT INTO business_fixture VALUES(1);
SELECT echoo_pgmq.enqueue('sql/rollback',decode('00ff','hex'));
ROLLBACK TO SAVEPOINT atomic_business;
DO $$ BEGIN
    IF EXISTS(SELECT * FROM business_fixture) OR
       EXISTS(SELECT * FROM echoo_pgmq.read('sql/rollback',gen_random_uuid())) THEN
        RAISE EXCEPTION 'business mutation or enqueue survived rollback';
    END IF;
END $$;

DO $$
BEGIN
    BEGIN
        PERFORM echoo_pgmq.enqueue('sql/private',decode('00','hex'));
        RAISE EXCEPTION 'cross-tenant publish incorrectly permitted';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN
        PERFORM * FROM echoo_pgmq.read('sql/private',gen_random_uuid());
        RAISE EXCEPTION 'cross-tenant consume incorrectly permitted';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN
        PERFORM echoo_pgmq.publish('sql/private',decode('00','hex'),'echoo_sql_bob');
        RAISE EXCEPTION 'caller-spoofed identity incorrectly permitted';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN
        PERFORM echoo_pgmq._enqueue('sql/private',decode('00','hex'),'echoo_sql_bob',NULL);
        RAISE EXCEPTION 'private enqueue helper incorrectly permitted';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN
        PERFORM echoo_pgmq.create_queue('sql/unauthorized');
        RAISE EXCEPTION 'non-admin created queue';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN
        PERFORM * FROM echoo_pgmq.messages;
        RAISE EXCEPTION 'direct table access incorrectly permitted';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
END $$;

DO $$
DECLARE v_id bigint; v_owner uuid:=gen_random_uuid(); v_receipt record;
BEGIN
    v_id:=echoo_pgmq.enqueue('sql/capacity',decode('00010203','hex'));
    PERFORM echoo_pgmq.enqueue('sql/capacity',decode('04050607','hex'));
    BEGIN
        PERFORM echoo_pgmq.enqueue('sql/capacity',decode('08','hex'));
        RAISE EXCEPTION 'message/byte capacity incorrectly exceeded';
    EXCEPTION WHEN program_limit_exceeded THEN NULL; END;
    SELECT * INTO STRICT v_receipt FROM echoo_pgmq.read('sql/capacity',v_owner);
    PERFORM echoo_pgmq.ack('sql/capacity',v_receipt.id,v_receipt.generation,v_owner);
    BEGIN
        PERFORM echoo_pgmq.enqueue('sql/capacity',decode('0001020304','hex'));
        RAISE EXCEPTION 'byte capacity incorrectly exceeded';
    EXCEPTION WHEN program_limit_exceeded THEN NULL; END;
    BEGIN
        PERFORM echoo_pgmq.enqueue('sql/capacity',decode('000102030405','hex'));
        RAISE EXCEPTION 'individual message bound incorrectly exceeded';
    EXCEPTION WHEN program_limit_exceeded THEN NULL; END;
    PERFORM echoo_pgmq.enqueue('sql/capacity',decode('08090a0b','hex'));
END $$;

DO $$
DECLARE v_id bigint; v_owner uuid:=gen_random_uuid(); v_first record; v_second record;
BEGIN
    v_id:=echoo_pgmq.enqueue('sql/dead',decode('00ff','hex'));
    SELECT * INTO STRICT v_first FROM echoo_pgmq.read('sql/dead',v_owner,1);
    PERFORM pg_sleep(1.05);
    IF echoo_pgmq.ack('sql/dead',v_id,v_first.generation,v_owner) THEN
        RAISE EXCEPTION 'expired receipt acknowledged without redelivery';
    END IF;
    SELECT * INTO STRICT v_second FROM echoo_pgmq.read('sql/dead',v_owner,30);
    IF v_second.id<>v_id OR v_second.generation<=v_first.generation THEN
        RAISE EXCEPTION 'expired message not reclaimed with newer generation';
    END IF;
    IF echoo_pgmq.ack('sql/dead',v_id,v_first.generation,v_owner) THEN
        RAISE EXCEPTION 'stale receipt acknowledged new claim';
    END IF;
    IF NOT echoo_pgmq.release('sql/dead',v_id,v_second.generation,v_owner) THEN
        RAISE EXCEPTION 'current release rejected';
    END IF;
    IF EXISTS(SELECT * FROM echoo_pgmq.read('sql/dead',v_owner)) THEN
        RAISE EXCEPTION 'maximum attempts did not dead-letter message';
    END IF;
END $$;

-- Temporary objects and a caller-controlled search_path cannot shadow internals.
CREATE TEMP TABLE messages(id bigint,body bytea);
SET LOCAL search_path=pg_temp,public;
DO $$ DECLARE v_receipt record; v_owner uuid:=gen_random_uuid(); BEGIN
    PERFORM echoo_pgmq.enqueue('sql/binary',decode('ff0080','hex'));
    SELECT * INTO STRICT v_receipt FROM echoo_pgmq.read('sql/binary',v_owner);
    IF NOT echoo_pgmq.reject('sql/binary',v_receipt.id,v_receipt.generation,v_owner) THEN
        RAISE EXCEPTION 'search_path test reject failed';
    END IF;
END $$;
RESET SESSION AUTHORIZATION;

DO $$
DECLARE v_expected_count bigint; v_expected_bytes bigint;
BEGIN
    SELECT count(*),coalesce(sum(octet_length(body)),0)
      INTO v_expected_count,v_expected_bytes FROM echoo_pgmq.messages;
    IF NOT EXISTS(SELECT 1 FROM echoo_pgmq.limits
      WHERE message_count=v_expected_count AND total_bytes=v_expected_bytes) THEN
        RAISE EXCEPTION 'global capacity counters drifted';
    END IF;
    IF EXISTS(SELECT 1 FROM echoo_pgmq.queues q
      WHERE q.message_count<>(SELECT count(*) FROM echoo_pgmq.messages m WHERE m.queue_id=q.queue_id)
         OR q.total_bytes<>(SELECT coalesce(sum(octet_length(body)),0) FROM echoo_pgmq.messages m WHERE m.queue_id=q.queue_id)) THEN
        RAISE EXCEPTION 'per-queue counters drifted';
    END IF;
    IF NOT EXISTS(SELECT 1 FROM echoo_pgmq.messages m JOIN echoo_pgmq.queues q USING(queue_id)
      WHERE q.name='sql/dead' AND m.state='dead' AND m.attempts=2) THEN
        RAISE EXCEPTION 'dead-letter body not retained';
    END IF;
    IF echoo_pgmq.purge_dead('sql/dead',100)<>1 THEN
        RAISE EXCEPTION 'bounded dead-letter purge failed';
    END IF;
    IF echoo_pgmq.authorize('sql/private','echoo_sql_alice','publish') OR
       echoo_pgmq.authorize('missing','echoo_sql_alice','consume') OR
       NOT echoo_pgmq.authorize('sql/private','echoo_sql_bob','consume') THEN
        RAISE EXCEPTION 'worker authorization probe incorrect';
    END IF;
END $$;
ROLLBACK;
\echo 'core SQL tests passed'
