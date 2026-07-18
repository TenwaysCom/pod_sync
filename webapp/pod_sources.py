from __future__ import annotations

from typing import NamedTuple


class PodSource(NamedTuple):
    carrier_id: int
    source_name: str
    carrier_name: str


# Keep the existing CMC / EMTC presentation order and append newly supported
# sources.  Report, comparison and export code should derive its carrier
# filters and labels from this registry instead of maintaining separate maps.
POD_SOURCE_REGISTRY = (
    PodSource(carrier_id=25, source_name="CMC", carrier_name="CMC"),
    PodSource(carrier_id=24, source_name="EMTC", carrier_name="EMTC"),
    PodSource(carrier_id=38, source_name="Westerman", carrier_name="Westerman"),
)

POD_SOURCE_NAMES = tuple(source.source_name for source in POD_SOURCE_REGISTRY)
POD_SOURCE_CARRIER_IDS = frozenset(source.carrier_id for source in POD_SOURCE_REGISTRY)
POD_SOURCE_NAME_BY_CARRIER_ID = {
    source.carrier_id: source.source_name for source in POD_SOURCE_REGISTRY
}
