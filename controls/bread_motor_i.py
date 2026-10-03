import struct
from database.model import ControlReading

# Open loop: safe to apply a change from the interface straight away
APPLY_ON_CHANGE = True


class feedback:
    """
    Feedback mechanism for Motor I controls.
    """

    def __init__(self, name, I2C):
        self.data = 0
        self.params = {}
        self.name = name
        self.I2C = I2C
        self.motorID = 1  # Motor I identifier
        self.outputType = "b"

    def getParams(self):
        try:
            self.params = (
                ControlReading.select()
                .where(ControlReading.name == self.name)
                .order_by(ControlReading.time.desc())
                .get()
                .params
            )
        except Exception as e:
            print(f"Error retrieving parameters for {self.name}: {e}")
            self.params = {}

    def params2data(self):
        try:
            # Pack the motor ID and speed into bytes
            id_byte = struct.pack(self.outputType, self.motorID)
            speed = int(self.params.get("speed", 0))
            speed_byte = struct.pack(self.outputType, speed)
            self.data = id_byte + speed_byte
        except Exception as e:
            print(f"Error packing data for {self.name}: {e}")
            self.data = b""

    def process(self):
        self.getParams()
        self.params2data()
        output = self.data
        return output

    def reset(self):
        try:
            default_params = (
                ControlReading.select()
                .where(ControlReading.name == self.name)
                .order_by(ControlReading.time.asc())
                .get()
                .params
            )
            self.params = default_params
            id_byte = struct.pack(self.outputType, self.motorID)
            default_speed = int(default_params.get("speed", 0))
            speed_byte = struct.pack(self.outputType, default_speed)
            return id_byte + speed_byte
        except Exception as e:
            print(f"Error resetting {self.name}: {e}")
            return b""
