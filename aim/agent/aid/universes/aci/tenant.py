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

import copy
import json
import Queue
import time
import traceback

from acitoolkit import acitoolkit
from apicapi import apic_client
from apicapi import exceptions as apic_exc
import gevent
from oslo_log import log as logging

from aim.agent.aid import event_handler
from aim.agent.aid.universes.aci import converter
from aim.agent.aid.universes import base_universe
from aim.common.hashtree import structured_tree
from aim.db import tree_model
from aim import exceptions

LOG = logging.getLogger(__name__)
TENANT_KEY = 'fvTenant'
INFRA_KEY = 'infraInfra'
ROOT_KEYS = [TENANT_KEY, INFRA_KEY]
FAULT_KEY = 'faultInst'
TAG_KEY = 'tagInst'
STATUS_FIELD = 'status'
SEVERITY_FIELD = 'severity'
CHILDREN_FIELD = 'children'
CHILDREN_LIST = set(converter.resource_map.keys() + ['fvTenant', 'tagInst'])
INFRA_CHILDREN_LIST = set(['infraInfra', 'tagInst'])
OPERATIONAL_LIST = [FAULT_KEY]


class WebSocketSubscriptionFailed(exceptions.AimException):
    message = ("Web socket session failed to subscribe for tenant %(tn_name)s "
               "with error %(code)s: %(text)s")


class Tenant(acitoolkit.Tenant):

    def __init__(self, *args, **kwargs):
        self.filtered_children = kwargs.pop('filtered_children', [])
        super(Tenant, self).__init__(*args, **kwargs)

    def _rn_fmt(self):
        return 'tn-{}'

    def _get_instance_subscription_urls(self):
        url = ('/api/mo/uni/' + self._rn_fmt() + '.json?query-target=subtree&'
               'rsp-prop-include=config-only&rsp-subtree-include=faults&'
               'subscription=yes'.format(self.name))
        if self.filtered_children:
            url += '&target-subtree-class=' + ','.join(self.filtered_children)
        return [url]

    def _instance_subscribe(self, session, extension=''):
        """Subscribe to this tenant if not subscribed yet."""
        urls = self._get_instance_subscription_urls()
        resp = None
        for url in urls:
            if not session.is_subscribed(url + extension):
                resp = session.subscribe(url + extension)
                LOG.debug('Subscribed to %s %s %s ', url + extension, resp,
                          resp.text)
                if not resp.ok:
                    return resp
        return resp

    def instance_subscribe(self, session):
        # Have it publicly available
        resp = self._instance_subscribe(session)
        if resp:
            if resp.ok:
                return json.loads(resp.text)['imdata']
            else:
                raise WebSocketSubscriptionFailed(tn_name=self.name,
                                                  code=resp.status_code,
                                                  text=resp.text)

    def instance_unsubscribe(self, session):
        urls = self._get_instance_subscription_urls()
        LOG.debug("Subscription urls: %s", urls)
        for url in urls:
            session.unsubscribe(url)

    def instance_get_event_data(self, session):
        # Replace _instance_get_event to avoid object creation, we just need
        # the sheer data
        urls = self._get_instance_subscription_urls()
        for url in urls:
            # Aggregate similar events
            result = []
            while session.has_events(url):
                event = session.get_event(url)['imdata'][0]
                event_klass = event.keys()[0]
                event_dn = event[event_klass]['attributes']['dn']
                for partial in result:
                    if (event_klass == partial.keys()[0] and
                            event_dn == partial[event_klass][
                                'attributes']['dn']):
                        partial.update(event)
                        break
                else:
                    result.append(event)
            return result

    def instance_has_event(self, session):
        return self._instance_has_events(session)


class Infra(Tenant):

    def _rn_fmt(self):
        return '{}'


class AciTenantManager(gevent.Greenlet):

    def __init__(self, tenant_name, apic_config, apic_session, ws_context,
                 creation_succeeded=None, creation_failed=None,
                 *args, **kwargs):
        self.tenant_name = tenant_name
        LOG.info("Init manager for %s" % self._qualified_name())
        super(AciTenantManager, self).__init__(*args, **kwargs)
        self.apic_config = apic_config
        # Each tenant has its own sessions
        self.aci_session = apic_session
        self.dn_manager = apic_client.DNManager()
        self._state = structured_tree.StructuredHashTree()
        self._operational_state = structured_tree.StructuredHashTree()
        self._monitored_state = structured_tree.StructuredHashTree()
        self._health_state = False
        self.polling_yield = self.apic_config.get_option(
            'aci_tenant_polling_yield', 'aim')
        self.to_aim_converter = converter.AciToAimModelConverter()
        self.to_aci_converter = converter.AimToAciModelConverter()
        self.object_backlog = Queue.Queue()
        self.tree_maker = tree_model.AimHashTreeMaker()
        self.tag_name = self.apic_config.get_option('aim_system_id', 'aim')
        self.tag_set = set()
        self.failure_log = {}

        def noop(par):
            pass
        self.creation_succeeded = creation_succeeded or noop
        self.creation_failed = creation_failed or noop
        # Warm bit to avoid rushed synchronization before receiving the first
        # batch of APIC events
        self._warm = False
        self.ws_context = ws_context
        self.initialize_target()

    def _qualified_name(self):
        return 'Tenant %s' % self.tenant_name

    def initialize_target(self):
        children_mos = set()
        for mo in CHILDREN_LIST:
            if mo in apic_client.ManagedObjectClass.supported_mos:
                children_mos.add(apic_client.ManagedObjectClass(mo).klass_name)
            else:
                children_mos.add(mo)
        self.tenant = Tenant(self.tenant_name, filtered_children=children_mos)

    def is_dead(self):
        # Wrapping the greenlet property for easier testing
        return self.dead

    def is_warm(self):
        return self._warm

    @property
    def health_state(self):
        return self._health_state

    @health_state.setter
    def health_state(self, value):
        self._health_state = value

    # These methods are dangerous if run concurrently with _event_to_tree.
    # However, serialization/deserialization of the in-memory tree should not
    # cause I/O operation, therefore they can't be context switched.
    def get_state_copy(self):
        return structured_tree.StructuredHashTree.from_string(
            str(self._state), root_key=self._state.root_key)

    def get_operational_state_copy(self):
        return structured_tree.StructuredHashTree.from_string(
            str(self._operational_state),
            root_key=self._operational_state.root_key)

    def get_monitored_state_copy(self):
        return structured_tree.StructuredHashTree.from_string(
            str(self._monitored_state),
            root_key=self._monitored_state.root_key)

    def _run(self):
        LOG.debug("Starting main loop for %s" % self._qualified_name())
        try:
            while True:
                self._main_loop()
        except gevent.GreenletExit:
            try:
                self._unsubscribe_tenant()
            except Exception as e:
                LOG.error("An exception has occurred while exiting thread "
                          "for %s: %s" % (self._qualified_name(), e.message))
            finally:
                # We need to make sure that this thread dies upon
                # GreenletExit
                return

    def _main_loop(self):
        try:
            # tenant subscription is redone upon exception
            LOG.debug("Starting event loop for %s" % self._qualified_name())
            count = 3
            last_time = 0
            epsilon = 0.5
            while True:
                start = time.time()
                self._subscribe_tenant()
                self._event_loop()
                if count == 0:
                    LOG.debug("Setting %s to warm state" %
                              self._qualified_name())
                    self._warm = True
                    count -= 1
                elif count > 0:
                    count -= 1
                curr_time = time.time() - start
                if abs(curr_time - last_time) > epsilon:
                    # Only log significant differences
                    LOG.debug("Event loop for %s completed in %s "
                              "seconds" % (self._qualified_name(),
                                           time.time() - start))
                    last_time = curr_time
                if not last_time:
                    last_time = curr_time
        except gevent.GreenletExit:
            raise
        except Exception as e:
            LOG.error("An exception has occurred in thread serving "
                      "%s, error: %s" % (self._qualified_name(), e.message))
            LOG.debug(traceback.format_exc())
            self._unsubscribe_tenant()
            # TODO(ivar): sleep to avoid reconnecting too frequently

    def _event_loop(self):
        start_time = time.time()
        # Push the backlog at the very start of the event loop, so that
        # all the events we generate here are likely caught in this iteration.
        self._push_aim_resources()
        if self.tenant.instance_has_event(self.ws_context.session):
            LOG.debug("Event for %s in warm state %s" %
                      (self._qualified_name(), self._warm))
            # Continuously check for events
            events = self.tenant.instance_get_event_data(
                self.ws_context.session)
            for event in events:
                if (event.keys()[0] in ROOT_KEYS and not
                        event[event.keys()[0]]['attributes'].get(
                            STATUS_FIELD)):
                    LOG.info("Resetting Tree for %s" % self._qualified_name())
                    # This is a full resync, tree needs to be reset
                    self._state = structured_tree.StructuredHashTree()
                    self._operational_state = (
                        structured_tree.StructuredHashTree())
                    break
            LOG.debug("received events: %s", events)
            # Make events list flat
            self.flat_events(events)
            # Pull incomplete objects
            events = self._fill_events(events)
            LOG.debug("Filled events: %s", events)
            # Manage Tags
            owned, monitored = self._filter_ownership(events)
            LOG.debug("Filtered events: %s", events)
            self._event_to_tree(owned, monitored)
        # yield for other threads
        gevent.sleep(max(0, self.polling_yield - (time.time() -
                                                  start_time)))

    def push_aim_resources(self, resources):
        """Given a map of AIM resources for this tenant, push them into APIC

        Stash the objects to be eventually pushed. Given the nature of the
        system we don't really care if we lose one or two messages, or
        even all of them, or if we mess the order, or get involved in
        a catastrophic meteor impact, we should always be able to get
        back in sync.

        :param resources: a dictionary with "create" and "delete" resources
        :return:
        """
        # TODO(ivar): improve performance by squashing similar events
        self.object_backlog.put(resources)

    def _push_aim_resources(self):
        while not self.object_backlog.empty():
            request = self.object_backlog.get()
            LOG.debug("Requests: %s" % request)
            for method, aim_objects in request.iteritems():
                # Method will be either "create" or "delete"
                for aim_object in aim_objects:
                    # get MO from ACI client, identify it via its DN parts and
                    # push the new body
                    LOG.debug('%s AIM object %s in APIC' % (method,
                                                            aim_object))
                    if method == base_universe.DELETE:
                        to_push = [copy.deepcopy(aim_object)]
                    else:
                        if getattr(aim_object, 'monitored', False):
                            # When pushing to APIC, treat monitored
                            # objects as pre-existing
                            aim_object.monitored = False
                            aim_object.pre_existing = True
                        to_push = self.to_aci_converter.convert([aim_object])
                    # Set TAGs before pushing the request
                    tags = []
                    if method == base_universe.CREATE:
                        # No need to deal with tags on deletion
                        for obj in to_push:
                            dn = obj.values()[0]['attributes']['dn']
                            dn += '/tag-%s' % self.tag_name
                            tags.append({"tagInst__%s" % obj.keys()[0]:
                                         {"attributes": {"dn": dn}}})
                    LOG.debug("Pushing %s into APIC: %s" %
                              (method, to_push + tags))
                    # Multiple objects could result from a conversion, push
                    # them in a single transaction
                    MO = apic_client.ManagedObjectClass
                    decompose = apic_client.DNManager().aci_decompose_dn_guess
                    try:
                        if method == base_universe.CREATE:
                            with self.aci_session.transaction(
                                    top_send=True) as trs:
                                for obj in to_push + tags:
                                    attr = obj.values()[0]['attributes']
                                    mo, parents_rns = decompose(attr.pop('dn'),
                                                                obj.keys()[0])
                                    # exclude RNs that are fixed
                                    rns = [mr[1] for mr in parents_rns
                                           if (mr[0] not in MO.supported_mos or
                                               MO(mr[0]).rn_param_count)]
                                    getattr(getattr(self.aci_session, mo),
                                            method)(
                                                *rns, transaction=trs, **attr)
                        else:
                            for obj in to_push + tags:
                                attr = obj.values()[0]['attributes']
                                self.aci_session.DELETE(
                                    '/mo/%s.json' % attr.pop('dn'))
                        # Object creation was successful, change object state
                        if method == base_universe.CREATE:
                            self.creation_succeeded(aim_object)
                    except apic_exc.ApicResponseNotOk as e:
                        LOG.debug(traceback.format_exc())
                        try:
                            printable = aim_object.__dict__
                        except AttributeError:
                            printable = aim_object
                        LOG.error("An error as occurred during %s for "
                                  "object %s" % (method, printable))
                        # REVISIT(ivar) find a way to extrapolate reason only
                        if method == base_universe.CREATE:
                            self.creation_failed(aim_object, e.message)

    def _unsubscribe_tenant(self):
        self.tenant.instance_unsubscribe(self.ws_context.session)

    def _subscribe_tenant(self):
        self.tenant.instance_subscribe(self.ws_context.session)
        self.health_state = True

    def _event_to_tree(self, owned, monitored):
        """Parse the event and push it into the tree

        This method requires translation between ACI and AIM model in order
        to  honor the Universe contract.
        :param events: an ACI event in the form of a list of objects
        :return:
        """
        config_tree = {'create': [], 'delete': []}
        operational_tree = {'create': [], 'delete': []}
        monitored_tree = {'create': [], 'delete': []}
        trees = {True: operational_tree, False: config_tree}
        states = {id(operational_tree): self._operational_state,
                  id(config_tree): self._state,
                  id(monitored_tree): self._monitored_state}
        # - Deleting objects go to operational_tree as well.
        # - Owned objects don't go to monitored tree
        # - Monitored objects also go to config tree, but need monitored
        # attribute in conversion

        def evaluate_event(event):
            aci_resource = event.values()[0]
            if self._is_deleting(event):
                trees[event.keys()[0] == FAULT_KEY]['delete'].append(event)
                # Pop deleted object from the TAG list
                dn = aci_resource['attributes']['dn']
                self.tag_set.discard(dn)
            else:
                trees[event.keys()[0] == FAULT_KEY]['create'].append(event)
        # Set the owned events
        for event in owned:
            evaluate_event(event)
        # Set the monitored events
        trees[False] = monitored_tree
        for event in monitored:
            evaluate_event(event)

        def _monitor(state, obj):
            if state is self._monitored_state:
                obj.monitored = True
            return obj

        def _screen_monitored(obj):
            return self.to_aim_converter.convert(
                self.to_aci_converter.convert([obj]))[0]

        def _set_pre_existing(obj):
            obj.monitored = False
            obj.pre_existing = True
            return obj

        # Convert objects
        for tree in (monitored_tree, config_tree, operational_tree):
            state = states[id(tree)]
            tree['create'] = [_monitor(state, x) for x in
                              self.to_aim_converter.convert(tree['create'])]
            tree['delete'] = [_monitor(state, x) for x in
                              self.to_aim_converter.convert(tree['delete'])]
            modified = False
            # Config tree also gets monitored events
            if state is self._state:
                # Need double conversion to screen unwanted objects
                additional_objects = dict(
                    (x.dn, _set_pre_existing(_screen_monitored(x))) for x in
                    copy.deepcopy(monitored_tree['create']))
                for obj in tree['create']:
                    if obj.dn in additional_objects:
                        _set_pre_existing(obj)
                tree['create'].extend(additional_objects.values())

                additional_objects = dict(
                    (x.dn, _set_pre_existing(_screen_monitored(x))) for x in
                    copy.deepcopy(monitored_tree['delete']))
                for obj in tree['delete']:
                    if obj.dn in additional_objects:
                        _set_pre_existing(obj)
                tree['delete'].extend(additional_objects.values())

            if tree['delete']:
                modified = True
                self.tree_maker.delete(state, tree['delete'])
                if state is self._state:
                    # Delete also from Operational tree for branch cleanup
                    self.tree_maker.delete(self._operational_state,
                                           tree['delete'])
            if tree['create']:
                modified = True
                self.tree_maker.update(state, tree['create'])

            if modified:
                LOG.debug("New tree for %s: %s" % (self._qualified_name(),
                                                   str(state)))
                event_handler.EventHandler.reconcile()
            else:
                LOG.debug("No changes in tree for %s: " %
                          self._qualified_name())

    def _fill_events(self, events):
        """Gets incomplete objects from APIC if needed

        - Objects with no status field are already completed
        - Objects with status "created" are already completed
        - Objects with status "deleted" do not exist on APIC anymore
        - Objects with status "modified" need to be retrieved fully via REST

        Whenever an object is missing on retrieval, status will be set to
        "deleted".
        Some objects might be incomplete without their RSs, this method
        will take care of retrieving them.
        :param events: List of events to retrieve
        :return:
        """
        start = time.time()
        result = self.retrieve_aci_objects(events, self.to_aim_converter,
                                           self.aci_session)
        LOG.debug('Filling procedure took %s for %s' %
                  (time.time() - start, self._qualified_name()))
        return result

    @staticmethod
    def retrieve_aci_objects(events, to_aim_converter, aci_session,
                             get_all=False, include_tags=True):
        visited = set()
        result = []

        for event in events:
            resource = event.values()[0]
            res_type = event.keys()[0]
            status = resource['attributes'].get(STATUS_FIELD)
            raw_dn = resource['attributes'].get('dn')
            if status == converter.DELETED_STATUS:
                if raw_dn not in visited:
                    result.append(event)
            elif get_all or status or res_type in OPERATIONAL_LIST:
                try:
                    # Use the parent type and DN for related objects (like RS)
                    # Event is an ACI resource
                    aim_resources = to_aim_converter.convert([event])
                    for aim_res in aim_resources:
                        dn = aim_res.dn
                        res_type = aim_res._aci_mo_name
                        if dn in visited:
                            continue
                        query_targets = set([res_type])
                        if include_tags:
                            query_targets.add(TAG_KEY)
                        kargs = {'rsp_prop_include': 'config-only',
                                 'query_target': 'subtree'}
                        # See if there's any extra object to be retrieved
                        for filler in converter.reverse_resource_map.get(
                                type(aim_res), []):
                            if 'resource' in filler:
                                query_targets.add(filler['resource'])
                        kargs['target_subtree_class'] = ','.join(query_targets)
                        # Operational state need full configuration
                        if event.keys()[0] in OPERATIONAL_LIST:
                            kargs.pop('rsp_prop_include')
                        # TODO(ivar): 'mo/' suffix should be added by APICAPI
                        data = aci_session.get_data('mo/' + dn, **kargs)
                        if not data:
                            LOG.warn("Resource %s not found", dn)
                            # The object doesn't exist anymore, a delete event
                            # is expected.
                        for item in data:
                            if item.values()[0][
                                    'attributes']['dn'] not in visited:
                                result.append(item)
                                visited.add(
                                    item.values()[0]['attributes']['dn'])
                        visited.add(dn)
                except apic_exc.ApicResponseNotOk as e:
                    # The object doesn't exist anymore, a delete event
                    # is expected.
                    if str(e.err_code) == '404':
                        LOG.warn("Resource %s not found", dn)
                    else:
                        LOG.error(e.message)
                        raise
            if not status:
                if raw_dn not in visited:
                    result.append(event)
        return result

    @staticmethod
    def flat_events(events):
        # If there are children objects, put them at the top level
        for event in events:
            if event.values()[0].get(CHILDREN_FIELD):
                # Rebuild the DN
                children = event.values()[0].pop(CHILDREN_FIELD)
                valid_children = []
                for child in children:
                    attrs = child.values()[0]['attributes']
                    rn = attrs.get('rn')
                    name_or_code = attrs.get('name', attrs.get('code'))
                    # Set DN of this object the the parent DN plus
                    # the proper prefix followed by the name or code (in case
                    # of faultInst)
                    try:
                        prefix = apic_client.ManagedObjectClass.mos_to_prefix[
                            child.keys()[0]]
                    except KeyError:
                        # We don't manage this object type
                        LOG.warn("Unmanaged object type: %s" % child.keys()[0])
                        continue

                    attrs['dn'] = (
                        event.values()[0]['attributes']['dn'] + '/' +
                        (rn or (prefix + (('-' + name_or_code)
                                          if name_or_code else ''))))
                    valid_children.append(child)
                events.extend(valid_children)

    def _filter_ownership(self, events):
        LOG.debug('Filter ownership for events: %s' % events)
        managed, owned, monitored = [], [], []
        for event in events:
            if event.keys()[0] == TAG_KEY:
                decomposed = event.values()[0]['attributes']['dn'].split('/')
                if decomposed[-1] == 'tag-' + self.tag_name:
                    if self._is_deleting(event):
                        self.tag_set.discard('/'.join(decomposed[:-1]))
                    else:
                        self.tag_set.add('/'.join(decomposed[:-1]))
            else:
                managed.append(event)
        for event in managed:
            is_owned = self._is_owned(event)
            if is_owned or self._is_deleting(event):
                owned.append(event)
            if not is_owned or self._is_deleting(event):
                monitored.append(event)
        return owned, monitored

    def _is_owned(self, aci_object):
        dn = aci_object.values()[0]['attributes']['dn']
        if aci_object.keys()[0] in apic_client.MULTI_PARENT:
            decomposed = dn.split('/')
            # Check for parent ownership
            return '/'.join(decomposed[:-1]) in self.tag_set
        else:
            return dn in self.tag_set

    def _is_deleting(self, aci_object):
        attrs = aci_object.values()[0]['attributes']
        status = attrs.get(STATUS_FIELD, attrs.get(SEVERITY_FIELD))
        return status in [converter.DELETED_STATUS, converter.CLEARED_SEVERITY]


class AciInfraManager(AciTenantManager):

    def __init__(self, apic_config, apic_session, ws_context,
                 creation_succeeded=None, creation_failed=None,
                 *args, **kwargs):
        super(AciInfraManager, self).__init__(
            'infra', apic_config, apic_session, ws_context, creation_succeeded,
            creation_failed, *args, **kwargs)

    def initialize_target(self):
        children_mos = set()
        for mo in INFRA_CHILDREN_LIST:
            if mo in apic_client.ManagedObjectClass.supported_mos:
                children_mos.add(apic_client.ManagedObjectClass(mo).klass_name)
            else:
                children_mos.add(mo)
        self.tenant = Infra(self.tenant_name, filtered_children=children_mos)

    def _qualified_name(self):
        return 'ACI Infra'
