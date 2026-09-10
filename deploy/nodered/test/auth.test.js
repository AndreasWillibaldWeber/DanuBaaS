'use strict'
const { test } = require('node:test')
const assert = require('node:assert/strict')
const express = require('express')
const bcrypt = require('bcryptjs')
const { EventEmitter } = require('node:events')
const createAuth = require('../auth')
const origin = 'https://nodered.example.test'
const hash = bcrypt.hashSync('dashboard-test-password', 4)

async function fixture (t, options = {}) {
  const auth = createAuth({ username: 'operator', passwordHash: hash, origin, ...options })
  const app = express()
  app.use(auth.middleware)
  app.get('/dashboard', auth.requireSession, (req, res) => res.json({ secret: 'measurement' }))
  const server = app.listen(0, '127.0.0.1')
  await new Promise(resolve => server.once('listening', resolve))
  t.after(() => new Promise(resolve => server.close(resolve)))
  const base = `http://127.0.0.1:${server.address().port}`
  const call = (path, options = {}) => fetch(base + path, { redirect: 'manual', ...options })
  const login = (extra = {}) => call('/login', { method: 'POST', headers: { Origin: origin, 'Content-Type': 'application/x-www-form-urlencoded' }, body: 'username=operator&password=dashboard-test-password', ...extra })
  return { auth, call, login }
}
function fakeSocket (cookie, source = origin) {
  const s = new EventEmitter()
  s.request = { headers: { cookie, origin: source } }
  s.disconnect = () => { s.disconnected = true; s.emit('disconnect') }
  return s
}
test('Caddy credentials alone do not authenticate the application', async t => {
  const { call } = await fixture(t)
  const response = await call('/dashboard', { headers: { Authorization: 'Basic dXNlcjpwYXNz', 'X-Forwarded-User': 'operator' } })
  assert.equal(response.status, 303)
  assert.equal(response.headers.get('location'), '/login')
})
test('valid login creates secure session; logout revokes HTTP and sockets', async t => {
  const { call, login, auth } = await fixture(t)
  const response = await login()
  assert.equal(response.status, 303)
  const setCookie = response.headers.get('set-cookie')
  for (const flag of ['HttpOnly', 'Secure', 'SameSite=Strict', 'Path=/']) assert.ok(setCookie.includes(flag))
  const cookie = setCookie.split(';')[0]
  assert.equal((await call('/dashboard', { headers: { cookie } })).status, 200)
  const socket = fakeSocket(cookie)
  auth.ioMiddleware(socket, err => assert.equal(err, undefined))
  assert.equal((await call('/logout', { method: 'POST', headers: { cookie, origin } })).status, 303)
  assert.equal(socket.disconnected, true)
  assert.equal((await call('/dashboard', { headers: { cookie } })).status, 303)
  auth.ioMiddleware(fakeSocket(cookie), err => assert.ok(err))
})
test('invalid credentials and cross-origin login fail', async t => {
  const { login } = await fixture(t)
  assert.equal((await login({ body: 'username=operator&password=wrong' })).status, 401)
  assert.equal((await login({ headers: { Origin: 'https://evil.test', 'Content-Type': 'application/x-www-form-urlencoded' } })).status, 403)
})
test('session expiration, forged cookies, and websocket origins fail closed', async t => {
  let clock = 1000
  const { call, login, auth } = await fixture(t, { now: () => clock, ttl: 60000 })
  const response = await login()
  const cookie = response.headers.get('set-cookie').split(';')[0]
  auth.ioMiddleware(fakeSocket(cookie, 'https://evil.test'), err => assert.ok(err))
  auth.ioMiddleware(fakeSocket('__Host-dbe-dashboard=' + 'a'.repeat(64)), err => assert.ok(err))
  auth.ioMiddleware(fakeSocket(cookie + '; ' + cookie), err => assert.ok(err))
  clock += 60001
  assert.equal((await call('/dashboard', { headers: { cookie } })).status, 303)
  auth.ioMiddleware(fakeSocket(cookie), err => assert.ok(err))
})
test('login throttling bounds password checks', async t => {
  const { login } = await fixture(t)
  for (let i = 0; i < 30; i++) assert.equal((await login({ body: 'username=operator&password=wrong' })).status, 401)
  assert.equal((await login()).status, 429)
})
