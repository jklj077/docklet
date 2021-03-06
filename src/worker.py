#!/usr/bin/python3

# first init env
import env, tools
config = env.getenv("CONFIG")
tools.loadenv(config)

# must import logger after initlogging, ugly
from log import initlogging
initlogging("docklet-worker")
from log import logger

import xmlrpc.server, sys, time
from socketserver import ThreadingMixIn
import threading
import etcdlib, container
import network

from nettools import netcontrol
from network import VSMgr
import monitor
from lvmtool import *

##################################################################
#                       Worker
# Description : Worker starts at worker node to listen rpc request and complete the work
# Init() :
#      get master ip
#      initialize lvm group
#      initialize rpc server
#          register rpc functions
#      initialize network
#          setup GRE tunnel
# Start() :
#      register in etcd
#      start rpc service
##################################################################

# imitate etcdlib to genernate the key of etcdlib manually
def generatekey(path):
    clustername = env.getenv("CLUSTER_NAME")
    return '/'+clustername+'/'+path

class ThreadXMLRPCServer(ThreadingMixIn, xmlrpc.server.SimpleXMLRPCServer):
    pass

class Worker(object):
    def __init__(self, etcdclient, addr, port):
        self.addr = addr
        self.port = port
        logger.info("begin initialize on %s" % self.addr)

        self.fspath = env.getenv('FS_PREFIX')
        self.poolsize = env.getenv('DISKPOOL_SIZE')

        self.etcd = etcdclient
        [_, self.master] = self.etcd.getkey("service/master")
        self.mode = None

        self.etcd.setkey("machines/runnodes/"+self.addr, "waiting")
        [status, _] = self.etcd.getkey("machines/runnodes/"+self.addr)
        if status:
            self.key = generatekey("machines/allnodes/"+self.addr)
        else:
            logger.error("get key failed. %s" % self.addr)
            sys.exit(1)

        # check token to check global directory
        [status, token_1] = self.etcd.getkey("token")
        tokenfile = open(self.fspath+"/global/token", 'r')
        token_2 = tokenfile.readline().strip()
        if token_1 != token_2:
            logger.error("check token failed, global directory is not a shared filesystem")
            sys.exit(1)
        logger.info("worker registered and checked the token")

        # worker search all run nodes to judge how to init
        value = 'init-new'
        [status, alllist] = self.etcd.listdir("machines/allnodes")
        for node in alllist:
            if node['key'] == self.key:
                value = 'init-recovery'
                break
        logger.info("worker start in "+value+" mode")

        self.thread_sendheartbeat = threading.Thread(target=self.sendheartbeat)

        Containers = container.Container(self.addr, etcdclient)
        if value == 'init-new':
            logger.info("init worker with mode:new")
            self.mode = 'new'
            # check global directory do not have containers on this worker
            [both, _, onlyglobal] = Containers.diff_containers()
            if len(both+onlyglobal) > 0:
                logger.error("mode:new will clean containers recorded in global, please check")
                sys.exit(1)
            [status, _] = Containers.delete_allcontainers()
            if not status:
                logger.error("delete all containers failed")
                sys.exit(1)
            # create new lvm VG at last
            new_group("docklet-group", self.poolsize, self.fspath + "/local/docklet-storage")
            #subprocess.call([self.libpath+"/lvmtool.sh", "new", "group", "docklet-group", self.poolsize, self.fspath+"/local/docklet-storage"])
        elif value == 'init-recovery':
            logger.info("init worker with mode:recovery")
            self.mode = 'recovery'
            # recover lvm VG first
            recover_group("docklet-group", self.fspath + "/local/docklet-storage")
            #subprocess.call([self.libpath+"/lvmtool.sh", "recover", "group", "docklet-group", self.fspath+"/local/docklet-storage"])
            [status, _] = Containers.check_allcontainers()
            if status:
                logger.info("all containers check ok")
            else:
                logger.info("not all containers check ok")
                #sys.exit(1)
        else:
            logger.error("worker init mode:%s not supported" % value)
            sys.exit(1)

        self.vsmgr = VSMgr(None, self.addr, self.master, self.mode)

        # initialize rpc
        # xmlrpc.server.SimpleXMLRPCServer(addr) -- addr : (ip-addr, port)
        # if ip-addr is "", it will listen ports of all IPs of this host
        logger.info("initialize rpcserver %s:%d" % (self.addr, int(self.port)))
        # logRequests=False : not print rpc log
        # self.rpcserver = xmlrpc.server.SimpleXMLRPCServer((self.addr, self.port), logRequests=False)
        self.rpcserver = ThreadXMLRPCServer((self.addr, int(self.port)), allow_none=True)
        # register functions or instances to server for rpc
        # self.rpcserver.register_function(function_name)
        self.rpcserver.register_introspection_functions()
        self.rpcserver.register_instance(Containers)
        self.rpcserver.register_function(monitor.workerFetchInfo)
        self.rpcserver.register_function(self.vsmgr.check_switch_with_gre)


    # start service of worker
    def start(self):
        self.etcd.setkey("machines/runnodes/"+self.addr, "work")
        self.thread_sendheartbeat.start()
        # start serving for rpc
        logger.info("begins to work")
        self.rpcserver.serve_forever()

    # send heardbeat package to keep alive in etcd, ttl=2s
    def sendheartbeat(self):
        while True:
            # check send heartbeat package every 1s
            time.sleep(1)
            [status, value] = self.etcd.getkey("machines/runnodes/"+self.addr)
            if status:
                # master has know the worker so we start send heartbeat package
                if value == 'ok':
                    self.etcd.setkey("machines/runnodes/"+self.addr, "ok", ttl=2)
            else:
                logger.error("get key %s failed, master crashed or initialized. restart worker please." % self.addr)
                sys.exit(1)

if __name__ == '__main__':

    etcdaddr = env.getenv("ETCD")
    logger.info("using ETCD %s" % etcdaddr)

    clustername = env.getenv("CLUSTER_NAME")
    logger.info("using CLUSTER_NAME %s" % clustername)

    # get network interface
    net_dev = env.getenv("NETWORK_DEVICE")
    logger.info("using NETWORK_DEVICE %s" % net_dev)

    ipaddr = network.getip(net_dev)
    if ipaddr is False:
        logger.error("network device is not correct")
        sys.exit(1)
    else:
        logger.info("using ipaddr %s" % ipaddr)
    # init etcdlib client
    try:
        etcdclient = etcdlib.Client(etcdaddr, prefix=clustername)
    except Exception:
        logger.error("connect etcd failed, maybe etcd address not correct...")
        sys.exit(1)
    else:
        logger.info("etcd connected")

    cpu_quota = env.getenv('CONTAINER_CPU')
    logger.info("using CONTAINER_CPU %s" % cpu_quota)

    mem_quota = env.getenv('CONTAINER_MEMORY')
    logger.info("using CONTAINER_MEMORY %s" % mem_quota)

    worker_port = env.getenv('WORKER_PORT')
    logger.info("using WORKER_PORT %s" % worker_port)

    # init collector to collect monitor infomation
    con_collector = monitor.Container_Collector()
    con_collector.start()
    collector = monitor.Collector()
    collector.start()
    logger.info("CPU and Memory usage monitor started")

    logger.info("Starting worker")
    worker = Worker(etcdclient, addr=ipaddr, port=worker_port)
    worker.start()
