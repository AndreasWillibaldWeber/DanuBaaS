'use strict'

const crypto = require('node:crypto')
const express = require('express')
const bcrypt = require('bcryptjs')

const COOKIE = '__Host-dbe-dashboard'

// A local session login is deliberately separate from Caddy Basic Auth and the
// Node-RED editor's adminAuth. Session loss on restart fails closed.
module.exports = function createAuth ({ username, passwordHash, origin, ttl = 8 * 60 * 60 * 1000, now = Date.now }) {
  if (!username || !passwordHash || !origin) throw new Error('dashboard authentication settings are required')
  const expectedOrigin = new URL(origin).origin
  const sessions = new Map()
  let attempts = []
  const router = express.Router()
  router.use((req, res, next) => {
    res.setHeader('Cache-Control', 'no-store')
    res.setHeader('X-Content-Type-Options', 'nosniff')
    next()
  })

  function cookieToken (req) {
    const matches = (req.headers.cookie || '').split(';').map(v => v.trim()).filter(v => v.startsWith(COOKIE + '='))
    if (matches.length !== 1) return null
    const value = matches[0].slice(COOKIE.length + 1)
    return /^[a-f0-9]{64}$/.test(value) ? value : null
  }
  function remove (token) {
    const session = sessions.get(token)
    if (session) for (const socket of session.sockets) socket.disconnect(true)
    sessions.delete(token)
  }
  function sessionFor (req) {
    const token = cookieToken(req)
    const session = sessions.get(token)
    if (!session) return null
    if (session.expires <= now()) { remove(token); return null }
    return session
  }
  function sameOrigin (req, res, next) {
    if (req.headers.origin !== expectedOrigin) return res.status(403).send('Invalid origin')
    next()
  }
  function requireSession (req, res, next) {
    if (!sessionFor(req)) return res.redirect(303, '/login')
    next()
  }
  const form = `<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Sensor dashboard login</title><body><main><h1>Sensor dashboard</h1><form method="post" action="/login"><p><label>Username <input name="username" autocomplete="username" required maxlength="128"></label></p><p><label>Password <input type="password" name="password" autocomplete="current-password" required maxlength="256"></label></p><button type="submit">Sign in</button></form></main></body></html>`
  router.get('/', (req, res) => res.redirect(303, '/dashboard'))
  router.get('/login', (req, res) => {
    if (sessionFor(req)) return res.redirect(303, '/dashboard')
    res.setHeader('Content-Security-Policy', "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
    res.type('html').send(form)
  })
  router.post('/login', sameOrigin, (req, res, next) => {
    // Global limit also bounds expensive password verification when the source is
    // a reverse proxy. Do not trust client-supplied forwarded-IP headers here.
    attempts = attempts.filter(t => t > now() - 60_000)
    if (attempts.length >= 30) { res.setHeader('Retry-After', '60'); return res.status(429).send('Try again later') }
    attempts.push(now())
    next()
  }, express.urlencoded({ extended: false, limit: '2kb', parameterLimit: 2 }), async (req, res, next) => {
    try {
      const name = req.body?.username
      const password = req.body?.password
      if (typeof name !== 'string' || typeof password !== 'string' || password.length > 256) return res.status(401).send('Invalid credentials')
      const valid = await bcrypt.compare(password, passwordHash)
      if (!valid || name !== username) return res.status(401).send('Invalid credentials')
      for (const [token, session] of sessions) if (session.expires <= now()) remove(token)
      if (sessions.size >= 128) return res.status(503).send('Session capacity reached')
      const previous = cookieToken(req)
      if (previous) remove(previous)
      const token = crypto.randomBytes(32).toString('hex')
      sessions.set(token, { expires: now() + ttl, sockets: new Set() })
      res.setHeader('Set-Cookie', `${COOKIE}=${token}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${Math.floor(ttl / 1000)}`)
      res.redirect(303, '/dashboard')
    } catch (err) { next(err) }
  })
  router.post('/logout', sameOrigin, (req, res) => {
    remove(cookieToken(req))
    res.setHeader('Set-Cookie', `${COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0`)
    res.redirect(303, '/login')
  })
  router.use((err, req, res, next) => {
    if (res.headersSent) return next(err)
    res.status(err.type === 'entity.too.large' ? 413 : 400).send('Invalid request')
  })

  return {
    middleware: router,
    requireSession,
    ioMiddleware: (socket, next) => {
      // Socket.IO middleware is essential: protecting just HTML leaves a data path open.
      const session = sessionFor(socket.request)
      if (!session || socket.request.headers.origin !== expectedOrigin) return next(new Error('unauthorized'))
      session.sockets.add(socket)
      const timeout = setTimeout(() => socket.disconnect(true), Math.max(1, session.expires - now()))
      timeout.unref()
      socket.once('disconnect', () => { clearTimeout(timeout); session.sockets.delete(socket) })
      next()
    }
  }
}
