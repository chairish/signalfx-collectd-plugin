#!/bin/bash
set -x
set -e

if [ -z "$CIRCLE_TEST_REPORTS" ]; then
	export CIRCLE_TEST_REPORTS=$TMPDIR
fi
mkdir -p "$CIRCLE_TEST_REPORTS/nosetests/"
mdl README.md
mdl CHANGELOG.md
nosetests -v --nologcapture -s --process-timeout 1s  --with-xunit --xunit-file="$CIRCLE_TEST_REPORTS/nosetests/junit.xml"
flake8 src/collectd_dogstatsd.py src/dummy_collectd.py src/test_dogstatsd.py src/signalfx_metadata.py
pylint src/test_dogstatsd.py src/dummy_collectd.py src/collectd_dogstatsd.py -r n
