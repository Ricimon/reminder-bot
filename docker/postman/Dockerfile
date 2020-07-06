FROM rust:1.44-slim-stretch AS builder
RUN apt-get update && \
    apt-get install -y default-libmysqlclient-dev libssl-dev pkg-config
WORKDIR /src
COPY postman-rs .
RUN cargo build --release

FROM debian:stretch-slim
RUN apt-get update && \
    apt-get install -y default-libmysqlclient-dev libssl-dev ca-certificates
COPY docker/wait-for-it/wait-for-it.sh /bin/
WORKDIR /app
COPY docker/postman/run.sh ./
COPY --from=builder /src/target/release .
CMD ["./run.sh"]
