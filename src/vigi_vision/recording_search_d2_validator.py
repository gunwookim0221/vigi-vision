"""Public total adapters for the D2-0 closed result boundary."""

from vigi_vision.recording_search_d2_c2_adapter import adapt_c2, adapt_c2_result
from vigi_vision.recording_search_d2_d1_adapter import adapt_d1, adapt_d1_result

__all__ = ["adapt_c2", "adapt_c2_result", "adapt_d1", "adapt_d1_result"]
