#!/usr/bin/env python
# coding: utf-8
#
# Licensed under MIT
#
import setuptools
from solox import __version__

with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    install_requires=['flask>=2.0.1', 'requests>=2.28.2', 'logzero', 'Flask-SocketIO==4.3.1', 'fire',
                      'python-engineio==3.13.2', 'python-socketio==4.6.0', 'Werkzeug==2.0.3',
                      'Jinja2==3.0.1','py-ios-device>=1.0.0', 'tqdm', 'xlwt','pyfiglet','psutil',
                      'opencv-python'],
    # NOTE: py-ios-device supports iOS 17+ (WDA replacement)
    # tidevice is kept for backward compatibility but deprecated
    version=__version__,
    long_description=long_description,
    python_requires='>=3.10',
    long_description_content_type="text/markdown",
    description="SoloX - Real-time collection tool for Android/iOS performance data.",
    packages=setuptools.find_namespace_packages(include=["solox", "solox.*"], ),
    include_package_data=True
)
