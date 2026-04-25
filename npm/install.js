// install.js — postinstall script for the exvisit-mcp npm package.
//
// Downloads the correct pre-compiled exvisit-mcp binary from the GitHub
// Releases page that matches the installed package version and the current
// OS/architecture, then saves it to npm/bin/ so run.js can invoke it.
//
// Security measures
// ─────────────────
// • Download URL is assembled from a hardcoded GitHub base (no user input).
// • Redirect targets are validated against an allowlist of trusted GitHub CDN
//   hostnames before following.
// • The binary filename is validated against a strict safe-character regex
//   before being used in any filesystem call.
// • HTTPS only — http:// URLs are rejected.

'use strict';

const https  = require('https');
const fs     = require('fs');
const path   = require('path');
const { URL } = require('url');

const PKG     = require('./package.json');
const VERSION = PKG.version;
const REPO    = 'SaiAvinashPatoju/exvisit';

// Mapping from Node.js (platform, arch) to the asset names published by
// .github/workflows/rust-release.yml.
const BINARY_MAP = {
  'linux-x64':    'exvisit-mcp-linux-amd64',
  'darwin-arm64': 'exvisit-mcp-macos-arm64',
  'darwin-x64':   'exvisit-mcp-macos-x64',
  'win32-x64':    'exvisit-mcp-windows-amd64.exe',
};

const platformKey = `${process.platform}-${process.arch}`;
const assetName   = BINARY_MAP[platformKey];

if (!assetName) {
  console.error(
    `[exvisit-mcp] Unsupported platform: ${platformKey}\n` +
    `Supported: ${Object.keys(BINARY_MAP).join(', ')}\n` +
    'You can build from source: https://github.com/SaiAvinashPatoju/exvisit/tree/main/rust'
  );
  process.exit(1);
}

// Validate asset name: only alphanumeric, hyphen, underscore, dot.
// This prevents any path-traversal attack on the filename component.
if (!/^[A-Za-z0-9._-]+$/.test(assetName)) {
  throw new Error(`[exvisit-mcp] Unexpected asset name format: "${assetName}" — aborting`);
}

const binDir   = path.join(__dirname, 'bin');
const binName  = process.platform === 'win32' ? 'exvisit-mcp.exe' : 'exvisit-mcp';
const binPath  = path.join(binDir, binName);
const assetUrl = `https://github.com/${REPO}/releases/download/v${VERSION}/${assetName}`;

fs.mkdirSync(binDir, { recursive: true });

console.log(`[exvisit-mcp] Downloading ${assetName} v${VERSION}...`);

download(assetUrl, binPath, () => {
  if (process.platform !== 'win32') {
    fs.chmodSync(binPath, 0o755);
  }
  console.log(`[exvisit-mcp] Installed: ${binPath}`);
  console.log('[exvisit-mcp] Add this to your claude_desktop_config.json:');
  console.log(JSON.stringify({
    mcpServers: {
      exvisit: {
        command: 'exvisit-mcp',
        args: [],
      },
    },
  }, null, 2));
});

// ── Helpers ───────────────────────────────────────────────────────────────────

// Trusted hostnames for GitHub release binary downloads.
const ALLOWED_HOSTS = new Set([
  'github.com',
  'objects.githubusercontent.com',
  'objects.github.com',
  'codeload.github.com',
]);

/**
 * Download `url` to `dest`, following HTTPS redirects only to trusted hosts.
 * Calls `done()` when the file is fully written.
 */
function download(url, dest, done, _redirectCount = 0) {
  if (_redirectCount > 5) {
    throw new Error('[exvisit-mcp] Too many redirects — aborting download');
  }

  const parsed = new URL(url);
  if (parsed.protocol !== 'https:') {
    throw new Error(`[exvisit-mcp] Refusing non-HTTPS URL: ${url}`);
  }

  https.get(url, (res) => {
    // Follow redirects (GitHub releases 302 → objects.githubusercontent.com).
    if (res.statusCode === 301 || res.statusCode === 302) {
      const location = res.headers.location;
      if (!location) {
        throw new Error('[exvisit-mcp] Redirect with no Location header');
      }
      const redirectHost = new URL(location).hostname;
      if (!ALLOWED_HOSTS.has(redirectHost)) {
        throw new Error(
          `[exvisit-mcp] Redirect to untrusted host "${redirectHost}" — aborting`
        );
      }
      // Consume the body so the socket is released before following.
      res.resume();
      download(location, dest, done, _redirectCount + 1);
      return;
    }

    if (res.statusCode !== 200) {
      throw new Error(
        `[exvisit-mcp] Download failed: HTTP ${res.statusCode} from ${url}`
      );
    }

    const file = fs.createWriteStream(dest);
    res.pipe(file);
    file.on('finish', () => file.close(done));
    file.on('error', (err) => {
      fs.unlink(dest, () => {});
      throw err;
    });
  }).on('error', (err) => {
    fs.unlink(dest, () => {});
    throw err;
  });
}
