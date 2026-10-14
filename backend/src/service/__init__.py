"""Quantik backend."""
import os

# Bound numerical threads before workers import NumPy/SciPy; a separate process
# budget prevents each worker creating a full machine's worth of BLAS threads.
_threads = int(os.environ.get("QUANTIK_NUMERICAL_THREADS", "1"))
if not 1 <= _threads <= 8:
    raise ValueError("QUANTIK_NUMERICAL_THREADS must be between 1 and 8")
for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[_name] = str(_threads)
