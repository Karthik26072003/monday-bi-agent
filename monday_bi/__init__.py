"""monday.com Business Intelligence agent package."""

# pandas >= 3 (and 2.x with the future flag) infers Arrow-backed string columns,
# whose `.map()` leaves NaN in place instead of passing None - which breaks the
# cleaning layer. Force the classic object dtype for the whole process.
import pandas as _pd

try:
    _pd.set_option("future.infer_string", False)
except (KeyError, ValueError):
    pass
