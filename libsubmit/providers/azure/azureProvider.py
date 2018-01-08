import os
import pprint
import json
import time
import logging
import atexit
from libsubmit.providers.provider_base import ExecutionProvider
from libsubmit.error import *

try:
    from azure.common.credentials import UserPassCredentials
    from libsubmit.azure.azureDeployer import Deployer

except ImportError:
    _azure_enabled = False
else:
    _azure_enabled = True

translate_table = {'PD': 'PENDING',
                   'R': 'RUNNING',
                   'CA': 'CANCELLED',
                   'CF': 'PENDING',  # (configuring),
                   'CG': 'RUNNING',  # (completing),
                   'CD': 'COMPLETED',
                   'F': 'FAILED',  # (failed),
                   'TO': 'TIMEOUT',  # (timeout),
                   'NF': 'FAILED',  # (node failure),
                   'RV': 'FAILED',  # (revoked) and
                   'SE': 'FAILED'}  # (special exit state

template_string = """
cd ~
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip ipython
sudo pip3 install ipyparallel parsl
"""


class AzureProvider(ExecutionProvider):
     '''
    Here's a sample config for the Azure provider:

    .. code-block:: python

         { "auth" : { # Definition of authentication method for AWS. One of 3 methods are required to authenticate
                      # with AWS : keyfile, profile or env_variables. If keyfile or profile is not set Boto3 will
                      # look for the following env variables :
                      # AWS_ACCESS_KEY_ID : The access key for your AWS account.
                      # AWS_SECRET_ACCESS_KEY : The secret key for your AWS account.
                      # AWS_SESSION_TOKEN : The session key for your AWS account.

              #{Description: Path to json file that contains 'AWSAccessKeyId' and 'AWSSecretKey'
              "keyfile"    :
                             # Type : String,
                             # Required : False},

              #{Description: Specify the profile to be used from the standard aws config file
              "profile"    :
                             # ~/.aws/config.
                             # Type : String,
                             # Expected : "default", # Use the 'default' aws profile
                             # Required : False},

            },

           "execution" : { # Definition of all execution aspects of a site

              "executor"   : #{Description: Define the executor used as task executor,
                             # Type : String,
                             # Expected : "ipp",
                             # Required : True},

              "provider"   : #{Description : The provider name, in this case ec2
                             # Type : String,
                             # Expected : "aws",
                             # Required :  True },

              "block" : { # Definition of a block

                  "nodes"      : #{Description : # of nodes to provision per block
                                 # Type : Integer,
                                 # Default: 1},

                  "taskBlocks" : #{Description : # of workers to launch per block
                                 # as either an number or as a bash expression.
                                 # for eg, "1" , "$(($CORES / 2))"
                                 # Type : String,
                                 #  Default: "1" },

                  "walltime"  :  #{Description : Walltime requested per block in HH:MM:SS
                                 # Type : String,
                                 # Default : "00:20:00" },

                  "initBlocks" : #{Description : # of blocks to provision at the start of
                                 # the DFK
                                 # Type : Integer
                                 # Default : ?
                                 # Required :    },

                  "minBlocks" :  #{Description : Minimum # of blocks outstanding at any time
                                 # WARNING :: Not Implemented
                                 # Type : Integer
                                 # Default : 0 },

                  "maxBlocks" :  #{Description : Maximum # Of blocks outstanding at any time
                                 # WARNING :: Not Implemented
                                 # Type : Integer
                                 # Default : ? },

                  "options"   : {  # Scheduler specific options


                      #{Description : Instance type t2.small|t2...
                      "instanceType" :
                                       # Type : String,
                                       # Required : False
                                       # Default : t2.small },

                      #{"Description : String to append to the #SBATCH blocks
                      "imageId"      :
                                       # in the submit script to the scheduler
                                       # Type : String,
                                       # Required : False },

                      "region"       : #{"Description : AWS region to launch machines in
                                       # in the submit script to the scheduler
                                       # Type : String,
                                       # Default : 'us-east-2',
                                       # Required : False },

                      #{"Description : Name of the AWS private key (.pem file)
                      "keyName"      :
                                       # that is usually generated on the console to allow ssh access
                                       # to the EC2 instances, mostly for debugging.
                                       # in the submit script to the scheduler
                                       # Type : String,
                                       # Required : True },

                      #{"Description : If requesting spot market machines, specify
                      "spotMaxBid"   :
                                       # the max Bid price.
                                       # Type : Float,
                                       # Required : False },
                  }
              }
            }
         }
    '''
    def __repr__(self):
        return "<Azure Execution Provider for site:{0}>".format(self.sitename)
 
    def __init__(self, config):
        """Initialize Azure provider. Uses Azure python SDK to provide execution resources"""
        self.config = self.read_configs(config)
        self.config_logger()

        if not _azure_enabled:
            raise OptionalModuleMissing(
                ['azure', 'haikunator'], "Azure Provider requires the azure and haikunator modules.")

        credentials = UserPassCredentials(
            self.config['username'], self.config['pass'])
        subscription_id = self.config['subscriptionId']

        # self.resource_client = ResourceManagementClient(credentials, subscription_id)
        # self.storage_client = StorageManagementClient(credentials, subscription_id)

        self.resource_group_name = 'my_resource_group'
        self.deployer = Deployer(
            subscription_id,
            self.resource_group_name,
            self.read_configs(config))

        
        self.channel = channel
        if not _boto_enabled:
            raise OptionalModuleMissing(
                ['boto3'], "AWS Provider requires boto3 module.")

        self.config = config
        self.sitename = config['site']
        self.current_blocksize = 0
        self.resources = {}

        self.config = config
        options = self.config["execution"]["block"]["options"]
        logger.warn("Options %s", options)
        self.instance_type = options.get("instanceType", "t2.small")
        self.image_id = options["imageId"]
        self.key_name = options["keyName"]
        self.region = options.get("region", 'us-east-2')
        self.max_nodes = (self.config["execution"]["block"].get("maxBlocks", 1) *
                          self.config["execution"]["block"].get("nodes", 1))

        self.spot_max_bid = options.get("spotMaxBid", 0)

        try:
            self.initialize_boto_client()
        except Exception as e:
            logger.error("Site:[{0}] Failed to initialize".format(self))
            raise e

        try:
            self.statefile = self.config["execution"]["block"]["options"].get("stateFile",
                                                                              '.ec2site_{0}.json'.format(self.sitename))
            self.read_state_file(self.statefile)

        except Exception as e:
            self.create_vpc().id
            logger.info(
                "No State File. Cannot load previous options. Creating new infrastructure")
            self.write_state_file()

    @property
    def channels_required(self):
        ''' No channel required for EC2
        '''
        return False

    def config_logger(self):
        """Configure Logger"""
        logger = logging.getLogger("AzureProvider")
        logger.setLevel(logging.INFO)
        if not os.path.isfile(self.config['logFile']):
            with open(self.config['logFile'], 'w') as temp_log:
                temp_log.write("Creating new log file.\n")
        fh = logging.FileHandler(self.config['logFile'])
        fh.setLevel(logging.INFO)
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        logger.addHandler(fh)
        self.logger = logger

    def _read_conf(self, config_file):
        """read config file"""
        config = json.load(open(config_file, 'r'))
        return config

    def pretty_configs(self, configs):
        """prettyprint config"""
        printer = pprint.PrettyPrinter(indent=4)
        printer.pprint(configs)

    def read_configs(self, config_file):
        """Read config file"""
        config = self._read_conf(config_file)
        return config

    def ipyparallel_configuration(self):
        config = ''
        try:
            with open(os.path.expanduser(self.config['iPyParallelConfigFile'])) as f:
                config = f.read().strip()
        except Exception as e:
            self.logger.error(e)
            self.logger.info(
                "Couldn't find user iPyParallel config file. Trying default location.")
            with open(os.path.expanduser("~/.ipython/profile_parallel/security/ipcontroller-engine.json")) as f:
                config = f.read().strip()
        else:
            self.logger.error(
                "Cannot find iPyParallel config file. Cannot proceed.")
            return -1
        ipptemplate = """
cat <<EOF> ipengine.json
{}
EOF

mkdir -p '.ipengine_logs'
sleep 5
ipengine --file=ipengine.json &> .ipengine_logs/ipengine.log""".format(config)
        return ipptemplate

    def submit(self):
        """Uses AzureDeployer to spin up an instance and connect it to the iPyParallel controller"""
        self.deployer.deploy()

    def status(self):
        """Get status of azure VM. Not implemented yet."""
        raise NotImplemented

    def cancel(self):
        """Destroy an azure VM"""
        self.deployer.destroy()


if __name__ == '__main__':
    config = "azureconf.json"
