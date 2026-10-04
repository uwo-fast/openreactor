# openreactor

> Supervise benchtop lab reactors from a Raspberry Pi: temperature, mixing,
> dosing and water chemistry, with recorded runs.

[![License: AGPL-3.0-or-later](https://img.shields.io/badge/License-AGPL--3.0--or--later-blue.svg)](LICENSE)
[![Contributions welcome](https://img.shields.io/badge/Contributions-welcome-brightgreen.svg)](https://github.com/uwo-fast/.github/blob/main/CONTRIBUTING.md)

## Overview

openreactor runs batch and semi-batch reactors on the bench, for biology, wet
chemistry and thermal work such as melting and curing. It reads sensors and
drives heaters, motors and pumps over I2C from a Raspberry Pi, and records each
run.

In 2.0, the heater and motor boards run their own control loops and stop their
outputs if they lose contact with the Pi. openreactor supervises them: it
applies setpoints and profiles, reads water-chemistry sensors, records each
run, and stops every output on request.

## Status

2.0 is being rewritten on `main` and is not usable yet. Progress is tracked in
[#17](https://github.com/uwo-fast/openreactor/issues/17).

For a working install, use **1.x**: the latest release is
[v1.2.1](https://github.com/uwo-fast/openreactor/releases/tag/v1.2.1), and the
[`1.x` branch](https://github.com/uwo-fast/openreactor/tree/1.x) has its setup
and usage instructions.

## Development

Needs [uv](https://docs.astral.sh/uv/), [just](https://just.systems/) and a C
compiler: [ezo-driver](https://github.com/feastorg/ezo-driver) is installed from
git, and built from source, until it is published to PyPI.

```sh
git clone https://github.com/uwo-fast/openreactor.git
cd openreactor
just setup   # create the environment
just check   # format check, lint, type-check
just test    # run the tests
```

To validate a configuration file, run
`just run check-config examples/openreactor.toml`. The example lists every
setting with a comment. `just --list` shows the other recipes. Optionally, install the pre-commit hooks
with `pre-commit install`.

## Command line

The device commands read `/etc/openreactor/openreactor.toml` unless given
`-c FILE`. They go through one controller that owns the bus, and only one can
run at a time on a machine, whichever user runs it: each holds
`/run/lock/openreactor.lock`, and a second command refuses, naming the
holder's user and pid. Ctrl-C and SIGTERM send stop-all before the bus is
closed; an interrupted `read` or `ezo cal` exits 130 and sends nothing more to
the circuit.

```sh
openreactor check-config FILE        # validate a config file
openreactor read                     # read every EZO sensor once
openreactor read --follow            # read them every ezo_period_s until Ctrl-C
openreactor run --name brew          # record a run until Ctrl-C
openreactor runs                     # list recorded runs
openreactor export 3                 # write run 3 as run-3.zip
openreactor ezo cal ph status        # show a circuit's calibration
openreactor ezo cal ph mid 7.00      # calibrate a point
openreactor ezo cal ph clear --yes   # erase a circuit's calibration
```

A run records every reading and event (stop-all, failed reads) in SQLite, at
`storage.database`, by default `~/.local/state/openreactor/openreactor.db`. An
export is a zip of `readings.csv`, `events.csv` and `run.json`, with the run's
config. If the database fails mid-run (a full disk, say), recording stops and
the run is marked interrupted; control and stop-all carry on.

Calibration points by family:

- pH: `mid`, then `low` and `high`, each with a pH value. A `mid` calibration
  clears the other two points, so do it first.
- ORP and RTD: `ref`, with the reference mV or temperature.
- EC: `dry`, then `single`, or `low` and `high`, with the reference µS/cm.
- DO: `atmospheric` and `zero`, with no value.
- HUM: `temperature`, with the reference °C.

Before calibrating, the command checks the circuit at that address is the
family the config names. EC and DO are first set back to their default
compensation temperature (25 °C and 20 °C), as their datasheets require; the
next `read` sends the measured temperature again.

## Contributing

Contributions are welcome. See the organization
[contribution guide](https://github.com/uwo-fast/.github/blob/main/CONTRIBUTING.md).

## Citation

If you use openreactor in your work, please cite it. Use the **Cite this
repository** button on GitHub, or see [`CITATION.cff`](CITATION.cff).

## Acknowledgements

openreactor was originally written by [Etienne Michels](https://github.com/ebmichel),
[Wilson J. Holmes](https://github.com/wilsonjholmes) and
[Finn Hafting](https://github.com/FinnWestern) in the MOST Research Group.

Icons from [Feather](https://feathericons.com); open source logo from
[Remix Icon](https://remixicon.com/).

## License

Copyright © 2021–2026 the openreactor contributors.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version. See [`LICENSE`](LICENSE).

It was first released in 2021 under GPL-2.0-only by the MOST Research Group.
In January 2025 the FAST Research Group reworked it and relicensed it under the
AGPL-3.0, so that improvements stay available to everyone who uses it.

## Contact

Maintained by the [FAST research group](https://uwo-fast.github.io/). For
research collaboration inquiries, contact Dr. Joshua Pearce
(<joshua.pearce@uwo.ca>).
