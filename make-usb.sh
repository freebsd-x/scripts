#!/bin/sh
# scripts/make-usb.sh
# Generates a bootable FreeBSD-X USB image (.img).
# MUST BE RUN AS ROOT.

set -e

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORK_DIR="$ROOT_DIR/iso_build"
ROOTFS="$WORK_DIR/rootfs"
IMG_NAME="$ROOT_DIR/FreeBSD-X-1.0-GoldMaster.img"

echo "=========================================="
echo " FreeBSD-X 1.0 USB Image Generator"
echo "=========================================="

if [ ! -d "$ROOTFS" ]; then
    echo "Rootfs not found at $ROOTFS."
    echo "Please run ./scripts/make-iso.sh first to stage the rootfs."
    exit 1
fi

echo "=> [1/4] Preparing EFI Boot Partition..."
rm -rf "$WORK_DIR/efi_staging"
mkdir -p "$WORK_DIR/efi_staging/EFI/BOOT"
cp "$ROOTFS/boot/loader.efi" "$WORK_DIR/efi_staging/EFI/BOOT/BOOTX64.EFI"
makefs -t msdos -s 32m -o fat_type=16 "$WORK_DIR/efi.img" "$WORK_DIR/efi_staging"

echo "=> [2/4] Generating UFS Root Filesystem..."
makefs -t ffs -o label=FREEBSDX "$WORK_DIR/rootfs.ufs" "$ROOTFS"

echo "=> [3/4] Wrapping into GPT Disk Image..."
mkimg -s gpt \
    -b "$ROOTFS/boot/pmbr" \
    -p efi:="$WORK_DIR/efi.img" \
    -p freebsd-boot:="$ROOTFS/boot/gptboot" \
    -p freebsd-ufs:="$WORK_DIR/rootfs.ufs" \
    -o "$IMG_NAME"

echo "=> [4/4] Cleaning up temporary image files..."
rm -rf "$WORK_DIR/efi_staging" "$WORK_DIR/efi.img" "$WORK_DIR/rootfs.ufs"

echo "=========================================="
echo " USB IMAGE GENERATION COMPLETE"
echo " Location: $IMG_NAME"
echo " Usage: dd if=$(basename "$IMG_NAME") of=/dev/daX bs=1m status=progress"
echo "=========================================="
