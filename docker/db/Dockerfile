FROM mariadb:10.5.4
COPY create.sql /docker-entrypoint-initdb.d/01-create.sql
COPY docker/db/init.sh /docker-entrypoint-initdb.d/02-init.sh
