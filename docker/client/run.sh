#!/bin/sh
set -e
wait-for-it.sh --timeout=0 db:3306
poetry run python languages/to_database.py
poetry run python main.py
