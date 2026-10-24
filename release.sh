#!/bin/sh
# scripts/release.sh — FreeBSD-X release packaging
#
# Usage:
#   scripts/release.sh [--version <tag>] [--sign <secret.key>] [--build-dir <dir>]
#                      [--out-dir <dir>] [--catalog-url <url>]
#
# What it does:
#   1. (Optional) compile a release build if build-dir doesn't already have an xpkg binary
#   2. xpkg pack every apps/*.xapp → <out-dir>/<AppName>-<version>.xpkg
#   3. (Optional) sign each .xpkg with minisign using the provided secret key
#   4. Generate <out-dir>/catalog.json with SHA-256, size, and version metadata
#   5. (Optional) git tag <version> if --tag flag is passed
#
# Environment variables (all overridable via flags):
#   FREEBSDX_VERSION     — release tag, e.g. v1.0.0      (default: git describe)
#   FREEBSDX_SIGN_KEY    — path to minisign secret key   (default: none → skip signing)
#   FREEBSDX_BUILD_DIR   — meson build directory         (default: build)
#   FREEBSDX_OUT_DIR     — output directory for packages (default: dist)
#   FREEBSDX_CATALOG_URL — base URL for catalog download_url fields
#                          (default: https://pkg.freebsd-x.org/releases/<version>)

set -e

# ─── defaults ────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

VERSION="${FREEBSDX_VERSION:-}"
SIGN_KEY="${FREEBSDX_SIGN_KEY:-}"
BUILD_DIR="${FREEBSDX_BUILD_DIR:-${REPO_ROOT}/build}"
OUT_DIR="${FREEBSDX_OUT_DIR:-${REPO_ROOT}/dist}"
CATALOG_URL="${FREEBSDX_CATALOG_URL:-}"
DO_TAG=0

# ─── argument parsing ─────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
    case "$1" in
        --version)    VERSION="$2";     shift 2 ;;
        --sign)       SIGN_KEY="$2";    shift 2 ;;
        --build-dir)  BUILD_DIR="$2";   shift 2 ;;
        --out-dir)    OUT_DIR="$2";     shift 2 ;;
        --catalog-url) CATALOG_URL="$2"; shift 2 ;;
        --tag)        DO_TAG=1;         shift   ;;
        -h|--help)
            sed -n '2,20p' "$0" | sed 's/^# *//'
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

# ─── resolve version ──────────────────────────────────────────────────────────
if [ -z "${VERSION}" ]; then
    if command -v git >/dev/null 2>&1 && git -C "${REPO_ROOT}" rev-parse --git-dir >/dev/null 2>&1; then
        VERSION="$(git -C "${REPO_ROOT}" describe --tags --always --dirty 2>/dev/null || echo "dev")"
    else
        VERSION="dev"
    fi
fi

CATALOG_URL="${CATALOG_URL:-https://pkg.freebsd-x.org/releases/${VERSION}}"

echo "FreeBSD-X release packaging"
echo "  Version    : ${VERSION}"
echo "  Build dir  : ${BUILD_DIR}"
echo "  Output dir : ${OUT_DIR}"
echo "  Sign key   : ${SIGN_KEY:-<none — signing skipped>}"
echo "  Catalog URL: ${CATALOG_URL}"
echo ""

# ─── locate xpkg binary ───────────────────────────────────────────────────────
XPKG="${BUILD_DIR}/xpkg"
if [ ! -x "${XPKG}" ]; then
    echo "==> xpkg not found at ${XPKG}; running meson compile…"
    if ! command -v meson >/dev/null 2>&1; then
        echo "Error: meson not found. Install it or pass --build-dir pointing to a completed build." >&2
        exit 1
    fi
    if [ ! -f "${BUILD_DIR}/build.ninja" ]; then
        echo "==> No build.ninja in ${BUILD_DIR}; running meson setup…"
        meson setup "${BUILD_DIR}" "${REPO_ROOT}" \
            --buildtype=release \
            -Denable_vulkan_validation=false
    fi
    meson compile -C "${BUILD_DIR}" xpkg
fi
echo "==> xpkg: ${XPKG}"

# ─── create output directory ──────────────────────────────────────────────────
mkdir -p "${OUT_DIR}"

# ─── pack every .xapp ─────────────────────────────────────────────────────────
echo ""
echo "==> Packing .xapp bundles…"

PACKED=0
FAILED=0

for app_dir in "${REPO_ROOT}"/apps/*.xapp; do
    [ -d "${app_dir}" ] || continue
    app_name="$(basename "${app_dir}" .xapp)"
    out_pkg="${OUT_DIR}/${app_name}-${VERSION}.xpkg"

    printf "    %-24s → %s\n" "${app_name}" "$(basename "${out_pkg}")"
    if "${XPKG}" pack "${app_dir}" "${out_pkg}" >/dev/null 2>&1; then
        PACKED=$((PACKED + 1))
    else
        echo "      ERROR: pack failed for ${app_name}" >&2
        FAILED=$((FAILED + 1))
    fi
done

echo ""
echo "    Packed: ${PACKED}   Failed: ${FAILED}"

if [ "${FAILED}" -gt 0 ]; then
    echo "Error: ${FAILED} app(s) failed to pack." >&2
    exit 1
fi

# ─── sign bundles ─────────────────────────────────────────────────────────────
if [ -n "${SIGN_KEY}" ]; then
    echo ""
    echo "==> Signing bundles with ${SIGN_KEY}…"

    if ! command -v minisign >/dev/null 2>&1; then
        echo "Error: minisign not found. Install it: pkg install minisign" >&2
        exit 1
    fi

    for pkg in "${OUT_DIR}"/*.xpkg; do
        [ -f "${pkg}" ] || continue
        pkg_name="$(basename "${pkg}")"
        printf "    signing %-40s" "${pkg_name}"
        if "${XPKG}" sign "${pkg}" "${SIGN_KEY}" >/dev/null 2>&1; then
            echo "OK"
        else
            echo "FAILED" >&2
            echo "Error: signing failed for ${pkg_name}" >&2
            exit 1
        fi
    done
else
    echo ""
    echo "==> Signing skipped (no --sign key provided)."
    echo "    To sign: scripts/release.sh --sign /path/to/release.key"
fi

# ─── generate catalog.json ────────────────────────────────────────────────────
echo ""
echo "==> Generating catalog.json…"

CATALOG="${OUT_DIR}/catalog.json"
GENERATED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

# Build JSON manually (no python/jq dependency required at release time)
cat > "${CATALOG}" <<HEADER
{
  "catalog_version": 1,
  "platform": "FreeBSD-X",
  "generated_at": "${GENERATED_AT}",
  "release": "${VERSION}",
  "packages": [
HEADER

first=1
for pkg in "${OUT_DIR}"/*.xpkg; do
    [ -f "${pkg}" ] || continue
    pkg_file="$(basename "${pkg}")"
    # strip version suffix: AppName-v1.0.0.xpkg → AppName
    pkg_stem="$(echo "${pkg_file}" | sed "s/-${VERSION}\\.xpkg\$//")"
    pkg_id="com.freebsdx.${pkg_stem}"
    pkg_size="$(stat -f '%z' "${pkg}" 2>/dev/null || stat -c '%s' "${pkg}")"

    # SHA-256: try shasum (macOS/Linux), then sha256 (FreeBSD)
    if command -v sha256 >/dev/null 2>&1; then
        pkg_sha256="$(sha256 -q "${pkg}")"
    elif command -v shasum >/dev/null 2>&1; then
        pkg_sha256="$(shasum -a 256 "${pkg}" | awk '{print $1}')"
    else
        pkg_sha256="$(openssl dgst -sha256 -hex "${pkg}" | awk '{print $2}')"
    fi

    # minisig sidecar presence
    if [ -f "${pkg}.minisig" ]; then
        signed="true"
    else
        signed="false"
    fi

    if [ "${first}" -eq 0 ]; then
        printf ',\n' >> "${CATALOG}"
    fi
    first=0

    cat >> "${CATALOG}" <<ENTRY
    {
      "id": "${pkg_id}",
      "name": "${pkg_stem}",
      "version": "${VERSION}",
      "filename": "${pkg_file}",
      "size": ${pkg_size},
      "sha256": "${pkg_sha256}",
      "signed": ${signed},
      "download_url": "${CATALOG_URL}/${pkg_file}"
    }
ENTRY
done

cat >> "${CATALOG}" <<FOOTER

  ]
}
FOOTER

pkg_count="$(grep -c '"id":' "${CATALOG}" || true)"
echo "    catalog.json: ${pkg_count} package(s)"

# ─── print manifest ───────────────────────────────────────────────────────────
echo ""
echo "==> Output:"
ls -lh "${OUT_DIR}"

# ─── optional git tag ─────────────────────────────────────────────────────────
if [ "${DO_TAG}" -eq 1 ]; then
    echo ""
    echo "==> Tagging ${VERSION}…"
    git -C "${REPO_ROOT}" tag -a "${VERSION}" -m "FreeBSD-X release ${VERSION}"
    echo "    Tagged. Push with: git push origin ${VERSION}"
fi

echo ""
echo "Done. Release artifacts are in: ${OUT_DIR}"
