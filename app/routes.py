from app import app, utils
from flask import render_template, request, jsonify
from app import (
    database,
    toDisplay,
    devices,
    controls,
    I2C_dev,
    I2C_connections,
    equations,
    THREAD_TIME,
    experimentThreadStart,
    experimentThreadStop,
    graphsUpdate,
    experiment_entries,
    basedir,
)
from database.model import (
    Sensor,
    SensorReading,
    Control,
    ControlReading,
    systemSettings,
    Data,
)
from experiments.experiments import experiment
import json
import time
import traceback
from datetime import datetime, timedelta
import os
from pathlib import Path


@app.route("/")
def index():
    """
    Default landing page
    Returns
    -------
        loads the default index.html
    """
    return render_template("index.html")


# Graph update methods
@app.route("/update/graphs/<graphID>/<name>", methods=["GET", "POST"])
def update(graphID, name):
    """
    Fetches up-to-date data for the graph.

    Parameters
    ----------
    graphID : int
        The index for the requested graph to be updated.
    name : string
        Name for the sensor that is being displayed on the graph.

    Returns
    -------
    Method 'GET'
        JSON with the time array, value array, title of the graph, and formatted time array.
    Method 'POST'
        Returns confirmation of success, adds that graph to toDisplay.
    """
    if request.method == "GET":
        print(toDisplay)
        print(f"Selected: {name}")
        if int(graphID) >= len(toDisplay):
            return jsonify({"error": "Graph ID out of range"}), 400
        time_values, values, title, toParse = graphsUpdate(
            toDisplay[int(graphID)], name
        )
        return json.dumps(
            {"Time": time_values, "Values": values, "Title": title, "Parsed": toParse}
        )
    elif request.method == "POST":
        data = request.json
        print(data[0])
        toDisplay[int(graphID)] = data[0]
        return jsonify(data)


@app.route("/add", methods=["POST"])
def add():
    """
    Used whenever an empty graph is created on the page.

    Parameters
    ----------
    Request : json
        JSON sent from interface containing number of current graphs.
    """
    print(toDisplay)
    data = request.json[0]
    print(data)
    if data == 0:
        toDisplay.clear()
    if len(toDisplay) == data:
        toDisplay.append("-1")
    return jsonify(data)


# Graphs page
@app.route("/graphs", methods=["GET", "POST"])
def graphs():
    """
    Loads the default graphs page.
    """
    # Initialize toDisplay with a default value if empty
    if not toDisplay:
        toDisplay.append("-1")
    return render_template("graphs.html", Devices=devices)


@app.route("/about")
def about():
    """
    Loads the default info page.

    Returns
    -------
        Renders about.html.
    """
    return render_template("about.html")


# Page for controls
@app.route("/controls")
def controls_page():
    """
    Loads the controls page, sends information at time of request.

    Returns
    -------
        Renders controls.html with initial values.
    """
    sensors = []
    values = []
    controls_list = []
    enabled = []
    controls_values = []
    pars = []

    for dev in Sensor.select():
        sensors.append(dev.name)
        print(f"Name: {dev.name}")
        val = (
            SensorReading.select()
            .where(SensorReading.name == dev.name)
            .order_by(SensorReading.time.desc())
            .get()
            .value
        )
        print(val)
        values.append(val)
    for con in Control.select():
        controls_list.append(con.name)
        valEnabled = (
            ControlReading.select()
            .where(ControlReading.name == con.name)
            .order_by(ControlReading.id.desc())
            .get()
            .enabled
        )
        enabled.append(str(valEnabled))
        valCon = (
            ControlReading.select()
            .where(ControlReading.name == con.name)
            .order_by(ControlReading.id.desc())
            .get()
            .value
        )
        controls_values.append(valCon)
        par = (
            ControlReading.select()
            .where(ControlReading.name == con.name)
            .order_by(ControlReading.id.desc())
            .get()
            .params
        )
        pars.append(par)
    return render_template(
        "controls.html",
        Sensors=sensors,
        Values=values,
        Controls=controls_list,
        Enabled=enabled,
        ControlsValues=controls_values,
        Params=pars,
    )


@app.route("/update/controls", methods=["GET"])
def updateControls():
    """
    Updates the controls page with current values.

    Returns
    -------
    json
        Contains connected sensors, values, initiated controls, control targets, and control values.
    """
    if request.method == "GET":
        sensors = []
        values = []
        enabled = []
        controls_list = []
        controls_values = []
        pars = []
        for dev in Sensor.select():
            sensors.append(dev.name)
            print(dev.name)
            values.append(
                SensorReading.select()
                .where(SensorReading.name == dev.name)
                .order_by(SensorReading.time.desc())
                .get()
                .value
            )
        for con in Control.select():
            controls_list.append(con.name)
            valEnabled = (
                ControlReading.select()
                .where(ControlReading.name == con.name)
                .order_by(ControlReading.id.desc())
                .get()
                .enabled
            )
            enabled.append(int(valEnabled))
            valCon = (
                ControlReading.select()
                .where(ControlReading.name == con.name)
                .order_by(ControlReading.id.desc())
                .get()
                .value
            )
            controls_values.append(valCon)
            par = (
                ControlReading.select()
                .where(ControlReading.name == con.name)
                .order_by(ControlReading.id.desc())
                .get()
                .params
            )
            pars.append(par)
        return json.dumps(
            {
                "sen": sensors,
                "val": values,
                "con": controls_list,
                "en": enabled,
                "con_val": controls_values,
                "par": pars,
            }
        )


@app.route("/controls/calibrate", methods=["POST", "GET"])
def calibrate():
    """
    Handles calibration of sensors.

    Updates the calibration equations stored in equations.json.

    Returns
    -------
    Empty response with status code 204.
    """
    if request.method == "POST":
        req = request.json
        print(req)
        equations[req[0]] = req[1]
        eq = Path(os.path.join(basedir, "sensors", "maths", "equations.json"))
        with open(eq, "w") as f:
            json.dump(equations, f)
    return ("", 204)


@app.route("/controls/reset", methods=["POST"])
def resetControls():
    """
    Resets the parameters of the control systems to default.
    """
    global I2C_connections
    with utils.dataLock:
        for con in I2C_connections:
            con.reset_control()
    ret = updateControls()
    return ("", 204)


@app.route("/controls/measure/<side>", methods=["GET", "POST"])
def measure(side):
    """
    Takes measurements of sensors when selected in controls interface,
    sets targets for control systems.

    Parameters
    ----------
    side : string
        Either "sensor" or "control" - the interface is split into two sides,
        one for sensors and one for controls.
    """
    if request.method == "POST":
        # Hold dataLock so this cannot interleave with an experiment cycle
        if side == "sensor":
            sensor_measure = request.json
            for dev in I2C_dev:
                if dev.name == sensor_measure:
                    with utils.dataLock:
                        dev.read()
                        dev.store(equations[dev.name])
        elif side == "control":
            control_return = request.json
            control_name = control_return["name"]
            print(f"Control Return: {control_return}")
            for control in I2C_connections:
                if control.name == control_name:
                    # What remains after removing name and enabled is the params
                    del control_return["name"]
                    with utils.dataLock:
                        control.control_state(int(control_return["enabled"]))
                        del control_return["enabled"]
                        control.edit_params(control_return)
                        try:
                            utils.applyControlChange(control)
                        except Exception:
                            print("Error applying control :: {}".format(control.name))
                            traceback.print_exc()
    return ("", 204)


@app.route("/update/experiments/<method>/<name>", methods=["GET", "POST"])
def updateExp(method, name):
    """
    Interfaces with the experiment files, handles writing and reading.
    See /experiments/experiments.py

    Parameters
    ----------
    method : string
        The requested information/instruction.
    name : string
        The name of the experiment to be interfaced with.

    Returns
    -------
    json
        Content returned is based on request method:
        - If 'GET', returns a JSON with experiment info.
        - If 'POST', returns success boolean.
    """
    exp = experiment(os.path.join(basedir, "experiments"))
    if request.method == "GET":
        if method == "list":
            path, ret = exp.list()
        elif method == "info":
            ret = exp.info(name)
        return json.dumps({"ret": ret})
    elif request.method == "POST":
        if method == "new":
            success = exp.new(name)
        elif method == "start":
            experimentThreadStart(THREAD_TIME, I2C_dev, I2C_connections)
            success = exp.start(name)
        elif method == "end":
            experimentThreadStop()
            success = exp.end(name)
        elif method == "delete":
            success = exp.delete(name)
        elif method == "exists":
            success = exp.exists(name)
            time.sleep(1)
        return jsonify(success)


@app.route("/download/csv/<selected>/<forSensor>", methods=["GET"])
def createDownload(selected, forSensor):
    """
    Gathers data for export as CSV.

    Parameters
    ----------
    selected : string
        The name of the selected experiment.
    forSensor : string
        Name of the sensor for which the measurements should be exported.

    Returns
    -------
    json
        Contains sensor name, array of values, array of timestamps.
    """
    exp = experiment(os.path.join(basedir, "experiments"))
    sen, timeStart, timeEnd, running = exp.info(selected)
    if timeEnd == -1:
        timeEnd = datetime.fromtimestamp(time.time()).timestamp()
    sen, dataData, dataTime = experiment_entries(timeStart, timeEnd, forSensor)
    return json.dumps({"sensorName": sen, "values": dataData, "time": dataTime})
