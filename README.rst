
OpenStack Cinder - Eguan distribution
*************************************

This is the eguan distribution of OpenStack's Cinder block storage service.

It adds the eguan storage backend volume driver and its unit tests:
- the driver is in cinder/volume/drivers/eguan/
- tests can be found at cinder/tests/unit/test_eguan.py and associated mocks at cinder/tests/unit/volume/drivers/eguan/mocks.py

Unit tests can be run against a live backend by setting the EGUAN_IT_BACKEND
environment variable to "<tenant_UUID>|<eguan REST URI>", e.g.:

::

    export EGUAN_IT_BACKEND="fa753716fd1411e382fe180373e17099|https://eguan.example.com:8080/storage"

See the eguan configuration documentation for more details on how to setup the eguan service.

Below is the original README content.

======
CINDER
======

You have come across a storage service for an open cloud computing service.
It has identified itself as `Cinder`. It was abstracted from the Nova project.

* Wiki: http://wiki.openstack.org/Cinder
* Developer docs: http://docs.openstack.org/developer/cinder

Getting Started
---------------

If you'd like to run from the master branch, you can clone the git repo:

    git clone https://github.com/openstack/cinder.git

For developer information please see
`HACKING.rst <https://github.com/openstack/cinder/blob/master/HACKING.rst>`_

You can raise bugs here http://bugs.launchpad.net/cinder

Python client
-------------
https://github.com/openstack/python-cinderclient.git
