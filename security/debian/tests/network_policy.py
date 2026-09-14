#!/usr/bin/env python3
"""Exercise real nftables/DNAT only in an isolated network namespace."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('render', ROOT / 'render.py')
render = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render)
children = []


def run(*args, input=None):
    return subprocess.run(args, input=input, text=True, check=True, capture_output=True).stdout


def child_namespace():
    child = subprocess.Popen(['unshare', '--net', 'sleep', '90'])
    children.append(child)
    for _ in range(100):
        if child.poll() is not None:
            raise RuntimeError('Could not create child network namespace')
        if os.readlink(f'/proc/{child.pid}/ns/net') != os.readlink('/proc/self/ns/net'):
            return child.pid
        time.sleep(.01)
    raise RuntimeError('Child namespace not ready')


def inside(pid, *args):
    return run('nsenter', '-t', str(pid), '-n', *args)


def ip(*args):
    return run('ip', *args)


SERVER = '''import selectors,socket,sys
sel=selectors.DefaultSelector()
for address,ports in ((sys.argv[1],sys.argv[2]),(sys.argv[3],sys.argv[2])):
 for port in ports.split(','):
  s=socket.socket(socket.AF_INET6 if ':' in address else socket.AF_INET,socket.SOCK_STREAM)
  s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
  if ':' in address:s.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
  s.bind((address,int(port)));s.listen();sel.register(s,selectors.EVENT_READ)
print('ready',flush=True)
while True:
 for key,_ in sel.select():
  c,_=key.fileobj.accept();c.sendall(b'ok');c.close()
'''
PROBE = '''import socket,sys
s=socket.socket(socket.AF_INET6 if ':' in sys.argv[1] else socket.AF_INET,socket.SOCK_STREAM)
s.settimeout(.4)
if sys.argv[3]!='-':s.bind((sys.argv[3],0))
try:
 s.connect((sys.argv[1],int(sys.argv[2])));sys.exit(0 if s.recv(2)==b'ok' else 2)
except OSError:sys.exit(1)
'''


def server(pid, addr4, addr6, ports):
    prefix = ['nsenter', '-t', str(pid), '-n'] if pid else []
    child = subprocess.Popen(prefix + [sys.executable, '-c', SERVER, addr4, ports, addr6],
                             stdout=subprocess.PIPE, text=True)
    children.append(child)
    if child.stdout.readline().strip() != 'ready':
        raise RuntimeError('Test listener failed')


def probe(pid, addr, port, allowed, source='-'):
    result = subprocess.run(['nsenter', '-t', str(pid), '-n', sys.executable, '-c', PROBE,
                             addr, str(port), source], capture_output=True)
    if (result.returncode == 0) != allowed:
        raise AssertionError(f'{addr}:{port} from {source}: expected allowed={allowed}, exit={result.returncode}')


def main():
    parent = os.environ.get('DANUBAAS_PARENT_NETNS', '')
    if os.geteuid() != 0 or not parent.startswith('net:[') or os.readlink('/proc/self/ns/net') == parent:
        raise SystemExit('Use tests/network_policy.sh to create and identify an isolated namespace.')
    # Refuse nonempty network namespaces, including an existing container network.
    links = json.loads(run('ip', '-j', 'link'))
    if any(link['ifname'] != 'lo' for link in links):
        raise SystemExit('Expected a NEW empty network namespace.')
    client, container = child_namespace(), child_namespace()
    ip('link', 'set', 'lo', 'up')
    ip('link', 'add', 'ens3', 'type', 'veth', 'peer', 'name', 'client0')
    ip('link', 'set', 'client0', 'netns', str(client))
    ip('link', 'add', 'br-test', 'type', 'bridge')
    ip('link', 'add', 'veth0', 'type', 'veth', 'peer', 'name', 'eth0')
    ip('link', 'set', 'eth0', 'netns', str(container))
    ip('link', 'set', 'veth0', 'master', 'br-test')
    for device, a4, a6 in [('ens3', '198.18.0.1/24', 'fd42:1::1/64'),
                           ('br-test', '172.30.0.1/24', 'fd42:2::1/64')]:
        ip('addr', 'add', a4, 'dev', device)
        ip('-6', 'addr', 'add', a6, 'dev', device, 'nodad')
        ip('link', 'set', device, 'up')
    ip('link', 'set', 'veth0', 'up')
    for pid, device, a4, a6, gateway4, gateway6 in [
        (client, 'client0', '198.18.0.2/24', 'fd42:1::2/64', '198.18.0.1', 'fd42:1::1'),
        (container, 'eth0', '172.30.0.2/24', 'fd42:2::2/64', '172.30.0.1', 'fd42:2::1')]:
        inside(pid, 'ip', 'link', 'set', 'lo', 'up')
        inside(pid, 'ip', 'addr', 'add', a4, 'dev', device)
        inside(pid, 'ip', '-6', 'addr', 'add', a6, 'dev', device, 'nodad')
        inside(pid, 'ip', 'link', 'set', device, 'up')
        inside(pid, 'ip', 'route', 'add', 'default', 'via', gateway4)
        inside(pid, 'ip', '-6', 'route', 'add', 'default', 'via', gateway6)
    inside(client, 'ip', 'addr', 'add', '198.18.0.3/24', 'dev', 'client0')
    inside(client, 'ip', '-6', 'addr', 'add', 'fd42:1::3/64', 'dev', 'client0', 'nodad')
    run('sysctl', '-w', 'net.ipv4.ip_forward=1', 'net.ipv6.conf.all.forwarding=1')
    server(None, '198.18.0.1', 'fd42:1::1', '22,8081')
    ports = '80,443,8883,1880,1883,3000,5432,8080'
    server(container, '172.30.0.2', 'fd42:2::2', ports)
    server(client, '198.18.0.2', 'fd42:1::2', '9090')
    config = json.loads((ROOT / 'host.example.json').read_text())
    config.update(admin_networks=['198.18.0.2/32', 'fd42:1::2/128'],
                  mqtt_networks=['198.18.0.2/32', 'fd42:1::2/128'])
    with tempfile.TemporaryDirectory(prefix='danubaas-network-') as temporary:
        file = Path(temporary) / 'rules.nft'
        file.write_text(render.firewall(config))
        nat = '''table inet docker_test {
 chain forward { type filter hook forward priority 0; policy accept; }
 chain prerouting { type nat hook prerouting priority dstnat; policy accept;
'''
        for port in ports.split(','):
            nat += f'ip daddr 198.18.0.1 tcp dport {port} dnat ip to 172.30.0.2:{port}\n'
            nat += f'ip6 daddr fd42:1::1 tcp dport {port} dnat ip6 to [fd42:2::2]:{port}\n'
        run('nft', '-f', '-', input=nat + '}\n}\n')
        run('nft', '--check', '-f', str(file))
        for _ in range(2):
            run('nft', '-f', str(file))
        run('nft', 'list', 'table', 'inet', 'docker_test')
        for host, target, trusted, untrusted in [
            ('198.18.0.1', '172.30.0.2', '198.18.0.2', '198.18.0.3'),
            ('fd42:1::1', 'fd42:2::2', 'fd42:1::2', 'fd42:1::3')]:
            probe(client, host, 22, True, trusted)
            probe(client, host, 22, False, untrusted)
            for port in (80, 443, 8883):
                probe(client, host, port, True, trusted)
            probe(client, host, 8883, False, untrusted)
            for port in (1880, 1883, 3000, 5432, 8080, 8081):
                probe(client, host, port, False, trusted)
            probe(client, target, 80, False, trusted)
            probe(container, trusted, 9090, True)
        # Restore only our table; the simulated Docker table must survive.
        previous = run('nft', 'list', 'table', 'inet', 'danubaas_host')
        run('nft', '-f', '-', input='add table inet danubaas_host\ndelete table inet danubaas_host\n' + previous)
        run('nft', 'list', 'table', 'inet', 'docker_test')
        probe(client, '198.18.0.1', 443, True)
    print('PASS: IPv4/IPv6 SSH sources, published ports, MQTT sources, blocked/direct ports, egress, reload and scoped restoration.')


if __name__ == '__main__':
    try:
        main()
    finally:
        for child in reversed(children):
            child.terminate()
        for child in reversed(children):
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
