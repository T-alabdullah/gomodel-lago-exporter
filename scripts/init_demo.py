"""Generate private local-demo configuration once; never overwrite existing secrets."""
import base64
import os
from pathlib import Path
import secrets
import subprocess


def main():
    target = Path('.env.demo')
    if target.exists():
        print('.env.demo already exists; retaining credentials.')
        return
    key = subprocess.check_output(['openssl', 'genrsa', '2048'], stderr=subprocess.DEVNULL)
    values = {name: secrets.token_hex(24) for name in (
        'GOMODEL_PASSWORD', 'EXPORTER_PASSWORD', 'LAGO_PASSWORD', 'READONLY_PASSWORD',
        'GOMODEL_MASTER_KEY', 'LAGO_ORG_API_KEY', 'LAGO_ORG_USER_PASSWORD',
        'LAGO_ENCRYPTION_PRIMARY_KEY', 'LAGO_ENCRYPTION_DETERMINISTIC_KEY',
        'LAGO_ENCRYPTION_KEY_DERIVATION_SALT')}
    values['SECRET_KEY_BASE'] = secrets.token_hex(64)
    values['LAGO_RSA_PRIVATE_KEY'] = base64.b64encode(key).decode()
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as out:
        out.write(''.join(f'{k}={v}\n' for k, v in values.items()))
    print('Created .env.demo (mode 600). Keep it with the persistent volumes.')


if __name__ == '__main__':
    main()
