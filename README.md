
## About
When the configured max stream limit is reached. Show a configurable display to users.

If you want to show a useful screen / message to users when your stream limit is hit, then this is the plugin for you. 
Rather than have the stream error or show a static image. This plugin will show what other currently in use channels are available to view when someone tries to view a new channel when your at your configured limit. See the [example](#example)



- [Features](#features)
- [Notes](#notes)
  - [Dependencies](#dependencies)
- [Install](#install)
- [Config](#config)
- [Development](#development)
- [Build](#build)
- [Example](#example)

# Features
- Show a dynamic stream to users when the max stream limit is reached. This will show all currently active streams, that the user can view.
- Can show a static image by providing the path, via the `TMS_IMAGE_PATH` environment variable.

# Notes:
- This plugin requires some extra packages so please read the <b>Dependencies</b> section.
- This may add ~20-30s extra stop time to channels. You may see stopped channels hanging around longer then they should. My own testing shows no impact to any users. Feel free to raise an issue if there is one.
- This plugin creates its own <u>TooManyStreams</u> stream, that will be automatically added and removed. This is always added at the bottom of the channel list, preserving any of your channels stream setup.
- This patches the `apps.channels.models.Channel get_stream()` function. Adding a small amount of code to change the way streams are handled when the max stream limit is hit. See here: [TooManyStreams.py](https://github.com/JamesWRC/Dispatcharr_Too_Many_Streams/blob/d8071dd470bf1e95147004812d82eeaec828afc9/src/TooManyStreams.py#L438-L446)

### Dependencies
This plugin builds and renders some HTML to a JPG - using `wkhtmltopdf`.
To get this working, on install this plugin will `apt-get update && apt-get install -y wkhtmltopdf`
As always, install at your own risk. This could break your install

# Install.
1. Read the Notes section above first.
2. Take the zip file from [release](https://github.com/JamesWRC/Dispatcharr_Too_Many_Streams/releases/) and install it to your Dispatcharr.
3. Restart Dispatcharr. 
> [!IMPORTANT]  
> Anytime you install / uninstall. You must restart, else this plugin wont work.

# Config


### UI settings
In the Plugin page, you can now customize the Title, Description, column limit, and CSS. Restart is required after each change.

### Environment variables
This plugin uses environment variables for config.

| Variable           | Default   | Description                                                                                                   | Example                                   |
|--------------------|-----------|---------------------------------------------------------------------------------------------------------------|-------------------------------------------|
| `TMS_LOG_LEVEL`    | `INFO`    | Logging verbosity for the plugin. Common values: `DEBUG`, `INFO`, `WARNING`, `ERROR`. If not provided, it will use the value of DISPATCHARR_LOG_LEVEL. Defaults to info if `TMS_LOG_LEVEL` or `DISPATCHARR_LOG_LEVEL` is not provided                   | `TMS_LOG_LEVEL=DEBUG`                     |
| `TMS_IMAGE_PATH`   | *(unset)* | Path to a static image to serve when streams are maxed. If unset, a dynamic image is generated at runtime. If provided and using docker, you must mount that image to the path specified.   | `TMS_IMAGE_PATH=/app/assets/tms.png`      |
| `TMS_HOST`         | `0.0.0.0` | Host/IP for the internal HTTP server that serves the still image/TS stream.                                   | `TMS_HOST=0.0.0.0`                      |
| `TMS_PORT`         | `1337`    | TCP port for the internal HTTP server. Ensure the port is free or run a single instance per machine/process.  | `TMS_PORT=1337`                           |

## Development.
Feel free to fork, raise a PR or request features via the [Discussions](https://github.com/JamesWRC/Dispatcharr_Too_Many_Streams/discussions)
Im not a front end dev, so the HTML that gets rendered could <b>definitely</b> be improved.

How to develop:
- tested on windows WSL (Ubuntu)
1. Run the `./setup_dev.sh`
2. Restart VSCode.
3. Done. Happy plugin developing.


## Build.
To make a build, run: `./build.sh`


# Example

This example shows an example M3U account with a limit of 2 streams. With the third channel showing the 'TooManyStreams' stream, after all other streams fail.

##### M3U account
![m2u account profile](img/m3u.png)

##### Example with 2 streams and one extra showing the TooManyStreams stream
![Example with 2 streams and one extra showing the TooManyStreams stream](img/TooManyStreamsExample.png)
---
##### Too Many Streams
This will show all other streams that are currently in use and available to watch
![Larger image of what someone would see when there are Too Many Streams](img/TooManyStreamsExampleStream.png)