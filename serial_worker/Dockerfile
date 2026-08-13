FROM golang:1.22-alpine AS build
WORKDIR /src
COPY serial_worker/go.mod ./
RUN go mod download
COPY serial_worker/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o /serial-worker .

FROM alpine:3.20
COPY --from=build /serial-worker /usr/local/bin/serial-worker
ENTRYPOINT ["serial-worker"]
