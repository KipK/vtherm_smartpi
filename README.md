# Versatile Thermostat SmartPI

[Lire la version française](README.fr.md)

<p align="center">
  <img src="assets/brand/logo.png" alt="SmartPI Logo" width="300" />
</p>

<p align="center">
  <strong>Advanced Proportional Thermostat Control for Home Assistant</strong>
</p>

<p align="center">
  Elevate your home heating/cooling system with SmartPI, the cutting-edge proportional algorithm designed for precision temperature management.
</p>

## 🌟 What is SmartPI?

SmartPI is an advanced PI-based thermal control algorithm built around a first-order thermal model (1R1C) rather than a classic PID loop. It learns your room's heating capability, heat loss rate and dead time, then uses that model to compute a much more accurate heating command than fixed-time proportional controllers.

- **Precise Temperature Control**: Maintains target temperatures with minimal fluctuations
- **Energy Efficiency**: Optimizes heating cycles using learned thermal behavior
- **Adaptive Learning**: Continuously adjusts based on your home's thermal model
- **Model-based Response**: Uses a 1R1C approximation to anticipate how the room reacts to heating changes
- **Advanced Protections**: Includes hysteresis, deadbands, anti-windup and setpoint recovery handling

SmartPI transforms your thermostat control into a model-aware algorithm that learns and adapts to the real thermal behavior of your home.
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

## 🚀 Features

- **SmartPI Algorithm**: Advanced proportional control for precise temperature management
- **Versatile Integration**: Works seamlessly with Versatile Thermostat
- **Per-Device Configuration**: Customize settings for each thermostat
- **Global Defaults**: Easy setup with fallback configurations
- **Robust Implementation**: Includes safety features like anti-windup and hysteresis
- **Open Source**: Fully transparent and community-driven development

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

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.
