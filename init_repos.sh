#!/bin/sh
# init_repos.sh — initialise one git repository per FreeBSD-X component,
# then create a repo-tool-compatible manifest repo.
#
# Usage: sh scripts/init_repos.sh [--remote <base-url>]
#
#   --remote  Base URL for the remote origin of each repo.
#             Default: https://github.com/freebsd-x
#             Set to a local path (e.g. /srv/git) for a self-hosted setup.
#
# After this script runs every component has its own .git and an initial
# commit.  The manifest repo at manifest/ is updated with a pinned default.xml
# that records the HEAD SHA-1 of every repo so the tree is fully reproducible
# with `repo sync`.
#
# To use with the repo tool:
#   repo init -u <manifest-remote> -m default.xml
#   repo sync
#
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_BASE="${1:-}"
if [ "$1" = "--remote" ]; then
    REMOTE_BASE="$2"
    shift 2
fi
: "${REMOTE_BASE:=https://github.com/freebsd-x}"

# ── Colour helpers ────────────────────────────────────────────────────────────
green()  { printf '\033[0;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[0;33m%s\033[0m\n' "$*"; }
red()    { printf '\033[0;31m%s\033[0m\n' "$*"; }

# ── init_repo <rel-path> <remote-name> <commit-message> ──────────────────────
# Initialise a git repo at ROOT/<rel-path> if it does not already have one,
# stage all files, and create an initial commit.
init_repo() {
    local rel="$1"
    local remote_name="$2"
    local msg="$3"
    local dir="$ROOT/$rel"

    if [ ! -d "$dir" ]; then
        yellow "  SKIP  $rel  (directory not found)"
        return
    fi

    if [ -d "$dir/.git" ]; then
        yellow "  SKIP  $rel  (already a git repo)"
        return
    fi

    git -C "$dir" init -q
    git -C "$dir" remote add origin "${REMOTE_BASE}/${remote_name}.git"

    # Write a component-specific .gitignore if one doesn't exist.
    write_gitignore "$dir"

    git -C "$dir" add -A
    # Allow empty repos (e.g. stub service dirs with only a src/)
    if git -C "$dir" diff --cached --quiet; then
        yellow "  EMPTY $rel  (nothing to commit)"
    else
        git -C "$dir" commit -q -m "$msg

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
        green "  INIT  $rel"
    fi
}

# ── write_gitignore <abs-dir> ─────────────────────────────────────────────────
write_gitignore() {
    local dir="$1"
    [ -f "$dir/.gitignore" ] && return

    cat > "$dir/.gitignore" << 'EOF'
# Build artefacts
build/
*.o
*.a
*.so
*.spv
*.d

# Editor
.vscode/
.idea/
*.swp
*~

# Core dumps
*.core

# macOS
.DS_Store
EOF
}

# ── Manifest helpers ──────────────────────────────────────────────────────────
# Returns the HEAD SHA-1 of a repo, or "UNCOMMITTED" if the dir has no repo.
head_sha() {
    local dir="$ROOT/$1"
    if [ -d "$dir/.git" ]; then
        git -C "$dir" rev-parse HEAD 2>/dev/null || echo "UNCOMMITTED"
    else
        echo "UNCOMMITTED"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# REPO DEFINITIONS
# Format: init_repo  <path-relative-to-ROOT>  <remote-repo-name>  <commit-msg>
# ─────────────────────────────────────────────────────────────────────────────

echo ""
green "=== FreeBSD-X multi-repo initialisation ==="
echo ""

# ── Platform ──────────────────────────────────────────────────────────────────
echo "Platform:"
init_repo "include"              "freebsdx/platform/protocol"       "Initial commit: shared protocol headers"
init_repo "core/xserver"         "freebsdx/platform/xserver"        "Initial commit: compositor, DRM/KMS, XBus dispatcher"
init_repo "xwayland-bridge"      "freebsdx/platform/wayland-bridge" "Initial commit: Wayland compatibility bridge"
init_repo "sdk/xkit"             "freebsdx/platform/xkit"           "Initial commit: xkit UI toolkit"
init_repo "sdk/xrender"          "freebsdx/platform/xrender"        "Initial commit: xrender GPU 2D engine"

# ── Services ──────────────────────────────────────────────────────────────────
echo ""
echo "Services:"
init_repo "core/services/xaudio"   "freebsdx/services/xaudio"   "Initial commit: audio service"
init_repo "core/services/xnotify"  "freebsdx/services/xnotify"  "Initial commit: notification service"
init_repo "core/services/xportal"  "freebsdx/services/xportal"  "Initial commit: portal service (file picker, ZFS)"
init_repo "core/services/xnetwork" "freebsdx/services/xnetwork" "Initial commit: network service"
init_repo "core/services/xstorage" "freebsdx/services/xstorage" "Initial commit: storage service"
init_repo "core/services/xconfig"  "freebsdx/services/xconfig"  "Initial commit: configuration service"
init_repo "core/services/xsystem"  "freebsdx/services/xsystem"  "Initial commit: system stats service"
init_repo "core/services/xupdate"  "freebsdx/services/xupdate"  "Initial commit: software update service"

# ── Apps ──────────────────────────────────────────────────────────────────────
echo ""
echo "Apps:"
init_repo "apps/Terminal.xapp"    "freebsdx/apps/Terminal"    "Initial commit: Terminal app"
init_repo "apps/TextEditor.xapp"  "freebsdx/apps/TextEditor"  "Initial commit: TextEditor app"
init_repo "apps/Files.xapp"       "freebsdx/apps/Files"       "Initial commit: Files app"
init_repo "apps/Settings.xapp"    "freebsdx/apps/Settings"    "Initial commit: Settings app"
init_repo "apps/Calculator.xapp"  "freebsdx/apps/Calculator"  "Initial commit: Calculator app"
init_repo "apps/AppManager.xapp"  "freebsdx/apps/AppManager"  "Initial commit: AppManager app"
init_repo "apps/GpuDemo.xapp"     "freebsdx/apps/GpuDemo"     "Initial commit: GpuDemo reference app"
init_repo "apps/Installer.xapp"   "freebsdx/apps/Installer"   "Initial commit: Installer app"

# ── Shell ─────────────────────────────────────────────────────────────────────
echo ""
echo "Shell:"
init_repo "core/xshell" "freebsdx/platform/xshell" "Initial commit: xshell system shell + app launcher"

# ── Tools ─────────────────────────────────────────────────────────────────────
echo ""
echo "Tools:"
init_repo "tools" "freebsdx/tools" "Initial commit: developer tools (xpkg, diag, icon_gen)"

# ── Build root ────────────────────────────────────────────────────────────────
# The root repo owns meson.build, scripts/, tests/, iso_build/, ROADMAP.md.
# Sub-repos are excluded via .gitignore so they don't nest inside this repo.
echo ""
echo "Build root:"

ROOT_GITIGNORE="$ROOT/.gitignore"
cat > "$ROOT_GITIGNORE" << 'EOF'
# Sub-repos managed by repo tool — excluded from the build-root repo.
include/
core/xserver/
core/services/xaudio/
core/services/xnotify/
core/services/xportal/
core/services/xnetwork/
core/services/xstorage/
core/services/xconfig/
core/services/xsystem/
core/services/xupdate/
core/xshell/
xwayland-bridge/
sdk/xkit/
sdk/xrender/
apps/Terminal.xapp/
apps/TextEditor.xapp/
apps/Files.xapp/
apps/Settings.xapp/
apps/Calculator.xapp/
apps/AppManager.xapp/
apps/GpuDemo.xapp/
apps/Installer.xapp/
tools/

# Build artefacts
build/
*.o
*.a
*.so
*.spv
*.d
*.core

# Disk images (too large for git)
*.img
*.iso

# Logs
logs/

# Editor
.vscode/
.idea/
*.swp
*~
.DS_Store
EOF

if [ ! -d "$ROOT/.git" ]; then
    git -C "$ROOT" init -q
    git -C "$ROOT" remote add origin "${REMOTE_BASE}/freebsdx/build-root.git"
fi
git -C "$ROOT" add meson.build scripts/ tests/ ROADMAP.md start_freebsd_x.sh .gitignore 2>/dev/null || true
if ! git -C "$ROOT" diff --cached --quiet 2>/dev/null; then
    git -C "$ROOT" commit -q -m "Initial commit: build root (meson.build, scripts, tests)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
    green "  INIT  . (build-root)"
else
    yellow "  SKIP  . (build-root — nothing to commit)"
fi

# ── Generate manifest/default.xml ─────────────────────────────────────────────
echo ""
green "=== Writing manifest/default.xml ==="

mkdir -p "$ROOT/manifest"

# Collect HEAD SHAs for pinning.
SHA_PROTOCOL=$(head_sha "include")
SHA_XSERVER=$(head_sha "core/xserver")
SHA_BRIDGE=$(head_sha "xwayland-bridge")
SHA_XKIT=$(head_sha "sdk/xkit")
SHA_XRENDER=$(head_sha "sdk/xrender")
SHA_XAUDIO=$(head_sha "core/services/xaudio")
SHA_XNOTIFY=$(head_sha "core/services/xnotify")
SHA_XPORTAL=$(head_sha "core/services/xportal")
SHA_XNETWORK=$(head_sha "core/services/xnetwork")
SHA_XSTORAGE=$(head_sha "core/services/xstorage")
SHA_XCONFIG=$(head_sha "core/services/xconfig")
SHA_XSYSTEM=$(head_sha "core/services/xsystem")
SHA_XUPDATE=$(head_sha "core/services/xupdate")
SHA_TERMINAL=$(head_sha "apps/Terminal.xapp")
SHA_TEXTEDITOR=$(head_sha "apps/TextEditor.xapp")
SHA_FILES=$(head_sha "apps/Files.xapp")
SHA_SETTINGS=$(head_sha "apps/Settings.xapp")
SHA_CALCULATOR=$(head_sha "apps/Calculator.xapp")
SHA_APPMANAGER=$(head_sha "apps/AppManager.xapp")
SHA_GPUDEMO=$(head_sha "apps/GpuDemo.xapp")
SHA_INSTALLER=$(head_sha "apps/Installer.xapp")
SHA_XSHELL=$(head_sha "core/xshell")
SHA_TOOLS=$(head_sha "tools")
SHA_ROOT=$(head_sha ".")

cat > "$ROOT/manifest/default.xml" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!--
  FreeBSD-X manifest — generated by scripts/init_repos.sh
  Use with the Android repo tool:
    repo init -u ${REMOTE_BASE}/freebsdx/manifest.git -m default.xml
    repo sync
-->
<manifest>
  <remote name="origin" fetch="${REMOTE_BASE}" />
  <default revision="main" remote="origin" sync-j="4" />

  <!-- ── Platform ─────────────────────────────────────────────────────── -->
  <project name="freebsdx/platform/protocol"       path="include"        revision="${SHA_PROTOCOL}" />
  <project name="freebsdx/platform/xserver"        path="core/xserver"   revision="${SHA_XSERVER}" />
  <project name="freebsdx/platform/wayland-bridge" path="xwayland-bridge" revision="${SHA_BRIDGE}" />
  <project name="freebsdx/platform/xkit"           path="sdk/xkit"       revision="${SHA_XKIT}" />
  <project name="freebsdx/platform/xrender"        path="sdk/xrender"    revision="${SHA_XRENDER}" />
  <project name="freebsdx/platform/xshell"         path="core/xshell"     revision="${SHA_XSHELL}" />

  <!-- ── Services ─────────────────────────────────────────────────────── -->
  <project name="freebsdx/services/xaudio"   path="core/services/xaudio"   revision="${SHA_XAUDIO}" />
  <project name="freebsdx/services/xnotify"  path="core/services/xnotify"  revision="${SHA_XNOTIFY}" />
  <project name="freebsdx/services/xportal"  path="core/services/xportal"  revision="${SHA_XPORTAL}" />
  <project name="freebsdx/services/xnetwork" path="core/services/xnetwork" revision="${SHA_XNETWORK}" />
  <project name="freebsdx/services/xstorage" path="core/services/xstorage" revision="${SHA_XSTORAGE}" />
  <project name="freebsdx/services/xconfig"  path="core/services/xconfig"  revision="${SHA_XCONFIG}" />
  <project name="freebsdx/services/xsystem"  path="core/services/xsystem"  revision="${SHA_XSYSTEM}" />
  <project name="freebsdx/services/xupdate"  path="core/services/xupdate"  revision="${SHA_XUPDATE}" />

  <!-- ── Apps ──────────────────────────────────────────────────────────── -->
  <project name="freebsdx/apps/Terminal"   path="apps/Terminal.xapp"   revision="${SHA_TERMINAL}" />
  <project name="freebsdx/apps/TextEditor" path="apps/TextEditor.xapp" revision="${SHA_TEXTEDITOR}" />
  <project name="freebsdx/apps/Files"      path="apps/Files.xapp"      revision="${SHA_FILES}" />
  <project name="freebsdx/apps/Settings"   path="apps/Settings.xapp"   revision="${SHA_SETTINGS}" />
  <project name="freebsdx/apps/Calculator" path="apps/Calculator.xapp" revision="${SHA_CALCULATOR}" />
  <project name="freebsdx/apps/AppManager" path="apps/AppManager.xapp" revision="${SHA_APPMANAGER}" />
  <project name="freebsdx/apps/GpuDemo"    path="apps/GpuDemo.xapp"    revision="${SHA_GPUDEMO}" />
  <project name="freebsdx/apps/Installer"  path="apps/Installer.xapp"  revision="${SHA_INSTALLER}" />

  <!-- ── Tools + build root ────────────────────────────────────────────── -->
  <project name="freebsdx/tools"       path="tools" revision="${SHA_TOOLS}" />
  <project name="freebsdx/build-root"  path="."     revision="${SHA_ROOT}" />
  <project name="freebsdx/manifest"    path="manifest" revision="main" />
</manifest>
EOF

green "  WRITTEN manifest/default.xml"

# Commit the manifest repo itself.
if [ ! -d "$ROOT/manifest/.git" ]; then
    git -C "$ROOT/manifest" init -q
    git -C "$ROOT/manifest" remote add origin "${REMOTE_BASE}/freebsdx/manifest.git"
fi
git -C "$ROOT/manifest" add default.xml
if ! git -C "$ROOT/manifest" diff --cached --quiet 2>/dev/null; then
    git -C "$ROOT/manifest" commit -q -m "Initial manifest: pin all repos at initial commit

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
    green "  INIT  manifest"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
green "=== Done ==="
echo ""
echo "Repos initialised under: $ROOT"
echo ""
echo "Next steps:"
echo "  1. Create bare remotes and push:"
echo "       repo forall -c 'git push -u origin main'"
echo "  2. Or push the manifest repo first and use repo tool:"
echo "       cd \$ROOT && repo init -u ${REMOTE_BASE}/freebsdx/manifest.git"
echo "       repo sync"
echo ""
echo "To see all repos and their HEAD:"
echo "       repo forall -c 'echo \$REPO_PATH: \$(git log -1 --oneline)'"
echo ""
