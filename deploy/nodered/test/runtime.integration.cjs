'use strict'
// Real Node-RED + FlowFuse HTTP/Socket.IO integration, using a controlled API peer.
// NODE_RED_BIN points at the installed node-red/red.js; no Docker daemon is needed.
const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const http = require('node:http')
const net = require('node:net')
const { spawn } = require('node:child_process')
const bcrypt = require('bcryptjs')
const WebSocket = require('ws')
const runtime = path.resolve(__dirname, '..')

async function listen (server) {
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
  return server.address().port
}
function socketOutcome (port, cookie, origin) {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(`ws://127.0.0.1:${port}/dashboard/socket.io/?EIO=4&transport=websocket`, { headers: { Origin: origin, Cookie: cookie } })
    const timer = setTimeout(() => { socket.terminate(); reject(new Error('Socket.IO handshake timeout')) }, 3000)
    socket.on('error', err => { clearTimeout(timer); reject(err) })
    socket.on('message', data => {
      const message = data.toString()
      if (message.startsWith('0')) socket.send('40')
      if (message.startsWith('40') || message.startsWith('44')) { clearTimeout(timer); socket.close(); resolve(message.startsWith('40')) }
    })
  })
}

for (const backend of ['api', 'mqtt']) test(`actual Node-RED flows and dashboard preserve boundaries (${backend})`, { timeout: 30000 }, async t => {
  assert.ok(process.env.NODE_RED_BIN, 'set NODE_RED_BIN to node-red/red.js')
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dbe-runtime-'))
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  const apiKey = 'a'.repeat(64)
  let unavailable = false
  let apiPosts = 0
  const peer = http.createServer((req, res) => {
    if (unavailable) { req.socket.destroy(); return }
    res.setHeader('Content-Type', 'application/json')
    if (req.headers['x-api-key'] !== apiKey) { res.writeHead(401); res.end('{"error":{"code":"unauthorized"}}'); return }
    if (req.method === 'GET') { res.end('[]'); return }
    apiPosts++
    let body = ''
    req.on('data', b => { body += b })
    req.on('end', () => { res.writeHead(201); res.end(body) })
  })
  const apiPort = await listen(peer)
  t.after(() => new Promise(resolve => peer.close(resolve)))
  const probe = http.createServer()
  const port = await listen(probe)
  await new Promise(resolve => probe.close(resolve))
  const mqttIdentities = []
  const published = []
  const mqttSockets = new Set()
  const broker = net.createServer(socket => {
    mqttSockets.add(socket)
    socket.on('close', () => mqttSockets.delete(socket))
    let buffered = Buffer.alloc(0)
    socket.on('data', chunk => {
      buffered = Buffer.concat([buffered, chunk])
      while (buffered.length > 1) {
        let cursor = 1; let remaining = 0; let multiplier = 1
        while (true) {
          if (cursor >= buffered.length) return
          const digit = buffered[cursor++]
          remaining += (digit & 127) * multiplier
          if (!(digit & 128)) break
          multiplier *= 128
        }
        if (buffered.length < cursor + remaining) return
        const packet = buffered.subarray(0, cursor + remaining)
        buffered = buffered.subarray(cursor + remaining)
        function string () {
          const length = packet.readUInt16BE(cursor); cursor += 2
          const result = packet.subarray(cursor, cursor + length).toString(); cursor += length
          return result
        }
        const type = packet[0] >> 4
        if (type === 1) {
          assert.equal(string(), 'MQTT')
          assert.equal(packet[cursor++], 4)
          const flags = packet[cursor++]; cursor += 2
          string()
          assert.equal(flags & 4, 0)
          mqttIdentities.push({ user: flags & 128 ? string() : '', password: flags & 64 ? string() : '' })
          socket.write(Buffer.from([0x20, 0x02, 0x00, 0x00]))
        } else if (type === 8) {
          socket.write(Buffer.from([0x90, 0x03, packet[cursor], packet[cursor + 1], 0x01]))
        } else if (type === 3) {
          const topic = string()
          const qos = (packet[0] >> 1) & 3
          assert.equal(qos, 1)
          const msb = packet[cursor++]; const lsb = packet[cursor++]
          published.push({ topic, body: JSON.parse(packet.subarray(cursor).toString()) })
          socket.write(Buffer.from([0x40, 0x02, msb, lsb]))
        } else if (type === 12) socket.write(Buffer.from([0xd0, 0]))
      }
    })
  })
  const mqttPort = await listen(broker)
  t.after(() => { for (const socket of mqttSockets) socket.destroy(); broker.close() })
  const origin = `http://127.0.0.1:${port}`
  const env = { ...process.env, NODE_PATH: path.join(runtime, 'node_modules'), DASHBOARD_ORIGIN: origin, INGESTION_BACKEND: backend, MQTT_URL: `mqtt://127.0.0.1:${mqttPort}` }
  for (const [name, value] of Object.entries({
    API_KEY: apiKey,
    DASHBOARD_PASSWORD_HASH: bcrypt.hashSync('dashboard-password', 4),
    NODE_RED_ADMIN_PASSWORD_HASH: bcrypt.hashSync('editor-password', 4),
    NODE_RED_CREDENTIAL_SECRET: 'b'.repeat(64),
    MQTT_NODE_RED_PASSWORD: 'mqtt-test-password'
  })) {
    const file = path.join(dir, name)
    fs.writeFileSync(file, value, { mode: 0o600 })
    env[name + '_FILE'] = file
  }
  const flows = JSON.parse(fs.readFileSync(path.join(runtime, 'flows.json')))
  for (const node of flows) {
    if (node.func) node.func = node.func.replaceAll('http://api:8080', `http://127.0.0.1:${apiPort}`)
    if (node.type === 'mqtt-broker') { node.broker = '127.0.0.1'; node.port = String(mqttPort) }
  }
  fs.writeFileSync(path.join(dir, 'flows.json'), JSON.stringify(flows))
  fs.copyFileSync(path.join(runtime, 'flows_cred.json'), path.join(dir, 'flows_cred.json'))
  const settings = `const s=require(${JSON.stringify(path.join(runtime, 'settings.js'))});s.flowFile=${JSON.stringify(path.join(dir, 'flows.json'))};s.uiPort=${port};s.uiHost='127.0.0.1';s.nodesDir=${JSON.stringify(path.join(runtime, 'node_modules'))};module.exports=s;`
  fs.writeFileSync(path.join(dir, 'settings.js'), settings)
  const child = spawn(process.execPath, [process.env.NODE_RED_BIN, '--userDir', dir, '--settings', path.join(dir, 'settings.js')], { env, stdio: ['ignore', 'pipe', 'pipe'] })
  let logs = ''
  child.stdout.on('data', b => { logs += b.toString() })
  child.stderr.on('data', b => { logs += b.toString() })
  t.after(async () => {
    if (child.exitCode === null) {
      child.kill('SIGTERM')
      await new Promise(resolve => {
        const kill = setTimeout(() => child.kill('SIGKILL'), 3000)
        child.once('exit', () => { clearTimeout(kill); resolve() })
      })
    }
  })
  const call = (url, options = {}) => fetch(origin + url, { redirect: 'manual', ...options })
  let ready = false
  for (let i = 0; i < 100; i++) {
    if (child.exitCode !== null) assert.fail('Node-RED exited: ' + logs)
    try {
      const r = await call('/api/v1/values')
      if (r.status === 401) { ready = true; break }
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 100))
  }
  assert.ok(ready, 'Node-RED did not become ready: ' + logs)
  assert.ok(!logs.includes('missing node types'), logs)
  const clients = backend === 'mqtt' ? 2 : 1
  for (let i = 0; i < 20 && mqttIdentities.length < clients; i++) await new Promise(resolve => setTimeout(resolve, 50))
  assert.equal(mqttIdentities.length, clients)
  for (const identity of mqttIdentities) assert.deepEqual(identity, { user: 'node-red', password: 'mqtt-test-password' })
  assert.equal((await call('/dashboard')).status, 303)
  assert.equal((await call('/dashboard/_setup')).status, 303)
  assert.equal(await socketOutcome(port, '', origin), false)
  const login = await call('/login', { method: 'POST', headers: { Origin: origin, 'Content-Type': 'application/x-www-form-urlencoded' }, body: 'username=operator&password=dashboard-password' })
  assert.equal(login.status, 303)
  const cookie = login.headers.get('set-cookie').split(';')[0]
  assert.equal((await call('/dashboard/_setup', { headers: { Cookie: cookie } })).status, 200)
  assert.equal(await socketOutcome(port, cookie, origin), true)
  assert.equal(await socketOutcome(port, cookie, 'https://evil.test'), false)
  assert.equal((await call('/api/v1/values', { headers: { 'X-API-Key': apiKey } })).status, 200)
  const observation = { id: '12345678-1234-4234-8234-123456789001', sensor_id: 's1', gateway_id: 'demo', sensor_type: 'water-level', timestamp: '2026-09-14T10:00:00.123456Z', value: 0, unit: 'm', metadata: { nested: [null, 3] } }
  const write = body => call('/api/v1/values', { method: 'POST', headers: { 'X-API-Key': apiKey, 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  assert.equal((await write(observation)).status, backend === 'mqtt' ? 202 : 201)
  const batch = [observation, { ...observation, id: '12345678-1234-4234-8234-123456789002' }]
  assert.equal((await write(batch)).status, backend === 'mqtt' ? 202 : 201)
  assert.equal((await call('/api/v1/values', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(observation) })).status, 401)
  for (const url of ['/api/v1/values/', '/API/V1/VALUES']) {
    assert.equal((await call(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(observation) })).status, 401)
  }
  if (backend === 'mqtt') {
    assert.equal(apiPosts, 0)
    assert.deepEqual(published, [{ topic: 'normalized/v1/batch', body: observation }, { topic: 'normalized/v1/batch', body: batch }])
    assert.equal((await write([observation, {}])).status, 422)
    assert.equal(published.length, 2)
  } else { assert.equal(apiPosts, 2); assert.equal(published.length, 0) }
  assert.equal((await call('/admin/flows')).status, 401)
  unavailable = true
  const failed = await call('/api/v1/values', { headers: { 'X-API-Key': apiKey } })
  assert.equal(failed.status, 503)
  assert.equal((await failed.json()).error.code, 'storage_unavailable')
  if (backend === 'mqtt') assert.equal((await write(observation)).status, 202)
})
