"""Constants for the Daejeon CCTV integration."""

DOMAIN = "daejeon_cctv"

# Config keys
CONF_CCTV_URL = "cctv_url"
CONF_CCTV_NAME = "cctv_name"
CONF_HLS_SEGMENT_DURATION = "hls_segment_duration"
CONF_MAX_SEGMENTS = "max_segments"

# Defaults
DEFAULT_NAME = "Daejeon CCTV"
DEFAULT_HLS_SEGMENT_DURATION = 4
DEFAULT_MAX_SEGMENTS = 10

# Timing
FETCH_INTERVAL_SUCCESS = 5  # seconds between downloads when video found
FETCH_INTERVAL_FAIL = 3     # seconds to retry when no video found
IDLE_TIMEOUT = 120          # seconds before stopping inactive stream

# Paths
VIDEO_BASE_DIR = "/tmp/daejeon_cctv"
