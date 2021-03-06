########
# Copyright (c) 2014 GigaSpaces Technologies Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
############

__author__ = 'adaml'
import os
import shutil
from copy import deepcopy
from libcloud.compute.types import Provider
from libcloud.compute.providers import get_driver
import yaml
import errno

from fabric.api import put, env
from fabric.context_managers import settings

import libcloud.security
# from CLI
# provides a logger to be used throughout the provider code
# returns a tuple of a main (file+console logger) and a file
# (file only) logger.
from cosmo_cli.cosmo_cli import init_logger
# from cosmo_cli.cosmo_cli import set_global_verbosity_level
# provides 2 base methods to be used.
# if not imported, the bootstrap method must be implemented
from cosmo_cli.provider_common import BaseProviderClass
from os.path import expanduser

libcloud.security.VERIFY_SSL_CERT = False

# initialize logger
lgr, flgr = init_logger()

CONFIG_FILE_NAME = 'cloudify-config.yaml'
DEFAULTS_CONFIG_FILE_NAME = 'cloudify-config.defaults.yaml'


is_verbose_output = False

class ProviderManager(BaseProviderClass):

    """class for base methods
        name must be kept as is.

        inherits BaseProviderClass from the cli containing the following
        methods:
        __init__: initializes base mandatory params provider_config and
        is_verbose_output. additionally, optionally receives a schema param
        that enables the default schema validation method to be executed.
        bootstrap: installs cloudify on the management server.
        validate_config_schema: validates a schema file against the provider
        configuration file supplied with the provider module.
        (for more info on BaseProviderClass, see the CLI's documentation.)

        ProviderManager classes:
        __init__: *optional* - only if more params are initialized
        provision: *mandatory*
        validate: *mandatory*
        teardown: *mandatory*
        """

    def __init__(self, provider_config=None, is_verbose_output=False):
        """
        initializes base params.
        provider_config and is_verbose_output are initialized in the
        base class and are mandatory. if more params are needed, super can
        be used to init provider_config and is_verbose_output.

        :param dict provider_config: inherits the config yaml from the cli
        :param bool is_verbose_output: self explanatory
        :param dict schema: is an optional parameter containing a jsonschema
        object. If initialized it will automatically trigger schema validation
        for the provider.
        """
        self.provider_config = provider_config
        super(ProviderManager, self).__init__(provider_config,
                                              is_verbose_output)


    def _get_private_key_path_from_keypair_config(self, keypair_config):
        path = keypair_config['provided']['private_key_filepath'] if \
            'provided' in keypair_config else \
            keypair_config['auto_generated']['private_key_target_path']
        return expanduser(path)

    def copy_files_to_manager(self, mgmt_ip, config, ssh_key, ssh_user):
        def _copy(userhome_on_management,agents_key_path):

            env.user = ssh_user
            env.key_filename = ssh_key
            env.abort_on_prompts = False
            env.connection_attempts = 12
            env.keepalive = 0
            env.linewise = False
            env.pool_size = 0
            env.skip_bad_hosts = False
            env.timeout = 5
            env.forward_agent = True
            env.status = False
            env.disable_known_hosts = False

            lgr.info('uploading agents private key to manager')
            # TODO: handle failed copy operations
            put(agents_key_path, userhome_on_management + '/.ssh')

        def _get_private_key_path_from_keypair_config(keypair_config):
            path = keypair_config['provided']['private_key_filepath'] if \
                'provided' in keypair_config else \
                keypair_config['auto_generated']['private_key_target_path']
            return expanduser(path)

        compute_config = config['compute']
        mgmt_server_config = compute_config['management_server']

        with settings(host_string=mgmt_ip):
            _copy(
                mgmt_server_config['userhome_on_management'],
                _get_private_key_path_from_keypair_config(
                    compute_config['agent_servers']['agents_keypair']))

    def provision(self):
        """
        provisions resources for the management server

        :rtype: 'tuple' with the machine's public and private ip's,
        the ssh key and user configured in the config yaml and
        the prorivder's context (a dict containing the privisioned
        resources to be used during teardown)
        """
        lgr.info('bootstrapping to Exoscale provider.')

        lgr.debug('reading configuration file')
        # provider_config = _read_config(None)

        #init keypair and security-group resource creators.
        cloud_driver = ExoscaleConnector(self.provider_config).create()
        keypair_creator = ExoscaleKeypairCreator(
            cloud_driver, self.provider_config)
        security_group_creator = ExoscaleSecurityGroupCreator(
            cloud_driver, self.provider_config)

        #create required node topology
        lgr.debug('creating the required resources for management vm')
        security_group_creator.create_security_groups()
        keypair_creator.create_key_pairs()

        keypair_name = keypair_creator.get_management_keypair_name()
        sg_name = security_group_creator.get_mgmt_security_group_name()

        lgr.debug('reading server configuration.')
        mgmt_server_config = self.provider_config.get('compute', {}) \
        .get('management_server', {})

        # init compute node creator
        compute_creator = ExoscaleComputeCreator(cloud_driver,
                                                 self.provider_config,
                                                 keypair_name,
                                                 sg_name)

        #spinning-up a new instance using the above topology.
        #Exoscale provider supports only public ip allocation.
        #see cloudstack 'basic zone'
        public_ip = compute_creator.create_node()

        provider_context = {"ip":str(public_ip)}

        print('public ip: ' + public_ip + ' key name: ' + self._get_private_key_path_from_keypair_config(
            mgmt_server_config['management_keypair']) + 'user name: ' + mgmt_server_config.get('user_on_management'))

        self.copy_files_to_manager(
            public_ip,
            self.provider_config,
            self._get_private_key_path_from_keypair_config(
                mgmt_server_config['management_keypair']),
            mgmt_server_config.get('user_on_management'))

        return public_ip, \
               public_ip, \
               self._get_private_key_path_from_keypair_config(
                   mgmt_server_config['management_keypair']), \
               mgmt_server_config.get('user_on_management'), \
               provider_context

    def validate(self, validation_errors={}):
        """
        validations to be performed before provisioning and bootstrapping
        the management server.

        :param dict schema: a schema dict to validate the provider config
        against
        :rtype: 'dict' representing validation_errors. provisioning will
        continue only if the dict is empty.
        """
        return validation_errors

    def teardown(self, provider_context, ignore_validation=False):
        """
        tears down the management server and its accompanied provisioned
        resources

        :param dict provider_context: context information with the previously
        provisioned resources
        :param bool ignore_validation: should the teardown process ignore
        conflicts during teardown
        :rtype: 'None'
        """
        management_ip = provider_context['ip']
        lgr.info('tearing-down management vm {0}.'.format(management_ip))

        # lgr.debug('reading configuration file {0}'.format(config_path))
        # provider_config = _read_config(config_path)

        #init keypair and security-group resource creators.
        cloud_driver = ExoscaleConnector(self.provider_config).create()
        keypair_creator = ExoscaleKeypairCreator(
            cloud_driver, self.provider_config)
        security_group_creator = ExoscaleSecurityGroupCreator(
            cloud_driver, self.provider_config)
        # init compute node creator
        compute_creator = ExoscaleComputeCreator(cloud_driver,
                                                 self.provider_config,
                                                 keypair_name=None,
                                                 security_group_name=None,
                                                 node_name=None)

        resource_terminator = ExoscaleResourceTerminator(security_group_creator,
                                                         keypair_creator,
                                                         compute_creator,
                                                         management_ip)

        lgr.debug('terminating management vm and all of its resources.')
        resource_terminator.terminate_resources()


# Create the provider folder in script location.
def init(target_directory, reset_config, is_verbose_output=False):
    if not reset_config and os.path.exists(
            os.path.join(target_directory, CONFIG_FILE_NAME)):
        lgr.debug('config file path {0} already exists. '
                  'either set a different config target directory '
                  'or enable reset_config property'.format(target_directory))
        return False

    provider_dir = os.path.dirname(os.path.realpath(__file__))
    files_path = os.path.join(provider_dir, CONFIG_FILE_NAME)

    lgr.debug('Copying provider files from {0} to {1}'
        .format(files_path, target_directory))
    shutil.copy(files_path, target_directory)
    return True


def _deep_merge_dictionaries(overriding_dict, overridden_dict):
    merged_dict = deepcopy(overridden_dict)
    for k, v in overriding_dict.iteritems():
        if k in merged_dict and isinstance(v, dict):
            if isinstance(merged_dict[k], dict):
                merged_dict[k] = _deep_merge_dictionaries(v, merged_dict[k])
            else:
                raise RuntimeError('type conflict at key {0}'.format(k))
        else:
            merged_dict[k] = deepcopy(v)
    return merged_dict


def _read_config(config_file_path):
    if not config_file_path:
        config_file_path = os.path.join(
            os.path.dirname(os.path.realpath(__file__)),
            CONFIG_FILE_NAME)
    defaults_config_file_path = os.path.join(
        os.path.dirname(os.path.realpath(__file__)),
        DEFAULTS_CONFIG_FILE_NAME)

    if not os.path.exists(config_file_path) or not os.path.exists(
            defaults_config_file_path):
        if not os.path.exists(defaults_config_file_path):
            raise ValueError('Missing the defaults configuration file; '
                             'expected to find it at {0}'
                .format(defaults_config_file_path))
        raise ValueError('Missing the configuration file; expected to find '
                         'it at {0}'.format(config_file_path))

    lgr.debug('reading provider config files')
    with open(config_file_path, 'r') as config_file, open(
            defaults_config_file_path, 'r') as defaults_config_file:

        lgr.debug('safe loading user config')
        user_config = yaml.safe_load(config_file.read())

        lgr.debug('safe loading default config')
        defaults_config = yaml.safe_load(defaults_config_file.read())

    lgr.debug('merging configurations')
    merged_config = _deep_merge_dictionaries(user_config, defaults_config) \
        if user_config else defaults_config
    return merged_config


# def bootstrap(config_path=None, is_verbose_output=False,
#               bootstrap_using_script=True, keep_up=False,
#               dev_mode=False):
#     lgr.info('bootstrapping to Exoscale provider.')
#     _set_global_verbosity_level(is_verbose_output)
#
#     lgr.debug('reading configuration file {0}'.format(config_path))
#     provider_config = _read_config(config_path)
#
#     #init keypair and security-group resource creators.
#     cloud_driver = ExoscaleConnector(provider_config).create()
#     keypair_creator = ExoscaleKeypairCreator(cloud_driver, provider_config)
#     security_group_creator = ExoscaleSecurityGroupCreator(cloud_driver,
#                                                           provider_config)
#     #create required node topology
#     lgr.debug('creating the required resources for management vm')
#     security_group_creator.create_security_groups()
#     keypair_creator.create_key_pairs()
#
#     keypair_name = keypair_creator.get_management_keypair_name()
#     security_group_name = security_group_creator.get_mgmt_security_group_name()
#
#     # init compute node creator
#     compute_creator = ExoscaleComputeCreator(cloud_driver,
#                                              provider_config,
#                                              keypair_name,
#                                              security_group_name)
#
#     #spinning-up a new instance using the above topology.
#     #Exoscale provider supports only public ip allocation.
#     #see cloudstack 'basic zone'
#     public_ip = compute_creator.create_node()
#     cosmo_bootstrapper = CosmoOnExoscaleBootstrapper(provider_config,
#                                                      public_ip,
#                                                      public_ip,
#                                                      bootstrap_using_script,
#                                                      dev_mode)
#     #bootstrap to cloud.
#     cosmo_bootstrapper.do(keep_up)
#     return public_ip


#TODO: no config_path named property on openstack. why?
# def teardown(management_ip,
#              is_verbose_output=False,
#              config_path=None):
#     lgr.info('tearing-down management vm {0}.'.format(management_ip))
#
#     lgr.debug('reading configuration file {0}'.format(config_path))
#     provider_config = _read_config(config_path)
#
#     #init keypair and security-group resource creators.
#     cloud_driver = ExoscaleConnector(provider_config).create()
#     keypair_creator = ExoscaleKeypairCreator(cloud_driver, provider_config)
#     security_group_creator = ExoscaleSecurityGroupCreator(cloud_driver,
#                                                           provider_config)
#     # init compute node creator
#     compute_creator = ExoscaleComputeCreator(cloud_driver,
#                                              provider_config,
#                                              keypair_name=None,
#                                              security_group_name=None,
#                                              node_name=None)
#
#     resource_terminator = ExoscaleResourceTerminator(security_group_creator,
#                                                      keypair_creator,
#                                                      compute_creator,
#                                                      management_ip)
#
#     lgr.debug('terminating management vm and all of its resources.')
#     resource_terminator.terminate_resources()


class ExoscaleResourceTerminator(object):
    def __init__(self,
                 security_group_creator,
                 key_pair_creator,
                 compute_creator,
                 mgmt_ip):
        self.security_group_creator = security_group_creator
        self.key_pair_creator = key_pair_creator
        self.compute_creator = compute_creator
        self.mgmt_ip = mgmt_ip

    def terminate_resources(self):
        lgr.info('terminating management vm {0}'.format(self.mgmt_ip))
        self.compute_creator.delete_node(self.mgmt_ip)

        lgr.info('deleting agent and management keypairs')
        self.key_pair_creator.delete_keypairs()

        lgr.info('deleting agent and management security-groups')
        self.security_group_creator.delete_security_groups()


class ExoscaleLogicError(RuntimeError):
    pass


class ExoscaleConnector(object):
    def __init__(self, provider_config):
        self.config = provider_config

    def create(self):
        lgr.debug('creating exoscale cloudstack connector')
        api_key = self.config['authentication']['api_key']
        api_secret_key = self.config['authentication']['api_secret_key']
        cls = get_driver(Provider.EXOSCALE)
        return cls(api_key, api_secret_key)


class ExoscaleKeypairCreator(object):
    def __init__(self, cloud_driver, provider_config):
        self.cloud_driver = cloud_driver
        self.provider_config = provider_config

    def _get_keypair(self, keypair_name):
        keypairs = [kp for kp in self.cloud_driver.list_key_pairs()
                    if kp.name == keypair_name]
        if keypairs.__len__() == 0:
            return None
        return keypairs[0]

    def delete_keypairs(self):
        mgmt_keypair_name = self.get_management_keypair_name()
        lgr.info('deleting management keypair {0}'.format(mgmt_keypair_name))
        self.cloud_driver.ex_delete_keypair(mgmt_keypair_name)

        agent_keypair_name = self._get_agents_keypair_name()
        lgr.info('deleting agents keypair {0}'.format(agent_keypair_name))
        self.cloud_driver.ex_delete_keypair(agent_keypair_name)

    def get_management_keypair_name(self):
        keypair_config = self.provider_config['compute']['management_server'][
            'management_keypair']
        return keypair_config['name']

    def _get_agents_keypair_name(self):
        keypair_config = self.provider_config['compute']['agent_servers'][
            'agents_keypair']
        return keypair_config['name']

    def create_key_pairs(self,
                         mgmt_private_key_target_path=None,
                         mgmt_public_key_filepath=None,
                         mgmt_keypair_name=None,
                         agent_private_key_target_path=None,
                         agent_public_key_filepath=None,
                         agent_keypair_name=None):

        lgr.debug('reading management keypair configuration')
        mgmt_kp_config = self.provider_config['compute']['management_server'][
            'management_keypair']
        self._create_keypair(mgmt_kp_config,
                             mgmt_private_key_target_path,
                             mgmt_public_key_filepath,
                             mgmt_keypair_name)

        lgr.debug('reading agent keypair configuration')
        agent_kp_config = self.provider_config['compute']['agent_servers'][
            'agents_keypair']
        self._create_keypair(agent_kp_config,
                             agent_private_key_target_path,
                             agent_public_key_filepath,
                             agent_keypair_name)


    def _create_keypair(self, keypair_config,
                        private_key_target_path=None,
                        public_key_filepath=None,
                        keypair_name=None):

        if not keypair_name:
            keypair_name = keypair_config['name']
        if not private_key_target_path:
            private_key_target_path = keypair_config.get('auto_generated',
                {}).get('private_key_target_path', None)
        if not public_key_filepath:
            public_key_filepath = keypair_config.get('provided', {}).get(
                'public_key_filepath', None)

        if self._get_keypair(keypair_name):
            lgr.info('using existing keypair {0}'.format(keypair_name))
            return
        else:
            if not private_key_target_path and not public_key_filepath:
                raise RuntimeError(
                    '{0} keypair not found. '
                    'you must provide either a private key target path, '
                    'public key file-path or an existing keypair name '
                    'in configuration file')

        if public_key_filepath:
            if not os.path.exists(public_key_filepath):
                raise RuntimeError('public key {0} was not found on your local'
                                   'file system.'.format(public_key_filepath))

            lgr.debug('importing public key with name {0} from {1}'.format(
                keypair_name, public_key_filepath))
            self.cloud_driver.import_key_pair_from_file(keypair_name,
                                                        public_key_filepath)
        else:
            lgr.info('creating a keypair named {0}'.format(keypair_name))
            result = self.cloud_driver.create_key_pair(keypair_name)
            pk_target_path = os.path.expanduser(private_key_target_path)

            try:
                lgr.debug('creating dir {0}'.format(pk_target_path))
                os.makedirs(os.path.dirname(private_key_target_path))
            except OSError, exc:
                if not exc.errno == errno.EEXIST or not \
                    os.path.isdir(os.path.dirname(private_key_target_path)):
                    raise

            lgr.debug('writing private key to file {0}'.format(pk_target_path))
            with open(pk_target_path, 'w') as f:
                f.write(result.private_key)
                os.system('chmod 600 {0}'.format(pk_target_path))


class ExoscaleSecurityGroupCreator(object):
    def __init__(self, cloud_driver, provider_config):
        self.cloud_driver = cloud_driver
        self.provider_config = provider_config

    def _add_rule(self, security_group_name,
                  protocol, cidr_list, start_port,
                  end_port=None):

        lgr.debug('creating security-group rule for {0} with details {1}'
            .format(security_group_name, locals().values()))
        self.cloud_driver.ex_authorize_security_group_ingress(
            securitygroupname=security_group_name,
            startport=start_port,
            endport=end_port,
            cidrlist=cidr_list,
            protocol=protocol)

    def get_security_group(self, security_group_name):
        security_groups = [sg for sg in self.cloud_driver
            .ex_list_security_groups() if sg['name'] == security_group_name]
        if security_groups.__len__() == 0:
            return None
        return security_groups[0]


    def delete_security_groups(self):

        mgmt_security_group_name = self.get_mgmt_security_group_name()
        lgr.debug('deleting management security-group {0}'.format(
            mgmt_security_group_name))
        try:
            self.cloud_driver.ex_delete_security_group(mgmt_security_group_name)
        except:
            lgr.warn(
                'management security-group {0} may not have been deleted'
                    .format(mgmt_security_group_name))
            pass

        agents_security_group_name = self._get_agent_security_group_name()
        lgr.debug('deleting agents security-group {0}'.format(
            agents_security_group_name))
        try:
            self.cloud_driver.ex_delete_security_group(
                agents_security_group_name)
        except:
            lgr.warn(
                'agent security-group {0} may not have been deleted'.format(
                    agents_security_group_name))
            pass

    def get_mgmt_security_group_name(self):
        mgmt_sg_conf = self.provider_config['networking'][
            'management_security_group']
        return mgmt_sg_conf['name']

    def _get_agent_security_group_name(self):
        agent_sg_conf = self.provider_config['networking'][
            'agents_security_group']
        return agent_sg_conf['name']

    def _is_sg_exists(self, security_group_name):
        exists = self.get_security_group(security_group_name)
        if not exists:
            return False
        return True

    def create_security_groups(self):

        # Security group for Cosmo created instances
        # Security group for Cosmo manager, allows created
        # instances -> manager communication
        lgr.debug('reading management security-group configuration.')
        management_sg_config = self.provider_config['networking'][
            'management_security_group']
        management_sg_name = management_sg_config['name']

        if not self._is_sg_exists(management_sg_name):
            lgr.info('creating management security group: {0}'
                .format(management_sg_name))
            self.cloud_driver.ex_create_security_group(management_sg_name)

            mgmt_ports = management_sg_config['ports']
            #for each port, add rule
            for port in mgmt_ports:
                cidr = management_sg_config.get('cidr', None)
                protocol = management_sg_config.get('protocol', None)
                self._add_rule(security_group_name=management_sg_name,
                               start_port=port,
                               end_port=None,
                               cidr_list=cidr,
                               protocol=protocol)
        else:
            lgr.info('using existing management security group {0}'.format(
                management_sg_name))

        lgr.debug('reading agent security-group configuration.')
        agent_sg_config = self.provider_config['networking'][
            'agents_security_group']
        agent_sg_name = agent_sg_config['name']

        if not self._is_sg_exists(agent_sg_name):
            lgr.info('creating agent security group {0}'.format(agent_sg_name))
            self.cloud_driver.ex_create_security_group(agent_sg_name)

            agent_ports = agent_sg_config['ports']
            #for each port, add rule
            for port in agent_ports:
                cidr = agent_sg_config['cidr']
                protocol = agent_sg_config['protocol']
                self._add_rule(security_group_name=agent_sg_name,
                               start_port=port,
                               end_port=None,
                               cidr_list=cidr,
                               protocol=protocol)
        else:
            lgr.info(
                'using existing agent security group {0}'.format(agent_sg_name))


class ExoscaleComputeCreator(object):
    def __init__(self, cloud_driver,
                 provider_config,
                 keypair_name=None,
                 security_group_name=None,
                 node_name=None):
        self.cloud_driver = cloud_driver
        self.provider_config = provider_config
        self.keypair_name = keypair_name
        self.security_group_names = [security_group_name, ]
        self.node_name = node_name

    def delete_node(self, node_ip):
        lgr.debug('getting node for if {0}'.format(node_ip))
        node = [node for node in self.cloud_driver.list_nodes() if
                node_ip in node.public_ips][0]

        lgr.debug('destroying node {0}'.format(node))
        self.cloud_driver.destroy_node(node)

    def create_node(self):

        lgr.debug('reading server configuration.')
        server_config = self.provider_config.get('compute', {}) \
            .get('management_server', {}).get('instance', None)

        lgr.debug('reading management vm image and size IDs from config')
        image_id = server_config.get('image')
        size_id = server_config.get('size')

        lgr.debug('getting node image for ID {0}'.format(image_id))
        image = [image for image in self.cloud_driver.list_images() if
                 image_id == image.id][0]
        lgr.debug('getting node size for ID {0}'.format(size_id))
        size = [size for size in self.cloud_driver.list_sizes() if
                size.name == size_id][0]

        if self.node_name is None:
            self.node_name = server_config.get('name', None)
        if self.keypair_name is None:
            self.keypair_name = server_config['management_keypair']['name']
        if self.security_group_names is None:
            network_config = self.provider_config.get('networking', {}) \
                .get('management_security_group', {})
            self.security_group_names = [network_config['name'], ]

        lgr.info(
            'starting a new virtual instance named {0}'.format(self.node_name))
        result = self.cloud_driver.create_node(
            name=self.node_name,
            ex_keyname=self.keypair_name,
            ex_security_groups=self.security_group_names,
            image=image,
            size=size)

        return result.public_ips[0]


