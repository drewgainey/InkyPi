# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

InkyPi is a Flask app that runs on a Raspberry Pi and drives an e-ink display (Pimoroni Inky or Waveshare). Plugins generate PIL images, and a background thread cycles through them on a schedule. A web UI is used to configure everything.

## Commands

```bash
# Setup (or use devbox: `devbox shell` / `devbox run dev`, which sources scripts/venv.sh)
python3 -m venv venv && source venv/bin/activate
pip install -r install/requirements-dev.txt
bash install/update_vendors.sh          # downloads frontend vendor JS/CSS into src/static

# Run without hardware: uses src/config/device_dev.json, mock display, port 8080
python src/inkypi.py --dev
# Rendered output goes to mock_display_output/latest.png

# Tests: src/ must be on PYTHONPATH because app code uses top-level imports (`from utils...`)
PYTHONPATH=src pytest
PYTHONPATH=src pytest tests/test_model.py::TestPlaylist -k "21:00"
```

There's no linter config. Follow `.editorconfig`: 4-space indents for Python, 2-space for HTML/JS/CSS/JSON/sh.

## Import conventions

App code runs with `src/` as the working root, so modules import each other as `from utils.image_utils import ...` and `from model import ...`. Tests import through the package path (`from src.model import ...`), so both `.` and `src/` have to resolve. That's why tests need `PYTHONPATH=src`. Use the top-level style in new app code.

## Architecture

**Startup (`src/inkypi.py`)**: builds the `Config`, `DisplayManager` and `RefreshTask` singletons and stores them in `app.config['DEVICE_CONFIG' | 'DISPLAY_MANAGER' | 'REFRESH_TASK']`. Blueprints in `src/blueprints/` get them from `current_app.config`. The Jinja loader searches both `src/templates/` and `src/plugins/`, which is how plugin `settings.html` files get included in the web UI. The app is served with waitress using `threads=1`.

**Config (`src/config.py`)**: holds all persistent state in a single JSON file. That's `src/config/device.json` in production (copied from `install/config_base/device.json` at install time) or `device_dev.json` with `--dev`. It also reads every `src/plugins/*/plugin-info.json` to build the plugin list. Secrets come from a `.env` file at the project root through `load_env_key` (see `docs/api_keys.md` for the key names). `write_config()` serializes the `PlaylistManager` and `RefreshInfo` model objects back into the JSON.

**Model (`src/model.py`)**: `PlaylistManager` → `Playlist` (a time window `start_time`–`end_time`; windows can wrap past midnight, and when several playlists are active the narrowest wins) → `PluginInstance` (a named, configured use of a plugin, with its own settings and refresh interval). `RefreshInfo` records what is currently on the display, including an image hash.

**Refresh loop (`src/refresh_task.py`)**: a daemon thread waits on a `Condition` for `plugin_cycle_interval_seconds` or until it's woken by `manual_update()`. Each pass does the following:
1. Takes a `ManualRefresh` (from "Display" in the UI) or picks the next `PlaylistRefresh` from the active playlist.
2. Calls the plugin's `generate_image(settings, device_config)`.
3. Hashes the image and skips the display update if the hash matches the last refresh.
4. Saves `RefreshInfo` to the config.

Blueprints call `manual_update()` and then wait on `refresh_event`. Exceptions are passed back to them through `refresh_result`.

**Display (`src/display/`)**: `DisplayManager` picks a driver from `display_type`. The options are `mock`, `inky`, or any `epd*in*` value, which loads the matching Waveshare driver from `waveshare_epd/`. Hardware imports are optional, so they fail quietly on dev machines. Before passing the image to the driver, it saves it to `src/static/images/current_image.png`, then applies orientation, resize and enhancement.

**Plugins (`src/plugins/`)**: each plugin is a directory `<id>/` containing `<id>.py`, `plugin-info.json` (`id`, `class`, `display_name`), `icon.png`, and optionally `settings.html` and `render/`. `plugin_registry.load_plugins` imports `plugins.<id>.<id>` and creates **one shared instance per plugin type**. Per-instance state lives in the `settings` dict, not on the plugin object, and plugins may change `settings` to keep data between refreshes. Subclass `BasePlugin`:
- `generate_image()` returns a `PIL.Image`. Raise `RuntimeError` with a message meant for users when something fails; it's shown in the UI.
- `render_image(dimensions, html_file, css_file, template_params)` renders `render/*.html`, which extends `plugin.html` in `base_plugin/render`, then screenshots it with a headless Chromium found on PATH (`chromium-headless-shell`, `chromium` or `chrome`; see `utils/image_utils.take_screenshot`). Pass `plugin_settings=settings` in `template_params` to apply the shared style, frame and background options.
- `generate_settings_template()` adds variables for `settings.html`. Set `style_settings: True` to include the shared style controls.
- `settings.html` has to prefill its form in edit mode when `loadPluginSettings` is set, using `pluginSettings`.
- `cleanup(settings)` runs when an instance is deleted.

Full guide: `docs/building_plugins.md`. Weather and AI Text are good examples of HTML-rendered plugins; `clock` is a simple Pillow-based one.

## Deployment

`install/install.sh` installs to the Pi as the systemd service `inkypi.service`, running on port 80. `inkypi plugin install <plugin_id> <git_url>` (`install/cli/inkypi-plugin`) installs third-party plugins by copying a repo's `<plugin_id>/` folder into `src/plugins/`. Restart the service with `sudo systemctl restart inkypi.service` after code changes on a device.
