#!/usr/bin/python

import fcntl
import array
import os
import os.path
import random
import re
import struct
import socket
import sys
import string
import platform
import signal
import subprocess
import time
import binascii
import zlib

import psutil

import collectd_dogstatsd

try:
    import urllib.request as urllib2
except ImportError:
    import urllib2

try:
    import json
except ImportError:
    import simplejson as json

try:
    str.decode("ami3?")
    bytes = str
except:
    pass

try:
    import collectd
    import logging

    logging.basicConfig(level=logging.INFO)
except ImportError:
    try:
        import dummy_collectd as collectd
    except:
        pass

PLUGIN_NAME = 'signalfx-metadata'
API_TOKEN = ""
TIMEOUT = 10
POST_URL = "https://ingest.signalfx.com/v1/collectd"
VERSION = "0.0.10"
NOTIFY_LEVEL = -1
HOST_TYPE_INSTANCE = "host-meta-data"
TOP_TYPE_INSTANCE = "top-info"
TYPE = "objects"
NEXT_METADATA_SEND = 0
NEXT_METADATA_SEND_INTERVAL = [1, 60, 3600 + random.randint(0, 60), 86400
                               + random.randint(0, 600)]
LAST = 0
AWS = True
PROCESS_INFO = True
INTERVAL = 10
FUDGE = 1.0  # fudge to check intervals
HOST = ""

DOGSTATSD_INSTANCE = collectd_dogstatsd.DogstatsDCollectD(collectd)


class LargeNotif:
    """
    Used because the Python plugin supplied notification does not provide
    us with enough space
    """
    host = ""
    message = ""
    plugin = PLUGIN_NAME
    plugin_instance = ""
    severity = 4
    time = 0
    type = TYPE
    type_instance = ""

    def __init__(self, message, type_instance="", plugin_instance=""):
        self.plugin_instance = plugin_instance
        self.type_instance = type_instance
        self.message = message
        self.host = HOST

    def __repr__(self):
        return 'PUTNOTIF %s/%s-%s/%s-%s %s' % (self.host, self.plugin,
                                               self.plugin_instance,
                                               self.type, self.type_instance,
                                               self.message)


def log(param):
    """ log messages and understand if we're in collectd or a program """
    if __name__ != '__main__':
        collectd.info("%s: %s" % (PLUGIN_NAME, param))
    else:
        sys.stderr.write("%s\n" % param)


def plugin_config(conf):
    """
    :param conf:
      https://collectd.org/documentation/manpages/collectd-python.5.shtml#config

    Parse the config object for config parameters:
      ProcessInfo: true or false, whether or not to collect process
        information. Default is true.
      Notifications: true or false, whether or not to emit notifications
      if Notifications is true:
        URL: where to POST the notifications to
        Token: what auth to send along
        Timeout: timeout for the POST
        NotifyLevel: what is the lowest level of notification to emit.
          Default is to only emit notifications generated by this plugin
    """

    DOGSTATSD_INSTANCE.config.configure_callback(conf)

    for kv in conf.children:
        if kv.key == 'Notifications':
            if kv.values[0]:
                collectd.register_notification(receive_notifications)
        elif kv.key == 'ProcessInfo':
            if kv.values[0]:
                global PROCESS_INFO
                PROCESS_INFO = True
        elif kv.key == 'URL':
            global POST_URL
            POST_URL = kv.values[0]
        elif kv.key == 'Token':
            global API_TOKEN
            API_TOKEN = kv.values[0]
        elif kv.key == 'Timeout':
            global TIMEOUT
            TIMEOUT = int(kv.values[0])
        elif kv.key == 'Interval':
            global INTERVAL
            INTERVAL = int(kv.values[0])
        elif kv.key == 'NotifyLevel':
            global NOTIFY_LEVEL
            if string.lower(kv.values[0]) == "okay":
                NOTIFY_LEVEL = 4
            elif string.lower(kv.values[0]) == "warning":
                NOTIFY_LEVEL = 2
            elif string.lower(kv.values[0]) == "failure":
                NOTIFY_LEVEL = 1


def compact(thing):
    return json.dumps(thing, separators=(',', ':'))


def send():
    """
    Send proof-of-life datapoint, top, and notifications if interval elapsed

    dimensions existing
    """
    global LAST
    DOGSTATSD_INSTANCE.read_callback()
    diff = time.time() - LAST + FUDGE
    if diff < INTERVAL:
        log("interval not expired %s" % str(diff))
        return

    send_datapoint()
    send_top()

    # race condition with host dimension existing
    # don't send metadata on initial iteration, but on second
    # send it then on minute later, then one hour, then one day, then once a
    # day from then on but off by a fudge factor
    global NEXT_METADATA_SEND
    if NEXT_METADATA_SEND == 0:
        NEXT_METADATA_SEND = time.time() + NEXT_METADATA_SEND_INTERVAL.pop(0)
        log("waiting one interval before sending notifications")
    if NEXT_METADATA_SEND < time.time():
        send_notifications()
        if len(NEXT_METADATA_SEND_INTERVAL) > 1:
            NEXT_METADATA_SEND = \
                time.time() + NEXT_METADATA_SEND_INTERVAL.pop(0)
        else:
            NEXT_METADATA_SEND = time.time() + NEXT_METADATA_SEND_INTERVAL[0]
        log("till next metadata " + str(NEXT_METADATA_SEND - time.time()))

    LAST = time.time()


def all_interfaces():
    """
    source # http://bit.ly/1K8LIFH
    could use netifaces but want to package as little code as possible

    :return: all ip addresses by interface
    """
    is_64bits = struct.calcsize("P") == 8
    struct_size = 32
    if is_64bits:
        struct_size = 40
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    max_possible = 8  # initial value
    while True:
        _bytes = max_possible * struct_size
        names = array.array('B')
        for i in range(0, _bytes):
            names.append(0)
        outbytes = struct.unpack('iL', fcntl.ioctl(
            s.fileno(),
            0x8912,  # SIOCGIFCONF
            struct.pack('iL', _bytes, names.buffer_info()[0])
        ))[0]
        if outbytes == _bytes:
            max_possible *= 2
        else:
            break
    namestr = names.tostring()
    ifaces = []
    for i in range(0, outbytes, struct_size):
        iface_name = bytes.decode(namestr[i:i + 16]).split('\0', 1)[0]
        iface_addr = socket.inet_ntoa(namestr[i + 20:i + 24])
        ifaces.append((iface_name, iface_addr))

    return ifaces


def get_interfaces(host_info={}):
    """populate host_info with the ipaddress and fqdn for each interface"""
    interfaces = {}
    for interface, ipaddress in all_interfaces():
        if ipaddress == "127.0.0.1":
            continue
        interfaces[interface] = \
            (ipaddress, socket.getfqdn(ipaddress))
    host_info["sf_host_interfaces"] = compact(interfaces)


def get_cpu_info(host_info={}):
    """populate host_info with cpu information"""
    with open("/proc/cpuinfo") as f:
        nb_cpu = 0
        nb_cores = 0
        nb_units = 0
        for p in f.readlines():
            if ':' in p:
                x, y = map(lambda x: x.strip(), p.split(':', 1))
                if x.startswith("physical id"):
                    if nb_cpu < int(y):
                        nb_cpu = int(y)
                if x.startswith("cpu cores"):
                    if nb_cores < int(y):
                        nb_cores = int(y)
                if x.startswith("processor"):
                    if nb_units < int(y):
                        nb_units = int(y)
                if x.startswith("model name"):
                    model = y

        nb_cpu += 1
        nb_units += 1
        host_info["host_cpu_model"] = model
        host_info["host_physical_cpus"] = str(nb_cpu)
        host_info["host_cpu_cores"] = str(nb_cores)
        host_info["host_logical_cpus"] = str(nb_units)

    return host_info


def get_kernel_info(host_info={}):
    """
    gets kernal information from platform, relies on the restore_sigchld
    call above to work on python 2.6
    """
    try:
        host_info["host_kernel_name"] = platform.system()
        host_info["host_kernel_release"] = platform.release()
        host_info["host_kernel_version"] = platform.version()
        host_info["host_machine"] = platform.machine()
        host_info["host_processor"] = platform.processor()
    except:
        log("still seeing exception in platform module")

    return host_info


def get_aws_info(host_info={}):
    """
    call into aws to get some information about the instance, timeout really
    small for non aws systems and only try the once per startup
    """
    global AWS
    if not AWS:
        return host_info

    url = "http://169.254.169.254/latest/dynamic/instance-identity/document"
    try:
        req = urllib2.Request(url)
        response = urllib2.urlopen(req, timeout=0.1)
        identity = json.loads(response.read())
        want = {
            'availability_zone': 'availabilityZone',
            'instance_type': 'instanceType',
            'instance_id': 'instanceId',
            'image_id': 'imageId',
            'account_id': 'accountId',
            'region': 'region',
            'architecture': 'architecture',
        }
        for k, v in iter(want.items()):
            host_info["aws_" + k] = identity[v]
    except:
        log("not an aws box")
        AWS = False

    return host_info


def popen(command):
    """ using subprocess instead of check_output for 2.6 comparability """
    output = subprocess.Popen(command, stdout=subprocess.PIPE).communicate()[0]
    return output.strip()


def get_collectd_version(host_info={}):
    """
    exec the pid (which will be collectd) with help and parse the help
    message for the version information
    """
    host_info["host_collectd_version"] = "UNKNOWN"
    try:
        output = popen(["/proc/self/exe", "-h"])
        regexed = re.search("collectd (.*), http://collectd.org/",
                            output.decode())
        if regexed:
            host_info["host_collectd_version"] = regexed.groups()[0]
    except Exception:
        t, e = sys.exc_info()[:2]
        log("trying to parse collectd version failed %s" % e)

    return host_info


def getLsbRelease(host_info={}):
    if os.path.isfile("/etc/lsb-release"):
        with open("/etc/lsb-release") as f:
            for line in f.readlines():
                regexed = re.search('DISTRIB_DESCRIPTION="(.*)"', line)
                if regexed:
                    host_info["host_linux_version"] = regexed.groups()[0]
                    return host_info["host_linux_version"]


def getOsRelease(host_info={}):
    if os.path.isfile("/etc/os-release"):
        with open("/etc/os-release") as f:
            for line in f.readlines():
                regexed = re.search('PRETTY_NAME="(.*)"', line)
                if regexed:
                    host_info["host_linux_version"] = regexed.groups()[0]
                    return host_info["host_linux_version"]


def getCentos(host_info={}):
    for file in ["/etc/centos-release", "/etc/redhat-release",
                 "/etc/system-release"]:
        if os.path.isfile(file):
            with open(file) as f:
                line = f.read()
                host_info["host_linux_version"] = line.strip()
                return host_info["host_linux_version"]


def get_linux_version(host_info={}):
    """
    read a variety of files to figure out linux version
    """

    for f in [getLsbRelease, getOsRelease, getCentos]:
        if f(host_info):
            return

    host_info["host_linux_version"] = "UNKNOWN"
    return host_info


def parse_bytes(possible_bytes):
    """bytes can be compressed with suffixes but we want real numbers in kb"""
    try:
        return int(possible_bytes)
    except:
        if possible_bytes[-1].lower() == 'm':
            return int(float(possible_bytes[:-1]) * 1024)
        if possible_bytes[-1].lower() == 'g':
            return int(float(possible_bytes[:-1]) * 1024 ** 2)
        if possible_bytes[-1].lower() == 't':
            return int(float(possible_bytes[:-1]) * 1024 ** 3)
        if possible_bytes[-1].lower() == 'p':
            return int(float(possible_bytes[:-1]) * 1024 ** 4)
        if possible_bytes[-1].lower() == 'e':
            return int(float(possible_bytes[:-1]) * 1024 ** 5)


def parse_priority(priority):
    """
    priority can sometimes be "rt" for real time, make that 99, the highest
    """
    try:
        return int(priority)
    except:
        return 99


def to_time(secs):
    minutes = int(secs / 60)
    seconds = secs % 60.0
    sec = int(seconds)
    dec = int((seconds - sec) * 100)
    return "%02d:%02d.%02d" % (minutes, sec, dec)


def read_proc_file(pid, file, field=None):
    with open("/proc/%s/%s" % (pid, file)) as f:
        if not field:
            return f.read().strip()
        for x in f.readlines():
            if x.startswith(field):
                return x.split(":")[1].strip()


def get_priority(pid):
    try:
        val = read_proc_file(pid, "sched", "prio")
        val = int(val) - 100
        if val < 0:
            val = 99
    except Exception:
        t, e = sys.exc_info()[:2]
        sys.stdout.write(str(e))
        log("unsuccessful read of priority: %s" % str(e))
        val = 0
    return val


def get_command(p):
    val = " ".join(p.cmdline())
    if not val:
        val = read_proc_file(p.pid, "status", "Name")
        val = "[%s]" % val
    return val


def get_nice(p):
    val = read_proc_file(p.pid, "stat")
    return val.split()[18]


def send_top():
    """
    Parse top unless told not to
    filter out any zeros and common values to save space send it directly
    without going through collectd mechanisms because it is too large
    """
    if not PROCESS_INFO:
        return

    status_map = {
        "sleeping": "S",
        "uninterruptible sleep": "D",
        "running": "R",
        "traced": "T",
        "stopped": "T",
        "zombie": "Z",
    }

    # send version up with the values
    response = {"v": VERSION}
    top = {}
    for p in psutil.process_iter():
        try:
            top[p.pid] = [
                p.username(),  # user
                get_priority(p.pid),  # priority
                get_nice(p),  # nice value, numerical
                p.memory_info_ex()[1],  # virtual memory size in kb int
                p.memory_info_ex()[0],  # resident memory size in kd int
                p.memory_info_ex()[2],  # shared memory size in kb int
                status_map.get(p.status(), "D"),  # process status
                p.cpu_percent(),  # % cpu, float
                p.memory_percent(),  # % mem, float
                to_time(p.cpu_times().system + p.cpu_times().user),  # cpu time
                get_command(p)  # command
            ]
        except Exception:
            t, e = sys.exc_info()[:2]
            sys.stdout.write(str(e))
            log("pid disappeared %d: %s" % (p.pid, str(e)))

    s = compact(top)
    compressed = zlib.compress(s.encode("utf-8"))
    base64 = binascii.b2a_base64(compressed)
    response["t"] = base64.decode("utf-8")
    response_json = compact(response)
    notif = LargeNotif(response_json, TOP_TYPE_INSTANCE, VERSION)
    receive_notifications(notif)


def get_memory(host_info):
    """get total physical memory for machine"""
    with open("/proc/meminfo") as f:
        pieces = f.readline()
        _, mem_total, _ = pieces.split()
        host_info["host_mem_total"] = mem_total

    return host_info


def get_host_info():
    """ aggregate all host info """
    host_info = {"host_metadata_version": VERSION}
    get_cpu_info(host_info)
    get_kernel_info(host_info)
    get_aws_info(host_info)
    get_collectd_version(host_info)
    get_linux_version(host_info)
    get_memory(host_info)
    get_interfaces(host_info)
    return host_info


def map_diff(host_info, old_host_info):
    """
    diff old and new host_info for additions of modifications
    don't look for removals as they will likely be spurious
    """
    diff = {}
    for k, v in iter(host_info.items()):
        if k not in old_host_info:
            diff[k] = v
        elif old_host_info[k] != v:
            diff[k] = v
    return diff


def put_val(pname, metric, val):
    """Create collectd metric"""

    if __name__ != "__main__":
        collectd.Values(plugin=PLUGIN_NAME,
                        plugin_instance=pname,
                        meta={'0': True},
                        type=val[1].lower(),
                        type_instance=metric,
                        values=[val[0]]).dispatch()
    else:
        h = platform.node()
        print('PUTVAL %s/%s/%s-%s interval=%d N:%s' % (
            h, PLUGIN_NAME, val[1].lower(), metric, INTERVAL, val[0]))


def get_uptime():
    """get uptime for machine"""
    with open("/proc/uptime") as f:
        pieces = f.read()
        uptime, idle_time = pieces.split()
        return uptime

    return None


def send_datapoint():
    """write proof-of-life datapoint"""
    put_val("", "sf.host-uptime", [get_uptime(), "gauge"])


def putnotif(property_name, message, plugin_name=PLUGIN_NAME,
             type_instance=HOST_TYPE_INSTANCE, type=TYPE):
    """Create collectd notification"""
    if __name__ != "__main__":
        notif = collectd.Notification(plugin=plugin_name,
                                      plugin_instance=property_name,
                                      type_instance=type_instance,
                                      type=type)
        notif.severity = 4  # OKAY
        notif.message = message
        notif.dispatch()
    else:
        h = platform.node()
        print('PUTNOTIF %s/%s-%s/%s-%s %s' % (h, plugin_name, property_name,
                                              type, type_instance, message))


def write_notifications(host_info):
    """emit any new notifications"""
    for property_name, property_value in iter(host_info.items()):
        if len(property_value) > 255:
            receive_notifications(LargeNotif(property_value,
                                             HOST_TYPE_INSTANCE,
                                             property_name))
        else:
            putnotif(property_name, property_value)


def send_notifications():
    host_info = get_host_info()
    write_notifications(host_info)


def get_severity(severity_int):
    """helper meethod to swap severities"""
    return {
        1: "FAILURE",
        2: "WARNING",
        4: "OKAY"
    }[severity_int]


def receive_notifications(notif):
    """
    callback to consume notifications from collectd and emit them to SignalFx.
    callback will only be called if Notifications was configured to be true.
    Only send notifications created by other plugs which are above or equal
    the configured NotifyLevel.
    """
    if not notif:
        return

    if __name__ == "__main__":
        log(notif)
        return

    # we send our own notifications but we don't have access to collectd's
    # "host" from collectd.conf steal it from notifications we've put on the
    # bus so we can use it for our own
    global HOST
    if not HOST and notif.host:
        HOST = notif.host
        log("found host " + HOST)

    if not API_TOKEN:
        return

    notif_dict = {}
    # because collectd c->python is a bit limited and lacks __dict__
    for x in ['host', 'message', 'plugin', 'plugin_instance', 'severity',
              'time', 'type', 'type_instance']:
        notif_dict[x] = getattr(notif, x, "")

    # emit notifications that are ours, or satisfy the notify level
    if notif_dict['plugin'] != PLUGIN_NAME and notif_dict['type'] != TYPE \
            and notif_dict['type_instance'] not in [HOST_TYPE_INSTANCE, TOP_TYPE_INSTANCE] \
            and notif_dict["severity"] > NOTIFY_LEVEL:
        log("event ignored: " + str(notif_dict))
        return

    if not notif_dict["time"]:
        notif_dict["time"] = time.time()
    if not notif_dict["host"]:
        if HOST:
            notif_dict["host"] = HOST
        else:
            notif_dict["host"] = platform.node()
        log("no host info, setting to " + notif_dict["host"])

    notif_dict["severity"] = get_severity(notif_dict["severity"])
    data = compact([notif_dict])
    headers = {"Content-Type": "application/json"}
    if API_TOKEN != "":
        headers["X-SF-TOKEN"] = API_TOKEN
    try:
        req = urllib2.Request(POST_URL, data, headers)
        r = urllib2.urlopen(req, timeout=TIMEOUT)
        sys.stdout.write(string.strip(r.read()))
    except Exception:
        t, e = sys.exc_info()[:2]
        sys.stdout.write(str(e))
        log("unsuccessful response: %s" % str(e))


def restore_sigchld():
    """
    Restores the SIGCHLD handler if needed

    See https://github.com/deniszh/collectd-iostat-python/issues/2 for
    details.
    """
    try:
        platform.system()
    except:
        log("executing SIGCHLD workaround")
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    if __name__ != "__main__":
        DOGSTATSD_INSTANCE.init_callback()


# Note: Importing collectd_dogstatsd registers its own endpoints

if __name__ != "__main__":
    # when running inside plugin
    collectd.register_init(restore_sigchld)
    collectd.register_config(plugin_config)
    collectd.register_read(send)
    collectd.register_shutdown(DOGSTATSD_INSTANCE.register_shutdown)

else:
    # outside plugin just collect the info
    restore_sigchld()
    send()
    log(json.dumps(get_host_info(), sort_keys=True,
                   indent=4, separators=(',', ': ')))
    if len(sys.argv) < 2:
        while True:
            time.sleep(INTERVAL)
            send()
