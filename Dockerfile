FROM golang:1.26.8-alpine@sha256:ce864e7223ac17b1775e6fd0b4c0db580c2eb50e7953a427916379e4b92a1628 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN mkdir /dashboards && touch /dashboards/.keep
RUN CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o /sensor-api ./cmd/sensor-api

FROM gcr.io/distroless/static-debian12:nonroot@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab
COPY --from=build /sensor-api /sensor-api
COPY --from=build --chown=65532:65532 /dashboards /dashboards
USER 65532:65532
EXPOSE 8080 8081
ENTRYPOINT ["/sensor-api"]
