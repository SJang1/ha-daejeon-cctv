# Daejeon CCTV Integration for Home Assistant

A custom Home Assistant integration to view Daejeon city CCTV streams from utic.go.kr.

## Why?

Daejeon city's CCTV system serves short MP4 video clips (~30 seconds) instead of live streams. This integration downloads these clips, converts them to HLS format, and loops them seamlessly until new footage arrives - providing a continuous viewing experience.

## Features

- 🎥 Stream CCTV feeds directly in Home Assistant
- 🔄 Automatic video download and seamless looping via HLS
- 🔀 Seamless video switching when new footage arrives
- 📝 Automatic extraction of CCTV name from URL
- ⚙️ Configurable HLS segment duration and buffer size
- 🏠 Works with Home Assistant's native stream integration

## Requirements

- **FFmpeg**: Must be installed on your Home Assistant system
  - Home Assistant OS: Pre-installed
  - Docker: Ensure FFmpeg is available in the container
  - Manual install: `apt install ffmpeg` or `brew install ffmpeg`

## Installation

### HACS (Recommended)

1. Add this repository to HACS as a custom repository:
   - Open HACS → Integrations → ⋮ (three dots) → Custom repositories
   - Add URL: `https://github.com/siliconsjang/ha-daejeon-cctv`
   - Category: Integration

2. Install "Daejeon CCTV" through HACS
3. Restart Home Assistant

### Manual Installation

1. Copy the `custom_components/daejeon_cctv` directory to your Home Assistant's `custom_components` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings** → **Devices & Services**
2. Click **+ Add Integration**
3. Search for "Daejeon CCTV"
4. Enter the CCTV URL from [utic.go.kr](https://www.utic.go.kr/map/map.do?menu=cctv)

### Configuration Options

| Option | Default | Description |
|--------|---------|-------------|
| CCTV URL | (required) | The CCTV page URL from utic.go.kr |
| CCTV Name | (auto-detected) | Display name for the camera |
| HLS Segment Duration | 2 seconds | Length of each HLS segment |
| Max Segments | 5 | Number of segments to keep in playlist |

### Example URL Format

```
www.utic.go.kr/jsp/map/cctvStream.jsp?cctvid=E07070&cctvname=%EC%A4%91%EB%A6%AC%EB%84%A4%EA%B1%B0%EB%A6%AC&kind=E&...
```

The integration will automatically:
- Extract and decode the CCTV name from the `cctvname` parameter
- Fetch and download video clips from the source
- Convert to HLS for seamless looping and playback

## Usage

After configuration, the CCTV camera will appear as a camera entity in Home Assistant.

### Example Lovelace Cards

**Picture Glance Card:**
```yaml
type: picture-glance
title: Daejeon CCTV
camera_image: camera.jungrinegeori
entities: []
```

**Picture Entity Card (with live stream):**
```yaml
type: picture-entity
entity: camera.jungrinegeori
camera_view: live
show_state: false
```

## How It Works

```
┌─────────────────┐     ┌──────────────┐     ┌─────────────┐
│  utic.go.kr     │────▶│  Download    │────▶│   FFmpeg    │
│  (MP4 clips)    │     │  Video       │     │   (HLS)     │
└─────────────────┘     └──────────────┘     └─────────────┘
                                                    │
                                                    ▼
                                             ┌─────────────┐
                                             │  HA Stream  │
                                             │  Player     │
                                             └─────────────┘
```

1. **Fetch**: Polls the CCTV page every 5 seconds for new video URLs
2. **Download**: Downloads new video clips when the URL changes
3. **HLS Convert**: FFmpeg converts MP4 to HLS segments with infinite looping
4. **Seamless Switch**: When new video arrives, seamlessly switches using HLS append mode
5. **Stream**: Home Assistant's stream integration plays the HLS feed

## Known Issues & Warnings

### Timestamp Discontinuity Warning

You may see this error in logs when a new video arrives:

```
ERROR (stream_worker) [homeassistant.components.stream] Error from stream worker: 
Timestamp discontinuity detected: last dts = XXXXX, dts = XXXXX
```

**This is harmless and can be ignored.** It occurs because each video clip has independent timestamps. The stream recovers automatically and continues playing.

### Video Takes a Few Seconds to Start

On first load, the integration needs to:
1. Fetch the video URL
2. Download the video clip
3. Start FFmpeg and generate HLS segments

This typically takes 3-5 seconds.

## Troubleshooting

### No video appears
- Check that FFmpeg is installed: `ffmpeg -version`
- Verify the CCTV URL is accessible in a web browser
- Check Home Assistant logs for error messages

### Stream stops or freezes
- The integration automatically restarts FFmpeg if it crashes
- Check logs for "HLS process died" messages
- Verify disk space in `/tmp/daejeon_cctv/`

### High CPU usage
- FFmpeg uses stream copy (no re-encoding) for minimal CPU usage
- If CPU is high, check for multiple camera instances or zombie FFmpeg processes

### Clean up temp files
Video files are stored in `/tmp/daejeon_cctv/`. To clean up manually:
```bash
rm -rf /tmp/daejeon_cctv/
```

## Technical Details

### File Structure

```
/tmp/daejeon_cctv/
└── {camera_id}/
    ├── video_20251211_143500.mp4    # Downloaded video clips
    ├── video_20251211_143530.mp4
    └── hls/
        ├── index.m3u8               # HLS playlist
        ├── chunk_00100.ts           # HLS segments
        ├── chunk_00101.ts
        └── loop_100.txt             # FFmpeg concat loop file
```

### API Endpoints

| Endpoint | Description |
|----------|-------------|
| `/api/daejeon_cctv/{camera_id}/hls/index.m3u8` | HLS playlist |
| `/api/daejeon_cctv/{camera_id}/hls/{filename}` | HLS segments |

## Development

### Project Structure

```
custom_components/daejeon_cctv/
├── __init__.py          # Integration setup
├── camera.py            # Camera entity & HLS stream manager
├── config_flow.py       # Configuration flow UI
├── const.py             # Constants and defaults
├── manifest.json        # Integration metadata
└── translations/
    ├── en.json          # English translations
    └── ko.json          # Korean translations
```

### Key Components

- **HLSStreamManager**: Manages FFmpeg process for HLS streaming with seamless video switching
- **HLSPlaylistView**: HTTP endpoint serving HLS playlists
- **HLSSegmentView**: HTTP endpoint serving HLS segments
- **DaejeonCCTVCamera**: Camera entity with download loop and stream management

## License

MIT License

## Support

For issues and feature requests, please visit: [GitHub Issues](https://github.com/siliconsjang/ha-daejeon-cctv/issues)
