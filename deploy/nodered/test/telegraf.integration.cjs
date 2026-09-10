'use strict'
// Actual Mosquitto -> Telegraf -> PostgreSQL COPY, with the production publisher
// and Telegraf configuration. Run database migrations first in a disposable DB.
const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const net = require('node:net')
const crypto = require('node:crypto')
const { spawn, spawnSync } = require('node:child_process')
const mqtt = require('mqtt')
const { createIngestion, TOPIC } = require('../ingestion')

test('real Telegraf stores batches, replays safely and quarantines conflicts', { timeout: 45000 }, async t => {
  for (const key of ['TEST_DATABASE_URL', 'MOSQUITTO_BIN', 'TELEGRAF_BIN', 'PSQL_BIN']) assert.ok(process.env[key], `set ${key}`)
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dbe-telegraf-'))
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  const admin = new URL(process.env.TEST_DATABASE_URL)
  function sql (query) {
    const result = spawnSync(process.env.PSQL_BIN, ['-X', '-qAt', '-v', 'ON_ERROR_STOP=1'], {
      env: { ...process.env, PGHOST: admin.hostname, PGPORT: admin.port || '5432', PGDATABASE: admin.pathname.slice(1), PGUSER: decodeURIComponent(admin.username), PGPASSWORD: decodeURIComponent(admin.password), PGSSLMODE: admin.searchParams.get('sslmode') || 'require' },
      input: query, encoding: 'utf8', timeout: 5000
    })
    assert.equal(result.status, 0, result.stderr)
    return result.stdout.trim()
  }
  const suffix = crypto.randomBytes(6).toString('hex')
  const role = 'telegraf_test_' + suffix
  const password = crypto.randomBytes(32).toString('hex')
  sql(`CREATE ROLE ${role} LOGIN PASSWORD '${password}'; GRANT USAGE ON SCHEMA sensor TO ${role}; GRANT INSERT ON sensor.mqtt_ingest TO ${role};`)
  t.after(() => sql(`REVOKE INSERT ON sensor.mqtt_ingest FROM ${role}; REVOKE USAGE ON SCHEMA sensor FROM ${role}; DROP ROLE ${role};`))
  const probe = net.createServer()
  await new Promise(resolve => probe.listen(0, '127.0.0.1', resolve))
  const port = probe.address().port
  await new Promise(resolve => probe.close(resolve))
  fs.writeFileSync(path.join(dir, 'mosquitto.conf'), `listener ${port} 127.0.0.1\nallow_anonymous true\n`)
  const children = []
  t.after(async () => {
    for (const child of children.reverse()) {
      if (child.exitCode !== null) continue
      child.kill('SIGTERM')
      await new Promise(resolve => {
        const timer = setTimeout(() => child.kill('SIGKILL'), 3000)
        child.once('exit', () => { clearTimeout(timer); resolve() })
      })
    }
  })
  function start (binary, args, env = process.env) {
    const child = spawn(binary, args, { env, stdio: ['ignore', 'pipe', 'pipe'] })
    children.push(child)
    child.logs = ''
    child.stdout.on('data', data => { child.logs += data })
    child.stderr.on('data', data => { child.logs += data })
    return child
  }
  start(process.env.MOSQUITTO_BIN, ['-c', path.join(dir, 'mosquitto.conf'), '-v'])
  const brokerURL = `mqtt://127.0.0.1:${port}`
  const control = mqtt.connect(brokerURL, { connectTimeout: 3000 })
  t.after(() => control.end(true))
  await new Promise((resolve, reject) => { control.once('connect', resolve); control.once('error', reject) })
  let config = fs.readFileSync(path.join(__dirname, '../../telegraf/telegraf.conf'), 'utf8')
  config = config.replace('tcp://mosquitto:1883', `tcp://127.0.0.1:${port}`)
    .replace('telegraf-timescale-v1', 'telegraf-test-' + suffix)
    .replace('host=timescaledb user=telegraf_writer dbname=sensors sslmode=disable',
      `host=${admin.hostname} port=${admin.port || 5432} user=${role} dbname=${admin.pathname.slice(1)} sslmode=${admin.searchParams.get('sslmode') || 'require'}`)
  fs.writeFileSync(path.join(dir, 'telegraf.conf'), config)
  const telegraf = start(process.env.TELEGRAF_BIN, ['--config', path.join(dir, 'telegraf.conf')], { ...process.env, MQTT_TELEGRAF_PASSWORD: 'test', PGPASSWORD: password })
  async function eventually (check, message) {
    for (let i = 0; i < 100; i++) {
      assert.equal(telegraf.exitCode, null, telegraf.logs)
      if (check()) return
      await new Promise(resolve => setTimeout(resolve, 100))
    }
    assert.fail(message + '\n' + telegraf.logs)
  }
  // Mosquitto logs SUBSCRIBE only after the real Telegraf consumer is connected.
  await eventually(() => children[0].logs.includes('normalized/v1/batch'), 'Telegraf did not subscribe')
  const publisher = createIngestion({ backend: 'mqtt', apiKey: 'a'.repeat(64), brokerURL, password: 'test' })
  t.after(() => publisher.close())
  const observation = () => ({ id: crypto.randomUUID(), sensor_id: 'telegraf-quality', sensor_type: 'water-level', timestamp: '2026-09-14T10:00:00.123456Z', value: 0, unit: 'm', metadata: { raw: { nested: [null, 1], rssi: -70 } } })
  const a = observation(); const b = observation()
  // Connection establishment is asynchronous; only retry broker-unavailable errors.
  for (let i = 0; ; i++) {
    try { assert.equal((await publisher.publish([a, b])).status, 'accepted'); break } catch (err) {
      if (i >= 30 || !err.message.includes('unavailable')) throw err
      await new Promise(resolve => setTimeout(resolve, 100))
    }
  }
  const count = () => sql(`SELECT count(*) FROM sensor.measurements WHERE id IN ('${a.id}','${b.id}');`)
  await eventually(() => count() === '2', 'Telegraf did not commit the complete batch')
  assert.deepEqual(JSON.parse(sql(`SELECT payload->'metadata' FROM sensor.events WHERE id='${a.id}';`)), a.metadata)
  await publisher.publish([a, b])
  const fresh = observation()
  await publisher.publish([fresh, { ...a, value: 2 }])
  await eventually(() => sql(`SELECT count(*) FROM sensor.rejected_messages WHERE payload LIKE '%${fresh.id}%';`) === '1', 'conflicting batch not quarantined')
  assert.equal(sql(`SELECT count(*) FROM sensor.events WHERE id='${fresh.id}';`), '0')
  assert.equal(count(), '2')
  // A poison message bypassing Node-RED cannot stall later valid documents.
  await new Promise((resolve, reject) => control.publish(TOPIC, '{broken-' + suffix, { qos: 1 }, err => err ? reject(err) : resolve()))
  const c = observation()
  await publisher.publish(c)
  await eventually(() => sql(`SELECT count(*) FROM sensor.events WHERE id='${c.id}';`) === '1', 'poison message stalled ingestion')
  assert.equal(sql('SELECT count(*) FROM sensor.mqtt_ingest;'), '0')
  assert.ok(!telegraf.logs.includes('E!'), telegraf.logs)
})
