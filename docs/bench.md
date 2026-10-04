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

## How to run the EZO checks

With every EZO circuit in I2C mode and listed in the config:

1. `openreactor read --once` prints one line per channel, and exits 0. pH, EC
   and DO show `(compensated at … °C)` when their config names an RTD in
   `temp_comp`.
2. With an EZO-HUM, enable all three outputs (`O,T,1` and `O,Dew,1`) and
   check `read --once` prints humidity, air temperature and dew point. No
   vendor example shows the three-value reply, so only a real circuit can
   confirm ezo-driver parses it.
3. For each family, `openreactor ezo cal <device> status`, then one
   calibration point with a reference solution, then `status` again shows the
   new calibration. Record the points used.
