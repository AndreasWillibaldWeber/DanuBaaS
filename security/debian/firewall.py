#!/usr/bin/env python3
"""Try, confirm or roll back the DanuBaaS nftables policy on a Debian server."""
import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys

STATE = Path('/run/danubaas-security')
PERSIST = Path('/etc/danubaas-security')
UNIT = 'danubaas-firewall-rollback'
SERVICE = Path('/etc/systemd/system/danubaas-firewall.service')
NFT = '/usr/sbin/nft'
PREFIX = 'add table inet danubaas_host\ndelete table inet danubaas_host\n'


def run(*args, capture=False):
    return subprocess.run(args, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def private_directory(path):
    if path.is_symlink():
        raise ValueError('Refusing a symlink at ' + str(path))
    path.mkdir(mode=0o700, exist_ok=True)
    stat = path.stat()
    if stat.st_uid != 0 or stat.st_mode & 0o077:
        raise ValueError('Expected a root-owned private directory: ' + str(path))


def write(path, value, mode=0o600):
    temporary = path.with_name(path.name + '.new')
    # Parent is private, or /etc/systemd/system owned by root.
    with temporary.open('w') as out:
        out.write(value)
    temporary.chmod(mode)
    temporary.replace(path)


def snapshot():
    tables = json.loads(run(NFT, '-j', 'list', 'tables', capture=True).stdout)
    present = any(item.get('table', {}).get('family') == 'inet'
                  and item.get('table', {}).get('name') == 'danubaas_host'
                  for item in tables['nftables'])
    previous = run(NFT, 'list', 'table', 'inet', 'danubaas_host', capture=True).stdout if present else ''
    return PREFIX + previous


def pending():
    return (STATE / 'pending').is_file()


def restore():
    if pending():
        run(NFT, '-f', str(STATE / 'previous.nft'))
        (STATE / 'pending').unlink()
        print('Previous DanuBaaS runtime rules restored. Other firewall tables were untouched.')


def try_policy(config_path, console_confirmed):
    from render import artifacts, load
    if not console_confirmed:
        raise ValueError('Pass --console-access-confirmed only after arranging provider/local console recovery.')
    if pending():
        raise ValueError('A trial is pending. Confirm or roll it back before trying another.')
    config = load(config_path)
    for interface in config['external_interfaces']:
        if not (Path('/sys/class/net') / interface).exists():
            raise ValueError('Interface does not exist: ' + interface)
    documentation = [ipaddress.ip_network(v) for v in ('192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24', '2001:db8::/32')]
    for value in config['admin_networks']:
        network = ipaddress.ip_network(value)
        if any(network.version == doc.version and network.subnet_of(doc) for doc in documentation):
            raise ValueError('Replace documentation/example administrator addresses before applying.')
    connection = os.environ.get('SSH_CONNECTION', '').split()
    if connection:
        if len(connection) != 4:
            raise ValueError('Malformed SSH_CONNECTION.')
        client = ipaddress.ip_address(connection[0])
        if int(connection[3]) != config['ssh_port'] or not any(client in ipaddress.ip_network(n) for n in config['admin_networks']):
            raise ValueError('Current SSH connection is not allowed by this policy.')
    for manager in ('ufw.service', 'firewalld.service', 'nftables.service'):
        if subprocess.run(['systemctl', 'is-active', '--quiet', manager]).returncode == 0:
            raise ValueError('Existing firewall manager active: ' + manager + '; reconcile ownership first.')
    files = artifacts(config)
    write(STATE / 'candidate.nft', files['firewall.nft'])
    write(STATE / 'candidate.service', files['danubaas-firewall.service'])
    run(NFT, '--check', '-f', str(STATE / 'candidate.nft'))
    write(STATE / 'previous.nft', snapshot())
    write(STATE / 'controller.py', Path(__file__).read_text(), 0o700)
    write(STATE / 'pending', 'trial\n')
    try:
        run('systemd-run', '--quiet', '--collect', '--unit=' + UNIT, '--on-active=300s', '--timer-property=AccuracySec=1s',
            '/usr/bin/python3', str(STATE / 'controller.py'), 'rollback')
        run(NFT, '-f', str(STATE / 'candidate.nft'))
    except Exception:
        restore()
        raise
    print('Trial active for five minutes. Open a NEW SSH connection and test application access.')
    print('Then run sudo python3 security/debian/firewall.py confirm; otherwise rollback is automatic.')
    print('Do not reboot during the trial. Boot configuration is unchanged until confirmation.')


def confirm():
    if not pending():
        raise ValueError('No pending firewall trial.')
    # Install persistence only after the administrator has tested a new connection.
    private_directory(PERSIST)
    old_rules = (PERSIST / 'firewall.nft').read_text() if (PERSIST / 'firewall.nft').exists() else None
    old_service = SERVICE.read_text() if SERVICE.exists() else None
    enabled = subprocess.run(['systemctl', 'is-enabled', '--quiet', 'danubaas-firewall.service']).returncode == 0
    if old_rules is not None:
        write(PERSIST / 'firewall.nft.previous', old_rules)
    if old_service is not None:
        write(PERSIST / 'danubaas-firewall.service.previous', old_service)
    try:
        write(PERSIST / 'firewall.nft', (STATE / 'candidate.nft').read_text())
        write(SERVICE, (STATE / 'candidate.service').read_text(), 0o644)
        run('systemctl', 'daemon-reload')
        run('systemctl', 'enable', 'danubaas-firewall.service')
    except Exception:
        for path, content, mode in [(PERSIST / 'firewall.nft', old_rules, 0o600), (SERVICE, old_service, 0o644)]:
            if content is None:
                path.unlink(missing_ok=True)
            else:
                write(path, content, mode)
        if not enabled:
            subprocess.run(['systemctl', 'disable', 'danubaas-firewall.service'], check=False)
        subprocess.run(['systemctl', 'daemon-reload'], check=False)
        restore()
        raise
    # A rollback process may already be waiting on our lock. Removing this marker
    # makes it a no-op before stopping its timer; do not deadlock by stopping it.
    (STATE / 'pending').unlink()
    subprocess.run(['systemctl', 'stop', UNIT + '.timer'], check=False)
    print('Firewall confirmed and enabled for boot. Previous persisted files, if any, were backed up.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    trial = commands.add_parser('try')
    trial.add_argument('--config', required=True, type=Path)
    trial.add_argument('--console-access-confirmed', action='store_true')
    commands.add_parser('confirm')
    commands.add_parser('rollback')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.exit(1, 'Run this host-administration command as root. Rendering needs no root.\n')
    os.environ['PATH'] = '/usr/sbin:/usr/bin:/sbin:/bin'
    try:
        release = Path('/etc/os-release').read_text()
        if 'ID=debian\n' not in release and 'ID="debian"\n' not in release:
            raise ValueError('This installer is intended for Debian; use the templates for other systems.')
        private_directory(STATE)
        with (STATE / 'lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if args.command == 'try':
                try_policy(args.config, args.console_access_confirmed)
            elif args.command == 'confirm':
                confirm()
            else:
                restore()
                subprocess.run(['systemctl', 'stop', UNIT + '.timer'], check=False)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, 'Firewall operation failed: ' + str(error) + '\n')


if __name__ == '__main__':
    main()
