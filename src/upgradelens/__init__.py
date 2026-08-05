"""UpgradeLens — static dependency upgrade analysis.

Stage 1 scope: parse dependency manifests and compare the declared version
against a target version. Nothing in this package imports, installs or executes
the repository being analysed.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
