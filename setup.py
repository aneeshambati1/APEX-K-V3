"""Package installation configuration for APEX-K MAJOR."""

from setuptools import find_packages, setup

setup(
    name="apexk-major",
    version="1.0.0",
    description="APEX-K ETF paper-trading research and execution system",
    packages=find_packages(exclude=("tests", "tests.*")),
    python_requires=">=3.10",
    install_requires=[
        "PyYAML>=6.0,<7.0",
    ],
)
