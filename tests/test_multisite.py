import datetime
import json
import os
import sys
import time
import unittest

from copy import deepcopy
from threading import Event
from unittest.mock import MagicMock, Mock, mock_open, patch, PropertyMock

import etcd

from patroni import config, dcs, multisite
from patroni.dcs import Cluster, ClusterConfig, Failover, Leader, Member, Status, SyncState, TimelineHistory
from patroni.dcs.etcd import AbstractEtcdClientWithFailover
from patroni.multisite import MultisiteController
from patroni.postgresql.misc import PostgresqlRole
from tests.test_ha import Config

from .test_etcd import etcd_read, etcd_write, socket_getaddrinfo
from .test_ha import DCSError, get_cluster_initialized_with_leader

SYSID = '12345678901'

# def get_cluster_config():
#     return Config({"name": "", "scope": "alpha", "namespace": "batman", "loop_wait": 10, "ttl": 30, "retry_timeout": 10,
#             "multisite": {"ttl": 90}})

PATRONI_CONFIG = """
scope: mstest
restapi:
  listen: 0.0.0.0:8008
bootstrap:
postgresql:
  name: foo
  data_dir: data/postgresql0
  pg_rewind:
    username: postgres
    password: postgres
etcd:
  host: localhost
multisite:
  name: mstest
  namespace: /multisite/
  etcd:
    host: localhost
  host: 127.0.0.1
  port: 5432
  ttl: 90
  retry_timeout: 40
"""

STANDBY_CONFIG = {
    "host": "10.0.0.1",
    "port": 5432,
    "create_replica_methods": ["basebackup"],
    "leader_site": "other_dc"
}

def get_member():
    return Member.from_node(1, 'other_dc', 1, json.dumps(STANDBY_CONFIG))

def get_member_with_wrong_data():
    c = deepcopy(STANDBY_CONFIG)
    c.pop("host")
    with patch.dict(STANDBY_CONFIG, c):
        return Member.from_node(1, 'other_dc', 1, json.dumps(c))

def get_cluster(initialize, leader, members, failover, cluster_config=None):
    t = datetime.datetime.now().isoformat()
    history = TimelineHistory(1, '[[1,67197376,"no recovery target specified","' + t + '","foo"]]',
                              [(1, 67197376, 'no recovery target specified', t, 'foo')])
    cluster_config = cluster_config or ClusterConfig(1, {'check_timeline': True, 'member_slots_ttl': 0}, 1)
    s = SyncState.empty()
    return Cluster(initialize, cluster_config, leader, Status(10, None, []), members, failover, s, history, False)

def get_cluster_initialized_without_leader(leader=False, failover=None, cluster_config=None, failsafe=False):
    m1 = Member(0, 'leader', 28, {'conn_url': 'postgres://replicator:rep-pass@127.0.0.1:5435/postgres',
                                  'api_url': 'http://127.0.0.1:8008/patroni', 'xlog_location': 4,
                                  'role': PostgresqlRole.PRIMARY, 'state': 'running'})
    leader = Leader(0, 0, m1 if leader else Member(0, '', 28, {}))
    m2 = Member(0, 'other', 28, {'conn_url': 'postgres://replicator:rep-pass@127.0.0.1:5436/postgres',
                                 'api_url': 'http://127.0.0.1:8011/patroni',
                                 'state': 'running',
                                 'pause': True,
                                 'tags': {'clonefrom': True},
                                 'scheduled_restart': {'schedule': "2100-01-01 10:53:07.560445+00:00",
                                                       'postgres_version': '99.0.0'}})
    failsafe = {m.name: m.api_url for m in (m1, m2)} if failsafe else None
    return get_cluster(SYSID, leader, [m1, m2], failover, cluster_config)


def get_cluster_initialized_with_leader(failover=None):
    return get_cluster_initialized_without_leader(leader=True, failover=failover)

# def get_clusterleader():
    

# @patch.object(etcd.Client, 'write', etcd_write)
# @patch.object(etcd.Client, 'read', etcd_read)
# @patch.object(etcd.Client, 'delete', Mock(side_effect=etcd.EtcdException))
class TestMultisite(unittest.TestCase):

    # @patch('patroni.dcs.dcs_modules', Mock(return_value=['patroni.dcs.etcd']))
    # @patch.object(etcd.Client, 'read', etcd_read)
    def setUp(self):
        super(TestMultisite, self).setUp()
        os.environ[Config.PATRONI_CONFIG_VARIABLE] = PATRONI_CONFIG

        self.config = Config(None)
        self.multisite = MultisiteController(self.config)

    def test_get_dcs_config(self):
        # test if keys are inherited from 'main' config
        # assert elements of the returned msconfig match what's expected

        # TODO: check if host and port are used from main config if not in config
        msconfig, _ = MultisiteController.get_dcs_config(self.config)
        self.assertEqual(msconfig["multisite"], True)
        self.assertEqual(msconfig["ttl"], 90)
        self.assertEqual(msconfig["scope"], "mstest")
        
    def test_status(self):
        s = self.multisite.status()
        self.assertEqual(s["status"], "Leader")
        self.assertEqual(s["name"], "mstest")

        # TODO: test standby_config and Standby as status

    def test_set_standby_config(self):
        r = self.multisite._set_standby_config(get_member_with_wrong_data())
        self.assertTrue(r)
        self.assertEqual(self.multisite.get_active_standby_config(), {"restore_command": "false"})

        r = self.multisite._set_standby_config(get_member())
        self.assertTrue(r)
        self.assertEqual(self.multisite.get_active_standby_config(), STANDBY_CONFIG)
        r = self.multisite._set_standby_config(get_member())
        self.assertFalse(r)

    def test_observe_leader(self):
        # no leader
        with patch('patroni.dcs.Cluster.leader_name', ''), \
                patch('patroni.multisite.MultisiteController._disconnected_operation') as disco:
            self.multisite._observe_leader()
            disco.assert_called_once()
        with patch('patroni.dcs.Cluster.leader', get_cluster_initialized_with_leader().leader), \
              patch('patroni.multisite.MultisiteController._set_standby_config') as ssc:
            # can we avoid producing another instance? when trying to patch either multisite or the already patched 
            # object, I receive AttributeError: can't delete attribute
            os.environ[Config.PATRONI_CONFIG_VARIABLE] = PATRONI_CONFIG.replace('name: mstest', 'name: leader')
            self.config = Config(None)
            self.m = MultisiteController(self.config)

            self.m._observe_leader()
            self.assertIsNone(self.m.get_active_standby_config())

            self.multisite._observe_leader()
            ssc.assert_called_once()

    def test_resolve_leader(self):
        with patch.object(Event, 'clear', Mock()) as c, \
              patch.object(Event, 'wait', Mock()) as w, \
              patch.object(Event, 'set', Mock()) as s:
            self.multisite.resolve_leader()
            c.assert_called_once()
            s.assert_called_once()
            w.assert_called_once()

    def test_heartbeat(self):
        with patch.object(Event, 'set', Mock()) as s:
            self.multisite.heartbeat()
            s.assert_called_once()

    def test_release(self):
        with patch.object(Event, 'set', Mock()) as s:
            self.multisite.release()
            s.assert_called_once()
            self.assertEqual(self.multisite._release, True)

    def test_should_failover(self):
        m = self.multisite
        m._failover_target = None
        self.assertEqual(m.should_failover(), False)
        m._failover_target = 'foo'
        m.name = 'foo'
        self.assertEqual(m.should_failover(), False)
        m.name = 'bar'
        self.assertEqual(m.should_failover(), True)

    def test_on_shutdown(self):
        with patch.object(MultisiteController, 'release', Mock()) as r:
            self.multisite.release()
            r.assert_called_once()

    def test_disconnected_operation(self):
        self.multisite._disconnected_operation()
        self.assertEqual(self.multisite._standby_config, {'restore_command': 'false'})

    def test_set_standby_config(self):
        m = Member(0, 'foo', 111, {"host": "10.1.2.3"})
        # , "port": "5432"
        with patch.object(MultisiteController, '_disconnected_operation', Mock()) as d:
            r = self.multisite._set_standby_config(m)
            d.assert_called_once()
            self.assertEqual(r, False)

            m.data["port"] = "5432"
            r = self.multisite._set_standby_config(m)
            self.assertEqual(self.multisite._standby_config, {'host': '10.1.2.3',
                                                              'port': '5432',
                                                              'create_replica_methods': ['basebackup'],
                                                              'leader_site': 'foo'})
            self.assertEqual(r, True)

    def test_check_transition(self):
        self.multisite.on_change = Mock()
        self.multisite._has_leader = False
        self.multisite._check_transition(True, 'blahblah')
        self.assertEqual(self.multisite._has_leader, True)

        # TODO: state_handler case

    @patch.object(MultisiteController, 'touch_member')
    @patch.object(MultisiteController, '_disconnected_operation')
    @patch.object(MultisiteController, '_check_transition')
    def test_resolve_multisite_leader(self, check_transition, disconnected_operation, touch_member):
        self.multisite.on_change = Mock()

        c = get_cluster_initialized_without_leader(failover=Failover(0, '', 'foo', None, 'mstest'))
        self.multisite.dcs.get_cluster = Mock(return_value=c)

        self.multisite._release = True
        self.multisite._resolve_multisite_leader()
        disconnected_operation.assert_called_once()

        disconnected_operation.reset_mock()
        self.multisite._release = False
        self.multisite._failover_target = 'foo'
        self.multisite._failover_timeout = 9999999999  # I am not _that_ optimistic
        self.multisite._resolve_multisite_leader()
        disconnected_operation.assert_called_once()

        self.multisite._failover_target = ''
        self.multisite._standby_config = {}

        # could acquire multisite lock
        self.multisite.dcs.attempt_to_acquire_leader = Mock(return_value=True)
        self.multisite.dcs.manual_failover = Mock()
        self.multisite._resolve_multisite_leader()
        self.assertIsNone(self.multisite._standby_config)
        check_transition.assert_called_with(leader=True, note='Acquired multisite leader status')
        self.multisite.dcs.manual_failover.assert_called_with('', '')

        # could not...
        c = get_cluster_initialized_without_leader()
        self.multisite.dcs.get_cluster = Mock(return_value=c)
        self.multisite.dcs.manual_failover.reset_mock()
        self.multisite._resolve_multisite_leader()
        self.multisite.dcs.manual_failover.assert_not_called()

        # There is a leader, and it's us
        disconnected_operation.reset_mock()
        c = get_cluster_initialized_with_leader()
        self.multisite.dcs.get_cluster = Mock(return_value=c)
        # lock is being released
        self.multisite.dcs.delete_leader = Mock()
        self.multisite.name = 'leader'
        self.multisite._release = True
        self.multisite._resolve_multisite_leader()
        self.multisite.dcs.delete_leader.assert_called_once_with(c.leader)
        check_transition.assert_called_with(leader=False, note="Released multisite leader status on request")
        disconnected_operation.assert_called_once()
        self.assertFalse(self.multisite._release)

        self.multisite.dcs.update_leader = Mock(return_value = True)
        self.multisite._check_for_failover = Mock()
        self.multisite._resolve_multisite_leader()
        self.assertIsNone(self.multisite._standby_config)
        check_transition.assert_called_with(leader=True, note="Already have multisite leader status")
        self.multisite._check_for_failover.assert_called_once_with(c)
        
        disconnected_operation.reset_mock()
        self.multisite.dcs.update_leader = Mock(return_value = False)
        self.multisite._resolve_multisite_leader()
        disconnected_operation.assert_called_once()
        check_transition.assert_called_with(leader=False, note="Failed to update multisite leader status")

        # the leader is someone else
        self.multisite._release = True
        self.multisite._failover_target = 'foo'
        self.multisite._failover_timeout = 9999999999
        self.multisite._set_standby_config = Mock(return_value=True)
        self.multisite.name = 'foo'
        self.multisite._has_leader = True
        self.multisite._resolve_multisite_leader()
        check_transition.assert_called_with(leader=False, note="Lost leader lock to leader")
        self.assertIsNone(self.multisite._failover_target)
        self.assertIsNone(self.multisite._failover_timeout)

        # DCS errors
        self.multisite.dcs.get_cluster = Mock(side_effect=DCSError('foo'), return_value=c)
        disconnected_operation.reset_mock()
        self.multisite._has_leader = True
        self.multisite.name = 'blah'
        self.multisite._resolve_multisite_leader()

        self.assertEqual(self.multisite._dcs_error, 'Multi site DCS cannot be reached')
        disconnected_operation.assert_called_once()
        self.assertFalse(self.multisite._has_leader)
        self.multisite.on_change.assert_called_once()



        
        
        
        # touch_member.assert_called_once()

    def test_observe_leader(self):
        # there is no leader
        with patch.object(MultisiteController, '_disconnected_operation', Mock()) as d:
            self.multisite.dcs.get_cluster = Mock(return_value=get_cluster_initialized_without_leader())
            self.multisite._observe_leader()
            d.assert_called_once()

        # there is a leader and it's not us
        with patch.object(MultisiteController, '_set_standby_config', Mock()) as s:
            c = get_cluster_initialized_with_leader()
            self.multisite.dcs.get_cluster = Mock(return_value=c)
            self.multisite._observe_leader()
            s.assert_called_once_with(c.leader.member)

        # we are the leader
        self.multisite.name = 'leader'
        self.multisite._observe_leader()
        self.assertIsNone(self.multisite._standby_config)

    def test_update_history(self):
        pass

    def test_check_for_failover(self):
        self.multisite.dcs.manual_failover = Mock()

        c = get_cluster_initialized_with_leader(Failover(0, '', 'foo', None, 'mstest'))
        self.multisite._check_for_failover(c)
        self.multisite.dcs.manual_failover.assert_called_with('', '')

        c = get_cluster_initialized_with_leader(failover=None)
        self.multisite._check_for_failover(c)
        self.assertIsNone(self.multisite._failover_target)
        self.assertIsNone(self.multisite._failover_timeout)

        c = get_cluster_initialized_with_leader(Failover(0, '', 'foo', None, 'other'))
        self.multisite._check_for_failover(c)
        self.assertIsNotNone(self.multisite._failover_timeout)
        self.assertEqual(self.multisite._failover_target, 'other')

    def test_touch_member(self):
        self.multisite.dcs.touch_member = Mock()
        self.multisite.touch_member()
        self.multisite.dcs.touch_member.assert_called_with({'host': '127.0.0.1', 'port': 5432})


    # def test_get_dcs_config_exception(self):
    #     print(self.config.local_configuration)
    #     with patch(self.config.local_configuration, new_callable=PropertyMock) as local_config:
    #         local_config["multisite"]["etcd"]["host"] = ""
    #         self.assertRaises(Exception, MultisiteController.get_dcs_config)
    #     # _local_configuration["multisite"]["etcd"]["host"])
    #     # self.assertRaises(Exception, _)  # FIXME: ? into separate case?
