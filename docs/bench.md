# Bench checklist

CI runs against fakes and never shows how the hardware behaves. These checks
are run by hand on a reactor before a release. Record each one with the date,
the openreactor and library versions, and the firmware commit on each slice.

| Check | Date | openreactor | Libraries | Firmware | Result |
|---|---|---|---|---|---|
| Firmware commits flashed on each slice | | | | | |
| Version and caps reported as expected | | | | | |
| Watchdog armed, and it trips when the process is killed | | | | | |
| Desired state re-asserted after a slice power-cycle | | | | | |
| E-stop pressed and released resumes nothing | | | | | |
| Stop-all from the UI, the API and `systemctl stop` | | | | | |
| Time from stop-all to the relay off, on a heating RLHT output (#51) | | | | | |
| Each EZO family reads, with temperature compensation applied | | | | | |
| Each EZO family calibrated from the CLI | | | | | |
| A run recorded and exported from the CLI and the API | | | | | |
| The API from another machine on the lab network, signed in | | | | | |
| A 30-minute ramp-and-hold profile matches its `--dry-run` timeline | | | | | |

## Preparing a Pi

On 64-bit Raspberry Pi OS (Bookworm or Trixie):

1. Enable I2C and give your user access to it, then log out and back in:

   ```sh
   sudo raspi-config nonint do_i2c 0
   sudo usermod -aG i2c "$USER"
   sudo apt install -y git i2c-tools
   ```

2. Put every EZO circuit in I2C mode. They ship in UART mode. To switch one by
   hand: power off, disconnect TX and RX, connect TX to PGND, power on, and
   wait for the LED to go from green to blue; then power off and reconnect.
   Each circuit comes up at its family's default address:

   | Family | Address |
   |---|---|
   | pH | 0x63 |
   | ORP | 0x62 |
   | EC | 0x64 |
   | DO | 0x61 |
   | RTD | 0x66 |
   | HUM | 0x6F |

   `i2cdetect -y 1` should then list each one.

3. Install openreactor from a checkout. ezo-driver comes as a wheel for the
   Pi's aarch64, so nothing is compiled:

   ```sh
   curl -LsSf https://astral.sh/uv/install.sh | sh
   git clone https://github.com/uwo-fast/openreactor.git
   cd openreactor
   uv sync
   ```

4. Copy `examples/openreactor.toml`, keep the EZO devices you have with their
   addresses, and check it:

   ```sh
   uv run openreactor check-config ~/openreactor.toml
   uv run openreactor read -c ~/openreactor.toml
   ```

   A circuit that is missing, at the wrong address or of the wrong family is
   reported by name, and the others are still read. `read` never touches a
   slice; `run` and `serve` start the RLHT slices, and DCMT slices are skipped
   with a note until #25.

5. Check the controller lock across users: run `openreactor read --follow` as
   one user, then `sudo openreactor read` in another terminal. The second
   must refuse, naming the first. After the first exits, `sudo openreactor
   read` must work.

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

## How to run the RLHT checks

With an RLHT slice in the config and `openreactor serve` (or `run`) going.
Each one leaves an event in the run and shows in `/api/v1/status`.

1. **Watchdog armed:** the start-up event reads `watchdog armed at <timeout> ms`.
2. **Watchdog trip:** stop openreactor without stop-all (`kill -9` the
   process) while a heater runs. The relay must go off within
   `watchdog_timeout_ms`. Start it again: start-up arms the watchdog again.
3. **Trip noticed while running:** pause the bus for longer than the watchdog
   timeout (unplug the slice's I2C briefly). Within five polls of it answering
   again, a `slice-trip` event with result `re-asserted`.
4. **Re-assert after a power-cycle:** power-cycle the slice while openreactor
   runs. A `slice-reboot` event with result `re-asserted`, and GET_STATE shows
   the configured mode and thermocouples again.
5. **E-stop:** press the slice's e-stop. An `e-stop` event with result `held`,
   then stop-all; the relays stay off. Release it: an `e-stop` event with
   result `released`, and the setpoints stay at 0.
6. **Unreachable:** unplug the slice. After 3 polls, `/api/v1/status` shows it
   unreachable; plug it back in and it answers again.
