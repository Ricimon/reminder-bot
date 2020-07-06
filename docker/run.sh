#!/bin/sh
set -e
wait-for-it.sh --timeout=0 db:3306
poetry run python -u languages/to_database.py
poetry run python -u main.py
