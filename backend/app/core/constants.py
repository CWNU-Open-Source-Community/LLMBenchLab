"""Stable application and protocol constants."""

from decimal import Decimal

API_V1_PREFIX = "/api/v1"
PROTOCOL_VERSION = "llmbenchlab-protocol-v1"
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_READ_TIMEOUT_SECONDS = 60.0
DEFAULT_WRITE_TIMEOUT_SECONDS = 30.0
DEFAULT_POOL_TIMEOUT_SECONDS = 5.0
MAX_GENERATION_TOKENS = 131_072
MIN_READ_TIMEOUT_SECONDS = 1.0
MAX_READ_TIMEOUT_SECONDS = 1_800.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BACKOFF_BASE_SECONDS = 0.25
DEFAULT_RETRY_BACKOFF_CAP_SECONDS = 2.0
RETRYABLE_PROVIDER_STATUS_CODES = (408, 429, 500, 502, 503, 504)

# SQLite applies numeric affinity through IEEE-754 binary floats. Keeping the
# largest governance amount at 10 million USD leaves the float spacing below
# half of the 1e-8 storage quantum, so Numeric(20, 8) round-trips every accepted
# amount to the same eight-decimal value. PostgreSQL remains exact NUMERIC.
MAX_GOVERNANCE_COST_USD = Decimal("10000000.00000000")
