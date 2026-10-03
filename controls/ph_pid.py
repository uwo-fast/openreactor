import struct
from database.model import SensorReading, ControlReading
from controls.pid import pid


class feedback:
    def __init__(self, name: str, I2C):
        self.data = 0
        self.params = {}
        self.name = name
        self.I2C = I2C
        self.outputType = "b"
        self.baseID = 1
        self.acidID = 2
        self.identifier = self.baseID

    def resetPumps(self):
        id_base = struct.pack(self.outputType, self.baseID)
        id_acid = struct.pack(self.outputType, self.acidID)
        zero_speed = struct.pack(self.outputType, 0)
        self.I2C.controlMessage(id_base + zero_speed)
        self.I2C.write()
        self.I2C.controlMessage(id_acid + zero_speed)
        self.I2C.write()

    def getData(self):
        self.getParams()
        try:
            self.params["input"] = float(
                SensorReading.select()
                .where(SensorReading.name == "pH")
                .order_by(SensorReading.id.desc())
                .get()
                .value
            )
            self.params["lastInput"] = float(
                SensorReading.select()
                .where(SensorReading.name == "pH")
                .order_by(SensorReading.id.desc())
                .limit(2)[-1]
                .value
            )
            output, self.params["er"] = pid(
                self.params["input"],
                self.params["lastInput"],
                float(self.params["setpoint"]),
                float(self.params["kp"]),
                float(self.params["ki"]),
                float(self.params["kd"]),
                er=float(self.params.get("er", 0)),
                min_output=-100,
            )
            self.data = abs(output)
        except Exception as e:
            print(f"Error in PID calculation for {self.name}: {e}")
            self.data = 0

    def getParams(self):
        self.params = (
            ControlReading.select()
            .where(ControlReading.name == self.name)
            .order_by(ControlReading.time.desc())
            .get()
            .params
        )

    def params2data(self):
        if self.params["input"] - float(self.params["setpoint"]) < 0:
            # Need to add base
            self.identifier = self.baseID
            self.params["Acid Pump"] = 0
            self.params["Base Pump"] = self.data
        else:
            # Need to add acid
            self.identifier = self.acidID
            self.params["Acid Pump"] = self.data
            self.params["Base Pump"] = 0
        id_pump = struct.pack(self.outputType, self.identifier)
        speed = struct.pack(self.outputType, int(self.data))
        self.data = id_pump + speed

    def process(self):
        self.getData()
        self.resetPumps()
        self.params2data()
        self.I2C.edit_params(self.params)
        output = self.data
        return output

    def reset(self):
        self.resetPumps()
        default_params = (
            ControlReading.select()
            .where(ControlReading.name == self.name)
            .order_by(ControlReading.time.asc())
            .get()
            .params
        )
        id_pump = struct.pack(self.outputType, self.identifier)
        zero_speed = struct.pack(self.outputType, 0)
        return id_pump + zero_speed
