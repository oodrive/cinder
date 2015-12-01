
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
Volume Driver implementation for the eguan storage backend.

@author: pwehrle
"""


from cinder.exception import NotFound
from cinder.exception import VolumeBackendAPIException
from cinder.volume import driver
from cinder.volume.drivers.eguan.resource_proxies import TaskRes
from cinder.volume.drivers.eguan.resource_proxies import VvrRes
from cinder.volume.drivers.eguan.resource_proxies import VvrsRes
from oslo_config import cfg
from oslo_log import log
from oslo_messaging import get_notification_listener
from oslo_messaging import get_transport
from oslo_messaging.notify.dispatcher import NotificationResult
from oslo_messaging import Target
import requests
from requests.exceptions import ConnectionError
from requests.exceptions import Timeout
from time import sleep
from uuid import UUID


LOG = log.getLogger(__name__)

eguan_opts = {cfg.MultiStrOpt('vold_rest_uri', default='')}
CONF = cfg.CONF
CONF.register_group(cfg.OptGroup(name='eguan', title='eguan options'))
CONF.register_opts(eguan_opts, 'eguan')


def checked_get_tenant_id(dct):
    """Gets the tenant (or project) ID.

        Extracts it from the given dictionary,
        throwing an exception if none is found.

    """
    tenant_id = dct.get('project_id')
    if tenant_id is None:
        raise VolumeBackendAPIException('Missing project ID')
    return UUID(hex=tenant_id)


def map_tenant_to_vvr_id(tenant_id):
    """Maps the tenant ID to a suitable VVR ID."""
    if not isinstance(tenant_id, UUID):
        raise ValueError('tenant_id must be a UUID')
    return UUID(int=tenant_id.int ^ 0xffffffffffffffffffffffffffffffff)


class KeystoneNotificationHandler(object):
    """Endpoint for keystone notifications."""

    def __init__(self, eguan_driver):
        self._eguan_driver = eguan_driver

    def info(self, ctxt, publisher_id, event_type, payload, metadata):
        """Handles all info-level notifications."""
        LOG.debug("Got event of type %s, payload %s" % (event_type, payload))
        evt_type_list = event_type.split('.')
        if evt_type_list[0] != 'identity' or evt_type_list[1] != 'project':
            return None

        project_id = payload.get('resource_info')
        if not project_id:
            return None

        tenant_id = UUID(hex=project_id)

        if evt_type_list[2] == 'deleted':
            LOG.debug("Delete project %s" % project_id)
            try:
                target_vvr = self._eguan_driver._resolve_vvr(tenant_id)

                if target_vvr.started:
                    for device in target_vvr.get_devices_list():
                        if not device.get('active'):
                            continue

                        dev_id = device.get('uuid')
                        target_dev = target_vvr.get_device(dev_id)

                        try:
                            target_dev.remove_export()
                        except VolumeBackendAPIException as ve:
                            LOG.warn('Could not remove export for '
                                     + "device %s, exception: %s"
                                     % (dev_id, ve))
                    target_vvr.stop()

                target_vvr.delete()

            except VolumeBackendAPIException as ve:
                LOG.warn("Could not delete project %s, exception: %s"
                         % (project_id, ve))
            finally:
                return NotificationResult.HANDLED

        if evt_type_list[2] == 'created':
            LOG.debug("Create project %s" % project_id)
            self._eguan_driver.pending_project_creations[project_id] = None
            try:
                target_vvrs = self._eguan_driver._checked_vvrs_proxy(tenant_id)
                vvr_id = map_tenant_to_vvr_id(tenant_id)

                try:
                    target_vvrs.get_vvr(vvr_id)
                    return NotificationResult.HANDLED
                except NotFound:
                    LOG.debug("VVR does not yet exist; vvr_id=%s" % vvr_id)

                owner_id = target_vvrs.tenant_id

                def monitor(task_uri):
                    self._eguan_driver._monitor_creation_task(task_uri,
                                                              owner_id,
                                                              tenant_id)

                target_vvrs.create_vvr(vvr_id, monitor)

            except VolumeBackendAPIException as ve:
                LOG.warn("Could not create project %s, exception %s"
                         % (project_id, ve))
            finally:
                self._eguan_driver.pending_project_creations.pop(project_id)
                return NotificationResult.HANDLED


class EguanDriver(driver.VolumeDriver):
    """The main driver class."""

    VERSION = "0.6"

    DEFAULT_VOLUME_STATS = {'driver_version': VERSION,
                            'free_capacity_gb': 'unknown',
                            'reserved_percentage': 0,
                            'storage_protocol': 'iSCSI',
                            'total_capacity_gb': 'unknown',
                            'vendor_name': 'Oodrive',
                            'volume_backend_name': 'eguan'}

    def __init__(self, *args, **kwargs):
        """Constructs the driver instance."""
        super(EguanDriver, self).__init__(*args, **kwargs)
        self._vvrs_res_map = {}
        self._tenant_vvr_map = {}
        self._snapshot_vvr_map = {}
        self._device_vvr_map = {}
        self._volume_stats = EguanDriver.DEFAULT_VOLUME_STATS.copy()

        if self.configuration:
            backend_name = (self.configuration.
                            safe_get('volume_backend_name') or
                            'eguan')
            self._volume_stats['volume_backend_name'] = backend_name

        self._notif_transport = get_transport(CONF)
        self.pending_project_creations = {}

    def do_setup(self, context):
        """Initialize the volume driver while starting."""
        rest_uri_map = [conf_val.split('|') for conf_val in
                        CONF.eguan.vold_rest_uri]
        try:
            self._vvrs_res_map = {UUID(hex=tenant_id):
                                  VvrsRes(target_url + '/vvrs',
                                          tenant_id=UUID(hex=tenant_id))
                                  for tenant_id, target_url in rest_uri_map}
        except ValueError:
            raise VolumeBackendAPIException('Invalid tenant ID; '
                                            "ID list=%s" % rest_uri_map)
        if len(self._vvrs_res_map) == 0:
            raise VolumeBackendAPIException('No backend server configured')

        targets = [Target(topic="notifications")]
        handlers = [KeystoneNotificationHandler(self)]
        listener = get_notification_listener(self._notif_transport,
                                             targets,
                                             handlers,
                                             executor='eventlet',
                                             allow_requeue=True)
        listener.start()

    def check_for_setup_error(self):
        """Return an error if prerequisites aren't met."""
        LOG.debug("Checking for unreachable hosts in %s" % self._vvrs_res_map)
        for t_id, vvrs_res in self._vvrs_res_map.iteritems():
            ret_status = None
            try:
                ret_status = (requests.get(vvrs_res.uri,
                                           headers={'content-type':
                                                    'application/json'},
                                           params={'ownerId': str(t_id)},
                                           timeout=10)
                              .status_code)
                if ret_status == 403:
                    raise VolumeBackendAPIException('Unauthorized on backend' +
                                                    "; URI=%s, ownerId=%s" %
                                                    (vvrs_res.uri, t_id))
            except ConnectionError:
                raise VolumeBackendAPIException('Unreachable REST backend.')
            except Timeout:
                raise VolumeBackendAPIException('Timeout connecting ' +
                                                'to backend.')

    def create_volume(self, volume):
        """Creates a volume.

        Can optionally return a Dictionary of
        changes to the volume object to be persisted.

        """
        tenant_id = checked_get_tenant_id(volume)

        target_vvr = self._resolve_vvr(tenant_id=tenant_id)

        root_snap = target_vvr.get_root_snapshot()
        root_snap.new_device(volume)

    def create_volume_from_snapshot(self, volume, snapshot):
        """Creates a volume from a snapshot."""
        target_snap_id = UUID(hex=snapshot.get('id'))

        tenant_id = checked_get_tenant_id(snapshot)

        target_vvr = self._resolve_vvr(tenant_id=tenant_id)

        target_snapshot = target_vvr.get_snapshot(target_snap_id)
        if target_snapshot is None:
            raise VolumeBackendAPIException('Target snapshot for device' +
                                            ' creation missing; tenant_id=' +
                                            tenant_id + 'snapshot_id=' +
                                            target_snap_id)
        target_snapshot.new_device(volume)

    def create_cloned_volume(self, volume, src_vref):
        """Creates a clone of the specified volume."""
        src_tenant_id = checked_get_tenant_id(src_vref)
        dst_tenant_id = checked_get_tenant_id(volume)
        if src_tenant_id != dst_tenant_id:
            raise VolumeBackendAPIException('Tenant ID mismatch;' +
                                            ' src_tenant_id=' +
                                            str(src_tenant_id) +
                                            'dst_tenant_id=' +
                                            str(dst_tenant_id))
        src_vol_id = UUID(hex=src_vref['id'])

        target_vvr = self._resolve_vvr(tenant_id=src_tenant_id)

        source_vol = target_vvr.get_device(src_vol_id)
        if source_vol is None:
            target_vvr_id = getattr(target_vvr, target_vvr.id_attr)
            raise VolumeBackendAPIException('Source volume for volume ' +
                                            'cloning missing; ' +
                                            'src_tenant_id=' +
                                            str(src_tenant_id) +
                                            'vvr_id=' + target_vvr_id +
                                            'volume_id=' + src_vol_id)
        source_vol.create_clone(volume)

    def delete_volume(self, volume):
        """Deletes a volume."""
        tenant_id = checked_get_tenant_id(volume)
        target_vol_id = UUID(hex=volume['id'])

        target_vvr = self._resolve_vvr(tenant_id=tenant_id)
        target_volume = target_vvr.get_device(target_vol_id)
        target_volume.delete()

    def extend_volume(self, volume, new_size):
        """Extends a volume to the given size."""
        tenant_id = checked_get_tenant_id(volume)
        target_vol_id = UUID(hex=volume['id'])

        target_vvr = self._resolve_vvr(tenant_id=tenant_id)
        target_volume = target_vvr.get_device(target_vol_id)
        target_volume.resize(new_size)

    def create_snapshot(self, snapshot):
        """Creates a snapshot."""

        tenant_id = checked_get_tenant_id(snapshot)

        target_vol_id = UUID(hex=snapshot.get('volume_id'))
        if target_vol_id is None:
            raise VolumeBackendAPIException('Missing device ID for snapshot' +
                                            ' creation; tenant_id=' +
                                            tenant_id + ', snapshot=' +
                                            snapshot)

        target_vvr = self._resolve_vvr(tenant_id=tenant_id)
        target_volume = target_vvr.get_device(target_vol_id)
        target_volume.new_snapshot(snapshot)

    def delete_snapshot(self, snapshot):
        """Deletes a snapshot."""
        tenant_id = checked_get_tenant_id(snapshot)
        target_snap_id = UUID(hex=snapshot['id'])

        target_vvr = self._resolve_vvr(tenant_id=tenant_id)

        target_snapshot = target_vvr.get_snapshot(target_snap_id)
        target_snapshot.delete()

    def create_export(self, context, volume):
        """Exports the volume.

        Can optionally return a Dictionary of changes
        to the volume object to be persisted.

        """
        return self._resolve_volume(volume).export(volume)

    def ensure_export(self, context, volume):
        """Synchronously recreates an export for a volume."""
        self._resolve_volume(volume).ensure_export(volume)

    def remove_export(self, context, volume):
        """Removes an export for a volume."""
        self._resolve_volume(volume).remove_export()

    def initialize_connection(self, volume, connector):
        """Allow connection to connector and return connection info."""

        client_ip = connector.get('ip')

        client_protocol = 'iscsi'
        if 'initiator' in connector:
            client_protocol = 'iscsi'
        if 'nbd_client_id' in connector:
            client_protocol = 'nbd'

        target_volume = self._resolve_volume(volume)
        connect_info = target_volume.initialize_connection(client_ip,
                                                           client_protocol)

        volume_type = connect_info['driver_volume_type']
        result = {'driver_volume_type': volume_type}
        result['data'] = {'access_mode': ('ro' if getattr(target_volume,
                                                          'readOnly')
                                          else 'rw')}
        result['data']['target_portal'] = ("%s:%s" %
                                           (connect_info['server_address'],
                                            connect_info['server_port']))
        if volume_type == 'iscsi':
            result['data'].update({'target_iqn': connect_info.get('iqn') or '',
                                   'volume_id': '0',
                                   'target_lun': '0'})
        elif volume_type == 'nbd':
            result['data'].update({'nbd_dev_name':
                                   connect_info['devName']})
        return result

    def terminate_connection(self, volume, connector, **kwargs):
        """Disallow connection from connector"""
        self._resolve_volume(volume).terminate_connection(connector, **kwargs)

    def validate_connector(self, connector):
        """Fail if connector doesn't contain all the data needed by driver"""
        if 'ip' not in connector:
            err_msg = ('Eguan volume driver needs the IP in the connector.')
            LOG.error(err_msg)
            raise VolumeBackendAPIException(data=err_msg)

    def attach_volume(self, context, volume, instance_uuid, host_name,
                      mountpoint):
        """Callback for volume attached to instance or host."""
        # check the volume exists and is reachable
        self._resolve_volume(volume)
        #TODO(pwehrle): add check for same subnet between host and server

    def detach_volume(self, context, volume):
        """Callback for volume detached."""
        # check the volume exists and is reachable
        self._resolve_volume(volume)

        if not volume.get('attached_host'):
            LOG.error('Not attached.')

    def get_volume_stats(self, refresh=False):
        """Return the current state of the volume service.

        If 'refresh' is True, run the update first.

        """
        result_data = self._volume_stats
        if not refresh:
            return result_data

        #TODO(pwehrle): update with monitoring data from all REST backends
        result_data['total_capacity_gb'] = 'infinite'

        return result_data

    def migrate_volume(self, context, volume, host):
        """Migrate the volume to the specified host.

        Returns a boolean indicating whether the migration occurred, as well as
        model_update.
        """
        return (False, None)

    def backup_volume(self, context, backup, backup_service):
        """Create a new backup from an existing volume."""
        pass

    def restore_backup(self, context, backup, volume, backup_service):
        """Restore an existing backup to a new or existing volume."""
        pass

    def get_volume(self, tenant_id, volume_id):
        """Gets a volume identified by its tenant ID and volume ID."""
        target_vvr = self._resolve_vvr(tenant_id=tenant_id)
        result_device = target_vvr.get_device(volume_id)
        return result_device

    def get_snapshot(self, tenant_id, snapshot_id):
        """Gets a snapshot identified by its tenant ID and snapshot ID."""
        target_vvr = self._resolve_vvr(tenant_id=tenant_id)
        result_snapshot = target_vvr.get_snapshot(snapshot_id)
        return result_snapshot

    def _resolve_volume(self, volume):
        tenant_id = checked_get_tenant_id(volume)
        target_vol_id = UUID(hex=volume['id'])

        target_vvr = self._resolve_vvr(tenant_id=tenant_id)
        return target_vvr.get_device(target_vol_id)

    def _checked_vvrs_proxy(self, tenant_id):
        target_vvrs_res = self._vvrs_res_map.get(tenant_id)
        if not target_vvrs_res and len(self._vvrs_res_map) > 0:
            #TODO(pwehrle): find a mapping that's impervious to config changes
            sel_min = min([(owner_id.int - tenant_id.int)
                           for owner_id in self._vvrs_res_map.keys()])
            sel_id = UUID(int=(tenant_id.int + sel_min))
            target_vvrs_res = self._vvrs_res_map.get(sel_id)

        if target_vvrs_res is None:
            raise VolumeBackendAPIException('Missing backend endpoint;' +
                                            ' tenant_id=' + str(tenant_id))
        return target_vvrs_res

    def _resolve_vvr(self, tenant_id=None):
        if tenant_id is None:
            return None

        # get the Vvrs resource
        vvrs_res = self._checked_vvrs_proxy(tenant_id)
        if not vvrs_res:
            return None

        vvr_id = map_tenant_to_vvr_id(tenant_id)
        try:
            return vvrs_res.get_vvr(vvr_id)
        except NotFound:
            project_id = tenant_id.get_hex()
            while project_id in self.pending_project_creations:
                task_res = self.pending_project_creations[project_id]
                if task_res:
                    return task_res.wait_for_result()
                sleep(0.5)
            try:
                return vvrs_res.get_vvr(vvr_id)
            except NotFound:
                raise VolumeBackendAPIException('Missing VVR; tenant_id=' +
                                                str(tenant_id))

    def _monitor_creation_task(self, task_uri, owner_id, tenant_id):
        target_task = TaskRes(task_uri, owner_id, result_type=VvrRes)
        self.pending_project_creations[tenant_id.get_hex()] = target_task
