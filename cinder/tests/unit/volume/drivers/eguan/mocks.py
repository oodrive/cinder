
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
Vold REST mock classes.

@author: pwehrle
"""


from cinder.volume.drivers.eguan import resource_proxies
from httmock import all_requests
from httmock import HTTMock
from httmock import urlmatch
import json
from oslo_log import log
import re
from urllib3 import util
from requests import Session
from threading import RLock
from threading import Thread
from time import sleep
from urlparse import parse_qsl
from uuid import uuid4


LOG = log.getLogger(__name__)

UUID_REGEX = r'[0-9a-f\-]{36}'
VVRS_BASE_REGEX = r'^/storage/vvrs'
VVR_ID_REGEX = VVRS_BASE_REGEX + '/(' + UUID_REGEX + ')/?'
VVRS_TASKS_REGEX = VVRS_BASE_REGEX + '/tasks'
TASK_ID_REGEX = r'tasks/(' + UUID_REGEX + ')/?'
SNAPSHOT_ID_REGEX = r'snapshots/(' + UUID_REGEX + ')/?'
DEVICE_ID_REGEX = r'devices/(' + UUID_REGEX + ')/?'
ACTION_REGEX = r'action/(\w+)$'
SUBRES_REGEX = r'/(\w+)$'


def extract_query_param(param_name, query):
    """Extract the value for the given parameter name from a query string."""
    query_split = get_query_params(query)
    return query_split.get(param_name)


def get_query_params(query):
    return dict(parse_qsl(query))


def extract_tenant_id(url):
    """Extract the tenant ID from a parsed URL."""
    return extract_query_param('ownerId', url.query)


def extract_path_element(path, match_regex):
    matches = re.search(match_regex, path)
    if matches is None:
        return None
    return matches.groups()[0], matches.end()


class MockResource(object):
    def __init__(self, tenant_id, res_url, repr_dct):
        self.tenant_id = tenant_id
        self.res_url = res_url
        self.repr_dct = repr_dct

    def get_repr(self):
        return self.repr_dct


class MockVvrsRes(MockResource):
    """Mock facade for a vvrs resource."""
    def __init__(self, tenant_id, res_url, repr_dct):
        super(MockVvrsRes, self).__init__(tenant_id,
                                          res_url + '/vvrs',
                                          repr_dct)
        self.base_url = util.parse_url(res_url)
        self.vvrs = {}
        self.tasks = {}

    @all_requests
    def respond_all(self, url, request):
        """Respond to all requests not matched by another method."""
        if not (url.path.startswith(self.base_url.path)):
            return {'status_code': 403, 'content': 'forbidden'}
        if url.path.startswith(self.base_url.path + '/vvrs'):
            vvrs_mock = HTTMock(self.match_vvrs,
                                self.match_tasks,
                                self.match_vvr,
                                self.match_create_vvr,
                                self.match_task)
            with vvrs_mock:
                s = Session()
                return s.send(request)
        else:
            return {'status_code': 404, 'content': 'Not found'}

    @urlmatch(path=VVRS_BASE_REGEX + '/?$')
    def match_vvrs(self, url, request):
        LOG.debug('Matched Vvrs resource; url=' + str(url))
        return {'status_code': 200,
                'content': json.dumps({'VersionedVolumeRepository':
                                       [vvr.get_repr() for vvr
                                        in self.vvrs.values()]})}

    @urlmatch(path=VVRS_TASKS_REGEX + '/?$')
    def match_tasks(self, url, request):
        return {'status_code': 200,
                'content': json.dumps({resource_proxies.TASK_LIST_KEY:
                                       [task.get_repr()
                                        for task in self.tasks]})}

    @urlmatch(path=VVRS_BASE_REGEX + '/' + TASK_ID_REGEX)
    def match_task(self, url, request):
        task_match = extract_path_element(url.path, TASK_ID_REGEX)
        if task_match is None:
            return {'status_code': 404, 'content': 'Not found'}

        target_task = self.tasks.get(task_match[0])
        if target_task is None:
            return {'status_code': 404, 'content': 'Not found'}

        return {'status_code': 200,
                'content': json.dumps(target_task.get_repr())}

    @urlmatch(path=r'/storage/vvrs/action/createVvr$')
    def match_create_vvr(self, url, request):
        LOG.debug('Matched Vvr create resource; url=' + str(url))

        def create_vvr(self, uuid):
            new_vvr_id = uuid or str(uuid4())
            new_vvr = MockVvrRes(self.tenant_id,
                                 self.res_url + '/' + new_vvr_id,
                                 {'instanceCount': 1,
                                  'uuid': new_vvr_id,
                                  'started': True,
                                  'quota': 9223372036854775807,
                                  'ownerid': self.tenant_id,
                                  'initialized': True,
                                  'size': 0})
            self.vvrs[new_vvr_id] = new_vvr
            return new_vvr.res_url

        new_uuid = extract_query_param('uuid', url.query)
        task_id = str(uuid4())
        task_res = MockTaskRes(self.tenant_id, self.res_url +
                               '/tasks/' + task_id,
                               repr_dct={'uuid': task_id},
                               task_action=lambda: create_vvr(self, new_uuid),
                               delay=5)
        self.tasks.update({task_id: task_res})
        return {'status_code': 202,
                'headers': {'location': task_res.res_url}}

    @urlmatch(path=VVR_ID_REGEX)
    def match_vvr(self, url, request):
        LOG.debug('Matched Vvr resource; url=' + str(url))
        vvr_match = extract_path_element(url.path, VVR_ID_REGEX)
        if vvr_match is None:
            return {'status_code': 404, 'content': 'Not found'}
        target_vvr_id = vvr_match[0]
        result_vvr = self.vvrs.get(target_vvr_id)
        if result_vvr is None:
            return {'status_code': 404, 'content': 'Not found'}
        # check for actions and return object if none
        action_match = extract_path_element(url.path,
                                            target_vvr_id + '/' + ACTION_REGEX)
        if action_match is not None:
            action = action_match[0]
            action_task = None
            if action == 'start':
                action_task = result_vvr.start()
            if action == 'stop':
                action_task = result_vvr.stop()
            return {'status_code': 202,
                    'headers': {'location': action_task.res_url}}
        if vvr_match[1] < len(url.path):
            vvr_mock = HTTMock(result_vvr.match_snapshots,
                               result_vvr.match_devices,
                               result_vvr.match_tasks,
                               result_vvr.match_task,
                               result_vvr.match_root_snapshot,
                               result_vvr.match_snapshot,
                               result_vvr.match_device,
                               result_vvr.match_any)
            with vvr_mock:
                s = Session()
                return s.send(request)

        # check for delete operation
        if request.method == 'DELETE':
            LOG.debug('Delete request for VVR ' + result_vvr.res_url)

            task_id = str(uuid4())
            task_res = MockTaskRes(self.tenant_id, self.res_url +
                                   '/tasks/' + task_id,
                                   repr_dct={'uuid': task_id},
                                   task_action=(
                                   lambda: self.delete_vvr(target_vvr_id)))
            self.tasks.update({task_id: task_res})
            return {'status_code': 202,
                    'headers': {'location': task_res.res_url}}

        return {'status_code': 200,
                'content': json.dumps(result_vvr.get_repr())}

    def delete_vvr(self, vvr_id):

        if vvr_id not in self.vvrs:
            return None

        target_vvr = self.vvrs.get(vvr_id)
        vvr_repr = target_vvr.get_repr()
        if vvr_repr['started']:
            raise AssertionError('VVR is started')

        return self.vvrs.pop(vvr_id).res_url


class MockVvrRes(MockResource):
    def __init__(self, tenant_id, res_url, repr_dct):
        super(MockVvrRes, self).__init__(tenant_id, res_url, repr_dct)
        self.devices = {}
        root_snap_id = str(uuid4())
        self.root_snap = MockSnapshotRes(tenant_id,
                                         res_url + '/snapshots/' +
                                         root_snap_id,
                                         {'size': 0,
                                          'partial': True,
                                          'description': '',
                                          'parent': root_snap_id,
                                          'uuid': root_snap_id,
                                          'dataSize': 0,
                                          'name': 'root snapshot'})
        self.snapshots = {root_snap_id: self.root_snap}
        self.tasks = {}

    def start(self):
        def start_vvr(self):
            self.repr_dct['started'] = True
            return self.res_url

        task_id = str(uuid4())
        task_res = MockTaskRes(self.tenant_id, self.res_url +
                               '/tasks/' + task_id,
                               repr_dct={'uuid': task_id},
                               task_action=lambda: start_vvr(self))
        self.tasks.update({task_id: task_res})
        return task_res

    def stop(self):
        def stop_vvr(self):
            for dev in self.devices.viewvalues():
                if dev.get_repr()['active']:
                    raise AssertionError('Vvr has active devices.')
            self.repr_dct['started'] = False
            return self.res_url

        task_id = str(uuid4())
        task_res = MockTaskRes(self.tenant_id, self.res_url +
                               '/tasks/' + task_id,
                               repr_dct={'uuid': task_id},
                               task_action=lambda: stop_vvr(self))
        self.tasks.update({task_id: task_res})
        return task_res

    @urlmatch(path='.*')
    def match_any(self, url, request):
        LOG.error('No match for URI; uri=' + request.url)
        return {'status_code': 404, 'content': 'Not found.'}

    @urlmatch(path=VVR_ID_REGEX + '/snapshots/?$')
    def match_snapshots(self, url, request):
        return {'status_code': 200,
                'content': json.dumps({resource_proxies.SNAPSHOT_LIST_KEY:
                                       [snapshot.get_repr()
                                        for snapshot in
                                        self.snapshots.viewvalues()]})}

    @urlmatch(path=VVR_ID_REGEX + '/devices/?$')
    def match_devices(self, url, request):
        return {'status_code': 200,
                'content': json.dumps({resource_proxies.DEVICE_LIST_KEY:
                                       [device.get_repr()
                                        for device in
                                        self.devices.viewvalues()]})}

    @urlmatch(path=VVR_ID_REGEX + '/tasks/?$')
    def match_tasks(self, url, request):
        return {'status_code': 200,
                'content': json.dumps({resource_proxies.TASK_LIST_KEY:
                                       [task.get_repr()
                                        for task in self.tasks]})}

    @urlmatch(path=VVR_ID_REGEX + '/root')
    def match_root_snapshot(self, url, request):
        if url.path.endswith('/root'):
            return {'status_code': 200,
                    'content': json.dumps(self.root_snap.get_repr())}

        # check for actions and return object if none
        action_match = extract_path_element(url.path, r'root/' + ACTION_REGEX)
        if action_match is not None:
            action = action_match[0]
            if action == 'newDevice':
                check_result = MockSnapshotRes.check_new_dev_input(url)
                if check_result is not None:
                    return check_result
                task_id = str(uuid4())
                task_res = MockTaskRes(self.tenant_id, self.res_url +
                                       '/tasks/' + task_id,
                                       repr_dct={'uuid': task_id},
                                       task_action=(lambda:
                                                    self.
                                                    new_device(url,
                                                               self.
                                                               root_snap)))
                self.tasks.update({task_id: task_res})
                return {'status_code': 202,
                        'headers': {'location': task_res.res_url}}

    @urlmatch(path=VVR_ID_REGEX + TASK_ID_REGEX)
    def match_task(self, url, request):
        task_match = extract_path_element(url.path, TASK_ID_REGEX)
        if task_match is None:
            return {'status_code': 404, 'content': 'Not found'}

        target_task = self.tasks.get(task_match[0])
        if target_task is None:
            return {'status_code': 404, 'content': 'Not found'}

        return {'status_code': 200,
                'content': json.dumps(target_task.get_repr())}

    def new_device(self, url, tgt_snapshot):
        params = get_query_params(url.query)
        new_dev = tgt_snapshot.new_device(self.res_url + '/devices',
                                          dev_id=params.get('uuid'),
                                          name=params.get('name'),
                                          size=params.get('size') or
                                          tgt_snapshot.get_repr()['size'],
                                          desc=params.get('description'))
        self.devices[new_dev.get_repr()['uuid']] = new_dev
        return new_dev.res_url

    def new_snapshot(self, url, target_dev):
        params = get_query_params(url.query)
        new_snap = target_dev.new_snapshot(self.res_url + '/snapshots',
                                           snap_id=params.get('uuid'),
                                           name=params.get('name'),
                                           desc=params.get('description'))
        self.snapshots[new_snap.get_repr()['uuid']] = new_snap
        return new_snap.res_url

    @urlmatch(path=VVR_ID_REGEX + SNAPSHOT_ID_REGEX)
    def match_snapshot(self, url, request):
        snap_match = extract_path_element(url.path, SNAPSHOT_ID_REGEX)
        if snap_match is None:
            return {'status_code': 404, 'content': 'Not found'}

        tgt_snap = self.snapshots.get(snap_match[0])
        if tgt_snap is None:
            return {'status_code': 404, 'content': 'Not found'}

        is_final_res = (snap_match[1] == len(url.path))

        if is_final_res and request.method == 'DELETE':
            def delete_snap():
                self.snapshots.pop(snap_match[0])
                return None
            task_id = str(uuid4())
            task_res = MockTaskRes(self.tenant_id, self.res_url +
                                   '/tasks/' + task_id,
                                   repr_dct={'uuid': task_id},
                                   task_action=delete_snap)
            self.tasks.update({task_id: task_res})
            return {'status_code': 202,
                    'headers': {'location': task_res.res_url}}

        if is_final_res:
            return {'status_code': 200,
                    'content': json.dumps(tgt_snap.get_repr())}

        # check for actions and return object if none
        action_match = extract_path_element(url.path,
                                            snap_match[0] + '/' + ACTION_REGEX)
        if action_match is not None:
            action = action_match[0]
            if action == 'newDevice':
                check_result = MockSnapshotRes.check_new_dev_input(url)
                if check_result is not None:
                    return check_result
                task_id = str(uuid4())
                task_res = MockTaskRes(self.tenant_id, self.res_url +
                                       '/tasks/' + task_id,
                                       repr_dct={'uuid': task_id},
                                       task_action=(lambda:
                                                    self.
                                                    new_device(url,
                                                               tgt_snap)))
                self.tasks.update({task_id: task_res})
                return {'status_code': 202,
                        'headers': {'location': task_res.res_url}}

        return {'status_code': 404, 'content': 'Not found'}

    @urlmatch(path=VVR_ID_REGEX + DEVICE_ID_REGEX)
    def match_device(self, url, request):
        dev_match = extract_path_element(url.path, DEVICE_ID_REGEX)
        if dev_match is None:
            return {'status_code': 404, 'content': 'Not found'}

        target_dev = self.devices.get(dev_match[0])
        if target_dev is None:
            return {'status_code': 404, 'content': 'Not found'}

        is_final_res = (dev_match[1] == len(url.path))

        if is_final_res and request.method == 'DELETE':
            def delete_dev():
                self.devices.pop(dev_match[0])
                return None
            task_id = str(uuid4())
            task_res = MockTaskRes(self.tenant_id, self.res_url +
                                   '/tasks/' + task_id,
                                   repr_dct={'uuid': task_id},
                                   task_action=delete_dev)
            self.tasks.update({task_id: task_res})
            return {'status_code': 202,
                    'headers': {'location': task_res.res_url}}

        if is_final_res:
            return {'status_code': 200,
                    'content': json.dumps(target_dev.get_repr())}

        # check for actions and return object if none
        action_match = extract_path_element(url.path,
                                            dev_match[0] + '/' + ACTION_REGEX)
        if action_match is not None:
            action = action_match[0]
            if action == 'newSnapshot':
                check_result = target_dev.check_new_snapshot_conditions(url)
                if check_result is not None:
                    return check_result
                task_id = str(uuid4())
                task_res = MockTaskRes(self.tenant_id, self.res_url +
                                       '/tasks/' + task_id,
                                       repr_dct={'uuid': task_id},
                                       task_action=(lambda:
                                                    self.
                                                    new_snapshot(url,
                                                                 target_dev)))
                self.tasks.update({task_id: task_res})
                return {'status_code': 202,
                        'headers': {'location': task_res.res_url}}

            elif action == 'resize':
                def resize_dev(new_size):
                    target_dev.get_repr()['size'] = new_size

                size = extract_query_param('size', url.query)
                if size <= 0:
                    return {'status_code': 403, 'content': 'Invalid size'}
                task_id = str(uuid4())
                task_res = MockTaskRes(self.tenant_id, self.res_url +
                                       '/tasks/' + task_id,
                                       repr_dct={'uuid': task_id},
                                       task_action=lambda: resize_dev(size))
                self.tasks.update({task_id: task_res})
                return {'status_code': 202,
                        'headers': {'location': task_res.res_url}}

            elif action == 'activate':
                def activate_dev(read_only=None):
                    target_repr = target_dev.get_repr()
                    target_repr['active'] = True
                    target_repr['readOnly'] = bool(read_only)
                    return target_dev.res_url

                is_ro = extract_query_param('readOnly', url.query)
                task_id = str(uuid4())
                task_res = MockTaskRes(self.tenant_id, self.res_url +
                                       '/tasks/' + task_id,
                                       repr_dct={'uuid': task_id},
                                       task_action=lambda: activate_dev(is_ro))
                self.tasks.update({task_id: task_res})
                return {'status_code': 202,
                        'headers': {'location': task_res.res_url}}

            elif action == 'deactivate':
                def deactivate_dev():
                    target_repr = target_dev.get_repr()
                    target_repr['active'] = False
                    target_repr['readOnly'] = False
                    LOG.debug('deactivated device' + str(target_dev))
                    return target_dev.res_url
                task_id = str(uuid4())
                task_res = MockTaskRes(self.tenant_id, self.res_url +
                                       '/tasks/' + task_id,
                                       repr_dct={'uuid': task_id},
                                       task_action=deactivate_dev)
                self.tasks.update({task_id: task_res})
                return {'status_code': 202,
                        'headers': {'location': task_res.res_url}}

            elif action == 'clone':
                def clone_dev():
                    tgt_repr = target_dev.get_repr()
                    new_snap = target_dev.new_snapshot(self.res_url +
                                                       '/snapshots',
                                                       name=tgt_repr['name'])
                    self.snapshots[new_snap.get_repr()['uuid']] = new_snap
                    return self.new_device(url, new_snap)
                task_id = str(uuid4())
                task_res = MockTaskRes(self.tenant_id, self.res_url +
                                       '/tasks/' + task_id,
                                       repr_dct={'uuid': task_id},
                                       task_action=clone_dev)
                self.tasks.update({task_id: task_res})
                return {'status_code': 202,
                        'headers': {'location': task_res.res_url}}

        subres_match = extract_path_element(url.path, SUBRES_REGEX)
        if subres_match is not None:
            subres = subres_match[0]
            if subres == 'connection':
                params = get_query_params(url.query)
                if 'ip' not in params:
                    return {'status_code': 400, 'content':
                            'No client IP provided'}
                #TODO(pwehrle): add invalid client IP error condition
                #TODO(pwehrle): add inactive device error condition
                target_dev_repr = target_dev.get_repr()
                server_addr = url.netloc.split(':')
                server_ip = server_addr[0]
                server_port = server_addr[1]
                #TODO(pwehrle): track down driver_volume_type request param

                connection_type = params.get('clientProtocol')
                connection_info = {'driver_volume_type': connection_type,
                                   'server_address': server_ip,
                                   'server_port': server_port}

                if connection_type == 'iscsi':
                    iscsi_iqn = ('iqn.2000-06.com.oodrive.nuage:' +
                                 target_dev_repr.get('name'))
                    connection_info.update({'iqn': iscsi_iqn,
                                            'iscsiAlias': iscsi_iqn})
                elif connection_type == 'nbd':
                    connection_info['devName'] = target_dev_repr.get('name')
                return {'status_code': 200, 'content': connection_info}

        return {'status_code': 404, 'content': 'Not found'}


class MockSnapshotRes(MockResource):
    def __init__(self, tenant_id, res_url, repr_dct):
        super(MockSnapshotRes, self).__init__(tenant_id, res_url, repr_dct)

    @classmethod
    def check_new_dev_input(cls, url):
        params = get_query_params(url.query)
        if params.get('name') is None:
            return {'status_code': 400, 'content': 'name is null'}
        got_size = params.get('size')
        if got_size is None:
            return {'status_code': 400, 'content': 'Invalid parameters'}
        try:
            if long(got_size) <= 0:
                return {'status_code': 400, 'content': 'Invalid parameters'}
        except ValueError:
            return {'status_code': 400, 'content': 'Invalid parameters'}

    def new_device(self, base_url, dev_id=None, name=None, size=0, desc=None):
        new_dev_id = dev_id or str(uuid4())
        return MockDeviceRes(self.tenant_id,
                             base_url + '/' + new_dev_id,
                             {'partial': True,
                              'name': name or 'device',
                              'description': desc,
                              'parent': self.repr_dct['uuid'],
                              'uuid': new_dev_id,
                              'readOnly': False,
                              'active': False,
                              'dataSize': 0,
                              'size': size})


class MockDeviceRes(MockResource):
    def __init__(self, tenant_id, res_url, repr_dct):
        super(MockDeviceRes, self).__init__(tenant_id, res_url, repr_dct)

    def check_new_snapshot_conditions(self, url):
        params = get_query_params(url.query)

        if self.repr_dct['active'] and self.repr_dct['readOnly']:
            return {'status_code': 400, 'content': 'Activated read-only'}

        if params.get('name') in [None, '']:
            return {'status_code': 400, 'content': 'name is null or empty'}

    def new_snapshot(self, parent_url, snap_id=None, name=None, desc=None):
        new_snap_id = snap_id or str(uuid4())
        old_parent_id = self.repr_dct['parent']
        self.repr_dct['parent'] = new_snap_id
        return MockSnapshotRes(self.tenant_id,
                               parent_url + '/' + new_snap_id,
                               {'size': self.repr_dct.get('size') or 0,
                                'partial': True,
                                'description': '',
                                'parent': old_parent_id,
                                'uuid': new_snap_id,
                                'dataSize': 0,
                                'name': name,
                                'description': desc})


class MockTaskRes(MockResource):

    def __init__(self, tenant_id, res_url, repr_dct, task_action, delay=0):
        super(MockTaskRes, self).__init__(tenant_id, res_url, repr_dct)
        self._task_action = task_action
        self._exec_thread = Thread(target=self._delayed_exec,
                                   args=(task_action, delay))
        self._thread_lock = RLock()
        self.progress = 0
        self.status = 'PENDING'
        self.result_ref = None

    def get_repr(self):
        self._thread_lock.acquire()
        try:
            if self.status == 'PENDING' and not self._exec_thread.is_alive():
                self.states = 'PROCESSING'
                self._exec_thread.start()
            return {'progress': self.progress,
                    'state': self.status,
                    'resultRef': self.result_ref,
                    'uuid': self.repr_dct['uuid']}
        finally:
            self._thread_lock.release()

    def _delayed_exec(self, task_action, delay):
        sleep(delay)
        try:
            self.result_ref = task_action()
            self.status = 'DONE'
        except Exception as e:
            LOG.error('Exception in task %s: %s' % (self.repr_dct['uuid'], e))
            self.result_ref = None
            self.status = 'FAILED'
