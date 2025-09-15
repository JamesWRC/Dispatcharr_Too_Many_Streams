
#!/bin/bash
# Build script for Dispatcharr_Too_Many_Streams
# Create a staging folder. As the zip needs to contain a folder named Too_Many_Streams, with the source inside it.
mkdir -p zips/staging/too_many_streams
rm -rf zips/staging/too_many_streams/*

# Copy necessary files
cp plugin.py zips/staging/too_many_streams
cp __init__.py zips/staging/too_many_streams
cp -r src zips/staging/too_many_streams

# Create the zip file
(cd zips/staging && zip -r ../too_many_streams.zip too_many_streams)

