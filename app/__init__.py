from flask import Flask
from config import Config

app = Flask(__name__)
app.config.from_object(Config)

# Import utilities
from app import utils

# Expose variables to app package
database = utils.database
toDisplay = utils.toDisplay
devices = utils.devices
controls = utils.controls
I2C_dev = utils.I2C_dev
I2C_connections = utils.I2C_con
equations = utils.equations
THREAD_TIME = utils.THREAD_TIME
experimentThreadStart = utils.experimentThreadStart
experimentThreadStop = utils.experimentThreadStop
graphsUpdate = utils.graphsUpdate
experiment_entries = utils.experiment_entries
basedir = utils.basedir

# Import routes after app is created to avoid circular imports
from app import routes
