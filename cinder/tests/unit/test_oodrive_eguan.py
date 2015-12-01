
# Copyright (c) 2012 - 2015 Oodrive
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Test the eguan block storage backend driver.

@author: pwehrle
"""


from cinder import exception
from cinder import test
from cinder.tests.unit.volume.drivers.eguan import mocks
from cinder.volume.configuration import Configuration
from cinder.volume.drivers.eguan.driver import eguan_opts
from cinder.volume.drivers.eguan.driver import EguanDriver
from cinder.volume.drivers.eguan.driver import KeystoneNotificationHandler
from cinder.volume.drivers.eguan.driver import map_tenant_to_vvr_id
from cinder.volume.drivers.eguan import resource_proxies
from httmock import all_requests
from httmock import HTTMock
from os import environ
from oslo_config import cfg
from oslo_log import log
from oslo_messaging.notify.dispatcher import NotificationResult
from oslo_utils import units
from requests.exceptions import ConnectionError
from requests import Session
from threading import Thread
from uuid import UUID
from uuid import uuid4


LOG = log.getLogger(__name__)

CONF = cfg.CONF

INTEGR_BACKEND = environ.get('EGUAN_INT_BACKEND')

INT_SPLIT = INTEGR_BACKEND.split('|') if INTEGR_BACKEND else None

_VOLD_BACKENDS = ({UUID(hex=INT_SPLIT[0]): INT_SPLIT[1]}
                  if INTEGR_BACKEND else
                  {uuid4(): uri + '/storage'
                   for uri in ['http://192.0.2.130:2345',
                               'http://198.51.100.130:2345',
                               'http://203.0.113.130:2345']})

_MOCK_VOLD_REST_SERVERS = ({t_id: mocks.MockVvrsRes(str(t_id), url, {})
                            for t_id, url in
                           _VOLD_BACKENDS.iteritems()})


@all_requests
def respond_all(url, request):
    """Intercept all calls, raise common exceptions and
        forward to mock volds.
    """
    LOG.debug("Analyzing call to URL %(url)s with request %(request)s" %
              {'url': url, 'request': request})
    # checks if the requested host is reachable
    target_hosts = [host_url for host_url in
                    _MOCK_VOLD_REST_SERVERS.itervalues()
                    if host_url.base_url.netloc == url.netloc]
    LOG.debug('Found matching hosts; %s' % target_hosts)
    if len(target_hosts) == 0:
        raise ConnectionError()
    # checks for a valid tenant ID
    tenant_id = UUID(mocks.extract_tenant_id(url))
    target_server = _MOCK_VOLD_REST_SERVERS.get(tenant_id)
    if target_server is None:
        return {'status_code': 403}
    # forward request to next level
    server_mock = HTTMock(target_server.respond_all)
    with server_mock:
        s = Session()
        return s.send(request)

ROOT_MOCK = (HTTMock() if INTEGR_BACKEND
             else HTTMock(respond_all))


def _new_test_volume(tenant_id, snapshot_id=None, **kw_args):
    volume_id = uuid4().get_hex()
    display_name = 'test_vol_' + volume_id
    volume_size_gb = kw_args.get('size') or 4
    test_vol = {'name': display_name,
                'size': volume_size_gb,
                'display_name': display_name,
                'id': volume_id,
                'provider_auth': None,
                'project_id': tenant_id.get_hex(),
                'display_description':
                'test volume for owner ID ' + str(tenant_id),
                'snapshot_id': snapshot_id,
                'volume_type_id': None}
    test_vol.update(kw_args)
    return test_vol


def _new_test_snapshot(tenant_id, volume_id, **kw_args):
    snapshot_id = uuid4().get_hex()
    test_snap = {'display_name': 'testSnap_' + str(tenant_id),
                 'id': snapshot_id,
                 'volume_id': volume_id,
                 'volume_size': 1,
                 'project_id': tenant_id.get_hex()}
    test_snap.update(kw_args)
    return test_snap


class TestEguanDriver(test.TestCase):
    """Main test class for the eguan block storage backend driver."""
    def setUp(self):
        super(TestEguanDriver, self).setUp()
        self._driver = EguanDriver()
        CONF.import_group('eguan', 'cinder.volume.drivers.eguan')
        CONF.import_opt('vold_rest_uri', 'cinder.volume.drivers.eguan',
                        'eguan')
        CONF.eguan.vold_rest_uri = ([INTEGR_BACKEND] if INTEGR_BACKEND
                                    else [t_id.get_hex() + '|' + url for t_id,
                                          url in _VOLD_BACKENDS.iteritems()])
        # initialize backend by creating the appropriate VVR(s)
        for t_id, url in _VOLD_BACKENDS.iteritems():
            vvrs_res = resource_proxies.VvrsRes(url + '/vvrs', t_id)
            vvr_id = map_tenant_to_vvr_id(t_id)
            with ROOT_MOCK:
                try:
                    vvr_res = vvrs_res.get_vvr(vvr_id)
                except exception.NotFound:
                    vvr_res = vvrs_res.create_vvr(vvr_id)
                # start the VVR if it's not yet started
                if not vvr_res.started:
                    vvr_res.start()

    def test_do_setup(self):
        """Test successful setup using the test client configuration."""
        with HTTMock(respond_all):
            self._driver.do_setup({})

    def test_do_setup_fail_no_backend(self):
        """Test setup failure due to a missing backend address."""
        CONF.eguan.vold_rest_uri = []
        self.assertRaises(exception.VolumeBackendAPIException,
                          self._driver.do_setup, {})

    def test_do_setup_fail_invalid_tenant(self):
        """Test setup failure due to an invalid tenant ID."""
        CONF.eguan.vold_rest_uri.append("%s|http://192.0.2.134:4536/storage" %
                                        '1337-dead-beef')
        self.assertRaises(exception.VolumeBackendAPIException,
                          self._driver.do_setup, {})

    def test_check_for_setup_error(self):
        """Test successful post-setup checking."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()

    def test_check_for_setup_error_fail_unreachable(self):
        """Test post-setup check failure due to an unreachable vold server."""
        CONF.eguan.vold_rest_uri.append("%s|http://192.0.2.134:4536/storage" %
                                        uuid4().get_hex())
        with ROOT_MOCK:
            self._driver.do_setup({})
            self.assertRaises(exception.VolumeBackendAPIException,
                              self._driver.check_for_setup_error)

    def test_create_volume(self):
        """Test creating volumes on multiple REST backends."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                volume_id = UUID(hex=test_vol.get('id'))
                display_name = test_vol.get('display_name')
                volume_size_gb = test_vol.get('size')

                self._driver.create_volume(test_vol)
                read_volume = self._driver.get_volume(tenant_id, volume_id)
                self.assertEqual(volume_id,
                                 UUID(hex=getattr(read_volume, 'uuid')))
                self.assertEqual(display_name, getattr(read_volume, 'name'))
                self.assertEqual(volume_size_gb * units.Gi,
                                 long(getattr(read_volume, 'size')))

    def test_create_volume_with_description(self):
        """Test creating volumes with optional descriptions."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                description = 'Test volume description'
                test_vol['display_description'] = description

                volume_id = UUID(hex=test_vol.get('id'))
                display_name = test_vol.get('display_name')
                volume_size_gb = test_vol.get('size')

                self._driver.create_volume(test_vol)
                read_volume = self._driver.get_volume(tenant_id, volume_id)
                self.assertEqual(volume_id,
                                 UUID(hex=getattr(read_volume, 'uuid')))
                self.assertEqual(display_name, getattr(read_volume, 'name'))
                self.assertEqual(volume_size_gb * units.Gi,
                                 long(getattr(read_volume, 'size')))
                self.assertEqual(description, getattr(read_volume,
                                                      'description'))

    def test_create_volume_no_name(self):
        """Test creation of volumes without a name."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                volume_id = UUID(hex=test_vol.get('id'))
                test_vol.pop('display_name')
                self._driver.create_volume(test_vol)
                read_volume = self._driver.get_volume(tenant_id, volume_id)
                self.assertEqual(volume_id,
                                 UUID(hex=getattr(read_volume, 'uuid')))

    def test_create_volume_fail_missing_project_id(self):
        """Test failure to create volumes due to missing project IDs."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                test_vol.pop('project_id')
                self.assertIsNone(test_vol.get('project_id'))
                self.assertRaises(exception.VolumeBackendAPIException,
                                  lambda: self._driver.create_volume(test_vol))

    def test_create_volume_fail_bad_project_id(self):
        """Test failure to create volumes due to a bad project ID."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                test_vol.update({'project_id': uuid4().get_hex()})
                self.assertRaises(exception.VolumeBackendAPIException,
                                  lambda: self._driver.create_volume(test_vol))

    def test_create_volume_fail_invalid_size(self):
        """Test failure to create volumes due to a negative size."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                test_vol['size'] = -1
                self.assertRaises(exception.VolumeBackendAPIException,
                                  lambda: self._driver.create_volume(test_vol))

    def test_create_volume_fail_no_size(self):
        """Test failure to create volumes due to a missing size."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                test_vol.pop('size')
                self.assertRaises(exception.VolumeBackendAPIException,
                                  lambda: self._driver.create_volume(test_vol))

    def test_create_volume_from_snapshot(self):
        """Test creating volumes from existing snapshots."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                self._driver.create_volume(test_vol)

                test_snap = _new_test_snapshot(tenant_id,
                                               volume_id=test_vol['id'],
                                               display_name='test_snap')
                snapshot_id = UUID(hex=test_snap.get('id'))
                snapshot_name = test_snap.get('display_name')
                self._driver.create_snapshot(test_snap)
                test_snap_vol = _new_test_volume(tenant_id,
                                                 snapshot_id=test_snap['id'])
                self._driver.create_volume_from_snapshot(test_snap_vol,
                                                         test_snap)
                read_snap = self._driver.get_snapshot(tenant_id, snapshot_id)
                self.assertEqual(snapshot_id,
                                 UUID(hex=getattr(read_snap, 'uuid')))
                self.assertEqual(snapshot_name, getattr(read_snap, 'name'))

    def test_create_volume_from_snapshot_fail_no_snap(self):
        """Test volume creation failure if given an invalid snapshot ID."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                vol_id = uuid4().get_hex()
                bad_snap = _new_test_snapshot(tenant_id, volume_id=vol_id)
                bad_snap_id = bad_snap['id']
                test_vol = _new_test_volume(tenant_id, snapshot_id=bad_snap_id)
                test_vol['id'] = vol_id
                self.assertRaises(exception.NotFound, lambda:
                                  self._driver.
                                  create_volume_from_snapshot(test_vol,
                                                              bad_snap))

    def test_create_cloned_volume(self):
        """Test volume creation as a clone of an existing volume."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                orig_vol = _new_test_volume(tenant_id, size=6)
                self._driver.create_volume(orig_vol)

                cloned_vol = _new_test_volume(tenant_id, size=6)
                self._driver.create_cloned_volume(cloned_vol, orig_vol)

                read_cloned_vol = (self._driver.
                                   get_volume(tenant_id,
                                              UUID(hex=cloned_vol['id'])))
                self.assertNotEqual(UUID(hex=orig_vol['id']),
                                    UUID(hex=getattr(read_cloned_vol,
                                                     read_cloned_vol.id_attr)))
                self.assertEqual(orig_vol['size'] * units.Gi,
                                 long(getattr(read_cloned_vol, 'size')))

    def test_create_cloned_volume_with_description(self):
        """Test volume creation as a clone with a description."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                orig_vol = _new_test_volume(tenant_id, size=6)
                self._driver.create_volume(orig_vol)

                cloned_vol = _new_test_volume(tenant_id, size=6)
                clone_desc = 'volume cloned from ' + orig_vol['id']
                cloned_vol['display_description'] = clone_desc

                self._driver.create_cloned_volume(cloned_vol, orig_vol)

                read_cloned_vol = (self._driver.
                                   get_volume(tenant_id,
                                              UUID(hex=cloned_vol['id'])))
                self.assertNotEqual(UUID(hex=orig_vol['id']),
                                    UUID(hex=getattr(read_cloned_vol,
                                                     read_cloned_vol.id_attr)))
                self.assertEqual(orig_vol['size'] * units.Gi,
                                 long(getattr(read_cloned_vol, 'size')))
                self.assertEqual(clone_desc, getattr(read_cloned_vol,
                                                     'description'))

    def test_create_cloned_volume_fail_no_src_volume(self):
        """Test volume cloning failure if given a non-existent source."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                orig_vol = _new_test_volume(tenant_id)
                cloned_vol = _new_test_volume(tenant_id)

                self.assertRaises(exception.NotFound,
                                  lambda: self._driver.
                                  create_cloned_volume(cloned_vol, orig_vol))

    def test_create_cloned_volume_fail_diff_tenant_ids(self):
        """Test volume creation failure due to different tenant IDs."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                orig_vol = _new_test_volume(tenant_id)
                self._driver.create_volume(orig_vol)

                cloned_vol = _new_test_volume(uuid4())
                self.assertRaises(exception.VolumeBackendAPIException,
                                  lambda: self._driver.
                                  create_cloned_volume(cloned_vol, orig_vol))

    def test_extend_volume(self):
        """Test deletion of a volume."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                volume_id = UUID(hex=test_vol.get('id'))
                old_size = test_vol.get('size')
                self._driver.create_volume(test_vol)

                read_volume = self._driver.get_volume(tenant_id, volume_id)
                self.assertEqual(old_size * units.Gi,
                                 long(getattr(read_volume, 'size')))
                new_size = 2 * old_size
                self._driver.extend_volume(test_vol, new_size)
                new_read_vol = self._driver.get_volume(tenant_id, volume_id)
                self.assertEqual(new_size * units.Gi,
                                 long(getattr(new_read_vol, 'size')))

    def test_extend_volume_fail_negative_size(self):
        """Test deletion of a volume."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                volume_id = UUID(hex=test_vol.get('id'))
                old_size = test_vol.get('size')
                self._driver.create_volume(test_vol)

                read_volume = self._driver.get_volume(tenant_id, volume_id)
                self.assertEqual(old_size * units.Gi,
                                 long(getattr(read_volume, 'size')))
                new_size = -2 * old_size
                self._driver.extend_volume(test_vol, new_size)
                # check the volume's size was not set to the negative value
                reread_volume = self._driver.get_volume(tenant_id, volume_id)
                self.assertGreater(getattr(reread_volume, 'size'), 0)

    def test_delete_volume(self):
        """Test deletion of a volume."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                volume_id = UUID(hex=test_vol.get('id'))

                self._driver.create_volume(test_vol)

                self._driver.delete_volume(test_vol)
                self.assertRaises(exception.NotFound,
                                  lambda: self._driver.get_volume(tenant_id,
                                                                  volume_id))

    def test_delete_volume_fail_no_volume(self):
        """Test failed deletion of an inexistent volume."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                volume_id = UUID(hex=test_vol.get('id'))
                self.assertRaises(exception.NotFound,
                                  lambda: self._driver.get_volume(tenant_id,
                                                                  volume_id))

                self.assertRaises(exception.NotFound,
                                  lambda: self._driver.delete_volume(test_vol))

    def test_create_snapshot(self):
        """Test creation of a snapshot."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                self._driver.create_volume(test_vol)

                test_snap = _new_test_snapshot(tenant_id,
                                               volume_id=test_vol['id'])
                snap_id = UUID(hex=test_snap['id'])
                self._driver.create_snapshot(test_snap)
                read_snap = self._driver.get_snapshot(tenant_id, snap_id)
                self.assertEqual(snap_id,
                                 UUID(hex=getattr(read_snap, 'uuid')))
                self.assertEqual(test_snap['display_name'],
                                 getattr(read_snap, 'name'))

                read_vol = self._driver.get_volume(tenant_id,
                                                   UUID(hex=test_vol['id']))
                self.assertEqual(UUID(hex=test_snap['volume_id']),
                                 UUID(hex=getattr(read_vol, 'uuid')))
                self.assertEqual(snap_id,
                                 UUID(hex=getattr(read_vol, 'parent')))

    def test_create_snapshot_with_description(self):
        """Test creation of a snapshot with optional description."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                self._driver.create_volume(test_vol)

                tst_snap = _new_test_snapshot(tenant_id,
                                              volume_id=test_vol['id'])
                description = 'Test snapshot description'
                tst_snap['display_description'] = description

                snap_id = UUID(hex=tst_snap['id'])
                self._driver.create_snapshot(tst_snap)
                read_snap = self._driver.get_snapshot(tenant_id, snap_id)
                self.assertEqual(snap_id,
                                 UUID(hex=getattr(read_snap, 'uuid')))
                self.assertEqual(tst_snap['display_name'],
                                 getattr(read_snap, 'name'))

                read_vol = self._driver.get_volume(tenant_id,
                                                   UUID(hex=test_vol['id']))
                self.assertEqual(UUID(hex=tst_snap['volume_id']),
                                 UUID(hex=getattr(read_vol, 'uuid')))
                self.assertEqual(snap_id,
                                 UUID(hex=getattr(read_vol, 'parent')))

                read_snap = self._driver.get_snapshot(tenant_id,
                                                      UUID(hex=tst_snap['id']))
                self.assertEqual(snap_id, UUID(hex=getattr(read_snap, 'uuid')))
                self.assertEqual(description, getattr(read_snap,
                                                      'description'))

    def test_create_snapshot_no_name(self):
        """Test snapshot creation without a name."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                self._driver.create_volume(test_vol)

                test_snap = _new_test_snapshot(tenant_id,
                                               volume_id=test_vol['id'])
                test_snap.pop('display_name')
                snap_id = UUID(hex=test_snap['id'])
                self._driver.create_snapshot(test_snap)
                read_snap = self._driver.get_snapshot(tenant_id, snap_id)
                self.assertEqual(snap_id,
                                 UUID(hex=getattr(read_snap, 'uuid')))

    def test_create_snapshot_fail_no_volume(self):
        """Test snapshot creation failure if given an invalid volume ID."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                bad_vol_id = uuid4().get_hex()
                test_snap = _new_test_snapshot(tenant_id, volume_id=bad_vol_id)
                self.assertRaises(exception.NotFound, lambda:
                                  self._driver.
                                  create_snapshot(test_snap))

    def test_create_snapshot_fail_missing_project_id(self):
        """Test failure to create snapshots due to missing project IDs."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                vol_id = test_vol['id']
                test_snap = _new_test_snapshot(tenant_id,
                                               volume_id=vol_id)
                self._driver.create_volume(test_vol)
                test_snap.pop('project_id')
                self.assertIsNone(test_snap.get('project_id'))
                self.assertRaises(exception.VolumeBackendAPIException,
                                  lambda: self._driver.
                                  create_snapshot(test_snap))

    def test_create_snapshot_fail_bad_project_id(self):
        """Test failure to create snapshots due to a bad project ID."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                vol_id = test_vol['id']
                test_snap = _new_test_snapshot(tenant_id,
                                               volume_id=vol_id)
                self._driver.create_volume(test_vol)
                test_snap.update({'project_id': uuid4().get_hex()})
                self.assertRaises(exception.VolumeBackendAPIException,
                                  lambda: self.
                                  _driver.create_snapshot(test_snap))

    def test_create_snapshot_fail_read_only(self):
        """Test failure creating a snapshot on a read-only volume."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                volume_id = UUID(hex=test_vol.get('id'))

                self._driver.create_volume(test_vol)

                test_vol['readOnly'] = True
                self._driver.create_export({}, test_vol)

                read_volume = self._driver.get_volume(tenant_id, volume_id)
                self.assertTrue(getattr(read_volume, 'readOnly'))

                test_snap = _new_test_snapshot(tenant_id,
                                               volume_id=test_vol['id'])
                self.assertRaises(exception.VolumeBackendAPIException,
                                  lambda: self.
                                  _driver.create_snapshot(test_snap))

    def test_delete_snapshot(self):
        """Test deletion of a snapshot."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                self._driver.create_volume(test_vol)

                test_snap = _new_test_snapshot(tenant_id,
                                               volume_id=test_vol['id'])
                snap_id = UUID(hex=test_snap['id'])
                self._driver.create_snapshot(test_snap)

                self._driver.delete_snapshot(test_snap)
                self.assertRaises(exception.NotFound,
                                  lambda: self._driver.get_volume(tenant_id,
                                                                  snap_id))

    def test_delete_snapshot_fail_no_snapshot(self):
        """Test deletion of a snapshot."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                self._driver.create_volume(test_vol)

                test_snap = _new_test_snapshot(tenant_id,
                                               volume_id=test_vol['id'])
                snap_id = UUID(hex=test_snap.get('id'))

                self.assertRaises(exception.NotFound,
                                  lambda: self._driver.get_snapshot(tenant_id,
                                                                    snap_id))

                self.assertRaises(exception.NotFound,
                                  lambda: self._driver.
                                  delete_snapshot(test_snap))

    def test_create_export(self):
        """Test export creation."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                self._driver.create_volume(test_vol)

                update = self._driver.create_export({}, test_vol)
                self.assertTrue(update['provider_location'])
                #TODO(pwehrle): check for valid values

    def test_create_export_read_only(self):
        """Test export creation."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                volume_id = UUID(hex=test_vol.get('id'))
                self._driver.create_volume(test_vol)

                test_vol['readOnly'] = True

                update = self._driver.create_export({}, test_vol)
                self.assertTrue(update['provider_location'])
                #TODO(pwehrle): check for valid values

                read_volume = self._driver.get_volume(tenant_id, volume_id)
                self.assertTrue(getattr(read_volume, 'readOnly'))

    def test_create_export_fail_no_volume(self):
        """Test export creation failure due to a missing volume."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)

                self.assertRaises(exception.NotFound,
                                  lambda: self._driver.
                                  create_export({}, test_vol))

    def test_ensure_export(self):
        """Test ensuring export existence."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                volume_id = UUID(hex=test_vol['id'])
                self._driver.create_volume(test_vol)

                vol_res = self._driver.get_volume(tenant_id, volume_id)
                self.assertFalse(hasattr(vol_res, 'provider_location'))

                self._driver.ensure_export({}, test_vol)
                exp_vol_res = self._driver.get_volume(tenant_id, volume_id)
                self.assertTrue(hasattr(exp_vol_res, 'provider_location'))

    def test_remove_export(self):
        """Test export removal."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                volume_id = UUID(hex=test_vol['id'])
                test_vol.update(self._driver.create_volume(test_vol) or {})

                test_vol.update(self._driver.create_export({}, test_vol))
                self.assertTrue(test_vol['provider_location'])

                self._driver.remove_export({}, test_vol)
                read_vol_res = self._driver.get_volume(tenant_id, volume_id)
                self.assertFalse(hasattr(read_vol_res, 'provider_location'))

    def test_initialize_connection(self):
        """Test connection initialization for an unspecified client."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                test_vol.update(self._driver.create_volume(test_vol) or {})

                test_vol.update(self._driver.create_export({}, test_vol))
                self.assertTrue(test_vol.get('provider_location'))

                test_conn = {'ip': '192.0.2.232'}

                connection_info = (self._driver.
                                   initialize_connection(test_vol,
                                                         test_conn))

                vol_type = connection_info['driver_volume_type']
                self.assertEqual('iscsi', vol_type)
                self.assertTrue('data' in connection_info)

                conn_data = connection_info['data']
                self.assertTrue('access_mode' in conn_data)
                self.assertTrue('target_portal' in conn_data)
                #iscsi-specific part
                self.assertTrue('target_iqn' in conn_data)
                self.assertTrue('volume_id' in conn_data)
                self.assertTrue('target_lun' in conn_data)
                self.assertFalse('nbd_dev_name' in conn_data)

    def test_initialize_connection_iscsi(self):
        """Test connection initialization for an iSCSI client."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                test_vol.update(self._driver.create_volume(test_vol) or {})

                test_vol.update(self._driver.create_export({}, test_vol))
                self.assertTrue(test_vol.get('provider_location'))

                test_conn = {'ip': '192.0.2.232',
                             'initiator':
                             'iqn.1993-08.org.debian:01:80373e17099'}

                connection_info = (self._driver.
                                   initialize_connection(test_vol,
                                                         test_conn))

                vol_type = connection_info['driver_volume_type']
                self.assertEqual('iscsi', vol_type)
                self.assertTrue('data' in connection_info)

                conn_data = connection_info['data']
                self.assertTrue('access_mode' in conn_data)
                self.assertTrue('target_portal' in conn_data)
                #iscsi-specific part
                self.assertTrue('target_iqn' in conn_data)
                self.assertTrue('volume_id' in conn_data)
                self.assertTrue('target_lun' in conn_data)
                self.assertFalse('nbd_dev_name' in conn_data)

    def test_initialize_connection_nbd(self):
        """Test connection initialization for an NBD client."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                test_vol.update(self._driver.create_volume(test_vol) or {})

                test_vol.update(self._driver.create_export({}, test_vol))
                self.assertTrue(test_vol.get('provider_location'))

                test_conn = {'ip': '192.0.2.232',
                             'nbd_client_id':
                             'nbd-client version 2.9.25'}

                connection_info = (self._driver.
                                   initialize_connection(test_vol,
                                                         test_conn))

                vol_type = connection_info['driver_volume_type']
                self.assertEqual('nbd', vol_type)
                self.assertTrue('data' in connection_info)

                conn_data = connection_info['data']
                self.assertTrue('access_mode' in conn_data)
                self.assertTrue('target_portal' in conn_data)
                # nbd-specific part
                self.assertTrue('nbd_dev_name' in conn_data)
                self.assertFalse('target_iqn' in conn_data)

    def test_initialize_connection_not_exported(self):
        """Test connection initialization on a non-exported volume."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                volume_id = UUID(hex=test_vol['id'])
                test_vol.update(self._driver.create_volume(test_vol) or {})

                self.assertFalse(test_vol.get('provider_location'))

                test_conn = {'ip': '192.0.2.232',
                             'initiator':
                             'iqn.1993-08.org.debian:01:80373e17099'}

                connection_info = self._driver.initialize_connection(test_vol,
                                                                     test_conn)
                vol_res = self._driver.get_volume(tenant_id, volume_id)
                self.assertTrue(hasattr(vol_res, 'provider_location'))

                vol_type = connection_info['driver_volume_type']
                self.assertTrue(vol_type in {'iscsi', 'nbd'})
                self.assertTrue('data' in connection_info)

                conn_data = connection_info['data']
                self.assertTrue('access_mode' in conn_data)
                self.assertTrue('target_portal' in conn_data)
                if vol_type == 'iscsi':
                    self.assertTrue('target_iqn' in conn_data)
                    self.assertTrue('volume_id' in conn_data)
                    self.assertTrue('target_lun' in conn_data)
                elif vol_type == 'nbd':
                    self.assertTrue('nbd_dev_name' in conn_data)

    def test_initialize_connection_fail_no_client_ip(self):
        """Test connection init failure due to a missing client IP."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                test_vol.update(self._driver.create_volume(test_vol) or {})

                test_vol.update(self._driver.create_export({}, test_vol))
                self.assertTrue(test_vol['provider_location'])

                test_conn = {'initiator':
                             'iqn.1993-08.org.debian:01:80373e17099'}

                self.assertRaises(exception.VolumeBackendAPIException,
                                  lambda: self.
                                  _driver.initialize_connection(test_vol,
                                                                test_conn))

    def test_validate_connector(self):
        """Test the connector validation function."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                test_vol.update(self._driver.create_volume(test_vol) or {})

                test_vol.update(self._driver.create_export({}, test_vol))
                self.assertTrue(test_vol['provider_location'])

                test_conn = {'ip': '192.0.2.232',
                             'initiator':
                             'iqn.1993-08.org.debian:01:80373e17099'}
                self._driver.validate_connector(test_conn)

    def test_validate_connector_fail_no_ip(self):
        """Test the connector validation function."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                test_vol.update(self._driver.create_volume(test_vol) or {})

                test_vol.update(self._driver.create_export({}, test_vol))
                self.assertTrue(test_vol['provider_location'])

                test_conn = {'initiator':
                             'iqn.1993-08.org.debian:01:80373e17099'}
                self.assertRaises(exception.VolumeBackendAPIException,
                                  lambda: self._driver.
                                  validate_connector(test_conn))

    def test_terminate_connection(self):
        """Test connection termination."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                test_vol.update(self._driver.create_volume(test_vol) or {})

                test_vol.update(self._driver.create_export({}, test_vol))
                self.assertTrue(test_vol.get('provider_location'))

                test_conn = {'ip': '192.0.2.232',
                             'initiator':
                             'iqn.1993-08.org.debian:01:80373e17099'}

                self._driver.initialize_connection(test_vol, test_conn)

                self._driver.terminate_connection(test_vol, test_conn)
                #TODO(pwehrle): find some smart thing(s) to check

    def test_attach_volume(self):
        """Test attaching volumes to instances."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                test_vol.update(self._driver.create_volume(test_vol) or {})
                instance_uuid = uuid4()
                self._driver.attach_volume({}, test_vol, instance_uuid,
                                           'test_host.example.com',
                                           '/mnt/vol1')

    def test_attach_volume_fail_no_volume(self):
        """Test failure to attach non-existent volumes to instances."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                instance_uuid = uuid4()
                self.assertRaises(exception.NotFound,
                                  lambda: self._driver.
                                  attach_volume({}, test_vol, instance_uuid,
                                                'test_host.example.com',
                                                '/mnt/vol1'))

    def test_detach_volume(self):
        """Test detaching volumes from instances."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                test_vol.update(self._driver.create_volume(test_vol) or {})
                instance_uuid = uuid4()
                test_host = 'test_host.example.com'
                self._driver.attach_volume({}, test_vol, instance_uuid,
                                           test_host,
                                           '/mnt/vol1')
                # add attached_host manually
                test_vol['attached_host'] = test_host
                self._driver.detach_volume({}, test_vol)

    def test_detach_volume_not_attached(self):
        """Test detaching non-attached volumes from instances."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                test_vol.update(self._driver.create_volume(test_vol) or {})
                self._driver.detach_volume({}, test_vol)

    def test_detach_volume_fail_no_volume(self):
        """Test failure detaching non-existent volumes from instances."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            for tenant_id in _VOLD_BACKENDS.iterkeys():
                test_vol = _new_test_volume(tenant_id)
                test_vol['attached_host'] = 'test_host.example.com'
                self.assertRaises(exception.NotFound,
                                  lambda: self._driver.detach_volume({},
                                                                     test_vol))

    def test_get_volume_stats(self):
        """Test getting volume statistics."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            cap_values = ['unknown', 'infinite']
            stats_data = self._driver.get_volume_stats()
            self.assertTrue(stats_data.get('driver_version'))
            free_cap = stats_data.get('free_capacity_gb')
            self.assertTrue(free_cap in cap_values or
                            long(free_cap) >= 0)
            self.assertTrue(long(stats_data.
                                 get('reserved_percentage')) >= 0)
            self.assertTrue(stats_data.get('storage_protocol') in ['iSCSI',
                                                                   'nbd'])
            total_cap = stats_data.get('total_capacity_gb')
            self.assertTrue(total_cap in cap_values or
                            long(total_cap) >= 0)
            self.assertEqual(stats_data.get('vendor_name'), 'Oodrive')
            self.assertEqual(stats_data.get('volume_backend_name'),
                             'eguan')

    def test_get_volume_stats_with_conf_backend_name(self):
        """Test getting volume statistics with a configured backend name."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            cap_values = ['unknown', 'infinite']

            custom_backend_name = 'Custom backend'
            new_conf = Configuration(eguan_opts)
            new_conf.local_conf.set_override('volume_backend_name',
                                             custom_backend_name)
            try:
                new_driver = EguanDriver(configuration=new_conf)

                stats_data = new_driver.get_volume_stats(refresh=True)
                self.assertTrue(stats_data.get('driver_version'))
                free_cap = stats_data.get('free_capacity_gb')
                self.assertTrue(free_cap in cap_values or
                                long(free_cap) >= 0)
                self.assertTrue(long(stats_data.
                                     get('reserved_percentage')) >= 0)
                self.assertTrue(stats_data.get('storage_protocol') in ['iSCSI',
                                                                       'nbd'])
                total_cap = stats_data.get('total_capacity_gb')
                self.assertTrue(total_cap == 'infinite' or
                                long(total_cap) >= 0)
                self.assertEqual(stats_data.get('vendor_name'), 'Oodrive')
                self.assertEqual(stats_data.get('volume_backend_name'),
                                 custom_backend_name)
            finally:
                new_conf.local_conf.clear_override('volume_backend_name')

    def test_get_volume_stats_with_refresh(self):
        """Test getting volume statistics with forced refresh."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()
            cap_values = ['unknown', 'infinite']
            stats_data = self._driver.get_volume_stats(refresh=True)
            self.assertTrue(stats_data.get('driver_version'))
            free_cap = stats_data.get('free_capacity_gb')
            self.assertTrue(free_cap in cap_values or
                            long(free_cap) >= 0)
            self.assertTrue(long(stats_data.
                                 get('reserved_percentage')) >= 0)
            self.assertTrue(stats_data.get('storage_protocol') in ['iSCSI',
                                                                   'nbd'])
            total_cap = stats_data.get('total_capacity_gb')
            self.assertTrue(total_cap == 'infinite' or
                            long(total_cap) >= 0)
            self.assertEqual(stats_data.get('vendor_name'), 'Oodrive')
            self.assertEqual(stats_data.get('volume_backend_name'),
                             'eguan')

    def test_keystone_notification_create(self):
        """Test creating a VVR following a tenant creation notification."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()

            notif_handler = KeystoneNotificationHandler(self._driver)
            new_tenant_id = uuid4()
            notif_result = notif_handler.info({},
                                              'identity.localhost',
                                              'identity.project.created',
                                              {'resource_info':
                                               new_tenant_id.get_hex()},
                                              {})
            self.assertEqual(NotificationResult.HANDLED, notif_result)

            vvr_id = map_tenant_to_vvr_id(new_tenant_id)
            vvrs_proxy = self._driver._checked_vvrs_proxy(new_tenant_id)
            target_vvr = vvrs_proxy.get_vvr(vvr_id)
            self.assertEqual(vvr_id, UUID(hex=getattr(target_vvr, 'uuid')))

            test_vol = _new_test_volume(new_tenant_id)
            volume_id = UUID(hex=test_vol.get('id'))

            self._driver.create_volume(test_vol)
            read_volume = self._driver.get_volume(new_tenant_id, volume_id)
            self.assertEqual(volume_id,
                             UUID(hex=getattr(read_volume, 'uuid')))

    def test_keystone_notification_create_existing(self):
        """Test creating a VVR following a tenant creation notification."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()

            notif_handler = KeystoneNotificationHandler(self._driver)
            new_tenant_id = uuid4()
            notif_result = notif_handler.info({},
                                              'identity.localhost',
                                              'identity.project.created',
                                              {'resource_info':
                                               new_tenant_id.get_hex()},
                                              {})
            self.assertEqual(NotificationResult.HANDLED, notif_result)

            vvr_id = map_tenant_to_vvr_id(new_tenant_id)
            vvrs_proxy = self._driver._checked_vvrs_proxy(new_tenant_id)
            target_vvr = vvrs_proxy.get_vvr(vvr_id)
            self.assertEqual(vvr_id, UUID(hex=getattr(target_vvr, 'uuid')))

            notif_result = notif_handler.info({},
                                              'identity.localhost',
                                              'identity.project.created',
                                              {'resource_info':
                                               new_tenant_id.get_hex()},
                                              {})
            self.assertEqual(NotificationResult.HANDLED, notif_result)

    def test_keystone_notification_create_async_vol_request(self):
        """Test volume creation requests while the VVR is being created."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()

            notif_handler = KeystoneNotificationHandler(self._driver)
            new_tenant_id = uuid4()
            notif_thread = Thread(target=notif_handler.info,
                                  args=({},
                                        'identity.localhost',
                                        'identity.project.created',
                                        {'resource_info':
                                         new_tenant_id.get_hex()},
                                        {}))
            notif_thread.start()

            test_vol = _new_test_volume(new_tenant_id)
            volume_id = UUID(hex=test_vol.get('id'))

            self._driver.create_volume(test_vol)
            read_volume = self._driver.get_volume(new_tenant_id, volume_id)
            self.assertEqual(volume_id,
                             UUID(hex=getattr(read_volume, 'uuid')))

            notif_thread.join(10)

            vvr_id = map_tenant_to_vvr_id(new_tenant_id)
            vvrs_proxy = self._driver._checked_vvrs_proxy(new_tenant_id)
            target_vvr = vvrs_proxy.get_vvr(vvr_id)
            self.assertEqual(vvr_id, UUID(hex=getattr(target_vvr, 'uuid')))

    def test_keystone_notification_delete(self):
        """Test deleting a VVR following a tenant deletion notification."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()

            notif_handler = KeystoneNotificationHandler(self._driver)
            new_tenant_id = uuid4()
            notif_result = notif_handler.info({},
                                              'identity.localhost',
                                              'identity.project.created',
                                              {'resource_info':
                                               new_tenant_id.get_hex()},
                                              {})
            self.assertEqual(NotificationResult.HANDLED, notif_result)

            vvr_id = map_tenant_to_vvr_id(new_tenant_id)
            vvrs_proxy = self._driver._checked_vvrs_proxy(new_tenant_id)
            target_vvr = vvrs_proxy.get_vvr(vvr_id)
            self.assertEqual(vvr_id, UUID(hex=getattr(target_vvr, 'uuid')))

            notif_result = notif_handler.info({},
                                              'identity.localhost',
                                              'identity.project.deleted',
                                              {'resource_info':
                                               new_tenant_id.get_hex()},
                                              {})
            self.assertEqual(NotificationResult.HANDLED, notif_result)

            self.assertRaises(exception.NotFound,
                              lambda: vvrs_proxy.get_vvr(vvr_id))

    def test_keystone_notification_delete_exported_device(self):
        """Test deleting a VVR in the presence of an exported device."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()

            notif_handler = KeystoneNotificationHandler(self._driver)
            new_tenant_id = uuid4()
            notif_result = notif_handler.info({},
                                              'identity.localhost',
                                              'identity.project.created',
                                              {'resource_info':
                                               new_tenant_id.get_hex()},
                                              {})
            self.assertEqual(NotificationResult.HANDLED, notif_result)

            vvr_id = map_tenant_to_vvr_id(new_tenant_id)
            vvrs_proxy = self._driver._checked_vvrs_proxy(new_tenant_id)
            target_vvr = vvrs_proxy.get_vvr(vvr_id)
            self.assertEqual(vvr_id, UUID(hex=getattr(target_vvr, 'uuid')))

            for dev_nb in range(1, 5):
                test_vol = _new_test_volume(new_tenant_id)
                test_vol['display_name'] = 'Vol-' + str(dev_nb)
                self._driver.create_volume(test_vol)

                if (dev_nb % 2) == 0:
                    continue
                update = self._driver.create_export({}, test_vol)
                self.assertTrue(update['provider_location'])

            notif_result = notif_handler.info({},
                                              'identity.localhost',
                                              'identity.project.deleted',
                                              {'resource_info':
                                               new_tenant_id.get_hex()},
                                              {})
            self.assertEqual(NotificationResult.HANDLED, notif_result)

            self.assertRaises(exception.NotFound,
                              lambda: vvrs_proxy.get_vvr(vvr_id))

    def test_keystone_notification_delete_stopped_vvr(self):
        """Test deleting a stopped VVR."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()

            notif_handler = KeystoneNotificationHandler(self._driver)
            new_tenant_id = uuid4()
            notif_result = notif_handler.info({},
                                              'identity.localhost',
                                              'identity.project.created',
                                              {'resource_info':
                                               new_tenant_id.get_hex()},
                                              {})
            self.assertEqual(NotificationResult.HANDLED, notif_result)

            vvr_id = map_tenant_to_vvr_id(new_tenant_id)
            vvrs_proxy = self._driver._checked_vvrs_proxy(new_tenant_id)
            target_vvr = vvrs_proxy.get_vvr(vvr_id)
            self.assertEqual(vvr_id, UUID(hex=getattr(target_vvr, 'uuid')))

            updated_vvr = target_vvr.stop()
            self.assertFalse(updated_vvr.started)

            notif_result = notif_handler.info({},
                                              'identity.localhost',
                                              'identity.project.deleted',
                                              {'resource_info':
                                               new_tenant_id.get_hex()},
                                              {})
            self.assertEqual(NotificationResult.HANDLED, notif_result)

            self.assertRaises(exception.NotFound,
                              lambda: vvrs_proxy.get_vvr(vvr_id))

    def test_keystone_notification_delete_ignore_missing(self):
        """Test handling a deletion notification for a non-existent VVR."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()

            notif_handler = KeystoneNotificationHandler(self._driver)
            new_tenant_id = uuid4()
            notif_result = notif_handler.info({},
                                              'identity.localhost',
                                              'identity.project.deleted',
                                              {'resource_info':
                                               new_tenant_id.get_hex()},
                                              {})
            self.assertEqual(NotificationResult.HANDLED, notif_result)

            vvr_id = map_tenant_to_vvr_id(new_tenant_id)
            vvrs_proxy = self._driver._checked_vvrs_proxy(new_tenant_id)
            self.assertRaises(exception.NotFound,
                              lambda: vvrs_proxy.get_vvr(vvr_id))

    def test_keystone_notification_ignore(self):
        """Test miscellaneous ignored notifications."""
        with ROOT_MOCK:
            self._driver.do_setup({})
            self._driver.check_for_setup_error()

            notif_handler = KeystoneNotificationHandler(self._driver)
            tenant_id_hex = uuid4().get_hex()

            # missing resource ID
            self.assertIsNone(notif_handler.info({},
                                                 'identity.localhost',
                                                 'identity.project.created',
                                                 {},
                                                 {}))

            # identity service, but not on project topic
            self.assertIsNone(notif_handler.info({},
                                                 'identity.localhost',
                                                 'identity.user',
                                                 {'resource_info':
                                                  tenant_id_hex},
                                                 {}))

            # identity service, project topic, but unknown operation
            self.assertIsNone(notif_handler.info({},
                                                 'identity.localhost',
                                                 'identity.project.unknown',
                                                 {'resource_info':
                                                  tenant_id_hex},
                                                 {}))

            # another service entirely
            self.assertIsNone(notif_handler.info({},
                                                 'image.localhost',
                                                 'image',
                                                 {},
                                                 {}))
