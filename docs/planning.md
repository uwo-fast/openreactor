# Planning & Future Developments

This is the current plan for future development of this application, this should be reviewed and opened for feedback then turned into a development roadmap.

## Cam's Thoughts

The most important next step (in my opinion) is generalizing the software so that software-knowledgeable people (or software people who aren't aware of the underlying architecture) aren't needed to extend the system's function (within reason). In practice, this means creating a mechanism to allow non-coding experts to translate their domain knowledge (i.e., chemical engineering) into system behavior. This mechanism requires two parts:

1. The ability to configure your system (i.e., define the physical system by expressing in software what the hardware setup is).
2. The ability to define the behavior and logic of the system.

In application, this might look like a new tab on the interface for hardware and software configuration, where the user can define the logic of their experiment using a high-level script.

For example, suppose you are a chemical engineer who doesn't write software applications but is experienced in MATLAB or similar. You first connect together all their hardware (RPi, slices, apparatus), then define how your hardware is configured and how the software should behave. Below is a (very) conceptual example of how a script like this might look. In this example, our experimental setup consists of a pressure vessel with a heating element, a gas inlet, liquid dosing pump, and pressure sensor. Note that I say the pressure sensor is SPI, NOT I2C as the slices use; this just means the driver needs to be written accordingly.

```python
# Configure the devices
# Assumptions:
# 1) Pressure sensor is an SPI device, not I2C
# 2) RLAY slice has 2 relays (real has 4)
# 3) RLHT slice has 1 relay and 1 temperature sensor (real has 2)
# 4) DCMT slice has 1 motor controller (real has 2)

pressureSensor1 = wiredevice.MyPressureSensor(address=1)  # Pressure sensor for readings
rlaySlice = serialdevice.Slice(address=2, type='RLAY')  # Slice with relays to open/close valves
rlhtSlice = wiredevice.Slice(address=3, type='RLHT')  # Slice with temperature reading and relay for heater control
dcmtSlice = wiredevice.Slice(address=4, type='DCMT')  # Slice with motor controller

# Define variables
pressure = 0
temperature = 0
valve1Status = False  # False for closed
valve2Status = False  # False for closed
pump1Status = False  # False for off

duration = 60  # Experiment duration in minutes
time1, time2, time3, time4 = 5, 15, 40, 55
pLimit = 100  # Example pressure limit
tLimit = 75   # Example temperature limit

# Main experimental script
def myExperiment():
    def onStart():
        """Initialize the experiment and perform one-time setup."""
        experiment.start()
        # Additional start up initializations

    pressure = pressureSensor1.read(0)  # Read pressure from sensor
    temperature = rlhtSlice.readTemp(0)  # Read temperature from slice

    # Terminate experiment if limits are exceeded
    if pressure > pLimit or temperature > tLimit:
        rlaySlice.relay(0, False)  # Close valve
        rlhtSlice.setTemp(False)   # Turn off temperature controller
        rlaySlice.relay(1, False)  # Cut power to certain devices
        experiment.log(f"Exceeded limit! Pressure: {pressure}, Temperature: {temperature}")
        experiment.end()

    # Experiment phases based on time
    if experiment.time(elapsed) < time1:
        rlhtSlice.setTemp(50)  # Set temperature
        rlaySlice.relay(0, False)  # Close gas valve
        rlaySlice.relay(1, True)   # Start pumping substance
    elif experiment.time(elapsed) < time2:
        # Phase 2 logic...
        pass
    elif experiment.time(elapsed) < time3:
        # Phase 3 logic...
        pass
    elif experiment.time(elapsed) < time4:
        # Phase 4 logic...
        pass
    else:
        # Final phase logic...
        pass

```

Added benefit of using a separate script is that the clear separation of the high-level experiment logic from the underlying application would allow researchers to share their device configurations in a manner that's intuitive, concise, and reconfigurable. On the developer side, this separation of concerns also benefits the ability for new developers to actually maintain and contribute to the software, as the core logic and experimental setups are decoupled from the technical implementation, making the codebase more accessible and easier to maintain. In the future this could be further abstracted to a diagram-based method or similar rather than scripting.

Note that in the above example I used Python syntax since it's widely used and would probably be the easiest to implement. But this selection is arbitary and you could use Octave, MATLAB, or similar. Really any high-level scripting language that's commonly used in industry and academia would work. It would really just change the difficulty of the implemetation terms of creating the part of the application that interprets the script to run on the underlying system.
