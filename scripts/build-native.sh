#!/usr/bin/env bash
#
# Build the native toolchain (libmspub + librevenge) and assemble the
# standalone app's bin/ directory. Designed to run inside an MSYS2 MINGW64
# shell — locally or on a GitHub Actions windows runner.
#
# Prerequisites (install via pacman, see BUILD.md / the CI workflow):
#   mingw-w64-x86_64-{gcc,make,pkgconf,boost,icu,zlib}  make  m4
#   plus a Rust toolchain (for svg2pdf) — installed separately.
#
# Result: standalone/bin/ populated with pub2xhtml.exe, svg2pdf.exe and all
# required mingw64 DLLs (resolved dynamically via ldd, so ICU/GCC version
# bumps are picked up automatically).

set -euo pipefail

LIBREVENGE_VER=0.0.5
LIBMSPUB_VER=0.1.4

# Mirror candidates per package, tried in order. The dev-www LibreOffice mirror
# is canonical but intermittently 404s for librevenge, so SourceForge is listed
# as a fallback. Each download is validated as a real xz archive before use.
LIBREVENGE_URLS="
https://dev-www.libreoffice.org/src/librevenge-${LIBREVENGE_VER}.tar.xz
https://downloads.sourceforge.net/project/libwpd/librevenge/librevenge-${LIBREVENGE_VER}/librevenge-${LIBREVENGE_VER}.tar.xz
"
LIBMSPUB_URLS="
https://dev-www.libreoffice.org/src/libmspub-${LIBMSPUB_VER}.tar.xz
https://downloads.sourceforge.net/project/libmspub/libmspub/libmspub-${LIBMSPUB_VER}/libmspub-${LIBMSPUB_VER}.tar.xz
"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
build_dir="$repo_root/build"
prefix="$build_dir/prefix"     # local install prefix (keeps system clean)
bin_out="$repo_root/standalone/bin"

mkdir -p "$build_dir" "$prefix" "$bin_out"
export PKG_CONFIG_PATH="$prefix/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
export PATH="$prefix/bin:$PATH"

fetch_tarball() { # name version url-list -> echoes path to a validated .tar.xz
  local name="$1" ver="$2" urls="$3"
  local out="$build_dir/$name-$ver.tar.xz"
  if [ -f "$out" ] && xz -t "$out" 2>/dev/null; then
    echo "$out"; return 0
  fi
  local url
  for url in $urls; do
    echo "  fetching $url" >&2
    if curl -fsSL -o "$out" "$url" && xz -t "$out" 2>/dev/null; then
      echo "$out"; return 0
    fi
    rm -f "$out"
  done
  echo "ERROR: no working mirror for $name-$ver.tar.xz" >&2
  return 1
}

build_autotools() { # name version url-list configure-extra-args...
  local name="$1" ver="$2" urls="$3"; shift 3
  local srcdir="$build_dir/$name-$ver"
  local tarball; tarball="$(fetch_tarball "$name" "$ver" "$urls")"
  rm -rf "$srcdir"
  tar -C "$build_dir" -xf "$tarball"
  ( cd "$srcdir"
    ./configure --prefix="$prefix" --disable-static --disable-werror \
                --without-docs "$@"
    make -j"$(nproc)"
    make install )
}

echo "==> Building librevenge $LIBREVENGE_VER"
build_autotools librevenge "$LIBREVENGE_VER" "$LIBREVENGE_URLS" --disable-tests

echo "==> Building libmspub $LIBMSPUB_VER"
# GCC 13+ dropped transitive <cstdint>; the 2018-era headers need it forced.
CXXFLAGS="${CXXFLAGS:-} -O2 -include cstdint" \
  build_autotools libmspub "$LIBMSPUB_VER" "$LIBMSPUB_URLS"

echo "==> Locating svg2pdf.exe"
svg2pdf="${SVG2PDF_EXE:-}"          # CI passes an explicit path
[ -z "$svg2pdf" ] && svg2pdf="$(command -v svg2pdf || true)"
[ -z "$svg2pdf" ] && [ -x "$HOME/.cargo/bin/svg2pdf.exe" ] && svg2pdf="$HOME/.cargo/bin/svg2pdf.exe"
[ -n "$svg2pdf" ] && [ -x "$svg2pdf" ] || { echo "svg2pdf not found — run: cargo install svg2pdf-cli (or set SVG2PDF_EXE)" >&2; exit 1; }

echo "==> Assembling $bin_out"
rm -rf "$bin_out"; mkdir -p "$bin_out"
cp "$prefix/bin/pub2xhtml.exe" "$prefix/bin/pub2raw.exe" "$bin_out/"
cp "$svg2pdf" "$bin_out/svg2pdf.exe"

# Copy every mingw64 DLL that pub2xhtml depends on (recursively resolved).
ldd "$bin_out/pub2xhtml.exe" \
  | awk '/\/mingw64\// {print $3}' \
  | sort -u \
  | while read -r dll; do cp -n "$dll" "$bin_out/"; done

echo "==> bin/ contents:"
ls -1 "$bin_out"
echo "Done."
