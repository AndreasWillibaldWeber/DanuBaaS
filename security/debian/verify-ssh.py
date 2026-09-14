#!/usr/bin/env python3
"""Check installed SSH configuration, including the supplied connection context."""
import argparse
import ipaddress
from pathlib import Path
import subprocess
import sys
from render import load


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--client-address', required=True, type=ipaddress.ip_address)
    parser.add_argument('--client-hostname', default='unknown')
    args = parser.parse_args()
    config = load(args.config)
    if any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-' for c in args.client_hostname):
        parser.error('Supply a plain client hostname.')
    subprocess.run(['/usr/sbin/sshd', '-t'], check=True)
    for user in config['admin_users']:
        context = f'user={user},addr={args.client_address},host={args.client_hostname}'
        text = subprocess.check_output(['/usr/sbin/sshd', '-T', '-C', context], text=True)
        actual = {}
        for line in text.splitlines():
            key, _, value = line.partition(' ')
            actual.setdefault(key, []).append(value)
        expected = {'permitrootlogin': 'no', 'passwordauthentication': 'no',
                    'kbdinteractiveauthentication': 'no', 'pubkeyauthentication': 'yes',
                    'authenticationmethods': 'publickey', 'allowagentforwarding': 'no',
                    'x11forwarding': 'no', 'allowtcpforwarding': 'local', 'gatewayports': 'no',
                    'permitopen': '127.0.0.1:1880 localhost:1880', 'port': str(config['ssh_port']),
                    'allowusers': ' '.join(config['admin_users'])}
        for key, value in expected.items():
            if actual.get(key) != [value]:
                raise ValueError(f'Effective {key} for {user} differs from the intended policy; review Include/Match precedence.')
    print('SSH syntax and effective policies match for the supplied users/client context.')
    print('A NEW real public-key login and sudo test are still required before canceling rollback.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
