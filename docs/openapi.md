# OpenAPI specification

[Download the OpenAPI 3 document](openapi.yaml) or
[view its source on GitHub](https://github.com/AndreasWillibaldWeber/DanuBaaS/blob/main/docs/openapi.yaml).
It defines observation and alert routes, schemas, responses, and key-based
authentication. Treat this file as the source of truth for clients, and use the
[observation guide](api.md) or [alert guide](alerting.md) for workflow examples.

The direct API host serves alert administration and observation reads/writes.
The Node-RED `flows` host demonstrates observation ingestion and reads only; it
does not expose the alert administration routes. Configure a generated client
against the host and route your deployment actually provides.
