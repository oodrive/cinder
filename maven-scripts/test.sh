#!/bin/bash

# run tests
# -V in virtual execution environment
# -P without PEP8 checks
./run_tests.sh -V -P

# activate venv
source .venv/bin/activate

# restore the last testr run and convert results to jUnit XML
testr last --subunit | subunit-1to2 | subunit2junitxml --no-passthrough -o ${RESULT_FILENAME:-target/testresults.xml}

# run coverage for module 
./run_tests.sh -c xml --coverage-module cinder/volume/drivers/eguan cinder.tests.test_oodrive_eguan.TestEguanDriver

