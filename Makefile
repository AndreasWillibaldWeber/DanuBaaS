.DEFAULT_GOAL := test
.PHONY: test integration vet build fmt dev prod prod-check test-deploy

# These commands never delete volumes or rotate existing credentials.
dev:
	python3 deploy/scripts/environment.py dev

prod:
	python3 deploy/scripts/environment.py prod

prod-check:
	python3 deploy/scripts/environment.py prod-check

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
