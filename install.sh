#!/usr/bin/env bash
# Symlinks this repo into Touch Portal's plugins/ directory. No sudo, no root-owned
# files -- everything here runs as your own user. There's no config file to create
# or fill in first: VM Name and XML Directory are set inside Touch Portal itself,
# under Settings -> Plugins -> libvirt Bridge, once the plugin is loaded.
set -euo pipefail

REPO_ROOT=$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")
PLUGIN_FOLDER_NAME="libvirt-bridge"   # must match the %TP_PLUGIN_FOLDER% path in entry.tp
TP_PLUGINS_DIR="${HOME}/.config/TouchPortal/plugins"

mkdir -p "${TP_PLUGINS_DIR}"

TARGET="${TP_PLUGINS_DIR}/${PLUGIN_FOLDER_NAME}"
if [ -L "${TARGET}" ]; then
  ln -sfn "${REPO_ROOT}" "${TARGET}"
elif [ -e "${TARGET}" ]; then
  echo "Refusing to overwrite non-symlink ${TARGET} -- remove it manually first." >&2
  exit 1
else
  ln -s "${REPO_ROOT}" "${TARGET}"
fi

echo "Linked ${TARGET} -> ${REPO_ROOT}"
echo "Restart Touch Portal for it to pick up the new plugin."
echo "After restarting, check Settings -> Plugins in the app -- confirm it's"
echo "connected, and fill in VM Name and XML Directory there. This repo's"
echo "daemon.log records connection and configuration activity."
