import json
import os
import sys
import unittest

import etcd

from copy import deepcopy
from unittest.mock import MagicMock, Mock, mock_open, patch, PropertyMock

from patroni import config, dcs
from patroni import multisite
from patroni.multisite import MultisiteController
from patroni.dcs import Member
from patroni.dcs.etcd import AbstractEtcdClientWithFailover

from tests.test_ha import Config
from .test_etcd import etcd_read, etcd_write, socket_getaddrinfo
from .test_ha import get_cluster_initialized_with_leader

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

# def get_clusterleader():
    

@patch.object(etcd.Client, 'write', etcd_write)
# @patch.object(etcd.Client, 'read', etcd_read)
@patch.object(etcd.Client, 'delete', Mock(side_effect=etcd.EtcdException))
class TestMultisite(unittest.TestCase):

    @patch('patroni.dcs.dcs_modules', Mock(return_value=['patroni.dcs.etcd']))
    @patch.object(etcd.Client, 'read', etcd_read)
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
        # can we test these Event() based things?
        pass


    # def test_get_dcs_config_exception(self):
    #     print(self.config.local_configuration)
    #     with patch(self.config.local_configuration, new_callable=PropertyMock) as local_config:
    #         local_config["multisite"]["etcd"]["host"] = ""
    #         self.assertRaises(Exception, MultisiteController.get_dcs_config)
    #     # _local_configuration["multisite"]["etcd"]["host"])
    #     # self.assertRaises(Exception, _)  # FIXME: ? into separate case?
