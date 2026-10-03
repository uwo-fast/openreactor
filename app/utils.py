import os
import threading
import time
import importlib
import traceback
from pathlib import Path
from datetime import datetime, timedelta
import json

from sensors import sensor
from sensors.device_detect import connected as ct
from sensors.maths.symbolicParser import var, parse
from experiments.experiments import experiment
from database.model import (
    Sensor,
    SensorReading,
    Control,
    ControlReading,
    systemSettings,
    Data,
)

# Base directory
basedir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Initialize database
database = Data()


def graphsUpdate(toDisplay, selected):
    """
    Retrieves and formats data for graphing.

    Parameters
    ----------
    toDisplay : string
        Name of the sensor to display.
    selected : string
        Name of selected experiment that is to be displayed.

    Returns
    -------
    tuple
        Contains arrays that have the time of measurement, value of measurement,
        graph title, formatted time of measurement.
    """
    print(toDisplay)
    if toDisplay == "-1":
        time1 = []
        values1 = []
        title1 = ""
        toParse = []
    else:
        title1 = f"{toDisplay} over time"
        dataTime = []
        dataData = []
        exp = experiment(os.path.join(basedir, "experiments"))  # Initialize experiment
        sen, timeStart, timeEnd, running = exp.info(selected)  # Get experiment info
        current_time = datetime.now().timestamp()

        # Adjust timeStart and timeEnd if they are not set
        if timeStart == -1:
            timeStart = current_time - 3600  # Default to last hour
        if timeEnd == -1:
            timeEnd = current_time

        sen, dataData, dataTime = experiment_entries(timeStart, timeEnd, toDisplay)
        values1 = dataData
        time1 = dataTime

        # Normalize time values relative to the first timestamp
        if time1:
            first = time1[0]
            time1 = [(t - first) / 3600 for t in time1]
        else:
            time1 = []
        toParse = list(zip(dataTime, values1))
        print("Fetched Data")
    return time1, values1, title1, toParse


# Utility functions
def innit_connected():
    """
    Used to get all connected I2C sensors
    Returns
    ----------
    I2C_dev : array
        array that the I2C objects are appended to
    """
    connected = ct()
    I2C_dev: list[sensor.I2C] = []

    equations = Path(basedir + "/sensors/maths/equations.json")
    equations.touch(exist_ok=True)
    f = open(equations)
    try:
        j: dict[str, str] = json.load(f)
    except:
        f.close()
        j = {}
    f.close()

    print(j)

    for sen in connected.devs:
        dev = sensor.I2C(
            name=sen[1],
            units=sen[2],
            address=sen[0],
            form=sen[3],
            request_message=sen[4],
            delay=sen[5],
            read_length=sen[6],
            auto=sen[7],
        )  # creates I2C object for each detected sensor
        I2C_dev.append(dev)
        if not SensorReading.select().where(
            SensorReading.name == dev.name
        ):  # if this sensor hasn't been used before creates an 'empty' reading of -1 that creates an entry but isn't parsed
            print("Missing :: {}".format(dev.name))
            dev.readEmpty()
            dev.store()
            print("Created Entry :: {}".format(dev.name))

        if not dev.name in j:
            print("Creating Default Equation for {}".format(dev.name))
            j[dev.name] = "1x+0"
        f = open(equations, "w")
        json.dump(j, f)
        f.close()
    return I2C_dev, j


def innit_control():
    """
    Used to get all connected I2C control mechanisms
    Returns
    ----------
    I2C_con : array
        array that the I2C objects are appended to

    """
    connected = ct()
    I2C_con: list[sensor.I2C] = []
    for dev in connected.cons:
        global feedbackModules
        print(dev[1])
        feedbackModules[dev[1]] = importlib.import_module(dev[8][0]["control"])
        con: sensor.I2C = sensor.I2C(
            name=dev[1],
            units=dev[2],
            address=dev[0],
            form=dev[3],
            request_message=dev[4],
            delay=dev[5],
            read_length=dev[6],
            enabled=dev[7],
            params=dev[8][0],
            def_state=dev[9],
        )
        I2C_con.append(con)
        con.reset_control()
        if not ControlReading.select().where(ControlReading.name == con.name):
            print("Missing :: {}".format(con.name))
            # con.readEmpty()
            con.reset_control()
            # con.store()
            print("Created Entry :: {}".format(con.name))
    return I2C_con
    # SensorData().define_control(name="System Toggle", target=-1,value=0)


def experiment_entries(timeStart, timeEnd, name):
    """
    Used to determine measurements taken within a given experiment cycle
    Parameters
    ----------
    timeStart : float
        experiment start time given in seconds elapsed since Unix epoch, note that JS uses milliseconds
    timeEnd : float
        experiment end time given in seconds elapsed since Unix epoch
    name : string
        name of the sensor for which measurements are requested, if "all" then every reading regardless of sensor will be returned
    Returns
    -------
    tuple
        returns 3 arrays the sensor names, the sensor values, and the measurement times, sorted by time
    """
    timeStart = datetime.fromtimestamp(timeStart)
    timeEnd = datetime.fromtimestamp(timeEnd)
    sensor = []
    values = []
    times = []
    if name == "all":
        for v in (
            SensorReading.select()
            .where(SensorReading.time.between(timeStart, timeEnd))
            .order_by(SensorReading.time)
        ):  # selects all measurements between the two times
            sensor.append(v.name)
            values.append(v.value)
            times.append(v.time.timestamp())
        for v in (
            ControlReading.select()
            .where(ControlReading.time.between(timeStart, timeEnd))
            .order_by(ControlReading.time)
        ):  # selects all measurements between the two times
            sensor.append(v.name)
            values.append(v.enabled)
            times.append(v.time.timestamp())
        return sensor, values, times
    else:
        for v in (
            SensorReading.select()
            .where(
                SensorReading.name == name,
                SensorReading.time.between(timeStart, timeEnd),
            )
            .order_by(SensorReading.time)
        ):  # selects measurements for specified sensor in time range
            sensor.append(v.name)
            values.append(v.value)
            times.append(v.time.timestamp())
        for v in (
            ControlReading.select()
            .where(
                ControlReading.name == name,
                ControlReading.time.between(timeStart, timeEnd),
            )
            .order_by(ControlReading.time)
        ):  # selects measurements for specified sensor in time range
            sensor.append(v.name)
            values.append(v.enabled)
            times.append(v.time.timestamp())
        return sensor, values, times


def applyControl(control: sensor.I2C):
    """
    Runs the feedback plugin of a control and writes its output to the device.
    The caller must hold 'dataLock', as the experiment thread uses the same bus.
    """
    plugin = feedbackModules[control.name]
    cfb = plugin.feedback(control.name, control)
    out = cfb.process() if control.enabled else cfb.reset()
    control.controlMessage(out, cfb.outputType)
    control.write()


def applyControlChange(control: sensor.I2C):
    """
    Applies a change made in the interface straight away when that is safe:
    always when the control is disabled, so it is reset to its default output,
    and when enabled only if its plugin sets APPLY_ON_CHANGE. Closed-loop
    plugins are left to the experiment thread, which reads their inputs first.
    The caller must hold 'dataLock'.
    """
    plugin = feedbackModules[control.name]
    if control.enabled and not getattr(plugin, "APPLY_ON_CHANGE", False):
        return
    applyControl(control)


def experimentThread(
    cycle_length: int,
    dev: list[sensor.I2C],
    connections: list[sensor.I2C],
    generation: int = 0,
):
    """
    The function that is used to obtain measurements and run processes in a seperate thread as to not interupt connection.
    Measurements are obtained and formatted using the formatting code in 'sensors/sensor.py'
    Control systems are defined by individual formatted python scripts placed in the directory 'control'. The specific script for each control needs to be specified as a parameter in 'devices.json'.
    Restarts itself recursively until stopped.
    ----------
    cycle_length : int
        number of seconds between each cycle. NOTE:Does not include running time.
    dev : I2C object Array
        array that holds all the I2C sensor objects as defined in 'sensors/sensor.py'
    con : I2C object Array
        array that holds all the I2C control objects as defined in 'sensors/sensor.py'
    """
    with dataLock:
        global threadHandle
        global activeRead
        global running
        if not running or generation != loopGeneration:
            return  # stopped, or restarted, while this cycle waited for the lock
        activeRead = True
        threadStart = time.time()
        for d in dev:
            # print('Reading :: {}'.format(d.name))
            try:
                if d.auto:
                    d.read()
                    d.store(equations[d.name])
            except:
                print("Error with Read of Sensor :: {}\n".format(d.name))
                print(Exception)
                traceback.print_exc()
                # experimentThreadStop()
                # print("Relaunching Thread")
                # experimentThreadStart(cycle_length,dev,con)
            # print('Stored :: {}'.format(d.name))
        for control in connections:
            # print('Reading :: {}'.format(control.name))
            try:
                applyControl(control)
                control.store()
            except:
                print("Error with Read of Control:: {}\n".format(control.name))
                print(Exception)
                traceback.print_exc()
                # experimentThreadStop()
                # print("Reloading Thread")
                # experimentThreadStart(cycle_length,dev,con)
            # print('Stored :: {}'.format(c.name))
        elapsed = time.time() - threadStart
        newTime = cycle_length - elapsed  # accounts for time taken in measurements
        if newTime <= 0:
            newTime = (
                0  # if it took longer than cycle length, start new readings right away
            )
            cycle_length = elapsed  # update passed on cycle length as cycle isn't long enough for readings to be taken
            print("Experiment Cycle too short. Extended to :: {}".format(cycle_length))
        activeRead = False
        database.cycleSet(cycle_length)
        if running:
            threadHandle = threading.Timer(
                newTime, experimentThread, (cycle_length, dev, connections, generation)
            )
            threadHandle.daemon = True
            threadHandle.start()
        else:
            print("Ending Experiment")


def experimentThreadStop():
    """
    Used to stop the thread and data aq.
    As the .cancel() function only kills the thread during the time interval stage, will recursively call itself until it can safely kill the thread
    """
    global threadHandle
    global activeRead
    global running
    running = False
    if not activeRead:
        threadHandle.cancel()
        with dataLock:
            for i in range(len(I2C_con)):
                # print('Reading :: {}'.format(c.name))
                c = I2C_con[i]
                try:
                    m = feedbackModules[c.name]
                    cfb = m.feedback(c.name, c)
                    out = cfb.reset()
                    c.controlMessage(out, cfb.outputType)
                    c.write()
                    c.store()
                except:
                    print("Error with Reset of Control on Stop:: {}\n".format(c.name))
                    print(Exception)
                    traceback.print_exc()
    if activeRead:
        time.sleep(0.1)
        experimentThreadStop()


def experimentThreadStart(cycle_length, dev, con):
    """
    Used to start the initial thread for data aq.
    Parameters
    ----------
    cycle_length : int
        number of seconds between each cycle. NOTE: Does not include running time.
    dev : I2C object Array
        array that holds all the I2C sensor objects as defined in 'sensors/sensor.py'
    con : I2C object Array
        array that holds all the I2C control objects as defined in 'sensors/sensor.py'
    """
    global threadHandle
    global running
    global loopGeneration
    with dataLock:
        if running:
            return  # already running: a second loop would double every cycle
        running = True
        loopGeneration += 1

        print("Starting Threading with interval :: {}".format(cycle_length))
        threadHandle = threading.Timer(
            cycle_length, experimentThread, (cycle_length, dev, con, loopGeneration)
        )
        threadHandle.daemon = True
        threadHandle.start()


# Initialize variables
feedbackModules: dict = {}
runningExperiments = experiment(os.path.join(basedir, "experiments")).running
running_start = experiment(os.path.join(basedir, "experiments")).running_start
I2C_dev, equations = innit_connected()
I2C_con: list[sensor.I2C] = innit_control()

toDisplay = []
devices = [dev.name for dev in Sensor.select()]
controls = [con.name for con in Control.select()]

activeRead = False
running = False
loopGeneration = 0  # bumped by each start; cycles from an older start exit
# An unstarted timer, so experimentThreadStop() can cancel it before any start
threadHandle = threading.Timer(0, lambda: None)
dataLock = threading.Lock()

# Get cycle length from system settings or set default
if (
    systemSettings.select()
    .where(systemSettings.cycleLength)
    .order_by(systemSettings.id.desc())
    .exists()
):
    THREAD_TIME = (
        systemSettings.select()
        .where(systemSettings.cycleLength)
        .order_by(systemSettings.id.desc())
        .get()
        .cycleLength
    )
else:
    THREAD_TIME = 5
    database.cycleSet(THREAD_TIME)

# Start experiment thread if needed
try:
    experimentThreadStop()
except:
    print("No Running Experiment Thread Found")

if runningExperiments > 0:
    print("Experiment found to still be running on launch, resuming data gathering.")
    experimentThreadStart(THREAD_TIME, I2C_dev, I2C_con)
