"""Demo-only admin provisioning, after GoModel has migrated its own schema."""
import os
import psycopg
from psycopg import sql
from exporter.reader import UsageReader
from scripts import gomodel_setup, lago_setup


def main():
    with psycopg.connect(os.environ['GOMODEL_ADMIN_DB_URL'], autocommit=True) as conn:
        # Validate the actual pinned upstream schema; don't create a facsimile.
        expected = {'id': 'uuid', 'timestamp': 'timestamp with time zone', 'labels': 'jsonb',
                    'raw_data': 'jsonb', 'input_tokens': 'integer', 'output_tokens': 'integer'}
        columns = dict(conn.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema='public' AND table_name='usage'").fetchall())
        assert all(columns.get(k) == v for k, v in expected.items()), 'GoModel schema contract changed'
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname='exporter_ro'").fetchone():
            conn.execute('CREATE ROLE exporter_ro LOGIN')
        conn.execute(sql.SQL('ALTER ROLE exporter_ro PASSWORD {}').format(sql.Literal(os.environ['READONLY_PASSWORD'])))
        conn.execute('ALTER ROLE exporter_ro SET default_transaction_read_only = on')
        conn.execute('GRANT CONNECT ON DATABASE gomodel TO exporter_ro')
        conn.execute('GRANT USAGE ON SCHEMA public TO exporter_ro')
        conn.execute('GRANT SELECT ON TABLE usage TO exporter_ro')
    UsageReader(os.environ['EXPORTER_GOMODEL_DB_URL']).read_after(None, 1)
    # Even explicitly disabling transaction read-only must not permit writes.
    with psycopg.connect(os.environ['EXPORTER_GOMODEL_DB_URL'], autocommit=True) as conn:
        conn.execute('SET default_transaction_read_only=off')
        try:
            conn.execute('DELETE FROM usage WHERE false')
        except psycopg.errors.InsufficientPrivilege:
            pass
        else:
            raise AssertionError('Exporter login has source write privileges')
    gomodel_setup.main()
    lago_setup.main()


if __name__ == '__main__':
    main()
