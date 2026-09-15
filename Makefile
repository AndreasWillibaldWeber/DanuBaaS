.DEFAULT_GOAL := test
.PHONY: test integration vet build fmt dev dev-test-data prod prod-check test-deploy test-e2e test-security test-grafana test-data-reset test-ranges

# These commands never delete volumes or rotate existing credentials.
dev:
	python3 deploy/scripts/environment.py dev

# Start development services, then load and verify synthetic observations over HTTP.
dev-test-data: dev
	python3 deploy/scripts/dev_test_data.py $(TEST_DATA_ARGS)

prod:
	python3 deploy/scripts/environment.py prod

prod-check:
	python3 deploy/scripts/environment.py prod-check

# Requires running services; E2E_ARGS can override endpoints, credentials, and CA.
test-e2e:
	python3 deploy/scripts/e2e.py $(E2E_ARGS)

test-deploy:
	python3 -m unittest discover -s deploy/scripts/tests -v

test:
	go test -race -count=1 ./...

integration:
	go test -race -count=1 -tags=integration ./internal/postgres

vet:
	go vet ./...

build:
	go build -trimpath -buildvcs=false -o bin/sensor-api ./cmd/sensor-api

fmt:
	gofmt -w cmd internal

# Host-security validation only: never applies firewall/SSH settings.
test-security:
	python3 -m unittest discover -s security/debian/tests -v
	sh -n security/debian/install-packages.sh
	sh -n security/debian/audit.sh
	sh -n security/debian/tests/network_policy.sh

# Requires the development database; uses rolled-back temporary SQL fixtures.
test-grafana:
	python3 deploy/grafana/build_dashboard.py --check
	python3 deploy/grafana/verify_dashboard.py

test-data-reset:
	python3 deploy/scripts/verify_test_data_reset.py

test-ranges:
	python3 deploy/scripts/verify_measurement_ranges.py
