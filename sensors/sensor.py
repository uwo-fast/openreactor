import busio
from database.model import Sensor, SensorReading, Control, ControlReading, Data
from sensors.maths.symbolicParser import var, parse
import time
import datetime
import random
import struct
from board import SCL, SDA
from sensors.sensor_format import form
import peewee


class I2C:
    """
    Class used to define I2C devices connected to the system,
    contains methods for measurement and database interfacing.
    """

    def __init__(
        self,
        name: str,
        units: str,
        form: str = "atlas",
        address: int = 99,
        request_message=0x52,
        delay: float = 0.9,
        read_length: int = 31,
        enabled=-1,
        params=-1,
        def_state: bool = False,
        auto: bool = True,
    ):
        """
        Contains all essential information for communication with the device.

        Parameters
        ----------
        name : string
            Name of sensor.
        units : string
            Units of measurement.
        form : string
            Formatting type.
        address : int
            I2C address for sensor.
        request_message : int
            I2C register address.
        delay : float
            Delay in seconds between write and read.
        read_length : int
            Number of bytes to read from register.
        """
        self.name: str = name
        self.units = units
        self.addr = address
        self.req_msg = request_message
        self.delay = delay
        self.read_len = read_length
        self.value = 0.0
        self.time = datetime.datetime.now()
        self.form = form
        self.enabled = enabled
        self.params = params
        self.def_state = def_state
        self.db = Data()
        self.def_params = params
        self.auto = auto
        if params == -1:
            self.db.define_sensor(name, units)
        else:
            self.db.define_control(name, units, def_state)

    def __del__(self):
        """Deconstructor to close the connection to the database."""
        self.db.close()

    def read(self):
        """Reads from the sensor and places the reading in 'value'."""
        try:
            i2c = busio.I2C(SCL, SDA, frequency=400000)
            while not i2c.try_lock():
                time.sleep(0.01)
            if isinstance(self.req_msg, list):
                to_write = self.req_msg
            else:
                to_write = [self.req_msg]
            if isinstance(to_write[0], str):
                to_write = [ord(char) for char in to_write[0]]
            if to_write:
                i2c.writeto(self.addr, bytes(to_write))
            result = bytearray(self.read_len)
            time.sleep(self.delay)
            i2c.readfrom_into(self.addr, result)
            i2c.unlock()
            i2c.deinit()
            result = form(self.form, result)
            self.value = result
            self.time = datetime.datetime.now()
        except Exception as e:
            print(f"Error reading from sensor {self.name}: {e}")
            self.value = None
            self.time = datetime.datetime.now()

    def write(self):
        """Used for control systems, writes only."""
        try:
            i2c = busio.I2C(SCL, SDA, frequency=400000)
            while not i2c.try_lock():
                time.sleep(0.01)
            to_write = self.req_msg
            i2c.writeto(self.addr, bytearray(to_write))
            i2c.unlock()
            i2c.deinit()
        except Exception as e:
            print(f"Error writing to device {self.name}: {e}")

    def controlMessage(self, message: bytes, type="f"):
        """
        Used to change the byte array that is written to a given address.
        Store must be called separately.
        """
        self.req_msg = message
        self.time = datetime.datetime.now()

    def readFalse(self):
        """Reads a false random float as a measurement (for testing)."""
        self.value = random.uniform(4.5, 9.5)
        self.time = datetime.datetime.now()

    def readEmpty(self):
        """Reads a value of 0 as a measurement, used for init of sensor to avoid issues."""
        self.value = 0
        self.time = datetime.datetime.now()
        self.db.add_reading(time=self.time, name=f"{self.name}", value=self.value)

    def store(self, equation="1x+0"):
        """Stores the value of the latest sensor reading into the database."""
        if self.params == -1:  # Sensor
            if self.value is None:
                return
            print(f"Value: {self.value} Type: {type(self.value)}")
            eq = parse(equation)
            try:
                self.value = float(self.value)
                self.value = eq.apply(self.value)
                print(f"Applying equation {eq.equation()} :: {self.value}")
                self.db.add_reading(
                    time=self.time, name=f"{self.name}", value=self.value
                )
            except Exception as e:
                print(f"Error applying equation to sensor {self.name}: {e}")
        else:  # Control
            self.db.add_control_status(
                time=self.time,
                name=f"{self.name}",
                value=self.value,
                enabled=self.enabled,
                params=self.params,
            )

    def edit_params(self, new_params):
        """Used to edit the params of the control system."""
        self.params = new_params
        self.time = datetime.datetime.now()
        self.store()

    def control_state(self, state):
        """Changes the enabled state of the controls."""
        self.enabled = state

    def reset_control(self):
        """Resets to default state."""
        print(f"Resetting control {self.name} to default state.")
        self.enabled = self.def_state
        self.params = self.def_params
        self.time = datetime.datetime.now()
        self.store()

    def print_value(self):
        """Prints the most recent sensor value and time of its reading."""
        print(f"{self.time}: {self.value}\n")

    def print_i2c_info(self):
        """Prints the sensor's I2C info."""
        print(
            f"Address: {hex(self.addr)}\n"
            f"Read request message: {self.req_msg}\n"
            f"Read delay time: {self.delay} seconds\n"
            f"Length of read: {self.read_len} bytes\n"
        )

    def print_db_info(self):
        """Prints the sensor's database info."""
        print(f"{self.name} ({self.units})\n")

    def print_info(self):
        """Prints all of the sensor's info."""
        self.print_db_info()
        self.print_i2c_info()
        self.print_value()
