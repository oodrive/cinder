OpenStack Cinder - Eguan distribution
*************************************

This is the eguan distribution of OpenStack's Cinder block storage service.

It adds the eguan storage backend volume driver and its unit tests:
- the driver is in cinder/volume/drivers/eguan/
- tests can be found at cinder/tests/test_eguan.py and associated mocks at cinder/tests/eguan/mocks.py

Unit tests can be run against a live backend by setting the EGUAN_IT_BACKEND
environment variable to "<tenant_UUID>|<eguan REST URI>", e.g.:

::

    export EGUAN_IT_BACKEND="fa753716fd1411e382fe180373e17099|https://eguan.example.com:8080/storage"

See the eguan configuration documentation for more details on how to setup the eguan service.

Other modifications made for the eguan build process include:

- adding options to run_tests.sh to control coverage output formats and to run coverage tests only on specified modules,
- adding pom.xml and build scripts in maven-scripts to build a maven artifact and
- building  a custom distribution ZIP archive which is installable as an .egg package.

Below is the original README content.

The Choose Your Own Adventure README for Cinder
===============================================

You have come across a storage service for an open cloud computing service.
It has identified itself as "Cinder."   It was abstracted from the Nova project.

To monitor it from a distance: follow `@openstack <http://twitter.com/openstack>`_ on twitter.

To tame it for use in your own cloud: read http://docs.openstack.org

To study its anatomy: read http://cinder.openstack.org

To dissect it in detail: visit http://github.com/openstack/cinder

To taunt it with its weaknesses: use http://bugs.launchpad.net/cinder

To watch it: http://jenkins.openstack.org

To hack at it: read `HACKING.rst <https://github.com/openstack/cinder/blob/master/HACKING.rst>`_
