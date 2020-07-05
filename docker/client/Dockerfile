FROM python:3.8-alpine
RUN apk add --no-cache netcat-openbsd gcc musl-dev libffi-dev openssl-dev bash && \
    pip install poetry
COPY docker/wait-for-it/wait-for-it.sh /bin/
WORKDIR /src
COPY pyproject.toml poetry.lock poetry.toml ./
RUN poetry install
COPY . .
CMD ["./docker/client/run.sh"]
