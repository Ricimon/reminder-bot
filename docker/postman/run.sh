#!/bin/sh
set -e
wait-for-it.sh --timeout=0 db:3306
./main
