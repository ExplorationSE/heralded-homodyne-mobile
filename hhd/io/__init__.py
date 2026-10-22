"""Dateiformate: PTU (PicoQuant Generic T2) und HDF5-Schema v1 des Digitizers."""
from . import h5filters as _h5filters

_h5filters.register()          # Blosc-Filter für digitizer.h5 (ab 0.15), falls hdf5plugin installiert ist
