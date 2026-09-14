'use strict'
const { test } = require('node:test')
const assert = require('node:assert/strict')
const { EventEmitter } = require('node:events')
const { createIngestion, validate, TOPIC } = require('../ingestion')
const value = () => ({ id: '12345678-1234-4234-8234-123456789001', sensor_id: 's1', gateway_id: 'demo', sensor_type: 'water-level', timestamp: '2026-09-14T10:00:00.123456Z', value: 0, unit: 'm', metadata: { raw: { rssi: -80, optional: null } } })
const key = 'a'.repeat(64)
function fake () {
  const client = new EventEmitter()
  client.connected = true
  client.end = () => {}
  client.published = []
  client.publish = (topic, body, options, callback) => client.published.push({ topic, body, options, callback })
  return client
}
test('API selection does not create an MQTT publisher; invalid selection fails', () => {
  createIngestion({ backend: 'api', apiKey: key, connect: () => assert.fail('unexpected MQTT connection') })
  assert.throws(() => createIngestion({ backend: 'typo', apiKey: key }), /INGESTION_BACKEND/)
})
test('a complete batch is one QoS 1 message; success waits for PUBACK', async () => {
  const client = fake()
  const service = createIngestion({ backend: 'mqtt', apiKey: key, connect: () => client })
  const batch = [value(), { ...value(), id: '12345678-1234-4234-8234-123456789002' }]
  let resolved = false
  const pending = service.publish(batch).then(result => { resolved = true; return result })
  await Promise.resolve()
  assert.equal(resolved, false)
  assert.equal(client.published.length, 1)
  const packet = client.published[0]
  assert.equal(packet.topic, TOPIC)
  assert.deepEqual(JSON.parse(packet.body), batch)
  assert.deepEqual(packet.options, { qos: 1, retain: false })
  packet.callback()
  assert.deepEqual(await pending, { status: 'accepted', backend: 'mqtt', ids: batch.map(v => v.id) })
})
test('disconnection and failed acknowledgments never report acceptance', async () => {
  const client = fake()
  const service = createIngestion({ backend: 'mqtt', apiKey: key, connect: () => client })
  client.connected = false
  await assert.rejects(service.publish(value()), err => err.status === 503)
  assert.equal(client.published.length, 0)
  client.connected = true
  const pending = service.publish(value())
  client.published[0].callback(new Error('internal transport detail'))
  await assert.rejects(pending, err => err.status === 503 && !err.message.includes('internal transport'))
})
test('bad data is rejected before any publish, including an invalid final item', async () => {
  const client = fake()
  const service = createIngestion({ backend: 'mqtt', apiKey: key, connect: () => client })
  for (const body of [null, [], [value(), {}], [value(), value()], { ...value(), value: null }, { ...value(), value: Infinity }, { ...value(), timestamp: '2026-02-30T12:00:00Z' }, { ...value(), timestamp: '2026-09-14T10:00:00.1234567Z' }, { ...value(), surprise: 1 }]) {
    await assert.rejects(service.publish(body), err => err.status === 422)
  }
  assert.equal(client.published.length, 0)
})
test('metadata, zero values, and microseconds survive validation', () => {
  assert.deepEqual(JSON.parse(validate(value()).encoded), value())
})
test('oversized observations, excessive depth, and invalid metadata keys are rejected', () => {
  const deep = {}; let cursor = deep
  for (let i = 0; i < 32; i++) { cursor.child = {}; cursor = cursor.child }
  for (const metadata of [{ raw: 'x'.repeat(16 * 1024) }, deep, { '': 1 }, { ['x'.repeat(129)]: 1 }]) {
    assert.throws(() => validate({ ...value(), metadata }), err => err.status === 422)
  }
})
test('missing PUBACK times out and pending packets stay bounded until acknowledged', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const client = fake()
  const service = createIngestion({ backend: 'mqtt', apiKey: key, connect: () => client })
  const pending = Array.from({ length: 64 }, () => assert.rejects(service.publish(value()), err => err.status === 503))
  await assert.rejects(service.publish(value()), /busy/)
  t.mock.timers.tick(5000)
  await Promise.all(pending)
  await assert.rejects(service.publish(value()), /busy/)
  assert.equal(client.published.length, 64)
  for (const packet of client.published) packet.callback()
  const recovered = service.publish(value())
  client.published[64].callback()
  assert.equal((await recovered).status, 'accepted')
  service.close()
})
test('Node-RED authenticates POST independently of the selected backend', () => {
  for (const backend of ['api', 'mqtt']) {
    const service = createIngestion({ backend, apiKey: key, connect: fake })
    for (const supplied of ['', 'wrong', key + ', ' + key]) {
      const res = { status (code) { assert.equal(code, 401); return this }, json (body) { assert.equal(body.error.code, 'unauthorized') } }
      service.protectREST({ method: 'POST', path: '/api/v1/values', headers: { 'x-api-key': supplied } }, res, () => assert.fail('unauthenticated request passed'))
    }
    let passed = false
    service.protectREST({ method: 'POST', path: '/api/v1/values', headers: { 'x-api-key': key } }, {}, () => { passed = true })
    assert.equal(passed, true)
    service.close()
  }
})
test('optional locations preserve coordinates, IDs, zero, bounds, and null independently', () => {
  for (const location of [{}, { lon_lat: null, location_id: null }, { lon_lat: [16.3738, 48.2082] },
    { location_id: 7 }, { lon_lat: [0, 0], location_id: 0 },
    { lon_lat: [-180, -90], location_id: Number.MAX_SAFE_INTEGER }, { lon_lat: [180, 90], location_id: null },
    { lon_lat: null, location_id: 7 }]) {
    const body = { ...value(), ...location }
    assert.deepEqual(JSON.parse(validate(body).encoded), body)
  }
})
test('invalid location anywhere in a batch prevents MQTT publication', async () => {
  const client = fake()
  const service = createIngestion({ backend: 'mqtt', apiKey: key, connect: () => client })
  for (const location of [
    ...[[], [1], [1, 2, 3], [null, 1], [1, null], ['1', 2], {}, false, [Infinity, 0], [NaN, 0],
      [180.01, 0], [-180.01, 0], [0, 90.01], [0, -90.01]].map(lon_lat => ({ lon_lat })),
    ...[-1, Number.MAX_SAFE_INTEGER + 1, 1.5, '7', true, [], {}, Infinity, NaN].map(location_id => ({ location_id }))
  ]) {
    const bad = { ...value(), ...location, id: '12345678-1234-4234-8234-123456789002' }
    for (const body of [bad, [value(), bad]]) await assert.rejects(service.publish(body), err => err.status === 422)
  }
  assert.equal(client.published.length, 0)
  service.close()
})
