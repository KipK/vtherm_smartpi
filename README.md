# vtherm_smartpi

Home Assistant integration that provides the SmartPI proportional algorithm for Versatile Thermostat through the `vtherm_api` plugin registry.

Current scope:

- registers the `smartpi` proportional algorithm in VT
- embeds the SmartPI core and runtime handler
- provides per-thermostat SmartPI configuration entries

Planned next steps:

- move SmartPI services to the plugin domain
- add SmartPI entities for diagnostics and calibration state
- port and adapt the SmartPI test suite to the standalone plugin runtime
