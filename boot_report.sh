#!/bin/bash

HOSTNAME=$(hostname)
BOOT_TIME=$(date '+%b %d, %Y %I:%M %p %Z')

PRIMARY_IP=$(hostname -I | awk '{print $1}')

ETH_IP=$(ip -4 addr show eth0 2>/dev/null \
    | awk '/inet / {print $2}' \
    | cut -d/ -f1)

WIFI_IP=$(ip -4 addr show wlan0 2>/dev/null \
    | awk '/inet / {print $2}' \
    | cut -d/ -f1)

UPTIME=$(uptime -p)

OS=$(grep '^PRETTY_NAME=' /etc/os-release \
    | cut -d= -f2- \
    | tr -d '"')

KERNEL=$(uname -r)

DISK=$(df -h / | awk 'NR==2 {print $5}')

MEMORY=$(free | awk '/Mem:/ {
    printf "%.0f%%", $3/$2 * 100
}')

if command -v vcgencmd >/dev/null 2>&1; then
    CPU_TEMP=$(vcgencmd measure_temp \
        | sed 's/temp=//')
else
    CPU_TEMP=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null)
    if [ -n "$CPU_TEMP" ]; then
        CPU_TEMP=$(awk "BEGIN {
            printf \"%.1f C\", $CPU_TEMP / 1000
        }")
    else
        CPU_TEMP="Unavailable"
    fi
fi

BBS_STATUS=$(systemctl is-active nac-bbs 2>/dev/null || echo "not found")
WEATHER_STATUS=$(systemctl is-active nac-bbs-weather.timer 2>/dev/null || echo "not found")
SYNC_STATUS=$(systemctl is-active nac-bbs-activity-sync.timer 2>/dev/null || echo "not found")

cat <<EOF
NAC-BBS Boot Report

Hostname: $HOSTNAME
Boot time: $BOOT_TIME

Primary IP: ${PRIMARY_IP:-Unavailable}
Ethernet IP: ${ETH_IP:-Not connected}
Wi-Fi IP: ${WIFI_IP:-Not connected}

Uptime: $UPTIME
OS: $OS
Kernel: $KERNEL

NAC-BBS: $BBS_STATUS
Weather timer: $WEATHER_STATUS
Activity sync timer: $SYNC_STATUS

Disk usage: $DISK
Memory usage: $MEMORY
CPU temperature: $CPU_TEMP
EOF
