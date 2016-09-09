# Copyright (c) 2016 Cisco Systems
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

import collections
import copy

from apicapi import apic_client
import gevent
import json
import mock

from aim.agent.aid.universes.aci import aci_universe
from aim.agent.aid.universes.aci import converter
from aim.agent.aid.universes.aci import tenant as aci_tenant
from aim.api import resource as a_res
from aim.tests import base


AMBIGUOUS_TYPES = [aci_tenant.TAG_KEY, aci_tenant.FAULT_KEY]


class FakeResponse(object):

    def __init__(self, ok=True, text=None, status_code=200):
        self.ok = ok
        self.text = text or json.dumps({'imdata': {}})
        self.status_code = status_code


def _flat_result(result):
    flattened = []
    result = copy.deepcopy(result)
    children = result.values()[0].pop('children', [])
    flattened.append(result)
    for child in children:
        flattened.extend(_flat_result(child))
    return flattened


def mock_get_data(inst, dn, **kwargs):
    # Expected kwargs: query_target [subtree], target_subtree_class
    try:
        inst._data_stash
    except Exception:
        inst._data_stash = {}

    dn_mgr = apic_client.DNManager()
    # Since we have no APIC type, extract the object's RN:
    rn = []
    skip = False
    for x in range(len(dn)):
        # skip everything in square brackets
        c = dn[-1 - x]
        if c == '[':
            skip = False
            continue
        elif skip:
            continue
        elif c == ']':
            skip = True
            continue
        elif c == '/':
            break
        else:
            rn.append(c)
    rn = ''.join(reversed(rn))
    # From RN, infer the type
    if '-' in rn:
        rn = rn[:rn.find('-')]
    aci_type = apic_client.ManagedObjectClass.prefix_to_mos[rn]
    # Now we can decompose the DN, remove the mo/ in front
    decomposed = dn_mgr.aci_decompose_dn_guess(dn[3:], aci_type)[1]
    try:
        # Find the proper root node
        curr = copy.deepcopy(inst._data_stash[decomposed[0][1]])
        for index, part in enumerate(decomposed[1:]):
            # Look at the current's children and find the proper node.
            if part[0] in AMBIGUOUS_TYPES:
                partial_dn = (
                    dn_mgr.build(
                        decomposed[:index + 1]) + '/' +
                    apic_client.ManagedObjectClass.mos_to_prefix[part[0]] +
                    '-' + decomposed[index + 1][1])
            else:
                partial_dn = dn_mgr.build(decomposed[:index + 2])
            for child in curr.values()[0]['children']:
                if child.values()[0]['attributes']['dn'] == partial_dn:
                    curr = child
                    break
            else:
                raise KeyError
        # Curr is the looked up node. Look at the query params to filter the
        # result
        query_target = kwargs.get('query_target', 'self')
        if query_target == 'subtree':
            # Look at the target subtree class
            target_subtree_class = kwargs.get(
                'target_subtree_class', '').split(',')
            if not target_subtree_class:
                # Return everything
                return _flat_result(curr)
            else:
                # Only return the expected objects
                return [x for x in _flat_result(curr) if
                        x.keys()[0] in target_subtree_class]
        else:
            curr.values()[0].pop('children', [])
            return [curr]
    except KeyError:
        # Simulate 404
        if 'fault' in dn:
            # non existing faults return empty data
            return []
        raise apic_client.cexc.ApicResponseNotOk(
            request='get', status='404', reason='Not Found',
            err_text='Not Found', err_code='404')


class TestAciClientMixin(object):

    def _manipulate_server_data(self, data, manager=None, add=True, tag=True):
        manager = manager if manager is not None else self.manager
        try:
            manager.aci_session._data_stash
        except Exception:
            manager.aci_session._data_stash = {}

        def _tag_format(dn):
            return {
                'tagInst': {
                    'attributes': {
                        'dn': (dn + '/tag-' + self.sys_id)},
                    'children': []}
            }

        dn_mgr = apic_client.DNManager()
        for resource in copy.deepcopy(data):
            resource.values()[0]['attributes'].pop('status', None)
            data_type = resource.keys()[0]
            if data_type == 'tagInst' and tag:
                continue
            decomposed = dn_mgr.aci_decompose_dn_guess(
                resource.values()[0]['attributes']['dn'], data_type)[1]
            # Root is always a Tenant
            prev = manager.aci_session._data_stash
            if add:
                partial_dn = dn_mgr.build(decomposed[:1])
                curr = manager.aci_session._data_stash.setdefault(
                    decomposed[0][1],
                    {decomposed[0][0]:
                        {'attributes': {
                            'dn': decomposed[0][1]},
                            'children': [] if not tag else
                            [_tag_format(partial_dn)]}})
            else:
                curr = manager.aci_session._data_stash.get(decomposed[0][1],
                                                           [])
            child_index = None
            for index, part in enumerate(decomposed[1:]):
                # Look at the current's children and find the proper node.
                # if not found, it's a new node
                if part[0] in AMBIGUOUS_TYPES:
                    partial_dn = (
                        dn_mgr.build(
                            decomposed[:index + 1]) + '/' +
                        apic_client.ManagedObjectClass.mos_to_prefix[part[0]] +
                        '-' + decomposed[index + 1][1])
                else:
                    partial_dn = dn_mgr.build(decomposed[:index + 2])

                for index, child in enumerate(curr.values()[0]['children']):
                    if child.values()[0]['attributes']['dn'] == partial_dn:
                        child_index = index
                        prev = curr
                        curr = child
                        break
                else:
                    if add:
                        next = {
                            part[0]: {'attributes': {'dn': partial_dn},
                                      'children': [] if not tag else
                                      [_tag_format(partial_dn)]}}
                        curr.values()[0]['children'].append(next)
                        prev = curr
                        curr = next
                    else:
                        # Not found
                        return
            # Update body
            if add:
                resource.values()[0].pop('children', None)
                curr[curr.keys()[0]].update(resource.values()[0])
            else:
                if child_index is not None:
                    prev.values()[0]['children'].pop(child_index)
                else:
                    # Root node
                    prev.pop(decomposed[0][1])

    def _add_server_data(self, data, manager=None, tag=True):
        self._manipulate_server_data(data, manager=manager, add=True, tag=tag)

    def _remove_server_data(self, data, manager=None):
        self._manipulate_server_data(data, manager=manager, add=False)

    def _extract_rns(self, dn, mo):
        FIXED_RNS = ['rsctx', 'rsbd', 'intmnl', 'outtmnl']
        return [rn for rn in self.manager.dn_manager.aci_decompose(dn, mo)
                if rn not in FIXED_RNS]

    def _objects_transaction_create(self, objs, create=True, tag=None):
        tag = tag or self.sys_id
        result = []
        for obj in objs:
            conversion = converter.AimToAciModelConverter().convert([obj])
            transaction = apic_client.Transaction(mock.Mock())
            tags = []
            if create:
                for item in conversion:
                    dn = item.values()[0]['attributes']['dn']
                    dn += '/tag-%s' % tag
                    tags.append({"tagInst__%s" % item.keys()[0]:
                                 {"attributes": {"dn": dn}}})

            for item in conversion + tags:
                getattr(transaction, item.keys()[0]).add(
                    *self._extract_rns(
                        item.values()[0]['attributes'].pop('dn'),
                        item.keys()[0]),
                    **item.values()[0]['attributes'])
            result.append(transaction)
        return result

    def _objects_transaction_delete(self, objs):
        result = []
        for obj in objs:
            transaction = apic_client.Transaction(mock.Mock())
            item = copy.deepcopy(obj)
            getattr(transaction, obj.keys()[0]).remove(
                *self._extract_rns(
                    item.values()[0]['attributes'].pop('dn'),
                    item.keys()[0]))
            result.append(transaction)
        return result

    def _init_event(self):
        return [
            {"fvRsCtx": {"attributes": {
                "dn": "uni/tn-test-tenant/BD-test/rsctx",
                "tnFvCtxName": "test"}}},
            {"fvRsCtx": {"attributes": {
                "dn": "uni/tn-test-tenant/BD-test-2/rsctx",
                "tnFvCtxName": "test"}}},
            {"fvBD": {"attributes": {"arpFlood": "yes", "descr": "test",
                                     "dn": "uni/tn-test-tenant/BD-test",
                                     "epMoveDetectMode": "",
                                     "limitIpLearnToSubnets": "no",
                                     "llAddr": ":: ",
                                     "mac": "00:22:BD:F8:19:FF",
                                     "multiDstPktAct": "bd-flood",
                                     "name": "test",
                                     "ownerKey": "", "ownerTag": "",
                                     "unicastRoute": "yes",
                                     "unkMacUcastAct": "proxy",
                                     "unkMcastAct": "flood",
                                     "vmac": "not-applicable"}}},
            {"fvBD": {"attributes": {"arpFlood": "no", "descr": "",
                                     "dn": "uni/tn-test-tenant/BD-test-2",
                                     "epMoveDetectMode": "",
                                     "limitIpLearnToSubnets": "no",
                                     "llAddr": ":: ",
                                     "mac": "00:22:BD:F8:19:FF",
                                     "multiDstPktAct": "bd-flood",
                                     "name": "test-2", "ownerKey": "",
                                     "ownerTag": "", "unicastRoute": "yes",
                                     "unkMacUcastAct": "proxy",
                                     "unkMcastAct": "flood",
                                     "vmac": "not-applicable"}}},
            {"fvTenant": {"attributes": {"descr": "",
                                         "dn": "uni/tn-test-tenant",
                                         "name": "test-tenant",
                                         "ownerKey": "",
                                         "ownerTag": ""}}}]

    def _set_events(self, event_list, manager=None, tag=True):
        # Greenlets have their own weird way of calculating bool
        event_list_copy = copy.deepcopy(event_list)
        manager = manager if manager is not None else self.manager
        manager.ws_context.session.subscription_thread._events.setdefault(
            manager.tenant._get_instance_subscription_urls()[0], []).extend([
                dict([('imdata', [x])]) for x in event_list])
        # Add events to server
        aci_tenant.AciTenantManager.flat_events(event_list_copy)
        for event in event_list_copy:
            if event.values()[0]['attributes'].get('status') != 'deleted':
                self._add_server_data([event], manager=manager, tag=tag)
            else:
                self._remove_server_data([event], manager=manager)

    def _do_aci_mocks(self):
        self.set_override('apic_hosts', ['1.1.1.1'], 'apic')
        self.ws_login = mock.patch('acitoolkit.acitoolkit.Session.login')
        self.ws_login.start()

        self.tn_subscribe = mock.patch(
            'aim.agent.aid.universes.aci.tenant.Tenant._instance_subscribe',
            return_value=FakeResponse())
        self.tn_subscribe.start()

        self.process_q = mock.patch(
            'acitoolkit.acisession.Subscriber._process_event_q')
        self.process_q.start()

        self.post_body = mock.patch(
            'apicapi.apic_client.ApicSession.post_body')
        self.post_body.start()

        self.apic_login = mock.patch(
            'apicapi.apic_client.ApicSession.login')
        self.apic_login.start()
        apic_client.ApicSession.get_data = mock_get_data

        # Monkey patch APIC Transactions
        self.old_transaction_commit = apic_client.Transaction.commit

        self.addCleanup(self.ws_login.stop)
        self.addCleanup(self.apic_login.stop)
        self.addCleanup(self.tn_subscribe.stop)
        self.addCleanup(self.process_q.stop)
        self.addCleanup(self.post_body.stop)


class TestAciTenant(base.TestAimDBBase, TestAciClientMixin):

    def setUp(self):
        super(TestAciTenant, self).setUp()
        self._do_aci_mocks()
        self.manager = aci_tenant.AciTenantManager(
            'tenant-1', self.cfg_manager,
            aci_universe.AciUniverse.establish_aci_session(self.cfg_manager),
            aci_universe.get_websocket_context(self.cfg_manager))

    def test_event_loop(self):
        self.manager._subscribe_tenant()
        # Runs with no events
        self.manager._event_loop()
        self.assertIsNone(self.manager.get_state_copy().root)
        # Get an initialization event
        self.manager._subscribe_tenant()
        self._set_events(self._init_event())
        self.manager._event_loop()
        # TODO(ivar): Now root will contain all those new objects, check once
        # implemented

    def test_login_failed(self):
        # Mock response and login
        with mock.patch('acitoolkit.acitoolkit.Session.login',
                        return_value=FakeResponse(ok=False)):
            self.assertRaises(aci_universe.WebSocketSessionLoginFailed,
                              self.manager.ws_context.establish_ws_session)

    def test_is_dead(self):
        self.assertFalse(self.manager.is_dead())

    def test_event_loop_failure(self):
        manager = aci_tenant.AciTenantManager(
            'tenant-1', self.cfg_manager,
            aci_universe.AciUniverse.establish_aci_session(self.cfg_manager),
            aci_universe.get_websocket_context(self.cfg_manager))
        manager.tenant.instance_has_event = mock.Mock(side_effect=KeyError)
        # Main loop is not raising
        manager._main_loop()
        # Failure by GreenletExit
        manager.tenant.instance_has_event = mock.Mock(
            side_effect=gevent.GreenletExit)
        self.assertRaises(gevent.GreenletExit, manager._main_loop)
        # Upon GreenExit, even _run stops the loop
        manager._run()
        # Instance unsubscribe could rise an exception itself
        with mock.patch('acitoolkit.acitoolkit.Session.unsubscribe',
                        side_effect=Exception):
            manager._run()

    def test_squash_events(self):
        double_events = [
            {"fvRsCtx": {"attributes": {
                "dn": "uni/tn-test-tenant/BD-test/rsctx",
                "tnFvCtxName": "test"}}},
            {"fvRsCtx": {"attributes": {
                "dn": "uni/tn-test-tenant/BD-test/rsctx",
                "tnFvCtxName": "test-2"}}}
            ]
        self.manager._subscribe_tenant()
        self._set_events(double_events)
        res = self.manager.tenant.instance_get_event_data(
            self.manager.ws_context.session)
        self.assertEqual(1, len(res))
        self.assertEqual(double_events[1], res[0])

    def test_push_aim_resources(self):
        # Create some AIM resources
        bd1 = self._get_example_aim_bd()
        bd2 = self._get_example_aim_bd(name='test2')
        bda1 = self._get_example_aci_bd()
        bda2 = self._get_example_aci_bd(descr='test2')
        subj1 = a_res.ContractSubject(tenant_name='test-tenant',
                                      contract_name='c', name='s',
                                      in_filters=['i1', 'i2'],
                                      out_filters=['o1', 'o2'])
        self.manager.push_aim_resources({'create': [bd1, bd2, subj1]})
        self.manager._push_aim_resources()
        # Verify expected calls
        transactions = self._objects_transaction_create([bd1, bd2, subj1])
        exp_calls = [
            mock.call(mock.ANY, json.dumps(transactions[0].root),
                      'test-tenant'),
            mock.call(mock.ANY, json.dumps(transactions[1].root),
                      'test-tenant'),
            mock.call(mock.ANY, json.dumps(transactions[2].root),
                      'test-tenant')]
        self._check_call_list(exp_calls, self.manager.aci_session.post_body)

        # Delete AIM resources
        self.manager.aci_session.post_body.reset_mock()
        f1 = {'vzRsFiltAtt__In': {'attributes': {
            'dn': 'uni/tn-test-tenant/brc-c/subj-s/intmnl/rsfiltAtt-i1'}}}
        f2 = {'vzRsFiltAtt__Out': {'attributes': {
            'dn': 'uni/tn-test-tenant/brc-c/subj-s/outtmnl/rsfiltAtt-o1'}}}
        self.manager.push_aim_resources({'delete': [bda1, bda2, f1, f2]})
        self.manager._push_aim_resources()
        # Verify expected calls, add deleted status
        transactions = self._objects_transaction_delete([bda1, bda2, f1, f2])
        exp_calls = [
            mock.call(mock.ANY, json.dumps(transactions[0].root),
                      'test-tenant'),
            mock.call(mock.ANY, json.dumps(transactions[1].root),
                      'test-tenant'),
            mock.call(mock.ANY, json.dumps(transactions[2].root),
                      'test-tenant'),
            mock.call(mock.ANY, json.dumps(transactions[3].root),
                      'test-tenant')]
        self._check_call_list(exp_calls, self.manager.aci_session.post_body)

        # Create AND delete aim resources
        self.manager.aci_session.post_body.reset_mock()
        self.manager.push_aim_resources(collections.OrderedDict(
            [('create', [bd1]), ('delete', [bda2])]))
        self.manager._push_aim_resources()
        transactions = self._objects_transaction_create([bd1])
        transactions.extend(self._objects_transaction_delete([bda2]))
        exp_calls = [
            mock.call(mock.ANY, json.dumps(transactions[0].root),
                      'test-tenant'),
            mock.call(mock.ANY, json.dumps(transactions[1].root),
                      'test-tenant')]
        self._check_call_list(exp_calls, self.manager.aci_session.post_body)

        # Failure in pushing object
        self.manager.aci_session.post_body = mock.Mock(
            side_effect=apic_client.cexc.ApicResponseNotOk)
        # No exception is externally rised
        self.manager.push_aim_resources({'delete': [bda1, bda2]})
        self.manager._push_aim_resources()

    def test_fill_events_noop(self):
        # On unchanged data, fill events is a noop
        events = self._init_event()
        events_copy = copy.deepcopy(events)
        events = self.manager._fill_events(events)
        self.assertEqual(events, events_copy)

    def test_fill_events(self):
        events = [
            {"fvRsCtx": {"attributes": {
                "dn": "uni/tn-test-tenant/BD-test/rsctx",
                "tnFvCtxName": "test", "status": "modified"}}},
        ]
        complete = {"fvRsCtx": {"attributes": {
            "dn": "uni/tn-test-tenant/BD-test/rsctx",
            "tnFvCtxName": "test", "extra": "something_important"}}}
        parent_bd = self._get_example_aci_bd()
        self._add_server_data([complete, parent_bd])
        events, _ = self.manager._filter_ownership(
            self.manager._fill_events(events))
        self.assertEqual(sorted([complete, parent_bd]), sorted(events))

        # Now start from BD
        events = [{"fvBD": {"attributes": {
            "arpFlood": "yes", "descr": "test",
            "dn": "uni/tn-test-tenant/BD-test", "status": "modified"}}}]
        events, _ = self.manager._filter_ownership(
            self.manager._fill_events(events))
        self.assertEqual(sorted([parent_bd, complete]), sorted(events))

    def test_fill_events_not_found(self):
        events = [
            {"fvRsCtx": {"attributes": {
                "dn": "uni/tn-test-tenant/BD-test/rsctx",
                "tnFvCtxName": "test", "status": "modified"}}},
        ]
        parent_bd = self._get_example_aci_bd()
        # fvRsCtx is missing on server side
        self._add_server_data([parent_bd])
        events, _ = self.manager._filter_ownership(
            self.manager._fill_events(events))
        self.assertEqual([parent_bd], events)

        self.manager.aci_session._data_stash = {}
        self._add_server_data([])
        events = [
            {"fvRsCtx": {"attributes": {
                "dn": "uni/tn-test-tenant/BD-test/rsctx",
                "tnFvCtxName": "test", "status": "modified"}}},
        ]
        events, _ = self.manager._filter_ownership(
            self.manager._fill_events(events))
        self.assertEqual([], events)

    def test_flat_events(self):
        events = [
            {'fvRsCtx': {
                'attributes': {'dn': 'uni/tn-ivar-wstest/BD-test-2/rsctx',
                               'tnFvCtxName': 'asasa'},
                'children': [{'faultInst': {
                    'attributes': {'ack': 'no', 'delegated': 'no',
                                   'code': 'F0952', 'type': 'config'}}}]}},
            {'fvRsCtx': {'attributes': {
                'dn': 'uni/tn-ivar-wstest/BD-test/rsctx',
                'tnFvCtxName': 'test'},
                'children': [{'faultInst': {'attributes': {
                    'ack': 'no', 'delegated': 'no',
                    'code': 'F0952', 'type': 'config'}}}]}}]

        self.manager.flat_events(events)
        expected = [
            {'fvRsCtx': {
                'attributes': {'dn': 'uni/tn-ivar-wstest/BD-test-2/rsctx',
                               'tnFvCtxName': 'asasa'}}},
            {'fvRsCtx': {'attributes': {
                'dn': 'uni/tn-ivar-wstest/BD-test/rsctx',
                'tnFvCtxName': 'test'}}},
            {'faultInst': {'attributes': {
                'dn': 'uni/tn-ivar-wstest/BD-test-2/rsctx/fault-F0952',
                'ack': 'no', 'delegated': 'no',
                'code': 'F0952', 'type': 'config'}}},
            {'faultInst': {'attributes': {
                'dn': 'uni/tn-ivar-wstest/BD-test/rsctx/fault-F0952',
                'ack': 'no', 'delegated': 'no',
                'code': 'F0952', 'type': 'config'}}}
        ]
        self.assertEqual(expected, events)

    def test_flat_events_nested(self):
        events = [
            {'fvRsCtx': {
                'attributes': {'dn': 'uni/tn-ivar-wstest/BD-test-2/rsctx',
                               'tnFvCtxName': 'asasa'},
                'children': [
                    {'faultInst': {
                        'attributes': {'ack': 'no', 'delegated': 'no',
                                       'code': 'F0952', 'type': 'config'},
                        'children': [{'faultInst': {
                            'attributes': {
                                'ack': 'no', 'delegated': 'no',
                                'code': 'F0952', 'type': 'config'}}}]}}]}},
            {'fvRsCtx': {'attributes': {
                'dn': 'uni/tn-ivar-wstest/BD-test/rsctx',
                'tnFvCtxName': 'test'}}}]

        self.manager.flat_events(events)
        expected = [
            {'fvRsCtx': {
                'attributes': {'dn': 'uni/tn-ivar-wstest/BD-test-2/rsctx',
                               'tnFvCtxName': 'asasa'}}},
            {'fvRsCtx': {'attributes': {
                'dn': 'uni/tn-ivar-wstest/BD-test/rsctx',
                'tnFvCtxName': 'test'}}},
            {'faultInst': {'attributes': {
                'dn': 'uni/tn-ivar-wstest/BD-test-2/rsctx/fault-F0952',
                'ack': 'no', 'delegated': 'no',
                'code': 'F0952', 'type': 'config'}}},
            {'faultInst': {'attributes': {
                'dn': 'uni/tn-ivar-wstest/BD-test-2/rsctx/fault-F0952/'
                      'fault-F0952',
                'ack': 'no', 'delegated': 'no',
                'code': 'F0952', 'type': 'config'}}}
        ]
        self.assertEqual(expected, events)

    def test_flat_events_unmanaged_object(self):
        events = [
            {'fvRsCtx': {
                'attributes': {'dn': 'uni/tn-ivar-wstest/BD-test-2/rsctx',
                               'tnFvCtxName': 'asasa'},
                'children': [
                    {'faultInst': {
                        'attributes': {'ack': 'no', 'delegated': 'no',
                                       'code': 'F0952', 'type': 'config'}}},
                    # We don't manage faultDelegate objects
                    {'faultDelegate': {
                        'attributes': {'ack': 'no', 'delegated': 'no',
                                       'code': 'F0951', 'type': 'config'}}}]}},
            {'fvRsCtx': {'attributes': {
                'dn': 'uni/tn-ivar-wstest/BD-test/rsctx',
                'tnFvCtxName': 'test'},
                'children': [{'faultInst': {'attributes': {
                    'ack': 'no', 'delegated': 'no',
                    'code': 'F0952', 'type': 'config'}}}]}}]
        self.manager.flat_events(events)
        expected = [
            {'fvRsCtx': {
                'attributes': {'dn': 'uni/tn-ivar-wstest/BD-test-2/rsctx',
                               'tnFvCtxName': 'asasa'}}},
            {'fvRsCtx': {'attributes': {
                'dn': 'uni/tn-ivar-wstest/BD-test/rsctx',
                'tnFvCtxName': 'test'}}},
            {'faultInst': {'attributes': {
                'dn': 'uni/tn-ivar-wstest/BD-test-2/rsctx/fault-F0952',
                'ack': 'no', 'delegated': 'no',
                'code': 'F0952', 'type': 'config'}}},
            {'faultInst': {'attributes': {
                'dn': 'uni/tn-ivar-wstest/BD-test/rsctx/fault-F0952',
                'ack': 'no', 'delegated': 'no',
                'code': 'F0952', 'type': 'config'}}}
        ]
        self.assertEqual(expected, events)

    def test_operational_tree(self):
        events = [
            {'fvRsCtx': {
                'attributes': {'dn': 'uni/tn-ivar-wstest/BD-test-2/rsctx',
                               'tnFvCtxName': 'asasa'},
                'children': [{'faultInst': {
                    'attributes': {'ack': 'no', 'delegated': 'no',
                                   'code': 'F0952', 'type': 'config'}}}]}},
            {'fvRsCtx': {'attributes': {
                'dn': 'uni/tn-ivar-wstest/BD-test/rsctx',
                'tnFvCtxName': 'test'},
                'children': [{'faultInst': {'attributes': {
                    'ack': 'no', 'delegated': 'no',
                    'code': 'F0952', 'type': 'config'}}}]}}]
        self.manager._subscribe_tenant()
        self._set_events(events)
        self.manager._event_loop()
        self.assertIsNotNone(self.manager._operational_state)

    def test_filter_ownership(self):
        events = [
            {'fvRsCtx': {
                'attributes': {'dn': 'uni/tn-ivar-wstest/BD-test-2/rsctx',
                               'tnFvCtxName': 'asasa'}}},
            {'fvRsCtx': {'attributes': {
                'dn': 'uni/tn-ivar-wstest/BD-test/rsctx',
                'tnFvCtxName': 'test'}}},
            {'faultInst': {'attributes': {
                'dn': 'uni/tn-ivar-wstest/BD-test-2/rsctx/fault-F0952',
                'ack': 'no', 'delegated': 'no',
                'code': 'F0952', 'type': 'config'}}},
            {'faultInst': {'attributes': {
                'dn': 'uni/tn-ivar-wstest/BD-test/rsctx/fault-F0952/'
                      'fault-F0952',
                'ack': 'no', 'delegated': 'no',
                'code': 'F0952', 'type': 'config'}}}
        ]
        result, _ = self.manager._filter_ownership(events)
        self.assertEqual(set(), self.manager.tag_set)
        self.assertEqual([], result)

        # Now a tag is added to set ownership of one of the to contexts
        tag = {'tagInst': {
               'attributes': {
                   'dn': 'uni/tn-ivar-wstest/BD-test-2/rsctx/'
                         'tag-' + self.sys_id}}}
        events.append(tag)
        result, _ = self.manager._filter_ownership(events)
        self.assertEqual(set(['uni/tn-ivar-wstest/BD-test-2/rsctx']),
                         self.manager.tag_set)
        self.assertEqual(
            sorted([
                {'fvRsCtx': {
                    'attributes': {'dn': 'uni/tn-ivar-wstest/BD-test-2/rsctx',
                                   'tnFvCtxName': 'asasa'}}},
                {'faultInst': {'attributes': {
                    'dn': 'uni/tn-ivar-wstest/BD-test-2/rsctx/fault-F0952',
                    'ack': 'no', 'delegated': 'no',
                    'code': 'F0952', 'type': 'config'}}}]),
            sorted(result))

        # Now delete the tag
        tag['tagInst']['attributes']['status'] = 'deleted'
        result, _ = self.manager._filter_ownership(events)
        self.assertEqual(set(), self.manager.tag_set)
        self.assertEqual([], result)

    def test_fill_events_fault(self):
        events = [
            {'fvRsCtx': {
                'attributes': {'dn': 'uni/tn-ivar-wstest/BD-test/rsctx',
                               'tnFvCtxName': 'asasa', 'status': 'created'}}},
            {'faultInst': {'attributes': {
                'dn': 'uni/tn-ivar-wstest/BD-test/rsctx/fault-F0952',
                'code': 'F0952'}}}
        ]
        complete = [
            {'fvBD': {'attributes': {'dn': u'uni/tn-ivar-wstest/BD-test'}}},
            {'faultInst': {'attributes': {
             'dn': 'uni/tn-ivar-wstest/BD-test/rsctx/fault-F0952',
             'ack': 'no', 'delegated': 'no',
             'code': 'F0952', 'type': 'config'}}},
            {'fvRsCtx': {
                'attributes': {'dn': 'uni/tn-ivar-wstest/BD-test/rsctx',
                               'tnFvCtxName': 'asasa'}}},
        ]
        self._add_server_data(complete)
        events, _ = self.manager._filter_ownership(
            self.manager._fill_events(events))
        self.assertEqual(sorted(complete), sorted(events))
