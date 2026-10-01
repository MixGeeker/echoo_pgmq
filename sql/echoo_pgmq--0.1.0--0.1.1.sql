\echo Use "ALTER EXTENSION echoo_pgmq UPDATE TO '0.1.1'" to load this file. \quit

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
