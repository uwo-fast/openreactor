# openreactor

> Supervise benchtop lab reactors from a Raspberry Pi: temperature, mixing,
> dosing and water chemistry, with recorded runs.

[![License: AGPL-3.0-or-later](https://img.shields.io/badge/License-AGPL--3.0--or--later-blue.svg)](LICENSE)
[![Contributions welcome](https://img.shields.io/badge/Contributions-welcome-brightgreen.svg)](https://github.com/uwo-fast/.github/blob/main/CONTRIBUTING.md)

openreactor runs batch and semi-batch reactors on the bench, for biology, wet
chemistry and thermal work such as melting and curing. The heater and motor
boards run their own control loops, and a heater board with a command
watchdog stops its outputs if it loses contact with the Pi. openreactor
supervises them over I2C: it applies
setpoints, reads water-chemistry sensors, records each run, and stops every
output on request.

## Status

Version 2.0.0a1 is an alpha, and a rewrite that is not compatible with 1.x.
It reads and calibrates EZO sensors, drives RLHT heater slices and DCMT
motor slices (open loop) with their command watchdogs and stop-all, records
and exports runs, and serves an HTTP API and a browser UI. It does not yet
run profiles beyond validating them
([#28](https://github.com/uwo-fast/openreactor/issues/28)), or ship a
systemd unit ([#29](https://github.com/uwo-fast/openreactor/issues/29)).
[#17](https://github.com/uwo-fast/openreactor/issues/17) tracks 2.0.

For a stable install, use
[v1.2.1](https://github.com/uwo-fast/openreactor/releases/tag/v1.2.1); the
[`1.x` branch](https://github.com/uwo-fast/openreactor/tree/1.x) has its setup
and usage instructions. It takes fixes for reported bugs only. GitHub closes
an issue from `Closes #N` only on a merge into `main`, so an issue fixed in
`1.x` is closed by hand. Until 2.0.0 is released, `pip install openreactor`
without a version installs the alpha, because 2.0.0a1 is the only version on
PyPI.

## Scope

openreactor 2.0 supervises one bench-top vessel per instance, with every
device listed in the configuration file. It does not include:

- plugins or user scripts that drive the hardware
  ([#78](https://github.com/uwo-fast/openreactor/issues/78) collects ideas
  for experiment logic after 2.0);
- device discovery;
- a simulator;
- shredding or other mechanical processing;
- migrating 1.x data.

Host control loops fed by EZO readings, such as dosing, are planned for 2.1.

## Hardware

openreactor runs on a Raspberry Pi with 64-bit Raspberry Pi OS. It drives
RLHT heater slices running [Slice_RLHT](https://github.com/feastorg/Slice_RLHT)
firmware, each with an onboard PID on two outputs and two thermocouples (a
gen1 board has a heater relay on output 1 only). It reads Atlas Scientific
EZO pH, ORP, RTD, EC, DO and HUM circuits. Every device is listed in the
configuration file.

## Install and quick start

[docs/install.md](docs/install.md) covers preparing the Pi and installing
openreactor. Then copy [examples/openreactor.toml](examples/openreactor.toml),
keep the devices you have, check it, read the sensors once, and start the
server:

```sh
uv run openreactor check-config ~/openreactor.toml
uv run openreactor read -c ~/openreactor.toml
uv run openreactor serve -c ~/openreactor.toml
```

Open <http://127.0.0.1:8080/> in a browser on the Pi. Ctrl-C stops the
server, which sends stop-all first. `serve` starts every RLHT slice in the
configuration, so read [docs/safety.md](docs/safety.md) before you connect a
heater.

## Documentation

- [Install](docs/install.md): prepare a Pi and install openreactor.
- [Configuration](docs/configuration.md): every setting in the file.
- [Usage](docs/usage.md): the command line, runs, profiles and calibration.
- [Browser UI and HTTP API](docs/web.md), and serving on the lab network.
- [Safety](docs/safety.md): how openreactor keeps heaters safe.
- [Development](docs/development.md): set up, test and release.
- [Bench checklist](docs/bench.md): the hardware checks before a release.
- [Changelog](CHANGELOG.md)

## Development

You need [uv](https://docs.astral.sh/uv/) and [just](https://just.systems/).
In a clone, `just setup` creates the environment, `just check` runs the
format check, lint and type-check, and `just test` runs the tests.
[docs/development.md](docs/development.md) has the rest.

## Contributing

Contributions are welcome. See the organization
[contribution guide](https://github.com/uwo-fast/.github/blob/main/CONTRIBUTING.md).

## Citation

If you use openreactor in your work, please cite it with the "Cite this
repository" button on GitHub, or from [CITATION.cff](CITATION.cff).

## Acknowledgements

openreactor was originally written by
[Etienne Michels](https://github.com/ebmichel),
[Wilson J. Holmes](https://github.com/wilsonjholmes) and
[Finn Hafting](https://github.com/FinnWestern) in the MOST Research Group.
Icons are from [Feather](https://feathericons.com), and the open source logo
is from [Remix Icon](https://remixicon.com/).

## License

Copyright © 2021–2026 the openreactor contributors. openreactor is licensed
under the GNU Affero General Public License, version 3 or any later version;
see [LICENSE](LICENSE). The MOST Research Group first released it in 2021
under GPL-2.0-only. In January 2025 the FAST Research Group reworked it and
relicensed it under the AGPL-3.0, so that improvements stay available to
everyone who uses it.

## Contact

The [FAST research group](https://uwo-fast.github.io/) maintains openreactor.
For research collaboration, contact Dr. Joshua Pearce
(<joshua.pearce@uwo.ca>).
