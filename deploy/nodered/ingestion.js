'use strict'
const crypto = require('node:crypto')
const mqtt = require('mqtt')
const identifier = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const allowed = new Set(['id', 'sensor_id', 'gateway_id', 'sensor_type', 'timestamp', 'value', 'unit', 'metadata', 'lon_lat', 'location_id'])
const TOPIC = 'normalized/v1/batch'

function invalid (message) { const error = new Error(message); error.status = 422; return error }
function validTime (s) {
  if (typeof s !== 'string') return false
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|[+-](\d{2}):(\d{2}))$/.exec(s)
  if (!m) return false
  const [, y, month, day, hour, minute, second, , zone, zh, zm] = m
  return +y >= 1970 && +y <= 9999 && +month >= 1 && +month <= 12 && +day >= 1 &&
    +day <= new Date(Date.UTC(+y, +month, 0)).getUTCDate() && +hour < 24 && +minute < 60 && +second < 60 &&
    (zone === 'Z' || (+zh < 24 && +zm < 60)) && Number.isFinite(Date.parse(s))
}
function validate (body) {
  function checkDepth (value, depth) {
    if (depth > 32) throw invalid('JSON nesting exceeds 32 levels')
    if (value && typeof value === 'object') for (const child of Object.values(value)) checkDepth(child, depth + 1)
  }
  checkDepth(body, 0)
  const values = Array.isArray(body) ? body : [body]
  if (values.length < 1 || values.length > 1000) throw invalid('batch must contain 1..1000 observations')
  const ids = new Set()
  for (const v of values) {
    if (!v || typeof v !== 'object' || Array.isArray(v)) throw invalid('observation must be an object')
    if (Object.keys(v).some(k => !allowed.has(k))) throw invalid('unknown observation field')
    if (typeof v.id !== 'string' || !uuid.test(v.id) || ids.has(v.id)) throw invalid('invalid or repeated observation ID')
    ids.add(v.id)
    for (const key of ['sensor_id', 'sensor_type']) if (typeof v[key] !== 'string' || !identifier.test(v[key])) throw invalid('invalid ' + key)
    if (v.gateway_id != null && (typeof v.gateway_id !== 'string' || (v.gateway_id !== '' && !identifier.test(v.gateway_id)))) throw invalid('invalid gateway_id')
    if (!validTime(v.timestamp)) throw invalid('timestamp must be valid RFC3339 with at most microsecond precision')
    if (typeof v.value !== 'number' || !Number.isFinite(v.value)) throw invalid('value must be a finite number')
    if (typeof v.unit !== 'string' || v.unit.trim() !== v.unit || !v.unit || Buffer.byteLength(v.unit) > 32) throw invalid('invalid unit')
    // Missing and null locations are equivalent; [0, 0] and location ID 0 are valid.
    if (v.lon_lat != null && (!Array.isArray(v.lon_lat) || v.lon_lat.length !== 2 ||
      !v.lon_lat.every(Number.isFinite) || Math.abs(v.lon_lat[0]) > 180 || Math.abs(v.lon_lat[1]) > 90)) throw invalid('lon_lat must contain longitude [-180,180] and latitude [-90,90]')
    if (v.location_id != null && (!Number.isSafeInteger(v.location_id) || v.location_id < 0)) throw invalid('location_id must be an integer between 0 and 9007199254740991')
    if (v.metadata != null && (typeof v.metadata !== 'object' || Array.isArray(v.metadata))) throw invalid('metadata must be an object')
    if (v.metadata && Object.keys(v.metadata).some(k => !k || Buffer.byteLength(k) > 128)) throw invalid('invalid metadata key')
    if (Buffer.byteLength(JSON.stringify(v)) > 16 * 1024) throw invalid('observation exceeds 16 KiB')
  }
  const encoded = JSON.stringify(body)
  if (Buffer.byteLength(encoded) > 1024 * 1024) { const err = invalid('document exceeds 1 MiB'); err.status = 413; throw err }
  return { encoded, ids: [...ids] }
}

function createIngestion ({ backend = 'api', apiKey, password, brokerURL = 'mqtt://mosquitto:1883', connect = mqtt.connect }) {
  if (!['api', 'mqtt'].includes(backend)) throw new Error('INGESTION_BACKEND must be api or mqtt')
  if (!apiKey || apiKey.length < 32) throw new Error('API key must contain at least 32 bytes')
  const hash = crypto.createHash('sha256').update(apiKey).digest()
  let client
  let pending = 0
  if (backend === 'mqtt') {
    client = connect(brokerURL, {
      clientId: 'node-red-publisher-v1', username: 'node-red', password,
      protocolVersion: 4, clean: true, reconnectPeriod: 1000, connectTimeout: 5000,
      queueQoSZero: false
    })
    // Connection errors are surfaced as 503 at submission time. Never print credentials.
    client.on('error', () => {})
  }
  return {
    backend,
    protectREST (req, res, next) {
      // Express HTTP In routes accept case variations and a trailing slash.
      if (req.method !== 'POST' || !/^\/api\/v1\/values\/?$/i.test(req.path)) return next()
      const supplied = crypto.createHash('sha256').update(req.headers['x-api-key'] || '').digest()
      if (!crypto.timingSafeEqual(supplied, hash)) return res.status(401).json({ error: { code: 'unauthorized', message: 'a valid X-API-Key header is required' } })
      next()
    },
    async publish (body) {
      const { encoded, ids } = validate(body)
      if (!client?.connected) { const err = new Error('MQTT broker unavailable; retry with the same IDs'); err.status = 503; throw err }
      if (pending >= 64) { const err = new Error('MQTT publisher busy; retry with the same IDs'); err.status = 503; throw err }
      // One MQTT message per submitted batch retains atomic batch boundaries in SQL.
      // Resolve only on PUBACK, not merely when the packet enters a client buffer.
      await new Promise((resolve, reject) => {
        pending++
        let settled = false
        let completed = false
        const timer = setTimeout(() => {
          settled = true
          const err = new Error('MQTT acknowledgment timed out; retry with the same IDs'); err.status = 503; reject(err)
        }, 5000)
        const done = err => {
          if (completed) return
          completed = true
          pending--
          if (settled) return
          settled = true
          clearTimeout(timer)
          if (err) { const failure = new Error('MQTT publish failed; retry with the same IDs'); failure.status = 503; reject(failure) } else resolve()
        }
        try { client.publish(TOPIC, encoded, { qos: 1, retain: false }, done) } catch (err) { done(err) }
      })
      return { status: 'accepted', backend: 'mqtt', ids }
    },
    close () { client?.end(true) }
  }
}
module.exports = { createIngestion, validate, TOPIC }
