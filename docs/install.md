# Install

These steps set up openreactor on a Raspberry Pi running 64-bit Raspberry Pi
OS (Bookworm or Trixie). They install from a checkout of `main`, which these
documents describe.

## 1. Enable I2C

Enable I2C, give your user access to it, and install the I2C tools. Then log
out and back in, so the new group applies:

```sh
sudo raspi-config nonint do_i2c 0
sudo usermod -aG i2c "$USER"
sudo apt install -y git i2c-tools
```

On a Pi 4, also add these lines to `/boot/firmware/config.txt` and reboot:

```ini
dtparam=i2c_arm_baudrate=50000
core_freq_min=500
```

The Pi 4 derives its I2C clock from a core clock that its firmware scales, so
without `core_freq_min` the configured rate is only a ceiling. With it, 50 kHz
was the only rate that CRUMBS measured clean on a Pi 4; faster rates corrupted
replies or lost writes. The
[CRUMBS platform setup](https://github.com/feastorg/CRUMBS/blob/main/docs/platform-setup.md#the-ic-bus)
has the measurements.

## 2. Put the EZO circuits in I2C mode

Atlas Scientific EZO circuits ship in UART mode, and openreactor needs them in
I2C mode. To switch a circuit by hand:

1. Power it off, and disconnect TX and RX.
2. Connect TX to PGND, and power it on.
3. Wait for the LED to change from green to blue.
4. Power it off, and reconnect it as before.

Each circuit then comes up at its family's default address:

| Family | Address |
|---|---|
| pH | 0x63 |
| ORP | 0x62 |
| EC | 0x64 |
| DO | 0x61 |
| RTD | 0x66 |
| HUM | 0x6F |

`i2cdetect -y 1` should list each one.

## 3. Install openreactor

Install uv, clone the repository, and install openreactor with its
dependencies. ezo-driver ships as a wheel for the Pi's aarch64, so nothing is
compiled:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/uwo-fast/openreactor.git
cd openreactor
uv sync
```

Run openreactor from the checkout with `uv run openreactor`.

## 4. Write a configuration and check it

Copy `examples/openreactor.toml`, keep the devices you have, and set each
address to what the board answers on.
[configuration.md](configuration.md) describes every setting.

Check the file, then read every EZO circuit once:

```sh
uv run openreactor check-config ~/openreactor.toml
uv run openreactor read -c ~/openreactor.toml
```

`read` reports a circuit that is missing, at the wrong address or of the
wrong family by name, and still reads the others. It never touches a slice.

Without `-c`, the device commands read `/etc/openreactor/openreactor.toml`.

## Next steps

- [usage.md](usage.md) describes the commands, runs and calibration.
- [web.md](web.md) describes the browser UI and the HTTP API.
- Read [safety.md](safety.md) before you connect a heater: `run` and `serve`
  start every RLHT slice in the configuration.
