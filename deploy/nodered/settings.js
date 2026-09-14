'use strict'
const fs = require('node:fs')
const createAuth = require('./auth')
const { createIngestion } = require('./ingestion')
function secret (name) {
  const file = process.env[name + '_FILE']
  if (!file) throw new Error(name + '_FILE is required')
  return fs.readFileSync(file, 'utf8').trimEnd()
}
const dashboardAuth = createAuth({
  username: process.env.DASHBOARD_USER || 'operator',
  passwordHash: secret('DASHBOARD_PASSWORD_HASH'),
  origin: process.env.DASHBOARD_ORIGIN
})
process.env.MQTT_NODE_RED_PASSWORD = secret("MQTT_NODE_RED_PASSWORD")
const ingestion = createIngestion({
  backend: process.env.INGESTION_BACKEND || 'api',
  apiKey: secret('API_KEY'),
  password: secret('MQTT_NODE_RED_PASSWORD'),
  brokerURL: process.env.MQTT_URL || 'mqtt://mosquitto:1883'
})
module.exports = {
  uiPort: 1880,
  apiMaxLength: "1mb",
  httpRequestTimeout: 15000,
  flowFile: '/data/flows.json',
  // Only scan node definitions; dependency HTML files are not editor nodes.
  nodesDir: '/opt/dbe/node_modules/@flowfuse/node-red-dashboard/nodes',
  flowFilePretty: true,
  httpAdminRoot: '/admin',
  credentialSecret: secret('NODE_RED_CREDENTIAL_SECRET'),
  adminAuth: {
    type: 'credentials',
    users: [{ username: process.env.NODE_RED_ADMIN_USER || 'admin', password: secret('NODE_RED_ADMIN_PASSWORD_HASH'), permissions: '*' }]
  },
  // Authenticate writes before either backend is selected. GET still uses the read API.
  httpNodeMiddleware: [ingestion.protectREST, dashboardAuth.middleware],
  dashboard: {
    middleware: dashboardAuth.requireSession,
    ioMiddleware: dashboardAuth.ioMiddleware,
    maxHttpBufferSize: 64 * 1024
  },
  functionGlobalContext: {
    ingestion,
    apiKey: secret('API_KEY'),
    mqttUser: 'node-red',
    mqttPassword: secret('MQTT_NODE_RED_PASSWORD')
  },
  functionExternalModules: false,
  externalModules: { autoInstall: false, palette: { allowInstall: false, allowUpload: false } },
  editorTheme: { projects: { enabled: false } },
  logging: { console: { level: 'info', metrics: false, audit: false } }
}
