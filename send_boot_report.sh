#!/bin/bash

sleep 15

SUBJECT="NAC-BBS Online - $(hostname -I | awk '{print $1}')"

BODY=$(/home/nac-bbs/meshcore-bbs/boot_report.sh)

printf "%s\n" "$BODY" | mail \
  -s "$SUBJECT" \
  delgado@apollos-dev.com \
  treodelgado@gmail.com

