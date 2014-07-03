#!/usr/bin/env bash

./maven-scripts/clean.sh

echo "purging test environment"
for i in .testrepository .venv; do
    rm -rf $i
done
