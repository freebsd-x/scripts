#!/bin/sh
# scripts/make-iso.sh
# Generates a bootable FreeBSD-X ISO image.
# MUST BE RUN AS ROOT.

set -e

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORK_DIR="$ROOT_DIR/iso_build"
ROOTFS="$WORK_DIR/rootfs"
ISO_NAME="$ROOT_DIR/FreeBSD-X-1.0-GoldMaster.iso"

echo "=========================================="
echo " FreeBSD-X 1.0 Gold Master ISO Generator"
echo "=========================================="

# 1. Prepare Workspace
echo "=> Cleaning workspace..."
rm -rf "$WORK_DIR"
mkdir -p "$ROOTFS/usr/local/bin" 
mkdir -p "$ROOTFS/usr/local/lib" 
mkdir -p "$ROOTFS/usr/local/apps" 
mkdir -p "$ROOTFS/usr/local/etc/rc.d"
mkdir -p "$ROOTFS/etc"
mkdir -p "$ROOTFS/boot"

if [ "$1" != "--mock" ]; then
    echo "=> Downloading FreeBSD Base and Kernel..."
    fetch -o "$WORK_DIR/base.txz" https://download.freebsd.org/releases/amd64/amd64/15.0-RELEASE/base.txz
    fetch -o "$WORK_DIR/kernel.txz" https://download.freebsd.org/releases/amd64/amd64/15.0-RELEASE/kernel.txz
    echo "=> Extracting FreeBSD system..."
    tar -xf "$WORK_DIR/base.txz" -C "$ROOTFS" --no-same-owner 2>/dev/null || true
    tar -xf "$WORK_DIR/kernel.txz" -C "$ROOTFS" --no-same-owner 2>/dev/null || true
else
    echo "=> MOCK MODE: Skipping 500MB download."
    cp /boot/cdboot "$ROOTFS/boot/cdboot" 2>/dev/null || touch "$ROOTFS/boot/cdboot"
fi

# 2. Inject Core OS Binaries
echo "=> Injecting Core OS Stack..."
cp "$ROOT_DIR/build/freebsdx" "$ROOTFS/usr/local/bin/"
cp "$ROOT_DIR/build/xshell" "$ROOTFS/usr/local/bin/"
cp "$ROOT_DIR/build/libfreebsdx-ui.so" "$ROOTFS/usr/local/lib/"
cp "$ROOT_DIR/build/freebsdx-verify" "$ROOTFS/usr/local/bin/"
cp "$ROOT_DIR/build/gui_auditor" "$ROOTFS/usr/local/bin/"

# 3. Inject and Structure Application Bundles
echo "=> Structuring Application Ecosystem..."

# Helper to bundle an app
bundle_app() {
    NAME=$1
    BINARY=$2
    echo "   -> Bundling $NAME.xapp..."
    cp -r "$ROOT_DIR/apps/$NAME.xapp" "$ROOTFS/usr/local/apps/"
    mkdir -p "$ROOTFS/usr/local/apps/$NAME.xapp/bin"
    cp "$ROOT_DIR/build/$BINARY" "$ROOTFS/usr/local/apps/$NAME.xapp/bin/"
}

bundle_app "DemoApp" "demoapp"
bundle_app "Settings" "settings_app"
bundle_app "Calculator" "calculator"
bundle_app "Installer" "installer"
bundle_app "Terminal" "terminal"

# 4. System Configuration
echo "=> Configuring Init and Boot..."

cat << 'EOF' > "$ROOTFS/usr/local/etc/rc.d/freebsdx"
#!/bin/sh
# PROVIDE: freebsdx
# REQUIRE: DAEMON FILESYSTEMS seatd
# KEYWORD: shutdown
. /etc/rc.subr
name="freebsdx"
rcvar="freebsdx_enable"
command="/usr/local/bin/freebsdx"
pidfile="/var/run/freebsdx.pid"
command_args="> /var/log/freebsdx.log 2>&1 &"
load_rc_config $name
run_rc_command "$1"
EOF
chmod +x "$ROOTFS/usr/local/etc/rc.d/freebsdx"

cat << 'EOF' > "$ROOTFS/etc/rc.conf"
hostname="freebsdx-v1"
ifconfig_DEFAULT="DHCP"
kld_list="i915kms amdgpu"
seatd_enable="YES"
freebsdx_enable="YES"
EOF

# Disable default login prompt on ttyv0
if [ -f "$ROOTFS/etc/ttys" ]; then
    sed -i '' 's/^ttyv0.*getty.*/ttyv0\t"\/usr\/libexec\/getty Pc"\t\tcons25\toff\tsecure/' "$ROOTFS/etc/ttys"
else
    echo "ttyv0	\"/usr/libexec/getty Pc\"		cons25	off	secure" > "$ROOTFS/etc/ttys"
fi

echo "=========================================="
echo " GOLD MASTER ROOTFS STAGED"
echo " Location: $ROOTFS"
echo "=========================================="

echo "=> [5/5] Generating CD9660 ISO Image..."
if [ -f "$ROOTFS/boot/cdboot" ]; then
    makefs -t cd9660 -o rockridge -o bootimage=i386\;$ROOTFS/boot/cdboot -o no-emul-boot -o label=FREEBSDX "$ISO_NAME" "$ROOTFS"
else
    makefs -t cd9660 -o rockridge -o label=FREEBSDX "$ISO_NAME" "$ROOTFS"
fi

echo "=========================================="
echo " ISO GENERATION COMPLETE"
echo " Location: $ISO_NAME"
echo "=========================================="
