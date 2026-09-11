'use strict';
// Compatibility entry point; the Git hook calls the same Python implementation.
const {spawnSync}=require('node:child_process');
const path=require('node:path');
const result=spawnSync('python3',[path.join(__dirname,'export.py'),...process.argv.slice(2)],{stdio:'inherit'});
if(result.error)throw result.error;
process.exitCode=result.status??1;