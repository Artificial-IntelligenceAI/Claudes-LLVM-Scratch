#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
# Fetch the JavaScript implementations used as references:
#   ref/node_modules/scratch-vm   the official interpreter (test oracle + benchmark)
#   ref/turbowarp/scratch-vm      TurboWarp's compiler fork (benchmark only)
# Neither is redistributed with this repository.
set -e
cd "$(dirname "$0")/../ref"
npm install --no-fund --no-audit
mkdir -p turbowarp
[ -d turbowarp/scratch-vm ] || git clone --depth 1 https://github.com/TurboWarp/scratch-vm.git turbowarp/scratch-vm
cd turbowarp/scratch-vm && npm install --omit=dev --no-fund --no-audit --ignore-scripts
