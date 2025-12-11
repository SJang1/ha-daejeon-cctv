# Daejeon CCTV Integration for Home Assistant

A custom Home Assistant integration to view Daejeon city CCTV streams from utic.go.kr.

## Why?

Daejeon system uses unstable, .mp4 file to stream CCTV. it is hard to continusely show datas.

## Features

- 🎥 Stream CCTV feeds directly in Home Assistant
- 🔄 Automatic video URL refresh and looping
- 📝 Automatic extraction of CCTV name from URL
- ⚙️ Easy configuration through Home Assistant UI

## Installation

### HACS (Recommended)

1. Add this repository to HACS as a custom repository:
   - Open HACS
   - Go to "Integrations"
   - Click the three dots in the top right
   - Select "Custom repositories"
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

### Example URL Format

```
www.utic.go.kr/jsp/map/cctvStream.jsp?cctvid=E07070&cctvname=%EC%A4%91%EB%A6%AC%EB%84%A4%EA%B1%B0%EB%A6%AC&kind=E&cctvip=118&cctvch=1&id=CCTV36&cctvpasswd=CTV0036&cctvport=undefined&minX=127.40504453624955&minY=36.35172722054125&maxX=127.44854816958492&maxY=36.37581000176645
```

The integration will automatically:
- Extract and decode the CCTV name from the `cctvname` parameter
- Fetch the video source from the page
- Keep the stream updated (refreshes every 30 seconds)

## Usage

After configuration, the CCTV camera will appear as a camera entity in Home Assistant. You can:

- View it on your dashboard using the Picture Glance card or Camera card
- Use it in automations
- Stream it to compatible devices

### Example Lovelace Card

```yaml
type: picture-glance
title: Daejeon CCTV
camera_image: camera.daejeon_cctv
entities: []
```

## How It Works

1. **URL Input**: You provide the CCTV stream URL
2. **Name Extraction**: The integration extracts and decodes the `cctvname` parameter from the URL
3. **Video Fetching**: The integration requests the URL and scrapes the video source (`<source src="...">`) from the page
4. **Auto-Refresh**: Every 30 seconds, the integration refreshes the video URL to ensure continuous playback
5. **Streaming**: The video is made available as a camera entity in Home Assistant

## Troubleshooting

### No video appears
- Check that the CCTV URL is accessible in a web browser
- Verify that the page contains a `<video>` or `<source>` tag with a valid `src` attribute
- Check Home Assistant logs for error messages

### Video stops playing
- The integration automatically refreshes the video URL every 30 seconds
- If issues persist, try removing and re-adding the integration

## Development

### Project Structure

```
custom_components/daejeon_cctv/
├── __init__.py          # Integration setup
├── camera.py            # Camera entity implementation
├── config_flow.py       # Configuration flow UI
├── const.py             # Constants
└── manifest.json        # Integration metadata
```

## License

MIT License

## Support

For issues and feature requests, please visit: [GitHub Issues](https://github.com/siliconsjang/ha-daejeon-cctv/issues)
