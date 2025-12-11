Simple tool to listen to BLE broadcasts from a Govee hygrometer, and do _things_...

Functions as a Prometheus exporter, also pushes to both MQTT and memcached.

My setup runs on a few devices (mostly RasPi) so I push to MQTT for handling by node-red where duplication isn't an issue,
and also to memcached, which is consumed by a Prometheus exporter.
The extra step acts as natural deduplication of hygrometers which are received by multiple devices and solves any quatisation issues

TODO:
* Strip prometheus code
* Pull config from a url or private gh repo to save scp'ing it about everywhere
* Add auth
* Better handling/death on any issues (loss of mqtt/memcache)
