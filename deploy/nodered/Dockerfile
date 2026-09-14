FROM nodered/node-red:5.0.7@sha256:a649dd711d55490151a2c39a8e48ad0c44325488fbc0e66315f2d2e19e5e1ace
USER root
# Do not copy the base image's placeholder flow into new named volumes.
RUN rm -f /data/flows.json
WORKDIR /opt/dbe
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
COPY auth.js ingestion.js settings.js flows.json flows_cred.json start.sh ./
RUN chown -R node-red:node-red /opt/dbe
USER node-red
ENV NODE_PATH=/opt/dbe/node_modules
WORKDIR /usr/src/node-red
ENTRYPOINT ["/bin/sh", "/opt/dbe/start.sh"]
# The upstream probe assumes /data/settings.js; our editor uses /opt/dbe/settings.js.
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=5 CMD node -e "const http=require('node:http'); const r=http.get('http://127.0.0.1:1880/admin/',s=>{s.resume();process.exit(s.statusCode===200?0:1)});r.on('error',()=>process.exit(1));r.setTimeout(2000,()=>{r.destroy();process.exit(1)})"
