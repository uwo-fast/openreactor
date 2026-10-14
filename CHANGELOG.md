# Changelog

All notable changes to this project are documented in this file.

The format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Releases before
2.0 are described in their [GitHub releases](https://github.com/uwo-fast/openreactor/releases).

## [Unreleased]

2.0 is a rewrite and is not compatible with 1.x: new configuration, new
database, and new device protocols. 1.x is maintained on the `1.x` branch.

### Added

- Python packaging, linting, type-checking, tests and CI.
- A TOML configuration file that lists every device.
  `openreactor check-config FILE` validates it before anything starts, and
  `examples/openreactor.toml` is a commented example.
- Atlas EZO pH, ORP, RTD, EC, DO and HUM circuits, through ezo-driver.
- A check at start-up that each EZO circuit is the type the configuration
  names.
- Split-phase EZO reads, so no circuit waits on another.
- Temperature compensation for pH, EC and DO from the last RTD reading.
- `openreactor read`, which reads every EZO circuit once, and
  `read --follow`, which keeps reading.
- `openreactor ezo cal`, which shows, sets and clears a circuit's
  calibration.
- A controller that owns the bus. It runs a 100 ms tick that never waits on
  a device, and runs EZO commands between reads rather than during them.
- Stop-all, which runs on the next tick and reaches every actuator whatever
  else fails.
- A machine-wide lock, so only one controller runs at a time.
- Recorded runs in SQLite: `openreactor run --name X` records readings and
  events until stopped, and `openreactor runs` lists them.
- `openreactor export N`, which writes a run as a zip of `readings.csv`,
  `events.csv` and `run.json`.
- A failed database write stops recording and marks the run interrupted,
  without stopping control.
- An HTTP API, `openreactor serve`, under `/api/v1`: status, channels,
  setpoints, runs, export, stop-all and EZO calibration.
- `openreactor hash-password`. Without `server.password_hash`, `serve` binds
  only to a loopback address.
- Sign-in: browsers get a session cookie, and scripts send
  `Authorization: Bearer`.
- A browser UI from `openreactor serve`: a dashboard with live values and
  charts, controls, runs, EZO calibration, an about page, and a sign-in page
  when a password is set. Every page has a Stop all button.
- The UI works offline. Bootstrap, htmx and Chart.js are vendored, with
  their hashes recorded in `static/vendor/vendor.toml`, and `just vendor`
  updates them.
- RLHT heater slices over CRUMBS, started by `run` and `serve`.
- An RLHT start-up check of the slice's type, CRUMBS version and module
  version, followed by the safe state.
- The RLHT command watchdog, armed and confirmed at start-up. A slice
  without one is read-only unless `allow_unprotected` is set.
- RLHT closed-loop mode, thermocouples, periods and gains set at start-up,
  with new optional `kp`, `ki`, `kd` and `period_ms` per channel.
- PID gains on an RLHT with one channel, as on a gen1 board with one relay.
  The output with no channel gets the same `kp`, `ki` and `kd`.
- RLHT polling every `slice_poll_s`, which feeds the watchdog and records
  each channel's temperature, setpoint and duty.
- Stop-all sends both stop commands to every RLHT slice.
- RLHT trip and reboot detection from a watchdog read after every fifth
  poll. openreactor re-sends the wanted state and checks it at the next
  poll.
- After a slice reboot, or a trip count that went down, the setpoints stay
  at 0 until set again.
- A slice e-stop or stop-all sets the setpoints to 0, and nothing resumes
  until they are set again.
- A failed RLHT read is retried twice per poll. After 3 failed polls the
  slice is marked unreachable, and 3 failed watchdog reads in a row are
  logged.
- Each RLHT event is recorded in the run, and `/status` shows a slice that
  is unreachable or held by its e-stop.
- RLHT setpoints from the API and the Controls page, in °C to a tenth of a
  degree, up to an optional per-channel `max_setpoint`.
- Setpoints are refused while the slice's e-stop is held, it is read-only,
  or it is unreachable. Each one is recorded as an event.
- A slice found running other setpoints than wanted, with no trip or reboot
  behind it, is sent its safe state.
- Before a setpoint is sent, the slice is read, and the setpoint refused
  unless the slice is as openreactor left it. Setting one output no longer
  restarts the other after a reboot no poll has seen yet.
- A watchdog timeout other than `watchdog_timeout_ms` is treated as a
  reboot.
- `openreactor serve` prints each event as it happens, as `run` does.
- `openreactor status`, which shows each RLHT slice's firmware, state,
  watchdog, e-stop, mode and outputs from the running server. With no
  server it reads nothing, since reading a slice feeds its watchdog.
- `/api/v1/status` has the same slice detail under `slices`.
- Run profiles: a TOML schedule of `set`, `ramp`, `hold` and `off` steps per
  channel. `openreactor profile validate` checks one, and `--dry-run` prints
  its timeline. An example is in `examples/profiles/`.

### Removed

- The 1.x application. It stays on the `1.x` branch and its tags.
