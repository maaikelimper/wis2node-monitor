###############################################################################
#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#
###############################################################################

import os
import logging

import requests

import paho.mqtt.client as mqtt
import random

import ssl
import sys
import json
import time

from threading import Lock
from threading import Thread

from prometheus_client import start_http_server, Counter, Gauge

# de-register default-collectors
from prometheus_client import REGISTRY, PROCESS_COLLECTOR, PLATFORM_COLLECTOR

message_buffer = []
buffer_lock = Lock()

REGISTRY.unregister(PROCESS_COLLECTOR)
REGISTRY.unregister(PLATFORM_COLLECTOR)

# remove python-garbage-collector metrics
REGISTRY.unregister(
    REGISTRY._names_to_collectors['python_gc_objects_uncollectable_total'])

LOGGING_LEVEL = os.environ.get('LOG_LOGLEVEL', 'INFO')
# gotta log to stdout so docker logs sees the python-logs
logging.basicConfig(stream=sys.stdout)
# create our own logger using same log-level as wis2box
logger = logging.getLogger('mqtt_metrics_collector')
logger.setLevel(LOGGING_LEVEL)

INTERRUPT = False


wis2node_active = Gauge('wis2node_monitor_active',
                    'wis2node active by centre_id',
                    ["centre_id"])
metadata_received = Counter('wis2node_monitor_metadata_received',
                            'metadata notifications received by centre_id and generated_by',
                            ["centre_id", "generated_by"])
synop_messages_received = Counter('wis2node_monitor_synop_messages_received',
                                'synop messages received by centre_id and generated_by',
                                ["centre_id", "generated_by"])

class MetricsCollector:
    def __init__(self):
        self.message_buffer = []
        self.buffer_lock = Lock()

    def update_wis2node_active_gauge(self, centre_id_list):
        """
        function to update the wis2node_active-gauge

        :param centre_id_list: list of centre_ids

        :returns: `None`
        """

        wis2node_active._metrics.clear()
        for centre_id in centre_id_list:
            wis2node_active.labels(centre_id).set(0)

    def init_wis2node_active_gauge(self):
        """
        function to initialize the wis2node_active-gauge

        :returns: `None`
        """

        centre_id_list = []
        # TODO: get centre_id_list from wis2-registry
        self.update_wis2node_active_gauge(centre_id_list)

    def sub_connect(self, client, userdata, flags, rc, properties=None):
        """
        function executed 'on_connect' for paho.mqtt.client

        :param client: client-object associated to 'on_connect'
        :param userdata: userdata
        :param flags: flags
        :param rc: return-code received 'on_connect'
        :param properties: properties

        :returns: `None`
        """

        # topics to subscribe to
        topics = [
            'origin/a/wis2/+/data/core/weather/surface-based-observations/synop',
            'origin/a/wis2/+/metadata'	
        ]

        logger.info(f"on connection to subscribe: {mqtt.connack_string(rc)}")
        for s in topics:
            logger.info(f"subscribing to topic: {s}")
            client.subscribe(s, qos=1)

    def sub_mqtt_metrics(self, client, userdata, msg):
        """
        function executed 'on_message' for paho.mqtt.client
        updates counters for each new message received

        :param client: client-object associated to 'on_message'
        :param userdata: MQTT-userdata
        :param msg: MQTT-message-object received by subscriber

        :returns: `None`
        """

        # parse the centre_id from the topic
        centre_id = msg.topic.split('/')[3]

        #logger.info(f"Received message from centre_id={centre_id}")

        # update the gauge
        wis2node_active.labels(centre_id).set(1)

        # add received message to buffer
        with self.buffer_lock:
            self.message_buffer.append((msg.topic, msg))
            if len(self.message_buffer) >= 100:
                self.process_buffered_messages()

    def process_buffered_messages(self):
        """
        function to process buffered messages

        :returns: `None`
        """

        with self.buffer_lock:
            messages_to_process = self.message_buffer
            self.message_buffer = []

        for topic, msg in messages_to_process:
            centre_id = topic.split('/')[3]
            m = json.loads(msg.payload.decode('utf-8'))
            # parse generated_by from message-attribute
            generated_by = m.get('generated_by', 'none')
            # update the appropriate counter
            if 'metadata' in topic:
                metadata_received.labels(centre_id, generated_by).inc(1)
            elif 'synop' in topic:
                synop_messages_received.labels(centre_id, generated_by).inc(1)


    def periodic_buffer_processing(self):
        """
        function to process buffered messages every second

        :returns: `None`
        """

        while True:
            self.process_buffered_messages()
            time.sleep(1)

    def gather_mqtt_metrics(self):
        """
        setup mqtt-client to monitor metrics from broker on this box

        :returns: `None`
        """

        broker_host = os.environ.get('BROKER_HOST', '')
        broker_username = os.environ.get('BROKER_USERNAME', '')
        broker_password = os.environ.get('BROKER_PASSWORD', '')
        broker_port = int(os.environ.get('BROKER_PORT', '443'))
        broker_transport = os.environ.get('BROKER_TRANSPORT', 'websockets')

        r = random.Random()
        client_id = f"wis2node-monitor_{r.randint(1,1000):04d}"
        
        args = {
            'callback_api_version': mqtt.CallbackAPIVersion.VERSION2,
            'transport': broker_transport,
            'client_id': client_id,
            'clean_session': False
        }
        
        try:
            logger.info(f"setup connection: host={broker_host}, port={broker_port} user={broker_username}") # noqa
            client = mqtt.Client(**args)
            if broker_port in [443, 8883]:
                client.tls_set(ca_certs=None, certfile=None, keyfile=None,
                                cert_reqs=ssl.CERT_REQUIRED,
                                tls_version=ssl.PROTOCOL_TLS,
                                ciphers=None)
            client.on_connect = self.sub_connect
            client.on_message = self.sub_mqtt_metrics
            client.username_pw_set(broker_username, broker_password)
            client.connect(host=broker_host, port=broker_port)
            client.loop_forever()
        except Exception as err:
            logger.error(f"Failed to setup MQTT-client with error: {err}")


def main():
    start_http_server(8111)
    collector = MetricsCollector()
    collector.init_wis2node_active_gauge()

    Thread(target=collector.periodic_buffer_processing, daemon=True).start()
    collector.gather_mqtt_metrics()


if __name__ == '__main__':
    main()