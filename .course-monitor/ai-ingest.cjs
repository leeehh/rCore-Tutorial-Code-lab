'use strict';
// Compatibility entry point; Python is the common desktop/container policy.
const {spawnSync}=require('node:child_process');
const path=require('node:path');
const result=spawnSync('python3',[path.join(__dirname,'ai-ingest.py')],{stdio:'inherit'});
if(result.error)throw result.error;
process.exitCode=result.status??1;