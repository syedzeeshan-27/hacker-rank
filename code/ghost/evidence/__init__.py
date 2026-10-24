"""WS-B: evidence files -> Observations. See registry.extract_all / extract_all_detailed."""
from .registry import Coverage, ExtractionResult, extract_all, extract_all_detailed

__all__ = ["Coverage", "ExtractionResult", "extract_all", "extract_all_detailed"]
