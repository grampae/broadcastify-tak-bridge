#!/bin/bash
ALIAS="6a02416c4078c"
PATH_NAME="cam_mqt_downtown2"
MEDIAMTX_API="http://127.0.0.1:9997"

URL=$(curl -s "https://ipcamlive.com/player/getcamerastreamstate.php?alias=$ALIAS" | \
  python3 -c "import sys,json; d=json.load(sys.stdin)['details']; print(d['address']+'streams/'+d['streamid']+'/stream.m3u8')")

if [ -z "$URL" ]; then exit 1; fi

curl -s -X PATCH "$MEDIAMTX_API/v3/config/paths/patch/$PATH_NAME" \
  -H "Content-Type: application/json" \
  -d "{\"source\": \"$URL\"}"
