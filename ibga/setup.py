"""
Copyright (C) 2024 Interactive Brokers LLC. All rights reserved. This code is subject to the terms
 and conditions of the IB API Non-Commercial License or the IB API Commercial License, as applicable.
"""

from setuptools import setup

import sys

PACKAGE_VERSION = "10.30.1"

if sys.version_info < (3, 1):
    sys.exit("Only Python 3.1 and greater is supported")

setup(
    name="ibapi",
    version=PACKAGE_VERSION,
    packages=["ibapi", "ibapi/protobuf"],
    install_requires=["protobuf==5.29.3"],
    url="https://interactivebrokers.github.io/tws-api",
    license="IB API Non-Commercial License or the IB API Commercial License",
    author="IBG LLC",
    author_email="api@interactivebrokers.com",
    description="Python IB API",
)
