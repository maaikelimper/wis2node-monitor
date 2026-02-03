# wis2node-monitor

docker-compose setup to monitor the WIS2 Node with Prometheus and Grafana. The setup includes a custom MQTT metrics collector that subscribes to specific MQTT topics and exposes the collected metrics in a format that Prometheus can scrape. 

the docker-compose stack includes mqtt-metrics-collectors for all 4 WIS2 Global Brokers.

Work in progress !
