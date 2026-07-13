#!/usr/bin/env bash
# Compatibility stub — prefer ./boot/network.sh or ./boot/realm.sh
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/boot/network.sh" "$@"
