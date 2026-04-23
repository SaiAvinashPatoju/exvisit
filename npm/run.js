// run.js — shim that forwards all CLI arguments to the downloaded native binary.
//
// This is the entry point declared in package.json "bin.exvisit-mcp".
// After `npm install -g exvisit-mcp`, running `exvisit-mcp` in a terminal
// invokes this file, which delegates to the platform binary in ./bin/.

'use strict';

const path        = require('path');
const { spawnSync } = require('child_process');

const binName = process.platform === 'win32' ? 'exvisit-mcp.exe' : 'exvisit-mcp';
const binPath = path.join(__dirname, 'bin', binName);

if (!require('fs').existsSync(binPath)) {
  console.error(
    `[exvisit-mcp] Binary not found at ${binPath}.\n` +
    'Try reinstalling: npm install -g exvisit-mcp'
  );
  process.exit(1);
}

const result = spawnSync(binPath, process.argv.slice(2), { stdio: 'inherit' });
process.exit(result.status ?? 1);
