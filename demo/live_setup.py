"""Provision the optional Llama lab without changing existing demo API keys."""
import copy
import json
import os

import httpx
import psycopg
from psycopg import sql

from exporter.config import get_settings
from exporter.state.store import StateStore
from scripts import lago_setup


def main():
    settings = get_settings()
    with psycopg.connect(os.environ['GOMODEL_ADMIN_DB_URL'], autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname='exporter_ro'").fetchone():
            conn.execute('CREATE ROLE exporter_ro LOGIN')
        conn.execute(sql.SQL('ALTER ROLE exporter_ro PASSWORD {}').format(sql.Literal(os.environ['READONLY_PASSWORD'])))
        conn.execute('ALTER ROLE exporter_ro SET default_transaction_read_only = on')
        conn.execute('GRANT CONNECT ON DATABASE gomodel TO exporter_ro')
        conn.execute('GRANT USAGE ON SCHEMA public TO exporter_ro')
        conn.execute('GRANT SELECT ON TABLE usage TO exporter_ro')
    with StateStore(settings.state_db_url) as store:
        store.init_schema()
    pricing = copy.deepcopy(json.loads(lago_setup.PRICING_FILE.read_text()))
    pricing['prices_per_million_tokens']['llama3.2:1b'] = pricing['default_price_per_million_tokens']
    with httpx.Client(base_url=settings.lago_api_url,
                      headers={'Authorization': 'Bearer ' + settings.lago_api_key.get_secret_value()}, timeout=60) as client:
        lago_setup.wait_for_lago(client)
        metrics = [(spec, lago_setup.upsert_metric(client, spec, list(pricing['prices_per_million_tokens'])))
                   for spec in lago_setup.metric_specs(settings)]
        lago_setup.upsert_plan(client, pricing, metrics)
        for customer in lago_setup.TEST_CUSTOMERS:
            lago_setup.upsert_customer_and_subscription(client, customer, pricing)
    print('Llama lab billing configuration ready. Existing source and delivery history retained.')


if __name__ == '__main__':
    main()
