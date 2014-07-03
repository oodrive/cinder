
# Copyright (c) 2012 - 2014 Oodrive
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
Client resource proxies for the eguan storage backend REST interface.

@author: pwehrle
"""

from cinder.exception import NotFound
from cinder.exception import VolumeBackendAPIException
from cinder.openstack.common import log
from cinder import units

import json
import requests
import time
from urllib3 import util
from uuid import UUID


LOG = log.getLogger(__name__)

VVR_LIST_KEY = 'VersionedVolumeRepository'
SNAPSHOT_LIST_KEY = 'Snapshot'
DEVICE_LIST_KEY = 'Device'
TASK_LIST_KEY = 'Task'


class ResourceProxy(object):
    def __init__(self, uri=None, tenant_id=None, extra_attr_dct=None,
                 list_key=None, id_attr=None, target_type=None):
        util.parse_url(uri)
        self.uri = uri
        if not isinstance(tenant_id, UUID):
            raise ValueError('tenant_id must be a UUID')
        self.tenant_id = tenant_id
        self.list_key = list_key
        self.id_attr = id_attr
        self.target_type = target_type
        # TODO(pwehrle): add validity check for attributes
        if extra_attr_dct:
            self.__dict__.update(extra_attr_dct)

    @classmethod
    def get_from_task(cls, tenant_id, task_response, result_type, mon=None,
                      timeout=10):
        if task_response.status_code != 202:
            raise VolumeBackendAPIException('Task creation failed;' +
                                            "action=%s, status_code=%s" %
                                            (task_response.url,
                                             task_response.status_code))
        task_uri = task_response.headers.get('location')
        if not task_uri:
            raise VolumeBackendAPIException('No task_res URI;' +
                                            "action=%s, status_code=%s" %
                                            (task_response.url,
                                             task_response.status_code))
        if mon:
            mon(task_uri)
        task_res = TaskRes(task_uri, tenant_id, result_type=result_type)
        return task_res.wait_for_result(timeout)

    def get_query(self):
        return {'ownerId': str(self.tenant_id)}

    def get_headers(self):
        return {'content-type': 'application/json',
                'accept': 'application/json'}

    def read_resource(self, object_func=None):
        response = requests.get(self.uri, params=self.get_query(),
                                headers=self.get_headers())
        if response.status_code != 200:
            raise NotFound('Resource not available; ' +
                           "uri=%s, response_status=%s" %
                           (self.uri, response.status_code))
        else:
            return json.loads(response.content,
                              object_hook=object_func)

    def update_res(self, dct):
        self.__dict__.update(dct)

    def as_res_without_id(self, dct):
        if self.target_type is None or len(dct) == 0:
            return dct
        else:
            return self.target_type(self.uri,
                                    tenant_id=self.tenant_id,
                                    extra_attr_dct=dct)

    def as_res_with_id(self, dct):
        if self.target_type is None or len(dct) == 0:
            return dct
        else:
            return self.target_type(self.uri + '/' + dct[self.id_attr],
                                    tenant_id=self.tenant_id,
                                    extra_attr_dct=dct)

    def as_res_list(self, dct):
        if self.list_key in dct:
            return [self.as_res_with_id(res_dct)
                    for res_dct in dct.get(self.list_key)]
        return dct


class VvrsRes(ResourceProxy):
    def __init__(self, uri, tenant_id=None):
        super(VvrsRes, self).__init__(uri=uri, tenant_id=tenant_id,
                                      list_key=VVR_LIST_KEY,
                                      id_attr='uuid', target_type=VvrRes)

    def get_vvr(self, vvr_id):
        if vvr_id is None:
            raise VolumeBackendAPIException('Missing VVR ID; tenant_id=' +
                                            self.tenant_id)
        vvr_res = VvrRes(self.uri + '/' + str(vvr_id),
                         tenant_id=self.tenant_id)
        return vvr_res.read_resource(vvr_res.as_res_without_id)

    def create_vvr(self, uuid, monitor=None):
        params = self.get_query()
        params.update({'uuid': str(uuid),
                       'name': 'cinder'})
        response = requests.post(self.uri + '/action/createVvr',
                                 params=params,
                                 headers=self.get_headers())
        return ResourceProxy.get_from_task(self.tenant_id, response, VvrRes,
                                           monitor, 20)


class VvrRes(ResourceProxy):
    def __init__(self, uri, tenant_id=None, extra_attr_dct=None):
        super(VvrRes, self).__init__(uri=uri, tenant_id=tenant_id,
                                     extra_attr_dct=extra_attr_dct,
                                     id_attr='uuid', target_type=VvrRes)

    def start(self):
        response = requests.post(self.uri + '/action/start',
                                 params=self.get_query(),
                                 headers=self.get_headers())
        return ResourceProxy.get_from_task(self.tenant_id, response, VvrRes)

    def stop(self):
        response = requests.post(self.uri + '/action/stop',
                                 params=self.get_query(),
                                 headers=self.get_headers())
        return ResourceProxy.get_from_task(self.tenant_id, response, VvrRes)

    def get_snapshot(self, snapshot_id):
        snap_res = SnapshotRes(uri=self.uri + '/snapshots/' + str(snapshot_id),
                               tenant_id=self.tenant_id)
        return snap_res.read_resource(snap_res.as_res_without_id)

    def get_root_snapshot(self):
        root_snap_res = SnapshotRes(uri=self.uri + '/root',
                                    tenant_id=self.tenant_id)
        return root_snap_res.read_resource(root_snap_res.as_res_without_id)

    def get_devices_list(self):
        devs_res = ResourceProxy(uri=self.uri + '/devices',
                                 tenant_id=self.tenant_id,
                                 list_key=DEVICE_LIST_KEY,
                                 id_attr='uuid')
        return devs_res.read_resource(devs_res.as_res_list)

    def get_device(self, device_id):
        dev_res = DeviceRes(uri=self.uri + '/devices/' + str(device_id),
                            tenant_id=self.tenant_id)
        return dev_res.read_resource(dev_res.as_res_without_id)

    def delete(self):
        response = requests.delete(self.uri,
                                   params=self.get_query(),
                                   headers=self.get_headers())
        if response.status_code == 202:
            return ResourceProxy.get_from_task(self.tenant_id, response, None)


class DeviceRes(ResourceProxy):
    def __init__(self, uri, tenant_id=None, extra_attr_dct=None):
        super(DeviceRes, self).__init__(uri=uri, tenant_id=tenant_id,
                                        extra_attr_dct=extra_attr_dct,
                                        id_attr='uuid',
                                        target_type=DeviceRes)

    def read_resource(self, object_func=None):
        result = super(DeviceRes, self).read_resource(object_func=object_func)
        if hasattr(result, 'active') and getattr(result, 'active'):
            setattr(result, 'provider_location', self._get_provider_location())
        return result

    def new_snapshot(self, snapshot):
        snap_id = snapshot.get('id')
        params = self.get_query()
        params.update({'name': snapshot.get('display_name') or
                       (UUID(hex=snap_id).get_hex()),
                       'uuid': UUID(hex=snap_id) if snap_id else '',
                       'description': snapshot.get('display_description') or
                       ''})
        response = requests.post(self.uri + '/action/newSnapshot',
                                 params=params,
                                 headers=self.get_headers())
        return ResourceProxy.get_from_task(self.tenant_id, response,
                                           SnapshotRes)

    def create_clone(self, cloned_vol):
        clone_id = cloned_vol.get('id')
        params = self.get_query()
        params.update({'name': cloned_vol.get('display_name') or
                       (UUID(hex=clone_id).get_hex()),
                       'uuid': UUID(hex=clone_id) if clone_id else '',
                       'description': cloned_vol.get('display_description') or
                       ''})
        response = requests.post(self.uri + '/action/clone',
                                 params=params,
                                 headers=self.get_headers())
        return ResourceProxy.get_from_task(self.tenant_id, response,
                                           DeviceRes)

    def delete(self):
        response = requests.delete(self.uri,
                                   params=self.get_query(),
                                   headers=self.get_headers())
        if response.status_code == 202:
            return ResourceProxy.get_from_task(self.tenant_id, response, None)

    def resize(self, size):
        floored_size = long(max(size, 0.1) * units.GiB)
        params = self.get_query()
        params.update({'size': floored_size})
        response = requests.post(self.uri + '/action/resize',
                                 params=params,
                                 headers=self.get_headers())
        return ResourceProxy.get_from_task(self.tenant_id, response, DeviceRes)

    def export(self, volume):
        self.read_resource(self.update_res)
        if not getattr(self, 'active'):
            params = self.get_query()
            params.update({'readOnly': 'true'
                           if volume.get('readOnly') else 'false'})
            response = requests.post(self.uri + '/action/activate',
                                     params=params,
                                     headers=self.get_headers())
            ResourceProxy.get_from_task(self.tenant_id, response, None)
        self.read_resource(self.update_res)
        return {'provider_location': self._get_provider_location()}

    def _get_provider_location(self):
        conn_init_resp = self.initialize_connection('127.0.0.1', 'iscsi')
        return ("%(scheme)s:%(host)s:%(port)s" %
                {'scheme': conn_init_resp.get('driver_volume_type'),
                 'host': conn_init_resp.get('server_address'),
                 'port': conn_init_resp.get('server_port')})

    def ensure_export(self, volume):
        self.read_resource(self.update_res)
        if not getattr(self, 'active'):
            self.export(volume)

    def remove_export(self):
        self.read_resource(self.update_res)
        if not getattr(self, 'active'):
            return self
        response = requests.post(self.uri + '/action/deactivate',
                                 params=self.get_query(),
                                 headers=self.get_headers())
        return ResourceProxy.get_from_task(self.tenant_id, response, DeviceRes)

    def initialize_connection(self, client_ip, client_protocol):
        self.read_resource(self.update_res)
        if not getattr(self, 'active'):
            LOG.warn('Exporting volume to allow connection initialization.' +
                     'volume_id=' + getattr(self, self.id_attr))
            self.ensure_export(self.__dict__)
        params = self.get_query()
        params.update({'ip': client_ip, 'clientProtocol': client_protocol})
        resp = requests.get(self.uri + '/connection', params=params,
                            headers=self.get_headers())
        if resp.status_code != 200:
            raise VolumeBackendAPIException('Error getting connection info;' +
                                            (' status=%d, message=%s' %
                                             (resp.status_code, resp.content)))
        return json.loads(resp.content)

    def terminate_connection(self, connector, **kw_args):
        self.read_resource(self.update_res)
        if not getattr(self, 'active'):
            return
        #TODO(pwehrle): add call to disconnect action as soon as there's
        #one available


class SnapshotRes(ResourceProxy):
    def __init__(self, uri, tenant_id=None, extra_attr_dct=None):
        super(SnapshotRes, self).__init__(uri=uri, tenant_id=tenant_id,
                                          extra_attr_dct=extra_attr_dct,
                                          id_attr='uuid',
                                          target_type=SnapshotRes)

    def new_device(self, volume):
        floored_size = max(volume.get('size'), 0.1) * units.GiB
        vol_id = volume.get('id')
        params = self.get_query()
        params.update({'name': volume.get('display_name') or
                       (UUID(hex=vol_id).get_hex()),
                       'uuid': UUID(vol_id) if vol_id else '',
                       'size': floored_size,
                       'description': volume.get('display_description') or ''})
        response = requests.post(self.uri + '/action/newDevice',
                                 params=params,
                                 headers=self.get_headers())
        return ResourceProxy.get_from_task(self.tenant_id, response, DeviceRes)

    def delete(self):
        response = requests.delete(self.uri,
                                   params=self.get_query(),
                                   headers=self.get_headers())
        return ResourceProxy.get_from_task(self.tenant_id, response, None)


class TaskRes(ResourceProxy):
    def __init__(self, uri, tenant_id=None, extra_attr_dct=None,
                 result_type=ResourceProxy):
        super(TaskRes, self).__init__(uri=uri, tenant_id=tenant_id,
                                      extra_attr_dct=extra_attr_dct,
                                      id_attr='uuid', target_type=TaskRes)
        self._result_type = result_type
        self.state = 'PENDING'

    def get_result(self):
        result_ref = getattr(self, 'resultRef')
        if result_ref is None:
            return None
        if self.state == 'DONE':
            result_res = self._result_type(result_ref,
                                           tenant_id=self.tenant_id)
            return result_res.read_resource(result_res.as_res_without_id)

    def wait_for_result(self, timeout_s=10):
        timeout = time.time() + timeout_s
        while self.state != 'DONE' and time.time() < timeout:
            time.sleep(0.1)
            self.read_resource(self.update_res)
            if self.state in ('FAILED', 'CANCELED'):
                raise VolumeBackendAPIException('Task failed or canceled;' +
                                                "taskId=%s, state=%s" %
                                                (getattr(self, 'uuid'),
                                                 self.state))
        if self._result_type is None:
            return None
        return self.get_result()
