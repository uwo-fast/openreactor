#!/bin/bash

# Quit script if there are any errors
set -e

# Update all submodules
git submodule update --init --recursive

# Install needed dependancies
sudo apt update; sudo apt upgrade -y
sudo apt install -y python3-venv sqlite3 npm
sudo apt install -y python3-dev python-dev-is-python3
sudo apt install build-essential

# Create and configure python virtual enviroment
python3 -m venv .venv
sudo chmod -R a+rwx .venv
source .venv/bin/activate
pip install wheel
pip install --upgrade wheel
pip install --upgrade setuptools
sudo chmod -R 777 ./
pip install -r requirements.txt
pip3 install --upgrade adafruit-blinka adafruit-platformdetect
# Install NPM packages
cd app/static
npm i 
