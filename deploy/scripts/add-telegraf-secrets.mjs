#!/usr/bin/env node
// Safe upgrade for an existing deployment: add missing secrets, never rotate old ones.
import { mkdirSync, writeFileSync } from 'node:fs'
import { randomBytes } from 'node:crypto'
import { fileURLToPath } from 'node:url'
const directory = fileURLToPath(new URL('../secrets/', import.meta.url))
mkdirSync(directory, { recursive: true, mode: 0o700 })
for (const name of ['db_telegraf_password', 'mqtt_telegraf_password']) {
  try {
    writeFileSync(directory + name, randomBytes(32).toString('hex') + '\n', { mode: 0o444, flag: 'wx' })
    console.log('Created ' + name)
  } catch (err) { if (err.code !== 'EEXIST') throw err; console.log('Preserved ' + name) }
}
