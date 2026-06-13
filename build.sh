#!/bin/bash
# Build script for Dispatcharr_Too_Many_Streams
# Create a staging folder. As the zip needs to contain a folder named Too_Many_Streams, with the source inside it.
set -e

mkdir -p zips/staging/too_many_streams
rm -rf zips/staging/too_many_streams/*

# Copy necessary files
cp plugin.py zips/staging/too_many_streams
cp __init__.py zips/staging/too_many_streams
cp plugin.json zips/staging/too_many_streams
cp -r src zips/staging/too_many_streams

# Drop any stray byte-compiled files so they never ship in the artifact.
find zips/staging/too_many_streams -name '__pycache__' -type d -prune -exec rm -rf {} +

# Create the zip file. Remove any previous archive first: `zip` UPDATES in place,
# so without this, files deleted from src/ would linger as stale entries.
rm -f zips/too_many_streams.zip
(cd zips/staging && zip -r ../too_many_streams.zip too_many_streams)

# Keep the tracked artifact at the repo root in sync with the fresh build.
cp zips/too_many_streams.zip too_many_streams.zip
