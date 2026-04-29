# Versatile Thermostat SmartPI

[Lire la version française](README.fr.md)

<p align="center">
  <img src="assets/brand/logo.png" alt="SmartPI Logo" width="300" />
</p>

<p align="center">
  <strong>Self-Adaptive PI Thermostat Control for Home Assistant</strong>
</p>

<p align="center">
  A model-based PI controller that learns your room's thermal behavior and adapts regulation automatically — beyond fixed-coefficient approaches.
</p>

## 🌟 What is SmartPI?

SmartPI is an advanced PI-based thermal control algorithm built around a first-order thermal model (1R1C) rather than a classic PID loop. It learns your room's heating capability, heat loss rate and dead time, then uses that model to compute a much more accurate heating command than fixed-time proportional controllers.

- **1R1C Thermal Model**: Learns your room's heating gain, heat-loss rate, and reaction dead times from real observations — no manual tuning required
- **Auto-Tuned PI Gains**: Computes Kp and Ki automatically from the learned time constant and dead time using IMC and heuristic rules
- **Model-Based Feed-Forward**: Estimates the steady-state power needed to hold the setpoint, combined with a slow bias trim and an optional short-horizon predictive correction (FF3) for disturbance recovery
- **Analytical Setpoint Trajectory**: Shapes the proportional reference and applies a model-aware landing cap in heating mode to reach the target quickly while limiting overshoot
- **Safety-First Governance**: A regime-based matrix freezes or unlocks learning and gain adaptation depending on the current operating context
- **Auto-Calibration**: Monitors model quality over time and triggers a recalibration sequence when learning stagnates
- **Valve Curve Linearization**: Translates SmartPI demand into a valve position adapted to the non-linear behavior of radiator TRVs
- **Rich Diagnostics**: Publishes detailed learning progress, model state, and regulation data — viewable via a dedicated Home Assistant Markdown card

## 🔗 Integration with Versatile Thermostat

This integration extends the popular [Versatile Thermostat](https://github.com/jmcollin78/versatile_thermostat) integration by adding the SmartPI algorithm as a plugin. Versatile Thermostat already provides comprehensive thermostat management in Home Assistant, and with SmartPI, you get:

- Professional-grade temperature control algorithms
- Seamless integration with existing VT configurations
- Per-device customization options
- Global defaults for easy setup

## 📦 Installation

### Via HACS (Recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=KipK&repository=vtherm_smartpi&category=Integration)

1. Ensure you have [HACS](https://hacs.xyz/) installed in your Home Assistant instance
2. Click the button above or manually add this repository in HACS
3. Search for "Versatile Thermostat SmartPI" in HACS
4. Install the integration
5. Restart Home Assistant
6. Configure the SmartPI algorithm in your Versatile Thermostat devices

### Manual Installation

1. Download the latest release from the [Releases](https://github.com/KipK/vtherm_smartpi/releases) page
2. Extract the `vtherm_smartpi.zip` file
3. Copy the `custom_components/vtherm_smartpi` folder to your Home Assistant `custom_components` directory
4. Restart Home Assistant
5. Configure as above

## 📖 Documentation

Comprehensive documentation is available in English:

- 🇬🇧 [User Documentation (English)](documentation/en/vtherm_smartpi.md)
- 🇬🇧 [Technical Documentation (English)](documentation/en/technical_doc.md)

## 👥 Authors

- [@KipK](https://github.com/KipK)
- [@gael1980](https://github.com/gael1980)

## 🤝 Contributing

Contributions are welcome! Please see the documentation for development guidelines and testing procedures.

## 🙏 Thanks To

- [@gael1980](https://github.com/gael1980) for the scientific foundation and theory behind SmartPI
- [@caiusseverus](https://github.com/caiusseverus) for his helpful [heating simulator](https://github.com/caiusseverus/heating-simulator) and his support with testing SmartPI and sharing knowledge
- [@jmcollin](https://github.com/jmcollin78) for developing [Versatile Thermostat](https://github.com/jmcollin78/versatile_thermostat)
- [@bontiv](https://github.com/bontiv) for the [Versatile Thermostat website](https://www.versatile-thermostat.org/)
- All the people who tested SmartPI during development

## ☕ Support

If SmartPI is useful to you, you can support its development here:

<p>
  <a href="https://www.buymeacoffee.com/kipk" target="_blank" rel="noopener noreferrer">
    <img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me a Coffee" width="217" height="60" />
  </a>
</p>

## 📄 License

This project is licensed under the Smart-PI licensing terms. See [LICENSE.md](LICENSE.md) for details.
