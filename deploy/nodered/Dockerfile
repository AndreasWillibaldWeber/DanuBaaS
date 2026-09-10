FROM nodered/node-red:5.0.7@sha256:a649dd711d55490151a2c39a8e48ad0c44325488fbc0e66315f2d2e19e5e1ace
USER root
WORKDIR /opt/dbe
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
COPY auth.js ingestion.js settings.js flows.json flows_cred.json start.sh ./
RUN chown -R node-red:node-red /opt/dbe
USER node-red
ENV NODE_PATH=/opt/dbe/node_modules
WORKDIR /usr/src/node-red
ENTRYPOINT ["/bin/sh", "/opt/dbe/start.sh"]
