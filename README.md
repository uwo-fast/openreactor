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

Needs [uv](https://docs.astral.sh/uv/) and [just](https://just.systems/).

```sh
git clone https://github.com/uwo-fast/openreactor.git
cd openreactor
just setup   # create the environment
just check   # format check, lint, type-check
just test    # run the tests
```

`just --list` shows the other recipes. Optionally, install the pre-commit hooks
with `pre-commit install`.

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
