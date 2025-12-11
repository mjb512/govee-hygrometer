#!/usr/bin/env python3
# decoding stolen from 
# https://github.com/Home-Is-Where-You-Hang-Your-Hack/sensor.goveetemp_bt_hci/blob/master/custom_components/govee_ble_hci/govee_advertisement.py

import json
import logging
import os
from socket import gethostname
import sys
import time
import yaml

from bleson import get_provider, Observer
from bleson.core.hci.type_converters import hex_string
from prometheus_client import start_http_server, Summary, Counter, Gauge
import paho.mqtt.client as mqtt
from pymemcache.client.base import PooledClient as MemcacheClient

VERSION = "1.3"

G_TEMP = Gauge('govee_temperature', 'Reported temperature', ['address', 'name'])
G_HUMI = Gauge('govee_humidity', 'Reported relative humidity', ['address', 'name'])
G_BATT = Gauge('govee_battery', 'Reported battery level', ['address', 'name'])
G_RSSI = Gauge('govee_rssi', 'Reported RSSI', ['address', 'name', 'receiver'])


class Collector:
    def __init__(self, config_file):
        self.log = logging.getLogger(__name__)
        self.log.info(f'Version {VERSION}')
        self.log.info('Loading config.')
        self.load_conf(config_file)
        self.log.level = logging.__dict__[self.config['logging']]
        if self.mqtt_enabled:
            self.log.info('Connecting to MQTT')
            self.mqtt_init()
        if self.memcache_enabled:
            self.log.info('Connecting to Memcached')
            self.memcache_init()
        self.log.info('Running...')
        self.hostname = gethostname()

    def load_conf(self, config_file):
        with open(config_file, 'r') as fh:
            config = yaml.safe_load(fh)
        self.govees = config['govees'] or {}
        self.config = config['collector']
        self.mode = self.config['mode']

        if 'mqtt' in self.config:
            self.mqtt_enabled = self.config['mqtt']['enable']
        else:
            self.mqtt_enabled = False
        self.log.info(f'MQTT support: {self.mqtt_enabled}')

        if 'memcache' in self.config:
            self.memcache_enabled = self.config['memcache']['enable']
        else:
            self.memcache_enabled = False
        self.log.info(f'Memcached support: {self.memcache_enabled}')

    def mqtt_init(self):
        self.mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.mqttc.on_connect = self.__mqtt_on_connect
        self.mqttc.connect(self.config['mqtt']['server'], self.config['mqtt']['port'], 5)
        self.mqttc.loop_start()

    def __mqtt_on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self.log.error(f"Failed to connect to MQTT: {reason_code}")
            sys.exit(1)

    def memcache_init(self):
        # Use a pool, because it handles reconnections, timeouts, etc
        # should prob do the same with mqtt, or just die...
        server = (self.config['memcache']['server'], self.config['memcache']['port'])
        self.memcache = MemcacheClient(server, max_pool_size=1)

    @staticmethod
    def decode_temps(packet_value: int) -> float:
        """Decode potential negative temperatures."""
        # https://github.com/Thrilleratplay/GoveeWatcher/issues/2
        if packet_value & 0x800000:
            return float((packet_value ^ 0x800000) / -10000)
        return round(float(packet_value / 10000), 2)

    def on_advertisement(self, advertisement):
        a = advertisement
        if a.name is None:
            #print(f"no name! {a}")
            return
        if a.name not in self.govees.keys():
            if a.name.startswith("GVH"):
                self.log.warning(f"unknown device: {a.name} : {a}")
            return
        name = self.govees[a.name]['name']

        trv_id = None
        if 'trv_id' in self.govees[a.name]:
            trv_id = self.govees[a.name]['trv_id']

        #hexdump.hexdump(a.mfg_data)
        packet = int(hex_string(a.mfg_data[3:6]).replace(" ", ""), 16)

        temperature = self.decode_temps(packet)
        humidity = float((packet % 1000) / 10)
        battery = int(a.mfg_data[6])

        self.log.debug(f"{a.name} {name:15s} {packet:6d} {temperature=:.3f} {humidity=:.3f} {battery=:.3f} rssi={a.rssi}")

        # Fix weird bug
        if temperature <= 0.0 and humidity < 60.8:
            humidity += 39.2

        if self.mode == 'active':
            G_TEMP.labels(a.address.address, name).set(temperature)
            G_HUMI.labels(a.address.address, name).set(humidity)
            G_BATT.labels(a.address.address, name).set(battery)
            G_RSSI.labels(a.address.address, name, self.hostname).set(a.rssi)
            payload = {
                'version': VERSION,
                'ts': time.time(),
                'battery': battery,
                'ble_address': a.address.address,
                'ble_name': a.name,
                'config_name': name,
                'humidity': humidity,
                'packet': packet,
                'rssi': a.rssi,
                'received_by': gethostname(),
                'temperature': temperature,
                'trv_id': trv_id
            }

            # dupes shouldn't matter
            if self.mqtt_enabled:
                self.mqttc.publish('govee-hygrometers/readings', json.dumps(payload))
                self.mqttc.publish(f'govee-hygrometers/{a.name}/readings', json.dumps(payload))

            if self.memcache_enabled:
                self.memcache.set(f"govee_hygrometers_{a.name}", json.dumps(payload))
                self.memcache.set(f"govee_hygrometers_rssi_{a.name}_{self.hostname}", payload['rssi'])

                known_receivers = json.loads(self.memcache.get("govee_hygrometers_receivers") or "[]")
                if self.hostname not in known_receivers:
                    self.log.info(f"Nobody knows about me. Receivers: {known_receivers}")
                    try:
                        self.memcache.add("govee_hygrometers_receivers__lock", gethostname())
                        known_receivers.append(self.hostname)
                        self.memcache.set("govee_hygrometers_receivers", json.dumps(known_receivers))
                        self.memcache.delete("govee_hygrometers_receivers__lock")
                    except:
                        self.log.exception('adding to govee receivers list failed. might be a lock issue')

                known_govees = json.loads(self.memcache.get("govee_hygrometers") or "[]")
                if a.name not in known_govees:
                    self.log.info(f'New govee found: {a.name}')
                    try:
                        self.memcache.add("govee_hygrometers__lock", gethostname())
                        known_govees.append(a.name)
                        self.memcache.set("govee_hygrometers", json.dumps(known_govees))
                        self.memcache.delete("govee_hygrometers__lock")
                    except:
                        self.log.exception('adding to govee list failed. might be a lock issue')

    def run(self):
        adapter = get_provider().get_adapter()
        observer = Observer(adapter)
        observer.on_advertising_data = self.on_advertisement
        observer.start()

        start_http_server(38256)            
        while True:
            time.sleep(1)


if __name__ == "__main__":
    #logging.RootLogger.addHandler(logging.NullHandler())
    #logging.root.setLevel(logging.ERROR)
    logging.getLogger('bleson').setLevel(logging.ERROR)
    c = Collector(os.environ['CONFIG_FILE'])
    c.run()
