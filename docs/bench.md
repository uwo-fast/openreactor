# Bench checklist

CI runs against fakes and never shows how the hardware behaves. These checks
are run by hand on a reactor before a release. Record each one with the date,
the openreactor and library versions, and the firmware commit on each slice.

| Check | Date | openreactor | Libraries | Firmware | Result |
|---|---|---|---|---|---|
| Firmware commits flashed on each slice | 2026-10-12 | 2.0.0a1 (ade88dc) | crumbs-i2c 0.15.0, bread-crumbs-contracts 0.5.0, ezo-driver 0.6.0 | Slice_RLHT 4383bd9 (`gen1_nano`); 2× Slice_DCMT 44966b3 | RLHT flashed. The DCMTs were exercised from 2026-10-14 |
| Version and caps reported as expected | 2026-10-12 | 2.0.0a1 (ade88dc) | as above | Slice_RLHT 4383bd9 | Pass: RLHT 1.0.0, CRUMBS 0.12.5, caps 0x7F |
| Watchdog armed, and it trips when the process is killed | 2026-10-12 | 2.0.0a1 (ade88dc) | as above | Slice_RLHT 4383bd9 | Pass: armed at 5000 ms. After `kill -9` at ~400 ms on per 1000 ms, the relay went off ~5 s later, by eye. The trip count went 1, then 2, over two kills, and each restart re-armed the watchdog |
| Desired state re-asserted after a slice power-cycle | 2026-10-13 | 2.0.0a1 (1018584) | as above | Slice_RLHT 4383bd9 | Pass on 1018584, twice. With a 35 °C setpoint, an RLHT reset gave `slice-reboot` `re-asserted`, the setpoint went to 0 and the relay stayed off. A status read 1 s after the event showed closed loop, thermocouple 1, a 1000 ms period and the watchdog armed at 5000 ms. A new setpoint afterwards turned the relay on again. Pass on d50e61a (2026-10-14), with a 35 °C setpoint re-sent every 0.2 s through the reset: the next send after the reset was refused with 503 "could not read the slice" and never sent, then `slice-reboot` `re-asserted`, the setpoint at 0, the output off and the watchdog armed, within 4 s. Relay only, no heater attached |
| E-stop pressed and released resumes nothing | 2026-10-13 | 2.0.0a1 (1018584) | as above | Slice_RLHT 4383bd9 | Pass on 1018584 for an e-stop that cuts backplane power: at a 35 °C setpoint, release gave `slice-reboot` `re-asserted` and the relay stayed off (#62). No `slice-unreachable` event was logged while it was pressed; how long it was held was not timed. The ESTOP-pin e-stop is not tested on this rig. Pass on d50e61a (2026-10-14), with a 35 °C setpoint re-sent every 0.2 s: the send during the hold was refused with 503, and release gave `slice-reboot` `re-asserted` on all three slices, setpoint 0, output off. Holds of 10 s and 40 s were timed: during both, every I2C transfer blocked about 1 s, status read "the controller did not answer in time" or stale values, and `slice-unreachable` came only around the release of the 40 s hold, none for 10 s (#91). Relay only, no heater attached |
| Stop-all from the UI, the API and `systemctl stop` | 2026-10-12 | 2.0.0a1 (ade88dc) | as above | Slice_RLHT 4383bd9 | Pass, all three. `systemctl --user stop` of a transient `systemd-run` unit (none ships yet, #29): stop-all 91 ms later, and the relay went off at once |
| Time from stop-all to the relay off, on a heating RLHT output (#51) | 2026-10-12 | 2.0.0a1 (ade88dc) | as above | Slice_RLHT 4383bd9 | Pass: relay off at once, by eye. The API answered in 91 ms, and a status read 1 s later showed `on_ms 0`. Relay only, no heater wired |
| Each EZO family reads, with temperature compensation applied | 2026-10-12 | 2.0.0a1 (ade88dc) | as above | EZO pH 2.15, DO 2.17 | pH and DO read, exit 0. Compensation not tested: no RTD on this rig |
| Each EZO family calibrated from the CLI | | | | | |
| A run recorded and exported from the CLI and the API | 2026-10-13 | 2.0.0a1 (1018584) | as above | Slice_RLHT 4383bd9 | Pass. CLI: a 28 s run ended `stopped` on Ctrl-C with a stop-all event; `export` wrote `readings.csv` (129 rows), `events.csv` and `run.json`. API: a 45 s run ended `stopped`, and its export held 204 readings (on ade88dc, a 93 s run held 524) |
| The API from another machine on the lab network, signed in | 2026-10-12 | 2.0.0a1 (ade88dc) | as above | Slice_RLHT 4383bd9 | Pass: a wrong password was refused (401). Signed in, the UI set a setpoint and ran Stop all |
| The controller lock refuses a second command | 2026-10-13 | 2.0.0a1 (1018584) | as above | | Pass: `read` while `serve` ran exited 1, naming the holder's host, pid and command. The `sudo` half of the check was not run. On d50e61a (2026-10-14): `sudo openreactor read` while `serve` ran exited 1, naming `serve`'s pid; after `serve` stopped, `sudo openreactor read` exited 0. Pass |
| DCMT started safe and armed, version and caps as expected | 2026-10-14 | 2.0.0a1 (d50e61a) | crumbs-i2c 0.15.0, bread-crumbs-contracts 0.6.0, ezo-driver 0.6.0 | 2× Slice_DCMT 44966b3 | Pass, both slices: `slice-start` gave DCMT 1.0.0, CRUMBS 0.14.0, caps 0x00000077, `watchdog armed at 5000 ms`; open loop, all four motors at 0 and braked |
| DCMT duty from the UI and the API, each motor and each direction, and 0 brakes it | 2026-10-14 | 2.0.0a1 (d50e61a) | crumbs-i2c 0.15.0, bread-crumbs-contracts 0.6.0, ezo-driver 0.6.0 | 2× Slice_DCMT 44966b3 | Pass on all four motors (enclosed pumps): 40 % from the API, -40 % from the UI's setpoint action, 0 from the API, 3 s each. Each ran, reversed and braked, by eye and in status, and the other three stayed braked |
| DCMT stop-all from the UI, the API and Ctrl-C brakes both motors at once | 2026-10-14 | 2.0.0a1 (d50e61a) | crumbs-i2c 0.15.0, bread-crumbs-contracts 0.6.0, ezo-driver 0.6.0 | 2× Slice_DCMT 44966b3 | Pass, all three, with all four motors at 40 %: every stop-all reported `ok` for each slice and the motors braked. Status shows the last poll, so it can read the motors as running for up to `slice_poll_s` after the stop-all |
| DCMT e-stop pressed and released resumes nothing | 2026-10-14 | 2.0.0a1 (d50e61a) | crumbs-i2c 0.15.0, bread-crumbs-contracts 0.6.0, ezo-driver 0.6.0 | 2× Slice_DCMT 44966b3 | Pass for an e-stop that cuts backplane power, two motors at 40 %, holds of 10 s and 40 s: release gave `slice-reboot` `re-asserted` on both DCMTs, the motors stayed braked and the watchdogs were armed again. The ESTOP-pin e-stop is not tested on this rig. Status during the hold: #91 |
| DCMT watchdog trip held when the process is killed, and cleared only on request | 2026-10-14 | 2.0.0a1 (d50e61a) | crumbs-i2c 0.15.0, bread-crumbs-contracts 0.6.0, ezo-driver 0.6.0 | 2× Slice_DCMT 44966b3 | Pass: `kill -9` with all four motors at 40 %; how soon they stopped was not timed by eye. On restart both slices gave `slice-start` `held`, status showed TRIPPED with trips 1, and duties were refused with 409 from the API and the UI. Clear trip from the API (0x14) and the UI (0x15) left the motors braked until a duty was set. A clean Ctrl-C, 8 s down, then restart held no trip |
| DCMT reboot: motors stay stopped and the watchdog is armed again | 2026-10-14 | 2.0.0a1 (d50e61a) | crumbs-i2c 0.15.0, bread-crumbs-contracts 0.6.0, ezo-driver 0.6.0 | 2× Slice_DCMT 44966b3 | Pass: 0x15 pulled from the backplane with motor 1 at 40 %. It failed fast (`[Errno 5]`), went `slice-unreachable`, and on reseat gave `slice-reachable` and `slice-reboot` `re-asserted`, the motor braked and the watchdog armed. Reseating it also rebooted 0x14 and the RLHT, which re-asserted and stayed safe |
| A 30-minute ramp-and-hold profile matches its `--dry-run` timeline | 2026-10-14 | 2.0.0a1 (d50e61a) | crumbs-i2c 0.15.0, bread-crumbs-contracts 0.6.0, ezo-driver 0.6.0 | Slice_RLHT 4383bd9; 2× Slice_DCMT 44966b3 | Pass on timing: jacket 30 °C then a ramp to 37 °C over 20 min and a 10 min hold, stirrer 30 % for 10 min, a ramp to 60 % over 5 min and a 15 min hold. The run ended `completed` at 30:00.2. Stirrer ramp from 10:00.1, 60 % at 14:58.1; jacket 37 °C at 19:52.1; both safe at 30:00.1 and 30:00.2. No heater attached, so the jacket temperature did not follow |
| A profile aborted by stop-all, an e-stop and a power-cut e-stop resumes nothing | 2026-10-14 | 2.0.0a1 (d50e61a) | crumbs-i2c 0.15.0, bread-crumbs-contracts 0.6.0, ezo-driver 0.6.0 | 2× Slice_DCMT 44966b3 | Pass on a four-motor profile for stop-all, `POST /api/v1/profile/abort`, stopping its run, and a power-cut e-stop (aborted on `slice-reboot`): each run ended `stopped`, all four motors braked and nothing resumed. A setpoint by hand during it was refused with 409. Ctrl-C on `serve` ended its run `stopped`, not `interrupted` (#92). The ESTOP-pin e-stop is not tested on this rig |

## Before you start

Prepare the Pi and install openreactor as [install.md](install.md)
describes, with a configuration that lists the rig's devices.

## How to run the controller lock check

Run `openreactor read --follow` as one user, then `sudo openreactor read` in
another terminal. The second must refuse, naming the first. After the first
exits, `sudo openreactor read` must work.

## How to run the EZO checks

With every EZO circuit in I2C mode and listed in the config. Temperature
compensation uses the `RT` command, which needs pH firmware V2.12 or later and
DO firmware V2.13 or later; record each circuit's firmware (`i`) in the row.

1. `openreactor read` prints one line per channel, and exits 0. pH, EC
   and DO show `(compensated at … °C)` when their config names an RTD in
   `temp_comp`.
2. With an EZO-HUM, enable all three outputs (`O,T,1` and `O,Dew,1`) and
   check `openreactor read` prints humidity, air temperature and dew point. No
   vendor example shows the three-value reply, so only a real circuit can
   confirm ezo-driver parses it.
3. Check what the datasheets leave open: whether an RTD in °F or K expects its
   `Cal,t` reference in that scale, and that an EC calibrated at two and three
   points reports "two point" and "three point".
4. For each family, `openreactor ezo cal <device> status`, then one
   calibration point with a reference solution, then `status` again shows the
   new calibration. Record the points used.

## How to run the run and API checks

1. Record a short run from the CLI and export it:

   ```sh
   uv run openreactor run -c ~/openreactor.toml --name bench --notes "bench check"
   # Ctrl-C after a few readings
   uv run openreactor runs -c ~/openreactor.toml
   uv run openreactor export -c ~/openreactor.toml 1 -o bench-1.zip
   ```

   The run ends `stopped`, and `readings.csv` in the zip has a row per reading
   with its unit.

2. Serve on the Pi itself and drive the API from it:

   ```sh
   uv run openreactor serve -c ~/openreactor.toml
   curl -s localhost:8080/api/v1/status
   curl -s localhost:8080/api/v1/channels
   curl -s -X POST localhost:8080/api/v1/runs \
     -H 'Content-Type: application/json' -d '{"name": "api bench"}'
   curl -s -X POST localhost:8080/api/v1/runs/current/stop
   curl -s -o api-bench.zip localhost:8080/api/v1/runs/2/export
   curl -s localhost:8080/api/v1/ezo/ph/calibration
   ```

   Then stop the server with Ctrl-C: the run, if one is recording, ends
   `stopped`, with a stop-all event.

3. For the lab network, make a password hash, put it in the config with
   `host = "0.0.0.0"`, and serve again:

   ```sh
   uv run openreactor hash-password   # paste into server.password_hash
   uv run openreactor serve -c ~/openreactor.toml
   ```

   From another machine, `curl http://<pi>:8080/api/v1/status` answers 401,
   and with `-H 'Authorization: Bearer <password>'` it answers. Without the
   hash, `serve` refuses to bind `0.0.0.0`.

## How to run the DCMT checks

With a DCMT slice in the config and `openreactor serve` going. Use a motor
that can spin freely, or a stirrer in water. `openreactor status` and
`/api/v1/status` under `dcmt_slices` show each motor's duty and brake. To
run motor 1, set its duty:

```sh
curl -s -X PUT localhost:8080/api/v1/channels/stirrer/setpoint \
  -H 'Content-Type: application/json' -d '{"value": 40}'
```

1. **Start-up:** the `slice-start` event names the DCMT version, caps
   `0x00000077` on a gen1 Nano, and `watchdog armed at <timeout> ms`. Both
   motors are braked.
2. **Duty:** 40, -40 and 0 on each motor, from the API and the Controls page.
   The motor runs, reverses, then brakes; status shows the duty and the
   brake. Setting one motor never changes the other.
3. **Stop-all:** with both motors running, Stop all, the API's stop-all,
   and Ctrl-C on `serve`. Both brake at once.
4. **E-stop:** with a motor running, press the slice's e-stop: an `e-stop`
   `held` event and stop-all. Release it: `released`, and the motor stays
   stopped. On a rig whose e-stop cuts the backplane's power, release gives
   a `slice-reboot` event instead, and the motor stays stopped.
5. **Trip:** with a motor running, `kill -9` the server. The motor brakes
   within `watchdog_timeout_ms`. Start `serve` again: the `slice-start`
   event says a watchdog trip is held, duties are refused with 409, and the
   Devices page shows Clear trip. Clear it: the motors stay braked until a
   duty is set.
6. **Reboot:** power-cycle the DCMT alone while a motor runs. A
   `slice-reboot` event with result `re-asserted`, the motors stay stopped,
   and status shows the watchdog armed.
7. **Clean shutdown:** stop `serve` with Ctrl-C, then wait longer than the
   watchdog timeout and start it again. No trip is held.

## How to run the RLHT checks

With an RLHT slice in the config and `openreactor serve` going: it prints
each event as it happens, and records it in a run while one is recording.
`openreactor status` shows each slice's state, from the same server. To heat
an output, set its setpoint:

```sh
curl -s -X PUT localhost:8080/api/v1/channels/jacket/setpoint \
  -H 'Content-Type: application/json' -d '{"value": 40}'
```

1. **Watchdog armed:** the start-up event reads `watchdog armed at <timeout> ms`.
2. **Watchdog trip:** stop openreactor without stop-all (`kill -9` the
   process) while a heater runs. The relay must go off within
   `watchdog_timeout_ms`. Don't run `openreactor status` or anything else
   that reads the slice meanwhile: every reply feeds its watchdog. Start it
   again: start-up arms the watchdog again.
3. **Trip noticed while running:** pause the bus for longer than the watchdog
   timeout (unplug the slice's I2C briefly). Within five polls of it answering
   again, a `slice-trip` event with result `re-asserted`.
4. **Re-assert after a power-cycle:** power-cycle the slice while openreactor
   runs with a setpoint set. A `slice-reboot` event with result
   `re-asserted`, the channel's setpoint reading stays at 0 and the relay
   stays off, and `openreactor status` shows closed loop and the configured
   thermocouples. This is for a default build, which boots disarmed. A build
   with `RLHT_WATCHDOG_BOOT_MS` gives a `slice-trip` instead, and only if it
   had tripped before (#53).
5. **E-stop:** press the slice's e-stop. An `e-stop` event with result `held`,
   then stop-all; the relays stay off. Release it: an `e-stop` event with
   result `released`, and the setpoints stay at 0. On a rig whose e-stop
   cuts the backplane's power instead, release gives step 4's event, and
   the relay stays off.
6. **Unreachable:** disconnect the slice's I2C. After 3 failed polls, a
   `slice-unreachable` event, and `openreactor status` shows it unreachable.
   Reconnect it: a `slice-reachable` event, and the watchdog is checked at
   once, so a slice that lost power meanwhile also gives step 4's event. A
   disconnect longer than `watchdog_timeout_ms` also gives a `slice-trip`
   event with result `re-asserted`.
