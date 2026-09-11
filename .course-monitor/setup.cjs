const path = require('node:path');
const { execFileSync } = require('node:child_process');
execFileSync('python3', [path.resolve(__dirname, '..', 'course.py'), 'install', '--skip-extension'], { stdio: 'inherit' });
