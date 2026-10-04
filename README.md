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
[ezo-driver](https://github.com/feastorg/ezo-driver) installs as a wheel on
x86_64 and aarch64 Linux; elsewhere it builds from source and needs a C
compiler.

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
openreactor serve                    # serve the HTTP API until Ctrl-C
openreactor status                   # show each RLHT slice's state
openreactor hash-password            # make a server.password_hash
openreactor profile validate --dry-run FILE  # check a run profile, print its timeline
```

`run` and `serve` also start each RLHT heater slice in the config, and `read`
and `ezo cal` never touch one. Start-up checks the slice is an RLHT built with
CRUMBS 0.12.0 or later, sends its safe state (setpoints to zero, then open-loop
duty to zero), arms its command watchdog with `watchdog_timeout_ms` and
confirms it, then sets closed-loop mode, the thermocouples, and any periods and
PID gains the config sets. Gains go only when the config gives them for both of
the slice's outputs, since the slice takes both at once; nothing is filled in. A slice without the watchdog capability stays
read-only unless its config sets `allow_unprotected = true`. Each slice is then
polled every `slice_poll_s`, which also keeps its watchdog fed, and its
channels' temperature, setpoint and duty are read and recorded like any
sensor. Every fifth poll also reads the slice's watchdog. A reboot (the
watchdog disarmed) or a trip (tripped, or a changed trip count) is logged, and
the desired state is sent again: mode, setpoints, periods, thermocouples, gains,
and the watchdog armed. The next poll checks that it took; if it did not, that
is logged and it is sent again at the next scheduled check, and if sending
failed, at the next poll. A watchdog read that fails is tried again after the
next poll; after 3 in a row that is logged, and it waits for its usual slot.
A slice built with `RLHT_WATCHDOG_BOOT_MS` arms its
watchdog at boot, so its reboot is seen only if it had tripped before (#53).
A failed read is retried twice in the same poll; after 3 failed
polls in a row the slice shows as unreachable until it answers again. An e-stop
pressed on a slice sends stop-all, and its setpoints stay at zero until the
operator sets them again. Stop-all sends every slice its safe state, and its
setpoints stay at zero the same way. Slice_RLHT firmware before 16a04dd keeps
its PID integral through it, so a running heater decays to off over seconds.
From 16a04dd (feastorg/Slice_RLHT#13) its on-time is 0 at the next control
step, as a host check shows; the bench row in `docs/bench.md` is to confirm the
relay. openreactor cannot tell which a slice runs, since the version reply is
the same for both (feastorg/bread-crumbs-contracts#23), so flash 16a04dd or
later (#51).

An RLHT channel's setpoint is set from the API
(`PUT /api/v1/channels/<name>/setpoint`) or the Controls page, in °C to the
slice's tenth of a degree: from 0 up to the channel's `max_setpoint`, or
3276.7 °C when the config sets none. It is refused while the slice's e-stop
is held, the slice is read-only, or a change in its setpoints is being
checked (409, for a moment; see below), and while it is unreachable or did
not finish start-up (503). A send that fails answers 502, since the slice may
have taken it. Each one sent, or tried, is an event in the run. It stays the
desired setpoint, sent again after a trip or a reboot, until it is set again
or stop-all or an e-stop zeroes it. It is not restored after a restart.

`openreactor status` shows each RLHT slice: its firmware, state, watchdog,
e-stop, mode, and each output's temperature, setpoint, duty and
thermocouple. It asks the running server, at the config's `[server]`
address or `--url`, with the password from `OPENREACTOR_PASSWORD`, or a
prompt when the server asks for one. With no server it reads nothing: every
reply a slice builds feeds its watchdog, so reading a slice after
openreactor stopped would keep an armed one heating. `/api/v1/status` has
the same detail under `slices`.

When a poll finds the slice running setpoints other than those wanted, the
watchdog is checked on the next tick: a trip or a reboot is re-asserted as
above. With neither, the slice is sent its safe state and the setpoints stay
at zero until set again. That covers an e-stop pressed and released between
two polls, a setpoint that reached the slice although its send failed, and
another controller.

A run records every reading and event (stop-all, failed reads) in SQLite, at
`storage.database`, by default `~/.local/state/openreactor/openreactor.db`. An
export is a zip of `readings.csv`, `events.csv` and `run.json`, with the run's
config. If the database fails mid-run (a full disk, say), recording stops and
the run is marked interrupted; control and stop-all carry on.

`openreactor serve` also serves the operator's browser UI at `/`: a
dashboard with live values and charts, controls, runs (start, stop, export),
EZO calibration, and an about page, with Stop all on every page. It loads
nothing from the internet; the few browser files it uses are vendored, as
[VENDORED.md](VENDORED.md) describes.

`openreactor serve` runs the controller with the HTTP API at `/api/v1`:
status, live channels, setpoints, runs and their export, stop-all and EZO
calibration. It prints each event as it happens, as `run` does, and holds
the same lock as the other device commands. SIGTERM
sends stop-all at once; requests still in progress get 5 seconds to finish,
then the recording run is ended and the circuits are closed.

- With no `server.password_hash` it binds only to a loopback address, and
  answers only requests addressed to `localhost`, `127.0.0.1` or `[::1]`.
- To serve the lab network, set `server.host` and a `server.password_hash`
  from `openreactor hash-password`. A browser signs in with
  `POST /login` and gets a session cookie, which lasts until `POST /logout`,
  12 hours, or a server restart. A script sends
  `Authorization: Bearer <password>`. One password check runs at a time, and
  each client address has at most one in hand: a second guess from the same
  address gets 429 at once, and others wait up to 2 seconds for their turn.
- A request that changes something is refused if its `Origin` names another
  site; a signed-in browser request must carry one.
- The OpenAPI description is at `/api/v1/openapi.json`, behind the same
  sign-in.

Plain HTTP carries the password and the session cookie in the clear, so use it
on a lab network you trust, or put a TLS proxy in front. The proxy must run on
the same machine, keep the original `Host` header and send
`X-Forwarded-Proto`, or browser sign-in fails its Origin check. Never proxy a
server that has no password: it trusts every request that reaches it under a
loopback Host name, and the proxy is one.

A run profile is a TOML schedule of setpoints for the actuated channels: each
channel's steps `set` a value, `ramp` to one over a duration, `hold`, or turn
`off`. `openreactor profile validate FILE` checks one, naming each problem's
place in the file and how to fix it; `-c CONFIG` also checks its channels exist,
and `--dry-run` prints what happens when. `examples/profiles/ramp-and-hold.toml`
shows every step. Running a profile comes with the slices.

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
