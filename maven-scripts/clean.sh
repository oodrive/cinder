#!/usr/bin/env bash

[ -d .venv/ ] && source .venv/bin/activate

python setup.py clean

echo "removing build files"
rm -rf build

echo "removing test files"
for i in cover .coverage conversion subunit.log; do
    rm -rf $i
done
