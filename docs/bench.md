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
| Each EZO family reads, with temperature compensation applied | | | | | |
| Each EZO family calibrated from the CLI | | | | | |
| A 30-minute ramp-and-hold profile matches its `--dry-run` timeline | | | | | |

## Preparing a Pi

On 64-bit Raspberry Pi OS (Bookworm or Trixie):

1. Enable I2C and give your user access to it, then log out and back in:

   ```sh
   sudo raspi-config nonint do_i2c 0
   sudo usermod -aG i2c "$USER"
   sudo apt install -y git i2c-tools build-essential python3-dev
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

3. Install openreactor from a checkout. Until ezo-driver is on PyPI, `uv sync`
   builds it from source, which is why `build-essential` and `python3-dev`
   are needed above:

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
   reported by name, and the others are still read. Slices in the config are
   skipped with a note until #24 and #25.

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
