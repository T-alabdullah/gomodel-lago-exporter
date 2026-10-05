-- Read-only login for the exporter on GoModel's database (safe to run again).
-- It can SELECT from the usage table and nothing else.
-- The password is for local development only; use a real secret in production.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'exporter_ro') THEN
        CREATE ROLE exporter_ro LOGIN PASSWORD 'exporter_ro';
    END IF;
END
$$;

-- Belt and braces: every transaction this user opens is read-only.
ALTER ROLE exporter_ro SET default_transaction_read_only = on;

GRANT CONNECT ON DATABASE gomodel TO exporter_ro;
GRANT USAGE ON SCHEMA public TO exporter_ro;
GRANT SELECT ON TABLE usage TO exporter_ro;
