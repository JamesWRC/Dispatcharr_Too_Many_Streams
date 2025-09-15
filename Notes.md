






2025-09-14 02:27:16,113 INFO ts_proxy.url_utils Fetching channel ID 542d6b29-8978-4c63-ab1a-2536a2d27bee
2025-09-14 02:27:16,116 INFO ts_proxy.views [client_1757816836116_4918] Requested stream for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee
2025-09-14 02:27:16,116 DEBUG ts_proxy.views [client_1757816836116_4918] Client connected with user agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36
2025-09-14 02:27:16,117 INFO ts_proxy.views [client_1757816836116_4918] Starting channel 542d6b29-8978-4c63-ab1a-2536a2d27bee initialization
2025-09-14 02:27:16,117 INFO ts_proxy.url_utils Fetching channel ID 542d6b29-8978-4c63-ab1a-2536a2d27bee
2025-09-14 02:27:16,135 DEBUG ts_proxy.url_utils Executing URL pattern replacement:
2025-09-14 02:27:16,135 DEBUG ts_proxy.url_utils   base URL: https://pia.cx/live/bhxU28Co/TjwUwx7i/200151194.ts
2025-09-14 02:27:16,135 DEBUG ts_proxy.url_utils   search: ^(.*)$
2025-09-14 02:27:16,136 DEBUG ts_proxy.url_utils   replace: $1
2025-09-14 02:27:16,136 DEBUG ts_proxy.url_utils   safe replace: \1
2025-09-14 02:27:16,136 INFO ts_proxy.url_utils Generated stream url: https://pia.cx/live/bhxU28Co/TjwUwx7i/200151194.ts
2025-09-14 02:27:16,137 INFO ts_proxy.views [client_1757816836116_4918] Successfully obtained stream for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee
2025-09-14 02:27:16,139 INFO ts_proxy.views Channel 542d6b29-8978-4c63-ab1a-2536a2d27bee using stream ID 495, m3u account profile ID 2
2025-09-14 02:27:16,141 INFO ts_proxy Created initial metadata with stream_id 495 for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee
2025-09-14 02:27:16,142 DEBUG ts_proxy Verified stream_id 495 is now set in Redis
2025-09-14 02:27:16,144 DEBUG ts_proxy.client_manager Started heartbeat thread for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee (interval: 1s)
2025-09-14 02:27:16,144 DEBUG ts_proxy.client_manager Started client heartbeat thread for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee (interval: 1s)
2025-09-14 02:27:16,145 INFO ts_proxy.server Set early initializing state for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee
2025-09-14 02:27:16,146 INFO ts_proxy.server Worker 5d162250d533:205 acquired ownership of channel 542d6b29-8978-4c63-ab1a-2536a2d27bee
2025-09-14 02:27:16,146 INFO ts_proxy.server Worker 5d162250d533:205 is now the owner of channel 542d6b29-8978-4c63-ab1a-2536a2d27bee
2025-09-14 02:27:16,146 INFO ts_proxy.server Storing stream_id 495 in metadata for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee
2025-09-14 02:27:16,146 INFO ts_proxy.server Verified stream_id 495 is set in Redis for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee
2025-09-14 02:27:16,148 DEBUG ts_proxy.server Created StreamBuffer for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee
2025-09-14 02:27:16,150 INFO ts_proxy.stream_manager Initialized stream manager for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee with stream ID 495
2025-09-14 02:27:16,150 INFO ts_proxy.stream_manager Initialized stream manager for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee
2025-09-14 02:27:16,150 INFO ts_proxy.server Created StreamManager for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee with stream ID 495
2025-09-14 02:27:16,151 DEBUG ts_proxy.client_manager Started heartbeat thread for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee (interval: 1s)
2025-09-14 02:27:16,151 DEBUG ts_proxy.client_manager Started client heartbeat thread for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee (interval: 1s)
2025-09-14 02:27:16,152 INFO ts_proxy.server Started stream manager thread for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee
2025-09-14 02:27:16,152 INFO ts_proxy.stream_manager Starting stream for URL: https://pia.cx/live/bhxU28Co/TjwUwx7i/200151194.ts for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee
2025-09-14 02:27:16,153 INFO ts_proxy.stream_manager Connection attempt 1/3 for URL: https://pia.cx/live/bhxU28Co/TjwUwx7i/200151194.ts for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee
2025-09-14 02:27:16,153 DEBUG ts_proxy.stream_manager Building transcode command for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee
2025-09-14 02:27:16,153 INFO ts_proxy.url_utils Fetching channel ID 542d6b29-8978-4c63-ab1a-2536a2d27bee
2025-09-14 02:27:16,155 INFO ts_proxy.server Channel 542d6b29-8978-4c63-ab1a-2536a2d27bee state transition: initializing -> connecting
2025-09-14 02:27:16,155 INFO ts_proxy.server Channel 542d6b29-8978-4c63-ab1a-2536a2d27bee in connecting state - will start grace period after connection
2025-09-14 02:27:16,187 DEBUG ts_proxy.stream_manager Starting transcode process: ['ffmpeg', '-user_agent', 'VLC/3.0.21 LibVLC/3.0.21', '-i', 'https://pia.cx/live/bhxU28Co/TjwUwx7i/200151194.ts', '-c', 'copy', '-f', 'mpegts', 'pipe:1'] for channel: 542d6b29-8978-4c63-ab1a-2536a2d27bee
2025-09-14 02:27:16,189 DEBUG ts_proxy.stream_manager Started stderr reader thread for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee
2025-09-14 02:27:16,214 DEBUG ts_proxy.stream_manager FFmpeg stderr for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee: ffmpeg version 8.0 Copyright (c) 2000-2025 the FFmpeg developers
2025-09-14 02:27:16,214 DEBUG ts_proxy.stream_manager FFmpeg stderr for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee: built with gcc 13 (Ubuntu 13.3.0-6ubuntu2~24.04)
2025-09-14 02:27:16,215 DEBUG ts_proxy.stream_manager FFmpeg stderr for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee: configuration: --disable-debug --disable-doc --disable-ffplay --enable-alsa --enable-cuda-llvm --enable-cuvid --enable-ffprobe --enable-gpl --enable-libaom --enable-libass --enable-libdav1d --enable-libfdk_aac --enable-libfontconfig --enable-libfreetype --enable-libfribidi --enable-libharfbuzz --enable-libkvazaar --enable-liblc3 --enable-libmp3lame --enable-libopencore-amrnb --enable-libopencore-amrwb --enable-libopenjpeg --enable-libopus --enable-libplacebo --enable-librav1e --enable-librist --enable-libshaderc --enable-libsrt --enable-libsvtav1 --enable-libtheora --enable-libv4l2 --enable-libvidstab --enable-libvmaf --enable-libvorbis --enable-libvpl --enable-libvpx --enable-libvvenc --enable-libwebp --enable-libx264 --enable-libx265 --enable-libxml2 --enable-libxvid --enable-libzimg --enable-libzmq --enable-nonfree --enable-nvdec --enable-nvenc --enable-opencl --enable-openssl --enable-stripping --enable-vaapi --enable-vdpau --enable-version3 --enable-vulkan
2025-09-14 02:27:16,215 DEBUG ts_proxy.stream_manager FFmpeg stderr for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee: libavutil      60.  8.100 / 60.  8.100
2025-09-14 02:27:16,215 DEBUG ts_proxy.stream_manager FFmpeg stderr for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee: libavcodec     62. 11.100 / 62. 11.100
2025-09-14 02:27:16,215 DEBUG ts_proxy.stream_manager FFmpeg stderr for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee: libavformat    62.  3.100 / 62.  3.100
2025-09-14 02:27:16,215 DEBUG ts_proxy.stream_manager FFmpeg stderr for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee: libavdevice    62.  1.100 / 62.  1.100
2025-09-14 02:27:16,215 DEBUG ts_proxy.stream_manager FFmpeg stderr for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee: libavfilter    11.  4.100 / 11.  4.100
2025-09-14 02:27:16,216 DEBUG ts_proxy.stream_manager FFmpeg stderr for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee: libswscale      9.  1.100 /  9.  1.100
2025-09-14 02:27:16,216 DEBUG ts_proxy.stream_manager FFmpeg stderr for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee: libswresample   6.  1.100 /  6.  1.100
2025-09-14 02:27:16,256 INFO ts_proxy.views [client_1757816836116_4918] Successfully initialized channel 542d6b29-8978-4c63-ab1a-2536a2d27bee
2025-09-14 02:27:16,257 DEBUG ts_proxy.client_manager Storing user agent 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36' for client client_1757816836116_4918
2025-09-14 02:27:16,260 DEBUG ts_proxy.server Owner received client_connected event for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee
2025-09-14 02:27:16,260 DEBUG ts_proxy.server Owner received client_connected event for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee
2025-09-14 02:27:16,269 DEBUG asyncio Using selector: EpollSelector
2025-09-14 02:27:16,276 DEBUG daphne.ws_protocol Sent WebSocket packet to client for ['127.0.0.1', 52100]
2025-09-14 02:27:16,277 INFO ts_proxy.client_manager New client connected: client_1757816836116_4918 (local: 1, total: 1)
2025-09-14 02:27:16,278 INFO ts_proxy.views [client_1757816836116_4918] Client registered with channel 542d6b29-8978-4c63-ab1a-2536a2d27bee
2025-09-14 02:27:16,279 INFO ts_proxy.stream_generator [client_1757816836116_4918] Stream generator started, channel_ready=False
2025-09-14 02:27:16,279 INFO ts_proxy.stream_generator [client_1757816836116_4918] Channel 542d6b29-8978-4c63-ab1a-2536a2d27bee ready, starting normal streaming
2025-09-14 02:27:16,279 INFO ts_proxy.stream_generator [client_1757816836116_4918] Starting stream at index 0 (buffer at 0)
2025-09-14 02:27:16,690 DEBUG ts_proxy.stream_manager Buffer filling for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee: 0/4 chunks
2025-09-14 02:27:17,012 DEBUG ts_proxy.server Refreshed metadata TTL for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee
2025-09-14 02:27:17,014 DEBUG ts_proxy.server Channel 542d6b29-8978-4c63-ab1a-2536a2d27bee still connecting - not checking for clients yet
2025-09-14 02:27:17,088 DEBUG celery.beat beat: Waking up in 5.00 seconds.
2025-09-14 02:27:17,190 DEBUG ts_proxy.stream_manager Buffer filling for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee: 0/4 chunks
2025-09-14 02:27:17,691 DEBUG ts_proxy.stream_manager Buffer filling for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee: 0/4 chunks


Streams seem to be triggers from here
Dispatcharr\apps\proxy





2025-09-14 02:30:07,263 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:30:07,266 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:30:07,266 ERROR ts_proxy.url_utils No stream available for channel 84fa74a3-6465-44b9-9316-20f08a27a0a1: All M3U profiles have reached maximum connection limits
2025-09-14 02:30:07,272 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:30:07,275 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:30:07,277 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:30:07,280 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:30:07,280 INFO ts_proxy.views [client_1757817007250_3680] Waiting 0.5s for a connection to become available (attempt 1/3)
2025-09-14 02:30:07,286 DEBUG ts_proxy.stream_generator [client_1757816979980_4326] Retrieved 1 chunks (255868 bytes) from index 75 to 75
2025-09-14 02:30:07,287 DEBUG ts_proxy.stream_generator [client_1757816979980_4326] Sent chunk 75 (255868 bytes) for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee to client
2025-09-14 02:30:07,504 DEBUG ts_proxy.server Refreshed metadata TTL for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee
2025-09-14 02:30:07,733 DEBUG ts_proxy Updated stream stats in database for stream 495: {'ffmpeg_output_bitrate': 3822.3}
2025-09-14 02:30:07,733 DEBUG ts_proxy.stream_manager FFmpeg stats for channel 542d6b29-8978-4c63-ab1a-2536a2d27bee: - Speed: 1.64x, FFmpeg FPS: 49.0, Actual FPS: 29.9, Output Bitrate: 3822.3 kbps
2025-09-14 02:30:07,780 INFO ts_proxy.url_utils Fetching channel ID 84fa74a3-6465-44b9-9316-20f08a27a0a1
2025-09-14 02:30:07,791 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:30:07,793 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:30:07,796 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:30:07,800 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:30:07,800 ERROR ts_proxy.url_utils No stream available for channel 84fa74a3-6465-44b9-9316-20f08a27a0a1: All M3U profiles have reached maximum connection limits
2025-09-14 02:30:07,807 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:30:07,810 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:30:07,812 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:30:07,815 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:30:07,815 INFO ts_proxy.views [client_1757817007250_3680] Waiting 1.0s for a conn









2025-09-14 02:32:35,375 INFO ts_proxy.url_utils Fetching channel ID 84fa74a3-6465-44b9-9316-20f08a27a0a1
2025-09-14 02:32:35,378 INFO ts_proxy.views [client_1757817155377_6608] Requested stream for channel 84fa74a3-6465-44b9-9316-20f08a27a0a1
2025-09-14 02:32:35,378 DEBUG ts_proxy.views [client_1757817155377_6608] Client connected with user agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36
2025-09-14 02:32:35,378 INFO ts_proxy.views [client_1757817155377_6608] Starting channel 84fa74a3-6465-44b9-9316-20f08a27a0a1 initialization
2025-09-14 02:32:35,378 INFO ts_proxy.url_utils Fetching channel ID 84fa74a3-6465-44b9-9316-20f08a27a0a1
2025-09-14 02:32:35,389 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:32:35,393 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:32:35,397 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:32:35,401 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:32:35,401 ERROR ts_proxy.url_utils No stream available for channel 84fa74a3-6465-44b9-9316-20f08a27a0a1: All M3U profiles have reached maximum connection limits
2025-09-14 02:32:35,407 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:32:35,409 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:32:35,412 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:32:35,412 DEBUG ts_proxy.server Refreshed metadata TTL for channel bff437ca-8e79-4189-9980-e597732e246f
2025-09-14 02:32:35,414 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:32:35,415 INFO ts_proxy.views [client_1757817155377_6608] Waiting 0.5s for a connection to become available (attempt 1/3)
2025-09-14 02:32:35,616 DEBUG ts_proxy Updated stream stats in database for stream 508: {'ffmpeg_output_bitrate': 2756.4}
2025-09-14 02:32:35,616 DEBUG ts_proxy.stream_manager FFmpeg stats for channel bff437ca-8e79-4189-9980-e597732e246f: - Speed: 1.81x, FFmpeg FPS: 54.0, Actual FPS: 29.8, Output Bitrate: 2756.4 kbps
2025-09-14 02:32:35,915 INFO ts_proxy.url_utils Fetching channel ID 84fa74a3-6465-44b9-9316-20f08a27a0a1
2025-09-14 02:32:35,922 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:32:35,925 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:32:35,928 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:32:35,931 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:32:35,931 ERROR ts_proxy.url_utils No stream available for channel 84fa74a3-6465-44b9-9316-20f08a27a0a1: All M3U profiles have reached maximum connection limits
2025-09-14 02:32:35,937 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:32:35,940 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:32:35,943 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:32:35,945 DEBUG apps.channels.models Profile 2 at max connections: 1/1
2025-09-14 02:32:35,946 INFO ts_proxy.views [client_1757817155377_6608] Waiting 1.0s for a connection to become available (attempt 2/3)
2025-09-14 02:32:36,022 DEBUG ts_proxy.stream_buffer Added 1 chunks (255868 bytes each) to Redis for channel bff437ca-8e79-4189-9980-e597732e246f at index 51
2025-09-14 02:32:36,031 DEBUG ts_proxy.stream_buffer Added 1 chunks (255868 bytes each) to Redis for channel bff437ca-8e79-4189-9980-e597732e246f at index 52
2025-09-14 02:32:36,036 DEBUG ts_proxy.stream_buffer Added 1 chunks (255868 bytes each) to Redis for channel bff437ca-8e79-4189-9980-e597732e246f at index 53
2025-09-14 02:32:36,061 DEBUG ts_proxy.stream_generator [client_1757817131940_7168] Retrieved 3 chunks (767604 bytes) from index 51 to 53
2025-09-14 02:32:36,063 DEBUG ts_proxy.stream_generator [client_1757817131940_7168] Sent chunk 51 (255868 bytes) for channel bff437ca-8e79-4189-9980-e597732e246f to client
2025-09-14 02:32:36,065 DEBUG ts_proxy.stream_generator [client_1757817131940_7168] Sent chunk 52 (255868 bytes) for channel bff437ca-8e79-4189-9980-e597732e246f to 


-- SENDS HttpStatusCodeInvalid