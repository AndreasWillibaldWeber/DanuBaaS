#!/usr/bin/env node
// Generate independent demo credentials without printing them or overwriting a deployment.
import { mkdirSync, writeFileSync, existsSync, chmodSync } from 'node:fs'
import { randomBytes } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { resolve, dirname } from 'node:path'
import { execFileSync } from 'node:child_process'
import { createRequire } from 'node:module'
const require = createRequire(import.meta.url)
const bcrypt = require('../nodered/node_modules/bcryptjs')
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const secrets = resolve(root, 'secrets')
if (existsSync(secrets)) throw new Error('secrets/ already exists; setup never overwrites credentials')
if (existsSync(resolve(root, 'certs'))) throw new Error('certs/ already exists; setup never overwrites certificates')
execFileSync('openssl', ['version'], { stdio: 'ignore' })
mkdirSync(secrets, { mode: 0o700 })
const password = () => randomBytes(32).toString('hex')
const credentials = {}
function save (name, value) { writeFileSync(resolve(secrets, name), value + '\n', { mode: 0o444, flag: 'wx' }) }
for (const name of ['db_telegraf_password', 'mqtt_telegraf_password', 'db_admin_password', 'db_api_password', 'db_grafana_password', 'api_key', 'mqtt_node_red_password', 'mqtt_demo_password', 'node_red_credential_secret', 'grafana_admin_password']) {
  const value = password(); save(name, value)
  if (['api_key', 'mqtt_demo_password', 'grafana_admin_password'].includes(name)) credentials[name] = value
}
for (const name of ['caddy', 'dashboard', 'node_red_admin']) {
  const value = password(); save(name + '_password_hash', bcrypt.hashSync(value, 12)); credentials[name + '_password'] = value
}
writeFileSync(resolve(secrets, 'operator-credentials.json'), JSON.stringify(credentials, null, 2) + '\n', { mode: 0o600, flag: 'wx' })
const certRoot = resolve(root, 'certs')
const certDir = resolve(certRoot, 'mqtt')
mkdirSync(certRoot, { mode: 0o700 }); mkdirSync(certDir, { mode: 0o755 })
const host = process.env.MQTT_HOST || 'mqtt.localhost'
if (!/^[a-zA-Z0-9.-]+$/.test(host)) throw new Error('MQTT_HOST must be a DNS name')
const openssl = args => execFileSync('openssl', args, { stdio: 'ignore' })
openssl(['req', '-x509', '-newkey', 'rsa:3072', '-nodes', '-keyout', resolve(certRoot, 'ca.key'), '-out', resolve(certRoot, 'ca.crt'), '-days', '365', '-subj', '/CN=DBE demo MQTT CA'])
openssl(['req', '-new', '-newkey', 'rsa:3072', '-nodes', '-keyout', resolve(certDir, 'server.key'), '-out', resolve(certRoot, 'server.csr'), '-subj', `/CN=${host}`])
writeFileSync(resolve(certRoot, 'server.ext'), `subjectAltName=DNS:${host},DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n`)
openssl(['x509', '-req', '-in', resolve(certRoot, 'server.csr'), '-CA', resolve(certRoot, 'ca.crt'), '-CAkey', resolve(certRoot, 'ca.key'), '-CAcreateserial', '-out', resolve(certDir, 'server.crt'), '-days', '90', '-extfile', resolve(certRoot, 'server.ext')])
chmodSync(resolve(certRoot, 'ca.key'), 0o600)
chmodSync(resolve(certDir, 'server.key'), 0o444)
// Parent directories remain private on the host; container-mounted files must be
// readable by distinct non-root service UIDs (local Compose secrets use bind mounts).
console.log('Created independent credentials in deploy/secrets/operator-credentials.json and a 90-day demo MQTT certificate. No values were printed.')
