import threading
import time

PLUGIN_NAME = "dogstatsd"
DEFAULT_SOCKET = None
DEFAULT_IP = "0.0.0.0"
MAX_RECV_SIZE = 65535
INGEST_URL = "https://ingest.signalfx.com"

import dogstatsd


class Logger(object):
    def __init__(self, collectd_module):
        self.verbose_logging = False
        self.collectd_module = collectd_module

    def error(self, msg):
        self.collectd_module.error(
            '{name}: {msg}'.format(name=PLUGIN_NAME, msg=msg))

    def info(self, msg):
        self.collectd_module.info(
            '{name}: {msg}'.format(name=PLUGIN_NAME, msg=msg))

    def notice(self, msg):
        self.collectd_module.warning(
            '{name}: {msg}'.format(name=PLUGIN_NAME, msg=msg))

    def warning(self, msg):
        self.collectd_module.notice(
            '{name}: {msg}'.format(name=PLUGIN_NAME, msg=msg))

    def verbose(self, msg, *args):
        if self.verbose_logging:
            msg_out = msg.format(*args)
            self.collectd_module.info(
                '{name}: {msg}'.format(name=PLUGIN_NAME, msg=msg_out))


DOG_STATSD_TYPE_TO_COLLECTD_TYPE = {
    "gauge": "gauge",
    "rate": "absolute",
}


# pylint: disable=too-few-public-methods,too-many-instance-attributes
class DogstatsDConfig(object):
    def __init__(self, log, timeout=dogstatsd.UDP_SOCKET_TIMEOUT):
        self.udp_timeout = timeout
        self.listen_port = DEFAULT_SOCKET
        self.verbose_logging = False
        self.listen_ip = DEFAULT_IP
        self.max_recv_size = MAX_RECV_SIZE
        self.aggregator_interval = dogstatsd.DOGSTATSD_AGGREGATOR_BUCKET_SIZE
        self.read_to_collectd = False
        self.ingest_endpoint = INGEST_URL
        self.api_token = ""
        self.log = log
        self.collectd_send = False

    def configure_callback(self, conf):
        self.log.info("Configure callback")
        for node in conf.children:
            if node.key == "DogStatsDPort":
                self.listen_port = int(node.values[0])
            elif node.key == "IP":
                self.listen_ip = node.values[0]
            elif node.key == "Verbose":
                self.verbose_logging = bool(node.values[0])
            elif node.key == "MaxPacket":
                self.max_recv_size = int(node.values[0])
            elif node.key == "Interval":
                self.aggregator_interval = int(node.values[0])
            elif node.key == "ReadToCollectd":
                self.read_to_collectd = bool(node.values[0])
            elif node.key == "IngestEndpoint":
                self.ingest_endpoint = node.values[0]
            elif node.key == 'Token':
                self.api_token = node.values[0]
            elif node.key == "collectdsend":
                self.collectd_send = bool(node.values[0])
            # else:
            #     self.log.warning('Unknown config key: %s' % node.key)


def filter_signalfx_dimension(dogstatsddim):
    invalid_chars = "[],=:"
    ret = ""
    for dogchr in dogstatsddim:
        if dogchr in invalid_chars:
            ret += "_"
        else:
            ret += dogchr
    return ret


def dims_from_tags(tags):
    ret = {}
    if tags is None:
        return ret
    for tag in tags:
        parts = tag.split(":", 1)
        if len(parts) == 0:
            # Skip labels
            continue
        ret[parts[0]] = parts[1]
    return ret


def combine_dims(dims):
    if len(dims) == 0:
        return ""
    ret = []
    for dim_k, dim_v in dims.items():
        ret.append(filter_signalfx_dimension(dim_k) + "=" +
                   filter_signalfx_dimension(dim_v))
    return "[" + ",".join(ret) + "]"


class SignalfxPointSender(object):
    def __init__(self, config, log):
        self.config = config
        self.log = log
        import signalfx
        self.sfx = signalfx.SignalFx(config.api_token,
                                     ingest_endpoint=config.ingest_endpoint)

    def send_points(self, metrics):
        gauges = []
        counters = []
        for metric in metrics:
            sfx_metric = {}
            if metric['type'] in DOG_STATSD_TYPE_TO_COLLECTD_TYPE:
                mtype = DOG_STATSD_TYPE_TO_COLLECTD_TYPE[metric['type']]
            else:
                mtype = 'gauge'

            if mtype == "absolute":
                mtype = "counter"

            sfx_metric["metric"] = metric['metric']
            sfx_metric["dimensions"] = dims_from_tags(metric['tags'])
            sfx_metric["timestamp"] = int(metric['points'][0][0] * 1000)
            sfx_metric["value"] = metric['points'][0][1]
            if metric['type'] == "rate":
                sfx_metric["value"] *= self.config.aggregator_interval

            if mtype == "gauge":
                gauges.append(sfx_metric)
            elif mtype == "counter":
                counters.append(sfx_metric)
        self.log.verbose("Sending %d metrics" % len(metrics))
        self.sfx.send(gauges=gauges, counters=counters)


class CollectDPointSender(object):
    def __init__(self, config, Values, plugin, log):
        # pylint: disable=invalid-name
        self.Values = Values
        self.config = config
        self.log = log
        self.plugin = plugin

    def send_points(self, metrics):
        for metric in metrics:
            val = self.Values(plugin=self.plugin, meta={'0': True})

            if metric['type'] in DOG_STATSD_TYPE_TO_COLLECTD_TYPE:
                val.type = DOG_STATSD_TYPE_TO_COLLECTD_TYPE[metric['type']]
            else:
                val.type = 'gauge'

            val.type_instance = metric['metric']
            val.plugin_instance = combine_dims(dims_from_tags(metric['tags']))
            val.values = [metric['points'][0][1]]
            if metric['type'] == "rate":
                val.values[0] *= self.config.aggregator_interval

            self.log.verbose("m: {} v: {}", metric, val)
            val.dispatch()


class DogstatsDCollectD(object):

    # pylint: disable=too-many-instance-attributes
    # Reasonable to leave the config here
    def __init__(self, collectd_module, plugin='dogstatsd', register=False):
        self.log = Logger(collectd_module)
        self.plugin = plugin
        self.server = None
        self.config = DogstatsDConfig(self.log)
        self.sender = CollectDPointSender(self.config, collectd_module.Values,
                                          self.plugin, self.log)
        if register:
            self.register(collectd_module)

    def register(self, collectd_module):
        collectd_module.register_config(self.config.configure_callback)
        collectd_module.register_init(self.init_callback)
        collectd_module.register_read(self.read_callback)
        collectd_module.register_shutdown(self.register_shutdown)

    def read_callback(self):
        if self.server is None:
            return
        metrics = self.server.metrics_aggregator.flush()
        self.sender.send_points(metrics)

    def init_callback(self):
        self.log.info("plugin init %s" % self.config)
        if self.config.verbose_logging is True:
            self.log.verbose_logging = True
        assert self.server is None
        if self.config.listen_port is None:
            self.log.info("dogstatsd port listening not enabled")
            return
        if not self.config.collectd_send:
            self.sender = SignalfxPointSender(self.config, self.log)

        self.server = dogstatsd.init(
            self.config.listen_ip, self.config.listen_port,
            timeout=self.config.udp_timeout,
            aggregator_interval=self.config.aggregator_interval)
        udp_server_thread = threading.Thread(target=self.server.start)
        udp_server_thread.daemon = True
        udp_server_thread.start()

    def register_shutdown(self):
        self.log.info("shutting down plugin")
        if self.server is None:
            return
        self.server.stop()
        while not self.server.start_has_finished.acquire(False):
            time.sleep(.01)
        self.server.start_has_finished.release()
        self.server = None
