#!/usr/bin/env bash
# Run inside the pinned Python build container, from the repository root.
set -euo pipefail
out=firmware-build
base="$out/micropython.bin"
[[ -f software/main.py && -f "$base" ]] || { echo 'Missing software/main.py or firmware-build/micropython.bin' >&2; exit 1; }
work=$(mktemp -d "$out/.build.XXXXXX")
trap 'rm -rf "$work"' EXIT
# Refuse a different partition layout rather than overwrite an existing partition.
python emulator/validate_firmware.py base "$base"
mkdir "$work/rootfs" "$work/extracted"
heap_args=()
if [[ ${GUEST_HEAP:-0} == 1 ]]; then heap_args+=(--guest-heap); fi
python emulator/firmware_inputs.py stage "$work/rootfs" "${BADGE_VERSION:-2025}" "${heap_args[@]}"
if [[ -n $(find "$work/rootfs" ! -type d ! -type f -print -quit) ]]; then echo 'Unsupported filesystem entry' >&2; exit 1; fi
littlefs-python create "$work/rootfs" "$work/vfs.bin" --fs-size=2097152 --block-size=4096
littlefs-python extract "$work/vfs.bin" "$work/extracted" --block-size=4096
diff -qr "$work/rootfs" "$work/extracted"
esptool.py --chip esp32c3 merge_bin -o "$work/merged.bin" --flash_size keep --flash_mode keep --flash_freq keep 0x0 "$base" 0x200000 "$work/vfs.bin"
python emulator/validate_firmware.py merged "$work" --base "$base"
# Prepare every output before invalidating the old manifest or publishing.
mv "$work/merged.bin" "$work/bsides-badge-esp32c3.bin"
python -m pip freeze > "$work/build-requirements.txt"
(cd "$work" && sha256sum ../micropython.bin vfs.bin bsides-badge-esp32c3.bin | sed 's|  ../micropython.bin$|  micropython.bin|' > SHA256SUMS)
rm -f "$out/SHA256SUMS"
mv "$work/vfs.bin" "$out/vfs.bin"
mv "$work/bsides-badge-esp32c3.bin" "$out/bsides-badge-esp32c3.bin"
mv "$work/build-requirements.txt" "$out/build-requirements.txt"
mv "$work/SHA256SUMS" "$out/SHA256SUMS"
echo "Built $out/bsides-badge-esp32c3.bin"
