# Usage

This page describes the `openreactor` command line. From a checkout, run each
command as `uv run openreactor ...`. The browser UI and the HTTP API are in
[web.md](web.md).

## Commands

| Command | What it does |
|---|---|
| `openreactor check-config FILE` | Validate a configuration file. |
| `openreactor read` | Read every EZO sensor once. |
| `openreactor read --follow` | Read the EZO sensors every `ezo_period_s` until Ctrl-C. |
| `openreactor run --name NAME [--notes TEXT]` | Record a run until Ctrl-C. |
| `openreactor runs` | List the recorded runs. |
| `openreactor export N [-o FILE]` | Write run N as a zip, `run-N.zip` by default. |
| `openreactor ezo cal DEVICE status` | Show a circuit's calibration. |
| `openreactor ezo cal DEVICE POINT [VALUE]` | Calibrate one point. |
| `openreactor ezo cal DEVICE clear --yes` | Erase a circuit's calibration. |
| `openreactor serve [--host H] [--port P]` | Serve the browser UI and the HTTP API until Ctrl-C. |
| `openreactor status [--url URL]` | Show each slice's state, from the running server. |
| `openreactor hash-password` | Make a `server.password_hash`. |
| `openreactor profile validate [-c CONFIG] [--dry-run] FILE` | Check a run profile, and optionally print its timeline. |

Commands that use a configuration read `/etc/openreactor/openreactor.toml`
unless you pass `-c FILE`.

`run` and `serve` start each RLHT and DCMT slice in the configuration.
`read` and `ezo cal` never touch a slice.

## One controller at a time

`read`, `run`, `ezo cal` and `serve` each own the I2C bus while they run. Only
one of them can run at a time on a machine, whichever user starts it. Each
holds the lock file `/run/lock/openreactor.lock`, and a second command
refuses to start and names the user and process that hold it.

`runs`, `export`, `status` and `check-config` do not touch the bus, so they
run alongside any of them.

## Stopping a command

Ctrl-C and SIGTERM do the same thing. Before the bus is closed, openreactor
sends stop-all, which puts every slice in its safe state.

- An interrupted `read` or `ezo cal` exits with status 130 and sends nothing
  more to the circuit.
- `read --follow` exits with status 0, since Ctrl-C is how it ends.
- `run` ends the run as `stopped`.
- `serve` sends stop-all at once. Requests still in progress then get 5
  seconds to finish, and after that the recording run, if any, is ended and
  the circuits are closed.

## Runs

`openreactor run --name NAME` records every reading and every event until you
stop it. Events include setpoints, stop-all, failed reads, and what the
slices report. You can also start and stop runs from the browser UI or the
API while `serve` runs.

Runs are stored in SQLite, at `storage.database`. By default that is
`~/.local/state/openreactor/openreactor.db`, or under `$XDG_STATE_HOME` when
it is set.

If the database fails during a run, for example because the disk is full,
recording stops and the run is marked `interrupted`. Control and stop-all
carry on. When you stop it, `run` exits with status 1.

If openreactor dies while a run is recording, the run stays marked
`running`. The next run marks it `interrupted`.

`openreactor runs` lists each run's number, status, start and end time in
UTC, and name.

`openreactor export N` writes a zip with three files:

- `readings.csv`: every reading, with its unit.
- `events.csv`: every event.
- `run.json`: the run's details, including the configuration it ran with.

`export` refuses to overwrite an existing file.

## Slice status

`openreactor status` shows each RLHT slice: its firmware, state, watchdog,
e-stop, mode, and each output's temperature, setpoint, duty and
thermocouple. When the slice runs a setpoint other than the one openreactor
wants, the line shows the wanted one too.

`status` asks the running server, at the configuration's `[server]` address
or at `--url`. It sends the password from `OPENREACTOR_PASSWORD`. Without
it, when the server asks for a password, `status` prompts for one in a
terminal and fails otherwise.

With no server running, `status` reads nothing. Every reply a slice builds
feeds its watchdog, so reading a slice after openreactor stopped would keep
an armed slice heating. Instead, `status` says which process holds the bus,
if any. The same detail is in the API at `/api/v1/status`, under `slices`.

## Run profiles

A run profile is a TOML schedule of setpoints for the actuated channels.
Each channel has a list of steps, which run in order from the start:

| Step | Meaning |
|---|---|
| `{ set = 30.0 }` | Go to the value at once. |
| `{ ramp = 37.0, over = "30m" }` | Go to the value in a straight line over the duration. Add `from = 25.0` to start from another value. |
| `{ hold = "2h" }` | Keep the current value for the duration. |
| `{ off = true }` | Put the channel in its safe state. |

Durations are a number and a unit, `s`, `m` or `h`, such as `"45s"`, `"2m"`
or `"1.5h"`. The format specifies that when a profile ends or is aborted,
every channel in it goes to its safe state; openreactor cannot run a
profile yet.
[examples/profiles/ramp-and-hold.toml](../examples/profiles/ramp-and-hold.toml)
uses every step.

openreactor cannot run a profile yet. It can check one:

```sh
openreactor profile validate -c ~/openreactor.toml --dry-run examples/profiles/ramp-and-hold.toml
```

`profile validate` names each problem's place in the file and how to fix it.
With `-c`, it also checks that the profile's channels exist in that
configuration. With `--dry-run`, it prints what happens when.

## EZO calibration

Each family has its own calibration points:

| Family | Points | Value |
|---|---|---|
| pH | `mid`, then `low` and `high` | The pH of the reference solution. |
| ORP | `ref` | The reference in mV. |
| RTD | `ref` | The reference temperature. |
| EC | `dry`, then `single`, or `low` and `high` | The reference in µS/cm; `dry` takes none. |
| DO | `atmospheric` and `zero` | None. |
| HUM | `temperature` | The reference in °C. |

A pH `mid` calibration clears the `low` and `high` points, so calibrate `mid`
first.

To calibrate a circuit:

1. Show its current calibration:
   `openreactor ezo cal ph status`.
2. Put the probe in the reference solution, and calibrate one point:
   `openreactor ezo cal ph mid 7.00`.
3. Repeat for the family's other points.
4. Run `status` again to confirm the new calibration.

Before it calibrates, `ezo cal` checks that the circuit at the configured
address is the family the configuration names, and stops if it is not.
Calibrating the wrong circuit would corrupt its calibration.

EC and DO circuits are first set back to their default compensation
temperature, 25 °C for EC and 20 °C for DO, as their datasheets require. The
next `read` sends the measured temperature again.

`openreactor ezo cal ph clear --yes` erases a circuit's calibration. Without
`--yes`, it refuses.

The browser UI's Devices page and the API can also show, set and clear
calibration.
