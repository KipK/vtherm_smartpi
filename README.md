# Versatile Thermostat SmartPI

<p align="center">
  <img src="assets/brand/logo.png" alt="SmartPI Logo" width="300" />
</p>

Home Assistant integration that provides the SmartPI proportional algorithm for [Versatile Thermostat](https://github.com/jmcollin78/versatile_thermostat) through the `vtherm_api` plugin registry.

## 📖 Documentation

The full documentation for the SmartPI algorithm, including how it works, configuration options, and technical details, is available here:

- 🇫🇷 [Documentation utilisateur (Français)](documentation/fr/vtherm_smartpi.md)
- 🇫🇷 [Documentation technique (Français)](documentation/fr/technical_doc.md)
- 🇬🇧 [User Documentation (English)](documentation/en/vtherm_smartpi.md)
- 🇬🇧 [Technical Documentation (English)](documentation/en/technical_doc.md)

## 🚀 Features

Current scope:

- registers the `smartpi` proportional algorithm in VT
- embeds the SmartPI core and runtime handler
- provides per-thermostat SmartPI configuration entries
- supports an optional global defaults entry used when no per-thermostat entry matches

## 🛠 Planned Next Steps

- move SmartPI services to the plugin domain
- add SmartPI entities for diagnostics and calibration state
- port and adapt the SmartPI test suite to the standalone plugin runtime
