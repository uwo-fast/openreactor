import os
import busio
import time
import json
from board import SCL, SDA

class connected:
    def __init__(self):
        self.devs = []
        self.cons = []
        # Get the directory of the current file
        dir = os.path.dirname(os.path.abspath(__file__))

        # Initialize I2C bus
        i2c = busio.I2C(SCL, SDA)
        devices = i2c.scan()
        i2c.deinit()
        basedir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        # Open devices.json file
        with open(os.path.join(basedir, "devices.json")) as file:
            full_dict = json.load(file)
        self.dev = full_dict["DEVICES"]
        self.con = full_dict["CONTROLS"]

        for dev in devices:
            addrs = dev
            dev_name = self.find_dev(dev, "name")
            con_name = self.find_con(dev, "name")
            if dev_name:
                form = self.find_dev(dev, "form")
                unit = self.find_dev(dev, "unit")
                req_msg = self.find_dev(dev, "req_msg")
                delay = self.find_dev(dev, "delay")
                read_length = self.find_dev(dev, "read_length")
                auto = self.find_dev(dev, "auto")
                if isinstance(dev_name, list):
                    for i in range(len(dev_name)):
                        try:
                            self.devs.append(
                                (
                                    addrs,
                                    dev_name[i],
                                    unit[i],
                                    form[i],
                                    req_msg[i],
                                    delay[i],
                                    read_length[i],
                                    auto[i],
                                )
                            )
                        except Exception as e:
                            raise Exception(
                                "Issue assigning device information for addresses with more than one device. "
                                "Ensure that in devices.json all arguments except for address are arrays of the same length."
                            ) from e
                else:
                    self.devs.append(
                        (addrs, dev_name, unit, form, req_msg, delay, read_length, auto)
                    )
            if con_name:
                form = self.find_con(dev, "form")
                unit = self.find_con(dev, "unit")
                req_msg = self.find_con(dev, "req_msg")
                delay = self.find_con(dev, "delay")
                read_length = self.find_con(dev, "read_length")
                enabled = self.find_con(dev, "enabled")
                params = self.find_con(dev, "params")
                def_state = self.find_con(dev, "def_state")
                if isinstance(con_name, list):
                    for i in range(len(con_name)):
                        try:
                            self.cons.append(
                                (
                                    addrs,
                                    con_name[i],
                                    unit[i],
                                    form[i],
                                    req_msg[i],
                                    delay[i],
                                    read_length[i],
                                    enabled[i],
                                    params[i],
                                    def_state[i],
                                )
                            )
                        except Exception as e:
                            raise Exception(
                                "Issue assigning device information for addresses with more than one device. "
                                "Ensure that in devices.json all arguments except for address are arrays of the same length."
                            ) from e
                else:
                    self.cons.append(
                        (
                            addrs,
                            con_name,
                            unit,
                            form,
                            req_msg,
                            delay,
                            read_length,
                            enabled,
                            params,
                            def_state,
                        )
                    )
            elif not con_name and not dev_name:
                print(f"Unknown device found with address: {addrs}")

    def find_dev(self, addr, search):
        for device in self.dev:
            if device["address"] == addr:
                return device.get(search, None)
        return None

    def find_con(self, addr, search):
        for control in self.con:
            if control["address"] == addr:
                return control.get(search, None)
        return None
