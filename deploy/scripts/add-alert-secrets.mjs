#!/usr/bin/env node
// Upgrade without rotating secrets or enabling unsolicited external delivery.
import { writeFileSync } from 'node:fs'
import { randomBytes } from 'node:crypto'
import { fileURLToPath } from 'node:url'
const directory = fileURLToPath(new URL('../secrets/', import.meta.url))
for (const [name, value] of [['alert_admin_key', randomBytes(32).toString('hex') + '\n'], ['alert_webhook_url', '']]) {
  try {
    writeFileSync(directory + name, value, { mode: 0o444, flag: 'wx' })
    console.log('Created ' + name)
  } catch (err) { if (err.code !== 'EEXIST') throw err }
}
